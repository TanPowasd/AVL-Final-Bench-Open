"""中等陪练 bot（medium.Medium，公开给参赛者的对手样例）。

独立实现，不依赖任何 ref*.py。策略画像：“广度学者 + 外贸补短”。

1. 研究：广度优先。永远研究当前可研究的最低阶科技（同阶按编号），不设目标路线。
2. 实验室：只用基础实验室 P075（不升级实验室类型，不建 S2 以上设施）。
   实验室数量按劳动余量逐个增减（每次规划最多 ±1）。
3. 生产：贪心补缺。按当前产线的稳态流量（每条通用线满负荷）算各物品净流量，
   找缺口最大的本国可产物品，按固定配方表补线，直到没有缺口；只用通用人工线。
4. 采掘：原料净流量为负时补 G1 采掘场（按尾矿衰减后的实际产量），没有矿或矿位满了就进口。
5. 外贸：本国配方表以外的东西（精密试验包、系统试验包等验证品）全部进口；
   只在项目理论接近完成时才按需买验证品。

v1.3 增加（提案 H，在原策略上加功能）：
6. 电力：仍只用燃煤（只建 S1），电力闭合计入市政负荷，备用系数 1.12（检修与强迫停运）；电网走廊按缺口扩建。
7. 人口：住房不足 5% 余量时补住宅区，人口目标封顶 POP_CAP。
8. 金融：F01 后全部公司上市 49% 并全额分红（只为现金）；F02 后按简单泰勒规则定利率
   （3% + 1.5 ×（年化通胀 − 2%），1–12%）；不碰外汇干预、债券与股票交易。开启跨国电力耦合。
9. AI：不建数据中心（不建 S2 以上设施）；AI 节点照常按广度优先研究。
"""
import math
from sim import NODES, TIERS, RECIPES, PROFILES, MODES, EXTRACT, FOOD_PER_POP, SLOTS
from sim import HOUSE_CAP, HOUSE_WORK, RES_POWER, SECTORS

# 本国自产的固定配方表（物品 → 配方）；表外物品一律进口
MAKE = {
    'food': 'P031', 'grain': 'P028', 'steel': 'P001', 'copper': 'P002', 'glass': 'P004',
    'cement': 'P005', 'chemicals': 'P007', 'basic_parts': 'P011', 'lab_supplies': 'P062',
    'engineering_pack': 'P063', 'electricity': 'P070',
}
LAB = 'P075'
MANAGED = set(MAKE.values()) | {LAB}
PLAN_EVERY = 10
MINE_GOODS = sorted(EXTRACT)


def tech_order_key(tid):
    return (NODES[tid]['tier'], tid)


class Medium:
    name = 'medium'
    LAB_CAP = 40          # 基础实验室数量上限（实验室设施槽位规划）
    LABOR_USE = 0.97      # 规划时使用的劳动比例（v1.3.1 重标定 0.94 → 0.97）
    POP_CAP = 800.0       # v1.3.1 重标定（槽位随住宅区扩张后）

    def __init__(self):
        self.labs = 1
        self.raw_short = {}
        self.ipo_done = False

    # ---------- 研究 ----------
    def pick(self, n):
        av = n.available_techs()
        return min(av, key=tech_order_key) if av else None

    # ---------- 流量 ----------
    @staticmethod
    def mine_out(n, m):
        out = EXTRACT[m.good][m.level][0]
        dep = n.deposits[m.good]
        if dep != math.inf:
            rho = dep / n.dep0[m.good]
            out *= 1.0 if rho >= 0.3 else (0.25 + 0.75 * rho / 0.3 if rho > 0 else 0.0)
        return out

    def demand(self, n):
        hs = 0.002 * n.pop / HOUSE_CAP + max(0.0, self.house_target(n) - n.housing_cap()) / HOUSE_CAP / 40.0
        d = {'food': n.pop * FOOD_PER_POP * 1.04 + 0.5,
             'steel': 1.2 + 5 * hs, 'cement': 1.2 + 20 * hs, 'basic_parts': 0.5}
        if n.inv.get('food', 0) < n.pop * FOOD_PER_POP * 20:
            d['food'] += n.pop * FOOD_PER_POP * 0.15
        nxt = self.next_tier(n)
        if nxt == 2 and n.inv.get('engineering_pack', 0) < 16:
            d['engineering_pack'] = 0.4
        if n.inv.get('lab_supplies', 0) < 30:
            d['lab_supplies'] = 0.5
        return d

    def next_tier(self, n):
        if n.project: return NODES[n.project[0]]['tier']
        av = n.available_techs()
        return NODES[min(av, key=tech_order_key)]['tier'] if av else 0

    def balance(self, n, target, mines):
        """target: rid -> 线数。返回 (净流量, 劳动, 电力缺口)。"""
        net = {}
        for g, q in self.demand(n).items(): net[g] = net.get(g, 0) - q
        labor = 0.0; power = 0.0; gen = 0.0
        gm = MODES['general']
        for rid, k in target.items():
            if k <= 0: continue
            r = RECIPES[rid]
            for g, q in r['inputs'].items(): net[g] = net.get(g, 0) - q * k
            for g, q in r['outputs'].items():
                if g == 'electricity': gen += q * k
                elif g != 'research_points': net[g] = net.get(g, 0) + q * k
            if 'electricity' in r['outputs']: labor += r['workers'] * k
            else:
                labor += r['workers'] * gm['worker_factor'] * k
                power += r['power'] * gm['power_factor'] * k
        for good, cnt, out in mines:
            net[good] = net.get(good, 0) + out
            labor += EXTRACT[good]['G1'][2] * cnt; power += EXTRACT[good]['G1'][1] * cnt
        net['electricity'] = gen - (power + RES_POWER * n.pop) * 1.12
        return net, labor

    def mine_table(self, n):
        t = {}
        for m in n.mines:
            c, o = t.get(m.good, (0, 0.0))
            t[m.good] = (c + 1, o + self.mine_out(n, m))
        return t

    def expand(self, n, labs, base):
        target = dict(base); target[LAB] = labs
        mt = self.mine_table(n)
        add_mines = {}
        for _ in range(80):
            mines = []
            for gd in MINE_GOODS:
                c, o = mt.get(gd, (0, 0.0)); k = add_mines.get(gd, 0)
                if c or k: mines.append((gd, c + k, o + k * EXTRACT[gd]['G1'][0] * self.eta(n, gd)))
            net, labor = self.balance(n, target, mines)
            worst = None
            for g in sorted(net):
                if net[g] >= -1e-6 or g not in MAKE: continue
                if worst is None or net[g] < net[worst]: worst = g
            if worst is not None:
                r = RECIPES[MAKE[worst]]
                target[MAKE[worst]] = target.get(MAKE[worst], 0) + max(1, math.ceil(-net[worst] / r['outputs'][worst] - 0.15))
                continue
            changed = False
            for g in MINE_GOODS:
                if net.get(g, 0) >= 0 or g not in n.deposits or self.eta(n, g) < 0.3: continue
                have = mt.get(g, (0, 0))[0] + add_mines.get(g, 0)
                if have < n.mine_slots[g]:
                    add_mines[g] = add_mines.get(g, 0) + 1; changed = True
            if not changed: break
        short = {g: -net[g] for g in MINE_GOODS if net.get(g, 0) < 0}
        return target, add_mines, labor, short

    def eta(self, n, g):
        dep = n.deposits.get(g)
        if dep is None: return 0.0
        if dep == math.inf: return 1.0
        rho = dep / n.dep0[g]
        return 1.0 if rho >= 0.3 else (0.25 + 0.75 * rho / 0.3 if rho > 0 else 0.0)

    # ---------- 主循环 ----------
    def act(self, g, n):
        if g.cycle == 0: g.set_power_coupling(n, True)
        if n.project is None:
            t = self.pick(n)
            if t: g.research(n, t)
        if g.cycle % PLAN_EVERY == 0:
            self.plan(g, n)
            self.v13(g, n)
        self.trade(g, n)

    # ---------- v1.3 ----------
    def house_target(self, n):
        return min(n.pop * 1.05 + 40.0, self.POP_CAP)

    def v13(self, g, n):
        k = math.ceil((self.house_target(n) - n.housing_cap() - HOUSE_CAP * len(n.housing_build)) / HOUSE_CAP)
        if k > 0: g.build_housing(n, min(k, 2))
        if n.grid_need > 0.7 * n.grid_cap and not n.grid_build:
            for gd, q in (('steel', 6), ('copper', 6), ('cement', 4)):
                if n.inv.get(gd, 0) < q: g.buy(n, gd, q - n.inv.get(gd, 0))
            g.build_grid(n, 1)
        if 'F01' in n.known and not self.ipo_done:
            for s in SECTORS: g.ipo(n, s, 0.49); g.set_payout(n, s, 1.0)
            self.ipo_done = True
        if 'F02' in n.known:            # 简单泰勒规则：年化通胀高于 2% 就加息
            r = min(12.0, max(1.0, 3.0 + 1.5 * (n.pi * 1e4 - 2.0)))
            if abs(r - n.rate) >= 0.5: g.set_rate(n, round(r * 2) / 2)

    def plan(self, g, n):
        budget = (n.labor() - HOUSE_WORK * n.housing) * self.LABOR_USE * (1.0 if n.food_ratio >= 0.8 else 0.6)
        base = {}
        best = None
        for labs in (self.labs + 1, self.labs, self.labs - 1, self.labs - 2, self.labs - 4, 0):
            if labs < 0 or labs > self.LAB_CAP: continue
            res = self.expand(n, labs, base)
            if res[2] <= budget:
                best = (labs, res); break
        if best is None: best = (0, self.expand(n, 0, base))
        self.labs, (target, add_mines, _, short) = best
        self.raw_short = short
        for gd in sorted(add_mines):
            for _ in range(add_mines[gd]):
                if not g.build_mine(n, gd): break
        self.apply(g, n, target)

    def apply(self, g, n, target):
        cur = {}
        free = []
        for fi, f in enumerate(n.fac):
            for li, ln in enumerate(f.lines):
                if ln is None: free.append((fi, li))
                else: cur.setdefault(ln[0], []).append((fi, li))
        # 卸掉不在表里的产线和多余产线
        for rid in sorted(cur):
            locs = cur[rid]
            want = target.get(rid, 0) if rid in MANAGED else 0
            for fi, li in locs[want:]:
                g.assign(n, fi, li, None); free.append((fi, li))
        free.sort()
        building = sum(1 for f in n.fac if f.remaining > 0)
        for rid in sorted(target, key=lambda r: (r != 'P031', r != 'P028', r != 'P070', r)):
            need = target[rid] - len(cur.get(rid, []))
            for _ in range(max(0, need)):
                if free:
                    fi, li = free.pop(0)
                    g.assign(n, fi, li, rid)
                elif building < 4 and len(n.fac) < n.slot_cap() and g.build(n, 'S1'):
                    building += 1
                    fi = len(n.fac) - 1; g.assign(n, fi, 0, rid)
                else:
                    for k, q in PROFILES['S1']['build'].items():
                        if n.inv.get(k, 0) < q: g.buy(n, k, q - n.inv.get(k, 0))
                    return

    def trade(self, g, n):
        # 食品应急
        need = n.pop * FOOD_PER_POP
        if n.inv.get('food', 0) < need * 3:
            g.buy(n, 'food', need * 3 - n.inv.get('food', 0))
        # 原料缺口进口
        for gd in sorted(self.raw_short):
            if n.inv.get(gd, 0) < self.raw_short[gd] * 10:
                g.buy(n, gd, self.raw_short[gd] * 1.1)
        # 验证品：项目理论完成 70% 后备货
        want = {}
        for tid, vleft in n.background:
            if vleft is None:
                t = TIERS[NODES[tid]['tier']]; want[t['validation_good']] = want.get(t['validation_good'], 0) + t['validation_amount']
        if n.project:
            t = TIERS[NODES[n.project[0]]['tier']]
            if n.project[1] >= 0.7 * t['theory_points']:
                want[t['validation_good']] = want.get(t['validation_good'], 0) + t['validation_amount']
        for vg in sorted(want):
            if vg in MAKE: continue
            short = want[vg] - n.inv.get(vg, 0)
            if short > 0 and n.money > 3000: g.buy(n, vg, short)
        # 积压出售
        for gd in sorted(n.inv):
            if gd in ('consumer_goods', 'ammunition') and n.inv[gd] > 0:
                g.sell(n, gd, n.inv[gd])
