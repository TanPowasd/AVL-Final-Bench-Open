"""前沿实验室：按种子生成隐藏世界（只用标准库）。

world.generate(seed) 返回一个 dict，包含：
  - 隐藏训练参数（缩放律 α β A B E_d、迁移矩阵 T、数据质量 q 与污染 c、最优学习率系数、临界 batch）
  - 隐藏评测（6 个任务的映射 M、阈值 τ、尺度 s；τ/s 由参考配置自动标定）
  - 全局随机序列（AI 热度、需求冲击、拍卖供给与 NPC 出价、交付周期、数据合同、人才市场、
    NPC 实验室发布时间表、出口管制、电网限电、开源事件、核心研究员离职抽签、电价路径）
全局序列与双方动作无关，双方共享，所以交换座位时两局面对完全相同的世界。
每方自己的随机（故障、尖峰、慢节点、观测噪声、挖人、诉讼、抢占）用 hash_u() 公共随机数，
按（tick、本方第几个任务/第几次挖人）取数，不按座位取数，同样对称。
"""
from __future__ import annotations

import hashlib
import math
import os
import random

SALT = os.environ.get("FRONTIER_SALT", "practice")

# ----------------------------------------------------------------- 时间
DAY = 24
WEEK = 168
MONTH = 730
QUARTER = 2190
YEAR = 8760
TICKS = 3 * YEAR                       # 26280
WEEKS = TICKS // WEEK + 1             # 157（第 0..156 周）

# ----------------------------------------------------------------- 数据域
DOMAINS = ["web", "code", "math", "books", "science", "dialog", "multiling", "synth"]
REAL = DOMAINS[:7]                     # 参与评测的 7 个域
POOLS = DOMAINS + ["distill"]          # 训练配比可用的 9 个数据池
PUBLIC_B = {"web": 2000, "code": 400, "math": 60, "books": 300, "science": 150,
            "dialog": 100, "multiling": 800, "synth": 10}
TASKS = ["reasoning", "coding", "knowledge", "chat", "multilingual", "general"]
TASK_MAIN = {"reasoning": ["math"], "coding": ["code"], "knowledge": ["science", "books"],
             "chat": ["dialog"], "multilingual": ["multiling"], "general": ["web"]}

# ----------------------------------------------------------------- GPU
# 型号, 算力/张, 功耗 kW, 上市季度(0 起), 每季度全市场供给, 基准价 M/张
GPUS = [("G1", 1.0, 0.7, 0, 8192, 0.05),
        ("G2", 2.2, 1.0, 1, 6144, 0.09),
        ("G3", 5.0, 1.4, 3, 4096, 0.17),
        ("G4", 11.0, 1.8, 6, 3072, 0.32),
        ("G5", 24.0, 2.4, 10, 2048, 0.60)]
GPU_NAMES = [g[0] for g in GPUS]
GPU = {g[0]: {"power": g[1], "kw": g[2], "launch_q": g[3], "supply_q": g[4], "price": g[5],
              "gen": i} for i, g in enumerate(GPUS)}
LOT = 256
Q0 = 5.0                               # 开局 API 全市场需求（B token / tick）

M64 = (1 << 64) - 1


def _mix(z: int) -> int:
    z = (z + 0x9E3779B97F4A7C15) & M64
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & M64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & M64
    return z ^ (z >> 31)


def base_key(seed: int, tag: str) -> int:
    h = hashlib.sha256(f"{SALT}|{seed}|{tag}".encode()).hexdigest()
    return int(h[:16], 16)


def hash_u(base: int, a: int, b: int = 0, c: int = 0) -> float:
    """[0,1) 公共随机数：同一 (base, a, b, c) 永远同值。"""
    return _mix(_mix(_mix(base ^ (a & M64)) ^ (b & M64)) ^ (c & M64)) / 18446744073709551616.0


def _sigmoid(x):
    if x < -40:
        return 0.0
    if x > 40:
        return 1.0
    return 1.0 / (1.0 + math.exp(-x))


# ----------------------------------------------------------------- 训练核心公式（引擎与世界共用）
def eff_tokens(S, Sq, U):
    """某池：已训 S（B token）、质量加权和 Sq、去重量 U → 有效 token（重复衰减 × 质量）。"""
    if S <= 0 or U <= 0:
        return 0.0
    e = S / U
    return U * 4.0 * (1.0 - math.exp(-e / 4.0)) * (Sq / S)


def domain_losses(w, n_eff, eff_by_pool, data_eff, extra):
    """返回 7 个真实域的 loss（未乘尖峰/退火）。eff_by_pool: {pool: 有效 token}；extra: 加性惩罚。"""
    out = []
    an = w["A"] / (n_eff ** w["alpha"])
    for d in REAL:
        row = w["T"][d]
        de = 0.0
        for p, v in eff_by_pool.items():
            if v > 0:
                de += row[p] * v
        de = max(de * data_eff, 0.01)
        out.append(w["E"][d] + an + w["B"] / (de ** w["beta"]) + extra)
    return out


def capability(w, losses):
    tot = 0.0
    for k, task in enumerate(TASKS):
        m = w["M"][task]
        lw = sum(m[d] * losses[i] for i, d in enumerate(REAL))
        tot += _sigmoid((w["tau"][task] - lw) / w["s"][task])
    return tot / len(TASKS)


def ref_losses(w, n, d_tok):
    """标定用参考配置：均匀配比到 7 个真实域、公共数据、真实质量、最优学习率、不退火。"""
    per = d_tok / len(REAL)
    eff = {}
    for p in REAL:
        U = w["public_b"][p]
        eff[p] = eff_tokens(per, per * w["q"][p], U)
    return domain_losses(w, n, eff, 1.0, 0.0)


# ----------------------------------------------------------------- 生成
def generate(seed: int) -> dict:
    rng = random.Random(base_key(seed, "world"))
    w: dict = {"seed": seed}
    w["alpha"] = rng.uniform(0.28, 0.40)
    w["beta"] = rng.uniform(0.25, 0.35)
    w["A"] = rng.uniform(0.30, 0.60)
    w["B"] = rng.uniform(0.90, 1.50)
    w["E"] = {d: rng.uniform(1.4, 2.2) for d in REAL}
    w["eta_c"] = rng.uniform(0.0012, 0.0028)        # η* = eta_c × N^-0.3
    w["bc0"] = rng.uniform(192, 640)                # B_crit = bc0 × (1 + D/50)^0.4

    # 数据域：公共量（±40%）、质量 q、污染 c
    w["public_b"] = {d: round(PUBLIC_B[d] * rng.uniform(0.6, 1.4), 1) for d in DOMAINS}
    w["q"] = {d: rng.uniform(0.5, 1.5) for d in REAL}
    w["q"]["synth"] = rng.uniform(0.6, 0.9)
    w["c"] = {d: rng.uniform(0.0, 0.12) for d in REAL}
    w["c"]["synth"] = 0.0
    w["q_hint"] = {d: round(min(1.6, max(0.4, w["q"][d] + rng.uniform(-0.3, 0.3))), 2) for d in DOMAINS}

    # 迁移矩阵 T[d][k]：域 k 的数据对域 d 的帮助
    T = {}
    special = {("math", "code"): (0.4, 0.7), ("dialog", "multiling"): (0.3, 0.6),
               ("science", "books"): (0.3, 0.5), ("dialog", "web"): (0.2, 0.4),
               ("math", "science"): (0.2, 0.4), ("code", "web"): (0.1, 0.25)}
    for d in REAL:
        T[d] = {}
        for k in POOLS:
            if k == d:
                T[d][k] = 1.0
            elif (d, k) in special:
                lo, hi = special[(d, k)]
                T[d][k] = rng.uniform(lo, hi)
            elif k == "synth":
                T[d][k] = rng.uniform(0.25, 0.40)
            elif k == "distill":
                T[d][k] = rng.uniform(0.40, 0.60)
            else:
                T[d][k] = rng.uniform(0.02, 0.12)
    w["T"] = T

    # 评测映射 M（每行和为 1）
    M = {}
    for task in TASKS:
        main = TASK_MAIN[task]
        wt = {d: rng.uniform(0.02, 0.15) for d in REAL}
        tot_main = rng.uniform(0.45, 0.6)
        for i, d in enumerate(main):
            wt[d] = 0.0
        rest = sum(wt.values())
        for d in REAL:
            wt[d] = wt[d] / rest * (1 - tot_main)
        if len(main) == 1:
            wt[main[0]] = tot_main
        else:
            wt[main[0]] = tot_main * 0.6
            wt[main[1]] = tot_main * 0.4
        M[task] = wt
    w["M"] = M
    # τ, s 标定：小参考 (20B, 400B) → 0.15–0.25，大参考 (300B, 6T) → 0.80–0.90
    ls = ref_losses(w, 20.0, 400.0)
    ll = ref_losses(w, 300.0, 6000.0)
    w["tau"], w["s"] = {}, {}
    for task in TASKS:
        m = M[task]
        Ls = sum(m[d] * ls[i] for i, d in enumerate(REAL))
        Ll = sum(m[d] * ll[i] for i, d in enumerate(REAL))
        lo = rng.uniform(0.15, 0.25)
        hi = rng.uniform(0.80, 0.90)
        gs = math.log(lo / (1 - lo))
        gl = math.log(hi / (1 - hi))
        s = (Ls - Ll) / (gl - gs)
        w["s"][task] = s
        w["tau"][task] = Ls + gs * s

    # 地区（机房）
    regions = []
    for r in range(5):
        cap = rng.uniform(5, 40)
        regions.append({"id": r, "price": round(rng.uniform(0.0004, 0.0012), 6), "cap_mw": round(cap, 1),
                        "build_weeks": rng.randint(8, 20),
                        "build_cost": round(15 + 45 * (cap - 5) / 35 * rng.uniform(0.7, 1.0) + rng.uniform(0, 8), 1),
                        "reg": round(rng.uniform(0.8, 1.5), 2), "phase": rng.uniform(0, 2 * math.pi)})
    regions[0]["cap_mw"] = max(regions[0]["cap_mw"], 8.0)
    w["regions"] = regions

    # AI 热度（每周）与需求冲击（每季度）
    ph = rng.uniform(0, 2 * math.pi)
    heat, ar = [], 0.0
    for wk in range(WEEKS + 1):
        ar = 0.8 * ar + rng.gauss(0, 0.03)
        heat.append(round(min(1.5, max(0.6, 1 + 0.25 * math.sin(2 * math.pi * wk / 78 + ph) + ar)), 4))
    w["heat"] = heat
    shock, s = [], 1.0
    for q in range(13):
        if q > 0 and rng.random() < 0.2:
            s *= rng.choice([0.85, 1.15])
        shock.append(round(s, 4))
    w["shock"] = shock

    # 出口管制（60%）：某季度 G3 供给 −50%，提前 2 周公告
    w["export_q"] = rng.randint(4, 8) if rng.random() < 0.6 else -1
    # 开源事件（30%）
    if rng.random() < 0.3:
        w["open"] = {"tick": rng.randint(6 * MONTH, 24 * MONTH), "npc": rng.randint(0, 1)}
    else:
        w["open"] = None
    # 电网限电：每周每地区 3%，1–3 周，上限 −30%
    curt = []
    for wk in range(1, WEEKS):
        for r in range(5):
            if rng.random() < 0.03:
                curt.append({"region": r, "start": wk * WEEK, "end": (wk + rng.randint(1, 3)) * WEEK})
    w["curtail"] = curt
    # 核心研究员离职抽签：每月一个公共均匀数，双方都与 2% 比较
    w["quit_u"] = [rng.random() for _ in range(40)]

    # NPC 实验室：首代能力 0.3–0.5，每 6–9 个月一代 +0.05–0.10
    npcs = []
    for i in range(2):
        gens, t, c = [], 0, rng.uniform(0.30, 0.50)
        k = 1
        while t < TICKS:
            gens.append({"tick": t, "C": round(min(0.97, c), 4), "id": f"N{i + 1}-{k}"})
            t += rng.randint(6 * MONTH, 9 * MONTH)
            c += rng.uniform(0.05, 0.10)
            k += 1
        npcs.append({"name": f"N{i + 1}", "gens": gens, "staff0": rng.randint(30, 45)})
    w["npcs"] = npcs

    # 拍卖：每周每型号放出批数、NPC 出价、交付延迟
    auct = []
    acc = {m: 0.0 for m in GPU_NAMES}
    for wk in range(WEEKS):
        q = (wk * WEEK) // QUARTER
        row = {}
        h = heat[wk]
        for m in GPU_NAMES:
            g = GPU[m]
            per = g["supply_q"] / 13 / LOT
            early = False
            if q >= g["launch_q"]:
                f = 1.0
            elif m != "G1" and m != "G2" and q == g["launch_q"] - 1:
                f, early = 0.25, True
            else:
                acc[m] = 0.0
                continue
            if m == "G3" and q == w["export_q"]:
                f *= 0.5
            acc[m] += per * f
            lots = int(acc[m])
            acc[m] -= lots
            reserve = g["price"] * 0.8 * h
            bids = []
            if not early:
                for n in range(2):
                    want = rng.random() * per * 0.8 * h
                    nl = int(want) + (1 if rng.random() < want - int(want) else 0)
                    if nl > 0:
                        bids.append([nl, round(g["price"] * h * rng.uniform(0.95, 1.5), 5)])
            row[m] = {"lots": lots, "early": early, "reserve": round(reserve, 5), "npc": bids,
                      "delay": rng.randint(4, 10)}
        auct.append(row)
    w["auction"] = auct

    # 数据合同：每周 0–2 份
    deals, did = [], 0
    for wk in range(WEEKS):
        row = []
        for _ in range(rng.choice([0, 1, 1, 2])):
            d = rng.choice(REAL)
            tok = rng.uniform(50, 500)
            q = rng.uniform(0.6, 1.5)
            npc_bids = [round(tok * 0.03 * heat[wk] * rng.uniform(1.0, 2.5), 3)
                        for _ in range(2) if rng.random() < 0.5]
            row.append({"id": did, "domain": d, "tokens_b": round(tok, 1), "q": q,
                        "q_hint": round(q + rng.uniform(-0.2, 0.2), 2), "c": rng.uniform(0, 0.08),
                        "reserve": round(tok * 0.03 * heat[wk], 3), "npc": npc_bids})
            did += 1
        deals.append(row)
    w["deals"] = deals

    # 人才市场：每周 10–30 人
    talent, cid = [], 0
    for wk in range(WEEKS):
        row = []
        for _ in range(rng.randint(10, 30)):
            a = rng.choices([1, 2, 3, 4, 5], [0.3, 0.3, 0.2, 0.13, 0.07])[0]
            lo = max(1, min(3, a - rng.randint(0, 2)))
            row.append({"id": cid, "ab": a, "lo": lo, "hi": lo + 2,
                        "ask": round(0.0001 * (0.5 + 0.5 * a) * rng.uniform(0.9, 1.2), 6),
                        "npc": round(rng.uniform(1.0, 1.3), 3) if rng.random() < 0.12 else 0.0})
            cid += 1
        talent.append(row)
    w["talent"] = talent
    # 起始研究员（双方相同）
    start = []
    for _ in range(20):
        a = rng.choices([1, 2, 3, 4], [0.3, 0.4, 0.2, 0.1])[0]
        start.append({"ab": a, "sal": round(0.0001 * (0.5 + 0.5 * a) * rng.uniform(0.9, 1.2), 6)})
    w["staff0"] = start
    return w


def elec_price(w, region: int, t: int) -> float:
    r = w["regions"][region]
    return r["price"] * (1 + 0.12 * t / YEAR) * (1 + 0.1 * math.sin(2 * math.pi * t / YEAR + r["phase"]))


def demand_q(w, t: int) -> float:
    """API 全市场需求（B token / tick）。"""
    m = t / MONTH
    if m <= 12:
        g = 1.08 ** m
    elif m <= 24:
        g = 1.08 ** 12 * 1.05 ** (m - 12)
    else:
        g = 1.08 ** 12 * 1.05 ** 12 * 1.03 ** (m - 24)
    return Q0 * g * w["heat"][t // WEEK] * w["shock"][min(12, t // QUARTER)]


