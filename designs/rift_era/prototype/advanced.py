"""高级陪练 bot（advanced.Advanced，公开给参赛者的对手样例）。

独立实现，不依赖任何 ref*.py，也不与 medium.py 共用代码。策略画像：“固定系数计划 + 计算实验室突击”。

1. 研究：先纵深冲刺固定路线（土木 → 系统试验包配方链 → α 提升 → 计算研发 R07），
   之后按“边际性价比”贪心：理论点 ÷（理论点 + 验证品进口价折算），本国能产验证品的阶不计进口价；
   4 阶及以下研究完才碰 5 阶。
2. 生产：固定系数（投入产出）模板。科技变化时按本国矿藏重算一次“每单位最终品需要多少条产线、
   多少座矿、多少进口”的单位模板；每次规划只做线性叠加：线数 = Σ 最终需求 × 单位模板，向上取整，
   再按模板做电力闭合。不做运行时配方比较或试算。全部用通用人工线，不上专用/自动化线。
3. 实验室：R07 之前用基础实验室，数量由劳动余量线性解出；R07 之后改用固定模板：
   COMPUTE 座 S3 先进工厂各装一条计算实验室 P077（建材、装机件进口），基础实验室全部拆除。
4. 外贸：本国没有的矿、精密件以外的高阶装机件、维护件按需进口；系统/精密试验包自产，缺口才进口。

v1.3 增加（提案 H，在原策略上加功能）：
5. 电力：风光改为水电 P112（无燃料，按 0.85 来水折算），煤电保底；电力闭合计入市政负荷与数据中心；
   负荷带把工业排在实验室前；区间缺口超过走廊 70% 时扩建电网。
6. 人口：住宅区按“人口 × 1.05 + 50”维持，人口目标封顶 POP_CAP（固定槽位下的模板规模）。
7. 金融：F01 后全部公司一次上市 30%，分红 60%；F02 后利率 5%；F03 后把 100 万以上现金的 10% 配置到
   外国国债（票面高于本国利率 1 个百分点）与低估外国股票（公允值/市价 > 1.4，回到公允值卖出）；不发债、不融资融券。
8. AI：R07 之后研究 AI01/AI02，模板 1 座数据中心（加速器进口），只用 35% 算力持续训练；AI04 后推理算力按
   研究所需份额部署到研究（约占算力 5%），不卖推理服务、不做生产/金融部署（档位参数）。
9. 电力：开启跨国电力耦合（对方也开启时生效）。
"""
import math
from sim import NODES, TIERS, RECIPES, PROFILES, MODES, EXTRACT, FOOD_PER_POP, PRICE, VALUE, SLOTS
from sim import HOUSE_CAP, HOUSE_WORK, RES_POWER, SECTORS, DC_RECIPES

# 物品 → 自产配方（按掌握科技启用，未掌握就进口）
PRODUCE = {
    'food': 'P031', 'grain': 'P028', 'steel': 'P001', 'copper': 'P002', 'glass': 'P004',
    'cement': 'P005', 'chemicals': 'P007', 'basic_parts': 'P011', 'lab_supplies': 'P062',
    'engineering_pack': 'P063', 'electricity': 'P070', 'electronic_parts': 'P012',
    'pure_chemicals': 'P008', 'aluminum': 'P003', 'alloy': 'P009', 'precision_parts': 'P013',
    'chip': 'P015', 'communication_equipment': 'P018', 'battery': 'P023', 'sensor': 'P017',
    'system_pack': 'P065', 'precision_pack': 'P064',
}
BASIC_LAB, COMPUTE_LAB = 'P075', 'P077'
SPRINT = ['L01', 'I02', 'C01', 'C02', 'R02', 'E02', 'E03', 'C03', 'R05', 'C05', 'R07', 'I01', 'I03', 'E04']
GENERAL = MODES['general']
EVERY = 8
FILL_ORDER = ['P031', 'P028', 'P070', 'P112', 'P113', 'P072', 'P001', 'P005', 'P011', 'P002', 'P007', 'P004', 'P062', 'P063', 'P012',
              'P008', 'P003', 'P009', 'P013', 'P015', 'P018', 'P023', 'P017', 'P065', 'P064', BASIC_LAB]


def import_price(good):
    return VALUE.get(good, 1) * PRICE * 1.6


class Advanced:
    name = 'advanced'
    COMPUTE = 4            # 计算实验室模板座数（v1.3.1 重标定：Super 加强后由 3 → 4）
    LABOR_USE = 0.93
    POP_CAP = 900.0        # 人口目标上限（住宅区不再超建）
    HYDRO_CF = 0.85
    DC_MAX = 1
    IPO_FRAC = 0.30
    TRAIN_SHARE = 0.35     # 训练只用 35% 算力（其余闲置）：校准到 ≈0.3× Super 的档位参数
    AI_DEPLOY = True       # v1.3.1：推理算力部署到研究（所需份额 × RES_FILL），不卖推理服务
    RES_FILL = 1.0
    SEC_FRAC = 0.10        # 超过 SEC_FLOOR 的现金中投外国证券的比例
    SEC_FLOOR = 1_000_000.0

    def __init__(self):
        self.key = None
        self.lab_basic = 0
        self.imports = {}
        self.rp_est = 3.0
        self.ipo_done = False

    # ---------- 研究 ----------
    def pick(self, n):
        av = n.available_techs()
        if not av: return None
        for tid in SPRINT:
            if tid in av: return tid
        if 'R07' in n.known:
            for tid in ('AI01', 'AI02'):
                if tid in av: return tid
        if any(t not in n.known for t in SPRINT):
            low = sorted(t for t in av if NODES[t]['tier'] <= 2)
            return min(low, key=lambda t: (NODES[t]['tier'], t)) if low else None
        lim = 4 if any(NODES[t]['tier'] <= 4 for t in av) else 6

        def score(t):
            tr = TIERS[NODES[t]['tier']]
            vg = tr['validation_good']
            money = 0.0 if self.makes(n, vg) else tr['validation_amount'] * import_price(vg)
            return tr['theory_points'] / (tr['theory_points'] + money / 40.0)
        cand = [t for t in av if NODES[t]['tier'] <= lim]
        return max(cand, key=lambda t: (round(score(t), 6), -NODES[t]['tier'], t)) if cand else None

    def makes(self, n, good):
        rid = PRODUCE.get(good)
        if rid is None or not n.can_use(rid): return False
        # 计算研发之前只做 1 阶基础品；高阶配方链等计算实验室上线后再建
        return RECIPES[rid]['grade'] == 1 or ('L01' in n.known and 'R07' in n.known)

    # ---------- 固定系数模板 ----------
    def unit(self, n, good, depth=0):
        """每单位最终品（/周期）对应的 (产线, 矿, 进口) 系数。"""
        if good in self.memo: return self.memo[good]
        lines, mines, imp = {}, {}, {}
        if good in EXTRACT and good in n.deposits:
            out, pw, _ = EXTRACT[good]['G1']
            mines[good] = 1.0 / out
        elif self.makes(n, good) and depth < 14:
            r = RECIPES[PRODUCE[good]]
            per = 1.0 / r['outputs'][good]
            lines[r['id']] = per
            for g2, q in sorted(r['inputs'].items()):
                sub = self.unit(n, g2, depth + 1)
                for d, s in zip((lines, mines, imp), sub):
                    for k, v in s.items(): d[k] = d.get(k, 0) + v * q * per
        else:
            imp[good] = 1.0
        self.memo[good] = (lines, mines, imp)
        return self.memo[good]

    def compose(self, n, final):
        lines, mines, imp = {}, {}, {}
        for g in sorted(final):
            q = final[g]
            if q <= 0: continue
            for d, s in zip((lines, mines, imp), self.unit(n, g)):
                for k, v in s.items(): d[k] = d.get(k, 0) + v * q
        return lines, mines, imp

    @staticmethod
    def labor_of(lines, mines):
        lab = 0.0
        for rid, k in lines.items():
            r = RECIPES[rid]
            lab += r['workers'] * (1 if 'electricity' in r['outputs'] else GENERAL['worker_factor']) * k
        for gd, k in mines.items(): lab += EXTRACT[gd]['G1'][2] * k
        return lab

    # ---------- 规划 ----------
    def final_demand(self, n, basic_labs, compute_labs):
        f = {'food': n.pop * FOOD_PER_POP * 1.02 + 0.3, 'lab_supplies': basic_labs + compute_labs + 0.3}
        if n.inv.get('food', 0) < n.pop * FOOD_PER_POP * 15: f['food'] += n.pop * FOOD_PER_POP * 0.15
        for gd, rate in (('steel', 2.0), ('cement', 2.0), ('basic_parts', 0.8)):
            f[gd] = rate if n.inv.get(gd, 0) < 250 else 0.3
        if compute_labs: f['system_pack'] = 0.1 * compute_labs
        gap = max(0.0, self.house_target(n) - n.housing_cap()) / HOUSE_CAP
        f['cement'] += 20 * (0.002 * n.pop / HOUSE_CAP + gap / 40.0)
        f['steel'] += 5 * (0.002 * n.pop / HOUSE_CAP + gap / 40.0)
        # 当前项目所在阶的验证品：按预计理论用时摊到每周期
        tid = n.project[0] if n.project else None
        if tid:
            tr = TIERS[NODES[tid]['tier']]; vg = tr['validation_good']
            if self.makes(n, vg) and vg != 'lab_supplies':
                span = max(25.0, tr['theory_points'] / max(self.rp_est, 1.0))
                f[vg] = f.get(vg, 0) + tr['validation_amount'] * 1.1 / span
        if n.inv.get('engineering_pack', 0) < 16: f['engineering_pack'] = f.get('engineering_pack', 0) + 0.3
        return f

    def plan(self, g, n):
        key = len(n.known)
        if key != self.key: self.memo = {}; self.key = key
        if n.rp_log: self.rp_est = 0.8 * self.rp_est + 0.2 * (sum(n.rp_log[-EVERY:]) / len(n.rp_log[-EVERY:]))
        budget = (n.labor() - HOUSE_WORK * n.housing) * self.LABOR_USE * (1.0 if n.food_ratio >= 0.8 else 0.6)
        s3 = [(fi, f) for fi, f in enumerate(n.fac) if f.pid == 'S3' and not (f.lines[0] and f.lines[0][0] in DC_RECIPES)]
        compute_on = 'R07' in n.known
        compute_labs = min(self.COMPUTE, sum(1 for _, f in s3 if f.remaining == 0)) if compute_on else 0
        lab_w = RECIPES[BASIC_LAB]['workers'] * GENERAL['worker_factor']
        basic = 0
        if compute_labs == 0:
            base = self.compose(n, self.final_demand(n, 0, 0))
            one = self.compose(n, {'lab_supplies': 1.0})
            per = lab_w + self.labor_of(*one[:2]) + 0.4
            spare = budget - self.labor_of(*base[:2]) - 10.0
            basic = max(0, min(60, int(spare / per), self.lab_basic + 2))
        final = self.final_demand(n, basic, compute_labs)
        target, raw, imp = self.settle(n, self.round_up(self.compose(n, final)[0]), final, basic, compute_labs)
        while basic > 0 and self.labor_of(target, self.mine_count(n, raw)) > budget:
            basic -= 1
            final = self.final_demand(n, basic, 0)
            target, raw, imp = self.settle(n, self.round_up(self.compose(n, final)[0]), final, basic, 0)
        self.lab_basic = basic
        self.imports = imp
        self.mines_for(g, n, self.mine_count(n, raw))
        self.apply(g, n, target)
        if compute_on or (n.project and n.project[0] == 'R07') or any(b[0] == 'R07' for b in n.background):
            self.compute_wing(g, n, s3, compute_on)

    @staticmethod
    def round_up(lines):
        # 小于一条的配方链也至少保留 1 条线（否则高阶链会被取整吃掉）
        return {rid: max(1, math.ceil(k - 0.12)) for rid, k in lines.items() if k > 0.02}

    @staticmethod
    def mine_count(n, raw):
        return {g: q / EXTRACT[g]['G1'][0] for g, q in raw.items() if g in n.deposits}

    def wind_ok(self, n):
        return n.can_use('P112') and 'L01' in n.known      # v1.3：可再生扩容改用水电 P112

    def ren_rid(self, n, t):
        """可再生扩容配方：水电到河流潜力为止，之后风电 P113（有 E03 时），否则燃煤。"""
        if t.get('P112', 0) * 15.0 + 15.0 <= n.hydro_pot: return 'P112'
        return 'P113' if n.can_use('P113') else 'P070'

    def gen_out(self, rid):
        e = RECIPES[rid]['outputs']['electricity']
        return e * (self.HYDRO_CF if rid == 'P112' else 0.25 if rid == 'P072' else 0.30 if rid == 'P113' else 0.95)

    def settle(self, n, target, final, basic, compute_labs):
        """取整后按满负荷流量校正：补齐中间品与电力缺口，返回 (线数, 原矿需求, 进口)。"""
        t = dict(target)
        t[BASIC_LAB] = basic
        cur = {}; dc_pw = 0.0
        for f in n.fac:
            for ln in f.lines:
                if ln and ln[0] in ('P070', 'P072', 'P112', 'P113'): cur[ln[0]] = cur.get(ln[0], 0) + 1
                if ln and ln[0] in DC_RECIPES: dc_pw += RECIPES[ln[0]]['power']
        for rid_ in ('P070', 'P072', 'P112', 'P113'): t[rid_] = cur.get(rid_, 0)
        lab_pw = RECIPES[COMPUTE_LAB]['power'] * GENERAL['power_factor'] * compute_labs + dc_pw + RES_POWER * n.pop
        for _ in range(40):
            net = {g: -q for g, q in final.items()}
            use = lab_pw; gen = 0.0
            for rid, k in t.items():
                if k <= 0: continue
                r = RECIPES[rid]
                if 'electricity' in r['outputs']:
                    gen += self.gen_out(rid) * k
                    for g2, q in r['inputs'].items(): net[g2] = net.get(g2, 0) - q * k
                    continue
                use += r['power'] * GENERAL['power_factor'] * k
                if rid == BASIC_LAB: continue
                for g2, q in r['inputs'].items(): net[g2] = net.get(g2, 0) - q * k
                for g2, q in r['outputs'].items(): net[g2] = net.get(g2, 0) + q * k
            raw = {g: -v for g, v in net.items() if g in EXTRACT and v < 0}
            use += sum(EXTRACT[g]['G1'][1] * math.ceil(q / EXTRACT[g]['G1'][0]) for g, q in raw.items() if g in n.deposits)
            net['electricity'] = gen - use * 1.10
            worst = None
            for g in sorted(net):
                if net[g] < -0.05 and g not in EXTRACT and (g == 'electricity' or self.makes(n, g)):
                    if worst is None or net[g] < net[worst]: worst = g
            if worst is None: break
            if worst == 'electricity':
                rid = self.ren_rid(n, t) if self.wind_ok(n) else 'P070'
                k = max(1, math.ceil(-net[worst] / self.gen_out(rid) - 0.1))
                if rid == 'P112': k = min(k, max(1, int((n.hydro_pot - 15.0 * t.get('P112', 0)) // 15.0)))
                t[rid] = t.get(rid, 0) + k
            else:
                rid = PRODUCE[worst]
                t[rid] = t.get(rid, 0) + max(1, math.ceil(-net[worst] / RECIPES[rid]['outputs'][worst] - 0.15))
        if self.wind_ok(n) and t.get('P070', 0) > 0 and net['electricity'] >= 10 and self.ren_rid(n, t) != 'P070':
            t['P070'] -= 1                 # 有风光后逐步退煤电
        imp = {g: -v for g, v in net.items() if v < 0 and g != 'electricity' and g not in EXTRACT and not self.makes(n, g)}
        for g, q in raw.items():
            if g not in n.deposits: imp[g] = q
        return t, raw, imp

    def mines_for(self, g, n, mines):
        for gd in sorted(mines):
            dep = n.deposits.get(gd, 0)
            rho = 1.0 if dep == math.inf else dep / n.dep0[gd]
            eta = 1.0 if rho >= 0.3 else 0.25 + 0.75 * rho / 0.3
            out = EXTRACT[gd]['G1'][0]
            if rho < 0.05:
                self.imports[gd] = self.imports.get(gd, 0) + mines[gd] * out
                continue
            want = math.ceil(mines[gd] * 1.1 / eta - 0.1)
            have = sum(1 for m in n.mines if m.good == gd)
            if want > n.mine_slots[gd]:
                self.imports[gd] = self.imports.get(gd, 0) + (mines[gd] * 1.1 - n.mine_slots[gd] * eta) * out
            for _ in range(min(min(want, n.mine_slots[gd]) - have, 2)):     # 矿位已满不再下单（否则是被拒命令）
                if not g.build_mine(n, gd): break

    def apply(self, g, n, target):
        cur, free = {}, []
        for fi, f in enumerate(n.fac):
            if f.pid == 'S3': continue
            for li, ln in enumerate(f.lines):
                if ln is None: free.append((f.grade, fi, li))
                else: cur.setdefault(ln[0], []).append((fi, li))
        for rid in sorted(cur):
            want = target.get(rid, 0)
            for fi, li in cur[rid][want:]:
                g.assign(n, fi, li, None); free.append((n.fac[fi].grade, fi, li))
        free.sort()
        building = {1: 0, 2: 0}
        for f in n.fac:
            if f.remaining > 0 and f.pid in ('S1', 'S2'): building[f.grade] += 1
        stalled = set()
        for rid in sorted(target, key=lambda r: FILL_ORDER.index(r) if r in FILL_ORDER else 99):
            gr = RECIPES[rid]['grade']
            for _ in range(max(0, target[rid] - len(cur.get(rid, [])))):
                slot = next((s for s in free if s[0] >= gr), None)
                if slot:
                    for k, q in sorted(RECIPES[rid]['installed'].items()):
                        if n.inv.get(k, 0) < q: g.buy(n, k, q - n.inv.get(k, 0))
                    if any(n.inv.get(k, 0) < q for k, q in RECIPES[rid]['installed'].items()):
                        continue                    # 装机件没买到（如 P113 的 renewable_equipment）：不下会被拒的装线命令
                    if g.assign(n, slot[1], slot[2], rid): free.remove(slot)
                    continue
                if gr in stalled: break
                pid = 'S1' if gr == 1 else 'S2'
                if building[gr] < (5 if gr == 1 else 3) and len(n.fac) < n.slot_cap() and g.build(n, pid):
                    building[gr] += 1
                    if all(n.inv.get(k, 0) >= q for k, q in RECIPES[rid]['installed'].items()):
                        g.assign(n, len(n.fac) - 1, 0, rid)
                else:
                    stalled.add(gr)
                    if building[gr] == 0:
                        for k, q in sorted(PROFILES[pid]['build'].items()):
                            if n.inv.get(k, 0) < q: g.buy(n, k, q - n.inv.get(k, 0))
                    break

    def compute_wing(self, g, n, s3, compute_on):
        """按模板建 COMPUTE 座 S3（一次一座），每座一条计算实验室线。"""
        if len(s3) < self.COMPUTE and 'L01' in n.known and not any(f.remaining > 0 for _, f in s3):
            p = PROFILES['S3']['build']
            for k in sorted(p):
                short = p[k] - n.inv.get(k, 0)
                if short > 0: g.buy(n, k, short)
            if all(n.inv.get(k, 0) >= q for k, q in p.items()):
                g.build(n, 'S3')
        if not compute_on: return
        labs = sum(1 for _, f in s3 if f.lines[0] and f.lines[0][0] == COMPUTE_LAB)
        for fi, f in s3:
            if f.lines[0] is None and labs < self.COMPUTE:
                labs += 1
                inst = RECIPES[COMPUTE_LAB]['installed']
                for k in sorted(inst):
                    short = inst[k] - n.inv.get(k, 0)
                    if short > 0: g.buy(n, k, short)
                g.assign(n, fi, 0, COMPUTE_LAB)

    # ---------- 每周期 ----------
    def act(self, g, n):
        if g.cycle == 0: g.set_load_priority(n, ['B1', 'B3', 'B2', 'B4']); g.set_power_coupling(n, True)
        if n.project is None:
            t = self.pick(n)
            if t: g.research(n, t)
        if g.cycle % EVERY == 0:
            self.plan(g, n)
            self.v13(g, n)
        self.trade(g, n)
        k = math.ceil((self.house_target(n) - n.housing_cap() - HOUSE_CAP * len(n.housing_build)) / HOUSE_CAP)
        if k > 0: g.build_housing(n, min(k, 3))

    # ---------- v1.3 ----------
    def house_target(self, n):
        return min(n.pop * 1.05 + 50.0, self.POP_CAP)

    def v13(self, g, n):
        if n.grid_need > 0.7 * n.grid_cap and not n.grid_build:
            for k, q in sorted({'steel': 6, 'copper': 6, 'cement': 4}.items()):
                if n.inv.get(k, 0) < q: g.buy(n, k, q - n.inv.get(k, 0))
            g.build_grid(n, 1)
        if 'F01' in n.known and not self.ipo_done:
            for s in SECTORS: g.ipo(n, s, self.IPO_FRAC); g.set_payout(n, s, 0.6)
            self.ipo_done = True
        if 'F02' in n.known and n.rate != 5.0: g.set_rate(n, 5.0)
        if 'F03' in n.known: self.portfolio(g, n)
        if 'AI01' not in n.known: return
        dcs = [(fi, li) for fi, f in enumerate(n.fac) for li, ln in enumerate(f.lines) if ln and ln[0] in DC_RECIPES]
        if len(dcs) < self.DC_MAX and 'AI02' in n.known and n.money > 120000:
            spare = next(((fi, 0) for fi, f in enumerate(n.fac) if f.pid == 'S3' and f.remaining == 0 and f.lines[0] is None), None)
            if spare is None and not any(f.pid == 'S3' and f.remaining > 0 for f in n.fac):
                p = PROFILES['S3']['build']
                for k in sorted(p):
                    if n.inv.get(k, 0) < p[k]: g.buy(n, k, p[k] - n.inv.get(k, 0))
                g.build(n, 'S3')
            elif spare is not None:
                if n.inv.get('accelerator', 0) < 50: g.buy(n, 'accelerator', 50 - n.inv.get('accelerator', 0))
                g.assign(n, spare[0], spare[1], 'P109')
        for fi, li in dcs:
            if n.dc_health.get((fi, li), 1) < 0.7:
                need = (1 - n.dc_health[(fi, li)]) * 50
                if n.inv.get('accelerator', 0) < need: g.buy(n, 'accelerator', need - n.inv.get('accelerator', 0))
                g.dc_refit(n, fi, li)
        if 'AI02' in n.known and n.job is None: g.ai_train(n, 100)
        comp = max(n.compute, 1e-9)
        res = min(0.4, self.RES_FILL * 0.05 * max(getattr(n, '_base_rp', 0), 1) / comp) if ('AI04' in n.ai_ms and self.AI_DEPLOY) else 0.0
        g.ai_set_alloc(n, train=(1 - res) * self.TRAIN_SHARE if n.job else 0.0, research=res)

    def portfolio(self, g, n):
        """闲置现金的固定比例配置：外国国债（票面高于本国利率）与低估外国股票（公允值/市价 > 1.4，回到公允值卖出）。"""
        budget = self.SEC_FRAC * max(0.0, n.money - self.SEC_FLOOR)
        held = sum(q * g.companies[k[1]].price * g.fx(g.companies[k[1]].nid, n.nid) for k, q in g.holdings.items() if k[0] == n.nid and q > 0)
        for c in g.companies:
            if c.nid == n.nid or not c.listed: continue
            q = g.holdings.get((n.nid, c.cid), 0.0); v = g.fair(c); fx = g.fx(c.nid, n.nid)
            if q > 0 and c.price >= v:
                g.order(n, c.cid, -q, c.price * 0.99)
            elif v > 1.4 * c.price and held < budget:
                k = min(budget - held, 0.2 * budget) / max(c.price * fx, 1e-9)
                if k * c.price * fx > 1000: g.order(n, c.cid, k, c.price * 1.01); held += k * c.price * fx
        for b in g.bonds:
            if b.issuer == n.nid or b.holders.get(n.nid, 0.0) > 0: continue
            if b.coupon > n.rate + 1 and g.nations[b.issuer].credit > 0.8 and budget > 0:
                face = min(b.holders.get('npc', 0.0), 0.2 * budget / max(g.bond_price(b) * g.fx(b.issuer, n.nid), 1e-9))
                if face > 100: g.buy_bond(n, b.bid, face)

    def trade(self, g, n):
        need = n.pop * FOOD_PER_POP
        if n.inv.get('food', 0) < need * 3:
            g.buy(n, 'food', need * 3 - n.inv.get('food', 0))
        # 维护件
        for f_pid, good in (('S2', 'basic_parts'), ('S3', 'precision_parts')):
            k = sum(1 for f in n.fac if f.pid == f_pid)
            if k and n.inv.get(good, 0) < PROFILES[f_pid]['maintenance'][good] * k * 2:
                g.buy(n, good, PROFILES[f_pid]['maintenance'][good] * k * 4 - n.inv.get(good, 0))
        labs = sum(1 for f in n.fac for ln in f.lines if ln and ln[0] == COMPUTE_LAB)
        if labs and n.inv.get('system_pack', 0) < labs * 0.1 * 5:
            g.buy(n, 'system_pack', labs * 0.1 * 10 - n.inv.get('system_pack', 0))
        for gd in sorted(self.imports):
            if n.inv.get(gd, 0) < self.imports[gd] * 10: g.buy(n, gd, self.imports[gd] * 1.2)
        # 验证品：理论完成后（或接近完成）补齐
        want = {}
        for tid, vleft in n.background:
            if vleft is None:
                t = TIERS[NODES[tid]['tier']]; want[t['validation_good']] = want.get(t['validation_good'], 0) + t['validation_amount']
        if n.project:
            t = TIERS[NODES[n.project[0]]['tier']]
            if n.project[1] >= 0.9 * t['theory_points']:
                want[t['validation_good']] = want.get(t['validation_good'], 0) + t['validation_amount']
        for vg in sorted(want):
            extra = labs * 0.1 * 5 if vg == 'system_pack' else 0
            short = want[vg] + extra - n.inv.get(vg, 0)
            if short > 0 and (not self.makes(n, vg) or n.money > 60000):
                g.buy(n, vg, short)
        for gd in ('consumer_goods', 'ammunition'):
            if n.inv.get(gd, 0) > 0: g.sell(n, gd, n.inv[gd])
