#!/usr/bin/env python3
"""裂陆纪元 战略层原型 v1.3（只用于测量，不是正式引擎）。

v1.2 部分：全国单一库存池、采掘场（G1/G2）、S1–S5 设施建设与维护、产线配方与产线模式、
人口/劳动/食品、NPC 进出口市场、科研理论点与验证。开局按 decisions/0004 启动包。

v1.3（proposals/H 第 1–4、6、7 节；README “v1.3 机制与 bot API”）：
1. 人口无上限：出生/死亡率由口粮、住房（住宅区 P110）、发展指数（A/F 节点 + 入户民用品）、
   医疗（A04/A08/A11 × 药品供给率）决定；煤电污染、通胀、停电、支持度耦合进出生/死亡。
2. 货币与外汇：本币 + RU、汇率方程（贸易差额/利差/通胀/干预/噪声 + 原型 PPP 锚）、
   央行（利率/发行/干预/钉住/资本管制）、通胀（发行 + 电价与进口成本推动）。
3. 金融：每国 6 家国家公司（利润 = 0.3·行业增加值 − 电费）、IPO ≤49%、全球每周期集合竞价
   （限价单、融资融券、强平、分红）、种子化 NPC（基本面/动量/噪声/做市）、主权债（利差、违约）。
   税收 = 工资税 + 企业利润税（取代 v1.2 的每人 1 货币）。
4. 电力：机组参数表（燃料、稳燃、强迫停运、计划检修、核电冷启动）、种子化天气（风/云/日照/来水）、
   日/夜两段调度、按边际成本的优先顺序、3 区输电容量与损耗、储能（电化学/抽水蓄能）、
   负荷带 B1–B5 与切负荷、级联停电、电价、跨国市场耦合。
5. AI：AI01–03 研究 + AI04–10 能力里程碑（data/technology.json capability_milestones）；
   G1–G3 加速器与数据中心（寿命 400 周期）、原始/精标数据、缩放律训练运行与模型版本、
   研究/生产/金融部署、推理服务市场（RU）、对齐与失控事件、出口管制、训练电价上限。

v1.3.1（提案 H 实测修订 27–34）：制造业槽位随建成住宅区扩张（SLOT0 + 20/座）；每国河流水电潜力
（8 档 120–435 额定电/周期按种子分配，超建同比摊薄来水）；进口改为“配额 + 弹性加价”（基础 40 + 物流节点增量，
配额以上边际 ×(1 + (Q − B)/B)，硬上限 3 × 配额）；国家间撮合的经济形态：推理服务买单（按卖方 Cap 计入研究部署）、
权重/数据授权（60% Cap 副本 / 50% D_cur 计 80%）、国家间货物卖单（托管、占进口配额不加价），出口管制（掌握 C04
的管制方）阻断管制品、推理服务与授权的撮合。

不模拟：地图与运输、军事、外交、情报（含权重窃取）、城邦、治理细节、tick 级调度。
确定性：所有随机量来自按 seed 派生的独立 RNG，迭代顺序固定。
"""
import json, math, random, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TECH = json.loads((ROOT / 'data/technology.json').read_text(encoding='utf-8'))
IND = json.loads((ROOT / 'data/industry.json').read_text(encoding='utf-8'))
SCORING = json.loads((ROOT / 'data/scoring.json').read_text(encoding='utf-8'))
W_CAT = SCORING['shadow_weights']['w_cat']
TECH_ACTIVE_RULES = SCORING['pillars']['tech']['active_scale_rules']
SCORING_TIER_WEIGHT = {int(k): float(v) for k, v in SCORING['pillars']['tech']['tier_weights'].items()}
SCORING_MS_WEIGHT = {k: float(v) for k, v in SCORING['pillars']['tech']['milestone_weights'].items()}
NODES = {t['id']: t for t in TECH['technologies']}
TIERS = {t['tier']: t for t in TECH['research_tiers']}
RECIPES = {r['id']: r for r in IND['recipes']}
PROFILES = {p['id']: p for p in IND['facility_profiles']}
MODES = {m['id']: m for m in IND['line_modes']}
RAW = [s['good'] for s in IND['sources'] if s['kind'] != 'salvage']
TIER_WEIGHT = {1: 1, 2: 2, 3: 4, 4: 7, 5: 11, 6: 16}      # 提案 G 候选
CYCLES = 2880                # v1.2：28,800 tick
FOOD_PER_POP = 0.06          # 启动包：200 人口对应 food 12/周期
PARTICIPATION = 0.65
PRICE = 20.0                 # 1 估值单位 = 20 RU（世界市场按 RU 标价）
IMPORT_CAP = 40.0            # 每周期进口基础配额（抽象港口/口岸能力）；v1.3 实测修订：配额随物流科技扩大，超额按弹性加价
IMPORT_LOG = {'L03': 20.0, 'L04': 40.0, 'L05': 20.0, 'L07': 40.0, 'L09': 20.0, 'L10': 20.0, 'L11': 60.0}   # 物流节点 → 配额增量
IMPORT_HARD = 3.0            # 单周期进口硬上限 = 3 × 配额；配额以上边际加价 1 + (Q − B)/B（到硬上限时 ×3）
RP_SCALE = 1.0               # 研究点倍率（敏感性测试用）
DEP_SCALE = 3.0              # v1.2：储量 ×3（decisions/0010 第 6 条）
SLOTS = 200                  # 开局制造业设施槽位（= SLOT0 + SLOT_PER_HOUSE × 开局 3 座住宅区）
SLOT0, SLOT_PER_HOUSE = 140, 20   # v1.3 实测修订：槽位随建成区扩张（每座建成住宅区 +20 制造业用地），人口与槽位同比增长

# ---------------- v1.3 常数（提案 H 候选值；原型改动记在提案“实测修订”） ----------------
# 人口
BIRTH0, DEATH0, FAMINE0 = 0.0030, 0.0010, 0.0030
HOUSE_CAP, HOUSE_COST, HOUSE_WORK, HOUSE_BUILD = 100.0, {'cement': 20, 'steel': 5}, 0.5, 8
HOUSE_FIN = 150.0            # 住宅区建设的资金成本（本币，× (1 + i/20)），利率 → 住房成本
CG_PER_POP = 0.03            # 民生篮子 consumer_goods 0.6 / 20 人
MED_PER_POP = 0.01           # 民生篮子 medicine 0.20 / 20 人
RES_POWER = 0.02             # 市政民生用电 0.4 / 20 人（负荷带 B1）
MED_NODES = ('A04', 'A08', 'A11')
# 税收与工资（取代 v1.2 每人 1 货币）
WAGE0 = 5.0                  # 每实际在岗劳动单位·周期的名义工资基准（× 工资指数，20 周期滞后 CPI）；原型校准值
# 货币
RATE0, RATE_MIN, RATE_MAX = 3.0, 0.0, 20.0     # 年化 %（1 年 = 100 周期）
FX_TB, FX_I, FX_PI, FX_PPP, FX_SIG, FX_CAP, FX_CAP_BREAK = 0.02, 0.5, 0.5, 0.05, 0.002, 0.05, 0.15
FX_FEE = 0.002
M2_0 = 100000.0
# 金融
SECTORS = ('mining', 'materials', 'manufacturing', 'energy', 'logistics', 'tech')
SHARES = 1_000_000
TRADE_FEE = 0.001
MARGIN_INIT, MARGIN_MAINT, SHORT_FEE = 0.5, 0.3, 0.0002
SECTOR_OF_FAC = {'冶金厂': 'materials', '材料厂': 'materials', '化工厂': 'materials', '机械厂': 'manufacturing',
                 '轻工厂': 'manufacturing', '装备厂': 'manufacturing', '船厂': 'manufacturing', '航空厂': 'manufacturing',
                 '制药厂': 'manufacturing', '食品厂': 'manufacturing', '农场': 'mining', '受控农场': 'mining',
                 '电子厂': 'tech', '实验室': 'tech', '数据中心': 'tech', '电站': 'energy', '水厂': 'energy'}
BOND_TERMS = (20, 50, 100)
# AI
DC_SLOTS, ACC_LIFE = 50, 400.0
AI_MS_TIER = {'AI04': 3, 'AI05': 3, 'AI06': 4, 'AI07': 4, 'AI08': 5, 'AI09': 5, 'AI10': 6}   # 计入 TP 的等价阶
AI_RAW_POP, AI_RAW_INST, AI_RAW_DECAY = 0.02, 0.1, 0.002
EXPORT_CONTROLLED = ('accelerator', 'accelerator_g2', 'accelerator_g3', 'advanced_chip', 'photonic_chip')
# 电力（提案 H §6.1；per-cycle 抽象）
#   rid: (类型, 最低稳燃, 强迫停运率/周期, 检修周期, 检修时长, 冷启动周期)
GEN_TABLE = {'P070': ('fuel', 0.4, 0.005, 100, 2, 0), 'P071': ('fuel', 0.2, 0.004, 100, 2, 0),
             'P111': ('fuel', 0.2, 0.003, 100, 2, 0), 'P112': ('hydro', 0.0, 0.002, 200, 2, 0),
             'P113': ('wind', 0.0, 0.001, 0, 0, 0), 'P114': ('solar', 0.0, 0.001, 0, 0, 0),
             'P072': ('hybrid', 0.0, 0.001, 0, 0, 0), 'P074': ('fuel', 0.0, 0.003, 0, 0, 0),
             'P073': ('nuclear', 0.7, 0.002, 200, 10, 10), 'P083': ('nuclear', 0.7, 0.002, 300, 10, 10),
             'P086': ('nuclear', 0.5, 0.010, 200, 5, 10)}
REGIONS = 3
SEG = ((0.7, True), (0.3, False))          # 一周期 = 白天 7 tick + 夜间 3 tick
DAY_SUN = 3.732 / 7.0                       # 白天 7 tick 的平均 day(k)
GRID0, GRID_STEP, GRID_COST, GRID_LOSS, GRID_LOSS_E07 = 120.0, 60.0, {'steel': 6, 'copper': 6, 'cement': 4}, 0.03, 0.009
LOAD_BANDS = ('B1', 'B2', 'B3', 'B4')       # 可重排；B5（AI 训练）固定最低
PRICE_CAP = 50.0                            # 切负荷时电价（本币/电）
OM_FUEL, OM_REN = 0.2, 0.05
PTRADE_CAP, PTRADE_LOSS = 60.0, 0.08
HYDRO_POT = tuple(120.0 + 45.0 * k for k in range(8))   # 每国河流水电潜力（额定电/周期，8 档按种子分配）；超建时同比摊薄来水
STORE_RECIPES = {'XST': (80.0, 10.0, 0.85), 'XPH': (400.0, 40.0, 0.75)}   # 能量、功率、往返效率
# 系统耦合（10_system_coupling.md / decisions/0013 / data/coupling.json）
POLL_DECAY = 0.02            # C21：Pol ← 0.98 Pol + Σ e_f x_f
EMIT = {'P070': 1.0, 'P071': 0.6, 'P111': 0.3, 'P001': 0.5, 'P005': 0.4, 'P002': 0.3, 'P006': 0.3, 'P007': 0.3,
        'P066': 0.2, 'P067': 0.2}
POLL_K = 100.0               # Pollute = Pol/(Pol + 100)
CPI_W = (('food', 0.40), ('consumer_goods', 0.30), ('electricity', 0.15), ('medicine', 0.15))   # C02
SUPPORT0 = 0.7               # 支持度（1.0 = 1000 mU）

# 采掘场（BA 提案表 2）
EXTRACT = {}
for g in ('iron_ore', 'coal', 'stone'):
    EXTRACT[g] = dict(G1=(8.0, 1.0, 3.0), G2=(20.0, 3.0, 4.0))
for g in ('copper_ore', 'bauxite', 'quartz'):
    EXTRACT[g] = dict(G1=(5.0, 1.5, 3.0), G2=(13.0, 3.5, 4.0))
for g in ('crude_oil', 'natural_gas'):
    EXTRACT[g] = dict(G1=(5.0, 1.0, 2.5), G2=(14.0, 3.0, 3.0))
for g in ('rare_ore', 'uranium_ore'):
    EXTRACT[g] = dict(G1=(2.0, 2.0, 4.0), G2=(6.0, 5.0, 5.0))
EXTRACT['water'] = dict(G1=(16.0, 0.5, 1.5), G2=(40.0, 1.5, 2.0))
EXTRACT['biomass'] = dict(G1=(5.0, 0.0, 3.0), G2=(12.0, 0.5, 2.5))
G_BUILD = dict(G1=({'steel': 4, 'cement': 4, 'basic_parts': 1}, 6), G2=({'steel': 12, 'cement': 12, 'precision_parts': 3}, 14))
G2_REQ = {'default': ({'L01'}, {'E01', 'I01'}), 'water': ({'A01'}, set()), 'biomass': ({'A01', 'A02'}, set()),
          'rare_ore': ({'I01', 'C01'}, set()), 'uranium_ore': ({'I01', 'C01'}, set())}
BASE_SIX = ['coal', 'iron_ore', 'copper_ore', 'stone', 'water', 'biomass', 'quartz']
STRATEGIC = ['crude_oil', 'natural_gas', 'bauxite', 'rare_ore', 'uranium_ore']


# ---- 原型补充配方（data 中尚无对应配方；ID 以 X 开头，不与正式表冲突） ----
_EXTRA = [
    dict(id='XST', name='电化学储能站', requires_all=['E04'], facility='电站', grade=2, inputs={},
         outputs={'storage_capacity': 80}, power=0, workers=1, installed={'battery': 8}, condition='提案 H §6.4'),
    dict(id='XPH', name='抽水蓄能', requires_all=['E02', 'L01'], facility='电站', grade=2, inputs={},
         outputs={'storage_capacity': 400}, power=0, workers=2, installed={'steel': 30, 'cement': 60, 'basic_parts': 6},
         condition='提案 H §6.4；工期 30 周期'),
]
for _r in _EXTRA:
    if _r['id'] not in RECIPES: RECIPES[_r['id']] = _r; IND['recipes'].append(_r)
DC_RECIPES = sorted(r for r in RECIPES if 'compute' in RECIPES[r]['outputs'])
DC_GOOD = {r: next(iter(RECIPES[r]['installed']), 'accelerator') for r in DC_RECIPES}
if 'P110' in RECIPES:           # 住宅区不是产线：由 build_housing 建，不走 assign
    HOUSE_COST = dict(RECIPES['P110']['installed']); HOUSE_WORK = RECIPES['P110']['workers']
    HOUSE_CAP = RECIPES['P110']['outputs'].get('housing_capacity', HOUSE_CAP)
NON_LINE = {'P110'}
ELIGIBLE = frozenset(IND.get('mode_eligible_facilities', ()))
GRADE_WEIGHT = {'S1': 1.0, 'S2': 2.0, 'S3': 4.0, 'S4': 6.0, 'S5': 8.0}
BUILD_GOODS = set()
for _p in PROFILES.values(): BUILD_GOODS |= set(_p['build'])
PROTECTED = set(DC_RECIPES) | set(STORE_RECIPES) | {r for r in RECIPES if 'curated_data' in RECIPES[r]['outputs']}

_MS_DEFAULT = [('AI04', 'deployment', 5), ('AI05', 'algorithm', 10), ('AI06', 'deployment', 15), ('AI07', 'deployment', 20),
               ('AI08', 'algorithm', 25), ('AI09', 'deployment', 30), ('AI10', 'frontier', 35)]
AI_MS = [(m['id'], m['milestone'], m['cap_threshold']) for m in TECH.get('capability_milestones', [])] or _MS_DEFAULT
A_NODES = frozenset(t for t in NODES if NODES[t]['branch'] == 'A')
F_NODES = frozenset(t for t in NODES if NODES[t]['branch'] == 'F')
AF_N = max(1, len(A_NODES) + len(F_NODES))
LAB_BAND = {r for r in RECIPES if RECIPES[r]['facility'] in ('实验室', '受控农场')}
FARM = {r for r in RECIPES if RECIPES[r]['facility'] in ('农场', '受控农场')}


def _values():
    v = {g: 1.0 for g in RAW}
    fixed = set(v) | {'electricity', 'research_points', 'compute', 'housing_capacity', 'storage_capacity', 'curated_data'}
    for k in fixed - set(RAW): v[k] = 0.0
    v['electricity'] = 0.1
    for _ in range(60):
        for r in IND['recipes']:
            outs = [g for g in r['outputs'] if g not in fixed]
            if not outs or not all(g in v for g in r['inputs']):
                continue
            cost = sum(v[g] * q for g, q in r['inputs'].items()) + 0.1 * r['power'] + 0.2 * r['workers'] + 0.5
            per = 1.15 * cost / sum(r['outputs'][g] for g in outs)
            for g in outs:
                v[g] = min(v.get(g, math.inf), per)
    v['scrap_steel'] = v['scrap_copper'] = 0.5
    return v
VALUE = _values()
GENS = sorted(r for r in RECIPES if 'electricity' in RECIPES[r]['outputs'])
FUEL_VAL = {r: sum(VALUE.get(g, 1) * q for g, q in RECIPES[r]['inputs'].items()) / RECIPES[r]['outputs']['electricity'] for r in GENS}
GEN_MC = FUEL_VAL                     # 兼容旧名：估值单位/电
RENEWABLE = {r for r in GENS if not RECIPES[r]['inputs']}
for _r in GENS:
    if _r not in GEN_TABLE: GEN_TABLE[_r] = ('fuel' if RECIPES[_r]['inputs'] else 'wind', 0.0, 0.003, 0, 0, 0)


def tp(n):
    """理论点指标：已知科技的理论点 + AI 能力里程碑按等价阶计（AI04–05→3 阶，06–07→4，08–09→5，10→6）。"""
    s = sum(TIERS[NODES[t]['tier']]['theory_points'] for t in n.known if t in NODES)
    return s + sum(TIERS[AI_MS_TIER.get(m, 3)]['theory_points'] for m in n.ai_ms)


def max_tier(n):
    return max([NODES[t]['tier'] for t in n.known if t in NODES] + [AI_MS_TIER.get(m, 3) for m in n.ai_ms] + [0])


def v_tilde(v, v0=10.0):
    """07 号 §2.1 全域 C^1 正值平滑变换 V_tilde(V) > 0（负值分支 V0^2/(V0 - V) 严格递增且绝不发生浮点下溢）。"""
    v = float(v)
    return (v + v0) if v >= 0.0 else (v0 * v0 / (v0 - v))


def map_log_tanh(r):
    """07 号 §2.3 对数双曲正切百分制映射 s = 50 + 50*tanh(0.6*ln R)。"""
    if r is None or r <= 0.0:
        return 0.0
    return 50.0 + 50.0 * math.tanh(0.6 * math.log(r))


def score_v_one(v_p, v_s, v_peers_eval=None, v_peers_shadow=None):
    """07 号 §2.2-2.3 同地图影子基线相对比 R_k = V_tilde(V_player)/V_tilde(V_shadow) 与百分制得分 s(k)（AR-004 全域反焦土单调性）。"""
    vt_p = v_tilde(v_p)
    vt_s = v_tilde(v_s)
    r_self = vt_p / vt_s
    q_eff = 1.0
    r_k = r_self
    return r_k, map_log_tanh(r_k), r_self, q_eff


class Facility:
    __slots__ = ('pid', 'remaining', 'lines', 'arrears')
    def __init__(self, pid, remaining):
        self.pid = pid; self.remaining = remaining
        self.lines = [None] * PROFILES[pid]['lines']
        self.arrears = 0
    @property
    def grade(self): return PROFILES[self.pid]['recipe_grade']


class Mine:
    __slots__ = ('good', 'level', 'remaining')
    def __init__(self, good, level, remaining):
        self.good = good; self.level = level; self.remaining = remaining


class Company:
    __slots__ = ('cid', 'nid', 'sector', 'state', 'npc', 'price', 'ipo_price', 'profit', 'retained', 'payout', 'listed',
                 'hist', 'avg20', 'last_vol', 'ext_paid_in_ru', 'capex_escrow')
    def __init__(self, cid, nid, sector):
        self.cid = cid; self.nid = nid; self.sector = sector
        self.state = SHARES; self.npc = 0.0
        self.price = 0.05; self.ipo_price = 0.05; self.profit = 0.0; self.retained = 0.0; self.payout = 0.0
        self.listed = False; self.hist = [0.05] * 6; self.avg20 = 0.05; self.last_vol = 0.0
        self.ext_paid_in_ru = 0.0; self.capex_escrow = 0.0


class Bond:
    __slots__ = ('bid', 'issuer', 'face', 'coupon', 'maturity', 'holders')
    def __init__(self, bid, issuer, face, coupon, maturity):
        self.bid = bid; self.issuer = issuer; self.face = face; self.coupon = coupon; self.maturity = maturity
        self.holders = {'npc': face}      # holder(nid 或 'npc') -> 面值


class Nation:
    def __init__(self, nid, bot, deposits):
        self.nid = nid; self.bot = bot
        self.inv = dict(food=180, grain=90, coal=200, iron_ore=120, copper_ore=60, stone=90, quartz=45, water=180,
                        biomass=60, steel=48, copper=20, cement=48, glass=16, chemicals=20, basic_parts=18,
                        consumer_goods=90, lab_supplies=24, ammunition=32, vehicle=3)
        self.money = 100000.0          # 本币
        self.pop = 200.0
        self.known = set()
        self.project = None            # [tid, theory_done, None]
        self.deposits = dict(deposits)
        self.mine_slots = {g: (6 if g in BASE_SIX else 3) for g in deposits}
        self.fac = []; self.mines = []
        self.power_ratio = 1.0; self.labor_ratio = 1.0; self.food_ratio = 1.0
        self.value_log = []; self.va_cat_log = []; self.errors = 0; self.imported = 0.0
        self.rp_log = []
        self.u_log = []
        self._rp_overflow = 0.0
        self._base_rp = 0.0
        self._life_fac = 1.0
        self._default_fx_shock = 0.0
        self.last_default_cycle = -10**6
        self.last_peg_break_cycle = -10**6
        self.trade_abs_ema = 0.0
        self._good_net = {}
        self._good_net_hist = []
        self._svc_net_hist = []
        self._sink_cur = {}
        self._sink_hist = {g: [max(1.0, self.inv.get(g, 0.0) / 10.0)] * 20 for g in W_CAT}
        self.background = []
        # 人口与民生
        self.housing = math.ceil(1.5 * 200 / HOUSE_CAP); self.housing_build = []
        self.birth = self.death = 0.0; self.dev = 0.0; self.med = 0.0; self.cg_ratio = 0.0; self.med_ratio = 0.0
        self.support = SUPPORT0; self.pollution = 0.0; self.pollute = 0.0; self.employed = 0.0; self.tax_log = []; self.wages = 0.0
        self.house_avail = 1.0; self.house_private = 0; self.cpi = 1.0; self.cpi0 = None; self.wage_index = 1.0
        self.consumption = 0.0; self.tax_rate = 0.20; self.crisis = 0; self.shed_hist = [0.0] * 5; self.b1_short = 0.0
        self.import_ema = {}; self.build_fund = 0.0; self.portfolio_in = 0.0; self.d_syn = 0.0
        self.soc_paid_ratio = 1.0
        self.last_offer = {}
        # 货币
        self.e = 1.0; self.rate = RATE0; self.reserves = 0.0; self.peg = None; self.peg_ban = 0; self.capctl = 0.0
        self.m2 = M2_0; self.pi = 0.0; self.pi_cum = 0.0; self.price_level = 1.0
        self.gdp = 1.0; self.tb = 0.0; self.fx_order = 0.0; self.trade_ru = 0.0; self.printed = 0.0
        self.e_prev = 1.0; self.elec_ema = 8.0; self.issued = 0.0
        # 金融
        self.margin_debt = 0.0; self.short_coll = 0.0; self.credit = 1.0; self.defaults = 0
        self.liquidate = False; self.fin_pnl = 0.0; self.div_in = 0.0; self.ipo_cash = 0.0
        # AI
        self.compute = 0.0; self.compute_train = 0.0; self.compute_inf = 0.0; self.ai_alloc = {}
        self.job = None                # dict(left, run, alloc, max_price, base, base_raw)
        self.models = []               # [C_eff]
        self.models_raw = []           # [C_raw] 与 models 一一对应
        self.cap = 0.0; self.align = 0.0; self.align_acc = 0.0; self.ai_ms = set(); self.algo = 1.0
        self.d_raw = 0.0; self.d_cur = 0.0; self.d_lic = 0.0; self.d_lic_from = {}
        self.ai_halt = 0; self.incidents = 0; self.incident_log = []
        self.ai_rp_mult = 1.0; self.ai_workers = 0.0; self.ai_fee_cut = 0.0; self.ai_revenue = 0.0
        self.ai_offer_price = None; self.export_ctl = set(); self.ai_risk = 0.002
        self.dc_health = {}            # (fi, li) -> 加速器健康度
        # 电力
        self.grid_cap = GRID0; self.grid_build = []
        self.load_priority = list(LOAD_BANDS); self.band_ratio = dict.fromkeys(LOAD_BANDS + ('B5',), 1.0)
        self.outage = {}; self.cold = {}
        self.store_e = 0.0; self.store_cost = 0.0; self.store_cap = 0.0; self.store_pw = 0.0
        self.blackout = 0; self.blackouts = 0; self.elec_price = 8.0; self.grid_need = 0.0; self.grid_stress = False
        self.power_couple = False; self.power_traded = 0.0; self.power_imported = 0.0
        self.hydro_pot = HYDRO_POT[-1]
        # 国家间撮合（v1.3 实测修订）
        self.ai_bid = None; self.ai_bought = (0.0, 0.0); self.ai_spent = 0.0; self.lic_cool = {}
        self.goods_in = 0.0; self.goods_out = 0.0; self.lic_in = 0.0; self.lic_out = 0.0
        self.bond_issued = 0.0; self.ai_rev_cum = 0.0; self.ai_buy_cum = 0.0; self.sec_bought = 0.0; self.power_sold = 0.0
        self.load = {}; self.spill = 0.0; self.shed = 0.0
        self._sec_pw = {s: 0.0 for s in SECTORS}; self._sec_va = {s: 0.0 for s in SECTORS}; self._sec_w = {s: 0.0 for s in SECTORS}
        self._gen_out = {}; self._served = 0.0; self._tr_share = 0.0; self._res_bill = 0.0; self._labor = 130.0
        start = [('P070', 3), ('P028', 3), ('P031', 4), ('P032', 2), ('P001', 2), ('P002', 1), ('P005', 1),
                 ('P062', 1), ('P007', 1), ('P011', 1), ('P075', 1)]
        self._active_recipes = {rid: float(k) for rid, k in start}
        self._active_modes = {'general': sum(k for _, k in start)}
        for rid, k in start:
            for _ in range(k):
                f = Facility('S1', 0); f.lines[0] = (rid, 'general'); self.fac.append(f)
        for g, k in (('coal', 2), ('iron_ore', 1), ('copper_ore', 1), ('stone', 1), ('quartz', 1), ('water', 2), ('biomass', 1)):
            for _ in range(k): self.mines.append(Mine(g, 'G1', 0))

    def labor(self): return self.pop * PARTICIPATION
    def housing_cap(self): return (self.housing * self.house_avail + self.house_private) * HOUSE_CAP
    def slot_cap(self): return SLOT0 + SLOT_PER_HOUSE * self.housing
    def import_quota(self): return IMPORT_CAP + sum(v for k, v in IMPORT_LOG.items() if k in self.known)
    def can_use(self, rid): return set(RECIPES[rid]['requires_all']) <= self.known
    def can_mode(self, mid): return set(MODES[mid]['requires_all']) <= self.known
    def available_techs(self):
        out = []
        cur = self.project[0] if self.project else None
        bg = {b[0] for b in self.background}
        for t in NODES.values():
            if t['id'] in self.known or t['id'] == cur or t['id'] in bg: continue
            if set(t['requires_all']) <= self.known and (not t['requires_any'] or set(t['requires_any']) & self.known):
                out.append(t['id'])
        return out
    def alpha(self): return 0.92 if {'R05', 'C02'} <= self.known else 0.75
    def g2_ok(self, good):
        req_all, req_any = G2_REQ.get(good, G2_REQ['default'])
        return req_all <= self.known and (not req_any or req_any & self.known)
    def d_eff(self): return self.d_cur + 0.3 * self.d_raw + 0.8 * self.d_lic + 0.5 * min(self.d_syn, self.d_cur)


class Game:
    def __init__(self, seed, bots):
        rng = random.Random(seed)
        self.seed = seed
        self.cycle = 0
        self.nations = []
        order = STRATEGIC[:]; rng.shuffle(order)
        for i, b in enumerate(bots):
            dep = {g: math.inf for g in ('water', 'biomass')}
            for g in ('coal', 'iron_ore', 'stone'): dep[g] = DEP_SCALE * 16000.0
            dep['copper_ore'] = DEP_SCALE * 9000.0; dep['quartz'] = DEP_SCALE * 6000.0
            for k in (0, 1):
                dep[order[(2 * i + k) % 5]] = DEP_SCALE * (5000.0 if order[(2 * i + k) % 5] not in ('rare_ore', 'uranium_ore') else 2500.0)
            self.nations.append(Nation(i, b, dep))
        for n in self.nations: n.dep0 = dict(n.deposits)
        # v1.3 独立 RNG 流（互不干扰，保持确定性）
        self.rng_fx = random.Random(seed * 7919 + 1)
        self.rng_mkt = random.Random(seed * 7919 + 2)
        self.rng_w = random.Random(seed * 7919 + 3)
        self.rng_ai = random.Random(seed * 7919 + 4)
        self.rng_out = random.Random(seed * 7919 + 5)
        # 天气：每国 3 区；风况均值 0.20–0.50，云量均值 0.3–0.5，纬度系数 0.6–1.0（座位轮换对称）
        self.wx = []
        for i in range(len(bots)):
            regs = []
            for r in range(REGIONS):
                k = (i + r) % 8
                wbar = 0.20 + 0.30 * ((k * 3) % 8) / 7.0
                kbar = 0.30 + 0.20 * ((k * 5) % 8) / 7.0
                lat = 0.60 + 0.40 * ((k * 7) % 8) / 7.0
                regs.append(dict(wbar=wbar, kbar=kbar, lat=lat, W=wbar, K=kbar, rain=1.0))
            self.wx.append(regs)
        self.companies = []
        for n in self.nations:
            for s in SECTORS:
                self.companies.append(Company(len(self.companies), n.nid, s))
        self.orders = []
        self.holdings = {}             # (nid, cid) -> 股数（负 = 融券）
        self.bonds = []; self.bond_seq = 0
        self.noise_next = {}
        self.ai_offers = {}            # nid -> (qty, price)
        self._cap_w = 0.0
        self.ai_demand = 50.0; self.ai_sold = {}
        self.stats = dict(auction_vol=0.0, liquidations=0, defaults=0, pegs_broken=0, blackouts=0, incidents=0,
                          goods_trade=0.0, ai_trade=0.0, licenses=0)
        pots = list(HYDRO_POT); random.Random(seed * 7919 + 6).shuffle(pots)
        for n in self.nations: n.hydro_pot = pots[n.nid % len(pots)]
        self.goods_offers = {}         # (卖方 nid, good) -> [数量（已托管）, RU 单价]
        self.lic_offers = {}           # (卖方 nid, 'weights'|'data') -> (RU 价格, 目标 nid 或 None)

    # =============== 公开查询 ===============
    def company(self, nid, sector): return self.companies[nid * len(SECTORS) + SECTORS.index(sector)]
    def buy_price(self, n, good):
        """进口单价（本币）。出口管制：每个掌握 C04 且对本国管制的国家使管制品进口价 +50%（原型对“撮合被拒”的抽象）。"""
        mult = 1.0
        if good in EXPORT_CONTROLLED:
            mult += 0.5 * sum(1 for m in self.nations if m is not n and n.nid in m.export_ctl and 'C04' in m.known)
        return VALUE.get(good, 1) * PRICE * 1.6 * mult / n.e
    def sell_price(self, n, good): return VALUE.get(good, 1) * PRICE * 0.6 / n.e
    def fx(self, a, b): return self.nations[a].e / self.nations[b].e
    def annual_tax(self, n):
        t = n.tax_log[-20:]
        return 100.0 * sum(t) / max(1, len(t))
    def debt(self, n): return sum(b.face for b in self.bonds if b.issuer == n.nid)
    def debt_ceiling(self, n):
        t = n.tax_log[-20:]
        return 40.0 * max((sum(t) / len(t) if t else 0.0), 25.0)
    def spread(self, n):
        return 1.0 + 4.0 * self.debt(n) / max(self.annual_tax(n), 1.0) + 10.0 * (1 - n.credit) + 5.0 * n.defaults
    def bond_price(self, b):
        iss = self.nations[b.issuer]
        y = (iss.rate + self.spread(iss)) / 10000.0
        left = max(0, b.maturity - self.cycle); c = b.coupon / 10000.0
        if y <= 0: return c * left + 1.0
        df = (1 + y) ** (-left)
        return c * (1 - df) / y + df
    def fair(self, c):
        """公允值/股（C08）：m·100·Π̄/10^6，m = clip(1/(i/100 + 0.05), 5, 15)，Π̄ 为税后利润 EMA；
        × C03 切负荷/黑网因子 max(0.7, 1 − 0.5σ̄)，× C24 采掘储量寿命因子，危机期 ×0.8；下限 0.01。"""
        n = self.nations[c.nid]
        m = min(15.0, max(5.0, 1.0 / (n.rate / 100.0 + 0.05)))
        v = m * 100.0 * max(0.0, c.profit) / SHARES
        v *= max(0.7, 1 - 0.5 * sum(n.shed_hist) / len(n.shed_hist))
        if c.sector == 'mining': v *= getattr(n, '_life_fac', 1.0)
        if n.crisis > 0: v *= 0.8
        return max(0.01, v)
    def fair_neutral(self, c):
        """中性利率（i_0 = 5%，m_0 = 10）且不含本国危机折扣的公允值，用于 NFA 外资持股负债保底（防加息/自造危机压价消债）。"""
        n = self.nations[c.nid]
        v = 10.0 * 100.0 * max(0.0, c.profit) / SHARES
        v *= max(0.7, 1 - 0.5 * sum(n.shed_hist) / len(n.shed_hist))
        if c.sector == 'mining': v *= getattr(n, '_life_fac', 1.0)
        return max(0.01, v)
    def market_signal(self, n, cid):
        """AI 金融部署（AI07）：下一次竞价 NPC 净买量估计，误差 σ = |真值| × max(0.05, 1 − Cap/40)。"""
        if 'AI07' not in n.ai_ms or n.ai_fee_cut <= 0: return None
        true = self.noise_next.get(cid, 0.0)
        r = random.Random(self.seed * 31 + self.cycle * 997 + cid * 13 + n.nid)
        return true + r.gauss(0, abs(true) * max(0.05, 1 - n.cap / 40.0) + 1.0)
    def _eq_liab_ru(self, c):
        """07 号 §1.2 (AR-008)：本国被外部持有的股票每股负债估值（RU），含多轮增发累计实缴外资地板。"""
        n = self.nations[c.nid]
        ext_sh = max(1.0, SHARES - c.state)
        paid_in_ru = c.ext_paid_in_ru / ext_sh
        return max(min(c.avg20, 2.0 * self.fair(c)) * n.e, self.fair_neutral(c) * n.e, c.ipo_price * n.e, paid_in_ru)

    def nfa(self, n):
        """净对外金融资产（RU，07 号 §1.2 / AR-008）：持有他国股票、他国债券、外汇储备，
        减去空头仓位负债、他国持有的本国股票（含累计实缴外资保底）与他国持有的本国债券（面值 100% 保底）。"""
        v = n.reserves
        for (nid, cid), q in self.holdings.items():
            if not q: continue
            c = self.companies[cid]
            if nid == n.nid:
                if q > 0 and c.nid != n.nid:
                    v += q * min(c.avg20, 2.0 * self.fair(c)) * self.nations[c.nid].e
                elif q < 0:
                    px_short = max(c.price, min(c.avg20, 2.0 * self.fair(c)), self.fair_neutral(c))
                    v -= (-q) * px_short * self.nations[c.nid].e
            elif nid != n.nid and c.nid == n.nid and q > 0:
                v -= q * self._eq_liab_ru(c)
        for b in self.bonds:
            if b.issuer != n.nid:
                h = b.holders.get(n.nid, 0.0)
                if h: v += h * min(1.2, self.bond_price(b)) * self.nations[b.issuer].e
            else:
                for hd, f in b.holders.items():
                    if hd != 'npc' and hd != n.nid and f > 0:
                        v -= f * max(1.0, min(1.2, self.bond_price(b))) * n.e
        return v

    def nfa_full(self, n):
        """含本币结余变动、NPC 池持有的本国流通股实缴资本负债及国债/融资/融券/印钞负债的完整金融净头寸（07 号 §1.2 / AR-008，用于 U_ind 年金化）。"""
        v = self.nfa(n) + (n.money + n.build_fund - 100000.0 + n.short_coll - n.margin_debt - n.issued) * n.e
        for s in SECTORS:
            c = self.company(n.nid, s)
            if c.listed and c.npc > 0:
                v -= c.npc * self._eq_liab_ru(c)
        for b in self.bonds:
            if b.issuer == n.nid:
                f = b.holders.get('npc', 0.0)
                if f > 0: v -= f * max(1.0, min(1.2, self.bond_price(b))) * n.e
        return v

    def net_worth(self, n):
        """净资产（RU）：（本币现金 − 国债 − 融资负债 − 央行累计发行 + 融券保证金）× e + NFA + 库存估值（世界价）。本国国有股不计。"""
        cash = n.money - self.debt(n) - n.margin_debt - n.issued + n.short_coll
        stock = sum(VALUE.get(g, 1) * q for g, q in n.inv.items() if q > 0) * PRICE
        return cash * n.e + self.nfa(n) + stock

    # =============== 动作：v1.2 ===============
    def build(self, n, pid):
        p = PROFILES[pid]
        if (pid != 'S1' and 'L01' not in n.known) or len(n.fac) >= n.slot_cap() or any(n.inv.get(g, 0) < q for g, q in p['build'].items()):
            n.errors += 1; return False
        for g, q in p['build'].items():
            n.inv[g] -= q; n._sink_cur[g] = n._sink_cur.get(g, 0.0) + q
        n.fac.append(Facility(pid, p['build_cycles'])); return True

    def build_mine(self, n, good, level='G1'):
        cost, cyc = G_BUILD[level]
        if good not in n.deposits or (level == 'G2' and not n.g2_ok(good)):
            n.errors += 1; return False
        if sum(1 for m in n.mines if m.good == good) >= n.mine_slots[good] or any(n.inv.get(g, 0) < q for g, q in cost.items()):
            n.errors += 1; return False
        for g, q in cost.items():
            n.inv[g] -= q; n._sink_cur[g] = n._sink_cur.get(g, 0.0) + q
        n.mines.append(Mine(good, level, cyc)); return True

    def upgrade_mine(self, n, idx):
        m = n.mines[idx]; cost, cyc = G_BUILD['G2']
        if m.level == 'G2' or not n.g2_ok(m.good) or any(n.inv.get(g, 0) < q for g, q in cost.items()):
            n.errors += 1; return False
        for g, q in cost.items():
            n.inv[g] -= q; n._sink_cur[g] = n._sink_cur.get(g, 0.0) + q
        m.level = 'G2'; m.remaining = cyc; return True

    def assign(self, n, fi, li, rid, mode='general'):
        f = n.fac[fi]
        if rid is None:
            f.lines[li] = None; n.dc_health.pop((fi, li), None); return True
        if rid in NON_LINE: n.errors += 1; return False
        r = RECIPES[rid]; m = MODES[mode]
        if (mode != 'general' and ELIGIBLE and r['facility'] not in ELIGIBLE) or not n.can_use(rid) or not n.can_mode(mode) or r['grade'] > f.grade:
            n.errors += 1; return False
        need = dict(r['installed'])
        for g, q in m['installation'].items(): need[g] = need.get(g, 0) + q
        if any(n.inv.get(g, 0) < q for g, q in need.items()):
            n.errors += 1; return False
        for g, q in need.items():
            n.inv[g] -= q; n._sink_cur[g] = n._sink_cur.get(g, 0.0) + q
        f.lines[li] = (rid, mode); f.remaining = max(f.remaining, math.ceil(m['switch_cycles']))
        if rid == 'XPH': f.remaining = max(f.remaining, 30)
        if rid in DC_RECIPES: n.dc_health[(fi, li)] = 1.0
        return True

    def import_mult(self, n, qty):
        """本周期再进口 qty 的平均加价倍数（配额 B 以内 1；以上边际 1 + (Q − B)/B）。"""
        if qty <= 0: return 1.0
        B = n.import_quota(); q0 = n.imported; q1 = q0 + qty
        ex = (max(q1, B) - B) ** 2 - (max(q0, B) - B) ** 2
        return 1.0 + ex / (2 * B * qty)
    def import_cost(self, n, good, qty):
        """再进口 qty 单位 good 的总价（本币，含超配额加价）。"""
        return qty * self.buy_price(n, good) * self.import_mult(n, qty)
    def import_room(self, n): return max(0.0, IMPORT_HARD * n.import_quota() - n.imported)

    def buy(self, n, good, qty):
        price = self.buy_price(n, good)
        fund = n.build_fund if good in BUILD_GOODS else 0.0        # C30：增发募资只能买建材
        qty = min(qty, self.import_room(n))
        if qty <= 0 or good in ('electricity', 'research_points', 'compute'): return 0
        avail = n.money + fund
        if qty * price * self.import_mult(n, qty) > avail:
            lo, hi = 0.0, qty
            for _ in range(40):
                mid = 0.5 * (lo + hi)
                if mid * price * self.import_mult(n, mid) <= avail: lo = mid
                else: hi = mid
            qty = lo
            if qty <= 1e-9: return 0
        cost = qty * price * self.import_mult(n, qty); use = min(fund, cost); n.build_fund -= use
        n.imported += qty; n.money -= cost - use; n.inv[good] = n.inv.get(good, 0) + qty
        n.import_ema[good] = n.import_ema.get(good, 0.0) + 0.05 * qty
        n._good_net[good] = n._good_net.get(good, 0.0) - qty
        n.trade_ru -= cost * n.e
        return qty

    def sell(self, n, good, qty):
        qty = min(qty, n.inv.get(good, 0))
        if qty <= 0: return 0
        v = qty * self.sell_price(n, good)
        n.inv[good] -= qty; n.money += v; n.trade_ru += v * n.e
        n._good_net[good] = n._good_net.get(good, 0.0) + qty
        return qty

    def research(self, n, tid):
        if n.project is not None or tid not in n.available_techs(): n.errors += 1; return False
        n._rp_overflow = 0.0
        n.project = [tid, 0.0, None]; return True

    # =============== 动作：v1.3 人口 / 电力 ===============
    def housing_money(self, n): return HOUSE_FIN * (1 + n.rate / 20.0) * n.price_level
    def build_housing(self, n, k=1):
        """建 k 座住宅区（P110：cement 20 + steel 5 + 资金 150×(1+i/20)×价格水平；工期 8；维护 0.5 工人；不占制造业槽位）。"""
        done = 0; fin = self.housing_money(n)
        for _ in range(int(k)):
            if n.money < fin or any(n.inv.get(g, 0) < q for g, q in HOUSE_COST.items()): break
            for g, q in HOUSE_COST.items():
                n.inv[g] -= q; n._sink_cur[g] = n._sink_cur.get(g, 0.0) + q
            n.money -= fin; n.housing_build.append(HOUSE_BUILD); done += 1
        return done

    def build_grid(self, n, k=1):
        """区间高压走廊：容量 +60/回（steel 6 + copper 6 + cement 4，工期 10）。"""
        done = 0
        for _ in range(int(k)):
            if any(n.inv.get(g, 0) < q for g, q in GRID_COST.items()): break
            for g, q in GRID_COST.items():
                n.inv[g] -= q; n._sink_cur[g] = n._sink_cur.get(g, 0.0) + q
            n.grid_build.append(10); done += 1
        return done

    def set_load_priority(self, n, order):
        if sorted(order) == sorted(LOAD_BANDS): n.load_priority = list(order); return True
        n.errors += 1; return False

    def set_power_coupling(self, n, on=True):
        """跨国电力市场耦合（双方都开启才导通）：电从 RU 低价侧流向高价侧，每对每周期 ≤ 60，损耗 8%。"""
        n.power_couple = bool(on); return True

    def dc_refit(self, n, fi, li):
        """数据中心补装加速器，恢复健康度到 1（消耗 (1 − 健康度) × 50 台同代加速器）。"""
        ln = n.fac[fi].lines[li]
        if not ln or ln[0] not in DC_RECIPES: n.errors += 1; return False
        gd = DC_GOOD[ln[0]]; h = n.dc_health.get((fi, li), 1.0)
        need = (1 - h) * DC_SLOTS
        if n.inv.get(gd, 0) < need: n.errors += 1; return False
        n.inv[gd] -= need; n._sink_cur[gd] = n._sink_cur.get(gd, 0.0) + need
        n.dc_health[(fi, li)] = 1.0; return True

    # =============== 动作：v1.3 货币 ===============
    def set_rate(self, n, rate):
        if 'F02' not in n.known: n.errors += 1; return False
        n.rate = min(RATE_MAX, max(RATE_MIN - (1.0 if 'F06' in n.known else 0.0), float(rate))); return True

    def fx_intervene(self, n, ru):
        """ru>0：卖储备买本币（托汇率）；ru<0：卖本币买储备。需 F02。"""
        if 'F02' not in n.known: n.errors += 1; return False
        n.fx_order += ru; return True

    def set_peg(self, n, target):
        if 'F02' not in n.known or n.peg_ban > 0 or (target is not None and n.reserves <= 0): n.errors += 1; return False
        n.peg = target; return True

    def set_capital_controls(self, n, pct):
        if 'F02' not in n.known: n.errors += 1; return False
        n.capctl = 0.0 if pct <= 0 else min(5.0, max(1.0, pct)); return True

    def print_money(self, n, amount):
        if 'F02' not in n.known or amount <= 0: n.errors += 1; return False
        amount = min(amount, 0.02 * n.m2)
        n.money += amount; n.m2 += amount; n.printed += amount; n.issued += amount
        return True

    # =============== 动作：v1.3 金融 ===============
    def ipo(self, n, sector, frac):
        """一级市场：出售本国公司股份给 NPC（未上市按公允值 95%），累计流通 ≤ 49%。需 F01。"""
        c = self.company(n.nid, sector)
        if 'F01' not in n.known: n.errors += 1; return 0.0
        q = min(frac * SHARES, 0.49 * SHARES - (SHARES - c.state))
        if q <= 0: return 0.0
        if not c.listed:
            c.price = self.fair(c); c.ipo_price = c.price; c.hist = [c.price] * 6; c.avg20 = c.price
        px = c.price * 0.95
        c.state -= q; c.npc += q; c.listed = True
        v = q * px
        c.ext_paid_in_ru += v * n.e
        n.money += v; n.ipo_cash += v; return v

    def secondary_offering(self, n, sector, frac=0.10):
        """C30 增发（上市公司，每 100 周期 ≤ 10% 总股本，外部持股 ≤ 49%）：按市价 95% 卖给 NPC，募资进建设专户（只能买建材）。"""
        c = self.company(n.nid, sector)
        if not c.listed or self.cycle - n.last_offer.get(sector, -10**6) < 100: n.errors += 1; return 0.0
        q = min(frac, 0.10) * SHARES
        q = min(q, 0.49 * SHARES - (SHARES - c.state))
        if q <= 0: return 0.0
        c.state -= q; c.npc += q; n.last_offer[sector] = self.cycle
        v = q * c.price * 0.95
        c.ext_paid_in_ru += v * n.e
        c.capex_escrow += v; n.build_fund += v; return v

    def set_payout(self, n, sector, ratio):
        c = self.company(n.nid, sector); c.payout = min(1.0, max(0.0, ratio)); return True

    def order(self, n, cid, qty, limit, margin=False):
        """二级市场限价单（本周期集合竞价）。qty>0 买，<0 卖；超过持仓的卖出为融券（需 margin）。需 F03。"""
        c = self.companies[cid]
        if 'F03' not in n.known or not c.listed or qty == 0 or limit <= 0: n.errors += 1; return False
        self.orders.append((n.nid, cid, float(qty), float(limit), bool(margin))); return True

    def issue_bond(self, n, amount, term=50):
        """发国债（需 F02）：面值卖给 NPC，票面 = max(0, i + 利差)；上限 = 40 × max(T̄_20, 25)；利差 > 30 无人接。"""
        if 'F02' not in n.known or term not in BOND_TERMS or amount <= 0: n.errors += 1; return 0.0
        amount = min(amount, self.debt_ceiling(n) - self.debt(n))
        if amount <= 0 or self.spread(n) > 30: n.errors += 1; return 0.0
        b = Bond(self.bond_seq, n.nid, amount, max(0.0, n.rate + self.spread(n)), self.cycle + term); self.bond_seq += 1
        self.bonds.append(b); n.money += amount; n.bond_issued += amount; return amount

    def buy_bond(self, n, bid, face):
        b = next((x for x in self.bonds if x.bid == bid), None)
        if b is None or b.issuer == n.nid: n.errors += 1; return 0.0
        face = min(face, b.holders.get('npc', 0.0))
        cost = face * self.bond_price(b) * self.fx(b.issuer, n.nid) * (1 + FX_FEE + self.nations[b.issuer].capctl / 100)
        if face <= 0 or cost > n.money: n.errors += 1; return 0.0
        n.money -= cost; b.holders['npc'] -= face; b.holders[n.nid] = b.holders.get(n.nid, 0.0) + face
        self.nations[b.issuer].portfolio_in += cost * n.e; return face

    def sell_bond(self, n, bid, face):
        b = next((x for x in self.bonds if x.bid == bid), None)
        if b is None: return 0.0
        face = min(face, b.holders.get(n.nid, 0.0))
        if face <= 0: return 0.0
        n.money += face * self.bond_price(b) * self.fx(b.issuer, n.nid) * (1 - FX_FEE)
        b.holders[n.nid] -= face; b.holders['npc'] = b.holders.get('npc', 0.0) + face; return face

    def repay_bond(self, n, bid):
        """提前赎回本国债券（按市价从全部持有人买回）。"""
        b = next((x for x in self.bonds if x.bid == bid and x.issuer == n.nid), None)
        if b is None: return False
        px = self.bond_price(b); cost = b.face * px
        if cost > n.money: n.errors += 1; return False
        n.money -= cost
        for h, f in b.holders.items():
            if h != 'npc': self.nations[h].money += f * px * self.fx(n.nid, h)
        self.bonds.remove(b); return True

    # =============== 动作：v1.3 AI ===============
    def _phi_eff(self, n, c_raw):
        """04 号 (AR-025) 数据约束缩放律累计有效算力函数 Phi(C_raw)。"""
        if c_raw <= 0.0: return 0.0
        phi = min(1.0, n.d_eff() / max(20.0 * math.sqrt(c_raw), 1e-9)) ** 0.5
        return c_raw * phi

    def ai_train(self, n, cycles, alloc=None, max_price=None, base=True):
        """训练运行（需 AI02）：每周期累计训练算力；完成时按路径无关缩放律发布新模型版本。
        alloc：训练份额（默认沿用 ai_alloc['train']）；max_price：电价上限（本币/电），超过则本周期停转；
        base=True 以当前最佳模型为基座。"""
        if 'AI02' not in n.known or n.job is not None or cycles < 5: n.errors += 1; return False
        if alloc is not None:
            a = dict(n.ai_alloc); a['train'] = alloc; self.ai_set_alloc(n, **a)
        if base and n.models:
            best_eff = max(n.models)
            idx = n.models.index(best_eff)
            base_raw = n.models_raw[idx] if idx < len(n.models_raw) else best_eff
        else:
            best_eff = 0.0; base_raw = 0.0
        n.job = dict(left=int(cycles), run=0.0, max_price=max_price, base=best_eff, base_raw=base_raw)
        return True

    def ai_cancel(self, n):
        """04 号 (AR-025)：取消训练时按已投入算力的 50% 保存检查点模型。"""
        if n.job is None: return False
        if n.job.get('run', 0.0) > 0.0:
            cb = n.job['base']; cr_base = n.job.get('base_raw', cb); cr = 0.50 * n.job['run']
            c_raw_new = cr_base + cr
            dc_eff = max(0.0, self._phi_eff(n, c_raw_new) - self._phi_eff(n, cr_base))
            n.models.append(cb + dc_eff); n.models_raw.append(c_raw_new)
        n.job = None; return True

    def ai_set_alloc(self, n, **shares):
        """算力份额：train, align, research, production, finance, market, cyber；合计 ≤ 1。"""
        s = {k: max(0.0, float(v)) for k, v in shares.items() if k in ('train', 'align', 'research', 'production', 'finance', 'market', 'cyber', 'grid')}
        tot = sum(s.values())
        if tot > 1.0: s = {k: v / tot for k, v in s.items()}
        n.ai_alloc = s; return True

    def ai_offer(self, n, price_ru):
        """推理服务挂牌价（RU/单位·周期；数量 = 市场份额算力）。需 AI04。"""
        n.ai_offer_price = max(0.0, float(price_ru)); return True

    def export_control(self, n, target, on=True):
        if on: n.export_ctl.add(target)
        else: n.export_ctl.discard(target)
        return True


    # =============== 动作：v1.3 国家间撮合（实测修订：经济形态，无外交/运输） ===============
    def _blocked(self, seller, buyer):
        """出口管制：卖方对买方设了管制且掌握 C04 时，管制品、推理服务与授权都不能撮合。"""
        return buyer.nid in seller.export_ctl and 'C04' in seller.known

    def export_offer(self, n, good, qty, price_ru):
        """挂出口卖单：qty 从库存转入托管（覆盖同物品旧单，旧托管退回）；qty ≤ 0 撤单。"""
        key = (n.nid, good); old = self.goods_offers.pop(key, None)
        if old: n.inv[good] = n.inv.get(good, 0) + old[0]
        qty = min(float(qty), max(0.0, n.inv.get(good, 0)))
        if qty <= 1e-9 or price_ru <= 0 or good in ('electricity', 'research_points', 'compute'): return 0.0
        n.inv[good] -= qty; self.goods_offers[key] = [qty, float(price_ru)]; return qty

    def goods_board(self, good=None):
        """[(卖方 nid, good, 数量, RU 单价)]，按价格升序。"""
        out = [(k[0], k[1], v[0], v[1]) for k, v in self.goods_offers.items() if good is None or k[1] == good]
        return sorted(out, key=lambda t: (t[3], t[0]))

    def buy_from(self, n, seller, good, qty, max_price_ru=None):
        """按卖单价从他国买入（占本周期进口配额但不加价；受出口管制约束）。"""
        s = self.nations[seller]; off = self.goods_offers.get((seller, good))
        if s is n or off is None or (max_price_ru is not None and off[1] > max_price_ru): n.errors += 1; return 0.0
        if good in EXPORT_CONTROLLED and self._blocked(s, n): n.errors += 1; return 0.0
        px = off[1] / n.e
        qty = min(float(qty), off[0], self.import_room(n), max(0.0, n.money) / px)
        if qty <= 1e-9: return 0.0
        off[0] -= qty; n.imported += qty
        if off[0] <= 1e-9: self.goods_offers.pop((seller, good))
        ru = qty * off[1]
        n.money -= ru / n.e; s.money += ru / s.e; n.trade_ru -= ru; s.trade_ru += ru
        n.inv[good] = n.inv.get(good, 0) + qty; n.goods_in += ru; s.goods_out += ru; self.stats['goods_trade'] += ru
        n._good_net[good] = n._good_net.get(good, 0.0) - qty
        s._good_net[good] = s._good_net.get(good, 0.0) + qty
        return qty

    def ai_bid_service(self, n, qty, max_price_ru):
        """推理服务买单（持续到改单；qty ≤ 0 撤单）：买入的服务按卖方 Cap 计入本国研究部署（需 AI04）。"""
        n.ai_bid = (float(qty), float(max_price_ru)) if qty > 0 else None; return True

    def license_offer(self, n, kind, price_ru, target=None):
        """挂授权：kind='weights'（被授权方得 60% Cap 副本）或 'data'（被授权方 D_lic_from[seller] = max(prev, 50% D_cur)，计 80%）。
        price_ru ≤ 0 撤销；target 为 None 表示任何国家。"""
        if kind not in ('weights', 'data'): n.errors += 1; return False
        if price_ru <= 0: self.lic_offers.pop((n.nid, kind), None); return True
        self.lic_offers[(n.nid, kind)] = (float(price_ru), target); return True

    def license_buy(self, n, seller, kind, max_price_ru):
        """接受授权（每对国家每种授权 100 周期一次）；被授权方需 AI01。"""
        s = self.nations[seller]; off = self.lic_offers.get((seller, kind))
        if s is n or off is None or off[0] > max_price_ru or (off[1] is not None and off[1] != n.nid) \
                or self._blocked(s, n) or 'AI01' not in n.known or self.cycle < n.lic_cool.get((seller, kind), -1):
            n.errors += 1; return False
        if kind == 'weights' and (not s.models or s.ai_halt > 0): n.errors += 1; return False
        if kind == 'data' and s.d_cur <= 1: n.errors += 1; return False
        ru = off[0]
        if ru / n.e > n.money: n.errors += 1; return False
        n.money -= ru / n.e; s.money += ru / s.e; n.trade_ru -= ru; s.trade_ru += ru
        if kind == 'weights':
            s_best = max(s.models)
            c_eff = 1000.0 * ((1.0 + s_best / 1000.0) ** 0.6 - 1.0)
            n.models.append(c_eff); n.models_raw.append(c_eff)
        else:
            n.d_lic_from[seller] = max(n.d_lic_from.get(seller, 0.0), 0.5 * s.d_cur)
            n.d_lic = sum(n.d_lic_from.values())
        n.lic_cool[(seller, kind)] = self.cycle + 100; n.lic_in += ru; s.lic_out += ru; self.stats['licenses'] += 1
        return True

    # =============== 推进 ===============
    def step(self):
        self._weather()
        for n in self.nations:
            n.imported = 0.0; n._good_net.clear(); n._sink_cur.clear()
            n.bot.act(self, n)
        for n in self.nations:
            self._pre(n)
        for n in self.nations:
            self._dispatch(n)
        self._power_trade()
        for n in self.nations:
            self._produce(n)
        self._ai_market()
        for n in self.nations:
            self._ai(n)
        self._finance()
        for n in self.nations:
            self._forex(n)
            n.u_log.append(self.cycle_power(n))
        self.cycle += 1

    def _weather(self):
        r_ = self.rng_w; c = self.cycle
        season = 1 + 0.3 * math.sin(2 * math.pi * c / 100.0)
        for regs in self.wx:
            for w in regs:
                w['W'] = min(1.0, max(0.0, w['wbar'] + 0.5 * (w['W'] - w['wbar']) + 0.12 * r_.gauss(0, 1)))
                w['K'] = min(1.0, max(0.0, w['kbar'] + 0.4 * (w['K'] - w['kbar']) + 0.15 * r_.gauss(0, 1)))
                w['rain'] = 1.0 + 0.9 * (w['rain'] - 1.0) + 0.10 * r_.gauss(0, 1)
                w['Q'] = min(1.2, max(0.3, season * w['rain']))

    # ---------------- 人口、民生、负荷 ----------------
    def _pre(self, n):
        inv = n.inv; c = self.cycle
        need = n.pop * FOOD_PER_POP
        got = min(need, inv.get('food', 0)); inv['food'] = inv.get('food', 0) - got
        n._sink_cur['food'] = n._sink_cur.get('food', 0.0) + got
        if got < need:
            g2 = min((need - got) * 1.35, inv.get('grain', 0)); inv['grain'] = inv.get('grain', 0) - g2
            n._sink_cur['grain'] = n._sink_cur.get('grain', 0.0) + g2
            got += min(g2 / 1.35, 0.75 * need - got) if got < 0.75 * need else 0
        fr = got / need if need > 0 else 1.0
        n.food_ratio = fr
        for g_ in list(n.import_ema): n.import_ema[g_] *= 0.95          # 近 ~20 周期净进口（C06）
        # 入户民用品与药品（自动从库存消费）
        cg_need = CG_PER_POP * n.pop; cg = min(cg_need, max(0.0, inv.get('consumer_goods', 0)))
        inv['consumer_goods'] = inv.get('consumer_goods', 0) - cg; n.cg_ratio = cg / cg_need if cg_need else 0.0
        n._sink_cur['consumer_goods'] = n._sink_cur.get('consumer_goods', 0.0) + cg
        med_k = len(n.known & set(MED_NODES))
        if med_k:
            md_need = MED_PER_POP * n.pop; md = min(md_need, max(0.0, inv.get('medicine', 0)))
            inv['medicine'] = inv.get('medicine', 0) - md; n.med_ratio = md / md_need if md_need else 0.0
            n._sink_cur['medicine'] = n._sink_cur.get('medicine', 0.0) + md
        else: n.med_ratio = 0.0
        # 居民消费额（C20 消费税基）：口粮 + 民用品 + 药品按国内参考价
        n.consumption = (got * self.ref_price(n, 'food') + cg * self.ref_price(n, 'consumer_goods')
                         + (md if med_k else 0.0) * self.ref_price(n, 'medicine')) if need > 0 else 0.0
        # C11 住宅区维护：每 20 周期 cement 0.2/座，欠缴降低可用率（≥ 0.2）
        if c % 20 == 0 and n.housing:
            q = 0.2 * n.housing
            if inv.get('cement', 0) >= q:
                inv['cement'] -= q; n.house_avail = min(1.0, n.house_avail + 0.1)
                n._sink_cur['cement'] = n._sink_cur.get('cement', 0.0) + q
            else: n.house_avail = max(0.2, n.house_avail - 0.1)
        # 工期
        for lst, kind in ((n.housing_build, 0), (n.grid_build, 1)):
            if not lst: continue
            k = 0
            for i in range(len(lst)):
                lst[i] -= 1
                if lst[i] <= 0: k += 1
            if k:
                lst[:] = [x for x in lst if x > 0]
                if kind == 0: n.housing += k
                else: n.grid_cap += GRID_STEP * k
        # 人口：ΔP = P(b − d) + 耦合（污染、通胀、停电、支持度）
        F = 1.0 if fr >= 0.999 else (max(0.0, (fr - 0.8) / 0.2) if fr >= 0.8 else 0.0)
        famine = FAMINE0 * (0.8 - fr) / 0.8 if fr < 0.8 else 0.0
        hc = n.housing_cap()
        H = 1.0 if n.pop <= hc else max(0.0, 1 - 4 * (n.pop - hc) / max(hc, 1.0))
        D = 0.5 * (len(n.known & A_NODES) + len(n.known & F_NODES)) / AF_N + 0.5 * min(1.0, n.cg_ratio)
        M = med_k / 3.0 * n.med_ratio
        M *= 1 - 0.5 * n.b1_short                                   # C05 停电削弱医疗
        pi_ann = n.pi * 1e4                                          # 年化 %
        infl = max(0.7, 1 - 0.02 * max(0.0, pi_ann - 5))            # C12
        n.dev = D; n.med = M
        n.birth = BIRTH0 * F * H * (1 - 0.6 * D) * infl * (0.8 if n.support < 0.3 else 1.0)   # C32
        n.death = DEATH0 * (1 - 0.5 * M) * (1 + 0.5 * n.pollute) + famine                  # C22
        n.pop = max(0.0, n.pop + n.pop * (n.birth - n.death))
        labor_eff = (1.0 if fr >= 0.8 else 0.4 + 0.75 * fr) * (0.85 + 0.15 * min(1.0, n.support / 0.7))
        n._labor = max(0.0, n.labor() * labor_eff - HOUSE_WORK * n.housing)
        # 负荷（负荷带 B1 市政、B2 实验室/受控农业/数据工程、B3 工业与采掘、B4 AI 推理、B5 AI 训练）
        load = dict(B1=0.0, B2=0.0, B3=0.0, B4=0.0, B5=0.0)
        rdem = [0.0] * REGIONS
        gens = []; work = []; store_cap = store_pw = 0.0; store_eff = 0.85
        a = n.ai_alloc; tr_share = a.get('train', 0.0) if n.job is not None else 0.0
        if tr_share and n.job.get('max_price') is not None and n.elec_price > n.job['max_price']: tr_share = 0.0
        n._tr_share = tr_share
        for fi, f in enumerate(n.fac):
            if f.remaining > 0: f.remaining -= 1; continue
            eff = max(0.3, 1 - 0.1 * f.arrears); reg = fi % REGIONS
            for li, ln in enumerate(f.lines):
                if ln is None: continue
                rid = ln[0]; r = RECIPES[rid]
                if rid in GENS: gens.append((rid, r, eff, reg, (fi, li))); continue
                if rid in STORE_RECIPES:
                    e_, p_, ef_ = STORE_RECIPES[rid]; store_cap += e_ * eff; store_pw += p_ * eff; store_eff = ef_; continue
                md = MODES[ln[1]]
                p = r['power'] * md['power_factor']
                if rid in DC_RECIPES:
                    load['B4'] += p * (1 - tr_share); load['B5'] += p * tr_share; band = 'AI'
                elif rid in LAB_BAND or 'curated_data' in r['outputs']: load['B2'] += p; band = 'B2'
                else: load['B3'] += p; band = 'B3'
                rdem[reg] += p
                work.append((rid, r, md, eff, band, (fi, li)))
        mines = []
        for mi, m in enumerate(n.mines):
            if m.remaining > 0: m.remaining -= 1; continue
            out, pw, wk = EXTRACT[m.good][m.level]; mines.append((m, out, wk))
            load['B3'] += pw; rdem[mi % REGIONS] += pw
        res = RES_POWER * n.pop
        load['B1'] += res
        for k in range(REGIONS): rdem[k] += res / REGIONS
        n.store_cap = store_cap; n.store_pw = store_pw; n._store_eff = store_eff
        n.store_e = min(n.store_e, store_cap)
        n.load = load; n._rdem = rdem; n._gens = gens; n._work = work; n._mines = mines

    def ref_price(self, n, good):
        """国内参考价（本币）：近期净进口的物品按进口平价 1.6·RU/e（C06），否则按世界估值 RU/e。电按本国电价。"""
        if good == 'electricity': return n.elec_price
        if n.import_ema.get(good, 0.0) > 0.5: return VALUE.get(good, 1) * PRICE * 1.6 / n.e    # C06 进口平价
        return VALUE.get(good, 1) * PRICE                                                    # 国内自产：本币估值

    def _grid_ai(self, n):
        """C15：AI06 后部署项 grid，线损与强迫停运 ×(1 − min(0.3, 0.01·Cap·s_g))。"""
        if 'AI06' not in n.ai_ms or n.ai_halt > 0: return 1.0
        sg = min(1.0, n.compute_inf * n.ai_alloc.get('grid', 0.0) / max(0.01 * sum(n.load.values()), 1e-9))
        return 1 - min(0.3, 0.01 * n.cap * sg)

    # ---------------- 电力调度 ----------------
    def _plant_avail(self, n, wx):
        """各机组本周期在线情况（检修、强迫停运、核电冷启动）。返回 [(rid, reg, nameplate, kind, min_frac)]。"""
        out = []; rng = self.rng_out; c = self.cycle
        aif = self._grid_ai(n)                                       # C15 AI 电网调度
        for rid, r, eff, reg, pid in n._gens:
            kind, mn, fo, per, dur, cold = GEN_TABLE[rid]
            st = n.outage.get(pid, 0)
            if st > 0:
                n.outage[pid] = st - 1; continue
            if per and (c + (pid[0] * 37 + pid[1] * 11)) % per < dur: continue
            if rng.random() < fo * aif:
                n.outage[pid] = 3 + int(6 * rng.random()); continue
            if cold:
                cl = n.cold.get(pid, cold if c > 0 else 0)
                if cl > 0: n.cold[pid] = cl - 1; continue
                n.cold[pid] = 0
            out.append((rid, reg, r['outputs']['electricity'] * eff, kind, mn))
        return out

    def _dispatch(self, n):
        wx = self.wx[n.nid]
        plants = self._plant_avail(n, wx)
        loss = (GRID_LOSS_E07 if 'E07' in n.known else GRID_LOSS) * self._grid_ai(n)
        L = n.load; D_cyc = sum(L.values())
        mc_of = {}
        for rid in GENS:     # C06 进口平价：净进口燃料按 RU 进口价/e 计 MC
            r = RECIPES[rid]
            mc_of[rid] = sum(self.ref_price(n, g) * q for g, q in r['inputs'].items()) / r['outputs']['electricity'] \
                + (OM_REN if rid in RENEWABLE else OM_FUEL)
        served_tot = 0.0; gen_out = {}; spill_tot = 0.0; price_w = 0.0; band_got = dict.fromkeys(L, 0.0)
        need_tot = 0.0; spare = {}
        fuel_left = dict(n.inv)
        black = False
        hyd = sum(x[2] for x in plants if x[3] == 'hydro')       # 河流潜力：超建时全部水电同比摊薄来水
        hscale = min(1.0, n.hydro_pot / hyd) if hyd > 0 else 1.0
        for frac, day in SEG:
            # 可用出力（本段电量）
            av = []      # (mc, rid, reg, qty, must)
            for rid, reg, npl, kind, mn in plants:
                w = wx[reg]
                if kind == 'wind': cf = w['W']
                elif kind == 'solar': cf = (DAY_SUN * (1 - 0.7 * w['K']) * w['lat']) if day else 0.0
                elif kind == 'hybrid': cf = 0.5 * w['W'] + 0.5 * ((DAY_SUN * (1 - 0.7 * w['K']) * w['lat']) if day else 0.0)
                elif kind == 'hydro':
                    q = w['Q']; q = 1 + 0.3 * (q - 1) if 'A01' in n.known else q
                    cf = min(1.0, q) * hscale
                else: cf = 1.0
                qty = npl * cf * frac
                if qty <= 0: continue
                av.append([0.0 if kind in ('wind', 'solar', 'hybrid', 'hydro') else mc_of[rid], rid, reg, qty, mn * qty])
            # 燃料约束（同一燃料的全部机组按比例分享库存）
            need_f = {}
            for x in av:
                r = RECIPES[x[1]]
                for g, q in r['inputs'].items(): need_f[g] = need_f.get(g, 0.0) + x[3] * q / r['outputs']['electricity']
            for x in av:
                r = RECIPES[x[1]]
                for g, q in r['inputs'].items():
                    if need_f[g] > 1e-12: x[3] = min(x[3], x[3] * max(0.0, fuel_left.get(g, 0)) / need_f[g])
                x[4] = min(x[4], x[3])
            reg_av = [0.0] * REGIONS
            for x in av: reg_av[x[2]] += x[3]
            rdem = [d * frac for d in n._rdem]; D = D_cyc * frac
            surplus = sum(max(0.0, reg_av[k] - rdem[k]) for k in range(REGIONS))
            deficit = sum(max(0.0, rdem[k] - reg_av[k]) for k in range(REGIONS))
            moved = min(surplus, deficit, n.grid_cap * frac)
            local = sum(min(reg_av[k], rdem[k]) for k in range(REGIONS))
            n.grid_need = max(n.grid_need * 0.9, deficit / frac) if day else n.grid_need
            dmax = min(n.store_e, n.store_pw * frac)
            cap_grid = local + moved * (1 - loss)
            # 级联停电：可供 < 70% 市政负荷
            if cap_grid + dmax < 0.7 * L['B1'] * frac and L['B1'] > 0: black = True
            served = min(D, cap_grid + dmax)
            # 优先顺序：必须运行（风光水 + 核电稳燃）→ 储能（平均充电价/效率）→ 按边际成本
            gen_need = min(served, cap_grid)
            dis = max(0.0, served - cap_grid)
            # 机组组合：最低稳燃只对需要开机的机组生效（必须运行总量不超过本段需求）
            mr = sum(x[4] for x in av)
            if mr > gen_need + moved * loss and mr > 0:
                k_ = (gen_need + moved * loss) / mr
                for x in av: x[4] *= k_
            units = []
            for x in av:
                if x[4] > 0: units.append((0.0, x[1], x[4], x)); 
                if x[3] - x[4] > 0: units.append((x[0], x[1], x[3] - x[4], x))
            smc = n.store_cost / max(n._store_eff, 0.5)
            if dmax - dis > 0: units.append((smc, 'STORE', dmax - dis, None))
            units.sort(key=lambda u: (u[0], u[1]))
            left = gen_need + moved * loss; mc = 0.0; must = sum(x[4] for x in av)
            for u in units:
                if left <= 1e-9 and u[0] > 0: break
                q = min(u[2], max(0.0, left)) if u[0] > 0 or u[3] is None else u[2]
                left -= q
                if q > 1e-9: mc = u[0]
                if u[1] == 'STORE': dis += q; continue
                gen_out[u[1]] = gen_out.get(u[1], 0.0) + q
                u[3][3] -= q
                r = RECIPES[u[1]]
                for g, qq in r['inputs'].items(): fuel_left[g] = fuel_left.get(g, 0) - qq * q / r['outputs']['electricity']
            over = max(0.0, -left)            # 必须运行出力超过需求 → 充电或弃电
            ch = min(over, n.store_pw * frac, (n.store_cap - n.store_e) / max(n._store_eff, 0.5))
            if ch > 0:
                n.store_cost = (n.store_cost * n.store_e) / max(n.store_e + ch * n._store_eff, 1e-9)
            n.store_e = max(0.0, n.store_e - dis) + ch * n._store_eff
            spill_tot += over - ch
            # 剩余可调度出力（跨境卖电用）
            for x in av:
                if x[3] > 1e-6 and x[0] > 0:
                    spare[x[1]] = spare.get(x[1], 0.0) + x[3]
            served_tot += served; need_tot += D
            short_f = max(0.0, D - served) / D if D > 0 else 0.0
            price_w += (mc + (PRICE_CAP - mc) * min(1.0, 10 * short_f)) * D   # 段内按缺电比例逼近价格上限
        n._gen_out = gen_out; n._spare = sorted(((mc_of[r], r, q) for r, q in spare.items()), key=lambda t: (t[0], t[1]))
        n._served = served_tot; n._D = need_tot; n.spill = spill_tot
        n._black = black; n._mc_of = mc_of
        n.elec_price = price_w / need_tot if need_tot > 0 else mc_of.get('P070', 8.0)
        n.power_traded = 0.0; n.power_imported = 0.0
        disp = sum(x[2] for x in plants if x[3] not in ('wind', 'solar', 'hybrid'))
        n.grid_stress = (n.load.get('B4', 0) + n.load.get('B5', 0)) > 0.25 * max(disp, 1.0)

    def _power_trade(self):
        """市场耦合：双方开启耦合时，电从 RU 价低的一侧（空闲可调度出力）流向价高的一侧（缺电或贵机组），
        直到价差 ≤ 损耗成本或每对 60/周期上限；结算价为两侧 RU 价均值。"""
        cp = [n for n in self.nations if n.power_couple]
        if len(cp) < 2: return
        for b in sorted(cp, key=lambda n: (-n.elec_price * n.e, n.nid)):
            short = b._D - b._served
            for s in sorted(cp, key=lambda n: (n.elec_price * n.e, n.nid)):
                if s is b or not s._spare: continue
                cap = PTRADE_CAP
                while cap > 1e-6 and s._spare:
                    mcv, rid, q = s._spare[0]
                    ask_ru = mcv * s.e
                    bid_ru = (PRICE_CAP if short > 1e-6 else b.elec_price) * b.e
                    if ask_ru / (1 - PTRADE_LOSS) >= bid_ru: break
                    want = short / (1 - PTRADE_LOSS) if short > 1e-6 else 0.0
                    if want <= 1e-6:
                        # 替代买方的贵机组
                        disp = sum(v for r_, v in b._gen_out.items() if b._mc_of[r_] * b.e > ask_ru / (1 - PTRADE_LOSS))
                        want = disp / (1 - PTRADE_LOSS)
                        if want <= 1e-6: break
                    x = min(q, want, cap)
                    px = 0.5 * (ask_ru + bid_ru)
                    s._gen_out[rid] = s._gen_out.get(rid, 0.0) + x; cap -= x; s.power_traded += x; s.power_sold += x
                    s._spare[0] = (mcv, rid, q - x)
                    if q - x <= 1e-6: s._spare.pop(0)
                    got = x * (1 - PTRADE_LOSS)
                    if short > 1e-6:
                        b._served += got; short -= got
                    else:
                        left = got
                        for r_ in sorted(b._gen_out, key=lambda r_: -b._mc_of[r_]):
                            d = min(left, b._gen_out[r_]); b._gen_out[r_] -= d; left -= d
                            if left <= 1e-9: break
                    b.power_imported += got
                    b.money -= px * x / b.e; s.money += px * x / s.e; b.trade_ru -= px * x; s.trade_ru += px * x

    def _eta_sink_good(self, n, g):
        """07 号 §1.2 (AR-034/AR-043)：全品类 20 周期真实消纳均值 C_bar_20 = max(1.0, mean_20)，chi = Inv/C_bar_20，6..12 线性归零。"""
        if g not in W_CAT: return 1.0
        escrow = self.goods_offers[(n.nid, g)][0] if (n.nid, g) in self.goods_offers else 0.0
        inv_g = max(0.0, n.inv.get(g, 0.0)) + escrow
        hist = n._sink_hist.get(g)
        cbar20 = max(1.0, (sum(hist) / len(hist)) if hist else 1.0)
        chi = inv_g / cbar20
        if chi <= 6.0: return 1.0
        return max(0.0, 1.0 - (chi - 6.0) / 6.0)

    # ---------------- 生产 ----------------
    def _produce(self, n):
        inv = n.inv; c = self.cycle
        coal_burn = 0.0
        act_recipes = {}; act_modes = {}
        for rid, q in n._gen_out.items():
            r = RECIPES[rid]; s = q / r['outputs']['electricity']
            if s > 1e-9:
                act_recipes[rid] = act_recipes.get(rid, 0.0) + s
                act_modes['general'] = act_modes.get('general', 0.0) + 1.0
            for g, qq in r['inputs'].items():
                inv[g] = inv.get(g, 0) - qq * s
                n._sink_cur[g] = n._sink_cur.get(g, 0.0) + qq * s
                if g == 'coal': coal_burn += qq * s
        for f in n.fac:
            if f.remaining > 0: continue
            for ln in f.lines:
                if ln and ln[0] in STORE_RECIPES:
                    act_recipes[ln[0]] = act_recipes.get(ln[0], 0.0) + 1.0
        D = n._D; served = n._served
        if n._black and n.blackout == 0:
            n.blackout = 3; n.blackouts += 1; self.stats['blackouts'] += 1
            for pid in list(n.cold):
                if n.cold[pid] <= 0: n.cold[pid] = 10          # 运行中的核电跳闸后需 10 周期冷启动
            n.crisis = max(n.crisis, 10)                       # C03 严重黑网触发危机（10 周期内不叠加）
        if n.blackout > 0: n.support = max(0.0, n.support - 0.02)    # C04 黑网周期支持度 −20 mU
        # 切负荷：B1–B4 按玩家顺序，B5 最低
        band = {}
        left = served if n.blackout == 0 else 0.0
        for k in tuple(n.load_priority) + ('B5',):
            need = n.load.get(k, 0.0)
            got = min(need, left); left -= got
            band[k] = got / need if need > 1e-9 else 1.0
        if n.blackout > 0: n.blackout -= 1
        n.band_ratio = band
        n.power_ratio = served / D if D else 1.0
        dem24 = sum(n.load.get(k, 0) for k in ('B2', 'B3', 'B4'))
        n.shed_hist = n.shed_hist[1:] + [sum(n.load.get(k, 0) * (1 - band[k]) for k in ('B2', 'B3', 'B4')) / dem24 if dem24 > 1e-9 else 0.0]
        n.b1_short = 1 - band.get('B1', 1.0)
        n.shed = max(0.0, D - served)
        # 劳动
        need_w = sum(r['workers'] for _, r, _, _, _ in n._gens)
        need_w += sum(wk for _, _, wk in n._mines) + sum(r['workers'] * md['worker_factor'] for _, r, md, _, _, _ in n._work)
        prod_w = sum(r['workers'] * md['worker_factor'] for _, r, md, _, b_, _ in n._work if b_ == 'B3')
        aiw = min(n.ai_workers, (0.6 if 'AI10' in n.ai_ms else 0.5) * prod_w)
        labor = n._labor + aiw
        lr = min(1.0, labor / need_w) if need_w else 1.0
        n.labor_ratio = lr
        n.employed = min(n._labor, need_w)
        paid = 1 - aiw / prod_w if prod_w > 0 else 1.0          # C17：AI 替代的劳动不付工资
        wage = WAGE0 * n.wage_index
        sec_va = dict.fromkeys(SECTORS, 0.0); sec_pw = dict.fromkeys(SECTORS, 0.0); sec_w = dict.fromkeys(SECTORS, 0.0)
        emit = 0.0
        for rid, r, eff, reg, pid in n._gens:
            sec_w['energy'] += r['workers'] * lr * wage
        for rid, q in n._gen_out.items():
            emit += EMIT.get(rid, 0.0) * q / RECIPES[rid]['outputs']['electricity']
        k3 = min(band['B3'], lr)
        va_cat = 0.0
        for m, out, _ in n._mines:
            dep = n.deposits[m.good]
            take = out * k3
            if dep != math.inf:
                rho_d = dep / n.dep0[m.good]
                eta = 1.0 if rho_d >= 0.3 else (0.25 + 0.75 * rho_d / 0.3 if rho_d > 0 else 0.0)
                take = min(take * eta, dep); n.deposits[m.good] = dep - take
            inv[m.good] = inv.get(m.good, 0) + take
            sec_va['mining'] += 0.6 * VALUE.get(m.good, 1) * take; sec_pw['mining'] += EXTRACT[m.good][m.level][1] * k3
            sec_w['mining'] += EXTRACT[m.good][m.level][2] * lr * wage
            va_cat += 0.6 * W_CAT.get(m.good, 1.0) * self._eta_sink_good(n, m.good) * take
        added = 0.0; rp = []; out_buf = {}; compute_tr = compute_inf = 0.0; n_inst = 0
        for rid, r, m, eff, b_, pid in n._work:
            if rid in DC_RECIPES:
                h = n.dc_health.get(pid, 1.0)
                cmp_ = r['outputs']['compute'] * eff * h * lr
                compute_tr += cmp_ * n._tr_share * band['B5']; compute_inf += cmp_ * (1 - n._tr_share) * band['B4']
                n.dc_health[pid] = max(0.0, h - 1.0 / ACC_LIFE)
                sec_pw['tech'] += r['power'] * (n._tr_share * band['B5'] + (1 - n._tr_share) * band['B4'])
                if cmp_ * (n._tr_share * band['B5'] + (1 - n._tr_share) * band['B4']) > 1e-9:
                    act_recipes[rid] = act_recipes.get(rid, 0.0) + 1.0
                    act_modes[m['id']] = act_modes.get(m['id'], 0.0) + 1.0
                continue
            if m['id'] in ('automated', 'flexible'): n_inst += 1
            if 'research_points' in r['outputs'] and n.project is None:
                continue                                                # AR-037：未立项时实验室不空转产点
            s = m['capacity'] * min(band[b_], lr) * eff
            if rid in ('P028', 'P029'): s *= 1 - 0.2 * n.pollute            # C23 污染降低农业
            for g, q in r['inputs'].items():
                have = inv.get(g, 0)
                if have < q * s: s = have / q
            if 'curated_data' in r['outputs']:
                s = min(s, n.d_raw / max(r['outputs']['curated_data'], 1e-9))
                if 'AI04' in n.ai_ms: n.d_syn += r['outputs']['curated_data'] * s * min(0.5, 0.02 * n.cap)   # C14
            if s <= 1e-9: continue
            act_recipes[rid] = act_recipes.get(rid, 0.0) + s
            act_modes[m['id']] = act_modes.get(m['id'], 0.0) + 1.0
            va = 0.0; vc = 0.0
            for g, q in r['inputs'].items():
                inv[g] -= q * s; va -= VALUE.get(g, 1) * q * s
                vc -= W_CAT.get(g, 1.0) * q * s
                n._sink_cur[g] = n._sink_cur.get(g, 0.0) + q * s
                if g == 'coal': coal_burn += 0.5 * q * s
            for g, q in r['outputs'].items():
                if g == 'research_points': rp.append(q * s)
                elif g == 'curated_data':
                    n.d_raw -= q * s; n.d_cur += q * s
                else:
                    out_buf[g] = out_buf.get(g, 0) + q * s; va += VALUE.get(g, 1) * q * s
                    vc += W_CAT.get(g, 0.0) * self._eta_sink_good(n, g) * q * s
            added += va
            if 'research_points' not in r['outputs'] and 'curated_data' not in r['outputs']:
                va_cat += vc
            emit += EMIT.get(rid, 0.0) * s / max(m['capacity'], 1e-9)
            sc = SECTOR_OF_FAC.get(r['facility'], 'manufacturing')
            sec_w[sc] += r['workers'] * m['worker_factor'] * min(band[b_], lr) * eff * wage * (paid if b_ == 'B3' else 1.0)
            sec_va[sc] += va; sec_pw[sc] += r['power'] * m['power_factor'] * s / max(m['capacity'], 1e-9)
        # 能源公司：售电收入（已供电量 × 电价）− 燃料成本；其余行业按用电付电费（内部转移，不凭空造钱）
        sec_va['energy'] += (served * n.elec_price - sum((n._mc_of[r_] - (OM_REN if r_ in RENEWABLE else OM_FUEL)) * q
                                                          for r_, q in n._gen_out.items())) / PRICE
        n._res_bill = n.load.get('B1', 0.0) * band.get('B1', 1.0) * n.elec_price
        for g, q in out_buf.items(): inv[g] = inv.get(g, 0) + q
        if c and c % IND['maintenance_period_cycles'] == 0:
            for f in n.fac:
                mt = PROFILES[f.pid]['maintenance']
                if all(inv.get(g, 0) >= q for g, q in mt.items()):
                    for g, q in mt.items():
                        inv[g] -= q; n._sink_cur[g] = n._sink_cur.get(g, 0.0) + q
                    f.arrears = max(0, f.arrears - 1)
                else: f.arrears += 1
        n.value_log.append(added)
        n.va_cat_log.append(va_cat)
        n._active_recipes = act_recipes; n._active_modes = act_modes
        n._sec_va = sec_va; n._sec_pw = sec_pw; n._sec_w = sec_w; n._n_inst = n_inst
        n.compute_train = compute_tr; n.compute_inf = compute_inf; n.compute = compute_tr + compute_inf
        poll_decay = POLL_DECAY + (0.02 if 'V02' in n.known else 0.0)
        n.pollution = n.pollution * (1 - poll_decay) + emit          # C21 / V02
        n.pollute = n.pollution / (n.pollution + POLL_K)
        # C24：采掘储量寿命因子（按有限储量矿的加权平均寿命）
        if c % 20 == 0:
            rem = rate_ = 0.0
            for m in n.mines:
                dep = n.deposits[m.good]
                if dep != math.inf and m.remaining == 0: rate_ += EXTRACT[m.good][m.level][0]
            for gd, dep in n.deposits.items():
                if dep != math.inf and any(m.good == gd for m in n.mines): rem += dep
            n._life_fac = max(0.3, min(1.0, (rem / rate_ if rate_ > 0 else 1e9) / 200.0) ** 0.5)
        base_rp = RP_SCALE * (sum(rp) / len(rp)) * len(rp) ** n.alpha() if rp else 0.0
        n._base_rp = base_rp
        eff_rp = base_rp * n.ai_rp_mult
        n.rp_log.append(eff_rp)
        self._research(n, eff_rp)

    def _research(self, n, eff_rp):
        for item in list(n.background):
            tid, vleft = item
            tier = TIERS[NODES[tid]['tier']]
            if vleft is None:
                vg = tier['validation_good']; va = tier['validation_amount']
                if n.inv.get(vg, 0) >= va:
                    n.inv[vg] -= va; n._sink_cur[vg] = n._sink_cur.get(vg, 0.0) + va
                    item[1] = tier['validation_cycles']
            else:
                item[1] -= 1
                if item[1] <= 0: n.known.add(tid); n.background.remove(item)
        if n.project is None:
            n._rp_overflow = 0.0
            return
        tid, done, _ = n.project
        tier = TIERS[NODES[tid]['tier']]
        tot = done + eff_rp
        if tot >= tier['theory_points']:
            n._rp_overflow = 0.0
            n.background.append([tid, None]); n.project = None
        else:
            n._rp_overflow = 0.0
            n.project = [tid, tot, None]

    # ---------------- AI ----------------
    def cap_of(self, n, c_eff): return 10.0 * math.log10(1 + c_eff / 1000.0) * n.algo

    def _ai_market(self):
        """推理服务：NPC 需求 50(1 + c/720) 单位；保留价 0.5·Cap_s RU；按 价格/(Cap/10) 升序成交。"""
        self.ai_demand = 50.0 * (1 + self.cycle / 720.0)
        offers = []
        for n in self.nations:
            if 'AI04' not in n.ai_ms or n.ai_halt > 0 or not n.models: continue
            q = n.compute_inf * n.ai_alloc.get('market', 0.0) if n.compute_inf else 0.0
            if q <= 1e-6: continue
            p = n.ai_offer_price if n.ai_offer_price is not None else 0.4 * n.cap
            if p > 0.5 * n.cap: continue
            offers.append((p / max(n.cap / 10.0, 1e-6), (n.nid - self.cycle) % len(self.nations), n, q, p))
        offers.sort(key=lambda o: (o[0], o[1]))
        offers = [list(o) for o in offers]
        self.ai_sold = {}
        for n in self.nations: n.ai_bought = (0.0, 0.0); n.ai_spent = 0.0
        # 国家买单先撮合（按买方出价降序，平局座位轮换）：买方需 AI04，不与自己、不与管制方成交
        bids = [(-n.ai_bid[1], (n.nid - self.cycle) % len(self.nations), n) for n in self.nations
                if n.ai_bid and 'AI04' in n.ai_ms]
        for _, _, b in sorted(bids, key=lambda t: (t[0], t[1])):
            want, maxp = b.ai_bid; got = capw = spent = 0.0
            for o in offers:
                s = o[2]; p = o[4]
                if s is b or o[3] <= 1e-9 or p > maxp or self._blocked(s, b): continue
                x = min(o[3], want - got, max(0.0, b.money * b.e - spent) / max(p, 1e-9))
                if x <= 1e-9: continue
                o[3] -= x; got += x; capw += x * s.cap; spent += x * p
                q0, p0 = self.ai_sold.get(s.nid, (0.0, p)); self.ai_sold[s.nid] = (q0 + x, p)
                if got >= want - 1e-9: break
            if got > 0:
                b.ai_bought = (got, capw / got); b.ai_spent = spent; b.ai_buy_cum += spent
                b.money -= spent / b.e; b.trade_ru -= spent; self.stats['ai_trade'] += spent
        left = self.ai_demand
        for _, _, n, q, p in offers:
            if q <= 1e-9: continue
            x = min(q, left); left -= x
            q0, _p = self.ai_sold.get(n.nid, (0.0, p)); self.ai_sold[n.nid] = (q0 + x, p)
            if left <= 1e-9: break

    def _ai(self, n):
        a = n.ai_alloc
        n.d_raw = n.d_raw * (1 - AI_RAW_DECAY) + AI_RAW_POP * n.pop + AI_RAW_INST * getattr(n, '_n_inst', 0)
        if n.compute <= 0 and n.job is None and not n.models and n.ai_bought[0] <= 0:
            n.ai_rp_mult = 1.0; n.ai_workers = 0.0; n.ai_fee_cut = 0.0; n.ai_revenue = 0.0
            return
        halted = n.ai_halt > 0
        if halted: n.ai_halt -= 1
        inf = n.compute_inf; ninf = max(1e-9, 1 - n._tr_share)
        share = lambda k: a.get(k, 0.0) / ninf        # 推理份额在推理算力内的占比
        # 训练运行（路径无关缩放律 AR-025）
        if n.job is not None:
            n.job['run'] += n.compute_train; n.job['left'] -= 1
            if n.job['left'] <= 0:
                cb = n.job['base']; cr_base = n.job.get('base_raw', cb); cr = n.job['run']
                c_raw_new = cr_base + cr
                dc_eff = max(0.0, self._phi_eff(n, c_raw_new) - self._phi_eff(n, cr_base))
                n.models.append(cb + dc_eff); n.models_raw.append(c_raw_new); n.job = None
        best = max(n.models) if n.models else 0.0
        n.cap = self.cap_of(n, best)
        # 对齐（推理算力 + AI03）
        if 'AI03' in n.known:
            n.align_acc += inf * share('align')
        n.align = 10.0 * math.log10(1 + n.align_acc / 500.0) if 'AI03' in n.known else 0.0
        # 里程碑（按状态谓词判定，解锁后永久保留；单周期至多解锁 1 个算法突破里程碑 AR-025/AR-026）
        if 'AI02' in n.known:
            algo_unlocked_this_cycle = False
            for i, (mid, kind, thr) in enumerate(AI_MS):
                if mid in n.ai_ms: continue
                if kind in ('algorithm', 'frontier') and algo_unlocked_this_cycle: continue
                if (i == 0 or AI_MS[i - 1][0] in n.ai_ms) and n.cap >= thr:
                    n.ai_ms.add(mid)
                    if kind in ('algorithm', 'frontier'):
                        algo_unlocked_this_cycle = True
                        n.algo += 0.15; n.cap = self.cap_of(n, best)
                    if kind == 'frontier': n.ai_risk = 0.003
        # 部署
        if halted:
            n.ai_rp_mult = 1.0; n.ai_workers = 0.0; n.ai_fee_cut = 0.0; n.ai_revenue = 0.0
        else:
            if 'AI04' in n.ai_ms and n._base_rp > 0:
                own = inf * share('research'); bq, bcap = n.ai_bought         # 买入的推理服务按卖方 Cap 计
                tot = own + bq
                capw = (own * n.cap + bq * bcap) / tot if tot > 1e-9 else n.cap
                n.ai_rp_mult = 1.0 + 0.02 * capw * min(1.0, tot / (0.05 * n._base_rp))
            else: n.ai_rp_mult = 1.0
            n.ai_workers = 0.02 * n.cap * inf * share('production') if 'AI06' in n.ai_ms else 0.0
            n.ai_fee_cut = min(0.5, 0.015 * n.cap * min(1.0, inf * share('finance') / 20.0)) if 'AI07' in n.ai_ms else 0.0
            sold = self.ai_sold.get(n.nid)
            if sold:
                ru = sold[0] * sold[1]; n.ai_revenue = ru; n.money += ru / n.e; n.trade_ru += ru; n.ai_rev_cum += ru
            else: n.ai_revenue = 0.0
        # 失控事件（概率严格截断于 [0, 1]，金融闪崩仅扣现金不减股数 AR-026）
        p = min(1.0, max(0.0, n.ai_risk * max(0.0, n.cap - n.align)))
        if p > 0 and self.rng_ai.random() < p:
            kind = self.rng_ai.randrange(4); n.incidents += 1; n.incident_log.append((self.cycle, kind)); self.stats['incidents'] += 1
            if kind == 0: n.ai_halt = 10
            elif kind == 1:
                if n.job: n.job['run'] *= 0.9
                elif n.models:
                    i = n.models.index(max(n.models)); n.models[i] *= 0.9
                    if i < len(n.models_raw): n.models_raw[i] *= 0.9
            elif kind == 2:
                loss = 0.0
                for key in sorted(self.holdings):
                    q = self.holdings[key]
                    if key[0] == n.nid and q > 0:
                        c = self.companies[key[1]]
                        loss += 0.05 * q * c.price * self.fx(c.nid, n.nid)
                paid_loss = min(max(0.0, n.money), loss)
                n.money -= paid_loss
                rem_loss = loss - paid_loss
                if rem_loss > 1e-9:
                    ru_need = rem_loss * n.e
                    ru_paid = min(max(0.0, n.reserves), ru_need)
                    n.reserves -= ru_paid
                    rem_local = (ru_need - ru_paid) / max(n.e, 1e-9)
                    n.money -= rem_local
                n.fin_pnl -= loss
            else:
                others = [m for m in self.nations if m is not n]
                m = others[self.rng_ai.randrange(len(others))]
                if n.models:
                    n_best = max(n.models)
                    c_eff = 1000.0 * ((1.0 + n_best / 1000.0) ** 0.7 - 1.0)
                    m.models.append(c_eff); m.models_raw.append(c_eff)

    # ---------------- 金融 ----------------
    def _finance(self):
        nat = self.nations; h = self.holdings
        for n in nat:
            # 维护费用（本币）按设施首条产线的行业归集
            maint = dict.fromkeys(SECTORS, 0.0)
            for f in n.fac:
                ln = next((x for x in f.lines if x), None)
                if ln is None: continue
                v = sum(self.ref_price(n, g) * q for g, q in PROFILES[f.pid]['maintenance'].items()) / IND['maintenance_period_cycles']
                maint[SECTOR_OF_FAC.get(RECIPES[ln[0]]['facility'], 'manufacturing')] += v
            ptax = 0.0
            for s in SECTORS:
                c = self.company(n.nid, s)
                bill = 0.0 if s == 'energy' else n.elec_price * n._sec_pw.get(s, 0.0)            # C01
                va_net = n._sec_va.get(s, 0.0) * PRICE - bill
                w_cost = n._sec_w.get(s, 0.0)
                pi_cash = va_net - w_cost - maint[s]                                             # AR-007/AR-033 真实流转现金利润
                pi_val = (max(0.15 * va_net, va_net - w_cost) if va_net > 0 else va_net - w_cost) - maint[s]   # C17 估值基准利润
                tax = n.tax_rate * max(0.0, pi_cash); ptax += tax                                # C20 按现金利润征税
                net_cash = pi_cash - tax
                net_val = pi_val * (1.0 - n.tax_rate) if pi_val > 0 else pi_val
                c.profit = 0.95 * c.profit + 0.05 * net_val                                      # 税后估值利润 EMA（供 C08 公允值）
                div = c.payout * max(0.0, net_cash) if c.retained + net_cash > 0 else 0.0
                c.retained += net_cash - div                                                     # 留存与派息严格守恒于现金利润
                if div > 0:
                    ps = div / SHARES
                    n.money += ps * c.state
                    for key, q in list(h.items()):
                        if key[1] == c.cid and q:
                            hn = nat[key[0]]; v = ps * q * self.fx(c.nid, hn.nid)
                            hn.money += v; hn.div_in += v
            # C13 社会保障与再分配（F05 解锁后生效 AR-010/AR-033）
            soc_need = 0.05 * n.pop * min(3.0, n.cpi) if 'F05' in n.known else 0.0
            soc_paid = min(max(0.0, n.money), soc_need)
            n.money -= soc_paid
            n.soc_paid_ratio = (soc_paid / soc_need) if soc_need > 1e-9 else 1.0
            # C20 税收：τ·(工资总额 + 居民消费额) + τ·Σ max(0, Π_cash)；禁止人头税
            n.wages = sum(n._sec_w.values())
            cons = min(n.consumption, n.wages * (1 - n.tax_rate) + soc_paid)
            tax = n.tax_rate * (n.wages + cons) + ptax
            n.money += tax; n.tax_log.append(tax)
            if len(n.tax_log) > 40: del n.tax_log[0]
            if n.crisis > 0: n.crisis -= 1
        # 集合竞价
        self._cap_w = max([m.cap for m in nat if m.compute_inf * m.ai_alloc.get('finance', 0.0) >= 20] + [0.0])
        by_c = {}
        for o in self.orders: by_c.setdefault(o[1], []).append(o)
        self.orders = []
        for n in nat:
            if n.liquidate:
                for key, q in sorted(h.items()):
                    if key[0] == n.nid and abs(q) > 1e-9:
                        c = self.companies[key[1]]
                        by_c.setdefault(c.cid, []).append((n.nid, c.cid, -q, c.price * (0.01 if q > 0 else 100.0), True))
                self.stats['liquidations'] += 1; n.liquidate = False
        for c in self.companies:
            if not c.listed: continue
            self._auction(c, by_c.get(c.cid, []))
            c.avg20 = 0.95 * c.avg20 + 0.05 * c.price
        for n in nat:
            if n.margin_debt > 0: n.margin_debt *= 1 + n.rate / 10000.0
            longv = shortv = 0.0
            for key, q in h.items():
                if key[0] != n.nid or not q: continue
                c = self.companies[key[1]]; v = q * c.price * self.fx(c.nid, n.nid)
                if q > 0: longv += v
                else: shortv += -v
            if shortv > 0: n.money -= SHORT_FEE * shortv
            elif n.short_coll > 0: n.money += n.short_coll; n.short_coll = 0.0
            if n.margin_debt > 0 or shortv > 0:
                eq = longv - shortv + n.short_coll - n.margin_debt
                if eq < MARGIN_MAINT * (longv + shortv): n.liquidate = True
            if n.margin_debt > 0 and longv <= 1e-6 and n.money > n.margin_debt:
                n.money -= n.margin_debt; n.margin_debt = 0.0
        # 债券（违约重组 AR-018/AR-036：按发行国汇总全部当期应付本息，先按全体应付利息比例偿付票息、再按到期本金比例偿付到期本金，核减已偿本金、未付息资本化，顺序无关）
        for iss in nat:
            bs = [b for b in self.bonds if b.issuer == iss.nid]
            if not bs: continue
            items = []
            tot_coup = 0.0; tot_prin = 0.0
            for b in bs:
                due = self.cycle >= b.maturity
                coup = b.face * b.coupon / 10000.0
                prin = b.face if due else 0.0
                pay = coup + prin
                items.append((b, due, coup, prin, pay))
                tot_coup += coup; tot_prin += prin
            tot_pay = tot_coup + tot_prin
            if iss.money >= tot_pay - 1e-9:
                iss.money = max(0.0, iss.money - tot_pay)
                for b, due, coup, prin, pay in items:
                    if pay > 0 and b.face > 0:
                        for hd, f in b.holders.items():
                            if hd != 'npc' and f > 0: nat[hd].money += pay * (f / b.face) * self.fx(b.issuer, hd)
                    if due: self.bonds.remove(b)
            else:
                cash_used = max(0.0, iss.money); iss.money = 0.0
                shortfall_loc = max(0.0, tot_pay - cash_used)
                ru_needed = shortfall_loc * max(iss.e, 1e-9)
                ru_seized = min(max(0.0, iss.reserves), ru_needed)
                iss.reserves -= ru_seized
                tot_paid = cash_used + ru_seized / max(iss.e, 1e-9)
                coup_pool = min(tot_paid, tot_coup)
                prin_pool = max(0.0, tot_paid - coup_pool)
                for b, due, coup, prin, pay in items:
                    paid_coup_b = coup_pool * (coup / tot_coup) if tot_coup > 0 else 0.0
                    paid_prin_b = min(prin, prin_pool * (prin / tot_prin)) if (due and tot_prin > 0) else 0.0
                    paid_b = paid_coup_b + paid_prin_b
                    if paid_b > 0 and b.face > 0:
                        for hd, f in b.holders.items():
                            if hd != 'npc' and f > 0: nat[hd].money += paid_b * (f / b.face) * self.fx(b.issuer, hd)
                    coup_unpaid = max(0.0, coup - paid_coup_b)
                    rem_face = max(0.0, b.face - paid_prin_b) + coup_unpaid
                    if due and rem_face <= 1e-6:
                        self.bonds.remove(b)
                    elif b.face > 0:
                        scale = rem_face / b.face
                        for hd in list(b.holders): b.holders[hd] *= scale
                        b.face = rem_face
                if tot_paid < tot_pay - 1e-6 or ru_seized > 0:
                    iss.credit = max(0.0, iss.credit - 0.3); iss.defaults += 1; iss.last_default_cycle = self.cycle
                    self.stats['defaults'] += 1
                    iss._default_fx_shock = -0.05
                    iss.crisis = max(iss.crisis, 10); iss.support = max(0.0, iss.support - 0.1)
                    for bb in self.bonds:
                        if bb.issuer == iss.nid:
                            bb.maturity = max(bb.maturity, self.cycle + 50)
                            bb.coupon += 5.0
                    for s in SECTORS:
                        cc = self.company(iss.nid, s)
                        if cc.listed: cc.price *= 0.9
        for n in nat:
            n.credit = min(1.0, n.credit + 0.0005)

    def _npc_orders(self, c):
        r = self.rng_mkt; p = c.price; v = self.fair(c); fl = SHARES - c.state
        out = []
        gap = math.log(v / p)
        thr = max(0.02, 0.05 - 0.001 * self._cap_w)                 # C16 AI 提高市场效率
        qf = 0.03 * fl * max(-1.0, min(1.0, 3 * gap)) if abs(gap) > thr else 0.0
        if abs(qf) > 1: out.append((qf, v))
        mom = math.log(p / c.hist[0])
        qm = 0.01 * fl * math.tanh(10 * mom) * (1 - min(0.5, self._cap_w / 70.0))
        if abs(qm) > 1: out.append((qm, p * (1.02 if qm > 0 else 0.98)))
        qn = self.noise_next.get(c.cid)
        if qn is None: qn = r.gauss(0, 0.004 * fl)
        self.noise_next[c.cid] = r.gauss(0, 0.004 * fl)
        out.append((qn, p * (1 + (0.03 if qn > 0 else -0.03) * r.random())))
        for k in range(1, 6):
            out.append((0.004 * fl, p * (1 - 0.01 * k)))
            out.append((-min(0.004 * fl, max(0.0, c.npc) / 6), p * (1 + 0.01 * k)))
        return out

    def _auction(self, c, orders):
        nat = self.nations; h = self.holdings
        book = [(q, lim, None, False) for q, lim in self._npc_orders(c)]
        for nid, cid, q, lim, mg in orders:
            n = nat[nid]
            held = h.get((nid, cid), 0.0)
            cost_unit = lim * self.fx(c.nid, nid) * (1 + TRADE_FEE)
            if q > 0:
                q = min(q, max(0.0, n.money) / cost_unit / (MARGIN_INIT if mg else 1.0))
            elif -q > max(0.0, held):
                extra = -q - max(0.0, held) if mg else 0.0
                q = -(max(0.0, held) + min(extra, max(0.0, n.money) / (MARGIN_INIT * cost_unit)))
            if abs(q) > 1e-9: book.append((q, lim, nid, mg))
        buys = sorted([b for b in book if b[0] > 0], key=lambda b: -b[1])
        sells = sorted([s for s in book if s[0] < 0], key=lambda s: s[1])
        if not buys or not sells: return
        prices = sorted({b[1] for b in buys} | {s[1] for s in sells})
        best = None
        for px in prices:
            dq = sum(b[0] for b in buys if b[1] >= px); sq = sum(-s[0] for s in sells if s[1] <= px)
            key = (min(dq, sq), -abs(px - c.price))
            if best is None or key > best[0]: best = (key, px)
        (vol, _), px = best
        if vol <= 1e-9: return
        c.price = px; c.hist = c.hist[1:] + [px]; c.last_vol = vol
        self.stats['auction_vol'] += vol * px * nat[c.nid].e
        for side, lst in ((1, buys), (-1, sells)):
            left = vol
            for q, lim, nid, mg in lst:
                if left <= 1e-9: break
                if (side > 0 and lim < px) or (side < 0 and lim > px): continue
                x = min(abs(q), left); left -= x
                if nid is None:
                    if side > 0:
                        c.ext_paid_in_ru += x * px * nat[c.nid].e
                    elif c.npc > 1e-9:
                        c.ext_paid_in_ru = max(0.0, c.ext_paid_in_ru * max(0.0, c.npc - x) / c.npc)
                    c.npc += side * x; continue
                n = nat[nid]
                fx = self.fx(c.nid, nid)
                fee = TRADE_FEE * (1 - n.ai_fee_cut) + (nat[c.nid].capctl / 100.0 if nid != c.nid else 0.0)
                val = x * px * fx
                if nid != c.nid:
                    nat[c.nid].portfolio_in += side * val * n.e          # C31 资本账户（RU）
                    if side > 0: n.sec_bought += val * n.e
                held = h.get((nid, c.cid), 0.0)
                if side > 0:
                    cover = min(x, max(0.0, -held)) * px * fx
                    use = min(cover, n.short_coll); n.short_coll -= use
                    cost = val * (1 + fee) - use
                    cash = min(cost, max(0.0, n.money)); n.money -= cash
                    if cost > cash:
                        if mg: n.margin_debt += cost - cash
                        else: n.money -= cost - cash
                    h[(nid, c.cid)] = held + x
                else:
                    sell_long = min(x, max(0.0, held)); short = x - sell_long
                    n.money += sell_long * px * fx * (1 - fee)
                    if n.margin_debt > 0 and sell_long > 0:
                        rep = min(n.margin_debt, max(0.0, n.money)); n.margin_debt -= rep; n.money -= rep
                    if short > 0:
                        proceeds = short * px * fx
                        post = min(MARGIN_INIT * proceeds, max(0.0, n.money))
                        n.money -= post + proceeds * fee; n.short_coll += proceeds + post
                    h[(nid, c.cid)] = held - x
                n.fin_pnl -= val * fee
        c.npc = max(0.0, c.npc)

    # ---------------- 外汇、通胀、支持度 ----------------
    def _forex(self, n):
        nat = self.nations
        gdp_now = n.wages + PRICE * max(0.0, n.value_log[-1] if n.value_log else 0.0) * n.price_level + n.ai_revenue / max(n.e, 1e-9)
        n.gdp = 0.95 * n.gdp + 0.05 * max(gdp_now, 1.0)
        n.tb = 0.95 * n.tb + 0.05 * n.trade_ru; n.trade_ru = 0.0
        # C02 CPI 与通胀：π = EMA_0.1[0.5·(g_M − g_Y) + 0.5·clip(Δln CPI, ±0.02)]；C10 实际利率抑制
        #   原型：货币供应随实际产出被动扩张，g_M − g_Y 取央行发行率（实测修订）
        issue = n.printed / max(n.m2, 1.0); n.printed = 0.0
        cpi = sum(w * self.ref_price(n, g) for g, w in CPI_W)
        if n.cpi0 is None: n.cpi0 = cpi; n._cpi_prev = cpi
        dcpi = max(-0.02, min(0.02, math.log(max(cpi, 1e-9) / max(n._cpi_prev, 1e-9)))); n._cpi_prev = cpi
        n.cpi = cpi / n.cpi0
        pi_prev = n.pi
        c10 = max(-2e-4, min(2e-4, 2e-5 * (n.rate - 1e4 * pi_prev - 2)))   # C10 放进 EMA 输入（实测修订：直接逐周期相减会自激到截断上限）
        n.pi = 0.9 * n.pi + 0.1 * (0.5 * issue + 0.5 * dcpi - c10)
        n.price_level = n.cpi
        n.wage_index += 0.05 * (min(3.0, n.cpi) - n.wage_index)
        n.e_prev = n.e
        wts = [m.gdp * m.e for m in nat]; wt = sum(wts) or 1.0
        iw = sum(m.rate * w for m, w in zip(nat, wts)) / wt; piw = sum(m.pi * w for m, w in zip(nat, wts)) / wt
        n.pi_cum += n.pi - piw
        rate = n.rate if 'F02' in n.known else iw
        gdp_ru = n.gdp * n.e
        tb = max(-0.5, min(0.5, n.tb / max(gdp_ru, 1.0)))
        ki = FX_I * (0.5 if n.capctl > 0 else 1.0)
        d = FX_TB * tb + ki * n.credit * (rate - iw) / 10000.0 - FX_PI * (n.pi - piw) - FX_PPP * (math.log(n.e) + n.pi_cum + math.log(n.m2 / M2_0))   # C29；PPP 锚含货币发行
        d += (0.5 if n.capctl > 0 else 1.0) * max(-0.01, min(0.01, 0.01 * n.portfolio_in / max(gdp_ru, 1.0)))   # C31
        n.portfolio_in = 0.0
        if n.crisis > 0: d -= 0.01
        if getattr(n, '_default_fx_shock', 0.0):
            d += n._default_fx_shock; n._default_fx_shock = 0.0
        d += self.rng_fx.gauss(0, FX_SIG)
        cap = FX_CAP
        m_ru = max(n.m2 * n.e, 1.0)
        if n.peg_ban > 0: n.peg_ban -= 1
        if n.peg is not None:
            ru = (math.log(n.peg / n.e) - d) * m_ru
            if ru > n.reserves:
                n.peg = None; n.peg_ban = 20; ru = n.reserves; d -= 0.10; cap = FX_CAP_BREAK
                n.last_peg_break_cycle = self.cycle; self.stats['pegs_broken'] += 1
            n.fx_order += ru
        if n.fx_order:
            ru = n.fx_order; n.fx_order = 0.0
            if ru > 0:
                ru = min(ru, n.reserves); n.reserves -= ru; n.money += ru / n.e * (1 - FX_FEE)
            else:
                loc = min(-ru / n.e, max(0.0, n.money)); ru = -loc * n.e
                n.money -= loc; n.reserves += loc * n.e * (1 - FX_FEE)
            d += ru / m_ru
        d = max(-cap, min(cap, d))
        n.e = max(0.05, min(20.0, n.e * math.exp(d)))
        # 支持度：口粮、通胀、停电、切市政负荷、失业、违约、C13 社保
        unemp = 1 - n.employed / max(n._labor, 1.0)
        soc_adj = (0.01 if n.soc_paid_ratio >= 0.999 else -0.02 * (1.0 - n.soc_paid_ratio)) if 'F05' in n.known else 0.0
        tgt = 0.75 + soc_adj - 0.4 * max(0.0, 0.95 - n.food_ratio) - 0.02 * max(0.0, n.pi * 1e4 - 5) - 0.5 * n.pollute * 0.2 \
            - 0.3 * (1 - n.band_ratio.get('B1', 1.0)) - 0.2 * max(0.0, unemp - 0.3) - 0.05 * n.defaults
        n.support += 0.05 * (max(0.0, min(1.0, tgt)) - n.support)
        # P7 计分滑动窗口更新（AR-034/AR-035：确保 cycle_power / national_power 为 100% 纯只读函数）
        for g in W_CAT:
            s_cur = n._sink_cur.get(g, 0.0) + max(0.0, n._good_net.get(g, 0.0))
            hist = n._sink_hist.get(g)
            if hist is not None:
                hist.append(s_cur)
                if len(hist) > 20: del hist[0]
        n._good_net_hist.append(dict(n._good_net))
        if len(n._good_net_hist) > 20: del n._good_net_hist[0]
        svc_net_ru = (n.ai_revenue - n.ai_spent) + (n.power_traded - n.power_imported) * n.elec_price * n.e
        n._svc_net_hist.append(svc_net_ru)
        if len(n._svc_net_hist) > 20: del n._svc_net_hist[0]
        w_len = max(1, len(n._good_net_hist))
        good_keys = set()
        for d_g in n._good_net_hist: good_keys |= set(d_g)
        good_flow = sum(W_CAT.get(g, 1.0) * max(0.0, sum(d_g.get(g, 0.0) for d_g in n._good_net_hist)) / w_len for g in good_keys)
        svc_flow = max(0.0, sum(n._svc_net_hist)) / max(1, len(n._svc_net_hist)) / 10.0
        n.trade_abs_ema = 0.95 * n.trade_abs_ema + 0.05 * (good_flow + svc_flow)

    def run(self, cycles=CYCLES):
        t0 = time.perf_counter()
        for _ in range(cycles): self.step()
        return time.perf_counter() - t0

    def cycle_power(self, n):
        """07 号 §1 单周期五支柱综合国力 U_p(c) = 0.25 U_pop + 0.25 U_ind + 0.20 U_ctrl + 0.15 U_tech + 0.15 U_intl。
        严格纯只读函数（AR-034/AR-035/AR-043）：不修改任何状态或 EMA。"""
        # 1. U_pop：国民福祉与本土安全（25%，对齐 07 号 §1.1 与 data/scoring.json q_life / q_sec AR-002/AR-028/AR-043）
        psi = max(0.15, min(1.0, 0.50 + 0.50 * n.support))
        fr = max(0.0, min(1.0, n.food_ratio))
        qf = 0.80 * (fr / 0.80) ** 2 if fr < 0.80 else 0.80 + 0.20 * min(1.0, (fr - 0.80) / 0.20)
        s_cons = max(0.0, min(2.0, n.cg_ratio))
        s_med = max(0.0, min(2.0, n.med))
        s_util = max(0.0, min(1.0, 1.0 - n.b1_short))
        q_life = min(1.5, qf * (0.55 + 0.35 * math.sqrt(s_cons) + 0.25 * math.sqrt(s_med) + 0.15 * s_util))
        poll_coeff = 0.25 if 'V02' in n.known else 0.50
        q_sec = max(0.0, 1.0 - poll_coeff * n.pollute)
        u_pop = 100.0 * psi * (n.pop / 200.0) * q_life * q_sec
        # 2. U_ind：可持续净生产与 NFA 年金（25%，保留负增加值与负 NFA，禁止 max(0, ·) 吞负值 AR-034/AR-035）
        va_cat = n.va_cat_log[-1] if n.va_cat_log else 0.0
        nfa_ann = 0.01 * self.nfa_full(n) / 10.0
        u_ind = 0.35 * (va_cat + nfa_ann)
        # 3. U_ctrl：有效基础设施与资源控制（20%）
        fac_w = sum(GRADE_WEIGHT.get(f.pid, 1.0) * max(0.2, 1.0 - 0.1 * f.arrears) for f in n.fac if f.remaining == 0 and any(f.lines))
        mine_w = sum(1.5 if m.level == 'G2' else 1.0 for m in n.mines if m.remaining == 0 and n.deposits.get(m.good, 0) > 1e-6)
        infra_w = 2.0 * (n.grid_cap - GRID0) / 60.0 + 2.5 * n.store_cap / 80.0
        u_ctrl = (1.2 * fac_w + 1.5 * mine_w + infra_w) * n.power_ratio
        # 4. U_tech：已落地技术应用深度（15%，按 data/scoring.json active_scale_rules 9 类 kind 连续计量 AR-034/AR-043）
        act_r = getattr(n, '_active_recipes', {})
        act_m = getattr(n, '_active_modes', {})
        u_tech_sum = 0.0
        for t in sorted(n.known | n.ai_ms):
            if t in NODES:
                tw = SCORING_TIER_WEIGHT.get(NODES[t]['tier'], 1.0)
            elif t in SCORING_MS_WEIGHT:
                tw = SCORING_MS_WEIGHT[t]
            else:
                continue
            rule = TECH_ACTIVE_RULES.get(t)
            if not rule:
                ratio_t = 0.0
            else:
                kind = rule['kind']; refs = rule.get('ref_ids', []); th = max(1e-9, float(rule['theta']))
                if kind == 'active_recipe_batches':
                    sc = sum(act_r.get(rid, 0.0) for rid in refs)
                elif kind == 'active_mode_lines':
                    sc = sum(act_m.get(mid, 0.0) for mid in refs)
                elif kind == 'active_lab_lines':
                    sc = sum(1.0 for f in n.fac if f.remaining == 0 for ln in f.lines if ln and ln[0] in refs and act_r.get(ln[0], 0.0) > 1e-9)
                elif kind == 'active_grid_or_storage':
                    sc = max(0.0, (n.grid_cap - GRID0) / 60.0) + n.store_cap / 80.0 + sum(act_r.get(rid, 0.0) for rid in refs)
                elif kind == 'active_branch_installation':
                    if t == 'F05':
                        sc = 2.0 * n.soc_paid_ratio
                    elif t in ('V01', 'V02', 'V04'):
                        sc = sum(1.0 for m in n.mines if m.remaining == 0 and n.deposits.get(m.good, 0) > 1e-6) + sum(act_r.get(rid, 0.0) for rid in ('P093', 'P107'))
                    elif t in ('A01', 'A02', 'A03'):
                        sc = sum(act_r.get(rid, 0.0) for rid in ('P028', 'P029', 'P030', 'P112')) + sum(1.0 for m in n.mines if m.good in ('water', 'biomass') and m.remaining == 0)
                    elif t in ('C02', 'C04'):
                        sc = float(getattr(n, '_n_inst', 0)) + sum(act_r.get(rid, 0.0) for rid in ('P020', 'P021', 'P022'))
                    else:
                        sc = sum(act_r.get(rid, 0.0) for rid in refs) + sum(1.0 for m in n.mines if m.level == 'G2' and m.remaining == 0 and n.deposits.get(m.good, 0) > 1e-6)
                elif kind == 'active_logistics_or_s2plus':
                    sc = (sum(1.0 for f in n.fac if f.pid != 'S1' and f.remaining == 0 and any(f.lines))
                          + sum(1.0 for m in n.mines if m.level == 'G2' and m.remaining == 0 and n.deposits.get(m.good, 0) > 1e-6)
                          + sum(abs(v) for v in n._good_net.values()) / 10.0
                          + sum(act_r.get(rid, 0.0) for rid in refs))
                elif kind == 'active_military_or_orbital':
                    sc = sum(act_r.get(rid, 0.0) for rid in refs) + sum(act_r.get(rid, 0.0) for rid in ('P053', 'P054', 'P055', 'P056', 'P057', 'P082', 'P084', 'P085', 'P096', 'P098', 'P099', 'P100', 'P108', 'P115'))
                elif kind == 'active_finance_institution':
                    sc = (sum(1.0 for s in SECTORS if self.company(n.nid, s).listed)
                          + (1.0 if n.reserves > 0 else 0.0)
                          + (1.0 if self.debt(n) > 0 else 0.0)
                          + (1.0 if any(q != 0 for (nid_, _), q in self.holdings.items() if nid_ == n.nid) else 0.0))
                elif kind == 'active_compute_deployed':
                    sc = (n.compute + n.ai_bought[0]) / 10.0 + (n.d_cur / 200.0 if t == 'AI01' else 0.0) + (n.align_acc / 100.0 if t == 'AI03' else 0.0)
                else:
                    sc = sum(act_r.get(rid, 0.0) for rid in refs)
                ratio_t = min(1.0, max(0.0, sc / th))
            u_tech_sum += tw * (0.15 + 0.85 * ratio_t)
        u_tech = 0.60 * u_tech_sum
        # 5. U_intl：国际履约信用与外部耦合（15%，只读读取 P7 已更新的 20 周期净出口 EMA，系数 0.08 对齐 data/scoring.json AR-034/AR-035）
        u_intl = max(0.1, n.credit) * 0.08 * n.trade_abs_ema
        u_tot = 0.25 * u_pop + 0.25 * u_ind + 0.20 * u_ctrl + 0.15 * u_tech + 0.15 * u_intl
        arr = sum(f.arrears for f in n.fac) / max(1, len(n.fac))
        return (u_tot, u_pop, u_ind, u_ctrl, u_tech, u_intl, arr, n.food_ratio, n.power_ratio, n.support)

    def national_power(self, n):
        """07 号 §2 四纪元过程积分与终局可持续乘子 Φ_sustain（含带符号终局项 U_end_adj AR-003），纯只读函数。"""
        log = n.u_log
        if not log:
            c0 = self.cycle_power(n)
            log = [c0]
        T = len(log); q = max(1, T // 4)
        epochs = [log[0:q], log[q:2 * q], log[2 * q:3 * q], log[3 * q:]]
        u_ep = [sum(x[0] for x in ep) / max(1, len(ep)) for ep in epochs]
        tail = log[-min(40, T):]
        u_end = sum(x[0] for x in tail) / len(tail)
        d_maint = min(0.25, 0.15 * (sum(x[6] for x in tail) / len(tail)))
        fr_end = sum(x[7] for x in tail) / len(tail)
        pr_end = sum(x[8] for x in tail) / len(tail)
        sup_end = sum(x[9] for x in tail) / len(tail)
        d_short = min(0.25, 0.50 * max(0.0, 0.90 - fr_end) + 0.30 * max(0.0, 0.90 - pr_end))
        d_unrest = min(0.20, 0.40 * max(0.0, 0.50 - sup_end))
        d_insolv = min(0.35, min(0.25, 0.05 * n.defaults)
                       + (0.10 if self.cycle - n.last_default_cycle <= 40 else 0.0)
                       + (0.10 if self.cycle - n.last_peg_break_cycle <= 40 else 0.0))
        phi = max(0.35, 1.0 - d_maint - d_short - d_unrest - d_insolv)
        u_end_adj = max(u_end, 0.0) * phi + min(u_end, 0.0) * (2.0 - phi) - 10.0 * (1.0 - phi)
        V = 0.08 * u_ep[0] + 0.14 * u_ep[1] + 0.24 * u_ep[2] + 0.29 * u_ep[3] + 0.25 * u_end_adj
        pillars = [sum(x[k] for x in log) / T for k in range(1, 6)]
        return dict(V=V, U_I=u_ep[0], U_II=u_ep[1], U_III=u_ep[2], U_IV=u_ep[3], U_end=u_end, U_end_adj=u_end_adj, phi=phi,
                    pop=pillars[0], ind=pillars[1], ctrl=pillars[2], tech=pillars[3], intl=pillars[4])

    def power(self, n):
        tech = sum(TIER_WEIGHT[NODES[t]['tier']] for t in n.known)
        ind = sum(n.value_log[-100:]) / max(1, len(n.value_log[-100:]))
        stock = sum(VALUE.get(g, 1) * q for g, q in n.inv.items() if q > 0)
        np = self.national_power(n)
        return dict(tech=tech, industry=ind, stock=stock, pop=n.pop, V=np['V'], phi=np['phi'],
                    total=10 * tech + 10 * ind + 0.02 * (stock + n.money / PRICE) + 0.5 * n.pop)
