"""cache_duel 对战引擎：一局 = 一个 attacker bot 对一个 defender bot。

协议见 README.md。每个 bot 是一个 Python 文件，
以子进程运行，stdin/stdout 每行一个 JSON。
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import threading
import subprocess
import sys
import time
from dataclasses import dataclass, field

STEP_TIMEOUT = 1.0      # 单步硬超时（秒）
TIME_POOL = 5.0         # 时间池（秒）：独立于单步，单步超出 1s 的部分从池扣，池空后超 1s 即超时
TOTAL_BUDGET = 20.0     # 每个 bot 每局总思考时间（秒）


class BotError(Exception):
    pass


class Bot:
    def __init__(self, path: str, init: dict):
        self.path = path
        self.used = 0.0
        self.pool = TIME_POOL
        self.dead = False
        self.faults = 0
        env = {**os.environ, "PYTHONHASHSEED": "0"}
        self.proc = subprocess.Popen(
            [sys.executable, "-u", os.path.abspath(path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, env=env, cwd=os.path.dirname(os.path.abspath(path)),
        )
        self.q: queue.Queue = queue.Queue()
        threading.Thread(target=self._reader, daemon=True).start()
        self.ask(init)  # 握手：bot 需回一行（任意 JSON，如 {}）

    def _reader(self):  # 跨平台（Windows 管道不支持 select）
        for line in self.proc.stdout:
            self.q.put(line)
        self.q.put("")

    def ask(self, msg: dict):
        if self.dead:
            return None
        try:
            self.proc.stdin.write(json.dumps(msg, separators=(",", ":")) + "\n")
            self.proc.stdin.flush()
            t0 = time.perf_counter()
            limit = min(STEP_TIMEOUT + self.pool, max(0.0, TOTAL_BUDGET - self.used))
            try:
                line = self.q.get(timeout=limit)
            except queue.Empty:
                raise BotError("timeout")
            dt = time.perf_counter() - t0
            self.used += dt
            self.pool = max(0.0, self.pool - max(0.0, dt - STEP_TIMEOUT))
            if not line:
                raise BotError("eof")
            return json.loads(line)
        except (BotError, OSError, ValueError, RecursionError):
            self.dead = True
            self.kill()
            return None

    def kill(self):
        try:
            self.proc.kill()
        except Exception:
            pass


def belady_misses(trace: list[int], cap: int) -> int:
    """离线最优（Belady/MIN）缺失数。"""
    import heapq
    n = len(trace)
    nxt = [0] * n
    last: dict[int, int] = {}
    for i in range(n - 1, -1, -1):
        nxt[i] = last.get(trace[i], n + i)  # 不再出现时给一个唯一的大值
        last[trace[i]] = i
    cache: dict[int, int] = {}  # key -> 下一次出现位置
    heap: list[tuple[int, int]] = []  # (-next, key)，惰性删除
    misses = 0
    for i, k in enumerate(trace):
        if k in cache:
            cache[k] = nxt[i]
            heapq.heappush(heap, (-nxt[i], k))
            continue
        misses += 1
        if len(cache) >= cap:
            while True:
                nn, kk = heapq.heappop(heap)
                if kk in cache and cache[kk] == -nn:
                    del cache[kk]
                    break
        cache[k] = nxt[i]
        heapq.heappush(heap, (-nxt[i], k))
    return misses


def role_seed(seed: int, role: str) -> int:
    """按角色派生种子，一方无法从自己收到的种子推出对方的随机流。"""
    h = hashlib.sha256(f"{seed}|{role}".encode()).hexdigest()
    return int(h[:8], 16)


def is_key(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


@dataclass
class MatchResult:
    attacker: str
    defender: str
    config: dict
    def_misses: int
    opt_misses: int
    def_score: float          # OPT缺失 / 防守方缺失 ∈ (0,1]，越大防守越好
    faults: dict = field(default_factory=dict)


def play(attacker_path: str, defender_path: str, cache_size: int, universe: int,
         length: int, seed: int) -> MatchResult:
    cfg = {"cache_size": cache_size, "universe": universe, "length": length, "seed": seed}
    att = Bot(attacker_path, {"role": "attacker", **cfg, "seed": role_seed(seed, "attacker")})
    dfd = Bot(defender_path, {"role": "defender", **cfg, "seed": role_seed(seed, "defender")})
    cache: set[int] = set()
    insert_order: list[int] = []  # 用于非法驱逐时的惩罚：驱逐最新放入的
    trace: list[int] = []
    misses = 0
    last_hit = None
    att_faults = def_faults = 0
    for t in range(length):
        a = att.ask({"t": t, "last_hit": last_hit, "time_pool": round(att.pool, 3)})
        k = a.get("key") if isinstance(a, dict) else None
        if not is_key(k) or not (0 <= k < universe):
            att_faults += 1
            k = t % (cache_size + 1)  # 攻击方失误：退化为固定扫描
        trace.append(k)
        hit = k in cache
        need_evict = (not hit) and len(cache) >= cache_size
        d = dfd.ask({"t": t, "key": k, "hit": hit, "need_evict": need_evict, "time_pool": round(dfd.pool, 3)})
        if not hit:
            misses += 1
            if need_evict:
                e = d.get("evict") if isinstance(d, dict) else None
                if not is_key(e) or e not in cache:
                    def_faults += 1
                    e = insert_order[-1]  # 惩罚：驱逐最新放入的
                cache.discard(e)
                insert_order.remove(e)
            cache.add(k)
            insert_order.append(k)
        last_hit = hit
    att.ask({"t": length, "end": True}); dfd.ask({"t": length, "end": True})
    att.kill(); dfd.kill()
    opt = belady_misses(trace, cache_size)
    return MatchResult(
        attacker=attacker_path, defender=defender_path, config=cfg,
        def_misses=misses, opt_misses=opt, def_score=opt / misses if misses else 1.0,
        faults={"attacker": att_faults, "defender": def_faults,
                "attacker_dead": att.dead, "defender_dead": dfd.dead},
    )
