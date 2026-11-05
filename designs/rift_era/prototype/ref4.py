"""超级参考策略 ref4.Super：评分用的强参考 R。

在 ref3.Exact（精确记账、分阶段、滞回）之上：
- 配方选择把劳动视为稀缺资源（劳动权重 2.0，原 0.3），并降低进口原料的惩罚，
  于是会主动选受控环境农业（P030）、核电（P073）这类省人力的配方，缺的原料靠进口；
- 研究路线插入 A02（合成肥料）、A05（受控环境农业）、E05（核电）；
- 3 级设施批量建造：缺口一次最多 12 条，可同时建 2 座。
"""
import math
from sim import RECIPES, VALUE, RAW, EXTRACT
from bots import PRODUCERS, POWER, Planner
import ref3

LW = 3.0          # 每名工人折算的成本
RAW_PEN = 3.0     # 本国无矿原料的额外成本（ref3 为 8.0）


class Super(ref3.Exact):
    buf_a, buf_b, buf_t, buf_neg, buf_pos = 20.0, 5.0, 20.0, 1.0, 0.0
    def __init__(self, name='super', finals=('I11',), seed=0, every=10, spine=None, lw=LW, raw_pen=RAW_PEN, fac_batch=12):
        spine = spine or ['E03', 'I03', 'C01', 'I02', 'C03', 'C02', 'I05', 'C06', 'A02', 'R03', 'E04', 'R05',
                          'A05', 'E05', 'R07', 'C04', 'C05', 'I06', 'C07', 'I09', 'C10', 'R09']
        super().__init__(name, finals, seed, every, spine)
        self.lw = lw; self.raw_pen = raw_pen; self.fac_batch = fac_batch

    def choose(self, n, good, grade):
        best = None
        for r in PRODUCERS.get(good, []):
            if not n.can_use(r['id']) or r['grade'] > grade: continue
            if 'electricity' in r['outputs'] or 'research_points' in r['outputs']: continue
            if r['id'] in ('P066', 'P067', 'P068', 'P069'): continue
            if any(not self.producible(n, k) and n.inv.get(k, 0) < 1 and k not in RAW for k in r['inputs']): continue
            if any(not self.producible(n, k) and n.inv.get(k, 0) < q for k, q in r['installed'].items()): continue
            out = r['outputs'][good]
            cost = (sum(VALUE.get(k, 1) * q for k, q in r['inputs'].items()) + 0.1 * r['power'] + self.lw * r['workers']) / out
            cost += sum(self.raw_pen * r['inputs'][k] for k in r['inputs'] if k in RAW and not self.has_deposit(n, k)) / out
            if best is None or cost < best[0]: best = (cost, r['id'])
        return best and best[1]

    def raw_cost(self, n, good, depth=0):
        if good in RAW: return 1.0 if self.has_deposit(n, good) else 1.0 + self.raw_pen
        rid = self.choose(n, good, 4) if depth < 6 else None
        if rid is None: return 4.0 * VALUE.get(good, 1)
        r = RECIPES[rid]
        return (sum(self.raw_cost(n, k, depth + 1) * q for k, q in r['inputs'].items()) + self.lw * r['workers']) / r['outputs'][good]

    def power_recipe(self, n, grade):
        best = None
        for rid in POWER:
            r = RECIPES[rid]
            if not n.can_use(rid) or r['grade'] > grade: continue
            if any(not self.producible(n, k) and n.inv.get(k, 0) < q for k, q in r['installed'].items()): continue
            cost = (sum(self.raw_cost(n, k) * q for k, q in r['inputs'].items()) + self.lw * r['workers']
                    + sum(VALUE.get(k, 1) * q for k, q in r['installed'].items()) / 300) / r['outputs']['electricity']
            if best is None or cost < best[0]: best = (cost, rid)
        rid = best[1] if best else 'P070'
        self.power_goal = rid if rid != 'P070' else None
        return 'P070'

    def plan(self, g, n):
        super().plan(g, n)
        wf = getattr(self, 'want_fac', None)
        if wf:
            self.want_fac = (wf[0], min(self.fac_batch, max(wf[1], self.labs_now)))
            building = sum(1 for f in n.fac if f.remaining > 0 and f.grade >= wf[0])
            for _ in range(2 - building):
                if not self._build(g, n, wf[0]): break

    FOOD_RAW = ('natural_gas', 'crude_oil')

    def trade(self, g, n):
        # 进口额度有限：先保粮食链原料，再保实验室链，最后才是其他缺口
        raw = getattr(self, 'raw_need', {})
        need = {}
        for gd, q in raw.items():
            if gd in EXTRACT and not self.has_deposit(n, gd):
                need[gd] = q * 1.1
        for gd, q in getattr(self, 'deficit', {}).items():
            need[gd] = max(need.get(gd, 0), q)
        pri = lambda gd: (0 if gd in self.FOOD_RAW else 1, need[gd])   # 小额关键原料先买，煤这类大宗最后
        for gd in sorted(need, key=pri):
            if n.inv.get(gd, 0) < need[gd] * 12:
                g.buy(n, gd, need[gd] * self.every)
        for k, q in self.imports.items():
            g.buy(n, k, q * self.every)
        from sim import TIERS, NODES
        for tid, vleft in n.background:
            if vleft is None:
                tier = TIERS[NODES[tid]['tier']]; vg = tier['validation_good']
                if n.inv.get(vg, 0) < tier['validation_amount']:
                    g.buy(n, vg, tier['validation_amount'] - n.inv.get(vg, 0))
        for gd, q in list(n.inv.items()):
            if q > 600 and gd not in ('food', 'water', 'grain'):
                g.sell(n, gd, q - 500)

    def apply_exact(self, g, n, tgt):
        # 粮食保险：粮食库存不足 60 周期用量时，不拆基础农业线
        use = n.pop * 0.06
        if n.inv.get('food', 0) + n.inv.get('grain', 0) < 60 * use:
            have = sum(1 for f in n.fac for ln in f.lines if ln and ln[0] == 'P028')
            tgt = dict(tgt); tgt['P028'] = max(tgt.get('P028', 0), have)
        super().apply_exact(g, n, tgt)

    def expand(self, n, final, grade):
        """同 Planner.expand，但中间品按目标库存修正需求（目标 30 + 20 周期流量，偏差 20 周期内消化）。
        最终需求（含按缺口计算的投资需求）不抵扣，避免重复扣库存。"""
        from sim import MODES
        lines = {}; raw = {}; workers = 0.0
        seen = set()
        demand = dict(final); first = True
        prid = self.power_recipe(n, min(grade, self.max_grade(n)))
        for _ in range(12):
            pending = {k: v for k, v in demand.items() if v > 1e-6}
            if not pending: break
            demand = {}
            for g, q in pending.items():
                # 中间品目标库存控制：目标 = 30 + 20 周期流量；偏差按 20 周期修正（可正可负）
                if not first and g not in RAW and g not in ('food', 'water', 'electricity') and g not in seen:
                    seen.add(g)
                    tgt_stock = self.buf_a + self.buf_b * q
                    d = (tgt_stock - n.inv.get(g, 0)) / self.buf_t
                    d *= self.buf_neg if d < 0 else self.buf_pos
                    q = max(0.0, q + d)
                    if q <= 1e-6: continue
                if g in RAW:
                    raw[g] = raw.get(g, 0) + q; continue
                rid = prid if g == 'electricity' else self.choose(n, g, grade)
                if rid is None:
                    raw[g] = raw.get(g, 0) + q; continue
                r = RECIPES[rid]; md = MODES[self.mode(n, rid)]
                k = q / r['outputs'][g] / md['capacity']
                lines[rid] = lines.get(rid, 0) + k
                for a, b in r['inputs'].items(): demand[a] = demand.get(a, 0) + b * k
                if g != 'electricity':
                    demand['electricity'] = demand.get('electricity', 0) + r['power'] * k
                workers += r['workers'] * md['worker_factor'] * (k + 0.5)
            first = False
        return lines, raw, workers

    # ---- 去煤电：煤是钢、铜、玻璃、水泥、化工的唯一原料，不能拿来发电 ----
    def coal_lines(self, n):
        return sum(1 for f in n.fac for ln in f.lines if ln and ln[0] == 'P070')

    def final_demand(self, n, labs, lab_rid):
        f = super().final_demand(n, labs, lab_rid)
        if n.can_use('P072'):
            k = self.coal_lines(n)
            short = 2 * math.ceil(k * 10 / 6) + 4 - n.inv.get('renewable_equipment', 0)
            if short > 0: f['renewable_equipment'] = f.get('renewable_equipment', 0) + short / 15.0
        return f

    def solve(self, n, labs, lab_rid, grade):
        tgt, raw, builds, w = super().solve(n, labs, lab_rid, grade)
        if n.can_use('P072') and tgt.get('P070', 0) > 0:
            can = int(n.inv.get('renewable_equipment', 0) // 2) - max(0, tgt.get('P072', 0) - sum(1 for f in n.fac for ln in f.lines if ln and ln[0] == 'P072'))
            conv = min(tgt['P070'], 3, max(0, can * 6 // 10))
            if conv > 0:
                tgt['P070'] -= conv; tgt['P072'] = tgt.get('P072', 0) + math.ceil(conv * 10 / 6)
                if tgt['P070'] <= 0: del tgt['P070']
                w = self.exact(n, tgt, builds)[0]
        return tgt, raw, builds, w
