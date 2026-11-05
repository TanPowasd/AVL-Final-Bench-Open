"""Ball Machine（弹球机） 评测与数据模块。"""
from __future__ import annotations

import os
import lzma
import re
import random
import secrets
import shutil
from collections import deque
from typing import Any

from common import CompileResult, compile_cpp, run_sandboxed


def _speed_factor() -> float:
    import calib
    return calib.speed_factor()

PROBLEM_ID = "ball_machine"
PROBLEM_DIR_NAME = "ball_machine"
TITLE = "Ball Machine（弹球机）"
TIME_LIMIT_MS = 2500
MEMORY_LIMIT_MB = 2048

# 原 50 点保持不变；新增 50 点的实际最大 N/M。insert 上限仍为 max(500000,500*N)。
N_MAX = 149000
M_MAX = 656
# 按主题分组（按点给分：组分 = 分值 × 组内平均得分率）。组 1、4~10 全部 N = 1000；组 2、3 为超规模组。
# 组 1 为签到组：只要 C <= 1000 且正确还原即得满分率；其余组的得分率按 C 分段公式（见 case_rate）。
SUBTASKS = [
    {'id': 1, 'points': 5.0, 'name': '签到：N=1000，M<=3 的链/Y 形，或 M=200 且每个叶子到根为互不相交的单链（C<=1000 正确即满分）', 'checkin': True},
    {'id': 2, 'points': 5.0, 'name': '大量一元结点，链长互异/重尾'},
    {'id': 3, 'points': 5.0, 'name': '极深树（深度接近 N-M）'},
    {'id': 4, 'points': 5.0, 'name': '完全二叉/三叉/四叉均衡树'},
    {'id': 5, 'points': 5.0, 'name': '毛毛虫/扫帚/章鱼/蜘蛛'},
    {'id': 6, 'points': 5.0, 'name': '同构子树大量重复（平局对抗）'},
    {'id': 7, 'points': 5.0, 'name': '根只有两个孩子且两侧规模悬殊'},
    {'id': 8, 'points': 5.0, 'name': '叶深差异极大、宽层与深层混合'},
    {'id': 9, 'points': 5.0, 'name': '长链/单点叶子比例最坏（卡 sqrt 分块）'},
    {'id': 10, 'points': 5.0, 'name': '随机综合对抗'},
    {'id': 11, 'points': 10.0, 'name': '深主干与密集梳齿'},
    {'id': 12, 'points': 10.0, 'name': '重复同构子树与平局'},
    {'id': 13, 'points': 10.0, 'name': '不等长分支与重尾'},
    {'id': 14, 'points': 10.0, 'name': '平方根分块边界'},
    {'id': 15, 'points': 10.0, 'name': '深浅混合与拓扑乱序'},
]


def read_data_bytes(path: str) -> bytes:
    opener = lzma.open if str(path).endswith('.xz') else open
    with opener(path, 'rb') as f:
        return f.read()


def parse_and_validate_input(text: str) -> tuple[int, int, list[int]]:
    tokens = text.split()
    if len(tokens) < 2:
        raise ValueError("Input too short")
    N, M = int(tokens[0]), int(tokens[1])
    if not (2 <= N <= N_MAX):
        raise ValueError(f"N={N} out of range [2, {N_MAX}]")
    if not (1 <= M <= M_MAX and M < N):
        raise ValueError(f"M={M} invalid for N={N}")
    if len(tokens) != 2 + (N - 1):
        raise ValueError(f"Expected {N - 1} parent entries, got {len(tokens) - 2}")

    P = [int(x) for x in tokens[2:]]
    has_child = [False] * N
    for u, pu in enumerate(P):
        if not (u < pu < N):
            raise ValueError(f"Invalid P[{u}]={pu}: must satisfy {u} < P[u] < {N}")
        has_child[pu] = True

    leaves = [u for u in range(N - 1) if not has_child[u]]
    if leaves != list(range(M)):
        raise ValueError(f"Leaf set mismatch: expected 0..{M - 1}, got {leaves[:10]} (count={len(leaves)})")
    return N, M, P


def matching_subtasks(N: int, M: int) -> list[int]:
    """正式点在 manifest 中显式给出所属组；自定义输入无组时归入综合组 10。"""
    return [10]


def case_rate(C: int) -> float:
    """单点得分率（0..1），即原子任务 4 的 C 分段公式除以 60。"""
    if C is None or C > 1000 or C < 0:
        return 0.0
    if C <= 44:
        return 1.0
    if C <= 71:
        return (104 - C) / 60.0
    if C <= 200:
        return (47.0 - C / 5.0) / 60.0
    return 7.0 / 60.0


def subtask_case_score(subtask_id: int, C: int) -> float:
    """该点在所属组内的分数（= 组分值 × 得分率）。签到组只看 C <= 1000 且正确。"""
    st = next((s for s in SUBTASKS if s["id"] == subtask_id), None)
    if st is None or C is None or C > 1000 or C < 0:
        return 0.0
    if st.get("checkin"):
        return st["points"]
    return st["points"] * case_rate(C)


def check_tree_isomorphism(N: int, M: int, P: list[int], R: list[int]) -> tuple[bool, str]:
    """O(N) 精确判定是否存在保持叶结点 0..M-1 与根 N-1 不变的树同构双射 L。"""
    if len(R) != N - 1:
        return False, f"len(R)={len(R)} != N-1={N - 1}"
    for v, rv in enumerate(R):
        if not (0 <= rv < N):
            return False, f"R[{v}]={rv} out of range [0, {N})"
        if rv == v:
            return False, f"Self-loop R[{v}]={rv}"

    # 1. 验证 R 构成以 N-1 为根的内向树
    rev: list[list[int]] = [[] for _ in range(N)]
    for v, rv in enumerate(R):
        rev[rv].append(v)
    vis = [False] * N
    vis[N - 1] = True
    q = deque([N - 1])
    visited_cnt = 1
    while q:
        cur = q.popleft()
        for nxt in rev[cur]:
            if not vis[nxt]:
                vis[nxt] = True
                visited_cnt += 1
                q.append(nxt)
    if visited_cnt != N:
        return False, f"R is not a tree rooted at {N - 1} (reachable={visited_cnt}/{N})"

    # 2. 自底向上唯一推导双射 L（因 P[u] > u 且所有节点必有叶后代）
    L = [-1] * N
    for u in range(M):
        L[u] = u
    L[N - 1] = N - 1

    for u in range(N - 1):
        if L[u] == -1:
            return False, f"Internal node u={u} unreachable from leaves"
        if L[u] == N - 1:
            return False, f"Non-root node u={u} maps to the root (paths contracted)"
        target = R[L[u]]
        pu = P[u]
        if L[pu] == -1:
            L[pu] = target
        elif L[pu] != target:
            return False, f"Inconsistent parent mapping at u={u}: L[P[u]]={L[pu]} vs R[L[u]]={target}"

    if len(set(L)) != N or any(x < 0 or x >= N for x in L):
        return False, "Induced mapping L is not a bijection on 0..N-1"

    return True, "Valid isomorphic tree reconstruction"


def compile_submission(src_path: str, build_dir: str, root_dir: str) -> CompileResult:
    header_src = os.path.join(root_dir, "data", "include", "ballmachine.h")
    header_dst = os.path.join(build_dir, "ballmachine.h")
    os.makedirs(build_dir, exist_ok=True)
    with open(header_src, "r", encoding="utf-8") as f:
        h_content = f.read()
    if "#pragma once" not in h_content:
        h_content = "#pragma once\n" + h_content
    with open(header_dst, "w", encoding="utf-8") as f:
        f.write(h_content)

    user_copy = os.path.join(build_dir, "contestant_ballmachine.cpp")
    with open(src_path, "r", encoding="utf-8") as sf, open(user_copy, "w", encoding="utf-8") as df:
        df.write('#include "ballmachine.h"\n' + sf.read())
    grader_cpp = os.path.join(root_dir, "engine", "graders", "ballmachine_grader.cpp")
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
    N, M, P = parse_and_validate_input(raw_in)

    s_hi = secrets.randbits(63) | 1
    s_lo = secrets.randbits(63) | 1
    tie_mode = int(case.get("tie_mode", 0 if case.get("is_sample") else 3))
    seed = int(case.get("seed", 20261029)) & ((1 << 63) - 1)

    payload = f"{s_hi} {s_lo} {tie_mode} {seed}\n{N} {M}\n" + " ".join(map(str, P)) + "\n"
    rr = run_sandboxed(
        exe_path=exe_path,
        stdin_bytes=payload.encode("ascii"),
        time_limit_ms=tl_ms,
        memory_limit_mb=MEMORY_LIMIT_MB,
        handshake=True,
        wall_slack_s=2.0,
    )

    subtasks = case.get("subtasks") or matching_subtasks(N, M)
    base = {
        "name": case["name"],
        "subtasks": subtasks,
        "is_sample": case.get("is_sample", False),
        "N": N,
        "M": M,
        "tie_mode": tie_mode,
        "time_ms": round(rr.cpu_ms, 2),
        "wall_ms": round(rr.wall_ms, 2),
        "mem_mb": round(rr.peak_mem_mb, 2),
    }

    if rr.peak_mem_mb > MEMORY_LIMIT_MB:
        return {**base, "verdict": "MLE", "C": None, "msg": f"Memory {rr.peak_mem_mb:.1f} MB > {MEMORY_LIMIT_MB} MB"}
    if rr.timed_out:
        return {**base, "verdict": "TLE", "C": None, "msg": f"Timed out (wall {rr.wall_ms:.1f} ms, cpu {rr.cpu_ms:.1f} ms)"}
    if rr.ole:
        return {**base, "verdict": "OLE", "C": None, "msg": "Output limit exceeded"}
    if rr.returncode != 0:
        return {**base, "verdict": "RE", "C": None, "msg": f"Runtime error (exit code {rr.returncode})"}

    lines = rr.stdout.decode("utf-8", errors="replace").splitlines()
    if len(lines) < 2:
        return {**base, "verdict": "WA", "C": None, "msg": "Invalid/missing grader protocol output"}
    if lines[0].strip() != f"{s_hi} {s_lo}":
        return {**base, "verdict": "WA", "C": None, "msg": "Protocol secret mismatch (tampered stdout)"}

    status_parts = lines[1].split(maxsplit=2)
    if status_parts[0] == "FAIL":
        ns = int(status_parts[1])
        inner_ms = ns / 1e6 / _speed_factor()  # 交互器内计时为本机毫秒，换算为标准毫秒
        base["time_ms"] = round(inner_ms, 2)
        if inner_ms > tl_ms:
            return {**base, "verdict": "TLE", "C": None, "msg": f"Time {inner_ms:.1f} ms > {tl_ms} ms"}
        reason = status_parts[2] if len(status_parts) > 2 else "Protocol failure"
        return {**base, "verdict": "WA", "C": None, "msg": reason}

    ok_parts = lines[1].split()
    if len(ok_parts) != 7 or ok_parts[0] != "OK":
        return {**base, "verdict": "WA", "C": None, "msg": "Malformed grader OK header"}

    ns = int(ok_parts[1])
    empty_ok = int(ok_parts[2])
    K = int(ok_parts[3])
    B = int(ok_parts[4])
    C = int(ok_parts[5])
    ins_cnt = int(ok_parts[6])
    inner_ms = ns / 1e6 / _speed_factor()  # 交互器内计时为本机毫秒，换算为标准毫秒
    base["time_ms"] = round(inner_ms, 2)

    if inner_ms > tl_ms:
        return {**base, "verdict": "TLE", "C": None, "msg": f"Time {inner_ms:.1f} ms > {tl_ms} ms"}
    if not empty_ok:
        return {**base, "verdict": "WA", "C": None, "msg": "Machine is not empty upon return"}
    if len(lines) < 4:
        return {**base, "verdict": "WA", "C": None, "msg": "Missing array R"}

    Q = int(lines[2].strip())
    R = [int(x) for x in lines[3].split()] if Q > 0 else []
    iso_ok, iso_msg = check_tree_isomorphism(N, M, P, R)
    if not iso_ok:
        return {**base, "verdict": "WA", "C": None, "msg": iso_msg}

    checkin = any(st.get("checkin") and st["id"] in subtasks for st in SUBTASKS)
    verdict = "AC" if (C <= 44 or (checkin and C <= 1000)) else "PC"
    return {
        **base,
        "verdict": verdict,
        "K": K,
        "B": B,
        "C": C,
        "inserts": ins_cnt,
        "msg": f"K={K}, B={B}, C={C}, inserts={ins_cnt}",
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
        case_scores = [
            0.0 if c.get("C") is None else subtask_case_score(sid, int(c["C"])) * (efficiency(c) if use_eff else 1.0)
            for c in rel
        ]
        earned = sum(case_scores) / len(case_scores)  # 按点给分：组内平均，不取最小
        total += earned
        max_c = max((c["C"] for c in rel if c.get("C") is not None), default=None)
        subtask_details.append({
            "id": sid,
            "name": st["name"],
            "points": pts,
            "score": round(earned, 4),
            "cases": len(rel),
            "worst_C": max_c,
        })
    return {"score": round(total, 2), "max_score": round(active_max, 2), "subtasks": subtask_details}


def run_sample_or_custom(exe_path: str, case: dict[str, Any], in_dir: str) -> dict[str, Any]:
    return run_case(exe_path, case, in_dir)
