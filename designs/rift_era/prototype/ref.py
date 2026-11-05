"""参考强策略 v0：在 bots.Planner 之上修正四个已知缺陷。

1. 研究顺序：先沿“实验室链 + 验证包链”的前置走（R03→I03→C03/E04→R07/R05/C02→C04/C05→R09/I09/C10→R11），
   每开一项研究前确认它的验证包本国已能生产或可进口，否则先研究验证包的前置。
2. 实验室数量只受总劳动约束（不设科研劳动占比上限）。
3. 提前囤积下一阶研究所需验证包。
4. 资金充裕时用进口补足本国缺失或枯竭的原料。
"""
import functools
from sim import NODES, TIERS, RECIPES
from bots import Planner, ancestors, PRODUCERS, SPECIAL_FAC
from sim import MODES

PACK_TECH = {  # 验证包 -> 生产所需科技（含其投入品）
    'lab_supplies': set(), 'engineering_pack': set(),
    'precision_pack': {'I03', 'C01', 'I02', 'I01'},
    'system_pack': {'I03', 'C03', 'C02', 'E04', 'I02', 'I01'},
    'frontier_pack': {'R07', 'I03', 'C03', 'C02', 'E04', 'C04', 'C05', 'I06', 'I02', 'I01', 'C01'},
    'breakthrough_pack': {'R09', 'I09', 'C10', 'R07', 'C04', 'C05', 'C07', 'I05', 'C06', 'I06'},
}
SPINE = ['E03', 'I03', 'C01', 'I02', 'C03', 'C02', 'I05', 'C06', 'R03', 'E04', 'R05', 'R07', 'C04', 'C05', 'I06',
         'C07', 'I09', 'C10', 'R09', 'C09', 'E09', 'C11', 'R11']


class Ref(Planner):
    def __init__(self, name='ref', finals=('R12',), seed=0):
        super().__init__(name, 'deep', lab_share=1.0, target=[], seed=seed, imports=True)
        self.finals = list(finals)

    def tier_ready(self, n, tid):
        vg = TIERS[NODES[tid]['tier']]['validation_good']
        return PACK_TECH.get(vg, set()) <= n.known

    def pick(self, n):
        av = set(n.available_techs())
        if not av: return None
        goals = []
        for t in getattr(self, 'spine', SPINE) + self.finals:
            goals.append(t)
        for tgt in goals:
            need = (ancestors(tgt) | {tgt}) - n.known
            if not need: continue
            # 先补验证包所需科技
            cand = sorted((t for t in av if t in need), key=lambda t: (NODES[t]['tier'], t))
            for t in cand:
                if self.tier_ready(n, t): return t
            for t in cand:
                vg = TIERS[NODES[t]['tier']]['validation_good']
                for p in sorted(PACK_TECH.get(vg, set()) - n.known):
                    pn = (ancestors(p) | {p}) - n.known
                    c2 = sorted((x for x in av if x in pn), key=lambda x: NODES[x]['tier'])
                    if c2: return c2[0]
            if cand: return cand[0]
        rest = sorted(av, key=lambda t: (NODES[t]['tier'], t))
        ready = [t for t in rest if self.tier_ready(n, t)]
        return (ready or rest)[0]

    def auto_targets(self, n):
        if not n.can_mode('automated'): return 0
        return sum(1 for f in n.fac for ln in f.lines
                   if ln and ln[1] != 'automated' and RECIPES[ln[0]]['facility'] not in SPECIAL_FAC)

    def final_demand2(self, n, f):
        k = self.auto_targets(n)
        if k:
            for g, q in MODES['automated']['installation'].items():
                short = q * min(k, 30) - n.inv.get(g, 0)
                if short > 0: f[g] = f.get(g, 0) + short / 15.0
        return f

    def lab_choice(self, n, grade):
        from bots import LABS
        usable = [r for r in LABS if n.can_use(r) and RECIPES[r]['grade'] <= grade
                  and all(self.producible(n, k) or n.inv.get(k, 0) >= q for k, q in RECIPES[r]['installed'].items())]
        goal = usable[0] if usable else 'P075'
        # 当前能装上的：库存够装 2 座
        ready = [r for r in usable if all(n.inv.get(k, 0) >= 2 * q for k, q in RECIPES[r]['installed'].items())]
        return goal, (ready[0] if ready else 'P075')

    def final_demand(self, n, labs, lab_rid):
        f = Planner.final_demand(self, n, labs, lab_rid)
        goal = getattr(self, 'goal_lab', lab_rid)
        if goal != lab_rid or True:
            for k, q in RECIPES[goal]['installed'].items():
                short = q * max(labs, 4) * 1.2 - n.inv.get(k, 0)
                if short > 0: f[k] = f.get(k, 0) + short / 20.0
        if n.project:
            t = NODES[n.project[0]]['tier']
            for tt in (t, min(6, t + 1)):
                tier = TIERS[tt]; vg = tier['validation_good']
                if PACK_TECH.get(vg, set()) <= n.known:
                    short = tier['validation_amount'] * 1.2 - n.inv.get(vg, 0)
                    if short > 0: f[vg] = f.get(vg, 0) + short / 60.0
        return self.final_demand2(n, f)

    def plan(self, g, n):
        grade = self.buildable_grade(n)
        self.goal_lab, lab_rid = self.lab_choice(n, grade)
        labor = n.labor() * 0.93
        lo, hi, best = 1, 300, None
        while lo <= hi:
            mid = (lo + hi) // 2
            lines, raw, w, builds = self.evaluate(n, mid, lab_rid, grade)
            if w <= labor:
                best = (mid, lines, raw, builds, self.imports); lo = mid + 1
            else: hi = mid - 1
        if best is None:
            ev = self.evaluate(n, 1, lab_rid, grade); best = (1, ev[0], ev[1], ev[3], self.imports)
        labs, lines, raw, builds, self.imports = best
        # 库存只够装多少座就先装多少座
        inst = RECIPES[lab_rid]['installed']
        if inst:
            have = min(int(n.inv.get(k, 0) // q) for k, q in inst.items())
            cur = sum(1 for f in n.fac for l in f.lines if l and l[0] == lab_rid)
            labs = min(labs, cur + have)
        lines[lab_rid] = lines.get(lab_rid, 0) + max(1, labs)
        for i, m in enumerate(n.mines):
            if m.level == 'G1' and m.remaining == 0 and n.g2_ok(m.good) and n.inv.get('precision_parts', 0) >= 6:
                g.upgrade_mine(n, i)
        for gd, k in builds:
            for _ in range(k):
                if not g.build_mine(n, gd): break
        self.apply(g, n, lines)
        self.raw_need = raw; self.labs_now = labs
        # 把通用线逐步改造成自动化线（每次计划最多 4 条）
        if not n.can_mode('automated'): return
        done = 0
        inst = MODES['automated']['installation']
        order = sorted(range(len(n.fac)), key=lambda i: -max((RECIPES[l[0]]['workers'] for l in n.fac[i].lines if l), default=0))
        for fi in order:
            f = n.fac[fi]
            if f.remaining: continue
            for li, ln in enumerate(f.lines):
                if done >= 10: return
                if not ln or ln[1] == 'automated' or RECIPES[ln[0]]['facility'] in SPECIAL_FAC: continue
                if any(n.inv.get(k, 0) < q for k, q in inst.items()): return
                if g.assign(n, fi, li, ln[0], 'automated'): done += 1
