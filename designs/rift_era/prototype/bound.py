#!/usr/bin/env python3
"""理想化研究点上限估算（不是对局模拟）。

假设：人口按逻辑斯蒂增长；每单位人口有 FREE 劳动可投入科研链；
每座实验室连同供应链（耗材、验证包、电力、采掘）占 LAB_LABOR 劳动；
实验室按最优顺序升级（R03→P076，R07→P077，R07+C04→P079，R11→P102），
升级所需前置科技的研究点从同一研究流中扣除；忽略验证等待以外的一切摩擦。
"""
import json, sys
from pathlib import Path
from functools import lru_cache
ROOT = Path(__file__).resolve().parents[1]
T = json.loads((ROOT / 'data/technology.json').read_text(encoding='utf-8'))
I = json.loads((ROOT / 'data/industry.json').read_text(encoding='utf-8'))
N = {t['id']: t for t in T['technologies']}
TIER = {t['tier']: t for t in T['research_tiers']}
R = {r['id']: r for r in I['recipes']}

@lru_cache(None)
def anc(tid):
    t = N[tid]; ps = list(t['requires_all'])
    if t['requires_any']: ps.append(min(t['requires_any'], key=lambda p: cost(anc(p) | {p})))
    s = set(ps)
    for p in ps: s |= anc(p)
    return frozenset(s)
def cost(ids): return sum(TIER[N[i]['tier']]['theory_points'] for i in ids)
def vcycles(ids): return sum(TIER[N[i]['tier']]['validation_cycles'] for i in ids)

LAB_LABOR = {'P075': 5.5, 'P076': 10.0, 'P077': 15.0, 'P079': 16.0, 'P102': 17.0}
UNLOCK = [('P076', {'R03'}), ('P077', {'R07'}), ('P079', {'R07', 'C04'}), ('P102', {'R11'})]

def run(cycles=960, r=0.0015, K=None, lab_mult=None, free=0.35, alpha_switch=True, verbose=False):
    lab_mult = lab_mult or {}
    pop = 200.0; known = set(); lab = 'P075'; total = 0.0; bank = 0.0
    queue = []
    for rid, req in UNLOCK:
        need = set(req)
        for q in req: need |= anc(q)
        queue.append((rid, need))
    if alpha_switch: queue.insert(2, ('alpha', {'R05', 'C02'} | anc('R05') | anc('C02')))
    alpha = 0.75; marks = {}
    for c in range(cycles):
        pop += pop * r * (1 - pop / K) if K else pop * r
        labs = max(1.0, free * pop / LAB_LABOR[lab])
        rp = R[lab]['outputs']['research_points'] * lab_mult.get(lab, 1.0) * labs ** alpha
        total += rp; bank += rp
        while queue:
            rid, need = queue[0]
            rest = need - known
            if cost(rest) > bank: break
            bank -= cost(rest); known |= rest; queue.pop(0)
            marks[rid] = c + vcycles(rest) // 3          # 验证可部分并行，粗略记 1/3
            if rid == 'alpha': alpha = 0.92
            else: lab = rid
    return dict(total=total, pop=pop, marks=marks, unlock_spent=total - bank)

if __name__ == '__main__':
    tree = cost(set(N))
    t6 = sorted((cost(anc(t) | {t}), t) for t in N if N[t]['tier'] == 6)
    print(f'全树 {tree}；6 阶终端（含前置）最便宜 {t6[0]}，中位 {t6[len(t6)//2]}，最贵 {t6[-1]}')
    cases = [
        ('v1.1：960 周期，指数 0.15%', dict(cycles=960, lab_mult={'P077': 2/3, 'P079': 2/3, 'P102': 2/3})),
        ('v1.2：2880 周期，逻辑斯蒂 0.20% K=600', dict(cycles=2880, r=0.002, K=600)),
        ('对照：2880，K=400', dict(cycles=2880, r=0.002, K=400)),
        ('对照：2880，K=800', dict(cycles=2880, r=0.002, K=800)),
    ]
    for name, kw in cases:
        o = run(**kw)
        print(f"{name:40s} 累计 {o['total']:>9.0f}  终局人口 {o['pop']:>6.0f}  里程碑 {o['marks']}")
