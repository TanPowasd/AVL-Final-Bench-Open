"""前沿实验室（Frontier Lab）对战引擎，只用 Python 标准库。

规则与协议见 README.md。

结构：
  world.py    —— 按种子生成隐藏世界（缩放律、数据、评测映射、全局事件序列）
  Game        —— 纯状态机：Game(seed).step([actA, actB]) -> [obsA, obsB]
  Bot         —— 子进程 bot（stdin/stdout 每 tick 一行 JSON，线程读写，兼容 Windows）
  play(...)   —— 跑一整局，返回结果 dict（含 replay）
"""
from __future__ import annotations

import json
import math
import os
import queue
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import world as W  # noqa: E402
from world import (DAY, DOMAINS, GPU, GPU_NAMES, LOT, MONTH, POOLS, QUARTER, REAL,  # noqa: E402
                   TICKS, WEEK, YEAR, hash_u)

# ----------------------------------------------------------------- 常量
START_CASH = 200.0            # M
START_GPUS = {"G1": 512}
START_MW = 2.0
KC = 300.0                    # 训练 N(B 参数) × D(B token) 需要 KC 个 G1·小时（含 6ND）
SK = 25.0                     # 服务 1 B token/tick、N(B) 参数的模型需要 SK × N 个 G1 算力
PUE0, PUE1 = 1.3, 1.15
MTBF = 50000.0
RESTART = 2
REPAIR = 72
CKPT_COST = 0.2
SLOW_P = 0.01                 # 每任务每天
REG_COMPUTE = 20e6            # 单次训练累计算力（G1·h）超过此值需审核
REVIEW = 2 * WEEK
RENT_HOURS = 8000.0           # 按需租价基准 = 卡价 / 8000 每卡·tick
SPOT_MULT = 0.4
RENT_POOL_MULT = 2.0          # 云租池容量 = 2 × 季度供给
SELL_RATE = {0: 0.65, 1: 0.50, 2: 0.30}
SELL_DELAY = 48
EXPAND_COST = 2.0
EXPAND_TICKS = 8 * WEEK
PPA_DISC = 0.8
HW_LIFE = TICKS
SITE_LIFE = 10 * YEAR
DEFAULT_LR_C = 0.0010         # 默认学习率 = 0.0010 × N^-0.3
DEFAULT_BATCH = 512
DEFAULT_CKPT = 24
COLLAPSE_AT, COLLAPSE_K = 0.30, 0.5
MIX_PENALTY = 0.95
CONT_K = 1.0
AUDIT_GAP, AUDIT_REP = 0.05, 0.3
DISTILL_CAP_FRAC = 0.10
DISTILL_SUE_FRAC, DISTILL_SUE_P = 0.20, 0.10
LAUNCH = 24
PRICE_DEFAULT = 10.0          # 美元 / 百万 token；收入(M) = B token × 价格 / 1000
PRICE_MIN, PRICE_MAX = 0.5, 100.0
U_PUB, U_PRICE, U_DROP = 4.0, 1.5, 5.0
MIG_MAX = 0.15
OPEN_PRICE = 3.0
VAL_REV_MULT = 20.0
VAL_LEAD = 300.0
END_GM_MULT = 12 * 8
END_C_MULT = 300.0
BANKRUPT_TICKS = 30 * DAY
MAX_RUNS = 12
MAX_CKPTS = 300
MAX_STAFF = 300
EXCL_TICKS = YEAR
VALUE_SCALE = 4846.39  # scoring_v2: frozen independent baseline calibration
STEP_TIMEOUT = 2.0
TIME_POOL = 120.0  # 时间池（秒）：独立于单 tick，单步可超出 STEP_TIMEOUT，超出部分从池扣；池空后回到单 tick 硬时限
TOTAL_BUDGET = 40 * 60.0
INIT_TIMEOUT = 10.0
MAX_OUTSTANDING = 50

TECHS = {
    "algo_eff_1": (20000, None), "algo_eff_2": (60000, "algo_eff_1"), "algo_eff_3": (150000, "algo_eff_2"),
    "stable": (30000, None), "curriculum": (40000, None),
    "infer_opt_1": (25000, None), "infer_opt_2": (70000, "infer_opt_1"),
    "synth": (35000, None), "early_supply": (50000, None), "cooling": (20000, None),
}
for _d in DOMAINS:
    TECHS["filter:" + _d] = (25000, None)
ALGO_MULT = {0: 1.0, 1: 1.15, 2: 1.3, 3: 1.5}

# 公共随机数标签
TAG = {k: i * 7919 + 13 for i, k in enumerate(
    ["fail", "spike", "div", "slow", "slowf", "noise", "poach", "copy", "sue", "preempt", "tie", "brk"])}


MAX_NUMBER = 1e15             # 动作里的数值上限；NaN、Infinity 和超限数值都按非法动作处理


def _numbers_ok(v):
    if isinstance(v, bool) or v is None or isinstance(v, str):
        return True
    if isinstance(v, (int, float)):
        return math.isfinite(v) and abs(v) <= MAX_NUMBER
    if isinstance(v, dict):
        return all(_numbers_ok(x) for x in v.values())
    if isinstance(v, list):
        return all(_numbers_ok(x) for x in v)
    return False


def _clip(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


# ================================================================= 训练任务
class Run:
    __slots__ = ("name", "ordinal", "N", "target", "lr", "batch", "sched", "mix", "req", "every", "status",
                 "D", "S", "Sq", "pen", "spike", "cont", "deff", "compute", "algo", "ckpts", "since",
                 "restart", "slow", "review", "approved", "granted", "eta", "created", "ncards", "tpt",
                 "eff", "next_id", "web_books", "distill_tok", "teacher")

    def __init__(self, name, ordinal, t):
        self.name = name
        self.ordinal = ordinal
        self.created = t
        self.D = 0.0
        self.S = {p: 0.0 for p in POOLS}
        self.Sq = {p: 0.0 for p in POOLS}
        self.pen = 0.0
        self.spike = 1.0
        self.cont = 0.0
        self.deff = 1.0
        self.compute = 0.0
        self.ckpts = []
        self.since = 0
        self.restart = 0
        self.slow = 1.0
        self.review = 0
        self.approved = False
        self.granted = {}
        self.ncards = 0
        self.tpt = 0.0
        self.eff = 0.0
        self.next_id = 1
        self.teacher = 0.0

    def snap(self, t):
        s = {"id": self.next_id, "tick": t, "D": self.D, "S": dict(self.S), "Sq": dict(self.Sq),
             "pen": self.pen, "spike": self.spike, "cont": self.cont, "deff": self.deff,
             "compute": self.compute, "teacher": self.teacher}
        self.next_id += 1
        self.ckpts.append(s)
        if len(self.ckpts) > MAX_CKPTS:
            del self.ckpts[0]
        return s

    def restore(self, s):
        self.D = s["D"]
        self.S = dict(s["S"])
        self.Sq = dict(s["Sq"])
        self.pen = s["pen"]
        self.spike = s["spike"]
        self.cont = s["cont"]
        self.deff = s["deff"]
        self.compute = s["compute"]
        self.teacher = s["teacher"]


# ================================================================= 玩家
class Player:
    def __init__(self, seat, w):
        self.seat = seat
        self.label = "AB"[seat]
        self.cash = START_CASH
        self.equity = 1.0
        self.rep = 0.0
        self.bankrupt = False
        self.neg = 0
        r0 = w["regions"][0]
        self.sites = [{"id": 0, "region": 0, "mw": START_MW, "ready": 0, "cards": dict.fromkeys(GPU_NAMES, 0),
                       "cost": 0.0, "built": 0, "ppa": [], "pend": []}]
        self.sites[0]["cards"]["G1"] = START_GPUS["G1"]
        self.region_cap = {0: r0["cap_mw"]}
        self.batches = [["G1", START_GPUS["G1"], GPU["G1"]["price"], 0]]
        self.inv = dict.fromkeys(GPU_NAMES, 0)
        self.broken = []                       # [fix_tick, model]
        self.broken_n = dict.fromkeys(GPU_NAMES, 0)
        self.deliv = []                        # [tick, model, n, unit_price]
        self.rentals = []                      # {id, m, n, kind, end}
        self.rent_id = 1
        self.recv = []                         # [tick, amount]
        self.staff = [{"id": i, "ab": s["ab"], "sal": s["sal"], "since": 0} for i, s in enumerate(w["staff0"])]
        self.staff_id = 100000 + 1000 * seat
        self.tech = set()
        self.pts = {}
        self.share_r = {}
        self.data = {p: [] for p in POOLS}    # [tokens, q, c, kind, until]
        for d in DOMAINS:
            self.data[d].append([w["public_b"][d], w["q"][d], w["c"][d], "public", None])
        self.U = {p: sum(x[0] for x in self.data[p]) for p in POOLS}
        self.qavg = {}
        self.cavg = {}
        self.runs = {}
        self.run_order = []
        self.run_count = 0
        self.models = []
        self.serve = None
        self.bids_gpu = {}
        self.bids_data = {}
        self.hires = {}
        self.poaches = []
        self.raised = set()
        self.share = 0.0
        self.wk_dem = 0.0
        self.wk_srv = 0.0
        self.drop = 0.0
        self.rev_ring = [0.0] * MONTH
        self.cost_ring = [0.0] * MONTH
        self.tot_ring = [0.0] * MONTH     # 全部营业收入（含蒸馏销售），融资估值用
        self.distill_in = 0.0             # 本 tick 被蒸馏收到的费用
        self.price_day = {}
        self.max_teacher = 0.0
        self.copy_tok = 0.0
        self.month_tok = 0.0
        self.pend_synth = []
        self.res = []
        self.ev = []
        self.faults = {"illegal": 0}
        self.tot = {"revenue": 0.0, "salary": 0.0, "elec": 0.0, "rent": 0.0, "gpu_buy": 0.0, "data": 0.0,
                    "sites": 0.0, "raised": 0.0, "fines": 0.0, "synth": 0.0, "distill": 0.0}
        self.refresh_data(w)

    @property
    def pue(self):
        return PUE1 if "cooling" in self.tech else PUE0

    @property
    def algo_level(self):
        return sum(1 for k in ("algo_eff_1", "algo_eff_2", "algo_eff_3") if k in self.tech)

    def refresh_data(self, w):
        for p in POOLS:
            rows = self.data[p]
            U = sum(x[0] for x in rows)
            self.U[p] = U
            if U > 0:
                self.qavg[p] = sum(x[0] * x[1] for x in rows) / U
                f = 0.5 if ("filter:" + p) in self.tech else 1.0
                self.cavg[p] = sum(x[0] * x[2] for x in rows) / U * f
            else:
                self.qavg[p] = 0.0
                self.cavg[p] = 0.0


# ================================================================= 游戏
class Game:
    def __init__(self, seed: int, ticks: int = TICKS, wld: dict | None = None):
        self.seed = seed
        self.ticks = ticks
        self.w = wld or W.generate(seed)
        self.t = -1
        self.players = [Player(0, self.w), Player(1, self.w)]
        self.key = {k: W.base_key(seed, "p|" + k) for k in TAG}
        self.npc_staff = [n["staff0"] for n in self.w["npcs"]]
        self.npc_gen = [0, 0]
        self.open_model = None
        self.npc_price_mult = 1.0
        self.shares = {"N1": 0.5, "N2": 0.5}
        self.models = {}                       # 全部已上线模型 id → dict
        self.pending_models = []
        self.cur_week = -1
        self.offer = {}
        self.open_deals = []
        self.open_talent = []
        self.pub_ev = []
        self.week_info = {}
        self.engine_time = 0.0
        self.replay = {"days": [], "events": []}
        self.curtailed = {}
        self.excl = []
        for i, n in enumerate(self.w["npcs"]):
            g = n["gens"][0]
            self.models[g["id"]] = {"id": g["id"], "owner": n["name"], "C": g["C"], "pub": g["C"],
                                    "price": self._npc_price(g["C"]), "live": 0, "N": None}
        self.flag = {"N1": "N1-1", "N2": "N2-1"}

    # ------------------------------------------------------------ 工具
    def _npc_price(self, c):
        return round(10.0 * (0.5 + c) * self.npc_price_mult, 3)

    def heat(self, t=None):
        t = self.t if t is None else t
        return self.w["heat"][min(len(self.w["heat"]) - 1, max(0, t) // WEEK)]

    def launched(self, m, t=None):
        t = self.t if t is None else t
        return t // QUARTER >= GPU[m]["launch_q"]

    def newest_gen(self):
        g = 0
        for m in GPU_NAMES:
            if self.launched(m):
                g = GPU[m]["gen"]
        return g

    def rent_u(self, m):
        pool = GPU[m]["supply_q"] * RENT_POOL_MULT
        bg = _clip(0.3 + 0.6 * (self.heat() - 0.75), 0.15, 0.85)
        mine = sum(r["n"] for p in self.players for r in p.rentals if r["m"] == m)
        return _clip(bg + mine / pool, 0.0, 1.0), bg, pool

    def rent_price(self, m, u=None):
        if u is None:
            u = self.rent_u(m)[0]
        return GPU[m]["price"] / RENT_HOURS * (1 + 2 * u)

    def eta_star(self, n):
        return self.w["eta_c"] * n ** -0.3

    def loss_of(self, p: Player, st, run: Run, noise_key=None):
        eff = {}
        for q in POOLS:
            s = st["S"][q]
            if s > 0:
                eff[q] = W.eff_tokens(s, st["Sq"][q], p.U[q])
        D = st["D"]
        extra = st["pen"] / D if D > 0 else 0.0
        if D > 0:
            fs = st["S"]["synth"] / D
            if fs > COLLAPSE_AT:
                extra += COLLAPSE_K * (fs - COLLAPSE_AT)
        n_eff = run.N * ALGO_MULT[run.algo]
        L = W.domain_losses(self.w, n_eff, eff, st["deff"], extra)
        prog = D / run.target if run.target > 0 else 0.0
        if run.sched == "cosine":
            bonus = 0.02 * min(1.0, prog) ** 2
        elif run.sched == "wsd":
            bonus = 0.03 * _clip((prog - 0.85) / 0.15, 0.0, 1.0)
        else:
            bonus = 0.0
        mult = st["spike"] * (1 - bonus)
        return [x * mult for x in L]

    def cap_of(self, p, st, run):
        L = self.loss_of(p, st, run)
        c = W.capability(self.w, L)
        D = st["D"]
        if D > 0 and st["S"]["distill"] / D >= DISTILL_CAP_FRAC and st["teacher"] > 0:
            c = min(c, st["teacher"] - 0.03)
        return max(0.0, c)

    # ------------------------------------------------------------ init
    def init_msg(self, seat):
        w = self.w
        p = self.players[seat]
        return {
            "type": "init", "seat": seat, "ticks": self.ticks,
            "rules": RULE_CONSTS,
            "gpus": [{"model": m, **{k: GPU[m][k] for k in ("power", "kw", "launch_q", "supply_q", "price")}}
                     for m in GPU_NAMES],
            "regions": [{k: r[k] for k in ("id", "price", "cap_mw", "build_weeks", "build_cost", "reg")}
                        for r in w["regions"]],
            "domains": [{"name": d, "public_b": w["public_b"][d], "q_hint": w["q_hint"][d]} for d in DOMAINS],
            "pools": POOLS, "real": REAL,
            "techs": {k: {"cost": v[0], "pre": v[1]} for k, v in TECHS.items()},
            "start": {"cash": START_CASH, "gpus": START_GPUS, "site": {"region": 0, "mw": START_MW},
                      "staff": [{"id": s["id"], "ab": s["ab"], "sal": s["sal"]} for s in p.staff]},
            "npcs": ["N1", "N2"],
            "models": [self._pub_model(m) for m in self.models.values()],
        }

    def _pub_model(self, m):
        return {"id": m["id"], "owner": m["owner"], "pub": round(m["pub"], 4), "price": m["price"],
                "N": m.get("N")}

    # ------------------------------------------------------------ 主循环
    def step(self, acts):
        t0 = time.perf_counter()
        self.t += 1
        t = self.t
        for p in self.players:
            p.res = []
            p.ev = []
        self.pub_ev = []
        self._events(t)
        for i, p in enumerate(self.players):
            if not p.bankrupt:
                self._apply(p, acts[i] if isinstance(acts[i], dict) else {}, t)
        if t % WEEK == 0:
            self._weekly(t)
        for p in self.players:
            self._arrivals(p, t)
        for p in self.players:
            self._train(p, t)
        self._api(t)
        for p in self.players:
            self._costs(p, t)
        if t % MONTH == 0 and t > 0:
            self._monthly(t)
        obs = [self._obs(i, t) for i in (0, 1)]
        if t % DAY == DAY - 1 or t == self.ticks - 1:
            self._snapshot(t)
        self.engine_time += time.perf_counter() - t0
        return obs

    # ------------------------------------------------------------ 事件
    def _events(self, t):
        w = self.w
        # NPC 新一代
        for i, n in enumerate(w["npcs"]):
            k = self.npc_gen[i]
            if k + 1 < len(n["gens"]) and n["gens"][k + 1]["tick"] == t:
                self.npc_gen[i] = k + 1
                g = n["gens"][k + 1]
                m = {"id": g["id"], "owner": n["name"], "C": g["C"], "pub": g["C"],
                     "price": self._npc_price(g["C"]), "live": t, "N": None}
                self.models[g["id"]] = m
                self.flag[n["name"]] = g["id"]
                self.pub_ev.append({"type": "release", "model": self._pub_model(m)})
        # 开源
        o = w["open"]
        if o and o["tick"] == t:
            npc = w["npcs"][o["npc"]]
            c = npc["gens"][self.npc_gen[o["npc"]]]["C"] - 0.05
            self.open_model = {"id": "OPEN-1", "owner": "OPEN", "C": c, "pub": c, "price": OPEN_PRICE,
                               "live": t, "N": None}
            self.models["OPEN-1"] = self.open_model
            self.flag["OPEN"] = "OPEN-1"
            self.shares["OPEN"] = 0.0
            self.npc_price_mult = 0.7
            for m in self.models.values():
                if m["owner"] in ("N1", "N2"):
                    m["price"] = self._npc_price(m["C"])
            self.pub_ev.append({"type": "open_source", "model": self._pub_model(self.open_model),
                                "from": npc["name"]})
        # 出口管制公告（提前 2 周）
        if w["export_q"] >= 0 and t == w["export_q"] * QUARTER - 2 * WEEK:
            self.pub_ev.append({"type": "export_ctrl", "model": "G3", "quarter": w["export_q"],
                                "start": w["export_q"] * QUARTER, "end": (w["export_q"] + 1) * QUARTER,
                                "supply_mult": 0.5})
        # 限电
        if t % WEEK == 0:
            for c in w["curtail"]:
                if c["start"] == t:
                    self.curtailed[c["region"]] = c["end"]
                    self.pub_ev.append({"type": "curtail", "region": c["region"], "start": t, "end": c["end"],
                                        "cap_mult": 0.7})
            for r in list(self.curtailed):
                if self.curtailed[r] <= t:
                    del self.curtailed[r]
        # 上线中的玩家模型
        for m in list(self.pending_models):
            if m["live"] <= t:
                self.pending_models.remove(m)
                self.models[m["id"]] = m
                self.pub_ev.append({"type": "release", "model": self._pub_model(m)})
        if self.pub_ev:
            for e in self.pub_ev:
                self.replay["events"].append({"t": t, **e})

    # ------------------------------------------------------------ 动作
    def _bad(self, p, what, why):
        p.faults["illegal"] += 1
        p.res.append({"act": what, "ok": False, "why": why})

    def _apply(self, p: Player, a: dict, t: int):
        w = self.w
        try:
            if not _numbers_ok(a):
                self._bad(p, "?", "non-finite or oversized number")
                return
            self._apply_inner(p, a, t, w)
        except Exception as e:  # 防御：坏动作不让引擎崩
            self._bad(p, "?", "exception: " + type(e).__name__)

    def _apply_inner(self, p, a, t, w):
        for b in a.get("bid_gpu") or []:
            m = b.get("model")
            if m not in GPU:
                self._bad(p, "bid_gpu", "unknown model")
                continue
            lots = int(b.get("lots", 0))
            price = float(b.get("price", 0))
            if lots <= 0:
                p.bids_gpu.pop(m, None)
            elif price > 0:
                p.bids_gpu[m] = (min(lots, 64), price)
        for r in a.get("rent") or []:
            m = r.get("model")
            n = int(r.get("n", 0))
            kind = r.get("kind", "ondemand")
            ticks = int(r.get("ticks", WEEK))
            if m not in GPU or not self.launched(m) or n <= 0 or kind not in ("ondemand", "spot") or ticks <= 0:
                self._bad(p, "rent", "bad rent")
                continue
            u, bg, pool = self.rent_u(m)
            room = int(pool * (1 - u))
            if room <= 0:
                self._bad(p, "rent", "sold out")
                continue
            n = min(n, room)
            p.rentals.append({"id": p.rent_id, "m": m, "n": n, "kind": kind, "end": t + ticks})
            p.res.append({"act": "rent", "ok": True, "id": p.rent_id, "model": m, "n": n, "kind": kind})
            p.rent_id += 1
        for r in a.get("end_rent") or []:
            p.rentals = [x for x in p.rentals if x["id"] != r]
        for s in a.get("sell_gpu") or []:
            m = s.get("model")
            n = int(s.get("n", 0))
            if m not in GPU or n <= 0:
                self._bad(p, "sell_gpu", "bad")
                continue
            have = sum(site["cards"][m] for site in p.sites) + p.inv[m] - p.broken_n[m]
            n = min(n, have)
            if n <= 0:
                self._bad(p, "sell_gpu", "none")
                continue
            behind = max(0, min(2, self.newest_gen() - GPU[m]["gen"]))
            amt = n * GPU[m]["price"] * SELL_RATE[behind]
            left = n
            take = min(left, p.inv[m])
            p.inv[m] -= take
            left -= take
            for site in reversed(p.sites):
                take = min(left, site["cards"][m])
                site["cards"][m] -= take
                left -= take
            self._remove_batches(p, m, n)
            p.recv.append([t + SELL_DELAY, amt])
            p.res.append({"act": "sell_gpu", "ok": True, "model": m, "n": n, "amount": round(amt, 3)})
        for b in a.get("build_site") or []:
            r = int(b.get("region", -1))
            if not 0 <= r < 5:
                self._bad(p, "build_site", "bad region")
                continue
            if len(p.sites) >= 8:
                self._bad(p, "build_site", "max 8 sites")
                continue
            reg = w["regions"][r]
            cost = reg["build_cost"]
            if p.cash < cost:
                self._bad(p, "build_site", "cash")
                continue
            p.cash -= cost
            p.tot["sites"] += cost
            sid = len(p.sites)
            p.sites.append({"id": sid, "region": r, "mw": reg["cap_mw"], "ready": t + reg["build_weeks"] * WEEK,
                            "cards": dict.fromkeys(GPU_NAMES, 0), "cost": cost, "built": t, "ppa": [], "pend": []})
            p.res.append({"act": "build_site", "ok": True, "site": sid, "ready": t + reg["build_weeks"] * WEEK})
        for e in a.get("expand_site") or []:
            sid = int(e.get("site", -1))
            mw = float(e.get("mw", 0))
            if not 0 <= sid < len(p.sites) or mw <= 0:
                self._bad(p, "expand_site", "bad")
                continue
            site = p.sites[sid]
            capmax = w["regions"][site["region"]]["cap_mw"]
            pend = sum(x[1] for x in site["pend"])
            mw = min(mw, capmax - site["mw"] - pend)
            if mw <= 0:
                self._bad(p, "expand_site", "region cap")
                continue
            cost = mw * EXPAND_COST
            if p.cash < cost:
                self._bad(p, "expand_site", "cash")
                continue
            p.cash -= cost
            p.tot["sites"] += cost
            site["cost"] += cost
            site["pend"].append([t + EXPAND_TICKS, mw])
            p.res.append({"act": "expand_site", "ok": True, "site": sid, "mw": mw, "ready": t + EXPAND_TICKS})
        for e in a.get("ppa") or []:
            sid = int(e.get("site", -1))
            mwh = float(e.get("mwh", 0))
            if not 0 <= sid < len(p.sites) or mwh <= 0:
                self._bad(p, "ppa", "bad")
                continue
            site = p.sites[sid]
            price = W.elec_price(w, site["region"], t) * PPA_DISC
            site["ppa"].append([t + YEAR, mwh / YEAR, price])
            p.res.append({"act": "ppa", "ok": True, "site": sid, "price": round(price, 7)})
        for h in a.get("hire") or []:
            cid = h.get("cand")
            sal = float(h.get("salary", 0))
            if cid is None or sal <= 0:
                self._bad(p, "hire", "bad")
                continue
            p.hires[int(cid)] = sal
        for pc in a.get("poach") or []:
            tgt = pc.get("target")
            if tgt not in ("opp", "N1", "N2"):
                self._bad(p, "poach", "bad target")
                continue
            p.poaches.append((tgt, int(pc.get("idx", 0)), float(pc.get("salary", 0))))
        if a.get("fire"):
            ids = set(int(x) for x in a["fire"])
            p.staff = [s for s in p.staff if s["id"] not in ids]
        if "research" in a and a["research"] is not None:
            sh = {}
            for r in a["research"]:
                k = r.get("tech")
                if k in TECHS:
                    sh[k] = max(0.0, float(r.get("share", 0)))
                else:
                    self._bad(p, "research", "unknown tech")
            p.share_r = sh
        for b in a.get("bid_data") or []:
            p.bids_data[int(b.get("deal", -1))] = float(b.get("price", 0))
        for s in a.get("synth") or []:
            self._synth(p, s, t)
        for s in a.get("distill") or []:
            self._distill(p, s, t)
        for r in a.get("run") or []:
            self._new_run(p, r, t)
        for c in a.get("run_ctl") or []:
            self._run_ctl(p, c, t)
        for r in a.get("audit_request") or []:
            run = p.runs.get(r.get("run"))
            if not run or run.approved or run.review > 0 or run.status in ("done", "stopped"):
                self._bad(p, "audit_request", "bad run")
                continue
            run.review = self._review_len(p)
            p.res.append({"act": "audit_request", "ok": True, "run": run.name, "until": t + run.review})
        for r in a.get("release") or []:
            self._release(p, r, t)
        for r in a.get("price") or []:
            mid = r.get("model")
            m = self.models.get(mid) or next((x for x in self.pending_models if x["id"] == mid), None)
            if not m or m["owner"] != p.label:
                self._bad(p, "price", "not your model")
                continue
            day = t // DAY
            if p.price_day.get(mid) == day:
                self._bad(p, "price", "once per day")
                continue
            p.price_day[mid] = day
            m["price"] = round(_clip(float(r.get("value", PRICE_DEFAULT)), PRICE_MIN, PRICE_MAX), 4)
        for mid in a.get("delist") or []:
            m = self.models.get(mid)
            if m and m["owner"] == p.label:
                m["delisted"] = True
        if "serve" in a:
            sv = a["serve"]
            if not sv:
                p.serve = None
            else:
                tot = dict.fromkeys(GPU_NAMES, 0)
                for s in sv:
                    for m, n in (s.get("gpus") or {}).items():
                        if m in GPU:
                            tot[m] += max(0, int(n))
                p.serve = tot
        for r in a.get("raise") or []:
            self._raise(p, float(r.get("amount", 0)), t)

    def _remove_batches(self, p, m, n):
        for b in p.batches:
            if b[0] == m and n > 0:
                k = min(n, b[1])
                b[1] -= k
                n -= k
        p.batches = [b for b in p.batches if b[1] > 0]

    def _best_released(self, p):
        best = 0.0
        for m in self.models.values():
            if m["owner"] == p.label:
                best = max(best, m["C"])
        return best

    def _synth(self, p, s, t):
        tok = float(s.get("tokens_b", 0))
        if "synth" not in p.tech or tok <= 0:
            self._bad(p, "synth", "need tech synth")
            return
        best = None
        for m in self.models.values():
            if m["owner"] == p.label and (best is None or m["C"] > best["C"]):
                best = m
        if best is None:
            self._bad(p, "synth", "need a released model")
            return
        cost = tok * best["N"] * KC / 3.0 * self.rent_price("G1")
        if p.cash < cost:
            self._bad(p, "synth", "cash")
            return
        p.cash -= cost
        p.tot["synth"] += cost
        p.pend_synth.append([t + DAY, tok, 0.6 + 0.6 * best["C"]])
        p.res.append({"act": "synth", "ok": True, "tokens_b": tok, "cost": round(cost, 4), "ready": t + DAY})

    def _distill(self, p, s, t):
        mid = s.get("model")
        tok = float(s.get("tokens_b", 0))
        m = self.models.get(mid)
        if not m or m["owner"] == p.label or tok <= 0 or m.get("delisted"):
            self._bad(p, "distill", "bad model")
            return
        cost = 0.0 if m["owner"] == "OPEN" else tok * m["price"] / 1000.0
        if p.cash < cost:
            self._bad(p, "distill", "cash")
            return
        p.cash -= cost
        p.tot["distill"] += cost
        if m["owner"] in ("A", "B"):
            o = self.players[0 if m["owner"] == "A" else 1]
            o.cash += cost
            o.tot["revenue"] += cost
            o.distill_in += cost
        p.data["distill"].append([tok, 0.5 + 0.8 * m["C"], 0.0, "distill:" + mid, None])
        p.max_teacher = max(p.max_teacher, m["C"])
        p.refresh_data(self.w)
        p.res.append({"act": "distill", "ok": True, "model": mid, "tokens_b": tok, "cost": round(cost, 4)})

    def _new_run(self, p, r, t):
        name = str(r.get("name", ""))[:32]
        if not name or name in p.runs:
            self._bad(p, "run", "name missing or used")
            return
        active = sum(1 for x in p.runs.values() if x.status not in ("done", "stopped"))
        if active >= MAX_RUNS or p.run_count >= 400:
            self._bad(p, "run", "too many runs")
            return
        try:
            N = float(r.get("params_b"))
            D = float(r.get("tokens_b"))
        except (TypeError, ValueError):
            self._bad(p, "run", "params_b/tokens_b")
            return
        if not (0.01 <= N <= 5000 and 0.01 <= D <= 1e6):
            self._bad(p, "run", "range")
            return
        run = Run(name, p.run_count, t)
        p.run_count += 1
        run.N, run.target = N, D
        run.lr = float(r.get("lr") or DEFAULT_LR_C * N ** -0.3)
        if run.lr <= 0:
            run.lr = DEFAULT_LR_C * N ** -0.3
        run.batch = max(1, int(r.get("batch") or DEFAULT_BATCH))
        run.sched = r.get("sched") if r.get("sched") in ("cosine", "wsd", "const") else "cosine"
        mix = r.get("mix")
        if not mix:
            mix = {d: 1.0 for d in DOMAINS}
        run.mix = self._norm_mix(mix)
        if run.mix is None:
            self._bad(p, "run", "bad mix")
            return
        g = r.get("gpus")
        run.req = {m: max(0, int(g.get(m, 0))) for m in GPU_NAMES} if isinstance(g, dict) else \
            dict.fromkeys(GPU_NAMES, 10 ** 9)
        run.every = max(1, int(r.get("ckpt_every") or DEFAULT_CKPT))
        run.algo = p.algo_level
        run.eta = self.eta_star(N)
        run.status = "paused" if r.get("paused") else "running"
        p.runs[name] = run
        p.run_order.append(name)
        p.res.append({"act": "run", "ok": True, "name": name, "lr": run.lr})

    def _review_len(self, p):
        big = max(p.sites, key=lambda s: (s["mw"], -s["id"]))
        return int(REVIEW * self.w["regions"][big["region"]]["reg"])

    def _norm_mix(self, mix):
        out = {}
        for k, v in mix.items():
            if k not in POOLS:
                return None
            out[k] = max(0.0, float(v))
        s = sum(out.values())
        if s <= 0:
            return None
        return {k: v / s for k, v in out.items() if v > 0}

    def _run_ctl(self, p, c, t):
        run = p.runs.get(c.get("name"))
        op = c.get("op")
        arg = c.get("arg")
        if not run:
            self._bad(p, "run_ctl", "no such run")
            return
        if op == "pause":
            if run.status in ("running",):
                run.status = "paused"
        elif op == "resume":
            if run.status == "paused":
                run.status = "running"
        elif op == "stop":
            run.status = "stopped"
        elif op == "rollback":
            if not run.ckpts:
                self._bad(p, "run_ctl", "no ckpt")
                return
            s = run.ckpts[-1]
            if arg is not None:
                s = next((x for x in run.ckpts if x["id"] == int(arg)), None)
                if s is None:
                    self._bad(p, "run_ctl", "no such ckpt")
                    return
            run.restore(s)
            run.since = 0
            run.restart = max(run.restart, 1)
            if run.status in ("diverged", "done"):
                run.status = "running"
            p.res.append({"act": "rollback", "ok": True, "run": run.name, "ckpt": s["id"], "D": round(run.D, 3)})
        elif op == "set_lr":
            v = float(arg)
            if v > 0:
                run.lr = v
        elif op == "set_mix":
            mix = self._norm_mix(arg or {})
            if mix is None:
                self._bad(p, "run_ctl", "bad mix")
                return
            run.mix = mix
            if "curriculum" not in p.tech:
                run.deff *= MIX_PENALTY
        elif op == "ckpt":
            run.snap(t)
            run.since = 0
            run.restart = max(run.restart, 0)
            p.res.append({"act": "ckpt", "ok": True, "run": run.name, "ckpt": run.ckpts[-1]["id"]})
        elif op == "diagnose":
            run.slow = 1.0
            run.restart = max(run.restart, 1)
        elif op == "set_gpus":
            if isinstance(arg, dict):
                run.req = {m: max(0, int(arg.get(m, 0))) for m in GPU_NAMES}
        elif op == "set_tokens":
            v = float(arg)
            if v > run.D:
                run.target = v
                if run.status == "done":
                    run.status = "running"
        elif op == "set_batch":
            run.batch = max(1, int(arg))
        elif op == "set_ckpt":
            run.every = max(1, int(arg))
        else:
            self._bad(p, "run_ctl", "bad op")

    def _release(self, p, r, t):
        run = p.runs.get(r.get("run"))
        if not run or not run.ckpts:
            self._bad(p, "release", "no run/ckpt")
            return
        s = run.ckpts[-1]
        if r.get("ckpt") is not None:
            s = next((x for x in run.ckpts if x["id"] == int(r["ckpt"])), None)
            if s is None:
                self._bad(p, "release", "no such ckpt")
                return
        if s["D"] <= 0:
            self._bad(p, "release", "empty ckpt")
            return
        n = sum(1 for m in list(self.models.values()) + self.pending_models if m["owner"] == p.label) + 1
        C = self.cap_of(p, s, run)
        bonus = CONT_K * s["cont"] / s["D"]
        mid = f"{p.label}{n}"
        m = {"id": mid, "owner": p.label, "C": C, "pub": C + bonus, "price":
             round(_clip(float(r.get("price") or PRICE_DEFAULT), PRICE_MIN, PRICE_MAX), 4),
             "live": t + LAUNCH, "N": run.N, "run": run.name, "ckpt": s["id"], "D": s["D"], "audited": False}
        self.pending_models.append(m)
        p.models.append(m)
        p.res.append({"act": "release", "ok": True, "model": mid, "live": t + LAUNCH})

    def _raise(self, p, amount, t):
        q = t // QUARTER
        if q in p.raised or amount <= 0:
            self._bad(p, "raise", "once per quarter")
            return
        rev30 = sum(p.tot_ring)
        own = max([m["pub"] for m in self.models.values() if m["owner"] == p.label] or [0.0])
        other = max([m["pub"] for m in self.models.values() if m["owner"] != p.label] or [0.0])
        V = VAL_REV_MULT * rev30 * 12 + VAL_LEAD * max(0.0, own - other) + p.cash
        if V <= 0:
            self._bad(p, "raise", "valuation <= 0")
            return
        amount = min(amount, V)
        p.raised.add(q)
        p.equity *= V / (V + amount)
        p.cash += amount
        p.tot["raised"] += amount
        e = {"type": "raise", "lab": p.label, "amount": round(amount, 2), "valuation": round(V, 2)}
        self.pub_ev.append(e)
        self.replay["events"].append({"t": t, **e})
        p.res.append({"act": "raise", "ok": True, "amount": round(amount, 2), "valuation": round(V, 2),
                      "equity": round(p.equity, 5)})

    # ------------------------------------------------------------ 每周结算
    def _weekly(self, t):
        w = self.w
        wk = t // WEEK
        info = {"week": wk, "heat": self.heat()}
        # 1. GPU 拍卖（上周放出的批次）
        res = {}
        for m, o in self.offer.items():
            res[m] = self._auction(m, o, t)
        info["auction"] = res
        for p in self.players:
            p.bids_gpu = {}
        # 2. 数据招标
        dres = []
        for d in self.open_deals:
            dres.append(self._deal(d, t))
        info["deal_results"] = dres
        for p in self.players:
            p.bids_data = {}
        # 3. 招聘 / 挖人
        info["hires"] = self._hire(t)
        self._poach(t)
        # 4. API 份额迁移
        self._shares(t)
        # 新一周的供给、合同、人才
        self.offer = w["auction"][wk] if wk < len(w["auction"]) else {}
        self.open_deals = w["deals"][wk] if wk < len(w["deals"]) else []
        self.open_talent = w["talent"][wk] if wk < len(w["talent"]) else []
        # 独家数据到期 → 公开
        for p in self.players:
            changed = False
            for pool in POOLS:
                for row in p.data[pool]:
                    if row[4] is not None and row[4] <= t:
                        row[4] = None
                        changed = True
            if changed:
                p.refresh_data(w)
        for d in self.excl:
            if d["until"] <= t and not d.get("public"):
                d["public"] = True
                for p in self.players:
                    if p.label != d["winner"]:
                        p.data[d["domain"]].append([d["tokens_b"], d["q"], d["c"], "deal-public", None])
                        p.refresh_data(w)
        # 蒸馏诉讼
        for i, p in enumerate(self.players):
            if p.bankrupt:
                continue
            frac = 0.0
            for name in p.run_order:
                r = p.runs[name]
                if r.status == "running" and r.D > 0:
                    frac = max(frac, r.S["distill"] / r.D)
            if frac > DISTILL_SUE_FRAC and hash_u(self.key["sue"], wk, 0) < DISTILL_SUE_P:
                fine = 0.15 * max(0.0, p.cash)
                p.cash -= fine
                p.tot["fines"] += fine
                p.rep = max(-3.0, p.rep - 0.2)
                p.ev.append({"type": "lawsuit", "kind": "distill", "fine": round(fine, 3)})
        self.week_info = info

    def _auction(self, m, o, t):
        lots = o["lots"]
        bids = []                               # (price, lots, who)
        for i, p in enumerate(self.players):
            if p.bankrupt or m not in p.bids_gpu:
                continue
            if o["early"] and "early_supply" not in p.tech:
                p.res.append({"act": "bid_gpu", "ok": False, "model": m, "why": "early supply needs tech"})
                continue
            nl, pr = p.bids_gpu[m]
            if pr < o["reserve"]:
                p.res.append({"act": "bid_gpu", "ok": False, "model": m, "why": "below reserve"})
                continue
            bids.append((pr, nl, p.label))
        for k, (nl, pr) in enumerate(o["npc"]):
            bids.append((pr, nl, "N%d" % (k + 1)))
        bids.sort(key=lambda x: -x[0])
        won = {}
        left = lots
        i = 0
        clear = o["reserve"]
        losing = None
        while i < len(bids):
            j = i
            while j < len(bids) and bids[j][0] == bids[i][0]:
                j += 1
            grp = bids[i:j]
            need = sum(b[1] for b in grp)
            if need <= left:
                for b in grp:
                    won[b[2]] = won.get(b[2], 0) + b[1]
                left -= need
            else:
                if left > 0:
                    each = left // len(grp)
                    for b in grp:
                        g = min(b[1], each)
                        if g:
                            won[b[2]] = won.get(b[2], 0) + g
                            left -= g
                losing = grp[0][0]
                left = 0
                # 同价分配后的余量不再分（保持对称）
                break
            i = j
        if losing is None and i < len(bids):
            losing = bids[i][0]
        if losing is not None:
            clear = max(clear, losing)
        winners = {}
        for i, p in enumerate(self.players):
            n = won.get(p.label, 0)
            if n <= 0:
                continue
            cost_unit = clear
            # 现金不足时按可负担批数缩减
            afford = int(max(0.0, p.cash) // (cost_unit * LOT))
            n2 = min(n, afford)
            if n2 < n:
                p.res.append({"act": "bid_gpu", "ok": False, "model": m, "why": "cash, lots cut", "won": n2})
            if n2 <= 0:
                continue
            cost = n2 * LOT * cost_unit
            p.cash -= cost
            p.tot["gpu_buy"] += cost
            p.deliv.append([t + o["delay"] * WEEK, m, n2 * LOT, cost_unit])
            winners[p.label] = n2
            p.res.append({"act": "bid_gpu", "ok": True, "model": m, "lots": n2, "price": round(clear, 5),
                          "arrive": t + o["delay"] * WEEK})
        for k in won:
            if k.startswith("N"):
                winners[k] = won[k]
        return {"lots": lots, "clear": round(clear, 5), "winners": winners, "delay_weeks": o["delay"]}

    def _deal(self, d, t):
        bids = []
        for p in self.players:
            pr = p.bids_data.get(d["id"])
            if pr is not None and pr >= d["reserve"] and not p.bankrupt and p.cash >= pr:
                bids.append((pr, p.label))
        for k, pr in enumerate(d["npc"]):
            bids.append((pr, "N%d" % (k + 1)))
        if not bids:
            return {"deal": d["id"], "winner": None}
        bids.sort(key=lambda x: -x[0])
        if len(bids) > 1 and bids[0][0] == bids[1][0]:
            return {"deal": d["id"], "winner": None, "why": "tie"}
        pr, who = bids[0]
        if who in ("A", "B"):
            p = self.players[0 if who == "A" else 1]
            p.cash -= pr
            p.tot["data"] += pr
            p.data[d["domain"]].append([d["tokens_b"], d["q"], d["c"], "deal%d" % d["id"], t + EXCL_TICKS])
            p.refresh_data(self.w)
            p.res.append({"act": "bid_data", "ok": True, "deal": d["id"], "price": pr})
            self.excl.append({**d, "winner": who, "until": t + EXCL_TICKS})
        return {"deal": d["id"], "winner": who, "price": round(pr, 3)}

    def _hire(self, t):
        out = []
        for c in self.open_talent:
            offers = []
            for p in self.players:
                s = p.hires.get(c["id"])
                if s is not None and s >= c["ask"] and not p.bankrupt and len(p.staff) < MAX_STAFF:
                    offers.append((s, p.label))
            if c["npc"] > 0:
                offers.append((c["ask"] * c["npc"], "N" + str(1 + (c["id"] % 2))))
            if not offers:
                continue
            offers.sort(key=lambda x: -x[0])
            if len(offers) > 1 and offers[0][0] == offers[1][0]:
                out.append({"cand": c["id"], "winner": None})
                continue
            s, who = offers[0]
            if who in ("A", "B"):
                p = self.players[0 if who == "A" else 1]
                p.staff.append({"id": p.staff_id, "ab": c["ab"], "sal": s, "since": t})
                p.res.append({"act": "hire", "ok": True, "cand": c["id"], "id": p.staff_id, "ab": c["ab"],
                              "salary": s})
                p.staff_id += 1
            else:
                self.npc_staff[int(who[1]) - 1] += 1
            out.append({"cand": c["id"], "winner": who})
        for p in self.players:
            for cid in p.hires:
                if not any(o.get("cand") == cid and o.get("winner") == p.label for o in out):
                    if any(c["id"] == cid for c in self.open_talent):
                        p.res.append({"act": "hire", "ok": False, "cand": cid})
            p.hires = {}
        return out

    def _poach(self, t):
        wk = t // WEEK
        moves = []
        for i, p in enumerate(self.players):
            for k, (tgt, idx, sal) in enumerate(p.poaches[:5]):
                u = hash_u(self.key["poach"], wk, k)
                if tgt == "opp":
                    o = self.players[1 - i]
                    if not 0 <= idx < len(o.staff):
                        continue
                    s = o.staff[idx]
                    loyal = 0.5 + (t - s["since"]) / YEAR
                    pr = W._sigmoid(3 * (sal / s["sal"] - 1.3) - loyal)
                    if u < pr:
                        moves.append((i, 1 - i, s["id"], sal))
                else:
                    j = int(tgt[1]) - 1
                    if self.npc_staff[j] <= 0:
                        continue
                    pr = W._sigmoid(3 * (sal / 0.0003 - 1.3) - 1.0)
                    if u < pr:
                        moves.append((i, None, j, sal))
            p.poaches = []
        for i, frm, sid, sal in moves:
            p = self.players[i]
            if frm is None:
                if self.npc_staff[sid] <= 0:
                    continue
                self.npc_staff[sid] -= 1
                ab = 1 + int(hash_u(self.key["poach"], wk, 99, sid) * 5)
                p.staff.append({"id": p.staff_id, "ab": ab, "sal": sal, "since": t})
                p.res.append({"act": "poach", "ok": True, "from": "N%d" % (sid + 1), "id": p.staff_id, "ab": ab})
                p.staff_id += 1
                continue
            o = self.players[frm]
            s = next((x for x in o.staff if x["id"] == sid), None)
            if s is None:
                continue
            o.staff.remove(s)
            o.ev.append({"type": "poached", "id": sid, "ab": s["ab"]})
            p.staff.append({"id": p.staff_id, "ab": s["ab"], "sal": sal, "since": t})
            p.res.append({"act": "poach", "ok": True, "from": "opp", "id": p.staff_id, "ab": s["ab"]})
            p.staff_id += 1

    def _flagship(self, lab):
        best, bu = None, -1e9
        for m in self.models.values():
            if m["owner"] != lab or m.get("delisted"):
                continue
            u = U_PUB * m["pub"] - U_PRICE * math.log(m["price"])
            if u > bu:
                best, bu = m, u
        return best, bu

    def _shares(self, t):
        labs = ["A", "B", "N1", "N2"] + (["OPEN"] if self.open_model else [])
        util = {}
        for lab in labs:
            m, u = self._flagship(lab)
            if m is None:
                continue
            if lab in ("A", "B"):
                p = self.players[0 if lab == "A" else 1]
                u += p.rep - U_DROP * p.drop
            util[lab] = u
        if not util:
            return
        mx = max(util.values())
        ex = {k: math.exp(v - mx) for k, v in util.items()}
        z = sum(ex.values())
        target = {k: ex.get(k, 0.0) / z for k in labs}
        new = {}
        for k in labs:
            cur = self.shares.get(k, 0.0)
            new[k] = cur + _clip(target[k] - cur, -MIG_MAX, MIG_MAX)
        s = sum(new.values())
        self.shares = {k: v / s for k, v in new.items()}
        for p in self.players:
            if p.wk_dem > 0:
                p.drop = 1 - p.wk_srv / p.wk_dem
                if p.drop > 0.001:
                    p.rep = max(-3.0, p.rep - min(0.1, p.drop))
                else:
                    p.rep = min(0.0, p.rep + 0.01)
            else:
                p.drop = 0.0
                p.rep = min(0.0, p.rep + 0.01)
            p.wk_dem = p.wk_srv = 0.0
            p.share = self.shares.get(p.label, 0.0)

    # ------------------------------------------------------------ 到货、建设、维修
    def _arrivals(self, p, t):
        changed = False
        if p.deliv:
            keep = []
            for d in p.deliv:
                if d[0] <= t:
                    p.inv[d[1]] += d[2]
                    p.batches.append([d[1], d[2], d[3], t])
                    p.ev.append({"type": "delivered", "model": d[1], "n": d[2]})
                    changed = True
                else:
                    keep.append(d)
            p.deliv = keep
        for site in p.sites:
            if site["pend"]:
                for e in list(site["pend"]):
                    if e[0] <= t:
                        site["mw"] += e[1]
                        site["pend"].remove(e)
                        p.ev.append({"type": "expanded", "site": site["id"], "mw": site["mw"]})
                        changed = True
            if site["ready"] == t and t > 0:
                p.ev.append({"type": "site_ready", "site": site["id"]})
                changed = True
        if p.broken and p.broken[0][0] <= t:
            while p.broken and p.broken[0][0] <= t:
                _, m = p.broken.pop(0)
                p.broken_n[m] -= 1
        if p.recv:
            keep = []
            for r in p.recv:
                if r[0] <= t:
                    p.cash += r[1]
                else:
                    keep.append(r)
            p.recv = keep
        if p.pend_synth:
            keep = []
            for s in p.pend_synth:
                if s[0] <= t:
                    p.data["synth"].append([s[1], s[2], 0.0, "generated", None])
                    p.refresh_data(self.w)
                else:
                    keep.append(s)
            p.pend_synth = keep
        if changed or (t % WEEK == 1 and any(p.inv.values())):
            self._install(p, t)
        if p.rentals:
            p.rentals = [r for r in p.rentals if r["end"] > t]

    def _install(self, p, t):
        pue = p.pue
        for m in GPU_NAMES:
            if p.inv[m] <= 0:
                continue
            kw = GPU[m]["kw"] * pue
            for site in p.sites:
                if site["ready"] > t:
                    continue
                used = sum(site["cards"][x] * GPU[x]["kw"] * pue for x in GPU_NAMES)
                room = int((site["mw"] * 1000 - used) // kw)
                if room <= 0:
                    continue
                k = min(room, p.inv[m])
                site["cards"][m] += k
                p.inv[m] -= k
                if p.inv[m] <= 0:
                    break

    # ------------------------------------------------------------ 训练
    def _usable(self, p, t):
        pue = p.pue
        own = dict.fromkeys(GPU_NAMES, 0)
        site_use = []
        for site in p.sites:
            if site["ready"] > t:
                site_use.append(None)
                continue
            cap = site["mw"] * 1000 * (0.7 if site["region"] in self.curtailed else 1.0)
            inst = sum(site["cards"][m] * GPU[m]["kw"] * pue for m in GPU_NAMES)
            f = 1.0 if inst <= cap else cap / inst
            for m in GPU_NAMES:
                own[m] += int(site["cards"][m] * f)
            site_use.append(f)
        for m in GPU_NAMES:
            own[m] = max(0, own[m] - p.broken_n[m])
        rented = dict.fromkeys(GPU_NAMES, 0)
        for r in p.rentals:
            rented[r["m"]] += r["n"]
        return own, rented

    def _train(self, p, t):
        own, rented = self._usable(p, t)
        avail = {m: own[m] + rented[m] for m in GPU_NAMES}
        p._own, p._rented = own, rented
        if p.bankrupt:
            p._used = dict.fromkeys(GPU_NAMES, 0)
            p._serve_cards = dict.fromkeys(GPU_NAMES, 0)
            for r in p.runs.values():
                r.granted = {}
                r.ncards = 0
            return
        left = dict(avail)
        serve = dict.fromkeys(GPU_NAMES, 0)
        if p.serve:
            for m in GPU_NAMES:
                k = min(p.serve.get(m, 0), left[m])
                serve[m] = k
                left[m] -= k
        # 预检查抢占：谁被抢就中断其中优先级最低、用到该型号的任务
        preempted = set()
        if p.rentals:
            keep = []
            for k, r in enumerate(p.rentals):
                if r["kind"] == "spot":
                    u = self.rent_u(r["m"])[0]
                    if hash_u(self.key["preempt"], t, r["id"]) < 0.02 + 0.08 * u:
                        preempted.add(r["m"])
                        p.ev.append({"type": "preempted", "rent_id": r["id"], "model": r["m"], "n": r["n"]})
                        avail[r["m"]] -= r["n"]
                        left[r["m"]] -= r["n"]
                        continue
                keep.append(r)
            p.rentals = keep
            for m in GPU_NAMES:
                if left[m] < 0:
                    d = -left[m]
                    left[m] = 0
                    serve[m] = max(0, serve[m] - d)
        pts = sum(s["ab"] for s in p.staff)
        self._research(p, pts)
        hot = []
        for name in p.run_order:
            run = p.runs[name]
            if run.status != "running" or run.review > 0:
                run.granted = {}
                run.ncards = 0
                if run.review > 0:
                    run.review -= 1
                    if run.review == 0:
                        run.approved = True
                        p.ev.append({"type": "review_done", "run": run.name})
                continue
            g = {}
            n = 0
            for m in GPU_NAMES:
                k = min(run.req.get(m, 0), left[m])
                if k > 0:
                    g[m] = k
                    left[m] -= k
                    n += k
            run.granted = g
            run.ncards = n
            hot.append(run)
        # 被抢占的型号：中断最后一个用到它的任务
        for m in preempted:
            for run in reversed(hot):
                if run.granted.get(m):
                    self._interrupt(p, run, t, "preempt")
                    break
        for run in hot:
            self._advance(p, run, t)
        p._serve_cards = serve
        p._left = left
        p._used = {m: avail[m] - left[m] for m in GPU_NAMES}

    def _research(self, p, pts):
        if not p.share_r or pts <= 0:
            return
        avail = {}
        for k, s in p.share_r.items():
            if s <= 0 or k in p.tech:
                continue
            pre = TECHS[k][1]
            if pre and pre not in p.tech:
                continue
            avail[k] = s
        z = sum(avail.values())
        if z <= 0:
            return
        for k, s in avail.items():
            p.pts[k] = p.pts.get(k, 0.0) + pts * s / z
            if p.pts[k] >= TECHS[k][0]:
                p.tech.add(k)
                ev = {"type": "tech", "tech": k}
                if k.startswith("filter:"):
                    d = k[7:]
                    ev["q"] = round(self.w["q"][d], 4)
                    ev["c"] = round(self.w["c"][d] * 0.5, 4)
                    p.refresh_data(self.w)
                if k == "cooling":
                    self._install(p, self.t)
                p.ev.append(ev)

    def _interrupt(self, p, run, t, why):
        if run.ckpts:
            run.restore(run.ckpts[-1])
            ck = run.ckpts[-1]["id"]
        else:
            run.restore({"D": 0.0, "S": {q: 0.0 for q in POOLS}, "Sq": {q: 0.0 for q in POOLS}, "pen": 0.0,
                         "spike": 1.0, "cont": 0.0, "deff": run.deff, "compute": 0.0, "teacher": 0.0})
            ck = 0
        run.restart = RESTART
        run.since = 0
        p.ev.append({"type": why, "run": run.name, "ckpt": ck, "D": round(run.D, 3)})

    def _advance(self, p, run, t):
        n = run.ncards
        if n <= 0:
            run.tpt = 0.0
            run.eff = 0.0
            return
        k = run.ordinal
        # 故障
        if hash_u(self.key["fail"], t, k) < n / MTBF:
            own_models = [m for m in GPU_NAMES if run.granted.get(m) and p._own[m] > 0]
            if own_models:
                m = max(own_models, key=lambda x: run.granted[x])
                p.broken.append((t + REPAIR, m))
                p.broken_n[m] += 1
            self._interrupt(p, run, t, "failure")
            return
        if run.restart > 0:
            run.restart -= 1
            run.tpt = 0.0
            return
        if t % DAY == 0 and run.slow >= 1.0 and hash_u(self.key["slow"], t, k) < SLOW_P:
            run.slow = 1.0 - (0.1 + 0.2 * hash_u(self.key["slowf"], t, k))
            p.ev.append({"type": "slow_node", "run": run.name})
        raw = sum(GPU[m]["power"] * c for m, c in run.granted.items())
        util = min(1.0, 2.0 * run.batch / n)
        bc = self.w["bc0"] * (1 + run.D / 50.0) ** 0.4
        b = run.batch
        waste = (min(b, bc) + 0.5 * max(0.0, b - bc)) / b
        eff = util * waste * run.slow
        run.since += 1
        do_ck = run.since >= run.every
        frac = (1 - CKPT_COST) if do_ck else 1.0
        work = raw * eff * frac
        tok = work / (run.N * KC)
        rem = run.target - run.D
        if tok >= rem:
            tok = max(0.0, rem)
            work = tok * run.N * KC
        run.tpt = tok
        run.eff = eff
        if tok > 0:
            run.D += tok
            for q, f in run.mix.items():
                x = tok * f
                run.S[q] += x
                run.Sq[q] += x * p.qavg[q]
                run.cont += x * p.cavg[q]
            if run.mix.get("distill"):
                run.teacher = p.max_teacher
            run.pen += tok * 0.08 * math.log(run.lr / run.eta) ** 2
            p.copy_tok += tok * (run.mix.get("web", 0) + run.mix.get("books", 0))
            p.month_tok += tok
            before = run.compute
            run.compute += work
            if not run.approved and before < REG_COMPUTE <= run.compute:
                run.review = self._review_len(p)
                p.ev.append({"type": "review", "run": run.name, "until": t + run.review})
        if do_ck and run.status == "running":
            run.snap(t)
            run.since = 0
        # 尖峰（在存档之后判定：回滚到最新存档即可撤销本次尖峰）
        ratio = run.lr / run.eta
        if ratio > 1.0:
            ps = 0.002 * math.exp(3 * (ratio - 1))
            if "stable" in p.tech:
                ps /= 2
            if hash_u(self.key["spike"], t, k) < ps:
                run.spike *= 1.02
                if hash_u(self.key["div"], t, k) < 0.1:
                    run.status = "diverged"
                    p.ev.append({"type": "diverged", "run": run.name})
                else:
                    p.ev.append({"type": "spike", "run": run.name, "D": round(run.D, 3)})
        if run.D >= run.target - 1e-9 and run.status == "running":
            run.status = "done"
            if not run.ckpts or run.ckpts[-1]["D"] < run.D:
                run.snap(t)
            L = self.loss_of(p, run.ckpts[-1], run)
            p.ev.append({"type": "done", "run": run.name, "ckpt": run.ckpts[-1]["id"],
                         "loss": [round(self._noisy(x, t, k, i), 5) for i, x in enumerate(L)]})

    def _noisy(self, x, t, k, i):
        u1 = hash_u(self.key["noise"], t, k, i)
        u2 = hash_u(self.key["noise"], t, k, i + 100)
        z = math.sqrt(-2 * math.log(max(1e-12, u1))) * math.cos(2 * math.pi * u2)
        return x * (1 + 0.01 * z)

    # ------------------------------------------------------------ API 市场
    def _api(self, t):
        Q = W.demand_q(self.w, t)
        self._Q = Q
        inf = {}
        for i, p in enumerate(self.players):
            p._rev = 0.0
            p._served = p._dem = 0.0
            m, _ = self._flagship(p.label)
            if m is None or p.bankrupt:
                continue
            dem = self.shares.get(p.label, 0.0) * Q
            mult = 1.0
            if "infer_opt_2" in p.tech:
                mult = 0.6
            elif "infer_opt_1" in p.tech:
                mult = 0.8
            per = SK * m["N"] * mult           # G1 算力 / (B token/tick)
            if p.serve:
                comp = sum(GPU[x]["power"] * p._serve_cards[x] for x in GPU_NAMES)
            else:
                # 自动：用剩余卡里最旧的，够用为止
                need = dem * per
                comp = 0.0
                sc = dict.fromkeys(GPU_NAMES, 0)
                for x in GPU_NAMES:
                    if comp >= need:
                        break
                    k = min(p._left[x], int(math.ceil((need - comp) / GPU[x]["power"])))
                    sc[x] = k
                    comp += k * GPU[x]["power"]
                p._serve_cards = sc
                for x in GPU_NAMES:
                    p._used[x] += sc[x]
            cap = comp / per if per > 0 else 0.0
            srv = min(dem, cap)
            rev = srv * m["price"] / 1000.0
            p._served, p._dem = srv, dem
            p._util = srv / cap if cap > 0 else 0.0
            p.cash += rev
            p._rev = rev
            p.tot["revenue"] += rev
            p.wk_dem += dem
            p.wk_srv += srv
            inf[i] = (m, srv)

    # ------------------------------------------------------------ 成本
    def _costs(self, p, t):
        idx = t % MONTH
        p.rev_ring[idx] = p._rev if not p.bankrupt else 0.0
        p.tot_ring[idx] = p._rev + p.distill_in
        p.distill_in = 0.0
        if p.bankrupt:
            p.cost_ring[idx] = 0.0
            return
        # 工资
        sal = sum(s["sal"] for s in p.staff)
        p.cash -= sal
        p.tot["salary"] += sal
        # 租金
        rent = 0.0
        rent_m = dict.fromkeys(GPU_NAMES, 0.0)
        for r in p.rentals:
            pr = self.rent_price(r["m"]) * (SPOT_MULT if r["kind"] == "spot" else 1.0) * r["n"]
            rent += pr
            rent_m[r["m"]] += pr
        p.cash -= rent
        p.tot["rent"] += rent
        # 电费：在用的自有卡（先用自有、后用租的），按各机房装机比例分摊
        pue = p.pue
        elec = 0.0
        serve_cost = 0.0
        own_tot = {m: sum(s["cards"][m] for s in p.sites if s["ready"] <= t) for m in GPU_NAMES}
        for site in p.sites:
            if site["ready"] > t:
                continue
            mwh = 0.0
            for m in GPU_NAMES:
                if own_tot[m] <= 0:
                    continue
                used_own = min(p._used[m], p._own[m])
                if p._serve_cards[m] and p._used[m] > 0:
                    sv_frac = p._serve_cards[m] / p._used[m]
                    used_own_kw = used_own * (1 - sv_frac) + used_own * sv_frac * (0.3 + 0.7 * getattr(p, "_util", 1.0))
                else:
                    used_own_kw = used_own
                mwh += used_own_kw * site["cards"][m] / own_tot[m] * GPU[m]["kw"] * pue / 1000.0
            price = W.elec_price(self.w, site["region"], t)
            cost = 0.0
            cover = 0.0
            for ppa in site["ppa"]:
                if ppa[0] > t:
                    cost += ppa[1] * ppa[2]
                    cover += ppa[1]
            if mwh > cover:
                cost += (mwh - cover) * price
            elec += cost
            site["ppa"] = [x for x in site["ppa"] if x[0] > t]
        p.cash -= elec
        p.tot["elec"] += elec
        # 服务成本（毛利用）：服务卡的电费份额 + 租金份额
        for m in GPU_NAMES:
            sc = p._serve_cards[m]
            if sc <= 0:
                continue
            used = max(1, p._used[m])
            serve_cost += rent_m[m] * sc / max(1, p._own[m] + sum(r["n"] for r in p.rentals if r["m"] == m))
            serve_cost += sc * GPU[m]["kw"] * pue / 1000.0 * W.elec_price(self.w, p.sites[0]["region"], t) * \
                (0.3 + 0.7 * getattr(p, "_util", 1.0))
        p.cost_ring[idx] = serve_cost
        # 破产
        if p.cash < 0:
            p.neg += 1
            if p.neg >= BANKRUPT_TICKS:
                p.bankrupt = True
                p.ev.append({"type": "bankrupt"})
                self.replay["events"].append({"t": t, "type": "bankrupt", "lab": p.label})
                for r in p.runs.values():
                    if r.status not in ("done", "stopped"):
                        r.status = "stopped"
                p.rentals = []
                p.staff = []
                for m in self.models.values():
                    if m["owner"] == p.label:
                        m["delisted"] = True
        else:
            p.neg = 0

    # ------------------------------------------------------------ 每月
    def _monthly(self, t):
        mi = t // MONTH
        for m in self.models.values():
            if m["owner"] in ("A", "B") and not m.get("audited") and m["pub"] - m["C"] > AUDIT_GAP:
                p = self.players[0 if m["owner"] == "A" else 1]
                m["audited"] = True
                m["pub"] = m["C"]
                p.rep = max(-3.0, p.rep - AUDIT_REP)
                e = {"type": "audit_fail", "model": m["id"], "lab": m["owner"], "pub": round(m["pub"], 4)}
                self.pub_ev.append(e)
                self.replay["events"].append({"t": t, **e})
        u = self.w["quit_u"][min(len(self.w["quit_u"]) - 1, mi)]
        for i, p in enumerate(self.players):
            if p.bankrupt:
                continue
            if u < 0.02 and p.staff:
                s = max(p.staff, key=lambda x: (x["ab"], -x["since"], -x["id"]))
                p.staff.remove(s)
                p.ev.append({"type": "quit", "id": s["id"], "ab": s["ab"]})
            # 版权诉讼
            frac = p.copy_tok / p.month_tok if p.month_tok > 0 else 0.0
            if frac > 0.5 and hash_u(self.key["copy"], mi, 0) < 0.1 * (frac - 0.5) / 0.5:
                fine = 0.03 * max(0.0, p.cash)
                p.cash -= fine
                p.tot["fines"] += fine
                p.ev.append({"type": "lawsuit", "kind": "copyright", "fine": round(fine, 3)})
            p.copy_tok = p.month_tok = 0.0
        for j in range(2):
            self.npc_staff[j] += 1

    # ------------------------------------------------------------ 观测
    def _obs(self, i, t):
        p = self.players[i]
        o = {"t": t, "cash": round(p.cash, 4), "eq": round(p.equity, 5), "rep": round(p.rep, 4),
             "bk": p.bankrupt}
        gpu = {}
        for m in GPU_NAMES:
            inst = sum(s["cards"][m] for s in p.sites)
            if inst or p.inv[m] or p._rented[m]:
                gpu[m] = [p._own[m], p.broken_n[m], p.inv[m], p._rented[m], p._used[m]]
        o["gpu"] = gpu
        runs = []
        for name in p.run_order:
            r = p.runs[name]
            if r.status in ("done", "stopped") and not any(e.get("run") == name for e in p.ev):
                continue
            runs.append([name, r.status if r.review <= 0 else "review", round(r.D, 4), round(r.target, 4),
                         round(r.tpt, 5), round(r.eff, 4), r.ckpts[-1]["id"] if r.ckpts else 0, r.ncards])
        o["runs"] = runs
        o["api"] = [round(p._dem, 5), round(p._served, 5), round(p._rev, 6)]
        ev = list(self.pub_ev) + p.ev
        if ev:
            o["ev"] = ev
        if p.res:
            o["res"] = p.res
        if t % DAY == DAY - 1:
            loss = {}
            for name in p.run_order:
                r = p.runs[name]
                if r.status in ("running", "paused", "diverged") and r.D > 0:
                    st = {"D": r.D, "S": r.S, "Sq": r.Sq, "pen": r.pen, "spike": r.spike, "cont": r.cont,
                          "deff": r.deff, "teacher": r.teacher}
                    L = self.loss_of(p, st, r)
                    loss[name] = [round(self._noisy(x, t, r.ordinal, j), 5) for j, x in enumerate(L)]
            if loss:
                o["loss"] = loss
            o["rent"] = {m: round(self.rent_price(m), 8) for m in GPU_NAMES if self.launched(m)}
            o["elec"] = [round(W.elec_price(self.w, r, t), 7) for r in range(5)]
            o["pts"] = {k: round(v) for k, v in p.pts.items() if k not in p.tech}
        if t % WEEK == 0:
            wk = dict(self.week_info)
            wk["offer"] = {m: {"lots": x["lots"], "reserve": x["reserve"], "early": x["early"]}
                           for m, x in self.offer.items()}
            wk["deals"] = [{k: d[k] for k in ("id", "domain", "tokens_b", "q_hint", "reserve")}
                           for d in self.open_deals]
            wk["talent"] = [{k: c[k] for k in ("id", "lo", "hi", "ask")} for c in self.open_talent]
            wk["shares"] = {k: round(v, 4) for k, v in self.shares.items()}
            wk["models"] = [self._pub_model(m) for m in self.models.values() if not m.get("delisted")]
            o_ = self.players[1 - i]
            wk["staff"] = {"opp": len(o_.staff), "N1": self.npc_staff[0], "N2": self.npc_staff[1]}
            wk["demand"] = round(self._Q, 5)
            wk["my"] = {"staff": [[s["id"], s["ab"], s["sal"]] for s in p.staff],
                        "sites": [{"id": s["id"], "region": s["region"], "mw": s["mw"], "ready": s["ready"],
                                   "pend_mw": sum(x[1] for x in s["pend"]), "cards": {m: n for m, n in s["cards"].items() if n}} for s in p.sites],
                        "data": {q: round(p.U[q], 2) for q in POOLS},
                        "tech": sorted(p.tech), "share": round(p.share, 4), "drop": round(p.drop, 4),
                        "deliv": [[d[0], d[1], d[2]] for d in p.deliv]}
            o["wk"] = wk
        return o

    # ------------------------------------------------------------ 估值
    def best_c(self, seat):
        lab = self.players[seat].label
        return max([m["C"] for m in self.models.values() if m["owner"] == lab] or [0.0])

    def value(self, seat):
        p = self.players[seat]
        t = max(0, self.t)
        hw = 0.0
        for b in p.batches:
            hw += b[1] * b[2] * max(0.0, 1 - (t - b[3]) / HW_LIFE)
        site = 0.0
        for s in p.sites:
            site += s["cost"] * max(0.0, 1 - (t - s["built"]) / SITE_LIFE)
        recv = sum(r[1] for r in p.recv)
        gm = sum(p.rev_ring) - sum(p.cost_ring)
        C = self.best_c(seat)
        if p.bankrupt:
            V = max(0.0, p.cash + 0.3 * hw)
            parts = {"cash": p.cash, "hw": 0.3 * hw, "site": 0.0, "recv": 0.0, "gm": 0.0, "C": C}
        else:
            V = p.cash + recv + hw + site + END_GM_MULT * gm + END_C_MULT * C
            parts = {"cash": p.cash, "hw": hw, "site": site, "recv": recv, "gm30": gm, "C": C}
        return {"value": p.equity * V, "V": V, "equity": p.equity, **parts}

    def _snapshot(self, t):
        day = {"day": t // DAY, "shares": {k: round(v, 4) for k, v in self.shares.items()}}
        for i, p in enumerate(self.players):
            v = self.value(i)
            comp = sum(GPU[m]["power"] * (p._own[m] + p._rented[m]) for m in GPU_NAMES)
            day[p.label] = {"value": round(v["value"], 2), "cash": round(p.cash, 2), "C": round(v["C"], 4),
                            "compute": round(comp), "staff": len(p.staff), "eq": round(p.equity, 4),
                            "rep": round(p.rep, 3), "runs": sum(1 for r in p.runs.values() if r.ncards > 0)}
        self.replay["days"].append(day)


RULE_CONSTS = {k: v for k, v in dict(
    DAY=DAY, WEEK=WEEK, MONTH=MONTH, QUARTER=QUARTER, YEAR=YEAR, TICKS=TICKS, LOT=LOT, KC=KC, SK=SK,
    PUE0=PUE0, PUE1=PUE1, MTBF=MTBF, RESTART=RESTART, REPAIR=REPAIR, CKPT_COST=CKPT_COST, SLOW_P=SLOW_P,
    REG_COMPUTE=REG_COMPUTE, REVIEW=REVIEW, RENT_HOURS=RENT_HOURS, SPOT_MULT=SPOT_MULT,
    RENT_POOL_MULT=RENT_POOL_MULT, SELL_RATE=SELL_RATE, SELL_DELAY=SELL_DELAY, EXPAND_COST=EXPAND_COST,
    EXPAND_TICKS=EXPAND_TICKS, PPA_DISC=PPA_DISC, DEFAULT_LR_C=DEFAULT_LR_C, DEFAULT_BATCH=DEFAULT_BATCH,
    DEFAULT_CKPT=DEFAULT_CKPT, COLLAPSE_AT=COLLAPSE_AT, COLLAPSE_K=COLLAPSE_K, MIX_PENALTY=MIX_PENALTY,
    CONT_K=CONT_K, AUDIT_GAP=AUDIT_GAP, AUDIT_REP=AUDIT_REP, DISTILL_CAP_FRAC=DISTILL_CAP_FRAC,
    DISTILL_SUE_FRAC=DISTILL_SUE_FRAC, DISTILL_SUE_P=DISTILL_SUE_P, LAUNCH=LAUNCH, PRICE_DEFAULT=PRICE_DEFAULT,
    PRICE_MIN=PRICE_MIN, PRICE_MAX=PRICE_MAX, U_PUB=U_PUB, U_PRICE=U_PRICE, U_DROP=U_DROP, MIG_MAX=MIG_MAX,
    OPEN_PRICE=OPEN_PRICE, VAL_REV_MULT=VAL_REV_MULT, VAL_LEAD=VAL_LEAD, END_GM_MULT=END_GM_MULT,
    END_C_MULT=END_C_MULT, BANKRUPT_TICKS=BANKRUPT_TICKS, MAX_RUNS=MAX_RUNS, MAX_CKPTS=MAX_CKPTS,
    EXCL_TICKS=EXCL_TICKS, HW_LIFE=HW_LIFE, SITE_LIFE=SITE_LIFE, START_CASH=START_CASH, START_MW=START_MW,
    ALGO_MULT=ALGO_MULT, STEP_TIMEOUT=STEP_TIMEOUT, TIME_POOL=TIME_POOL, TOTAL_BUDGET=TOTAL_BUDGET, VALUE_SCALE=VALUE_SCALE,
    ETA_EXP=-0.3, BCRIT="bc0*(1+D/50)^0.4", Q0=W.Q0).items()}


# ================================================================= Bot 进程
class BotError(Exception):
    pass


class Bot:
    """子进程 bot。读写都在后台线程里（Windows 管道不支持 select）。
    超时：本 tick 按空动作处理，迟到的那一行在之后被丢弃（按未答消息计数对齐）。"""

    def __init__(self, path: str, init: dict):
        self.path = path
        self.used = 0.0
        self.pool = TIME_POOL
        self.dead = False
        self.timeouts = 0
        self.bad = 0
        self.outstanding = 0
        self.max_step = 0.0
        env = {**os.environ, "PYTHONHASHSEED": "0"}
        self.proc = subprocess.Popen(
            [sys.executable, "-u", os.path.abspath(path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, env=env, cwd=os.path.dirname(os.path.abspath(path)),
        )
        self.q: queue.Queue = queue.Queue()
        self.wq: queue.Queue = queue.Queue()
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._writer, daemon=True).start()
        self.ask(init, timeout=INIT_TIMEOUT)

    def _reader(self):
        try:
            for line in self.proc.stdout:
                self.q.put(line)
        except Exception:
            pass
        self.q.put("")

    def _writer(self):
        while True:
            s = self.wq.get()
            if s is None:
                return
            try:
                self.proc.stdin.write(s)
                self.proc.stdin.flush()
            except Exception:
                return

    def _charge(self, dt: float, timeout: float) -> None:
        """时间池：单 tick 超出 STEP_TIMEOUT 的部分从池里扣，池独立于单 tick、整局不回补。"""
        if timeout == STEP_TIMEOUT:
            self.pool = max(0.0, self.pool - max(0.0, dt - STEP_TIMEOUT))

    def ask(self, msg: dict, timeout: float = STEP_TIMEOUT):
        if self.dead:
            return None
        if self.outstanding >= MAX_OUTSTANDING:
            self.dead = True
            self.kill()
            return None
        self.wq.put(json.dumps(msg, separators=(",", ":")) + "\n")
        self.outstanding += 1
        t0 = time.perf_counter()
        step = timeout + (self.pool if timeout == STEP_TIMEOUT else 0.0)
        limit = min(step, max(0.0, TOTAL_BUDGET - self.used))
        deadline = t0 + limit
        try:
            while True:
                rem = deadline - time.perf_counter()
                if rem <= 0:
                    raise queue.Empty
                line = self.q.get(timeout=rem)
                if not line:
                    raise BotError("eof")
                self.outstanding -= 1
                if self.outstanding > 0:
                    continue          # 之前超时那几 tick 的迟到回复，丢弃
                break
        except queue.Empty:
            self.used += time.perf_counter() - t0
            self._charge(time.perf_counter() - t0, timeout)
            self.timeouts += 1
            if self.used >= TOTAL_BUDGET:
                self.dead = True
                self.kill()
            return None
        except (BotError, OSError):
            self.used += time.perf_counter() - t0
            self.dead = True
            self.kill()
            return None
        dt = time.perf_counter() - t0
        self.used += dt
        self._charge(dt, timeout)
        self.max_step = max(self.max_step, dt)
        try:
            v = json.loads(line)
            if not isinstance(v, dict):
                raise ValueError
            return v
        except (ValueError, RecursionError):
            self.bad += 1
            return None

    def kill(self):
        try:
            self.wq.put(None)
            self.proc.kill()
        except Exception:
            pass


class _InProc:
    def __init__(self, path, init):
        import importlib.util
        spec = importlib.util.spec_from_file_location("bot_" + str(id(self)), path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.bot = mod.make_bot(json.loads(json.dumps(init)))
        self.timeouts = self.bad = 0
        self.dead = False
        self.used = 0.0
        self.pool = TIME_POOL
        self.max_step = 0.0

    def ask(self, msg):
        if msg.get("type") == "end":
            return {}
        t0 = time.perf_counter()
        r = self.bot.step(json.loads(json.dumps(msg)))
        dt = time.perf_counter() - t0
        self.used += dt
        self.max_step = max(self.max_step, dt)
        return json.loads(json.dumps(r))

    def kill(self):
        pass


SOLO_NPC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "solo_npc.py")


# ================================================================= 整局
def play(path_a: str, path_b: str, seed: int, ticks: int = TICKS, inproc: bool = False, log=None,
         inproc_b: bool = False, wld: dict | None = None) -> dict:
    """跑一局。path_a 坐 A 位，path_b 坐 B 位。返回结果 dict（含 replay）。

    inproc=True：两个 bot 都在本进程里导入运行（仅供开发调参）；inproc_b=True：只有 B 位在进程内
    （单人赛的固定 NPC 实验室 solo_npc.py 就这样跑，它是引擎的一部分，不计思考时间）。"""
    g = Game(seed, ticks, wld)
    wall0 = time.perf_counter()
    bots = []
    for seat, path in enumerate((path_a, path_b)):
        if inproc or (seat == 1 and inproc_b):
            bots.append(_InProc(path, g.init_msg(seat)))
        else:
            bots.append(Bot(path, g.init_msg(seat)))
    acts = [{}, {}]
    for t in range(ticks):
        obs = g.step(acts)
        acts = []
        for seat in (0, 1):
            obs[seat]["missed"] = bots[seat].timeouts + bots[seat].bad
            obs[seat]["time_pool"] = round(getattr(bots[seat], "pool", TIME_POOL), 3)
            try:
                r = bots[seat].ask(obs[seat])
            except Exception:
                r = None
            acts.append(r if isinstance(r, dict) else {})
        if log and t % (30 * DAY) == 30 * DAY - 1:
            log(t, g)
    for seat in (0, 1):
        try:
            bots[seat].ask({"type": "end", "t": ticks})
        except Exception:
            pass
        bots[seat].kill()
    wall = time.perf_counter() - wall0
    vals = [g.value(s) for s in (0, 1)]
    res = {
        "seed": seed, "ticks": ticks, "A": os.path.basename(path_a), "B": os.path.basename(path_b),
        "value": [round(v["value"], 2) for v in vals],
        "C": [round(g.best_c(s), 5) for s in (0, 1)],
        "value_detail": [{k: (round(x, 4) if isinstance(x, float) else x) for k, x in v.items()} for v in vals],
        "models": [[{k: (round(m[k], 4) if isinstance(m[k], float) else m[k])
                     for k in ("id", "C", "pub", "price", "live", "N", "D")} for m in p.models]
                   for p in g.players],
        "faults": [{"timeouts": b.timeouts, "bad_json": b.bad, "dead": b.dead,
                    "illegal": g.players[i].faults["illegal"], "think_s": round(b.used, 2), "pool_left_s": round(getattr(b, "pool", TIME_POOL), 2),
                    "max_step_s": round(b.max_step, 3)} for i, b in enumerate(bots)],
        "totals": [{k: round(v, 2) for k, v in p.tot.items()} for p in g.players],
        "tech": [sorted(p.tech) for p in g.players],
        "engine_s": round(g.engine_time, 2), "wall_s": round(wall, 2),
        "replay": g.replay,
    }
    return res


def s_share(v_x, v_r, scale=VALUE_SCALE):
    return 0.5 + 0.5 * math.tanh((v_x - v_r) / scale)
