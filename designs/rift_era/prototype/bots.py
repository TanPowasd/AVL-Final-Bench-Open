"""原型参考 bot：需求展开式线性计划器。启发式，不代表最优策略。"""
import functools, math, random
from sim import NODES, TIERS, RECIPES, PROFILES, MODES, VALUE, RAW, EXTRACT, FOOD_PER_POP

LABS = ['P102', 'P079', 'P077', 'P076', 'P075']
POWER = ['P086', 'P083', 'P073', 'P072', 'P071', 'P070']
SPECIAL_FAC = ('电站', '实验室', '农场', '受控农场', '水厂')

@functools.lru_cache(None)
def ancestors(tid):
    t = NODES[tid]
    parents = list(t['requires_all']) + ([min(t['requires_any'], key=lambda p: len(ancestors(p)))] if t['requires_any'] else [])
    out = set(parents)
    for p in parents: out |= ancestors(p)
    return frozenset(out)

PRODUCERS = {}
for r in RECIPES.values():
    for g in r['outputs']: PRODUCERS.setdefault(g, []).append(r)


class Blank:
    name = 'blank'
    def act(self, g, n): pass


class Planner:
    def __init__(self, name, policy='cheap', lab_share=0.25, target=(), seed=0, every=5, imports=False):
        self.name = name; self.policy = policy; self.lab_share = lab_share; self.target = list(target)
        self.rng = random.Random(seed); self.every = every; self.use_imports = imports; self.imports = {}

    # ---------- 研究 ----------
    def pick(self, n):
        av = n.available_techs()
        if not av: return None
        if self.policy == 'cheap':
            return min(av, key=lambda t: (NODES[t]['tier'], self.rng.random()))
        if self.policy == 'recipes':
            unlock = lambda t: sum(1 for r in RECIPES.values() if t in r['requires_all'])
            return min(av, key=lambda t: (NODES[t]['tier'] - 0.7 * unlock(t), self.rng.random()))
        if self.policy == 'deep':
            for tgt in self.target:
                need = (ancestors(tgt) | {tgt}) - n.known
                cand = [t for t in av if t in need]
                if cand: return min(cand, key=lambda t: (NODES[t]['tier'], t))
            return min(av, key=lambda t: (NODES[t]['tier'], self.rng.random()))
        return self.rng.choice(av)

    # ---------- 工具 ----------
    def max_grade(self, n):
        return max([f.grade for f in n.fac] + [1])

    def buildable_grade(self, n):
        best = 1
        for p in PROFILES.values():
            if all(n.inv.get(k, 0) >= q or self.producible(n, k) for k, q in p['build'].items()):
                best = max(best, p['recipe_grade'])
        return best

    def has_deposit(self, n, good):
        d = n.deposits.get(good)
        return d is not None and (d == math.inf or d > 0.08 * n.dep0[good])

    def producible(self, n, good, depth=0):
        if good in RAW: return self.has_deposit(n, good)
        return any(n.can_use(r['id']) for r in PRODUCERS.get(good, []))

    def choose(self, n, good, grade):
        best = None
        for r in PRODUCERS.get(good, []):
            if not n.can_use(r['id']) or r['grade'] > grade: continue
            if 'electricity' in r['outputs'] or 'research_points' in r['outputs']: continue
            if r['id'] in ('P066', 'P067', 'P068', 'P069'): continue      # 回收：本原型无废料来源
            if any(not self.producible(n, k) and n.inv.get(k, 0) < 1 for k in r['inputs']): continue
            if any(not self.producible(n, k) and n.inv.get(k, 0) < q for k, q in r['installed'].items()): continue
            cost = (sum(VALUE.get(k, 1) * q for k, q in r['inputs'].items()) + 0.1 * r['power'] + 0.3 * r['workers']) / r['outputs'][good]
            cost += sum(8.0 * r['inputs'][k] for k in r['inputs'] if k in RAW and not self.has_deposit(n, k)) / r['outputs'][good]
            if best is None or cost < best[0]: best = (cost, r['id'])
        return best and best[1]

    def raw_cost(self, n, good, depth=0):
        if good in RAW: return 1.0 if self.has_deposit(n, good) else 4.0
        rid = self.choose(n, good, 4) if depth < 6 else None
        if rid is None: return 4.0 * VALUE.get(good, 1)
        r = RECIPES[rid]
        return (sum(self.raw_cost(n, k, depth + 1) * q for k, q in r['inputs'].items()) + 0.3 * r['workers']) / r['outputs'][good]

    def power_recipe(self, n, grade):
        best = None
        for rid in POWER:
            r = RECIPES[rid]
            if not n.can_use(rid) or r['grade'] > grade: continue
            if any(not self.producible(n, k) and n.inv.get(k, 0) < q for k, q in r['installed'].items()): continue
            cost = (sum(self.raw_cost(n, k) * q for k, q in r['inputs'].items()) + 0.3 * r['workers']
                    + sum(VALUE.get(k, 1) * q for k, q in r['installed'].items()) / 300) / r['outputs']['electricity']
            if best is None or cost < best[0]: best = (cost, rid)
        return best[1] if best else 'P070'

    def mode(self, n, rid):
        if RECIPES[rid]['facility'] in SPECIAL_FAC: return 'general'
        for m in ('automated', 'specialized'):
            if n.can_mode(m) and all(n.inv.get(k, 0) >= q * 3 for k, q in MODES[m]['installation'].items()):
                return m
        return 'general'

    # ---------- 需求展开 ----------
    def expand(self, n, final, grade):
        lines = {}; raw = {}; elec = 0.0; workers = 0.0
        demand = dict(final)
        prid = self.power_recipe(n, min(grade, self.max_grade(n)))
        for _ in range(12):
            pending = {k: v for k, v in demand.items() if v > 1e-6}
            if not pending: break
            demand = {}
            for g, q in pending.items():
                if g in RAW:
                    raw[g] = raw.get(g, 0) + q; continue
                if g == 'electricity':
                    rid = prid
                else:
                    rid = self.choose(n, g, grade)
                if rid is None:
                    raw[g] = raw.get(g, 0) + q; continue      # 无法生产：进口
                r = RECIPES[rid]; md = MODES[self.mode(n, rid)]
                k = q / r['outputs'][g] / md['capacity']
                lines[rid] = lines.get(rid, 0) + k
                for a, b in r['inputs'].items(): demand[a] = demand.get(a, 0) + b * k
                if g != 'electricity':
                    demand['electricity'] = demand.get('electricity', 0) + r['power'] * k
                workers += r["workers"] * md["worker_factor"] * (k + 0.5)
        return lines, raw, workers

    def final_demand(self, n, labs, lab_rid):
        f = {'food': n.pop * FOOD_PER_POP * 1.08, 'steel': 4.0, 'cement': 4.0, 'basic_parts': 1.5}
        g = self.max_grade(n)
        if 'I03' in n.known: f['precision_parts'] = 0.4
        if {'I05', 'C06'} <= n.known: f['automation_module'] = 0.15
        if {'I09'} <= n.known: f['superalloy'] = 0.12
        if {'C10'} <= n.known: f['ai_core'] = 0.04
        r = RECIPES[lab_rid]
        for k, q in r['inputs'].items(): f[k] = f.get(k, 0) + q * labs
        f['electricity'] = f.get('electricity', 0) + r['power'] * labs
        # 验证包：按当前或下一项目所需阶
        tier = TIERS[NODES[n.project[0]]['tier']] if n.project else None
        if tier:
            rp = r['outputs']['research_points'] * max(1, labs) ** n.alpha()
            span = max(15.0, tier['theory_points'] / rp)
            have = n.inv.get(tier['validation_good'], 0)
            f[tier['validation_good']] = f.get(tier['validation_good'], 0) + max(0.0, tier['validation_amount'] * 1.1 - have) / span
        # 产线模式与装机件
        if n.can_mode('automated'):
            for k, q in MODES['automated']['installation'].items(): f[k] = f.get(k, 0) + 0.1 * q
        return f

    # ---------- 主循环 ----------
    def act(self, g, n):
        if n.project is None:
            t = self.pick(n)
            if t: g.research(n, t)
        if g.cycle % self.every: 
            self.emergency(g, n); return
        self.plan(g, n)
        self.trade(g, n)

    def emergency(self, g, n):
        for gd in ('coal', 'water'):
            if n.inv.get(gd, 0) < 30: g.buy(n, gd, 20)
        if n.inv.get('food', 0) < n.pop * FOOD_PER_POP * 2:
            g.buy(n, 'food', n.pop * FOOD_PER_POP * 2)

    def import_plan(self, n, final):
        """资金充裕时，用进口替代部分最终需求（每周期受 IMPORT_CAP 约束）。"""
        from sim import IMPORT_CAP, PRICE
        self.imports = {}
        budget = max(0.0, n.money - 30000) / 200      # 每周期可花的钱（约 200 周期摊销）
        room = IMPORT_CAP * 0.9
        for k in sorted(final, key=lambda x: -VALUE.get(x, 1)):
            if k in ('electricity',) or k in RAW: continue
            unit = VALUE.get(k, 1) * PRICE * 1.6
            q = min(final[k], room, budget / unit)
            if q <= 0.01: continue
            self.imports[k] = q; final[k] -= q; room -= q; budget -= q * unit
        return final

    def evaluate(self, n, labs, lab_rid, grade):
        """返回 (lines, raw, 总劳动, 新矿列表)。矿场的电与劳动并入需求后再展开一次。"""
        final = self.final_demand(n, labs, lab_rid)
        if self.use_imports: final = self.import_plan(n, final)
        extra_e = 0.0
        for _ in range(3):
            f2 = dict(final); f2['electricity'] = f2.get('electricity', 0) + extra_e
            lines, raw, w = self.expand(n, f2, grade)
            builds = self.mine_plan(n, raw)
            mine_e = sum(EXTRACT[m.good][m.level][1] for m in n.mines) + sum(EXTRACT[gd]['G1'][1] * k for gd, k in builds)
            if abs(mine_e - extra_e) < 0.5: break
            extra_e = mine_e
        mine_w = sum(EXTRACT[m.good][m.level][2] for m in n.mines) + sum(EXTRACT[gd]['G1'][2] * k for gd, k in builds)
        return lines, raw, w + mine_w + labs * RECIPES[lab_rid]['workers'] * MODES['general']['worker_factor'], builds

    def plan(self, g, n):
        grade = self.buildable_grade(n)
        lab_rid = next((r for r in LABS if n.can_use(r) and RECIPES[r]['grade'] <= grade
                        and all(self.producible(n, k) or n.inv.get(k, 0) >= q for k, q in RECIPES[r]['installed'].items())), 'P075')
        labor = n.labor() * 0.93
        lo, hi, best = 1, 300, None
        while lo <= hi:
            mid = (lo + hi) // 2
            lines, raw, w, builds = self.evaluate(n, mid, lab_rid, grade)
            imp = self.imports
            share_ok = mid * RECIPES[lab_rid]['workers'] * 1.4 <= self.lab_share * labor
            if w <= labor and share_ok:
                best = (mid, lines, raw, builds, imp); lo = mid + 1
            else: hi = mid - 1
        if best is None:
            ev = self.evaluate(n, 1, lab_rid, grade)
            best = (1, ev[0], ev[1], ev[3], self.imports)
        labs, lines, raw, builds, self.imports = best
        lines[lab_rid] = lines.get(lab_rid, 0) + labs
        for i, m in enumerate(n.mines):
            if m.level == 'G1' and m.remaining == 0 and n.g2_ok(m.good) and n.inv.get('precision_parts', 0) >= 6:
                g.upgrade_mine(n, i)
        for gd, k in builds:
            for _ in range(k):
                if not g.build_mine(n, gd): break
        self.apply(g, n, lines)
        self.raw_need = raw
        self.labs_now = labs

    def mine_plan(self, n, raw):
        cap = {}
        for m in n.mines:
            cap[m.good] = cap.get(m.good, 0) + EXTRACT[m.good][m.level][0]
        builds = []
        for gd, q in raw.items():
            if gd not in EXTRACT or not self.has_deposit(n, gd): continue
            short = q * 1.2 - cap.get(gd, 0)
            if short > 0:
                k = min(math.ceil(short / EXTRACT[gd]['G1'][0]), n.mine_slots[gd] - sum(1 for m in n.mines if m.good == gd))
                if k > 0: builds.append((gd, k))
        return builds

    def apply(self, g, n, lines):
        target = {rid: max(1, math.ceil(k * 1.08 - 0.15)) for rid, k in lines.items() if k > 0.02}
        cur = {}
        for fi, f in enumerate(n.fac):
            for li, ln in enumerate(f.lines):
                if ln: cur.setdefault(ln[0], []).append((fi, li))
        # 释放多余
        for rid, locs in cur.items():
            extra = len(locs) - target.get(rid, 0)
            for fi, li in locs[:max(0, extra)]:
                g.assign(n, fi, li, None)
        # 补足缺口：高等级配方优先占高等级设施
        need = []
        for rid, t in target.items():
            have = len(cur.get(rid, [])); 
            for _ in range(t - have): need.append(rid)
        need.sort(key=lambda r: -RECIPES[r]['grade'])
        for rid in need:
            r = RECIPES[rid]
            slot = None
            for fi, f in sorted(enumerate(n.fac), key=lambda x: x[1].grade):
                if f.grade < r['grade']: continue
                for li, ln in enumerate(f.lines):
                    if ln is None: slot = (fi, li); break
                if slot: break
            if slot is None:
                if not self.build_for(g, n, r['grade']): continue
                continue    # 新设施建成后下一轮再装
            m = self.mode(n, rid)
            if not g.assign(n, slot[0], slot[1], rid, m):
                if not g.assign(n, slot[0], slot[1], rid, 'general'):
                    for k, q in r['installed'].items():
                        if n.inv.get(k, 0) < q: g.buy(n, k, q - n.inv.get(k, 0))

    def build_for(self, g, n, grade):
        building = sum(1 for f in n.fac if f.remaining > 0)
        if building >= 4: return False
        for pid in sorted(PROFILES, key=lambda p: (PROFILES[p]['recipe_grade'], -PROFILES[p]['lines'])):
            p = PROFILES[pid]
            if p['recipe_grade'] < grade: continue
            if all(n.inv.get(k, 0) >= q for k, q in p['build'].items()):
                return g.build(n, pid)
        return False

    def trade(self, g, n):
        for k, q in self.imports.items():
            g.buy(n, k, q * self.every)
        raw = getattr(self, 'raw_need', {})
        # 进口：本国无矿或产不出的
        for gd, q in sorted(raw.items(), key=lambda x: -x[1]):
            if not self.has_deposit(n, gd) or gd not in EXTRACT:
                if n.inv.get(gd, 0) < q * 8:
                    g.buy(n, gd, q * self.every * 1.1)
        if n.project:
            tier = TIERS[NODES[n.project[0]]['tier']]; vg = tier['validation_good']
            if n.project[1] >= tier['theory_points'] and n.inv.get(vg, 0) < tier['validation_amount']:
                g.buy(n, vg, tier['validation_amount'] - n.inv.get(vg, 0))
        # 出售长期积压
        for gd, q in list(n.inv.items()):
            if q > 400 and gd not in ('food', 'water'):
                g.sell(n, gd, q - 300)


def roster(seed=0):
    return {
        'blank': lambda: Blank(),
        'cheap': lambda: Planner('cheap', 'cheap', lab_share=0.15, seed=seed),
        'industry': lambda: Planner('industry', 'recipes', lab_share=0.10, seed=seed),
        'rush_fusion': lambda: Planner('rush_fusion', 'deep', lab_share=0.35, target=['E11', 'R11'], seed=seed),
        'rush_cyber': lambda: Planner('rush_cyber', 'deep', lab_share=0.35, target=['S08', 'C12'], seed=seed),
        'rush_orbit': lambda: Planner('rush_orbit', 'deep', lab_share=0.35, target=['N12', 'N11'], seed=seed),
        'rush_heavy': lambda: Planner('rush_heavy', 'deep', lab_share=0.55, target=['R03', 'R07', 'R05', 'R11', 'E11'], seed=seed),
        'labs_first': lambda: Planner('labs_first', 'deep', lab_share=0.40, target=['R03', 'R07', 'R05', 'E11', 'R11'], seed=seed),
        'trader_labs': lambda: Planner('trader_labs', 'deep', lab_share=0.60, target=['R03', 'R07', 'R05', 'E11', 'R11'], seed=seed, imports=True),
        'trader_cheap': lambda: Planner('trader_cheap', 'cheap', lab_share=0.30, seed=seed, imports=True),
    }
