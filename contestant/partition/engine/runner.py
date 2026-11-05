"""Partition（划分） 评测与数据模块。"""
from __future__ import annotations

import os
import lzma
import re
from array import array
import random
import secrets
import shutil
from typing import Any

from common import CompileResult, compile_cpp, run_sandboxed

PROBLEM_ID = "partition"
PROBLEM_DIR_NAME = "partition"
TITLE = "Partition（划分）"
TIME_LIMIT_MS = 700


def _speed_factor() -> float:
    import calib
    return calib.speed_factor()
MEMORY_LIMIT_MB = 2048

SUBTASKS = [
    {'id': 1, 'points': 3.0, 'name': '签到：M = 1（K 大）'},
    {'id': 2, 'points': 2.5, 'name': '签到：M = 2（K 大）'},
    {'id': 3, 'points': 3.5, 'name': 'K = 2，M ≤ 1e9'},
    {'id': 4, 'points': 3.5, 'name': 'K = 3'},
    {'id': 5, 'points': 4.0, 'name': '4 ≤ K ≤ 10'},
    {'id': 6, 'points': 4.0, 'name': '11 ≤ K ≤ 100'},
    {'id': 7, 'points': 4.5, 'name': 'M < K（M ≤ 1000，K 很大）'},
    {'id': 8, 'points': 5.0, 'name': 'K = N'},
    {'id': 9, 'points': 5.0, 'name': 'K 很大的特殊结构（质数 / 2 的幂 / 高度合数）'},
    {'id': 10, 'points': 7.0, 'name': '综合对抗（标志值、溢出、输入顺序、重复值、余数）'},
    {'id': 11, 'points': 4.0, 'name': '超规模：K ≥ N−1（N ≤ 1210000）'},
    {'id': 12, 'points': 4.0, 'name': '超规模：大 K 综合（K ≥ 491500，N ≤ 2100000）'},
    {'id': 13, 'points': 10.0, 'name': '补数歧义与近 N 组'},
    {'id': 14, 'points': 10.0, 'name': '中值窄带与大组数'},
    {'id': 15, 'points': 10.0, 'name': '多尺度标志值与整数溢出'},
    {'id': 16, 'points': 10.0, 'name': '余数极值与有序段'},
    {'id': 17, 'points': 10.0, 'name': '周期骨架与贪心重排'},
]


def _wall_slack(n: int) -> float:
    """墙钟兜底余量（秒）：评测计时只算函数本身，但进程还要读写 O(N) 个整数；超规模组 N 达 2.9e6 时
    读写+管道开销可达秒级，余量按规模放宽，避免 IO 被误判 TLE（不影响用时计分）。"""
    return 1.0 + 2.0 * n / 1_000_000


def read_data_bytes(path: str) -> bytes:
    opener = lzma.open if str(path).endswith('.xz') else open
    with opener(path, 'rb') as f:
        return f.read()


def parse_and_validate_input(text: str) -> tuple[int, int, int, list[int]]:
    tokens = (m.group() for m in re.finditer(r"\S+", text))
    try:
        N, K, M = int(next(tokens)), int(next(tokens)), int(next(tokens))
    except StopIteration:
        raise ValueError("Input too short") from None
    if not 3 <= N <= 3_490_000:
        raise ValueError(f"N={N} out of range [3, 3490000]")
    if not 2 <= K <= min(N, 3_090_000):
        raise ValueError(f"K={K} invalid for N={N}")
    if not 1 <= M <= 1_000_000_000:
        raise ValueError(f"M={M} out of range")
    def values():
        for i, x in enumerate(tokens):
            v = int(x)
            if not 1 <= v <= M:
                raise ValueError(f"A[{i}]={v} out of range [1, {M}]")
            yield v
    A = array("i", values())
    if len(A) != N:
        raise ValueError(f"Expected {N} array elements, got {len(A)}")
    return N, K, M, A


def matching_subtasks(N: int, K: int, M: int) -> list[int]:
    res = [9]
    if N == 3:
        res.append(1)
    if M == 1:
        res.append(2)
    if M <= 2:
        res.append(3)
    if N <= 10:
        res.append(4)
    if K == 2:
        res.append(5)
    if K <= 3:
        res.append(6)
    if K <= 10:
        res.append(7)
    if K <= 100:
        res.append(8)
    return sorted(res)


def check_partition(B: list[int], K: int, P: list[int]) -> tuple[bool, str]:
    L = len(B)
    if len(P) != L:
        return False, f"len(P)={len(P)} != len(B)={L}"
    counts = array("I", [0]) * K
    sums = array("q", [0]) * K
    for j, g in enumerate(P):
        if not (0 <= g < K):
            return False, f"P[{j}]={g} out of range [0, {K})"
        counts[g] += 1
        sums[g] += B[j]
    for g in range(K):
        if counts[g] == 0:
            return False, f"Group {g} is empty"
    target = sums[0]
    for g in range(1, K):
        if sums[g] != target:
            return False, f"Unequal group sums: sum[0]={target} vs sum[{g}]={sums[g]}"
    return True, f"Valid {K}-partition with group sum {target}"


def compile_submission(src_path: str, build_dir: str, root_dir: str) -> CompileResult:
    header_src = os.path.join(root_dir, "data", "include", "partition.h")
    header_dst = os.path.join(build_dir, "partition.h")
    os.makedirs(build_dir, exist_ok=True)
    with open(header_src, "r", encoding="utf-8") as f:
        h_content = f.read()
    if "#pragma once" not in h_content:
        h_content = "#pragma once\n" + h_content
    with open(header_dst, "w", encoding="utf-8") as f:
        f.write(h_content)

    user_copy = os.path.join(build_dir, "contestant_partition.cpp")
    with open(src_path, "r", encoding="utf-8") as sf, open(user_copy, "w", encoding="utf-8") as df:
        df.write('#include "partition.h"\n' + sf.read())
    grader_cpp = os.path.join(root_dir, "engine", "graders", "partition_grader.cpp")
    out_exe = os.path.join(build_dir, "sol_bin")
    return compile_cpp(
        sources=[user_copy, grader_cpp],
        out_exe=out_exe,
        include_dirs=[build_dir],
        stack_mb=1024,
    )


def run_case(exe_path: str, case: dict[str, Any], tests_dir: str, tl_ms: int = TIME_LIMIT_MS) -> dict[str, Any]:
    in_path = os.path.join(tests_dir, case["in"])
    raw_in = read_data_bytes(in_path).decode("ascii")
    N, K, M, A = parse_and_validate_input(raw_in)
    subtasks = case.get("subtasks") or matching_subtasks(N, K, M)

    # ===== Phase 1: 独立进程 1 运行 add_numbers(A, K, M) =====
    s1_hi = secrets.randbits(63) | 1
    s1_lo = secrets.randbits(63) | 1
    payload1 = f"{s1_hi} {s1_lo} 1\n{N} {K} {M}\n" + " ".join(map(str, A)) + "\n"
    rr1 = run_sandboxed(
        exe_path=exe_path,
        stdin_bytes=payload1.encode("ascii"),
        time_limit_ms=tl_ms,
        memory_limit_mb=MEMORY_LIMIT_MB,
        handshake=True,
        wall_slack_s=min(15.0, _wall_slack(N)),
        max_output_bytes=max(64 * 1024 * 1024, 12 * (N + K) + 1024),
    )

    if rr1.peak_mem_mb > MEMORY_LIMIT_MB:
        return {
            "name": case["name"], "subtasks": subtasks, "is_sample": case.get("is_sample", False),
            "verdict": "MLE", "ratio": 0.0, "time_ms": round(rr1.cpu_ms, 2), "mem_mb": round(rr1.peak_mem_mb, 2),
            "msg": f"[Phase 1] Memory {rr1.peak_mem_mb:.1f} MB > {MEMORY_LIMIT_MB} MB",
        }
    if rr1.timed_out:
        return {
            "name": case["name"], "subtasks": subtasks, "is_sample": case.get("is_sample", False),
            "verdict": "TLE", "ratio": 0.0, "time_ms": round(rr1.cpu_ms, 2), "mem_mb": round(rr1.peak_mem_mb, 2),
            "msg": f"[Phase 1] Timed out (wall {rr1.wall_ms:.1f} ms)",
        }
    if rr1.ole:
        return {
            "name": case["name"], "subtasks": subtasks, "is_sample": case.get("is_sample", False),
            "verdict": "OLE", "ratio": 0.0, "time_ms": round(rr1.cpu_ms, 2), "mem_mb": round(rr1.peak_mem_mb, 2),
            "msg": "[Phase 1] Output limit exceeded",
        }
    if rr1.returncode != 0:
        return {
            "name": case["name"], "subtasks": subtasks, "is_sample": case.get("is_sample", False),
            "verdict": "RE", "ratio": 0.0, "time_ms": round(rr1.cpu_ms, 2), "mem_mb": round(rr1.peak_mem_mb, 2),
            "msg": f"[Phase 1] Exit code {rr1.returncode}",
        }

    lines1 = rr1.stdout.decode("utf-8", errors="replace").splitlines()
    if len(lines1) < 2 or lines1[0].strip() != f"{s1_hi} {s1_lo}":
        return {
            "name": case["name"], "subtasks": subtasks, "is_sample": case.get("is_sample", False),
            "verdict": "WA", "ratio": 0.0, "time_ms": round(rr1.cpu_ms, 2), "mem_mb": round(rr1.peak_mem_mb, 2),
            "msg": "[Phase 1] Protocol secret mismatch or missing output",
        }

    st1 = lines1[1].split(maxsplit=2)
    ns1 = int(st1[1])
    ms1 = ns1 / 1e6 / _speed_factor()  # 换算为标准毫秒
    if ms1 > tl_ms:
        return {
            "name": case["name"], "subtasks": subtasks, "is_sample": case.get("is_sample", False),
            "verdict": "TLE", "ratio": 0.0, "time_ms": round(ms1, 2), "mem_mb": round(rr1.peak_mem_mb, 2),
            "msg": f"[Phase 1] Time {ms1:.1f} ms > {tl_ms} ms",
        }
    if st1[0] != "OK":
        reason = st1[2] if len(st1) > 2 else "Invalid add_numbers return"
        return {
            "name": case["name"], "subtasks": subtasks, "is_sample": case.get("is_sample", False),
            "verdict": "WA", "ratio": 0.0, "time_ms": round(ms1, 2), "mem_mb": round(rr1.peak_mem_mb, 2),
            "msg": f"[Phase 1] {reason}",
        }

    S = int(lines1[2].strip())
    C = array("i", (int(x.group()) for x in re.finditer(r"\S+", lines1[3]))) if (S > 0 and len(lines1) > 3) else array("i")
    if len(C) != S or S > K - 1 or any(x < 1 or x > M for x in C):
        return {
            "name": case["name"], "subtasks": subtasks, "is_sample": case.get("is_sample", False),
            "verdict": "WA", "ratio": 0.0, "time_ms": round(ms1, 2), "mem_mb": round(rr1.peak_mem_mb, 2),
            "msg": f"[Phase 1] Invalid C (len={len(C)}, K={K}, M={M})",
        }

    # 裁判合并并排序得到 B
    B = sorted(A + C)
    del A, C, raw_in, payload1, lines1
    L = len(B)

    # ===== Phase 2: 全新独立进程 2 运行 find_partition(B, K) =====
    rem_tl = max(50, int(tl_ms - ms1 + 50))
    s2_hi = secrets.randbits(63) | 1
    s2_lo = secrets.randbits(63) | 1
    payload2 = f"{s2_hi} {s2_lo} 2\n{L} {K}\n" + " ".join(map(str, B)) + "\n"
    rr2 = run_sandboxed(
        exe_path=exe_path,
        stdin_bytes=payload2.encode("ascii"),
        time_limit_ms=rem_tl,
        memory_limit_mb=MEMORY_LIMIT_MB,
        handshake=True,
        wall_slack_s=min(15.0, _wall_slack(N)),
        max_output_bytes=max(64 * 1024 * 1024, 12 * (N + K) + 1024),
    )

    peak_mem = max(rr1.peak_mem_mb, rr2.peak_mem_mb)
    if peak_mem > MEMORY_LIMIT_MB:
        return {
            "name": case["name"], "subtasks": subtasks, "is_sample": case.get("is_sample", False),
            "verdict": "MLE", "ratio": 0.0, "time_ms": round(ms1 + rr2.cpu_ms, 2), "mem_mb": round(peak_mem, 2),
            "msg": f"[Phase 2] Memory {peak_mem:.1f} MB > {MEMORY_LIMIT_MB} MB",
        }
    if rr2.timed_out:
        return {
            "name": case["name"], "subtasks": subtasks, "is_sample": case.get("is_sample", False),
            "verdict": "TLE", "ratio": 0.0, "time_ms": round(ms1 + rr2.cpu_ms, 2), "mem_mb": round(peak_mem, 2),
            "msg": f"[Phase 2] Timed out (phase1 {ms1:.1f} ms + wall2 {rr2.wall_ms:.1f} ms)",
        }
    if rr2.ole:
        return {
            "name": case["name"], "subtasks": subtasks, "is_sample": case.get("is_sample", False),
            "verdict": "OLE", "ratio": 0.0, "time_ms": round(ms1 + rr2.cpu_ms, 2), "mem_mb": round(peak_mem, 2),
            "msg": "[Phase 2] Output limit exceeded",
        }
    if rr2.returncode != 0:
        return {
            "name": case["name"], "subtasks": subtasks, "is_sample": case.get("is_sample", False),
            "verdict": "RE", "ratio": 0.0, "time_ms": round(ms1 + rr2.cpu_ms, 2), "mem_mb": round(peak_mem, 2),
            "msg": f"[Phase 2] Exit code {rr2.returncode}",
        }

    del payload2, rr1
    lines2 = rr2.stdout.decode("utf-8", errors="replace").splitlines()
    if len(lines2) < 2 or lines2[0].strip() != f"{s2_hi} {s2_lo}":
        return {
            "name": case["name"], "subtasks": subtasks, "is_sample": case.get("is_sample", False),
            "verdict": "WA", "ratio": 0.0, "time_ms": round(ms1 + rr2.cpu_ms, 2), "mem_mb": round(peak_mem, 2),
            "msg": "[Phase 2] Protocol secret mismatch or missing output",
        }

    st2 = lines2[1].split(maxsplit=2)
    ns2 = int(st2[1])
    ms2 = ns2 / 1e6 / _speed_factor()
    total_ms = ms1 + ms2
    if total_ms > tl_ms:
        return {
            "name": case["name"], "subtasks": subtasks, "is_sample": case.get("is_sample", False),
            "verdict": "TLE", "ratio": 0.0, "time_ms": round(total_ms, 2), "mem_mb": round(peak_mem, 2),
            "msg": f"Combined time {total_ms:.1f} ms ({ms1:.1f} + {ms2:.1f}) > {tl_ms} ms",
        }
    if st2[0] != "OK":
        reason = st2[2] if len(st2) > 2 else "Invalid find_partition return"
        return {
            "name": case["name"], "subtasks": subtasks, "is_sample": case.get("is_sample", False),
            "verdict": "WA", "ratio": 0.0, "time_ms": round(total_ms, 2), "mem_mb": round(peak_mem, 2),
            "msg": f"[Phase 2] {reason}",
        }

    L_ret = int(lines2[2].strip())
    P = array("i", (int(x.group()) for x in re.finditer(r"\S+", lines2[3]))) if (L_ret > 0 and len(lines2) > 3) else array("i")
    ok, msg = check_partition(B, K, P)
    return {
        "name": case["name"],
        "subtasks": subtasks,
        "is_sample": case.get("is_sample", False),
        "N": N,
        "K": K,
        "M": M,
        "added_S": S,
        "phase1_ms": round(ms1, 2),
        "phase2_ms": round(ms2, 2),
        "time_ms": round(total_ms, 2),
        "mem_mb": round(peak_mem, 2),
        "verdict": "AC" if ok else "WA",
        "ratio": 1.0 if ok else 0.0,
        "msg": f"{msg} (added S={S}/{K - 1})",
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
TIME_FREE_MS = 50
MEM_FREE_MB = 16


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
    scored = [c for c in case_results if include_samples or not c.get("is_sample", False)]
    if not scored:
        scored = case_results

    subtask_details = []
    total = 0.0
    active_max = 0.0
    for st in SUBTASKS:
        sid = st["id"]
        pts = st["points"]
        rel = [c for c in scored if sid in c.get("subtasks", [])]
        if not rel:
            continue
        active_max += pts
        min_ratio = sum(c["ratio"] * (efficiency(c) if use_eff else 1.0) for c in rel) / len(rel)  # 按点给分：组内平均，不取最小
        earned = pts * min_ratio
        total += earned
        subtask_details.append({
            "id": sid,
            "name": st["name"],
            "points": pts,
            "score": round(earned, 2),
            "cases": len(rel),
            "passed": sum(1 for c in rel if c["verdict"] == "AC"),
        })
    return {"score": round(total, 2), "max_score": round(active_max, 2), "subtasks": subtask_details}


def run_sample_or_custom(exe_path: str, case: dict[str, Any], in_dir: str) -> dict[str, Any]:
    return run_case(exe_path, case, in_dir)
