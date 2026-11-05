"""超级参考策略 ref5.Super（重写版，不再继承 ref3/ref4）。

结构：
1. 研究：固定主干路线（专用线 → 工程实验室 → α 提升 → 自动化 → 计算实验室 → 验证包链 → 6 阶），
   主干受阻（前置在验证中）时用低阶填充项目，局末只挑能在剩余周期内完成的项目。
2. 生产：每 `every` 周期一次的线性需求展开。配方按“劳动当量成本”递归选择
   （劳动 × LW + 电力 + 原料；无矿原料按进口价折算劳动当量；储量预计撑不到局末的矿加稀缺溢价）。
   需求 = 民生（食品）+ 维护 + 投资（设施建材、自动化装机件、实验室设备、电站设备、G2 升级）
   + 验证包（按研究队列前瞻）+ 实验室投入；中间品带目标库存修正。
3. 劳动：先排民生与投资（投资占比有上限），剩余劳动按 RP 公式选实验室组合（不同实验室混合会拉低均值，
   低阶实验室只在能提高总 RP 时保留）。整数产线精确核算劳动，超了就减实验室，再减投资。
4. 电力：现有发电线保留；扩容优先风光/核电，煤电逐步替换（煤是冶金化工原料）。
5. 资金：每周期进口原料缺口；保留应急储备；可支配资金按“劳动当量/价格”购买投资品与验证包。
产线模式只用于 industry.json 的 mode_eligible_facilities（农场/电站/实验室不能用自动化线）；
S2 及以上设施在掌握 L01 后才建（规则 05 第 17 行；sim 未强制）。
"""
import math
from collections import defaultdict
from sim import (NODES, TIERS, RECIPES, PROFILES, MODES, VALUE, RAW, EXTRACT, FOOD_PER_POP, CYCLES,
                 IMPORT_CAP, PRICE, SLOTS, IND)
from bots import ancestors, PRODUCERS
from sim import PROTECTED

ELIGIBLE = set(IND['mode_eligible_facilities'])
LAB_RP = {'P075': 1, 'P076': 3, 'P077': 12, 'P079': 18, 'P102': 45}
LAB_ORDER = ['P102', 'P079', 'P077', 'P076', 'P075']
GENS = [r for r in RECIPES if 'electricity' in RECIPES[r]['outputs']]
SKIP = {'P066', 'P067', 'P068', 'P069'} | set(LAB_RP) | set(GENS)
BUY_MULT = PRICE * 1.6
PACKS = {TIERS[t]['validation_good']: t for t in TIERS}

ROUTE = ['R01', 'C01', 'I01', 'I02', 'L01', 'I03', 'R04', 'R03', 'E02', 'E03', 'C02', 'R02', 'C03', 'E01', 'A01', 'A02',
         'L02', 'A03', 'E04', 'R05', 'C05', 'C06', 'I05', 'I06', 'C04', 'R07', 'A05', 'C07', 'R09', 'C09',
         'C10', 'E05', 'E08', 'I09', 'C11']


def lab_rp(n, labs):
    """labs: list of rp per line → effective RP (sim._produce 同式)。"""
    if not labs: return 0.0
    return sum(labs) / len(labs) * len(labs) ** n.alpha()


class Super:
    name = 'super5'
    # 可调参数
    every = 5
    LW = 1.0            # 每工人·周期的成本（成本单位）
    LAMBDA = 40.0       # 货币 → 成本单位的折算（多少钱 = 1 工人·周期）
    PW = 0.0            # 电力额外成本（电力成本另按发电配方递归）
    buf_a, buf_b, buf_t = 15.0, 8.0, 15.0
    capex_share = 0.60
    capex_floor = 0.35
    capex_floor_late = 0.15
    capex_stop = 300
    mine_rise = 0.1
    mine_persist = 6
    keep_frac = 0.25
    ema_a = 0.1
    spec_min = 0.6
    auto_min = 1.2
    batch_thr = 0.6
    mf_floor = 0.6
    hyst_keep = 0.9
    auto_cool = 150
    surv_lo = 12.0
    surv_hi = 40.0
    hyst_new = 1.1
    hyst_new3 = 1.3
    batch_lo = 12.0
    batch_len = 30.0
    pack_h = 40.0
    pack_h_urg = 20.0
    pack_h_hi = 250.0
    pack_share = 0.3
    pack_share_max = 0.6
    pack_buf_k = 20.0   # 试验包目标库存 = 待验证量 + 4 + pack_buf_k × 流量
    press_thr = 4
    keep_abs = 0.5
    capex_T = 20.0
    reserve = 12000.0
    spend_T = 200.0
    spend_inc = 0.3
    labor_margin = 0.985
    route = ROUTE

    def __init__(self, **kw):
        for k, v in kw.items(): setattr(self, k, v)
        self.flow = {}
        self.labs_target = 0
        self.lab_plan = {}
        self.deficit = {}
        self.capex = {}
        self.log = []
        self.want_build = {}
        self.rp_est = 1.0
        self.import_set = set()
        self.cur = {}
        self.pack_cum = {}
        self.stock_tgt = {}
        self.raw_smooth = {}
        self.auto_rm = {}
        self.survival = False
        self.pressure = 0
        self.short_cnt = {}
        self.need_ema = {}
        self.batch_on = {}
        self.dbg = defaultdict(int)

    # ================= 基础工具 =================
    # ---- v1.3 钩子（基类保持 v1.2 行为，Super13 覆盖） ----
    def gen_e(self, rid):
        """规划用的电源可信出力（基类 = 额定）。"""
        return RECIPES[rid]['outputs']['electricity']
    def extra_power(self, n): return 0.0
    def gen_room(self, n, rid): return 10 ** 6
    def extra_workers(self, n): return 0.0
    def reserve_fn(self, n): return 30000.0 + 60.0 * n.pop
    power_margin = 1.06

    def has_deposit(self, n, good):
        d = n.deposits.get(good)
        return d is not None and (d == math.inf or d > 0.03 * n.dep0[good])

    def eta(self, n, good):
        d = n.deposits.get(good)
        if d is None: return 0.0
        if d == math.inf: return 1.0
        rho = d / n.dep0[good]
        return 1.0 if rho >= 0.3 else (0.25 + 0.75 * rho / 0.3 if rho > 0 else 0.0)

    def line_mode(self, n, rid, need=None):
        """新装一条线时实际能用的模式（看库存装机件）。需求不足半条线时用通用线（拆装不浪费装机件）。"""
        if RECIPES[rid]['facility'] not in ELIGIBLE: return 'general'
        if need is not None and need < self.spec_min: return 'general'
        for m in ('automated', 'specialized'):
            if m == 'automated' and need is not None and need < self.auto_min: continue
            if n.can_mode(m) and all(n.inv.get(k, 0) >= q for k, q in MODES[m]['installation'].items()):
                return m
        return 'general'

    def plan_mode(self, n, rid):
        """长期规划模式（假设装机件会被生产出来）。"""
        if RECIPES[rid]['facility'] not in ELIGIBLE: return 'general'
        if n.can_mode('automated'): return 'automated'
        if n.can_mode('specialized'): return 'specialized'
        return 'general'

    def max_grade(self, n):
        g = max([f.grade for f in n.fac] + [1])
        if 'L01' in n.known:
            g = max(g, 2)
            if n.can_mode('automated') or n.inv.get('automation_module', 0) >= 4: g = max(g, 3)
        return g

    def lines_of(self, n):
        out = []
        for fi, f in enumerate(n.fac):
            for li, ln in enumerate(f.lines):
                if ln: out.append((fi, li, ln[0], ln[1]))
        return out

    # ================= 成本模型 =================
    def raw_cost(self, n, good):
        imp = VALUE.get(good, 1) * BUY_MULT / self.lam
        if good in ('water', 'biomass') or self.has_deposit(n, good):
            if good not in EXTRACT: return imp
            lvl = 'G2' if n.g2_ok(good) else 'G1'
            out, pw, wk = EXTRACT[good][lvl]
            e = max(self.eta(n, good), 0.05)
            c = (wk * self.LW + pw * 0.05) / (out * e)
            # 储量预计撑不到局末：加稀缺溢价，逐步转向替代配方/进口
            d = n.deposits.get(good)
            if d != math.inf and d is not None:
                c += imp * self.scarcity(n, good)
            return min(c, imp * 1.2) if d != math.inf else c
        return imp

    def scarcity(self, n, good):
        """0..1：按当前用量，储量能否撑到局末（含尾矿区）。"""
        d = n.deposits.get(good)
        if d is None or d == math.inf: return 0.0
        use = max(self.flow.get(good, 0), self.raw_smooth.get(good, 0), 0.5)
        left = CYCLES - self.cycle
        avail = max(0.0, d - 0.15 * n.dep0[good]) + n.inv.get(good, 0)
        R = use * left / max(1.0, avail)
        return min(1.0, max(0.0, (R - 0.5) / 0.7))

    def unit_cost(self, n, good, stack=()):
        if good in self._uc: return self._uc[good]
        if good == 'electricity':
            return self._elec_cost
        if good in RAW:
            c = self.raw_cost(n, good); self._uc[good] = c; self._choice[good] = None; return c
        imp = VALUE.get(good, 1) * BUY_MULT / self.lam
        best = (imp * 1.5 + 5.0, None)
        if good in stack: return best[0]
        for r in PRODUCERS.get(good, []):
            rid = r['id']
            if rid in SKIP or not n.can_use(rid) or r['grade'] > self._grade: continue
            if self.survival and good in ('grain', 'food') and rid not in ('P028', 'P031'): continue
            md = MODES[self.plan_mode(n, rid)]
            out = r['outputs'][good]
            c = sum(self.unit_cost(n, k, stack + (good,)) * q for k, q in r['inputs'].items())
            c += (r['workers'] * md['worker_factor'] * self.LW + r['power'] * md['power_factor'] * self._elec_cost) / md['capacity']
            c /= out
            # 换配方的滞回：已有产线的配方打 9 折；没有产线的高等级配方加价（要新建设施）
            if self.cur.get(rid): c *= self.hyst_keep
            elif r['grade'] >= 2: c *= self.hyst_new if r['grade'] == 2 else self.hyst_new3
            if c < best[0]: best = (c, rid)
        if not stack or best[1] is not None:
            self._uc[good] = best[0]; self._choice[good] = best[1]
        return best[0]

    def setup_costs(self, n):
        self._uc = {}; self._choice = {}
        use = n.pop * FOOD_PER_POP
        stock = n.inv.get('food', 0) + n.inv.get('grain', 0)
        if stock < self.surv_lo * use or (n.money < 3000 and self.survival_import(n)):
            self.survival = True
        elif stock > self.surv_hi * use and n.money > 20000:
            self.survival = False
        res = self.reserve_fn(n)
        mf = min(1.5, max(self.mf_floor, n.money / (2.0 * res)))
        self.lam = self.LAMBDA * mf
        self._grade = self.max_grade(n)
        # 电力成本：按扩容用的发电配方估（劳动为主）
        self._elec_cost = 0.06
        gen = self.gen_choice(n)
        r = RECIPES[gen]
        c = r['workers'] * 1.4 * self.LW + sum(self.raw_cost(n, k) if k in RAW else VALUE.get(k, 1) * 0.5 for k, q in r['inputs'].items() for _ in [0]) * 0
        c = r['workers'] * 1.4 * self.LW
        for k, q in r['inputs'].items():
            c += q * (self.raw_cost(n, k) if k in RAW else VALUE.get(k, 1) * BUY_MULT / self.lam * 0.5)
        self._elec_cost = c / self.gen_e(gen)

    def choose(self, n, good):
        if good not in self._choice: self.unit_cost(n, good)
        return self._choice.get(good)

    # ================= 发电 =================
    def gen_choice(self, n):
        """扩容用的发电配方。"""
        grade = self._grade
        if self.allow_nuclear and n.can_use('P073') and grade >= 3 and (
                n.inv.get('reactor_module', 0) >= 1 or n.can_use('P026')) and self.nuclear_ok(n):
            return 'P073'
        if n.can_use('P072') and grade >= 2: return 'P072'
        if n.can_use('P071') and self.has_deposit(n, 'crude_oil') and not n.can_use('P072'): return 'P071'
        return 'P070'

    def nuclear_ok(self, n):
        return self.has_deposit(n, 'uranium_ore') or n.inv.get('nuclear_fuel', 0) > 2 or self.allow_nuclear_import

    allow_nuclear_import = True
    allow_nuclear = False

    # ================= 研究 =================
    def blocked(self, n):
        return {b[0] for b in n.background}

    def pick(self, n, known=None, busy=None, rp=None, left=None):
        known = n.known if known is None else known
        busy = (self.blocked(n) | ({n.project[0]} if n.project else set())) if busy is None else busy
        av = [t['id'] for t in NODES.values() if t['id'] not in known and t['id'] not in busy
              and set(t['requires_all']) <= known and (not t['requires_any'] or set(t['requires_any']) & known)]
        if not av: return None
        if left is not None and rp:
            # 局末：只挑能在剩余时间完成理论 + 验证的
            ok = [t for t in av if TIERS[NODES[t]['tier']]['theory_points'] / rp + TIERS[NODES[t]['tier']]['validation_cycles'] + 15 < left]
            if not ok: return None
            av = ok
        avs = set(av)
        for tgt in self.route:
            if tgt in known or tgt in busy: continue
            need = (ancestors(tgt) | {tgt}) - known
            cand = sorted((t for t in avs if t in need), key=lambda t: (NODES[t]['tier'], t))
            if cand: return cand[0]
        # 填充：低阶优先（验证包便宜），同阶里优先能解锁配方/后继多的
        # 主干的 6 阶终端尚未验证时，填充项目避开 5 阶（会和突破验证包抢前沿试验包）
        goal6 = any(NODES[t]['tier'] == 6 and t not in n.known for t in self.route)
        def key(t):
            tier = NODES[t]['tier']
            pack = TIERS[tier]['validation_good']
            ready = 0 if self.pack_ready(n, pack, known) else 1
            clash = 1 if (goal6 and tier >= 5) else 0
            return (clash, ready, tier, t)
        return min(av, key=key)

    def pack_ready(self, n, pack, known):
        prod = [r for r in PRODUCERS.get(pack, []) if set(r['requires_all']) <= known]
        return bool(prod)

    def queue(self, n, k=6):
        """预测接下来的研究队列（含当前项目）：[(tid, 理论剩余)]。"""
        out = []
        known = set(n.known) | self.blocked(n)
        busy = set()
        if n.project:
            tid, done, _ = n.project
            out.append((tid, TIERS[NODES[tid]['tier']]['theory_points'] - done)); known.add(tid)
        for _ in range(k):
            t = self.pick(n, known=known, busy=busy)
            if not t: break
            out.append((t, TIERS[NODES[t]['tier']]['theory_points'])); known.add(t)
        return out

    def research(self, g, n):
        if n.project is not None: return
        left = CYCLES - g.cycle
        rp = max(self.rp_est, 0.5)
        t = self.pick(n, rp=rp, left=left if left < 900 else None)
        if t: g.research(n, t)

    # ================= 需求 =================
    def pack_demand(self, n):
        """验证包按研究队列前瞻：返回 {pack: 每周期流量}，以及立即缺口。"""
        need_t = defaultdict(list)   # pack -> [(eta, amount)]
        for tid, vleft in n.background:
            if vleft is None:
                tier = TIERS[NODES[tid]['tier']]
                need_t[tier['validation_good']].append((0.0, tier['validation_amount']))
        rp = max(self.rp_est, 0.5); t_acc = 0.0
        for tid, rem in self.queue(n):
            t_acc += rem / rp
            if t_acc > 1500: break
            tier = TIERS[NODES[tid]['tier']]
            need_t[tier['validation_good']].append((t_acc, tier['validation_amount']))
        flows = {}; urgent = {}; self.pack_cum = {}
        for pk, lst in need_t.items():
            lst.sort(); cum = 0.0; f = 0.0
            have = n.inv.get(pk, 0)
            for eta, amt in lst:
                cum += amt
                short = cum - have
                if short > 0:
                    hmin = (self.pack_h_urg if eta < 1 else self.pack_h) if PACKS[pk] <= 4 else self.pack_h_hi
                    h = max(eta * 0.8, hmin)
                    h = min(h, max(10.0, CYCLES - self.cycle - TIERS[PACKS[pk]]['validation_cycles'] - 30))
                    f = max(f, short / h)
                    if eta < 1: urgent[pk] = urgent.get(pk, 0) + 0
            flows[pk] = f
            self.pack_cum[pk] = sum(a for e, a in lst if e < 150)
            if lst[0][0] < 1 and have < lst[0][1]: urgent[pk] = lst[0][1] - have
        return flows, urgent

    def survival_import(self, n):
        """当前粮食配方是否依赖进口原料。"""
        rid = self._choice.get('grain') if hasattr(self, '_choice') else None
        if rid not in ('P029', 'P030'): return False
        return not (self.has_deposit(n, 'natural_gas') and (rid == 'P030' or self.has_deposit(n, 'crude_oil')))

    def crit_packs(self, n):
        crit = set()
        for tid in self.route:
            if tid not in n.known: crit.add(TIERS[NODES[tid]['tier']]['validation_good'])
        return crit

    def maint_flow(self, n):
        f = defaultdict(float)
        for fa in n.fac:
            for k, q in PROFILES[fa.pid]['maintenance'].items(): f[k] += q / IND['maintenance_period_cycles']
        return f

    # ================= 展开 =================
    def expand(self, n, final, corr=None):
        """线性展开：返回 (产能需求 cap[rid]（以基础产能计）, raw[good], power, D[good])。"""
        D = defaultdict(float); cap = defaultdict(float); raw = defaultdict(float)
        pending = defaultdict(float)
        for g_, q in final.items(): pending[g_] += q
        if corr:
            for g_, q in corr.items(): pending[g_] += q
        power = 0.0
        for _ in range(25):
            items = [(g_, q) for g_, q in pending.items() if abs(q) > 1e-7]
            if not items: break
            pending = defaultdict(float)
            for g_, q in items:
                D[g_] += q
                if g_ == 'electricity':
                    power += q; continue
                if g_ in RAW or g_ in self.import_set:
                    raw[g_] += q; continue
                rid = self.choose(n, g_)
                if rid is None:
                    raw[g_] += q; continue
                r = RECIPES[rid]
                k = q / r['outputs'][g_]          # 基础产能（capacity=1 的线·周期）
                cap[rid] += k
                for a, b in r['inputs'].items(): pending[a] += b * k
        return cap, raw, power, D

    def gen_mix(self, n, pw):
        """给定电力需求，按现有电源（非煤优先）+ 扩容类型分配，返回 {rid: 线数(分数)}。"""
        have = defaultdict(int)
        for f in n.fac:
            for ln in f.lines:
                if ln and ln[0] in GENS: have[ln[0]] += 1
        mix = {}; left = pw * 1.06
        for rid in sorted(have, key=lambda r: (r == 'P070', r)):
            e = self.gen_e(rid)
            k = min(have[rid], left / e)
            if k > 0: mix[rid] = k; left -= k * e
        if left > 0:
            gt = self.gen_choice(n)
            mix[gt] = mix.get(gt, 0) + left / self.gen_e(gt)
        return mix

    def full_expand(self, n, final, corr=None):
        f2 = dict(final); fuel = {}
        for _ in range(3):
            cap, raw, pw, D = self.expand(n, f2, corr)
            pw_tot = pw + self.lines_power(n, cap) + self.mines_power(n, raw)
            mix = self.gen_mix(n, pw_tot)
            nf = defaultdict(float)
            for rid, k in mix.items():
                for a, q in RECIPES[rid]['inputs'].items(): nf[a] += q * k
            if all(abs(nf[a] - fuel.get(a, 0)) < 0.05 for a in nf): break
            fuel = dict(nf); f2 = dict(final)
            for a, q in fuel.items(): f2[a] = f2.get(a, 0) + q
        self.mix = mix
        return cap, raw, pw_tot, D

    def lines_power(self, n, cap):
        p = 0.0
        for rid, k in cap.items():
            r = RECIPES[rid]; md = MODES[self.plan_mode(n, rid)]
            p += k / md['capacity'] * r['power'] * md['power_factor']
        return p

    def mines_power(self, n, raw):
        p = 0.0
        for gd, q in raw.items():
            if gd in EXTRACT and (gd in ('water', 'biomass') or self.has_deposit(n, gd)):
                lvl = 'G2' if n.g2_ok(gd) else 'G1'
                out, p_, wk = EXTRACT[gd][lvl]
                p += q / (out * max(self.eta(n, gd), 0.05)) * p_
        return p

    # ================= 主循环 =================
    def act(self, g, n):
        self.cycle = g.cycle
        self.research(g, n)
        if g.cycle % self.every == 0:
            try:
                self.plan(g, n)
            except Exception:
                import traceback; traceback.print_exc(); raise
        self.trade(g, n)

    # ---------- 规划 ----------
    def plan(self, g, n):
        self.snapshot(n)
        self.setup_costs(n)
        rp_now = sum(n.rp_log[-10:]) / max(1, len(n.rp_log[-10:]))
        self.rp_est = max(rp_now, 0.7 * self.rp_est, 0.5)
        eff = 1.0 if n.food_ratio >= 0.8 else 0.4 + 0.75 * n.food_ratio
        L = n.labor() * eff * self.labor_margin
        self.L = L
        self.snapshot(n)
        self.import_set = self.small_imports(n)
        base = self.base_demand(n)
        capex = self.capex_demand(n)
        corr = {}
        self.stock_tgt = {}
        for gd, fl in self.flow.items():
            if gd in RAW or gd in ('electricity', 'food'): continue
            fl = max(fl, 0)
            if gd in PACKS and gd != 'lab_supplies':
                tgt = self.pack_cum.get(gd, 0) + 4 + self.pack_buf_k * fl
                dd = min(0.0, (tgt - n.inv.get(gd, 0)) / self.buf_t)
            else:
                tgt = self.buf_a + self.buf_b * fl
                dd = (tgt - n.inv.get(gd, 0)) / self.buf_t
            self.stock_tgt[gd] = tgt
            corr[gd] = max(dd, -fl)
        # 正向库存修正（补缓冲）属于投资，跟投资一起缩放；负向修正（去库存）留在基础需求
        for gd, v in list(corr.items()):
            if v > 0:
                capex[gd] = capex.get(gd, 0) + v; corr[gd] = 0.0
        cands = self.lab_cands(n)
        lab_exp = {}
        for t in sorted(set(cands)):
            lab_exp[t] = self.expand(n, dict(RECIPES[t]['inputs']))
        def best_prefix(base_exp, limit):
            lo, hi, feas = 0, len(cands), 0
            while lo <= hi:
                mid = (lo + hi) // 2
                if self.dry_run(n, base_exp, lab_exp, cands[:mid])['W'] <= limit: feas = mid; lo = mid + 1
                else: hi = mid - 1
            best_k, best_rp, rps = 0, 0.0, []
            for i in range(feas):
                rps.append(LAB_RP[cands[i]])
                v = lab_rp(n, rps)
                if v > best_rp + 1e-9: best_k, best_rp = i + 1, v
            return best_k
        exp0 = self.full_expand(n, base, corr)
        w0 = self.dry_run(n, exp0, lab_exp, [])['W']
        final1 = dict(base)
        for k_, q in capex.items(): final1[k_] = final1.get(k_, 0) + q
        exp1 = self.full_expand(n, final1, corr)
        w1 = self.dry_run(n, exp1, lab_exp, [])['W']
        left = CYCLES - g.cycle
        cf = 0.0 if left < self.capex_stop else (self.capex_floor_late if n.can_use('P079') else self.capex_floor)
        cf_eff = min(cf, max(0.0, w1 - w0) / max(L, 1))
        k = best_prefix(exp0, L * (1 - cf_eff))
        if k == 0 and cands: k = 1
        # 已装实验室（设备已消耗）每次规划最多拆 1 座
        top_t = cands[0] if cands else None
        inst_val = sum(VALUE.get(a, 1) * q for a, q in RECIPES[top_t]['installed'].items()) if top_t else 0
        n_have = len(self.cur.get(top_t, [])) if top_t and inst_val > 60 else 0
        # 验证压力：等验证包的项目多、或无项目可研时，实验室让位给验证包
        waiting = sum(1 for _, v in n.background if v is None)
        self.pressure = waiting + (6 if n.project is None else 0)
        if self.pressure >= self.press_thr:
            k_min = min(len(cands), max(1, n_have - 1))
        else:
            k_min = min(len(cands), n_have)
        k = max(k, k_min)
        scale, base_exp, final = 0.0, exp0, dict(base)
        for sc in (1.0, 0.7, 0.45, 0.25, 0.1):
            fin = dict(base)
            for k_, q in capex.items(): fin[k_] = fin.get(k_, 0) + q * sc
            ex = exp1 if sc == 1.0 else self.full_expand(n, fin, corr)
            if self.dry_run(n, ex, lab_exp, cands[:k])['W'] <= L:
                scale, base_exp, final = sc, ex, fin; break
        else:
            ok = self.dry_run(n, exp0, lab_exp, cands[:k])['W'] <= L
            if not ok and self.pressure >= self.press_thr:
                while k > 1 and self.dry_run(n, exp0, lab_exp, cands[:k])['W'] > L: k -= 1
                ok = self.dry_run(n, exp0, lab_exp, cands[:k])['W'] <= L
            if not ok:
                # 紧缩：先去掉库存正向修正与验证包需求（保实验室），仍不够再减实验室
                corr2 = {k_: min(0.0, v) for k_, v in corr.items()}
                crit = self.crit_packs(n)
                for ps in (0.5, 0.0):
                    fin = dict(base)
                    for pk in PACKS:
                        if pk in fin and pk != 'lab_supplies' and pk not in crit: fin[pk] *= ps
                    ex = self.full_expand(n, fin, corr2)
                    base_exp, final = ex, fin
                    if self.dry_run(n, ex, lab_exp, cands[:k])['W'] <= L: ok = True; break
                self.austerity = getattr(self, 'austerity', 0) + 1
            if not ok and k > 1: k -= 1
        self.capex_scale = scale
        self.dbg_plan = dict(w0=w0, w1=w1, k=k, k_min=k_min, cf=cf_eff, L=L, ncands=len(cands))
        best_t = self.best_lab(n)
        plw = max(self.per_lab_w(n, lab_exp, best_t), 4)
        self.labs_target = max(k, int((L * 1.05 * (1 - cf_eff) - w0) / plw))
        labs = cands[:k]
        # 平滑的原料需求（用于开矿/升级）：上升慢、下降快
        raw_now = defaultdict(float, base_exp[1])
        for t in labs:
            for k_, v in lab_exp[t][1].items(): raw_now[k_] += v
        for gd in sorted(set(raw_now) | set(self.short_cnt)):
            if raw_now.get(gd, 0.0) * 1.05 > self.mine_cap.get(gd, 0.0) + 0.3: self.short_cnt[gd] = self.short_cnt.get(gd, 0) + 1
            else: self.short_cnt[gd] = 0
        for gd in set(raw_now) | set(self.raw_smooth):
            old_v = self.raw_smooth.get(gd, 0.0); new_v = raw_now.get(gd, 0.0)
            self.raw_smooth[gd] = new_v if new_v < old_v else old_v + self.mine_rise * (new_v - old_v)
        capn = defaultdict(float, base_exp[0])
        for t in labs:
            for k_, v in lab_exp[t][0].items(): capn[k_] += v
        for rid in set(capn) | set(self.need_ema):
            self.need_ema[rid] = self.need_ema.get(rid, 0.0) * (1 - self.ema_a) + max(0.0, capn.get(rid, 0.0)) * self.ema_a
        dr = self.dry_run(n, base_exp, lab_exp, labs, full=True)
        # 流量记录（不含库存修正，含实验室投入）
        f2 = dict(final)
        for t in labs:
            for gd, q in RECIPES[t]['inputs'].items(): f2[gd] = f2.get(gd, 0) + q
        self.flow = dict(self.full_expand(n, f2)[3])
        self.labs_now = len(labs)
        self.execute(g, n, dr)

    def per_lab_w(self, n, lab_exp, t):
        if t not in lab_exp: lab_exp[t] = self.expand(n, dict(RECIPES[t]['inputs']))
        cap, raw, pw, _ = lab_exp[t]
        pw_t = self.lines_power(n, cap) + self.mines_power(n, raw) + RECIPES[t]['power'] * 0.8
        return self.frac_workers(n, cap, raw, pw_t) + RECIPES[t]['workers'] * 1.4

    def snapshot(self, n):
        self.cur = defaultdict(list)       # rid -> [(fi, li, mode)]
        self.free = []                     # (grade, lines_in_fac, fi, li)
        for fi, f in enumerate(n.fac):
            ready = f.remaining <= self.every
            for li, ln in enumerate(f.lines):
                if ln: self.cur[ln[0]].append((fi, li, ln[1]))
                elif ready: self.free.append((f.grade, len(f.lines), fi, li))
        self.free.sort()
        self.mine_cap = defaultdict(float)
        self.mine_w = 0.0; self.mine_p = 0.0
        for m in n.mines:
            out, pw, wk = EXTRACT[m.good][m.level]
            self.mine_cap[m.good] += out * self.eta(n, m.good); self.mine_w += wk; self.mine_p += pw

    def small_imports(self, n):
        """少量需求的中间品直接进口（整数产线会浪费大半条线的劳动）。"""
        out = set()
        budget = n.pop * 0.5 + max(0.0, n.money - self.reserve) / 400.0
        items = []
        for gd, fl in self.flow.items():
            if gd in RAW or gd in ('electricity', 'food', 'grain') or fl <= 0: continue
            rid = self._choice.get(gd) if hasattr(self, '_choice') else None
            if rid is None: continue
            r = RECIPES[rid]; md = MODES[self.plan_mode(n, rid)]
            lines = fl / (r['outputs'][gd] * md['capacity'])
            if gd in self.cur_produced(n) and lines > 0.15: continue
            if lines < 0.3:
                items.append((fl * VALUE.get(gd, 1) * BUY_MULT, gd))
        for cost, gd in sorted(items):
            if cost <= budget: out.add(gd); budget -= cost
        return out

    def cur_produced(self, n):
        return {gd for rid in self.cur for gd in RECIPES[rid]['outputs']}

    def dry_run(self, n, base_exp, lab_exp, labs, full=False):
        """整数化：返回劳动 W 以及要执行的动作。"""
        cap = defaultdict(float, base_exp[0]); raw = defaultdict(float, base_exp[1])
        for t in labs:
            c2, r2, _, _ = lab_exp[t]
            for k_, v in c2.items(): cap[k_] += v
            for k_, v in r2.items(): raw[k_] += v
        W = 0.0; P = 0.0
        remove = []; add = []
        self._pmode = {}
        for rid in sorted(set(self.cur) | set(cap)):
            if rid in LAB_RP or rid in GENS or rid in PROTECTED: continue
            have = self.cur.get(rid, [])
            need = cap.get(rid, 0.0)
            r = RECIPES[rid]
            ema = self.need_ema.get(rid, 0.0)
            outs = [o for o in r['outputs'] if self._choice.get(o) == rid] or list(r['outputs'])
            main = max(outs, key=lambda o: self.flow.get(o, 0))
            tg_ = self.stock_tgt.get(main, 1e9)
            inv_m = n.inv.get(main, 0)
            glut = inv_m > max(2 * tg_, tg_ + 30)
            glut_hi = inv_m > max(3 * tg_, tg_ + 60 + 40 * max(self.flow.get(main, 0), 0))
            # 低需求配方：批量生产（库存低于下限开 1 条通用线，高于上限停），避免整条线常年低负荷
            pk_short = main in PACKS and n.inv.get(main, 0) < self.pack_cum.get(main, 0)
            if self.batch_thr > 0 and (0.005 < max(need, ema) or pk_short) and max(need, ema) < self.batch_thr \
                    and all(m == 'general' for _, _, m in have) and rid not in ('P028', 'P029', 'P030', 'P031'):
                out = r['outputs'][main]
                fl = max(max(need, ema) * out, 0.01)
                lo = max(3.0, self.batch_lo * fl)
                if main in PACKS: lo = max(lo, self.pack_cum.get(main, 0) + 1)
                hi = lo + max(self.batch_len * fl, 6 * out)
                st = self.batch_on.get(rid, inv_m < lo)
                if inv_m < lo: st = True
                elif inv_m > hi: st = False
                if full: self.batch_on[rid] = st
                need = 1.0 if st else 0.0
            if need <= 0.03:
                for fi, li, m in have:
                    if m == 'general' or (glut if m != 'automated' else glut_hi) or ema < 0.3 * MODES[m]['capacity']:
                        remove.append((fi, li))
                        if full and m == 'automated': self.dbg[('zero', 'glut' if glut else 'ema')] += 1
                    else:
                        W += r['workers'] * MODES[m]['worker_factor']; P += r['power'] * MODES[m]['power_factor']
                continue
            capsum = sum(MODES[m]['capacity'] for _, _, m in have)
            kept = list(have)
            newm = self.line_mode(n, rid, min(need, ema) - capsum)
            if newm == 'automated' and self.cycle - self.auto_rm.get(rid, -10**6) < self.auto_cool:
                newm = 'specialized' if n.can_mode('specialized') and all(n.inv.get(k_, 0) >= q_ for k_, q_ in MODES['specialized']['installation'].items()) else 'general'
            ncap = MODES[newm]['capacity']
            if capsum < need - 0.12 * ncap:
                k = math.ceil((need - capsum) / ncap - 0.12)
                for _ in range(k):
                    add.append(rid); W += r['workers'] * MODES[newm]['worker_factor']; P += r['power'] * MODES[newm]['power_factor']
                    self._pmode[rid] = newm
            else:
                hs = sorted(have, key=lambda x: (MODES[x[2]]['capacity'], -MODES[x[2]]['worker_factor']))
                for x in hs:
                    c_ = MODES[x[2]]['capacity']
                    gl = glut if x[2] != 'automated' else glut_hi
                    slack = 0.05 if (x[2] == 'general' or gl) else max(need, ema) * self.keep_frac + self.keep_abs
                    base_ = need if (x[2] == 'general' or gl) else max(need, ema)
                    if capsum - c_ >= base_ + slack:
                        remove.append((x[0], x[1])); capsum -= c_; kept.remove(x)
                        if full and x[2] == 'automated': self.dbg[('excess', 'glut' if glut else 'cap')] += 1
            for _, _, m in kept:
                W += r['workers'] * MODES[m]['worker_factor']; P += r['power'] * MODES[m]['power_factor']
        # 实验室：目标组成
        want = defaultdict(int)
        for t in labs: want[t] += 1
        lab_add = []
        for t in LAB_RP:
            have = self.cur.get(t, [])
            k = want.get(t, 0)
            if len(have) > k: remove += [(fi, li) for fi, li, _ in have[k:]]
            for _ in range(min(k, len(have))):
                W += RECIPES[t]['workers'] * 1.4; P += RECIPES[t]['power'] * 0.8
            lab_add += [t] * max(0, k - len(have))
        lab_add.sort(key=lambda t: -LAB_RP[t])
        for t in lab_add:
            W += RECIPES[t]['workers'] * 1.4; P += RECIPES[t]['power'] * 0.8
        # 采掘：新矿按平滑需求（矿不能拆，劳动常驻）
        mine_builds = []; deficit = {}
        W += self.mine_w; P += self.mine_p
        for gd, q in raw.items():
            if q <= 0: continue
            if gd not in EXTRACT:
                deficit[gd] = q; continue
            persist = self.short_cnt.get(gd, 0) >= (1 if gd in ('water', 'biomass') else self.mine_persist)
            need = q * 1.05 if persist else min(q, self.raw_smooth.get(gd, q)) * 1.05
            if need < self.mine_cap.get(gd, 0) < q:
                deficit[gd] = q - self.mine_cap.get(gd, 0)
            sh = need - self.mine_cap.get(gd, 0)
            if sh <= 0: continue
            e = self.eta(n, gd)
            if gd in n.deposits and e > 0.25:
                room = n.mine_slots.get(gd, 0) - sum(1 for m in n.mines if m.good == gd)
                lvl = 'G2' if n.g2_ok(gd) and n.inv.get('precision_parts', 0) >= 5 else 'G1'
                per = EXTRACT[gd][lvl][0] * e
                k = max(0, min(room, math.ceil(sh / per - 0.4)))
                for _ in range(k):
                    mine_builds.append((gd, lvl)); W += EXTRACT[gd][lvl][2]; P += EXTRACT[gd][lvl][1]; sh -= per
            if sh > 0: deficit[gd] = sh
        # 电力
        P += self.extra_power(n); W += self.extra_workers(n)
        gen_have = {r_: list(self.cur.get(r_, [])) for r_ in GENS}
        gtype = self.gen_choice(n)
        want_e = P * self.power_margin + 2
        coal_f = 1.0
        if gen_have.get('P070') and (self.scarcity(n, 'coal') > 0.6 or n.inv.get('coal', 0) < 10 * 2 * len(gen_have['P070'])):
            coal_f = 0.5
        gen_cap = sum(self.gen_e(r_) * len(v) * (coal_f if r_ == 'P070' else 1.0) for r_, v in gen_have.items())
        gen_add = []; gen_rm = []
        if gen_cap < want_e:
            k = math.ceil((want_e - gen_cap) / self.gen_e(gtype))
            inst = RECIPES[gtype]['installed']
            can = min([int(n.inv.get(a_, 0) // q) for a_, q in inst.items()] or [999])
            k2 = min(k, can, self.gen_room(n, gtype)) if gtype != 'P070' else k
            gen_add += [gtype] * k2
            if k > k2:
                gen_add += ['P070'] * math.ceil((k - k2) * self.gen_e(gtype) / 10)
            self.gen_short = k
        else:
            self.gen_short = 0
            surplus = gen_cap - want_e
            for r_ in ('P070', 'P074', 'P071', 'P072'):
                e = self.gen_e(r_)
                while surplus >= e and gen_have.get(r_):
                    gen_rm.append(gen_have[r_].pop()); surplus -= e
        if gtype != 'P070' and gen_have.get('P070') and 'P070' not in gen_add:
            inst = RECIPES[gtype]['installed']
            can = min([int(n.inv.get(a_, 0) // q) for a_, q in inst.items()] or [999]) - gen_add.count(gtype)
            e = self.gen_e(gtype)
            rep = 0; acc = 0.0
            while can > 0 and gen_have['P070'] and rep < 4:
                gen_add.append(gtype); can -= 1; rep += 1; acc += e
                while acc >= 10 and gen_have['P070']:
                    gen_rm.append(gen_have['P070'].pop()); acc -= 10
        # 槽位：被移除的线释放出来；先装电站，再装民生/生产线，最后实验室
        free = list(self.free)
        for x in remove + gen_rm:
            fi, li = x[0], x[1]; f = n.fac[fi]; free.append((f.grade, len(f.lines), fi, li))
        free.sort()
        short = defaultdict(int)
        def take(grade):
            for i, s_ in enumerate(free):
                if s_[0] >= grade:
                    return free.pop(i)
            return None
        placed_gen = []
        for r_ in gen_add:
            s_ = take(RECIPES[r_]['grade'])
            if s_ is None:
                short[RECIPES[r_]['grade']] += 1
                if r_ != 'P070':
                    s_ = take(1)
                    if s_ is not None: placed_gen.append(('P070', s_)); W += 3 * 1.4
                continue
            placed_gen.append((r_, s_)); W += RECIPES[r_]['workers'] * 1.4
        for r_, v in gen_have.items():
            W += RECIPES[r_]['workers'] * 1.4 * len(v)
        placed_add = []
        pri = {'P028': 0, 'P029': 0, 'P030': 0, 'P031': 0}
        add.sort(key=lambda r_: (pri.get(r_, 1), -RECIPES[r_]['grade']))
        for rid in add:
            s_ = take(RECIPES[rid]['grade'])
            if s_ is None:
                short[RECIPES[rid]['grade']] += 1
                m_ = self._pmode.get(rid, 'general')
                W -= 0.5 * RECIPES[rid]['workers'] * MODES[m_]['worker_factor']
            placed_add.append((rid, s_, self._pmode.get(rid, 'general')))
        placed_lab = []
        for t in lab_add:
            s_ = take(RECIPES[t]['grade'])
            if s_ is None:
                short[RECIPES[t]['grade']] += 1
                W -= RECIPES[t]['workers'] * 1.4; continue
            placed_lab.append((t, s_))
        out = dict(W=W, P=P)
        if full:
            inst = defaultdict(float)
            for it in placed_add + placed_gen + placed_lab:
                rid = it[0]
                for a_, q in RECIPES[rid]['installed'].items(): inst[a_] += q
            self.install_need = dict(inst)
            out.update(remove=remove + [(x[0], x[1]) for x in gen_rm], add=placed_add, labs=placed_lab, gens=placed_gen,
                       mines=mine_builds, deficit=deficit, short=dict(short), raw=dict(raw))
        return out

    def execute(self, g, n, dr):
        self.deficit = dr['deficit']
        for fi, li in dr['remove']:
            ln = n.fac[fi].lines[li]
            if ln and ln[1] == 'automated': self.auto_rm[ln[0]] = g.cycle
            g.assign(n, fi, li, None)
        for item in dr['gens'] + dr['add'] + dr['labs']:
            rid, s = item[0], item[1]
            if s is None: continue
            _, _, fi, li = s
            for a, q in RECIPES[rid]['installed'].items():
                if n.inv.get(a, 0) < q and n.money - self.reserve > VALUE.get(a, 1) * BUY_MULT * q:
                    g.buy(n, a, q - n.inv.get(a, 0))
            m = item[2] if len(item) > 2 else 'general'
            if m != 'general' and not all(n.inv.get(k_, 0) >= q_ for k_, q_ in MODES[m]['installation'].items()):
                m = 'general'
            if not g.assign(n, fi, li, rid, m):
                if m != 'general': g.assign(n, fi, li, rid, 'general')
        for gd, lvl in dr['mines']:
            ok = g.build_mine(n, gd, lvl) or (lvl == 'G2' and g.build_mine(n, gd, 'G1'))
            if not ok:
                for a, q in G_BUILD_G1.items():
                    if n.inv.get(a, 0) < q: g.buy(n, a, q - n.inv.get(a, 0))
                g.build_mine(n, gd, 'G1')
        self.upgrade_mines(g, n, self.raw_smooth)
        # 设施：缺口 + 实验室扩张预建
        short = defaultdict(int, dr['short'])
        top = self.best_lab(n); tg = RECIPES[top]['grade']
        building = defaultdict(int)
        for f in n.fac:
            if f.remaining > 0 and not any(f.lines): building[f.grade] += PROFILES[f.pid]['lines']
        free_hi = sum(1 for f in n.fac if f.grade >= tg for ln in f.lines if ln is None)
        lab_hi = sum(1 for f in n.fac if f.grade >= tg for ln in f.lines if ln and ln[0] in LAB_RP and LAB_RP[ln[0]] >= LAB_RP[top])
        extra = self.labs_target - lab_hi - free_hi
        if extra > 0: short[tg] = max(short[tg], min(extra, 8))
        self.want_build = {}
        for gr, k in short.items():
            k2 = k - sum(v for gg, v in building.items() if gg >= gr)
            if k2 > 0: self.want_build[gr] = k2
        self.build(g, n)
        self.convert_modes(g, n)

    def upgrade_mines(self, g, n, raw):
        for i, m in enumerate(n.mines):
            need = raw.get(m.good, 0) * 1.05
            if need <= self.mine_cap.get(m.good, 0): continue
            if m.level == 'G1' and m.remaining == 0 and n.g2_ok(m.good) and self.eta(n, m.good) > 0.3:
                if all(n.inv.get(a, 0) >= q for a, q in G_BUILD_G2.items()) and n.inv.get('precision_parts', 0) >= 3 + 4:
                    if g.upgrade_mine(n, i): break

    def frac_workers(self, n, cap, raw, pw_tot):
        w = 0.0
        for rid, k in cap.items():
            r = RECIPES[rid]; md = MODES[self.plan_mode(n, rid)]
            w += k / md['capacity'] * r['workers'] * md['worker_factor']
        for gd, q in raw.items():
            if gd in EXTRACT and (gd in ('water', 'biomass') or self.has_deposit(n, gd)):
                lvl = 'G2' if n.g2_ok(gd) else 'G1'
                out, p_, wk = EXTRACT[gd][lvl]
                e = max(self.eta(n, gd), 0.05)
                w += q / (out * e) * wk
        gen = RECIPES[self.gen_choice(n)]
        w += pw_tot * 1.06 / self.gen_e(self.gen_choice(n)) * gen['workers'] * 1.4
        return w

    def base_demand(self, n):
        use = n.pop * FOOD_PER_POP
        grow = 1.0 + 0.002 * (1 - n.pop / 600) * 30
        stock = n.inv.get('food', 0) + 0.6 * n.inv.get('grain', 0)
        f = {'food': max(use * (0.95 if stock < 30 * use else 0.7), use * grow + (20 * use - stock) / 40.0)}
        for k, q in self.maint_flow(n).items(): f[k] = f.get(k, 0) + q * 1.3
        flows, urgent = self.pack_demand(n)
        # 验证包产能预算：按劳动当量成本封顶，避免 6 阶包需求瞬间挤垮经济
        waiting = sum(1 for _, v in n.background if v is None)
        press = waiting + (6 if n.project is None else 0)
        budget = min(self.pack_share_max, self.pack_share + 0.05 * press) * n.labor(); used = 0.0
        crit = self.crit_packs(n)
        for k in sorted(flows, key=lambda p: (p not in crit, p not in urgent, PACKS.get(p, 0))):
            q = flows[k]; c = self.unit_cost(n, k)
            q2 = min(q, max(0.0, budget - used) / max(c, 1e-6))
            used += q2 * c
            f[k] = f.get(k, 0) + q2
        self.pack_scale = min(1.0, budget / used) if used > 0 else 1.0
        self.urgent = urgent
        gt = self.gen_choice(n)
        if gt == 'P072':
            coal_lines = sum(1 for f_ in n.fac for ln in f_.lines if ln and ln[0] == 'P070')
            e = self.gen_e(gt)
            k = getattr(self, 'gen_short', 0) + math.ceil(coal_lines * 10 / e) + 1
            for a, q in RECIPES[gt]['installed'].items():
                short = q * min(k, 12) - n.inv.get(a, 0)
                if short > 0: f[a] = f.get(a, 0) + short / 10.0
        return f

    # ---------- 投资需求 ----------
    def capex_demand(self, n):
        want = defaultdict(float)     # 目标库存
        lines = self.lines_of(n)
        # 1) 产线模式升级
        elig = [(fi, li, rid, m) for fi, li, rid, m in lines if RECIPES[rid]['facility'] in ELIGIBLE]
        if n.can_mode('automated'):
            k = min(sum(1 for x in elig if x[3] != 'automated') + 2, 14)
            for a, q in MODES['automated']['installation'].items(): want[a] += q * k
        elif n.can_mode('specialized'):
            k = min(sum(1 for x in elig if x[3] == 'general') + 2, 16)
            want['precision_parts'] += 2 * k
        # 2) 设施建造（上次分配缺口 + 实验室扩张）
        for grade, k in self.want_build.items():
            pid = self.profile_for(n, grade)
            if pid is None: continue
            per = PROFILES[pid]['lines']
            nb = min(math.ceil(k / per), 8 if grade <= 2 else 5)
            for a, q in PROFILES[pid]['build'].items(): want[a] += q * nb
        # 3) 实验室设备
        lab = self.best_lab(n)
        if lab and RECIPES[lab]['installed']:
            have = sum(1 for x in lines if x[2] == lab)
            k = min(max(0, self.labs_target - have), 6)
            for a, q in RECIPES[lab]['installed'].items(): want[a] += q * k
        # 4) 发电设备
        gen = self.gen_choice(n)
        if gen != 'P070':
            gl = getattr(self, 'gen_short', 0)
            coal = sum(1 for x in lines if x[2] == 'P070')
            k = min(gl + coal * 10 / self.gen_e(gen) + 1, 8)
            for a, q in RECIPES[gen]['installed'].items(): want[a] += q * math.ceil(k)
        # 5) G2 升级 / 新矿
        if any(n.g2_ok(m.good) and m.level == 'G1' for m in n.mines):
            k = min(sum(1 for m in n.mines if m.level == 'G1' and n.g2_ok(m.good)), 3)
            for a, q in G_BUILD_G2.items(): want[a] += q * k
        for a, q in G_BUILD_G1.items(): want[a] += q * 2
        # 6) 农业装机件
        for rid, inst in (('P029', 'vehicle'), ('P030', 'control_module')):
            if self._choice.get('grain') == rid:
                want[inst] += 3
        for a, q in getattr(self, 'install_need', {}).items(): want[a] += q
        self.capex = dict(want)
        out = {}
        for a, q in want.items():
            short = q - n.inv.get(a, 0)
            if short > 0: out[a] = short / self.capex_T
        return out

    def profile_for(self, n, grade):
        cands = []
        for pid, p in PROFILES.items():
            if p['recipe_grade'] < grade: continue
            if p['recipe_grade'] >= 2 and 'L01' not in n.known: continue
            ok = all(n.inv.get(k, 0) >= q or self.choose_ok(n, k) for k, q in p['build'].items())
            if not ok: continue
            cost = sum(self.unit_cost(n, k) * q for k, q in p['build'].items()) / p['lines']
            cands.append((p['recipe_grade'], cost, pid))
        if not cands: return None
        cands.sort()
        return cands[0][2]

    def choose_ok(self, n, good):
        if good in RAW: return True
        return self.choose(n, good) is not None or VALUE.get(good, 1) * BUY_MULT < 4000

    # ---------- 实验室 ----------
    def lab_candidates(self, n):
        return [t for t in LAB_ORDER if n.can_use(t) and RECIPES[t]['grade'] <= self._grade]

    def best_lab(self, n):
        c = self.lab_candidates(n)
        return c[0] if c else 'P075'

    def lab_cands(self, n):
        """按 RP 从高到低排列的实验室候选：已有实验室 + 设备库存够装的新实验室。"""
        out = []
        for t in self.lab_candidates(n):
            have = len(self.cur.get(t, []))
            inst = RECIPES[t]['installed']
            can = min(int(n.inv.get(k, 0) // q) for k, q in inst.items()) if inst else 60
            out += [t] * min(have + can, 60)
        return out[:90]

    def build(self, g, n):
        if len(n.fac) >= n.slot_cap(): return
        for gr in sorted(self.want_build, reverse=True):
            k = self.want_build[gr]
            pid = self.profile_for(n, gr)
            if pid is None: continue
            nb = math.ceil(k / PROFILES[pid]['lines'])
            for _ in range(nb):
                if len(n.fac) >= n.slot_cap(): return
                p = PROFILES[pid]
                if all(n.inv.get(a, 0) >= q for a, q in p['build'].items()):
                    g.build(n, pid)
                else:
                    # 换一个能建的同级或更高型号
                    alt = [q for q in PROFILES if PROFILES[q]['recipe_grade'] >= gr and (PROFILES[q]['recipe_grade'] < 2 or 'L01' in n.known)
                           and all(n.inv.get(a, 0) >= x for a, x in PROFILES[q]['build'].items())]
                    if not alt: break
                    g.build(n, min(alt, key=lambda q: (PROFILES[q]['recipe_grade'], -PROFILES[q]['lines'])))

    def convert_modes(self, g, n):
        done = 0
        cands = []
        for fi, f in enumerate(n.fac):
            if f.remaining > 0: continue
            for li, ln in enumerate(f.lines):
                if not ln or RECIPES[ln[0]]['facility'] not in ELIGIBLE: continue
                cur = ln[1]
                tot_cap = sum(MODES[m_]['capacity'] for _, _, m_ in self.cur.get(ln[0], []))
                if self.need_ema.get(ln[0], 0) < 0.6 * tot_cap: continue
                for m in ('automated', 'specialized'):
                    if m == cur: break
                    if not n.can_mode(m): continue
                    if MODES[m]['worker_factor'] / MODES[m]['capacity'] >= MODES[cur]['worker_factor'] / MODES[cur]['capacity']: continue
                    gain = RECIPES[ln[0]]['workers'] * (MODES[cur]['worker_factor'] - MODES[m]['worker_factor'])
                    cands.append((-gain, fi, li, ln[0], m)); break
        cands.sort()
        for _, fi, li, rid, m in cands:
            if done >= 4: break
            if all(n.inv.get(k, 0) >= q for k, q in MODES[m]['installation'].items()):
                if g.assign(n, fi, li, rid, m): done += 1

    # ---------- 采掘 ----------
    def mines(self, g, n, raw):
        w = 0.0; p = 0.0
        self.deficit = {}
        capg = defaultdict(float)
        for m in n.mines:
            out, pw, wk = EXTRACT[m.good][m.level]
            capg[m.good] += out * self.eta(n, m.good); w += wk; p += pw
        # 升级 G1 → G2（每次规划最多 1 座/矿种，避免同时停产）
        for i, m in enumerate(n.mines):
            if m.level == 'G1' and m.remaining == 0 and n.g2_ok(m.good) and self.eta(n, m.good) > 0.3:
                if all(n.inv.get(a, 0) >= q for a, q in G_BUILD_G2.items()) and n.inv.get('precision_parts', 0) >= 3 + 4:
                    if g.upgrade_mine(n, i):
                        o1 = EXTRACT[m.good]['G1']; o2 = EXTRACT[m.good]['G2']
                        w += o2[2] - o1[2]; p += o2[1] - o1[1]
                        capg[m.good] += (o2[0] - o1[0]) * self.eta(n, m.good)
                        break
        for gd, q in raw.items():
            if gd not in EXTRACT: 
                if q > 0: self.deficit[gd] = q
                continue
            need = q * 1.08
            short = need - capg.get(gd, 0)
            if short <= 0: continue
            e = self.eta(n, gd)
            if gd in n.deposits and e > 0.25:
                room = n.mine_slots.get(gd, 0) - sum(1 for m in n.mines if m.good == gd)
                lvl = 'G2' if n.g2_ok(gd) and n.inv.get('precision_parts', 0) >= 3 + 2 else 'G1'
                per = EXTRACT[gd][lvl][0] * e
                # 只在缺口能填满一座矿的 40% 以上时开新矿（矿不能拆，劳动常驻）
                k = min(room, math.ceil(short / per - 0.4))
                for _ in range(max(0, k)):
                    if g.build_mine(n, gd, lvl) or (lvl == 'G2' and g.build_mine(n, gd, 'G1')):
                        short -= per; w += EXTRACT[gd][lvl][2]; p += EXTRACT[gd][lvl][1]
                    else: break
            if short > 0: self.deficit[gd] = short
        return w, p

    # ---------- 贸易 ----------
    def line_inputs(self, n):
        """现有产线每周期投入（按满负荷），按优先级分组。"""
        need = defaultdict(float); pri = {}
        for f in n.fac:
            if f.remaining > 0: continue
            for ln in f.lines:
                if not ln: continue
                r = RECIPES[ln[0]]; cap_ = MODES[ln[1]]['capacity'] if 'electricity' not in r['outputs'] else 1.0
                p = 0 if (ln[0] in GENS or ln[0] in ('P028', 'P029', 'P030', 'P031')) else (1 if ln[0] in LAB_RP else 2)
                for a, q in r['inputs'].items():
                    need[a] += q * cap_; pri[a] = min(pri.get(a, 9), p)
        return need, pri

    def essential_goods(self, n):
        """民生与电力链上的投入品（含其上游一层）。"""
        if getattr(self, '_ess_cycle', -1) == self.cycle: return self._ess
        ess = set(); frontier = []
        for f in n.fac:
            for ln in f.lines:
                if ln and (ln[0] in GENS or ln[0] in ('P028', 'P029', 'P030', 'P031')):
                    frontier += list(RECIPES[ln[0]]['inputs'])
        for _ in range(3):
            nxt = []
            for gd in frontier:
                if gd in ess: continue
                ess.add(gd)
                rid = self._choice.get(gd) if hasattr(self, '_choice') else None
                if rid: nxt += list(RECIPES[rid]['inputs'])
            frontier = nxt
        self._ess = ess; self._ess_cycle = self.cycle
        return ess

    def emergency(self, g, n):
        """每周期：任何在用产线的投入品库存不足 2 周期用量就进口补上（民生/电力优先，其次实验室）。"""
        use = n.pop * FOOD_PER_POP
        if n.inv.get('food', 0) < 3 * use:
            g.buy(n, 'food', min(4 * use - n.inv.get('food', 0), n.money / (VALUE['food'] * BUY_MULT)))
        need, pri = self.line_inputs(n)
        for a in sorted(need, key=lambda a: (pri[a], VALUE.get(a, 1))):
            q = need[a]
            have = n.inv.get(a, 0)
            if have < 2 * q:
                price = VALUE.get(a, 1) * BUY_MULT
                if pri[a] == 1 and n.money - 0.5 * self.reserve < 3 * q * price: continue
                if pri[a] >= 2 and n.money - self.reserve < 3 * q * price: continue
                g.buy(n, a, 3 * q - have)

    def trade(self, g, n):
        self.emergency(g, n)
        if g.cycle % self.every == 0 and getattr(self, 'gen_short', 0) > 0 and hasattr(self, '_grade'):
            gt = self.gen_choice(n)
            if gt != 'P070':
                for a, q in RECIPES[gt]['installed'].items():
                    need_ = q * self.gen_short - n.inv.get(a, 0)
                    if need_ > 0 and n.money > 0.5 * getattr(self, 'reserve', 30000) + need_ * VALUE.get(a, 1) * BUY_MULT:
                        g.buy(n, a, need_)
        use = n.pop * FOOD_PER_POP
        # 2) 原料缺口（本国无矿/产能不足）
        self.reserve = self.reserve_fn(n)
        budget = n.pop * 1.0 + max(0.0, n.money - 0.5 * self.reserve) / 80.0
        ess = self.essential_goods(n)
        for gd, q in sorted(self.deficit.items(), key=lambda x: (x[0] not in ess, VALUE.get(x[0], 1))):
            if gd == 'food': continue
            price = VALUE.get(gd, 1) * BUY_MULT
            if gd not in ess and (budget <= 0 or (price > 80 and n.money - self.reserve < q * 20 * price)): continue
            if n.inv.get(gd, 0) < q * 15:
                got = g.buy(n, gd, q * 1.1); budget -= got * price
        money_free = n.money - self.reserve
        # 3) 后台等待中的验证包
        for pk, q in getattr(self, 'urgent', {}).items():
            short = q - n.inv.get(pk, 0)
            if short > 0 and money_free > short * VALUE[pk] * BUY_MULT:
                prod = self.choose(n, pk) if hasattr(self, '_choice') else None
                # 能自产且产能在路上：只买一半缺口
                k = short
                got = g.buy(n, pk, k); money_free -= got * VALUE[pk] * BUY_MULT
        # 4) 可支配资金：买投资品（劳动当量/价格 最高者）
        if money_free <= 0 or n.imported >= IMPORT_CAP - 1: return
        rate = money_free / self.spend_T + n.pop * self.spend_inc
        cands = []
        for a, q in self.capex.items():
            short = q - n.inv.get(a, 0)
            if short <= 0: continue
            price = VALUE.get(a, 1) * BUY_MULT
            uc = self.unit_cost(n, a) if hasattr(self, '_uc') else 0
            score = uc * self.LAMBDA / price
            cands.append((-score, a, short, price))
        cands.sort()
        for s, a, short, price in cands:
            if -s < 0.6: break
            k = min(short, rate / price)
            if k < 0.5 and price < rate * 2: continue
            k = max(k, 1) if price <= rate * 3 else k
            if k <= 0: continue
            got = g.buy(n, a, math.floor(k) if k >= 1 else 0)
            rate -= got * price
            if rate <= 0: break
        # 5) 积压出售：只卖长期用不完的（库存超过 300 周期用量且不在投资清单里）
        if g.cycle % 20 == 0:
            for gd, q in list(n.inv.items()):
                if gd in ('food', 'grain', 'water') or gd in PACKS: continue
                if gd in RAW and gd not in n.deposits: continue
                fl = max(self.flow.get(gd, 0), self.raw_smooth.get(gd, 0))
                cap_ = max(400, 300 * fl + self.capex.get(gd, 0) * 2)
                d = n.deposits.get(gd)
                if d is not None and d != math.inf:
                    left = CYCLES - g.cycle
                    life = 2.0 * max(fl, 10.0) * left
                    if q + max(0.0, d - 0.15 * n.dep0[gd]) < life: continue
                    cap_ = max(1000, 100 * fl)
                if q > cap_ * 1.3:
                    if getattr(self, 'surplus', None) is not None and g.cycle > 0:
                        self.surplus[gd] = 0.5 * (q - cap_); g.sell(n, gd, 0.5 * (q - cap_))
                    else:
                        g.sell(n, gd, q - cap_)


from sim import G_BUILD
G_BUILD_G1 = G_BUILD['G1'][0]
G_BUILD_G2 = G_BUILD['G2'][0]


# =====================================================================================
# v1.3 扩展（提案 H）：在 ref5.Super 上加功能，不改动 v1.2 规划核心。
#   电力：按可信出力规划（风光按夜间/低风折算），燃煤/燃气/核电作基荷，负荷带把工业放在实验室前面，
#         电网走廊随区间缺口扩建，计入市政与数据中心负荷。
#   人口：住宅区超前建设（住房领先人口），粮食按出生率前瞻，储备金不随人口无限放大。
#   金融：F01 后全部公司上市 49% 募资、分红 100%；F02 后利率 6%（本币升值、进口便宜），
#         资金紧张时发 50 周期国债并预留还本。
#   AI：AI01–03 插入主干；建数据中心（加速器经规划器自产），持续训练（以最佳模型为基座），
#       推理算力分给研究（按 0.05/研究点吃满）、对齐、推理服务市场与生产替代。
# v1.3.1（规则修补后）：
#   电力：水电只建到本国河流潜力；E10 插入主干，扩容优先快堆 P083，在运热堆逐座换快堆。
#   人口：住房目标封顶 1,700（槽位随住宅区扩张），储备金上限 16 万（现金耗尽是长局崩溃的主因）。
#   进口：Game 代理——非必需品不超进口配额（现金 > 4 倍储备时可到 2 倍配额），口粮/燃料/验证包可超；
#         买入前先吃他国低于 NPC 进口价的卖单；积压物资一半挂国家间卖单（1.1 × 世界估值）。
#   燃料：能自产（有铀矿/煤/气储量）时只做 6 周期应急进口（否则安全库存压住规划器的自产需求）。
#   AI：数据中心随科技换代（G1 → G2 → G3，G3 需 S5），按在用代际补装；数据工程 P117 最多 3 条；
#       推理服务吃满 NPC 需求、报价 0.49 Cap；对齐份额随 Cap − Align 缺口加码。
#   金融：国债只平滑暂时性缺口（票面 < 14%、有投资缺口、现金低于 0.6 储备、按近 100 周期现金趋势外推到期能付清），
#         到期前 40 周期本息计入储备、临近到期或宽裕时提前赎回；外国国债与低估外国股票
#         （公允值/市价 > 1.15）用闲置资金配置；开启跨国电力耦合；挂权重/数据授权，他国授权明显更强时买入；
#         研究推理不足时向他国买推理服务。
# =====================================================================================
from sim import (DC_RECIPES, HOUSE_COST, HOUSE_CAP, GEN_TABLE, GRID_COST, SECTORS, RES_POWER, HOUSE_WORK, DC_SLOTS, DC_GOOD)
DATA_RECIPES = {r for r in RECIPES if 'curated_data' in RECIPES[r]['outputs']}
import sim as _sim

ROUTE13 = list(ROUTE)
for _after, _ins in (('R04', 'E05'), ('R01', 'F01'), ('C02', 'F03'), ('R04', 'F02'), ('R05', 'AI01'), ('AI01', 'AI03'), ('AI03', 'AI02')):
    ROUTE13.insert(ROUTE13.index(_after) + 1, _ins)
ROUTE13.insert(ROUTE13.index('I09') + 1, 'E10')         # 快堆：电力用工约为热堆的 2/3
CREDIT = {'wind': 0.3, 'solar': 0.0, 'hybrid': 0.2, 'hydro': 0.85, 'fuel': 0.9, 'nuclear': 0.8}


class Super13(Super):
    name = 'super13'
    route = ROUTE13
    power_margin = 1.08
    allow_nuclear = True
    pop_cap = 1700.0        # 住房目标上限（槽位随住宅区扩张后，人口规模由口粮/电力/资金决定；1,700 为实测最优档）
    house_lead = 1.10        # 住房容量 / 人口 目标
    dc_target_max = 4
    rate13 = 6.0

    proxy_cls = None           # 见类定义后：Super13 用 _GProxy

    def __init__(self, **kw):
        super().__init__(**kw)
        self.ipo_done = False
        self.dc_want = 0
        self.surplus = {}
        self._cash_hist = []; self._due = 0.0

    # ---------- 电力 ----------
    def gen_e(self, rid):
        e = RECIPES[rid]['outputs']['electricity'] * CREDIT[GEN_TABLE[rid][0]]
        if rid == 'P112':                       # 河流潜力：超建时全部水电同比摊薄
            k = getattr(self, '_hyd_n', 0)
            if k * 15.0 > self._hyd_pot: e *= self._hyd_pot / (k * 15.0)
        return e

    def hydro_count(self, n):
        return sum(1 for f in n.fac for ln in f.lines if ln and ln[0] == 'P112')

    def gen_room(self, n, rid):
        if rid != 'P112': return 10 ** 6
        return max(0, int(n.hydro_pot // 15.0) - self.hydro_count(n))

    def gen_choice(self, n):
        grade = self._grade if hasattr(self, '_grade') else 1
        self._hyd_n = self.hydro_count(n); self._hyd_pot = n.hydro_pot
        if n.can_use('P112') and grade >= 2 and self.gen_room(n, 'P112') > 0: return 'P112'   # 水电：无燃料，受河流潜力约束
        nuc = (n.inv.get('reactor_module', 0) >= 1 or n.can_use('P026')) and self.nuclear_ok(n)
        if self.adv_nuclear and grade >= 3 and nuc:
            if n.can_use('P083'): return 'P083'                 # 快堆：30 电/5 工人，燃料 0.02
        if n.can_use('P073') and grade >= 3 and (n.inv.get('reactor_module', 0) >= 1 or n.can_use('P026')) and self.nuclear_ok(n):
            return 'P073'
        if n.can_use('P111') and self.has_deposit(n, 'natural_gas'): return 'P111'
        d = n.deposits.get('coal', 0)
        if d > 0.2 * n.dep0.get('coal', 1): return 'P070'
        if n.can_use('P113') and grade >= 2: return 'P113'
        return 'P070'

    def extra_power(self, n):
        p = RES_POWER * n.pop * 1.05
        for f in n.fac:
            for ln in f.lines:
                if ln and (ln[0] in DC_RECIPES or ln[0] in DATA_RECIPES): p += RECIPES[ln[0]]['power']
        return p

    def extra_workers(self, n):
        w = HOUSE_WORK * (n.housing + len(n.housing_build))
        for f in n.fac:
            for ln in f.lines:
                if ln and (ln[0] in DC_RECIPES or ln[0] in DATA_RECIPES): w += RECIPES[ln[0]]['workers']
        return w

    reserve_cap = 160000.0
    adv_nuclear = True
    conv_k = 1.5
    def reserve_fn(self, n): return min(30000.0 + 60.0 * n.pop, self.reserve_cap) + getattr(self, '_due', 0.0)

    # ---------- 需求 ----------
    def growth(self, n):
        return max(0.0, n.birth - n.death)

    def base_demand(self, n):
        f = super().base_demand(n)
        use = n.pop * FOOD_PER_POP
        stock = n.inv.get('food', 0) + 0.6 * n.inv.get('grain', 0)
        f['food'] = max(f.get('food', 0), use * (1 + 40 * self.growth(n)) + (20 * use - stock) / 40.0)
        # 住宅区建材：按人口增速前瞻 + 缺口
        gap = max(0.0, self.house_target(n) - n.housing_cap() - HOUSE_CAP * len(n.housing_build)) / HOUSE_CAP
        per = n.pop * self.growth(n) * 1.2 / HOUSE_CAP
        for a, q in HOUSE_COST.items():
            f[a] = f.get(a, 0) + q * (per + gap / 30.0)
        return f

    def capex_demand(self, n):
        out = super().capex_demand(n)
        add = {}
        if n.grid_need > 0.7 * n.grid_cap:
            for a, q in GRID_COST.items(): add[a] = add.get(a, 0) + q * 2
        if self.dc_want > 0 and 'AI01' in n.known:
            rid = self.dc_rid(n); acc = DC_GOOD[rid]
            dcs = self.dc_lines(n)
            have_dc = len(dcs); old = sum(1 for x in dcs if x[2] != rid)
            k = min(2, self.dc_want - have_dc) + min(1, old)          # 新建 + 逐座换代
            if k > 0:
                add[acc] = add.get(acc, 0) + DC_SLOTS * k
                if self.dc_want > have_dc:
                    for a, q in PROFILES[self.dc_profile(n, rid)]['build'].items(): add[a] = add.get(a, 0) + q
            for x in dcs:                                                  # 补装库存（按在用代际）
                gd = DC_GOOD[x[2]]; add[gd] = add.get(gd, 0) + 0.3 * DC_SLOTS
        for a, q in add.items():
            self.capex[a] = self.capex.get(a, 0) + q
            short = self.capex[a] - n.inv.get(a, 0)
            if short > 0: out[a] = max(out.get(a, 0), short / self.capex_T)
        return out

    def house_target(self, n):
        return min(n.pop * self.house_lead + 60.0, self.pop_cap)

    # ---------- 主循环 ----------
    def act(self, g, n):
        if g.cycle == 0:
            g.set_load_priority(n, ['B1', 'B3', 'B2', 'B4'])
            g.set_power_coupling(n, True)          # 跨国电力耦合：对方也开启时，空闲机组卖电、缺电时买电
        gp = self.proxy_cls(g, self)
        super().act(gp, n)
        self.housing(gp, n)
        if g.cycle % self.every == 0:
            self.grid(gp, n)
            self.finance(g, n)
            self.ai(gp, n)
            self.interstate(g, n)

    def trade(self, g, n):
        # 燃料安全库存：发电燃料低于 25 周期用量时先进口（优先于投资品）
        burn = {}
        for rid, q in getattr(n, '_gen_out', {}).items():
            r = RECIPES[rid]
            for a, qq in r['inputs'].items(): burn[a] = burn.get(a, 0) + qq * q / r['outputs']['electricity']
        for a in sorted(burn):
            if a == 'water': continue
            # 能自产（核燃料有铀矿、煤/气有储量）时只做 6 周期的应急进口，否则安全库存压住了规划器的自产需求
            own = self.has_deposit(n, 'uranium_ore') if a == 'nuclear_fuel' else self.has_deposit(n, a)
            lo, hi = (6, 10) if own else (25, 30)
            if n.inv.get(a, 0) < lo * burn[a]:
                g.buy(n, a, hi * burn[a] - n.inv.get(a, 0))
        super().trade(g, n)

    def housing(self, g, n):
        k = math.ceil((self.house_target(n) - n.housing_cap() - HOUSE_CAP * len(n.housing_build)) / HOUSE_CAP)
        if k > 0: g.build_housing(n, min(k, 5))

    def grid(self, g, n):
        if n.grid_need > 0.7 * n.grid_cap and n.grid_build == []:
            g.build_grid(n, 1)
        # 热堆换快堆：同一 S3 产线 P073 → P083（燃料 0.05 → 0.02、水 2 → 0.5、出力 20 → 30），每次最多 2 座
        if self.adv_nuclear and n.can_use('P083') and CYCLES - g.cycle > 200:
            inst = RECIPES['P083']['installed']; k = 0
            for fi, f in enumerate(n.fac):
                for li, ln in enumerate(f.lines):
                    if k >= 2 or not ln or ln[0] != 'P073' or f.remaining > 0: continue
                    if any(n.inv.get(a_, 0) < q for a_, q in inst.items()):
                        if n.money < self.conv_k * self.reserve_fn(n): return
                        for a_, q in inst.items():
                            if n.inv.get(a_, 0) < q: g.buy(n, a_, q - n.inv.get(a_, 0))
                        if any(n.inv.get(a_, 0) < q for a_, q in inst.items()): return
                    g.assign(n, fi, li, None); g.assign(n, fi, li, 'P083'); k += 1

    # ---------- 金融 ----------
    bond_hurdle = 14.0       # 年化 %：国债成本低于此（扩张回报的保守估计）才借
    sec_share = 0.25         # 闲置资金中可投外国证券的比例

    def finance(self, g, n):
        if 'F01' in n.known and not self.ipo_done and g.cycle >= 300:
            for s_ in SECTORS:
                g.ipo(n, s_, 0.49); g.set_payout(n, s_, 1.0)
            self.ipo_done = True
        res = self.reserve_fn(n)
        if 'F02' in n.known:
            if n.rate != self.rate13: g.set_rate(n, self.rate13)
            self.bonds(g, n, res)
        if 'F03' in n.known:
            self.securities(g, n, res)

    def bonds(self, g, n, res):
        """国债：现金低于半个储备、仍有投资缺口、且票面（i + 利差）低于回报门槛时借 50 周期债；
        现金宽裕时提前赎回；闲置资金买收益率高于本国利率 2 个百分点以上的外国国债。"""
        own = [b for b in g.bonds if b.issuer == n.nid]
        # 到期前 40 周期把本息计入储备（trade 的支出闸门随之收紧）
        self._due = sum(b.face * (1 + b.coupon / 10000.0 * 40) for b in own if b.maturity - g.cycle < 40)
        y = n.rate + g.spread(n)
        short_capex = any(q > n.inv.get(a, 0) for a, q in self.capex.items())
        self._cash_hist = (self._cash_hist + [n.money])[-20:]
        slope = (self._cash_hist[-1] - self._cash_hist[0]) / (5.0 * (len(self._cash_hist) - 1)) if len(self._cash_hist) >= 20 else -1.0
        term = 50
        amt = min(g.debt_ceiling(n) - g.debt(n), 0.5 * res)
        # 借新债：现金低于 0.6 储备、仍有投资缺口、票面低于门槛、无违约记录，且按近 100 周期现金趋势外推到期时
        # 能付清本息（借款只用于平滑暂时性缺口，不借来填持续性赤字）
        if n.money < 0.6 * res and short_capex and y < self.bond_hurdle and g.cycle < CYCLES - 120 \
                and n.defaults == 0 and not own and amt > 1000 \
                and n.money + slope * term >= 1.2 * amt * (1 + y / 100.0 * term / 100.0):
            g.issue_bond(n, amt, term)
        elif own:
            b = min(own, key=lambda b: b.maturity)
            cost = b.face * g.bond_price(b)
            # 宽裕时提前赎回；临近到期且现金够本息时也提前赎回（不把违约风险留到到期日）
            if cost < n.money - 1.5 * res or (b.maturity - g.cycle < 20 and cost < n.money - 0.2 * res):
                g.repay_bond(n, b.bid)
        # 外国国债
        for b in g.bonds:
            if b.issuer == n.nid: continue
            iss = g.nations[b.issuer]; h = b.holders.get(n.nid, 0.0)
            if n.money < 0.5 * res and h > 0:
                g.sell_bond(n, b.bid, h); continue
            if n.money > 3 * res and iss.credit > 0.9 and b.coupon > n.rate + 2 and b.maturity - g.cycle > 20:
                face = min(b.holders.get('npc', 0.0), 0.05 * (n.money - 3 * res) / max(g.bond_price(b) * g.fx(b.issuer, n.nid), 1e-9))
                if face > 100: g.buy_bond(n, b.bid, face)

    def securities(self, g, n, res):
        """外国股票：公允值 / 市价 > 1.15 的上市公司按闲置资金分批买入（单家 ≤ 闲置资金 5%）；
        市价回到公允值 1.05 倍以上、或现金低于储备时卖出。"""
        idle = n.money - 1.5 * res
        for c in g.companies:
            if c.nid == n.nid or not c.listed: continue
            q = g.holdings.get((n.nid, c.cid), 0.0)
            v = g.fair(c); px = c.price; fx = g.fx(c.nid, n.nid)
            if q > 0 and (px > 1.05 * v or n.money < res):
                g.order(n, c.cid, -q, px * 0.98); continue
            if idle > 0 and v > 1.15 * px:
                cap_ = self.sec_share * idle * 0.05 / max(px * fx, 1e-9)
                k = cap_ - q
                if k * px * fx > 500: g.order(n, c.cid, k, px * 1.02)

    def interstate(self, g, n):
        """国家间撮合：挂权重/数据授权；他国权重（60% Cap）明显强于本国时买授权；
        自有研究推理不足时向他国买推理服务；积压物资挂到国家间卖单（价低于 NPC 进口价）。"""
        if n.models and 'AI04' in n.ai_ms:
            g.license_offer(n, 'weights', 1500.0 * n.cap)
        if n.d_cur > 500:
            g.license_offer(n, 'data', 0.5 * n.d_cur)
        if 'AI01' in n.known:
            for (sid, kind), (px, tgt) in sorted(g.lic_offers.items()):
                if sid == n.nid or (tgt is not None and tgt != n.nid): continue
                s_ = g.nations[sid]
                if kind == 'weights' and 0.6 * s_.cap > n.cap + 2 and px / n.e < 0.3 * n.money:
                    g.license_buy(n, sid, 'weights', px)
                elif kind == 'data' and 'AI02' in n.known and n.d_eff() < 20 * math.sqrt((max(n.models) if n.models else 0) + 2e5) \
                        and px / n.e < 0.1 * n.money:
                    g.license_buy(n, sid, 'data', px)
        if 'AI04' in n.ai_ms:
            need = 0.05 * max(getattr(n, '_base_rp', 0), 1) - n.compute_inf * n.ai_alloc.get('research', 0.0)
            g.ai_bid_service(n, need if need > 1 else 0, 0.5 * max(n.cap, 10))
        if g.cycle % 20 == 0:
            for gd in sorted(self.surplus):
                g.export_offer(n, gd, self.surplus[gd], VALUE.get(gd, 1) * PRICE * 1.1)

    def reserve_fn_due(self, g, n):
        return sum(b.face for b in g.bonds if b.issuer == n.nid and b.maturity - g.cycle < 40)

    # ---------- AI ----------
    def dc_rid(self, n):
        """可用的最高代数据中心（G3 P119 → G2 P118 → G1 P109）。"""
        if n.can_use('P119') and any(f.pid == 'S5' and f.remaining == 0 for f in n.fac): return 'P119'
        if n.can_use('P118'): return 'P118'
        return 'P109'

    def dc_profile(self, n, rid):
        gr = RECIPES[rid]['grade']
        return 'S3' if gr <= 3 else 'S5'

    def dc_lines(self, n):
        return [(fi, li, ln[0]) for fi, f in enumerate(n.fac) for li, ln in enumerate(f.lines) if ln and ln[0] in DC_RECIPES]

    def ai(self, g, n):
        if 'AI01' not in n.known: return
        left = CYCLES - g.cycle
        healthy = n.money > 0.5 * self.reserve_fn(n) and n.power_ratio > 0.97
        if left < 300 or 'AI02' not in n.known: self.dc_want = 0 if left < 300 else 1
        elif healthy and g.cycle % 100 == 0: self.dc_want = min(self.dc_target_max, self.dc_want + 1)
        rid = self.dc_rid(n); acc = DC_GOOD[rid]
        dcs = self.dc_lines(n)
        if n.inv.get(acc, 0) >= DC_SLOTS:
            old = [x for x in dcs if RECIPES[x[2]]['outputs']['compute'] < RECIPES[rid]['outputs']['compute']]
            if old:                                             # 换代：把最旧的一座换成新一代
                fi, li, _ = min(old, key=lambda x: RECIPES[x[2]]['outputs']['compute'])
                if n.fac[fi].grade >= RECIPES[rid]['grade']:
                    g.assign(n, fi, li, None); g.assign(n, fi, li, rid)
            elif len(dcs) < self.dc_want:
                gr = RECIPES[rid]['grade']; slot = None
                for fi, f in enumerate(n.fac):
                    if f.grade >= gr and f.remaining == 0:
                        for li, ln in enumerate(f.lines):
                            if ln is None: slot = (fi, li); break
                    if slot: break
                if slot: g.assign(n, slot[0], slot[1], rid)
                elif not any(f.grade >= gr and f.remaining > 0 and not any(f.lines) for f in n.fac):
                    g.build(n, self.dc_profile(n, rid))
        for fi, li, r_ in dcs:
            h = n.dc_health.get((fi, li), 1)
            if h < 0.75:
                if n.inv.get(DC_GOOD[r_], 0) >= (1 - h) * DC_SLOTS: g.dc_refit(n, fi, li)
                elif r_ != rid and n.inv.get(acc, 0) >= DC_SLOTS and n.fac[fi].grade >= RECIPES[rid]['grade']:
                    g.assign(n, fi, li, None); g.assign(n, fi, li, rid)
        self.data_lines(g, n)
        comp = max(n.compute, 1e-9)
        if 'AI02' in n.known and n.job is None and left > 40:
            g.ai_train(n, min(100, left - 20))
        # 份额：研究吃满 → 推理服务（不超过需求）→ 对齐 → 训练 → 生产
        # 份额：研究吃满（0.05 算力/研究点）→ 推理服务吃满 NPC 需求（每单位 ≈0.5 Cap RU，远高于生产替代）
        #       → 对齐随 Cap − Align 缺口加码 → 训练 → 生产替代
        res = min(0.3, 0.05 * max(n._base_rp if hasattr(n, '_base_rp') else 0, 1) / comp * 1.1) if 'AI04' in n.ai_ms else 0.0
        mkt = min(self.mkt_max, g.ai_demand * self.mkt_frac / comp) if 'AI04' in n.ai_ms else 0.0
        align = min(0.35, 0.10 + 0.03 * max(0.0, n.cap - n.align - 3)) if 'AI03' in n.known else 0.0
        train = max(0.0, min(0.55, 1 - res - mkt - align)) if n.job is not None else 0.0
        prod = max(0.0, 1 - res - mkt - align - train) if 'AI06' in n.ai_ms else 0.0
        g.ai_set_alloc(n, train=train, research=res, market=mkt, align=align, production=prod)
        if 'AI04' in n.ai_ms: g.ai_offer(n, self.offer_k * n.cap)

    data_max = 3
    mkt_max = 0.5
    mkt_frac = 1.0
    offer_k = 0.49

    def data_lines(self, g, n):
        """数据工程 P117：训练数据约束 φ < 1 时逐条加线（实验室 S2+，装 computing_equipment）。"""
        if 'AI02' not in n.known or CYCLES - g.cycle < 400: return
        have = sum(1 for f in n.fac for ln in f.lines if ln and ln[0] in DATA_RECIPES)
        best = max(n.models) if n.models else 0.0
        need = 20.0 * math.sqrt(best + 2e5)
        if have >= self.data_max or n.d_eff() >= need: return
        inst = RECIPES['P117']['installed']
        for a, q in inst.items():
            if n.inv.get(a, 0) < q: g.buy(n, a, q - n.inv.get(a, 0))
        for fi, f in enumerate(n.fac):
            if f.grade >= 2 and f.remaining == 0:
                for li, ln in enumerate(f.lines):
                    if ln is None:
                        g.assign(n, fi, li, 'P117'); return


_ESSENTIAL = {'food', 'grain', 'coal', 'nuclear_fuel', 'natural_gas', 'fuel', 'water'} | set(PACKS)


class _GProxy:
    """Super13 的 Game 代理：买入时先吃他国卖单中低于 NPC 进口价的部分（国家间撮合），其余照常走 NPC 市场。"""
    __slots__ = ('_g', '_b')
    def __init__(self, g, b): self._g = g; self._b = b
    def __getattr__(self, k): return getattr(self._g, k)
    def buy(self, n, good, qty):
        g = self._g; got = 0.0
        if g.goods_offers and qty > 0:
            npc_ru = g.buy_price(n, good) * n.e
            for sid, gd, q, px in g.goods_board(good):
                if sid == n.nid or px >= npc_ru: continue
                got += g.buy_from(n, sid, gd, qty - got, px)
                if got >= qty - 1e-9: return got
        # 超配额进口按边际加价：只有口粮、发电燃料、验证包可以超配额买；现金充裕（> 4 倍储备）时其余物品可买到边际 ×2
        room = n.import_quota() - n.imported
        if good not in _ESSENTIAL:
            if n.money > 4 * self._b.reserve_fn(n): room += n.import_quota()
            qty = min(qty - got, max(0.0, room)) + got
        return got + g.buy(n, good, qty - got)


Super13.proxy_cls = _GProxy


class _GProxy14(_GProxy):
    """Super14 的 Game 代理：在 _GProxy 之上，对建设/装线类命令先按 sim 的合法性条件自检，
    装机件不足时先试买一次，仍不满足就不下单（不发会被引擎拒绝、计入 errors 的命令）。"""
    __slots__ = ()

    def _need(self, n, need):
        short = {a: q - n.inv.get(a, 0) for a, q in need.items() if n.inv.get(a, 0) < q}
        if short and n.money > 0.3 * self._b.reserve_fn(n):
            for a in sorted(short): _GProxy.buy(self, n, a, short[a])
        return all(n.inv.get(a, 0) >= q for a, q in need.items())

    def assign(self, n, fi, li, rid, mode='general'):
        g = self._g
        if rid is not None:
            r = RECIPES.get(rid)
            f = n.fac[fi] if 0 <= fi < len(n.fac) else None
            if r is None or f is None or rid in _sim.NON_LINE: return False
            if mode != 'general' and _sim.ELIGIBLE and r['facility'] not in _sim.ELIGIBLE: return False
            if not n.can_use(rid) or not n.can_mode(mode) or r['grade'] > f.grade: return False
            need = dict(r['installed'])
            for a, q in MODES[mode]['installation'].items(): need[a] = need.get(a, 0) + q
            if not self._need(n, need): return False
        return g.assign(n, fi, li, rid, mode)

    def build(self, n, pid):
        p = PROFILES[pid]
        if (pid != 'S1' and 'L01' not in n.known) or len(n.fac) >= n.slot_cap(): return False
        if any(n.inv.get(a, 0) < q for a, q in p['build'].items()): return False
        return self._g.build(n, pid)

    def build_mine(self, n, good, level='G1'):
        cost = G_BUILD[level][0]
        if good not in n.deposits or (level == 'G2' and not n.g2_ok(good)): return False
        if sum(1 for m in n.mines if m.good == good) >= n.mine_slots[good]: return False
        if any(n.inv.get(a, 0) < q for a, q in cost.items()): return False
        return self._g.build_mine(n, good, level)

    def upgrade_mine(self, n, idx):
        m = n.mines[idx]
        if m.level == 'G2' or not n.g2_ok(m.good) or any(n.inv.get(a, 0) < q for a, q in G_BUILD['G2'][0].items()): return False
        return self._g.upgrade_mine(n, idx)


# =====================================================================================
# v1.4 / r6 原型规则适配（2026-11-03，Super14 = 评分参考）：在 Super13 上只加三处，不改规划核心。
#   评分改为 07 号五支柱国力 V（sim.national_power），不再只看 TP：
#   1) 不 IPO、不增发：外部/NPC 持有的本国股按 max(均价, 中性公允值, IPO 价, 累计实缴) 计 NFA 负债（AR-008），
#      公司越赚钱负债越大，U_ind 长期为负（v1.3.1 的 Super 因此 V 低于 Medium）。
#   2) 去库存：产出只在库存 ≤ 6 × 20 周期真实消纳均值时全额计入 VA（eta_sink，AR-034/043）。
#      目标库存改为 ≈ 4–5 倍消纳；超过 ~5.5 倍的部分（扣除投资预留、待验证包、燃料安全库存）卖给 NPC，
#      现金进入 NFA 年金，卖出量同时计入消纳与净出口（U_intl）。
#   3) 口粮缓冲 20 → 6 周期用量（emergency 仍在 < 3 周期时进口）。
# =====================================================================================
class Super14(Super13):
    name = 'super14'
    proxy_cls = _GProxy14
    buf_a = 3.0           # 中间品目标库存 = buf_a + buf_b × 流量（v1.3.1 为 15 + 8×：库存/消纳常年 > 6，产出 eta 归零）
    buf_b = 4.0
    pack_buf_k = 8.0      # 试验包目标库存 = 待验证量 + 4 + 8 × 流量（原 20 ×）
    food_days = 6.0       # 口粮缓冲（周期用量）
    mf_floor = 0.25       # 现金紧时进口在成本模型里最多贵 4 倍（原 0.6 → 1.67 倍），少靠进口撑规模
    bond_hurdle = -1.0    # 不再新发国债：违约进 Φ_sustain 且永久扣 d_insolvency；票面 + 利差远高于回报
    buyback_left = 0      # 回购 NPC 持股（实测：买盘把股价与 20 周期均价抬高，剩余外部持股负债随之上升，默认关）
    # 库存修剪（出售超过 trim_k 倍消纳的部分）：实测会卖掉规划器靠库存触发的投资品与原料，TP 掉 35–95%，默认关
    trim_on = False
    trim_raw_only = True
    trim_k = 5.5
    trim_keep = 4.5
    trim_every = 5

    ipo_sectors = ('mining',)

    crisis_ipo = ('materials', 'manufacturing', 'tech')

    def crisis(self, g, n):
        """现金见底兜底：现金低于 0.15 × 储备且口粮或电力已开始不足时，一次性上市其余公司换现金
        （外部股权负债拉低 U_ind，但比口粮/电力崩溃、人口塌缩便宜得多）。"""
        if self.crisis_done or 'F01' not in n.known: return
        if n.money < 0.15 * self.reserve_fn(n) and (n.food_ratio < 0.97 or n.power_ratio < 0.95):
            for s_ in self.crisis_ipo:
                g.ipo(n, s_, 0.49); g.set_payout(n, s_, 1.0)
            self.crisis_done = True

    def housing(self, g, n):
        if n.money < 0.5 * self.reserve_fn(n): return       # 现金紧时不再扩住房（不把人口养到财政撑不住）
        super().housing(g, n)

    def finance(self, g, n):
        # 只上市采掘：IPO 价已接近其后公允值（负债 ≈ 募资）；制造/材料/科技 IPO 价远低于日后公允值，
        # 外部持股负债会涨到募资的数倍（v1.3.1 全部上市时 NFA 负债约为募资的 3.4 倍）
        if 'F01' in n.known and not self.ipo_done and g.cycle >= 300:
            for s_ in self.ipo_sectors:
                g.ipo(n, s_, 0.49); g.set_payout(n, s_, 1.0)
            self.ipo_done = True
        super().finance(g, n)
        if self.buyback_left and CYCLES - g.cycle < self.buyback_left and 'F03' in n.known:
            self.buyback(g, n)

    buyback_left = 0          # 局末多少周期内回购 NPC 持有的本国股（0 = 不回购）
    buyback_frac = 0.5        # 每次最多动用（现金 − 1.2 × 储备）的比例

    def buyback(self, g, n):
        """回购：按不高于 1.05 × max(市价, 公允值) 的限价买回 NPC 持股，消去按增长后估值计的外部股权负债。"""
        free = self.buyback_frac * (n.money - 1.2 * self.reserve_fn(n))
        if free <= 0: return
        cs = [c for c in (g.company(n.nid, s_) for s_ in SECTORS) if c.listed and c.npc > 1.0]
        for c in sorted(cs, key=lambda c: -c.npc * g.fair(c)):
            px = max(c.price, g.fair(c)) * 1.05
            q = min(c.npc, free / max(px, 1e-9))
            if q * px < 200: continue
            g.order(n, c.cid, q, px); free -= q * px
            if free <= 0: break

    def base_demand(self, n):
        f = super().base_demand(n)
        use = n.pop * FOOD_PER_POP
        stock = n.inv.get('food', 0) + 0.6 * n.inv.get('grain', 0)
        f['food'] = max(0.0, use * (1 + 40 * self.growth(n)) + (self.food_days * use - stock) / 20.0)
        for a, q in self.maint_lump(n).items():
            short = 1.3 * q - n.inv.get(a, 0)
            if short > 0: f[a] = f.get(a, 0) + short / 10.0
        return f

    reserve_pop = 60.0

    bank_from = 0             # 从第几周期起逐步提高现金储备（0 = 不攒）
    bank_rate = 0.0           # 每周期增加的储备目标（本币）

    def bank(self):
        c = getattr(self, 'cycle', 0)
        return self.bank_rate * max(0, c - self.bank_from) if self.bank_from else 0.0

    def reserve_fn(self, n):
        return min(30000.0 + self.reserve_pop * n.pop, self.reserve_cap) + getattr(self, '_due', 0.0) + self.bank()

    def emergency(self, g, n):
        # 生产投入的应急进口只看基础储备（不被局末攒钱目标卡住）
        r = self.reserve; self.reserve = r - self.bank()
        try: super().emergency(g, n)
        finally: self.reserve = r

    def maint_lump(self, n):
        need = {}
        for fa in n.fac:
            for k, q in PROFILES[fa.pid]['maintenance'].items(): need[k] = need.get(k, 0) + q
        return need

    def act(self, g, n):
        if g.cycle % IND['maintenance_period_cycles'] == 0 and g.cycle:
            # 维护在本周期 P6 一次性扣料：先于其他采购补齐缺口，避免欠维护（d_maint 进 Φ_sustain，欠维护设施降效）
            for a, q in sorted(self.maint_lump(n).items()):
                short = q - n.inv.get(a, 0)
                if short > 0 and n.money > 2 * short * VALUE.get(a, 1) * BUY_MULT:
                    g.buy(n, a, short)
        left = CYCLES - g.cycle
        self.cycle = g.cycle
        if not hasattr(self, 'crisis_done'): self.crisis_done = False
        self.crisis(g, n)
        # 跨国电力耦合会在缺电时自动买电且不查现金（sim 未拦截，可把国库扣成负数）：现金不足储备 30% 时关掉
        want_cp = n.money > 0.3 * self.reserve_fn(n)
        if want_cp != getattr(self, '_cp_on', True):
            g.set_power_coupling(n, want_cp); self._cp_on = want_cp
        if left <= self.liq_left: self.capex = {}       # 局末不再买投资品
        super().act(g, n)
        if self.trim_on and g.cycle % self.trim_every == 0:
            self.trim(g, n)
        if left <= self.liq_left and g.cycle % 5 == 0:
            self.liquidate(g, n, left)

    liq_left = 60             # 局末清仓窗口（周期）

    def liquidate(self, g, n, left):
        """局末清仓：五支柱国力不计库存、计 NFA（现金）。保留剩余周期产线投入、口粮、发电燃料、
        剩余维护与待验证包，其余库存卖给 NPC。"""
        h = left + 5
        use, _ = self.line_inputs(n)
        keep = {a: q * h for a, q in use.items()}
        for rid, q in getattr(n, '_gen_out', {}).items():
            r = RECIPES[rid]
            for a, qq in r['inputs'].items(): keep[a] = keep.get(a, 0) + qq * q / r['outputs']['electricity'] * h
        keep['food'] = keep.get('food', 0) + n.pop * FOOD_PER_POP * h
        keep['consumer_goods'] = keep.get('consumer_goods', 0) + n.pop * _sim.CG_PER_POP * h      # 民生篮子（q_life）
        keep['medicine'] = keep.get('medicine', 0) + n.pop * _sim.MED_PER_POP * h
        mp = IND['maintenance_period_cycles']
        k_maint = left // mp + 1
        for a, q in self.maint_lump(n).items(): keep[a] = keep.get(a, 0) + q * k_maint
        for a, q in self.pack_cum.items(): keep[a] = keep.get(a, 0) + q
        for a, q in getattr(self, 'urgent', {}).items(): keep[a] = keep.get(a, 0) + q
        for tid, vleft in n.background:
            if vleft is None:
                t = TIERS[NODES[tid]['tier']]; keep[t['validation_good']] = keep.get(t['validation_good'], 0) + t['validation_amount']
        if n.project:
            t = TIERS[NODES[n.project[0]]['tier']]
            keep[t['validation_good']] = keep.get(t['validation_good'], 0) + t['validation_amount']
        for gd in sorted(n.inv):
            x = n.inv.get(gd, 0.0) - keep.get(gd, 0.0)
            if x >= 1.0 and gd in VALUE: g.sell(n, gd, x)

    def protect(self, n):
        """不出售的保留量：规划器自己的目标库存、投资预留、验证包、实验室装机件、发电燃料、口粮与一次维护用料。"""
        keep = dict(self.stock_tgt)
        for a, q in self.capex.items(): keep[a] = max(keep.get(a, 0), q)
        for a, q in getattr(self, 'urgent', {}).items(): keep[a] = keep.get(a, 0) + q
        lab = self.best_lab(n) if hasattr(self, '_grade') else None
        if lab:
            for a, q in RECIPES[lab]['installed'].items(): keep[a] = max(keep.get(a, 0), 6 * q)
        for rid, q in getattr(n, '_gen_out', {}).items():
            r = RECIPES[rid]
            for a, qq in r['inputs'].items(): keep[a] = keep.get(a, 0) + 12 * qq * q / r['outputs']['electricity']
        keep['food'] = max(keep.get('food', 0), self.food_days * n.pop * FOOD_PER_POP)
        for a, q in self.maint_lump(n).items(): keep[a] = keep.get(a, 0) + 1.3 * q
        return keep

    def trim(self, g, n):
        keep = self.protect(n)
        W = _sim.W_CAT
        for gd in sorted(W):
            q = n.inv.get(gd, 0.0)
            if q <= 1.0 or gd in PACKS or (self.trim_raw_only and gd not in RAW): continue
            h = n._sink_hist.get(gd)
            cbar = max(1.0, sum(h) / len(h)) if h else 1.0
            if q <= self.trim_k * cbar: continue
            hold = max(self.trim_keep * cbar, keep.get(gd, 0.0))
            x = q - hold
            if x >= 1.0: g.sell(n, gd, x)
