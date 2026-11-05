"""Frühlingsbeginn（春之始） 评测与数据模块。"""
from __future__ import annotations

import os
import lzma
import random
from typing import Any

from common import CompileResult, compile_cpp, run_sandboxed

PROBLEM_ID = "fruehlingsbeginn"
PROBLEM_DIR_NAME = "fruehlingsbeginn"
TITLE = "Frühlingsbeginn（春之始）"
TIME_LIMIT_MS = 1400
MEMORY_LIMIT_MB = 512

SUBTASKS = [
    {'id': 1, 'points': 5.0, 'name': '签到：所有区间长度均为 1'},
    {'id': 2, 'points': 4.5, 'name': '半开区间语义：恰好相接 vs 只差 1 点的缺口'},
    {'id': 3, 'points': 5.0, 'name': '边界越界 1 格与分离约束陷阱'},
    {'id': 4, 'points': 5.0, 'name': '超大覆盖/重叠≠包含'},
    {'id': 5, 'points': 5.0, 'name': '可重集：重复插入与部分删除'},
    {'id': 6, 'points': 5.0, 'name': '同起点多长度：贪心/只存最长或最短的错解'},
    {'id': 7, 'points': 6.0, 'name': '超长接力链与全域大跨度询问'},
    {'id': 8, 'points': 6.0, 'name': '时间维对抗：答案反复翻转、生命周期多尺度'},
    {'id': 9, 'points': 4.5, 'name': '询问密集 / 修改密集 / 重复询问缓存陷阱'},
    {'id': 10, 'points': 4.0, 'name': '分块边界缺口与高密度小步长综合'},
    {'id': 11, 'points': 10.0, 'name': '超长接力与末端单格缺口'},
    {'id': 12, 'points': 10.0, 'name': '多重副本的生命周期'},
    {'id': 13, 'points': 10.0, 'name': '同起点长短区间与越界'},
    {'id': 14, 'points': 10.0, 'name': '跨缺口大区间与包含约束'},
    {'id': 15, 'points': 10.0, 'name': '块边界翻转与缓存失效'},
]


def read_data_bytes(path: str) -> bytes:
    opener = lzma.open if str(path).endswith('.xz') else open
    with opener(path, 'rb') as f:
        return f.read()


def validate_input(text: str) -> dict[str, int]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise ValueError("Empty input")
    head = lines[0].split()
    if len(head) != 2:
        raise ValueError("First line must have 2 integers: n m")
    n, m = int(head[0]), int(head[1])
    if not (2 <= n <= 4_490_000):
        raise ValueError(f"n={n} out of range [2, 4490000]")
    if not (1 <= m <= 1_690_000):
        raise ValueError(f"m={m} out of range [1, 1690000]")
    if len(lines) != m + 1:
        raise ValueError(f"Expected {m + 1} lines, got {len(lines)}")

    inserted: set[int] = set()
    deleted: set[int] = set()
    queries = 0
    for idx in range(1, m + 1):
        parts = lines[idx].split()
        op = int(parts[0])
        if op == 1:
            if len(parts) != 3:
                raise ValueError(f"Op #{idx} (type 1) expects 3 ints")
            l, r = int(parts[1]), int(parts[2])
            if not (1 <= l < r <= n):
                raise ValueError(f"Op #{idx} invalid interval [{l}, {r}) for n={n}")
            inserted.add(idx)
        elif op == 2:
            if len(parts) != 2:
                raise ValueError(f"Op #{idx} (type 2) expects 2 ints")
            t = int(parts[1])
            if t not in inserted or t >= idx:
                raise ValueError(f"Op #{idx} deletes non-type-1 or future op t={t}")
            if t in deleted:
                raise ValueError(f"Op #{idx} deletes already deleted op t={t}")
            deleted.add(t)
        elif op == 3:
            if len(parts) != 3:
                raise ValueError(f"Op #{idx} (type 3) expects 3 ints")
            l, r = int(parts[1]), int(parts[2])
            if not (1 <= l < r <= n):
                raise ValueError(f"Op #{idx} invalid query [{l}, {r}) for n={n}")
            queries += 1
        else:
            raise ValueError(f"Op #{idx} unknown op={op}")
    return {"n": n, "m": m, "queries": queries}


def check_output(user_stdout: str, ans_text: str) -> tuple[bool, str]:
    exp = ans_text.split()
    got = user_stdout.split()
    if len(got) != len(exp):
        return False, f"Answer count mismatch: expected {len(exp)}, got {len(got)}"
    for i, (g, e) in enumerate(zip(got, exp), 1):
        if g != e:
            return False, f"Query #{i} mismatch: expected '{e}', got '{g}'"
    return True, f"All {len(exp)} queries matched"


def compile_submission(src_path: str, build_dir: str, root_dir: str = '') -> CompileResult:
    out_exe = os.path.join(build_dir, "sol_bin")
    return compile_cpp([src_path], out_exe, stack_mb=MEMORY_LIMIT_MB)


def run_case(exe_path: str, case: dict[str, Any], tests_dir: str, tl_ms: int = TIME_LIMIT_MS) -> dict[str, Any]:
    in_path = os.path.join(tests_dir, case["in"])
    out_path = os.path.join(tests_dir, case["out"])
    stdin_bytes = read_data_bytes(in_path)
    ans_text = read_data_bytes(out_path).decode("utf-8")

    rr = run_sandboxed(
        exe_path=exe_path,
        stdin_bytes=stdin_bytes,
        time_limit_ms=tl_ms,
        memory_limit_mb=MEMORY_LIMIT_MB,
        handshake=False,
    )

    base = {
        "name": case["name"],
        "subtasks": case.get("subtasks", [1]),
        "is_sample": case.get("is_sample", False),
        "time_ms": round(rr.cpu_ms, 2),
        "wall_ms": round(rr.wall_ms, 2),
        "mem_mb": round(rr.peak_mem_mb, 2),
    }

    if rr.peak_mem_mb > MEMORY_LIMIT_MB:
        return {**base, "verdict": "MLE", "ratio": 0.0, "msg": f"Memory {rr.peak_mem_mb:.1f} MB > {MEMORY_LIMIT_MB} MB"}
    if rr.timed_out or rr.cpu_ms > tl_ms:
        return {**base, "verdict": "TLE", "ratio": 0.0, "msg": f"Time {rr.cpu_ms:.1f} ms (wall {rr.wall_ms:.1f} ms) > {tl_ms} ms"}
    if rr.ole:
        return {**base, "verdict": "OLE", "ratio": 0.0, "msg": "Output limit exceeded"}
    if rr.returncode != 0:
        return {**base, "verdict": "RE", "ratio": 0.0, "msg": f"Exit code {rr.returncode}"}

    user_str = rr.stdout.decode("utf-8", errors="replace")
    ok, msg = check_output(user_str, ans_text)
    return {
        **base,
        "verdict": "AC" if ok else "WA",
        "ratio": 1.0 if ok else 0.0,
        "msg": msg,
    }



# ---- 时间/内存效率权重（2026-11-01）----
# 每个通过的测试点得分率再乘效率系数 E（对数刻度）：
#   ft = clamp(log(t/T0) / log(TL/T0), 0, 1)        t=标准毫秒用时（9950X），TL=时限
#   fm = clamp(log(m/M0) / log(ML/M0), 0, 1)        m=峰值内存 MB，ML=内存限制
#   E  = (1 - TIME_WEIGHT*ft) * (1 - MEM_WEIGHT*fm)
# 用时 ≤T0、内存 ≤M0 不扣分；顶到时限保留 1-TIME_WEIGHT，顶到内存限制保留 1-MEM_WEIGHT，两者相乘（最低 0.1×0.1=0.01）。
# 组分 = 组分值 × 组内平均(得分率 × E)；另报 raw_score（只看正确性）。
import math as _math
TIME_WEIGHT = 0.90
MEM_WEIGHT = 0.90
TIME_FREE_MS = 200
MEM_FREE_MB = 64


def _logfrac(x: float, lo: float, hi: float) -> float:
    if x <= lo or hi <= lo:
        return 0.0
    return min(1.0, _math.log(x / lo) / _math.log(hi / lo))


def efficiency(c: dict) -> float:
    ft = _logfrac(float(c.get("time_ms") or 0.0), TIME_FREE_MS, float(TIME_LIMIT_MS))
    fm = _logfrac(float(c.get("mem_mb") or 0.0), MEM_FREE_MB, float(MEMORY_LIMIT_MB))
    return max(0.0, (1.0 - TIME_WEIGHT * ft) * (1.0 - MEM_WEIGHT * fm))


def score_results(case_results, manifest, include_samples=False):
    res = _score_core(case_results, manifest, include_samples, True)
    raw = _score_core(case_results, manifest, include_samples, False)
    res["raw_score"] = raw["score"]
    scored = [c for c in case_results if include_samples or not c.get("is_sample", False)] or case_results
    res["avg_efficiency"] = round(sum(efficiency(c) for c in scored) / max(1, len(scored)), 4)
    res["weights"] = {"time": TIME_WEIGHT, "mem": MEM_WEIGHT, "time_free_ms": TIME_FREE_MS, "mem_free_mb": MEM_FREE_MB, "time_limit_ms": TIME_LIMIT_MS, "memory_limit_mb": MEMORY_LIMIT_MB}
    return res


def _score_core(case_results, manifest, include_samples=False, use_eff=True):
    scored_cases = [c for c in case_results if include_samples or not c.get("is_sample", False)]
    subtasks_spec = manifest.get("subtasks")
    if subtasks_spec:
        subtask_details = []
        total = 0.0
        active_max = 0.0
        for st in subtasks_spec:
            sid = st["id"]
            pts = float(st["points"])
            rel = [c for c in scored_cases if sid in c.get("subtasks", [])]
            if not rel:
                continue
            active_max += pts
            min_ratio = sum(c["ratio"] * (efficiency(c) if use_eff else 1.0) for c in rel) / len(rel)  # 按点给分：组内平均，不取最小
            earned = round(pts * min_ratio, 4)
            total += earned
            subtask_details.append({
                "id": sid,
                "name": st.get("name", f"Subtask {sid}"),
                "points": pts,
                "score": round(earned, 2),
                "cases": len(rel),
                "passed": sum(1 for c in rel if c["verdict"] == "AC"),
            })
        return {"score": round(total, 2), "max_score": round(active_max, 2), "subtasks": subtask_details}

    if not scored_cases:
        scored_cases = case_results
    n = len(scored_cases)
    passed = sum(1 for c in scored_cases if c["verdict"] == "AC")
    score = round(100.0 * passed / n, 2) if n else 0.0
    return {"score": score, "max_score": 100.0, "subtasks": []}


def run_sample_or_custom(exe_path: str, case: dict[str, Any], in_dir: str) -> dict[str, Any]:
    if "out" in case and os.path.isfile(os.path.join(in_dir, case["out"])):
        return run_case(exe_path, case, in_dir)
    in_path = os.path.join(in_dir, case["in"])
    raw = read_data_bytes(in_path).decode("utf-8")
    info = validate_input(raw)
    rr = run_sandboxed(exe_path, stdin_bytes=raw.encode("utf-8"), time_limit_ms=TIME_LIMIT_MS, memory_limit_mb=MEMORY_LIMIT_MB)
    verdict = "OK" if (rr.returncode == 0 and not rr.timed_out and not rr.ole and rr.peak_mem_mb <= MEMORY_LIMIT_MB) else "FAIL"
    tokens = rr.stdout.decode("utf-8", errors="replace").split()
    return {
        "name": case["name"], "verdict": verdict,
        "time_ms": round(rr.cpu_ms, 2), "mem_mb": round(rr.peak_mem_mb, 2),
        "msg": f"Produced {len(tokens)}/{info['queries']} answers (no .out reference)",
    }
