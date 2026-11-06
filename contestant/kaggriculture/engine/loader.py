"""受约束的 agent 加载器：静态扫描 + import 白名单 + 内置函数裁剪。

提交运行在独立子进程里，裁判能按时限杀掉卡死的策略，策略退出也不影响裁判。
沙箱本身仍是防呆不防恶；需要防恶意代码时要再加容器隔离。
"""
import builtins
import json
import os
import queue
import random
import re
import subprocess
import sys
import threading
import time
import types

ALLOWED_MODULES = {
    "math", "random", "itertools", "collections", "heapq", "bisect",
    "functools", "statistics", "json",
}
REMOVED_BUILTINS = {"open", "eval", "exec", "compile", "input", "__import__",
                    "breakpoint", "memoryview", "globals", "locals", "vars"}
MAX_LINES = None  # 行数不设上限
MAX_BYTES = None  # 字节数不设上限（按需求取消提交大小限制）
LOAD_TIMEOUT = 60.0  # 加载提交（含模块顶层代码和 Agent()）的时限
TIMEOUT_GRACE = 0.05

BANNED_PATTERNS = [
    (r"\bimport\s+(os|sys|subprocess|socket|urllib|requests|pathlib|shutil|"
     r"threading|multiprocessing|ctypes|asyncio|tempfile|importlib|inspect|pickle|"
     r"marshal|webbrowser|smtplib|http|ftplib|telnetlib|ssl|signal|resource|pty)\b",
     "禁用模块"),
    (r"\.__class__|__subclasses__|__globals__|__bases__|__mro__", "反射逃逸原语"),
    (r"open\s*\(|write\s*\(|remove\s*\(|rmdir\s*\(", "文件写删原语"),
    (r"\bexec\s*\(|\beval\s*\(|\bcompile\s*\(", "动态执行"),
    (r"arena\b|rules\b|_new_farm|_do_hire|interpreter\s*\(|random\.seed", "疑似探测/操纵裁判引擎"),
    (r"getpass|keyring|environ", "环境探测"),
]


class SandboxError(Exception):
    pass


def check_source(src: str):
    problems = []
    if MAX_LINES is not None:
        n_lines = src.count("\n") + 1
        if n_lines > MAX_LINES:
            problems.append("行数 %d 超过上限 %d" % (n_lines, MAX_LINES))
    if MAX_BYTES is not None and len(src.encode("utf-8")) > MAX_BYTES:
        problems.append("字节数超过上限 %d" % MAX_BYTES)
    for pat, why in BANNED_PATTERNS:
        m = re.search(pat, src)
        if m:
            problems.append("静态扫描命中[%s]: %r" % (why, m.group(0)[:40]))
    return problems


def _make_builtins():
    safe = {k: v for k, v in vars(builtins).items() if k not in REMOVED_BUILTINS}

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        root = name.split(".")[0]
        if level != 0 or root not in ALLOWED_MODULES:
            raise ImportError("沙箱拒绝 import %r（白名单外）" % name)
        return builtins.__import__(name, globals, locals, fromlist, level)

    safe["__import__"] = guarded_import
    return safe


def load_trusted(path: str):
    """可信 agent：不受沙箱与静态扫描约束。"""
    import types as _t
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    mod = _t.ModuleType("trusted_agent")
    mod.__file__ = os.path.abspath(path)
    ns = {"__name__": "trusted_agent", "__file__": mod.__file__}
    exec(compile(src, path, "exec"), ns)
    if "act" in ns and callable(ns["act"]):
        fn = ns["act"]
    elif "Agent" in ns:
        fn = ns["Agent"]().act
    else:
        raise SandboxError("参考agent未提供 act(observation)")
    label = os.path.splitext(os.path.basename(path))[0]
    return label, (lambda obs: fn(obs))


class PolicyProcess:
    """子进程里的提交。按行交换 JSON；超时或进程退出后不再可用。"""

    def __init__(self, path, seed):
        env = {**os.environ, "PYTHONHASHSEED": "0"}
        self.proc = subprocess.Popen(
            [sys.executable, "-u", os.path.abspath(__file__), "--worker", os.path.abspath(path), str(seed)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", bufsize=1, env=env)
        self.q = queue.Queue()
        threading.Thread(target=_pump, args=(self.proc.stdout, self.q), daemon=True).start()
        self.dead = False
        self.timeout = None
        reply = self._wait(LOAD_TIMEOUT)
        if reply is None or "load_error" in reply:
            self.close()
            raise SandboxError(reply["load_error"] if reply else "提交加载超时或进程退出")

    def set_timeout(self, seconds):
        self.timeout = seconds

    def _wait(self, timeout):
        try:
            line = self.q.get(timeout=timeout)
        except queue.Empty:
            line = None
        if line is None:
            self.close()
            return None
        try:
            reply = json.loads(line)
        except (ValueError, RecursionError):
            return {"error": "回复无法解析"}
        return reply if isinstance(reply, dict) else {"error": "回复不是对象"}

    def __call__(self, observation):
        if self.dead:
            raise RuntimeError("策略进程已退出")
        try:
            self.proc.stdin.write(json.dumps(observation, separators=(",", ":")) + "\n")
            self.proc.stdin.flush()
        except OSError:
            self.close()
            raise RuntimeError("策略进程已退出")
        limit = None if self.timeout is None else self.timeout + TIMEOUT_GRACE
        started = time.perf_counter()
        reply = self._wait(limit)
        if reply is None:
            over = limit is not None and time.perf_counter() - started >= limit
            raise TimeoutError("策略超时") if over else RuntimeError("策略进程已退出")
        if "error" in reply:
            raise RuntimeError(reply["error"])
        return reply.get("action")

    def close(self):
        self.dead = True
        try:
            self.proc.kill()
            self.proc.wait(timeout=5)
        except Exception:
            pass
        try:
            self.proc.stdin.close()  # stdout 留给读线程，它读到 EOF 后自己结束
        except Exception:
            pass

    def __del__(self):
        if not getattr(self, "dead", True):
            self.close()


def _pump(stream, q):
    try:
        for line in stream:
            q.put(line)
    except Exception:
        pass
    q.put(None)


def load_agent(path: str):
    """启动子进程加载提交，返回 (label, callable(observation)->action)。"""
    with open(path, "r", encoding="utf-8") as f:
        problems = check_source(f.read())
    if problems:
        raise SandboxError("; ".join(problems))
    # 子进程种子来自调用方当前的随机状态，调用方先 random.seed 就能复现。
    fn = PolicyProcess(path, random.getrandbits(62))
    return os.path.splitext(os.path.basename(path))[0], fn


def _load_inprocess(path: str):
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    problems = check_source(src)
    if problems:
        raise SandboxError("; ".join(problems))
    mod = types.ModuleType("submitted_agent")
    mod.__file__ = os.path.abspath(path)
    ns = {"__name__": "submitted_agent", "__file__": mod.__file__,
          "__builtins__": _make_builtins()}
    exec(compile(src, path, "exec"), ns)
    if "act" in ns and callable(ns["act"]):
        fn = ns["act"]
    elif "Agent" in ns:
        inst = ns["Agent"]()
        fn = inst.act
    else:
        raise SandboxError("未找到 act(observation) 函数或 Agent.act 方法")
    return fn


class Attr(dict):
    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            raise AttributeError(k)


def _attr(v):
    if isinstance(v, dict):
        return Attr({k: _attr(x) for k, x in v.items()})
    if isinstance(v, list):
        return [_attr(x) for x in v]
    return v


def _worker(path, seed):
    """子进程入口。协议走复制出来的 stdout，提交里的 print 进空设备。"""
    out = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    null = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null, 1)
    sys.stdin.reconfigure(encoding="utf-8")

    def send(obj):
        out.write(json.dumps(obj, separators=(",", ":")) + "\n")
        out.flush()

    random.seed(seed)
    try:
        fn = _load_inprocess(path)
    except Exception as e:
        send({"load_error": "%s: %s" % (type(e).__name__, e)})
        return
    random.seed(seed)
    send({"ok": True})
    for line in sys.stdin:
        try:
            send({"action": fn(_attr(json.loads(line)))})
        except Exception as e:
            send({"error": "%s: %s" % (type(e).__name__, e)})


if __name__ == "__main__" and sys.argv[1:2] == ["--worker"]:
    _worker(sys.argv[2], int(sys.argv[3]))
