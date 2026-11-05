"""参考强策略 v2：整数产线精确记账 + 分阶段投资 + 滞回。

与 v0/v1 的区别：规划时不再估算劳动，而是把需求展开成整数产线后，按每条线的实际模式
精确计算劳动与电力（含采掘场），只在精确劳动 ≤ 可用劳动时才接受方案。
投资（自动化装机件、新电站装机件、验证包囤积）排在民生之后、实验室之前：
劳动不够时先砍实验室，再砍投资，最后才动民生。
"""
import math
from sim import NODES, TIERS, RECIPES, MODES, FOOD_PER_POP, EXTRACT
from bots import LABS, SPECIAL_FAC, Planner, ancestors
import ref

GEN = {'P070', 'P071', 'P072', 'P073', 'P074', 'P083', 'P086'}


class Exact(ref.Ref):
    def __init__(self, name='ref3', finals=('I11',), seed=0, every=10, spine=None):
        super().__init__(name, finals, seed)
        # 默认路线：自动化与验证包链 → 4 阶实验室 → 指定 6 阶终端（I11：含验证包链总成本最低）
        self.spine = spine or ['E03', 'I03', 'C01', 'I02', 'C03', 'C02', 'I05', 'C06', 'R03', 'E04', 'R05',
                               'R07', 'C04', 'C05', 'I06', 'C07', 'I09', 'C10', 'R09']
        self.every = every; self.lab = 'P075'; self.labs_prev = 1; self.invest = 1.0; self.mult = 1.0

    # ---- 需求 ----
    def final_demand(self, n, labs, lab_rid):
        f = Planner.final_demand(self, n, labs, lab_rid)
        use = n.pop * FOOD_PER_POP
        f['food'] = use * (0.97 if n.inv.get('food', 0) > 40 * use else 1.06)
        for k in ('automation_module', 'control_module'): f.pop(k, None)
        # 验证品：后台等待中的项目全额 + 当前项目及下一阶 1.2 倍（不受投资档位影响）
        want = {}
        for tid, vleft in n.background:
            if vleft is None:
                tier = TIERS[NODES[tid]['tier']]; vg = tier['validation_good']
                want[vg] = want.get(vg, 0) + tier['validation_amount']
        if n.project:
            t = NODES[n.project[0]]['tier']
            for tt in (t, min(6, t + 1)):
                tier = TIERS[tt]; vg = tier['validation_good']
                if ref.PACK_TECH.get(vg, set()) <= n.known:
                    want[vg] = want.get(vg, 0) + tier['validation_amount'] * (1.2 if tt == t else 0.6)
        for vg, q in want.items():
            short = q - n.inv.get(vg, 0)
            if short > 0: f[vg] = f.get(vg, 0) + short / 30.0
        # 实验室投入品缓冲：保持 40 周期用量（不受投资档位影响，属于科研必需）
        for lr_ in {lab_rid, getattr(self, 'goal_lab', lab_rid)}:
            for g, q in RECIPES[lr_]['inputs'].items():
                short = q * max(labs, 1) * 40 - n.inv.get(g, 0)
                if short > 0: f[g] = f.get(g, 0) + short / 20.0
        inv = self.invest
        if inv > 0:
            k = min(self.auto_targets(n), 10)
            if k:
                for g, q in MODES['automated']['installation'].items():
                    short = q * k - n.inv.get(g, 0)
                    if short > 0: f[g] = f.get(g, 0) + inv * short / 25.0
            goal = getattr(self, 'goal_lab', lab_rid)
            for lr_ in {goal, self.lab}:
                cur_k = sum(1 for fa in n.fac for ln in fa.lines if ln and ln[0] == lr_)
                for g, q in RECIPES[lr_]['installed'].items():
                    short = q * max(labs - cur_k, 4) - n.inv.get(g, 0)
                    if short > 0: f[g] = f.get(g, 0) + inv * short / 25.0
            wf = getattr(self, 'want_fac', None)
            if wf:
                import sim as _s
                pid = min((p for p in _s.PROFILES if _s.PROFILES[p]['recipe_grade'] >= wf[0]), key=lambda p: (_s.PROFILES[p]['recipe_grade'], -_s.PROFILES[p]['lines']))
                lines_per = _s.PROFILES[pid]['lines']
                for g, q in _s.PROFILES[pid]['build'].items():
                    short = q * math.ceil(wf[1] / lines_per) - n.inv.get(g, 0)
                    if short > 0: f[g] = f.get(g, 0) + inv * short / 20.0
            pg = getattr(self, 'power_goal', None)
            if pg:
                for g, q in RECIPES[pg]['installed'].items():
                    short = q * 8 - n.inv.get(g, 0)
                    if short > 0: f[g] = f.get(g, 0) + inv * short / 30.0
        return f

    def power_recipe(self, n, grade):
        rid = Planner.power_recipe(self, n, grade)
        self.power_goal = rid if rid != 'P070' else None
        return 'P070'

    # ---- 精确记账 ----
    def cur_modes(self, n):
        out = {}
        for f in n.fac:
            for ln in f.lines:
                if ln: out.setdefault(ln[0], []).append(ln[1])
        return out

    def exact(self, n, target, builds):
        modes = self.cur_modes(n); w = 0.0; p = 0.0; gen = 0.0
        for rid, t in target.items():
            r = RECIPES[rid]; ms = sorted(modes.get(rid, []), key=lambda m: MODES[m]['worker_factor'])
            for i in range(t):
                m = MODES[ms[i] if i < len(ms) else self.mode(n, rid)]
                w += r['workers'] * m['worker_factor']
                if rid in GEN: gen += r['outputs']['electricity']
                else: p += r['power'] * m['power_factor']
        for m in n.mines:
            o, pw, wk = EXTRACT[m.good][m.level]; w += wk; p += pw
        for gd, k in builds:
            o, pw, wk = EXTRACT[gd]['G1']; w += wk * k; p += pw * k
        return w, p, gen

    def integer(self, lines):
        return {rid: max(1, math.ceil(k * 1.04 - 0.08)) for rid, k in lines.items() if k > 0.02}

    def solve(self, n, labs, lab_rid, grade):
        lines, raw, _, builds = self.evaluate(n, labs, lab_rid, grade)
        lines = {k: v for k, v in lines.items() if k not in LABS}
        if labs: lines[lab_rid] = labs
        tgt = self.integer(lines)
        if labs: tgt[lab_rid] = labs
        # 电力：保留现有全部发电线，只用当前装得上的配方补缺口
        for r in list(tgt):
            if r in GEN: del tgt[r]
        cur_gen = {}
        for f in n.fac:
            for ln in f.lines:
                if ln and ln[0] in GEN: cur_gen[ln[0]] = cur_gen.get(ln[0], 0) + 1
        for r, k in cur_gen.items(): tgt[r] = k
        add = 'P070'
        pg = getattr(self, 'power_goal', None) or Planner.power_recipe(self, n, grade)
        if pg != 'P070' and all(n.inv.get(k, 0) >= 2 * q for k, q in RECIPES[pg]['installed'].items()):
            add = pg
        w, p, gen = self.exact(n, tgt, builds)
        if gen < p * 1.12:
            tgt[add] = tgt.get(add, 0) + math.ceil((p * 1.12 - gen) / RECIPES[add]['outputs']['electricity'])
        elif gen > p * 1.3:
            surplus = gen - p * 1.2
            for r in ('P070', 'P071', 'P074', 'P072'):
                if surplus <= 0 or tgt.get(r, 0) <= 0: continue
                e = RECIPES[r]['outputs']['electricity']
                drop = min(tgt[r], int(surplus / e)); tgt[r] -= drop; surplus -= drop * e
                if tgt[r] <= 0: del tgt[r]
        w, p, gen = self.exact(n, tgt, builds)
        return tgt, raw, builds, w

    def lab_lines(self, n):
        return sum(1 for f in n.fac for ln in f.lines if ln and ln[0] in LABS)

    def plan(self, g, n):
        grade = self.buildable_grade(n)
        goal, _ = self.lab_choice(n, grade); self.goal_lab = goal
        cnt = self.lab_lines(n)
        self.want_fac = None
        gr = RECIPES[goal]['grade']
        room = sum(1 for f in n.fac if f.grade >= gr and f.remaining == 0
                   for ln in f.lines if ln is None or ln[0] in LABS)
        lab_rid = self.lab
        eff = 1.0 if n.food_ratio >= 0.8 else 0.4 + 0.75 * n.food_ratio
        used = sum(RECIPES[l[0]]['workers'] * MODES[l[1]]['worker_factor'] for f in n.fac if f.remaining == 0 for l in f.lines if l)
        used += sum(EXTRACT[m.good][m.level][2] for m in n.mines if m.remaining == 0)
        avail = n.labor() * eff
        # 规划估算偏保守：按实际用工自适应放宽预算
        if n.labor_ratio < 0.99: self.mult = max(0.8, self.mult * 0.93)
        elif used < 0.9 * avail: self.mult = min(1.6, self.mult * 1.04)
        labor = avail * 0.97 * self.mult
        best = None
        for invest in (1.0, 0.5, 0.0):
            self.invest = invest
            lo, hi = (0 if invest > 0 else 1), 400
            while lo <= hi:
                mid = (lo + hi) // 2
                sol = self.solve(n, max(mid, 1), lab_rid, grade) if mid else self.solve(n, 0, lab_rid, grade)
                if sol[3] <= labor: best = (mid,) + sol; lo = mid + 1
                else: hi = mid - 1
            if best: break
        if best is None:
            self.invest = 0.0; sol = self.solve(n, 1, lab_rid, grade); best = (1,) + sol
        labs, tgt, raw, builds, w = best
        # 实验室数量平滑
        if labs > self.labs_prev + 3: labs = self.labs_prev + 3
        labs = max(1, labs)
        for r in LABS: tgt.pop(r, None)
        # 实验室按类型从好到差贪心填充：受设施等级空位与装机件库存限制，装不下的用次一级实验室补
        left = labs; used_room = {}
        for r in LABS[LABS.index(goal):]:
            if left <= 0: break
            if not n.can_use(r): continue
            gr_r = RECIPES[r]['grade']
            cur_r = sum(1 for f in n.fac for ln in f.lines if ln and ln[0] == r)
            room_r = sum(1 for f in n.fac if f.grade >= gr_r and f.remaining == 0
                         for ln in f.lines if ln is None or ln[0] in LABS) - sum(v for k, v in used_room.items() if k >= gr_r)
            inst = RECIPES[r]['installed']
            have = min([int(n.inv.get(k, 0) // q) for k, q in inst.items()] or [999])
            k = min(left, max(0, room_r), cur_r + have)
            if r == goal and room_r < left: self.want_fac = (gr_r, min(left - room_r, 6))
            if k > 0:
                tgt[r] = k; left -= k; used_room[gr_r] = used_room.get(gr_r, 0) + k
        if left > 0: tgt['P075'] = tgt.get('P075', 0) + left
        self.lab = max((r for r in tgt if r in LABS), key=lambda r: tgt[r], default='P075')
        self.labs_prev = labs; self.labs_now = labs; self.slack = labor - w
        for i, m in enumerate(n.mines):
            if m.level == 'G1' and m.remaining == 0 and n.g2_ok(m.good) and n.inv.get('precision_parts', 0) >= 8:
                g.upgrade_mine(n, i)
        for gd, k in builds:
            for _ in range(k):
                if not g.build_mine(n, gd): break
        self.apply_exact(g, n, tgt)
        wf = getattr(self, 'want_fac', None)
        if wf:
            building = sum(1 for f in n.fac if f.remaining > 0 and f.grade >= wf[0])
            if building == 0: self._build(g, n, wf[0])
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
                if done >= 3: return
                if not ln or ln[1] == 'automated' or RECIPES[ln[0]]['facility'] in SPECIAL_FAC: continue
                if any(n.inv.get(k, 0) < q for k, q in inst.items()): return
                if g.assign(n, fi, li, ln[0], 'automated'): done += 1

    def apply_exact(self, g, n, tgt):
        cur = {}
        for fi, f in enumerate(n.fac):
            for li, ln in enumerate(f.lines):
                if ln: cur.setdefault(ln[0], []).append((fi, li))
        keep_extra = getattr(self, 'slack', 0) > 8      # 有劳动余量时保留 1 条多余线，减少反复拆装
        for rid, locs in cur.items():
            t = tgt.get(rid, 0)
            extra = len(locs) - t
            if keep_extra and rid not in GEN and t > 0 and extra == 1:
                extra = 0
            if extra <= 0: continue
            locs = sorted(locs, key=lambda x: MODES[n.fac[x[0]].lines[x[1]][1]]['worker_factor'], reverse=True)
            for fi, li in locs[:extra]: g.assign(n, fi, li, None)
        need = []
        for rid, t in tgt.items():
            need += [rid] * max(0, t - len(cur.get(rid, [])))
        # 民生与电力优先
        pri = lambda r: (0 if r in GEN or r in ('P028', 'P031', 'P029', 'P030') else 1, -RECIPES[r]['grade'])
        need.sort(key=pri)
        free = [(fi, li) for fi, f in enumerate(n.fac) for li, ln in enumerate(f.lines) if ln is None and f.remaining == 0]
        for rid in need:
            r = RECIPES[rid]
            slot = next((s for s in free if n.fac[s[0]].grade >= r['grade']), None)
            if slot is None:
                self.build_for(g, n, r['grade']); continue
            if g.assign(n, slot[0], slot[1], rid, self.mode(n, rid)) or g.assign(n, slot[0], slot[1], rid, 'general'):
                free.remove(slot)

    def build_for(self, g, n, grade):
        building = sum(1 for f in n.fac if f.remaining > 0)
        if building >= 6: return False
        return Planner.build_for(self, g, n, grade) if building < 4 else self._build(g, n, grade)

    def _build(self, g, n, grade):
        for pid in sorted(__import__('sim').PROFILES, key=lambda p: (__import__('sim').PROFILES[p]['recipe_grade'], -__import__('sim').PROFILES[p]['lines'])):
            p = __import__('sim').PROFILES[pid]
            if p['recipe_grade'] >= grade and all(n.inv.get(k, 0) >= q for k, q in p['build'].items()):
                return g.build(n, pid)
        return False

    # ---- 采掘与进口：按尾矿衰减后的实际产出计 ----
    def eta(self, n, good):
        d = n.deposits.get(good)
        if d is None: return 0.0
        if d == math.inf: return 1.0
        rho = d / n.dep0[good]
        return 1.0 if rho >= 0.3 else (0.25 + 0.75 * rho / 0.3 if rho > 0 else 0.0)

    def mine_plan(self, n, raw):
        cap = {}
        for m in n.mines:
            cap[m.good] = cap.get(m.good, 0) + EXTRACT[m.good][m.level][0] * self.eta(n, m.good)
        builds = []; self.deficit = {}
        for gd, q in raw.items():
            if gd not in EXTRACT: continue
            e = self.eta(n, gd)
            short = q * 1.15 - cap.get(gd, 0)
            if short <= 0: continue
            if e > 0.2:
                room = n.mine_slots.get(gd, 0) - sum(1 for m in n.mines if m.good == gd)
                k = min(math.ceil(short / (EXTRACT[gd]['G1'][0] * e)), room)
                if k > 0: builds.append((gd, k)); short -= k * EXTRACT[gd]['G1'][0] * e
            if short > 0: self.deficit[gd] = short
        return builds

    def trade(self, g, n):
        Planner.trade(self, g, n)
        for gd, q in sorted(getattr(self, 'deficit', {}).items(), key=lambda x: -x[1]):
            if n.inv.get(gd, 0) < q * 20:
                g.buy(n, gd, q * self.every)
