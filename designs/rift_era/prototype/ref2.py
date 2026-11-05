"""参考强策略 v1：分阶段建造顺序 + 滞回控制。

在 ref.Ref 的研究顺序、验证包囤积、自动化改造之上，修正 v0 的抖动：
- 每 10 周期才重新规划一次；
- 实验室类型粘滞：库存够装下当前全部实验室时才整体换代；
- 实验室数量平滑：每次最多 +3，劳动不足时才 −1；
- 产线释放加滞回：多出 2 条以上才释放，配方被完全移出目标连续 3 次才清零；
- 食品需求看库存：库存超过 40 周期消耗就按 0.95× 生产。
"""
import math
from sim import NODES, TIERS, RECIPES, MODES, FOOD_PER_POP
from bots import LABS, SPECIAL_FAC, Planner
import ref


class Staged(ref.Ref):
    def power_recipe(self, n, grade):
        rid = super().power_recipe(n, grade)
        inst = RECIPES[rid]['installed']
        # 新电站装机件库存不够时，沿用燃煤，防止拆旧不装新
        if rid != 'P070' and any(n.inv.get(k, 0) < 3 * q for k, q in inst.items()):
            self.power_goal = rid
            return 'P070'
        return rid

    def final_demand2(self, n, f):
        k = min(self.auto_targets(n), 30)
        if k:
            for g, q in MODES['automated']['installation'].items():
                short = q * k - n.inv.get(g, 0)
                if short > 0: f[g] = f.get(g, 0) + short / 20.0
        pg = getattr(self, 'power_goal', None)
        if pg:
            for g, q in RECIPES[pg]['installed'].items():
                short = q * 6 - n.inv.get(g, 0)
                if short > 0: f[g] = f.get(g, 0) + short / 40.0
        return f

    def __init__(self, name='ref2', finals=('R12',), seed=0, every=10):
        super().__init__(name, finals, seed)
        self.every = every
        self.lab = 'P075'; self.labs_prev = 1; self.absent = {}

    def final_demand(self, n, labs, lab_rid):
        f = super().final_demand(n, labs, lab_rid)
        use = n.pop * FOOD_PER_POP
        f['food'] = use * (0.95 if n.inv.get('food', 0) > 40 * use else 1.05)
        return f

    def lab_lines(self, n):
        out = {}
        for fi, fa in enumerate(n.fac):
            for li, ln in enumerate(fa.lines):
                if ln and 'research_points' in RECIPES[ln[0]]['outputs']:
                    out.setdefault(ln[0], []).append((fi, li))
        return out

    def plan(self, g, n):
        grade = self.buildable_grade(n)
        goal, _ = self.lab_choice(n, grade)
        self.goal_lab = goal
        cur = self.lab_lines(n)
        cnt = sum(len(v) for v in cur.values())
        if goal != self.lab and LABS.index(goal) < LABS.index(self.lab):
            inst = RECIPES[goal]['installed']
            if all(n.inv.get(k, 0) >= q * max(cnt, 2) for k, q in inst.items()):
                self.lab = goal
        lab_rid = self.lab
        labor = n.labor() * 0.95
        lo, hi, best = 1, 400, None
        while lo <= hi:
            mid = (lo + hi) // 2
            lines, raw, w, builds = self.evaluate(n, mid, lab_rid, grade)
            if w <= labor: best = (mid, lines, raw, builds, self.imports); lo = mid + 1
            else: hi = mid - 1
        if best is None:
            ev = self.evaluate(n, 1, lab_rid, grade); best = (1, ev[0], ev[1], ev[3], self.imports)
        want, lines, raw, builds, self.imports = best
        labs = min(want, self.labs_prev + 3)
        if n.labor_ratio < 0.97: labs = min(labs, self.labs_prev - 1)
        inst = RECIPES[lab_rid]['installed']
        if inst:
            have = min(int(n.inv.get(k, 0) // q) for k, q in inst.items())
            labs = min(labs, len(cur.get(lab_rid, [])) + have)
        labs = max(1, labs); self.labs_prev = labs; self.labs_now = labs
        lines = {k: v for k, v in lines.items() if 'research_points' not in RECIPES[k]['outputs']}
        lines[lab_rid] = labs
        for i, m in enumerate(n.mines):
            if m.level == 'G1' and m.remaining == 0 and n.g2_ok(m.good) and n.inv.get('precision_parts', 0) >= 8:
                g.upgrade_mine(n, i)
        for gd, k in builds:
            for _ in range(k):
                if not g.build_mine(n, gd): break
        self.apply(g, n, lines)
        self.raw_need = raw
        self.automate(g, n)

    def automate(self, g, n):
        if not n.can_mode('automated'): return
        inst = MODES['automated']['installation']; done = 0
        order = sorted(range(len(n.fac)), key=lambda i: -max((RECIPES[l[0]]['workers'] for l in n.fac[i].lines if l), default=0))
        for fi in order:
            f = n.fac[fi]
            if f.remaining: continue
            for li, ln in enumerate(f.lines):
                if done >= 10: return
                if not ln or ln[1] == 'automated' or RECIPES[ln[0]]['facility'] in SPECIAL_FAC: continue
                if any(n.inv.get(k, 0) < q for k, q in inst.items()): return
                if g.assign(n, fi, li, ln[0], 'automated'): done += 1

    def apply(self, g, n, lines):
        target = {rid: max(1, math.ceil(k * 1.05 - 0.1)) for rid, k in lines.items() if k > 0.02}
        cur = {}
        for fi, f in enumerate(n.fac):
            for li, ln in enumerate(f.lines):
                if ln: cur.setdefault(ln[0], []).append((fi, li))
        for rid, locs in cur.items():
            is_lab = 'research_points' in RECIPES[rid]['outputs']
            if rid in target:
                self.absent[rid] = 0
                extra = len(locs) - target[rid]
                if is_lab: drop = max(0, extra)
                else: drop = extra - 1 if extra >= 2 else 0
            else:
                self.absent[rid] = self.absent.get(rid, 0) + 1
                drop = len(locs) if (is_lab or self.absent[rid] >= 3) else 0
            # 自动化线最后释放
            locs = sorted(locs, key=lambda x: n.fac[x[0]].lines[x[1]][1] == 'automated')
            for fi, li in locs[:drop]: g.assign(n, fi, li, None)
        need = []
        for rid, t in target.items():
            for _ in range(t - len(cur.get(rid, []))): need.append(rid)
        need.sort(key=lambda r: -RECIPES[r]['grade'])
        free = [(fi, li) for fi, f in enumerate(n.fac) for li, ln in enumerate(f.lines) if ln is None and f.remaining == 0]
        for rid in need:
            r = RECIPES[rid]
            slot = next((s for s in free if n.fac[s[0]].grade >= r['grade']), None)
            if slot is None:
                self.build_for(g, n, r['grade']); continue
            m = self.mode(n, rid)
            if g.assign(n, slot[0], slot[1], rid, m) or g.assign(n, slot[0], slot[1], rid, 'general'):
                free.remove(slot)
