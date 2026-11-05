"""骨干争夺（Backbone Wars）对战引擎，只用 Python 标准库。

规则与协议见 README.md。

结构：
  Game        —— 纯状态机：Game(map, seed).step([actA, actB]) -> [obsA, obsB]
  Bot         —— 子进程 bot（stdin/stdout 每 tick 一行 JSON，线程读写，兼容 Windows）
  play(...)   —— 跑一整局，返回结果 dict（含 replay）
"""
from __future__ import annotations

import hashlib
import heapq
import json
import math
import os
import queue
import random
import subprocess
import sys
import threading
import time
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mapgen  # noqa: E402

# ----------------------------------------------------------------- 常量
DAY = 144
WEEK = 7 * DAY
TICKS = 8 * WEEK                       # 8064
YEAR = 365 * DAY
CLS = ["video", "rt", "backup"]
CLS_FRAC = [0.60, 0.15, 0.25]
CLS_LIMIT = [150.0, 40.0, None]        # 延迟上限 ms
CLS_PRICE = [1.0, 4.0, 0.4]            # 单价基准（每 100 单位流量·tick）
REV_DIV = 35.0
DEFER_MAX = 36
JITTER_LIMIT = 10.0

START_CASH = 20000.0
CREDIT = -10000.0
MAX_POPS = 20
POP_BUILD_K = 3000.0
BUILD_TICKS = 36
POP_OP_K = 40.0                        # 每天运营 = 地价 × 40 × 电价
PORT0 = 200.0
EXPAND_UNITS = 100.0
EXPAND_COST = 800.0
EXPAND_TICKS = 24
SELL_RATE = 0.6

LEASE_TERMS = {1: 1.0, 7: 0.8, 28: 0.6}
LEASE_SHARE, SPOT_SHARE, NPC_SHARE = 0.7, 0.2, 0.1
RELEASE_REFUND = 0.5

CACHE_COST = 500.0
CACHE_OP_DAY = 5.0
N_CAT = 20
CAT_HALF = 3.0                         # 某类目分配 a 单位时命中率 a/(a+CAT_HALF)
UPS_COST = 2000.0
SCRUB_COST = 50.0
DDOS_MULT = 5.0

INIT_SHARE = 0.35
NEW_SHARE = 0.05
MIG_MAX = 0.08
BETA, GAMMA, DELTA, ETA = 2.0, 2.0, 0.3, 1.0
NPC_SLA = 0.85
PRICE_MIN, PRICE_MAX, PRICE_STEP, PRICE_CHANGES = 0.2, 5.0, 0.30, 2

DEM_K = 50000.0                        # 引力模型系数
DEM_ALPHA = 1.2
TREND_PER_DAY = 0.004

CONTRACT_DAYS = 28
CONTRACT_SLA = 0.99
CONTRACT_PENALTY = 2.0
NW_SCALE = 102079.91  # scoring_v2: frozen independent baseline calibration

STEP_TIMEOUT = 0.2
TIME_POOL = 60.0  # 时间池（秒）：独立于单 tick，单步可超出 STEP_TIMEOUT，超出部分从池扣；池空后回到单 tick 硬时限
TOTAL_BUDGET = 20 * 60.0
INIT_TIMEOUT = 10.0
MAX_OUTSTANDING = 50

SALT = os.environ.get("BACKBONE_SALT", "practice")


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


def _sub_seed(seed, tag):
    h = hashlib.sha256(f"{SALT}|{seed}|{tag}".encode()).hexdigest()
    return int(h[:15], 16)


def _diurnal(h):
    """本地时间 h（小时，0-24）的昼夜系数，均值 1，21 点峰值、约 4 点低谷。"""
    return 1.0 + 0.55 * math.cos(2 * math.pi * (h - 21.0) / 24.0)


WEEKLY = [1.0, 1.0, 1.0, 1.0, 1.05, 1.15, 1.10]


# ================================================================= Game
class Player:
    def __init__(self, seat):
        self.seat = seat
        self.cash = START_CASH
        self.bankrupt = False
        self.pops = {}          # city -> dict
        self.assets = []        # [cost, tick]（PoP、扩容、缓存、UPS 的账面）
        self.leases = {}        # id -> dict
        self.lease_units = None  # per link
        self.routes = {}        # (a,b,c) -> [(nodes, links, split)]
        self.prices = [[1.0, 1.0, 1.0] for _ in range(6)]
        self.price_changes = [[0, 0, 0] for _ in range(6)]
        self.share = None       # [city][cls]
        self.queues = {}        # (a,b) -> deque[[tick, units]]
        self.defer_req = {}     # (a,b) -> units（本 tick）
        self.scrub = set()
        self.dirty = True
        self.plan = None
        self.leases_changed = True
        self.res = []
        self.day_rev = 0.0
        self.day_opex = 0.0
        self.gp_hist = []
        self.sla_num = None     # [city][cls] 当天
        self.sla_den = None
        self.sla_hist = deque(maxlen=7)   # 每天 (num[city][cls], den[city][cls], outages[city])
        self.outages = None
        self.down_state = {}
        self.cache_buy_day = {}
        self.hour = {}          # (a,b,c) -> [dem, good, lat*dem, loss*dem]
        self.vidvol = {}        # city -> 当天视频量
        self.evheat = {}        # city -> {cat: 突发额外量}
        self.heat_out = {}
        self.amort = 0.0
        self.contracts = []     # 中标合同
        self.faults = {"timeout": 0, "bad_json": 0, "illegal": 0, "dead": False}
        self.tot = {"rev": 0.0, "lease": 0.0, "opex": 0.0, "capex": 0.0, "penalty": 0.0,
                    "peer_in": 0.0, "peer_out": 0.0, "contract_in": 0.0,
                    "dem_video": 0.0, "dem_rt": 0.0, "dem_backup": 0.0, "good_video": 0.0,
                    "good_rt": 0.0, "good_backup": 0.0, "rev_video": 0.0, "rev_rt": 0.0, "rev_backup": 0.0}


class Game:
    def __init__(self, gmap: dict, seed: int, ticks: int = TICKS):
        self.map = gmap
        self.seed = seed
        self.T = ticks
        self.t = 0
        C = self.cities = gmap["cities"]
        L = self.links = gmap["links"]
        self.n, self.m = len(C), len(L)
        self.region = [c["region"] for c in C]
        self.tz = {r["id"]: r["tz"] for r in gmap["regions"]}
        self.adj = [[] for _ in range(self.n)]
        self.edge = {}
        for l in L:
            self.adj[l["u"]].append((l["v"], l["id"]))
            self.adj[l["v"]].append((l["u"], l["id"]))
            self.edge[(l["u"], l["v"])] = l["id"]
            self.edge[(l["v"], l["u"])] = l["id"]
        self.lat = [l["lat"] for l in L]
        self.cap = [float(l["cap"]) for l in L]
        self.leasable = [LEASE_SHARE * c for c in self.cap]
        self.spotcap = [SPOT_SHARE * c for c in self.cap]
        self.base = [l["base"] for l in L]
        self.lreg = [(self.region[l["u"]], self.region[l["v"]]) for l in L]
        self.is_mw = [l["type"] == "mw" for l in L]
        self.leased_total = [0.0] * self.m
        self.down_cnt = [0] * self.m
        self.rain = [0] * 6
        self.hike = [1.0] * 6
        self.topo_ver = 0
        self.sp_cache = {}
        self.spot_prev = [0.0] * self.m
        self.mirror_city = gmap.get("mirror") or list(range(self.n))
        self.mirror_link = [self.edge.get((self.mirror_city[l["u"]], self.mirror_city[l["v"]]), l["id"])
                            for l in L]
        rng = random.Random(_sub_seed(seed, "bg"))
        self.bg_frac = [0.0] * self.m
        for l in range(self.m):
            if l <= self.mirror_link[l]:
                self.bg_frac[l] = self.bg_frac[self.mirror_link[l]] = rng.uniform(0.2, 0.6)
        self.rng_obs = random.Random(_sub_seed(seed, "obs"))
        self.rng_misc = random.Random(_sub_seed(seed, "misc"))
        self.pair_base = {}
        self.players = [Player(0), Player(1)]
        for p, key in zip(self.players, ("A", "B")):
            p.lease_units = [0.0] * self.m
            p.share = [[0.0, 0.0, 0.0] for _ in range(self.n)]
            p.sla_num = [[0.0] * 3 for _ in range(self.n)]
            p.sla_den = [[0.0] * 3 for _ in range(self.n)]
            p.outages = [0] * self.n
            for c in gmap["start"][key]:
                cost = C[c]["land"] * POP_BUILD_K
                p.pops[c] = self._new_pop(c, -BUILD_TICKS, cost)
                a = [cost, 0]
                p.pops[c]["assets"].append(a)
                p.assets.append(a)
                p.share[c] = [INIT_SHARE] * 3
        self.next_lease = 1
        self.next_offer = 1
        self.offers = {}
        self.agreements = {}
        self.ev_mult = {}         # city -> [(extra_mult, cat)]
        self.power = {}           # city -> count
        self.ddos = {}            # (seat, city) -> end
        self._gen_noise()
        self._gen_popularity()
        self._gen_events()
        self.contracts = {}
        self.replay = {"days": [], "events": []}
        self.engine_time = 0.0

    # ---------------------------------------------------------- 生成
    def _gen_noise(self):
        """需求噪声：每个大区一条 AR(1)，镜像大区共用同一条（保证两半对称）。"""
        rng = random.Random(_sub_seed(self.seed, "noise"))
        self.F = []
        nz = [0.0] * 6
        for t in range(self.T):
            day = t // DAY
            trend = (1 + TREND_PER_DAY * day) * WEEKLY[day % 7]
            for r in range(3):
                nz[r] = 0.9 * nz[r] + rng.gauss(0, 0.02)
                nz[5 - r] = nz[r]
            row = []
            for r in range(6):
                h = ((t % DAY) / 6.0 + self.tz[r]) % 24
                row.append(_diurnal(h) * trend * (1 + nz[r]))
            self.F.append(row)

    def _gen_popularity(self):
        rng = random.Random(_sub_seed(self.seed, "pop"))
        H = sum(1 / (k + 1) for k in range(N_CAT))
        self.zipf = [1 / (k + 1) / H for k in range(N_CAT)]
        self.rank = [None] * 6
        for r in range(3):
            perm = list(range(N_CAT))
            rng.shuffle(perm)
            self.rank[r] = perm              # rank[r][i] = 第 i 热的类目
            self.rank[5 - r] = perm          # 镜像大区同一排列（同一个对象，漂移也同步）
        self.rng_pop = rng
        self._refresh_popularity()

    def _refresh_popularity(self):
        self.popw = []
        for r in range(6):
            w = [0.0] * N_CAT
            for i, k in enumerate(self.rank[r]):
                w[k] = self.zipf[i]
            self.popw.append(w)

    def _drift_popularity(self):
        rng = self.rng_pop
        for r in range(3):
            perm = self.rank[r]
            for _ in range(3):
                i = rng.randrange(N_CAT - 1)
                perm[i], perm[i + 1] = perm[i + 1], perm[i]
            if rng.random() < 0.2:
                i = rng.randrange(3, N_CAT)
                k = perm.pop(i)
                perm.insert(rng.randrange(3), k)
        self._refresh_popularity()

    def _gen_events(self):
        """所有随机事件都成对镜像生成：半图里发生的事，镜像城市/链路/大区同一时刻也发生。
        所以 A、B 面对的世界严格对称（除了两半的时区差）。"""
        rng = random.Random(_sub_seed(self.seed, "events"))
        T = self.T
        days = (T + DAY - 1) // DAY
        ev = [[] for _ in range(T + 1)]
        self.events_at = ev
        mc, ml = self.mirror_city, self.mirror_link
        half = [c for c in range(self.n) if c <= mc[c]]
        hpops = [self.cities[c]["pop"] for c in half]
        lreps = sorted({min(l, ml[l]) for l in range(self.m)})

        def put(t, item):
            if 0 <= t < T:
                ev[t].append(item)

        def both_links(l):
            return [l] if ml[l] == l else [l, ml[l]]

        def both_cities(c):
            return [c] if mc[c] == c else [c, mc[c]]

        # 直播/赛事
        for d in range(days):
            for _ in range(rng.choice([0, 0, 1, 1, 1, 2])):
                st = d * DAY + rng.randrange(DAY)
                dur = rng.randint(6, 24)
                ann = max(0, st - rng.randint(12, 36))
                c0 = rng.choices(half, weights=hpops)[0]
                mult = rng.uniform(3, 8)
                est = round(mult * rng.uniform(0.5, 1.5), 2)
                cat = rng.randrange(N_CAT)
                for city in both_cities(c0):
                    put(ann, ("ann", {"type": "live", "city": city, "cat": cat, "mult_est": est,
                                      "start": st, "end": st + dur}))
                    put(st, ("live_start", (city, mult, cat)))
                    put(st + dur, ("live_end", (city, mult, cat)))
        # 链路故障
        rate = {"land": 0.25, "mw": 0.4, "sub": 0.6}
        for l0 in lreps:
            typ = self.links[l0]["type"]
            lam = rate[typ] / (56 * DAY)
            t = 0.0
            while True:
                t += rng.expovariate(lam)
                if t >= T:
                    break
                st = int(t)
                if typ == "sub":
                    dur = rng.randint(2 * DAY, 7 * DAY)
                else:
                    dur = int(min(7 * DAY, max(6, rng.expovariate(1 / 48))))
                for l in both_links(l0):
                    put(st, ("fail_start", l))
                    put(st, ("ann", {"type": "fail", "link": l, "start": st}))
                    put(st + dur, ("fail_end", l))
                    put(st + dur, ("ann", {"type": "repair", "link": l}))
                t += dur
        # 计划维护
        for d in range(days):
            if rng.random() < 0.6:
                l0 = rng.choice(lreps)
                st = d * DAY + rng.randrange(DAY) + DAY
                dur = rng.randint(6, 12)
                for l in both_links(l0):
                    put(st - DAY, ("ann", {"type": "maint", "link": l, "start": st, "end": st + dur}))
                    put(st, ("fail_start", l))
                    put(st + dur, ("fail_end", l))
        # 机房断电（按城市，任何一方在该城的 PoP 都受影响）
        lam = 0.01 / WEEK
        for c0 in half:
            t = 0.0
            while True:
                t += rng.expovariate(lam)
                if t >= T:
                    break
                st, dur = int(t), rng.randint(2, 12)
                for c in both_cities(c0):
                    put(st, ("power_start", c))
                    put(st + dur, ("power_end", c))
                t += dur
        # DDoS：每次同时打双方（各自按同一个相对位置挑 PoP）
        for d in range(days):
            if rng.random() < 0.35:
                st = d * DAY + rng.randrange(DAY)
                dur = rng.randint(3, 18)
                r = rng.random()
                put(st, ("ddos_start", (0, r, st + dur)))
                put(st, ("ddos_start", (1, r, st + dur)))
        # 天气
        for d in range(days):
            if rng.random() < 0.5:
                r0 = rng.randrange(3)
                st = d * DAY + rng.randrange(DAY)
                dur = rng.randint(12, 48)
                for r in (r0, 5 - r0):
                    put(st - 12, ("ann", {"type": "rain", "region": r, "start": st, "end": st + dur}))
                    put(st, ("rain_start", r))
                    put(st + dur, ("rain_end", r))
        # 承运商涨价
        for w in range(1, (T + WEEK - 1) // WEEK):
            if rng.random() < 0.6:
                r0 = rng.randrange(3)
                mult = round(rng.uniform(1.2, 1.5), 3)
                st = w * WEEK + rng.randrange(WEEK)
                for r in (r0, 5 - r0):
                    put(st - DAY, ("ann", {"type": "hike", "region": r, "mult": mult,
                                           "start": st, "end": st + WEEK}))
                    put(st, ("hike_start", (r, mult)))
                    put(st + WEEK, ("hike_end", (r, mult)))
        # 大客户合同：成对镜像放出
        cid = 1
        for w in range((T + WEEK - 1) // WEEK):
            for _ in range(rng.randint(2, 3)):
                a = rng.choices(half, weights=hpops)[0]
                b = rng.choices(half, weights=hpops)[0]
                if a == b:
                    continue
                c = rng.choices(range(3), weights=CLS_FRAC)[0]
                units = rng.randint(5, 25)
                st = w * WEEK + rng.randrange(DAY)
                reserve = round(units * CLS_PRICE[c] * CONTRACT_DAYS * DAY / REV_DIV * 1.2, 1)
                pairs = [(a, b)] if (mc[a], mc[b]) in ((a, b), (b, a)) else [(a, b), (mc[a], mc[b])]
                for x, y in pairs:
                    k = {"id": cid, "a": min(x, y), "b": max(x, y), "cls": CLS[c], "units": units,
                         "days": CONTRACT_DAYS, "deadline": st + DAY, "reserve": reserve}
                    put(st, ("contract_open", k))
                    put(st + DAY, ("contract_close", cid))
                    cid += 1

    # ---------------------------------------------------------- 工具
    def _new_pop(self, city, t, cost):
        return {"ready": t + BUILD_TICKS, "port": PORT0, "port_pend": [], "cache": 0.0,
                "alloc": {}, "ups": False, "cost": cost, "assets": []}

    def pop_ok(self, p, c):
        """PoP 是否在线（建成、未断电或有 UPS）。"""
        q = p.pops.get(c)
        if q is None or q["ready"] > self.t:
            return False
        if self.power.get(c) and not q["ups"]:
            return False
        return True

    def pop_built(self, p, c):
        q = p.pops.get(c)
        return q is not None and q["ready"] <= self.t

    def base_pair(self, a, b):
        key = (a, b) if a < b else (b, a)
        v = self.pair_base.get(key)
        if v is None:
            ca, cb = self.cities[a], self.cities[b]
            d = math.hypot(ca["x"] - cb["x"], ca["y"] - cb["y"]) / 1000.0
            v = DEM_K * ca["pop"] * cb["pop"] / max(d, 0.3) ** DEM_ALPHA
            self.pair_base[key] = v
        return v

    def link_up(self, l):
        return self.down_cnt[l] == 0

    def capf(self, l):
        if self.down_cnt[l]:
            return 0.0
        if self.is_mw[l]:
            ra, rb = self.lreg[l]
            if self.rain[ra] or self.rain[rb]:
                return 0.5
        return 1.0

    def lease_price(self, l):
        ra, rb = self.lreg[l]
        u = self.leased_total[l] / self.leasable[l]
        return self.base[l] * max(self.hike[ra], self.hike[rb]) * (1 + 3 * u * u)

    def shortest(self, src):
        """按传播延迟、只走在线链路的单源最短路（结果按拓扑版本缓存）。"""
        key = src
        hit = self.sp_cache.get(key)
        if hit is not None:
            return hit
        dist = [math.inf] * self.n
        prev = [(-1, -1)] * self.n
        dist[src] = 0.0
        pq = [(0.0, src)]
        lat, down = self.lat, self.down_cnt
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist[u]:
                continue
            for v, l in self.adj[u]:
                if down[l]:
                    continue
                nd = d + lat[l]
                if nd < dist[v]:
                    dist[v] = nd
                    prev[v] = (u, l)
                    heapq.heappush(pq, (nd, v))
        self.sp_cache[key] = (dist, prev)
        return dist, prev

    def path_links(self, src, dst):
        dist, prev = self.shortest(src)
        if dist[dst] == math.inf:
            return None
        out = []
        v = dst
        while v != src:
            u, l = prev[v]
            out.append(l)
            v = u
        out.reverse()
        return tuple(out)

    def lease_path(self, seat, src, dst):
        """卖方网络里的路：只走卖方有租约的在线链路；连不通则退回普通最短路。"""
        p = self.players[seat]
        dist = {src: 0.0}
        prev = {}
        pq = [(0.0, src)]
        while pq:
            d, u = heapq.heappop(pq)
            if u == dst:
                break
            if d > dist[u]:
                continue
            for v, l in self.adj[u]:
                if self.down_cnt[l] or p.lease_units[l] <= 0:
                    continue
                nd = d + self.lat[l]
                if nd < dist.get(v, math.inf):
                    dist[v] = nd
                    prev[v] = (u, l)
                    heapq.heappush(pq, (nd, v))
        if dst not in prev and src != dst:
            return self.path_links(src, dst)
        out = []
        v = dst
        while v != src:
            u, l = prev[v]
            out.append(l)
            v = u
        out.reverse()
        return tuple(out)

    def _err(self, p, msg):
        p.faults["illegal"] += 1
        if len(p.res) < 50:
            p.res.append(msg)

    # ---------------------------------------------------------- 事件
    def _process_events(self):
        t = self.t
        anns = []
        for kind, x in self.events_at[t]:
            if kind == "ann":
                anns.append(x)
            elif kind == "fail_start":
                self.down_cnt[x] += 1
                self._topo_changed()
            elif kind == "fail_end":
                self.down_cnt[x] -= 1
                self._topo_changed()
            elif kind == "rain_start":
                self.rain[x] += 1
            elif kind == "rain_end":
                self.rain[x] -= 1
            elif kind == "hike_start":
                self.hike[x[0]] *= x[1]
            elif kind == "hike_end":
                self.hike[x[0]] /= x[1]
            elif kind == "live_start":
                city, mult, cat = x
                self.ev_mult.setdefault(city, []).append((mult - 1, cat))
                for p in self.players:
                    p.dirty = True
            elif kind == "live_end":
                city, mult, cat = x
                lst = self.ev_mult.get(city, [])
                if (mult - 1, cat) in lst:
                    lst.remove((mult - 1, cat))
                if not lst:
                    self.ev_mult.pop(city, None)
                for p in self.players:
                    p.dirty = True
            elif kind == "power_start":
                self.power[x] = self.power.get(x, 0) + 1
                anns.append({"type": "power", "city": x})
            elif kind == "power_end":
                self.power[x] -= 1
                if not self.power[x]:
                    del self.power[x]
            elif kind == "ddos_start":
                seat, r, end = x
                p = self.players[seat]
                live = sorted(c for c in p.pops if self.pop_built(p, c))
                if live:
                    c = live[int(r * len(live))]
                    self.ddos[(seat, c)] = max(end, self.ddos.get((seat, c), 0))
                    p.res.append({"ddos": c})
            elif kind == "contract_open":
                k = dict(x)
                k["bids"] = {}
                self.contracts[k["id"]] = k
                anns.append({"type": "contract", **{kk: x[kk] for kk in x}})
            elif kind == "contract_close":
                anns.append(self._award(x))
        for key in [k for k, e in self.ddos.items() if e <= t]:
            del self.ddos[key]
        return anns

    def _topo_changed(self):
        self.topo_ver += 1
        self.sp_cache = {}
        for p in self.players:
            p.dirty = True

    def _award(self, cid):
        k = self.contracts[cid]
        best = None
        for seat, price in k["bids"].items():
            p = self.players[seat]
            if p.bankrupt or price > k["reserve"] or price <= 0:
                continue
            if not (self.pop_built(p, k["a"]) and self.pop_built(p, k["b"])):
                continue
            if self.sla7(p) < 0.9:
                continue
            key = (price, -self.sla7(p), seat)
            if best is None or key < best[0]:
                best = (key, seat, price)
        k["winner"] = None
        if best:
            seat, price = best[1], best[2]
            k["winner"], k["price"] = seat, price
            k["start"], k["end"] = self.t, self.t + CONTRACT_DAYS * DAY
            k["good"], k["dem"] = 0.0, 0.0
            self.players[seat].contracts.append(k)
            self.players[seat].dirty = True
        return {"type": "award", "contract": cid, "winner": k["winner"],
                "price": k.get("price")}

    def sla7(self, p):
        num = den = 0.0
        for nm, dn, _ in p.sla_hist:
            for c in range(self.n):
                for k in range(3):
                    num += nm[c][k]
                    den += dn[c][k]
        return num / den if den > 0 else 1.0

    # ---------------------------------------------------------- 动作
    def _apply_actions(self, seat, act, lease_reqs):
        p = self.players[seat]
        if p.bankrupt or not isinstance(act, dict):
            return
        p.defer_req = {}
        try:
            if not _numbers_ok(act):
                self._err(p, {"action": "non-finite or oversized number"})
                return
            self._apply_inner(p, seat, act, lease_reqs)
        except Exception as e:  # 防御：坏字段不让引擎崩，本 tick 其后的动作作废
            self._err(p, {"action": "exception: " + type(e).__name__})

    def _apply_inner(self, p, seat, act, lease_reqs):
        t = self.t

        def items(name):
            v = act.get(name)
            return v if isinstance(v, list) else []

        def num(v, lo=None):
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                return None
            if lo is not None and v < lo:
                return None
            return v

        def city_ok(c):
            return isinstance(c, int) and not isinstance(c, bool) and 0 <= c < self.n

        for it in items("sell"):
            if city_ok(it) and it in p.pops:
                q = p.pops.pop(it)
                book = 0.0
                for a in q["assets"]:
                    book += a[0] * max(0.0, 1 - (t - a[1]) / YEAR)
                    p.assets.remove(a)
                p.cash += book * SELL_RATE
                p.share[it] = [0.0, 0.0, 0.0]
                p.dirty = True
            else:
                self._err(p, {"sell": it})
        for it in items("build"):
            c = it.get("city") if isinstance(it, dict) else None
            if not city_ok(c) or c in p.pops or len(p.pops) >= MAX_POPS:
                self._err(p, {"build": c, "why": "city/exists/max"})
                continue
            cost = self.cities[c]["land"] * POP_BUILD_K
            if p.cash - cost < CREDIT:
                self._err(p, {"build": c, "why": "cash"})
                continue
            p.cash -= cost
            p.tot["capex"] += cost
            p.pops[c] = self._new_pop(c, t, cost)
            a = [cost, t]
            p.pops[c]["assets"].append(a)
            p.assets.append(a)
        for it in items("expand"):
            c = it.get("pop") if isinstance(it, dict) else None
            u = num(it.get("units") if isinstance(it, dict) else None, 1)
            if not city_ok(c) or c not in p.pops or u is None:
                self._err(p, {"expand": c})
                continue
            k = math.ceil(u / EXPAND_UNITS)
            cost = k * EXPAND_COST
            if p.cash - cost < CREDIT:
                self._err(p, {"expand": c, "why": "cash"})
                continue
            p.cash -= cost
            p.tot["capex"] += cost
            p.pops[c]["port_pend"].append((t + EXPAND_TICKS, k * EXPAND_UNITS))
            a = [cost, t]
            p.pops[c]["assets"].append(a)
            p.assets.append(a)
        for i, it in enumerate(items("lease")):
            if not isinstance(it, dict):
                self._err(p, {"lease": i})
                continue
            l, u, term, mp = it.get("link"), num(it.get("units"), 0.0), it.get("term"), num(it.get("max_price"), 0.0)
            if not (isinstance(l, int) and 0 <= l < self.m) or not u or term not in LEASE_TERMS or mp is None:
                self._err(p, {"lease": i, "why": "bad"})
                continue
            lease_reqs.append((seat, i, l, float(u), term, float(mp)))
        for it in items("release"):
            lid = it.get("lease_id") if isinstance(it, dict) else it
            ls = p.leases.get(lid)
            if ls is None:
                self._err(p, {"release": lid})
                continue
            rem = max(0, ls["end"] - t)
            refund = ls["cost"] * rem / ls["term_ticks"] * RELEASE_REFUND
            p.cash += refund
            self._drop_lease(p, lid)
        for it in items("routes"):
            self._set_route(p, it)
        for it in items("defer"):
            if not isinstance(it, dict):
                continue
            a, b, u = it.get("src"), it.get("dst"), num(it.get("units"))
            if not (city_ok(a) and city_ok(b)) or a == b or u is None:
                self._err(p, {"defer": it})
                continue
            key = (min(a, b), max(a, b))
            p.defer_req[key] = p.defer_req.get(key, 0.0) + u
        for it in items("price"):
            if not isinstance(it, dict):
                continue
            r, c, v = it.get("region"), it.get("cls"), num(it.get("value"), 0.0)
            if r not in range(6) or c not in CLS or v is None:
                self._err(p, {"price": it})
                continue
            k = CLS.index(c)
            if p.price_changes[r][k] >= PRICE_CHANGES:
                self._err(p, {"price": it, "why": "limit"})
                continue
            old = p.prices[r][k]
            v = min(old * (1 + PRICE_STEP), max(old * (1 - PRICE_STEP), v))
            v = min(PRICE_MAX, max(PRICE_MIN, v))
            if abs(v - old) > 1e-9:
                p.prices[r][k] = v
                p.price_changes[r][k] += 1
        for it in items("cache"):
            if not isinstance(it, dict):
                continue
            c, al = it.get("pop"), it.get("alloc")
            if not city_ok(c) or c not in p.pops or not isinstance(al, dict):
                self._err(p, {"cache": c})
                continue
            day = t // DAY
            if p.cache_buy_day.get(c) == day:
                self._err(p, {"cache": c, "why": "once per day"})
                continue
            new = {}
            ok = True
            for kk, vv in al.items():
                try:
                    k = int(kk)
                except (TypeError, ValueError):
                    ok = False
                    break
                vv = num(vv, 0.0)
                if not (0 <= k < N_CAT) or vv is None:
                    ok = False
                    break
                if vv > 0:
                    new[k] = float(vv)
            if not ok:
                self._err(p, {"cache": c, "why": "alloc"})
                continue
            q = p.pops[c]
            need = math.ceil(max(0.0, sum(new.values()) - q["cache"]))
            cost = need * CACHE_COST
            if need and p.cash - cost < CREDIT:
                self._err(p, {"cache": c, "why": "cash"})
                continue
            if need:
                p.cash -= cost
                p.tot["capex"] += cost
                q["cache"] += need
                a = [cost, t]
                q["assets"].append(a)
                p.assets.append(a)
            q["alloc"] = new
            p.cache_buy_day[c] = day
            p.dirty = True
        for it in items("bid"):
            if not isinstance(it, dict):
                continue
            cid, pr = it.get("contract"), num(it.get("price"), 0.0)
            k = self.contracts.get(cid)
            if k is None or "winner" in k or pr is None:
                self._err(p, {"bid": cid})
                continue
            k["bids"][seat] = float(pr)
        for it in items("peer_offer"):
            if not isinstance(it, dict):
                continue
            c, u, pr, d = it.get("city"), num(it.get("units"), 0.0), num(it.get("price"), 0.0), it.get("ticks")
            o = self.players[1 - seat]
            if (not city_ok(c) or not u or pr is None or not isinstance(d, int) or d <= 0
                    or not self.pop_built(p, c) or not self.pop_built(o, c)):
                self._err(p, {"peer_offer": it})
                continue
            oid = self.next_offer
            self.next_offer += 1
            self.offers[oid] = {"id": oid, "seller": seat, "city": c, "units": float(u),
                                "price": float(pr), "ticks": d, "expires": t + DAY}
            p.res.append({"peer_offer": oid})
        for oid in items("peer_accept"):
            off = self.offers.get(oid)
            if off is None or off["seller"] == seat or off["expires"] < t:
                self._err(p, {"peer_accept": oid})
                continue
            s = self.players[off["seller"]]
            if not (self.pop_built(p, off["city"]) and self.pop_built(s, off["city"])):
                self._err(p, {"peer_accept": oid, "why": "pop"})
                continue
            del self.offers[oid]
            ag = dict(off)
            ag["buyer"] = seat
            ag["start"], ag["end"] = t, t + off["ticks"]
            self.agreements[oid] = ag
            p.dirty = s.dirty = True
            p.res.append({"peer_start": oid})
            s.res.append({"peer_start": oid})
        for c in items("ups"):
            if not city_ok(c) or c not in p.pops or p.pops[c]["ups"] or p.cash - UPS_COST < CREDIT:
                self._err(p, {"ups": c})
                continue
            p.cash -= UPS_COST
            p.tot["capex"] += UPS_COST
            p.pops[c]["ups"] = True
            a = [UPS_COST, t]
            p.pops[c]["assets"].append(a)
            p.assets.append(a)
        if "scrub" in act and isinstance(act["scrub"], list):
            p.scrub = {c for c in act["scrub"] if city_ok(c) and c in p.pops}

    def _set_route(self, p, it):
        if not isinstance(it, dict):
            self._err(p, {"route": "bad"})
            return
        a, b, c, paths, split = it.get("src"), it.get("dst"), it.get("cls"), it.get("paths"), it.get("split")
        if not (isinstance(a, int) and isinstance(b, int) and 0 <= a < self.n and 0 <= b < self.n and a != b):
            self._err(p, {"route": [a, b], "why": "src/dst"})
            return
        cl = [0, 1, 2] if c == "all" else ([CLS.index(c)] if c in CLS else None)
        if cl is None or not isinstance(paths, list) or len(paths) > 4:
            self._err(p, {"route": [a, b], "why": "cls/paths"})
            return
        flip = a > b
        lo, hi = (b, a) if flip else (a, b)
        if not paths:
            for k in cl:
                p.routes.pop((lo, hi, k), None)
            p.dirty = True
            return
        if split is None:
            split = [1.0] * len(paths)
        if not isinstance(split, list) or len(split) != len(paths):
            self._err(p, {"route": [a, b], "why": "split"})
            return
        out = []
        for nodes, s in zip(paths, split):
            if (not isinstance(nodes, list) or len(nodes) < 2 or nodes[0] != a or nodes[-1] != b
                    or not isinstance(s, (int, float)) or s < 0):
                self._err(p, {"route": [a, b], "why": "path"})
                return
            ls = []
            for u, v in zip(nodes, nodes[1:]):
                l = self.edge.get((u, v))
                if l is None:
                    self._err(p, {"route": [a, b], "why": f"no link {u}-{v}"})
                    return
                ls.append(l)
            if flip:
                ls.reverse()
            out.append((tuple(ls), float(s)))
        tot = sum(s for _, s in out)
        if tot <= 0:
            self._err(p, {"route": [a, b], "why": "split sum"})
            return
        out = [(ls, s / tot) for ls, s in out]
        for k in cl:
            p.routes[(lo, hi, k)] = out
        p.dirty = True

    def _drop_lease(self, p, lid):
        ls = p.leases.pop(lid)
        p.amort -= ls["cost"] / ls["term_ticks"]
        p.lease_units[ls["link"]] -= ls["units"]
        self.leased_total[ls["link"]] -= ls["units"]
        if p.lease_units[ls["link"]] < 1e-9:
            p.lease_units[ls["link"]] = 0.0
        if self.leased_total[ls["link"]] < 1e-9:
            self.leased_total[ls["link"]] = 0.0
        p.leases_changed = True
        p.dirty = True     # 互联卖方路径依赖租约网络

    def _settle_leases(self, reqs):
        t = self.t
        by_link = {}
        for r in reqs:
            by_link.setdefault(r[2], []).append(r)
        for l, rs in by_link.items():
            price = self.lease_price(l)
            avail = max(0.0, self.leasable[l] - self.leased_total[l])
            ok = [r for r in rs if r[5] >= price]
            for r in rs:
                if r[5] < price:
                    self.players[r[0]].res.append({"lease": r[1], "ok": False, "why": "price",
                                                   "price": round(price, 6)})
            ok.sort(key=lambda r: -r[5])
            i = 0
            while i < len(ok):
                j = i
                while j < len(ok) and ok[j][5] == ok[i][5]:
                    j += 1
                grp = ok[i:j]
                grants = {id(r): 0.0 for r in grp}
                rem = avail
                pending = list(grp)
                while pending and rem > 1e-9:          # 平分（不超过各自请求量）
                    share = rem / len(pending)
                    nxt = []
                    for r in pending:
                        g = min(share, r[3] - grants[id(r)])
                        grants[id(r)] += g
                        rem -= g
                        if r[3] - grants[id(r)] > 1e-9:
                            nxt.append(r)
                    if len(nxt) == len(pending):
                        break
                    pending = nxt
                for r in grp:
                    g = grants[id(r)]
                    p = self.players[r[0]]
                    if g < 1e-6:
                        p.res.append({"lease": r[1], "ok": False, "why": "sold out"})
                        continue
                    term_ticks = r[4] * DAY
                    cost = price * LEASE_TERMS[r[4]] * g * term_ticks
                    if p.cash - cost < CREDIT:
                        p.res.append({"lease": r[1], "ok": False, "why": "cash"})
                        continue
                    p.cash -= cost
                    p.tot["lease"] += cost
                    lid = self.next_lease
                    self.next_lease += 1
                    p.leases[lid] = {"id": lid, "link": l, "units": g, "start": t,
                                     "end": t + term_ticks, "cost": cost, "term_ticks": term_ticks}
                    p.lease_units[l] += g
                    p.amort += cost / term_ticks
                    self.leased_total[l] += g
                    avail -= g
                    p.leases_changed = True
                    p.dirty = True
                    p.res.append({"lease": r[1], "ok": True, "id": lid, "units": round(g, 3),
                                  "price": round(price, 6), "cost": round(cost, 2)})
                i = j

    # ---------------------------------------------------------- 路由计划
    def _build_plan(self, p):
        o = self.players[1 - p.seat]
        pops = sorted(c for c in p.pops if self.pop_built(p, c))
        path_ix = {}
        paths = []           # (own_links, seller_links, seller_seat)

        def pid(own, sell=(), seller=-1):
            key = (own, sell, seller)
            i = path_ix.get(key)
            if i is None:
                i = path_ix[key] = len(paths)
                paths.append(key)
            return i

        def hitrate(c, cat=None):
            q = p.pops[c]
            al = q["alloc"]
            if not al:
                return 0.0
            if cat is not None:
                a = al.get(cat, 0.0)
                return a / (a + CAT_HALF)
            w = self.popw[self.region[c]]
            return sum(w[k] * a / (a + CAT_HALF) for k, a in al.items())

        hit = {c: hitrate(c) for c in pops}
        cvol = {}
        for k in p.contracts:
            if k["start"] <= self.t < k["end"]:
                cvol[(k["a"], k["b"], CLS.index(k["cls"]))] = cvol.get((k["a"], k["b"], CLS.index(k["cls"])), 0) + k["units"]
        entries = []

        def group_routes(a, b):
            res = []
            dflt = None
            for k in range(3):
                r = p.routes.get((a, b, k))
                if r:
                    res.append([(pid(ls), s) for ls, s in r])
                else:
                    if dflt is None:
                        ls = self.path_links(a, b)
                        dflt = [(pid(ls), 1.0)] if ls is not None else []
                    res.append(dflt)
            return res

        for i, a in enumerate(pops):
            for b in pops[i + 1:]:
                sa, sb = p.share[a], p.share[b]
                D = self.base_pair(a, b)
                base = [D * CLS_FRAC[k] * 0.5 * (sa[k] + sb[k]) for k in range(3)]
                evs = []
                for c in (a, b):
                    for em, cat in self.ev_mult.get(c, ()):
                        evs.append((em, 0.5 * (hitrate(a, cat) + hitrate(b, cat))))
                cv = [cvol.get((a, b, k), 0) for k in range(3)]
                if sum(base) <= 0 and not any(cv):
                    continue
                ri, rj = self.region[a], self.region[b]
                pv = [0.5 * (p.prices[ri][k] + p.prices[rj][k]) * CLS_PRICE[k] / REV_DIV for k in range(3)]
                entries.append([a, b, ri, rj, base, 0.5 * (hit[a] + hit[b]), evs, cv,
                                group_routes(a, b), pv, None])
        # 互联：买方经互联城市 c 借卖方网络到达卖方独有的 PoP 城市
        for ag in self.agreements.values():
            if ag["buyer"] != p.seat or not (ag["start"] <= self.t < ag["end"]):
                continue
            c = ag["city"]
            if not (self.pop_built(p, c) and self.pop_built(o, c)):
                continue
            targets = [j for j in o.pops if self.pop_built(o, j) and j not in p.pops and j != c]
            for j in targets:
                seg = self.lease_path(o.seat, c, j)
                if seg is None:
                    continue
                for a in pops:
                    sa = p.share[a]
                    D = self.base_pair(a, j)
                    base = [D * CLS_FRAC[k] * 0.5 * sa[k] for k in range(3)]
                    if sum(base) <= 0:
                        continue
                    if a == c:
                        rt = [[(pid((), seg, o.seat), 1.0)] for _ in range(3)]
                    else:
                        lo, hi = min(a, c), max(a, c)
                        rt = []
                        for k in range(3):
                            r = p.routes.get((lo, hi, k))
                            if r:
                                own = [ls if a == lo else tuple(reversed(ls)) for ls, _ in r]
                                rt.append([(pid(ls, seg, o.seat), s) for ls, (_, s) in zip(own, r)])
                            else:
                                ls = self.path_links(a, c)
                                rt.append([(pid(ls, seg, o.seat), 1.0)] if ls is not None else [])
                    ri, rj = self.region[a], self.region[j]
                    pv = [0.5 * (p.prices[ri][k] + p.prices[rj][k]) * CLS_PRICE[k] / REV_DIV for k in range(3)]
                    entries.append([a, j, ri, rj, base, 0.5 * hit[a], [], [0, 0, 0], rt, pv, ag["id"]])
        p.plan = {"entries": entries, "paths": paths}
        p.dirty = False

    # ---------------------------------------------------------- 一个 tick
    def step(self, actions):
        t0 = time.perf_counter()
        t = self.t
        P = self.players
        for p in P:
            p.res = []
        anns = self._process_events()
        for x in anns:
            if x.get("type") not in ("repair",):
                self.replay["events"].append({"t": t, **x})
        lease_reqs = []
        for seat in (0, 1):
            self._apply_actions(seat, actions[seat] if actions else {}, lease_reqs)
        self._settle_leases(lease_reqs)
        for p in P:
            for lid in [lid for lid, ls in p.leases.items() if ls["end"] <= t]:
                self._drop_lease(p, lid)
            for c, q in p.pops.items():
                if q["ready"] == t:
                    p.dirty = True
                    if max(p.share[c]) <= 0:
                        p.share[c] = [NEW_SHARE] * 3
                if q["port_pend"]:
                    keep = []
                    for (tt, u) in q["port_pend"]:
                        if tt <= t:
                            q["port"] += u
                        else:
                            keep.append((tt, u))
                    q["port_pend"] = keep
        for oid in [oid for oid, o in self.offers.items() if o["expires"] < t]:
            del self.offers[oid]
        for aid in [a for a, ag in self.agreements.items() if ag["end"] <= t]:
            ag = self.agreements.pop(aid)
            P[ag["buyer"]].dirty = P[ag["seller"]].dirty = True
        self._network()
        self._costs()
        if t % DAY == DAY - 1:
            self._daily()
        obs = [self._obs(s, anns) for s in (0, 1)]
        self.spot_prev = self._spot_now
        self.t += 1
        self.engine_time += time.perf_counter() - t0
        return obs

    def _network(self):
        t = self.t
        F = self.F[t]
        P = self.players
        m = self.m
        loads = [dict(), dict()]
        port = [dict(), dict()]
        flows = []
        for p in P:
            if p.dirty or p.plan is None:
                self._build_plan(p)
        for p in P:
            plan = p.plan
            pl = [0.0] * len(plan["paths"])
            gl = []
            pt = port[p.seat]
            dq = p.defer_req
            peer_units = {}
            for e in plan["entries"]:
                a, b, ri, rj, base, hit, evs, cv, routes, pv, ag = e
                tf = 0.5 * (F[ri] + F[rj])
                dv = base[0] * tf
                v_tot, v_bb = dv, dv * (1 - hit)
                for em, h in evs:
                    x = dv * em
                    v_tot += x
                    v_bb += x * (1 - h)
                v_tot += cv[0]
                v_bb += cv[0] * (1 - hit)
                drt = base[1] * tf + cv[1]
                dbk = base[2] * tf + cv[2]
                bk_send = dbk
                key = (a, b)
                if key in dq or key in p.queues:
                    req = dq.get(key, 0.0)
                    qd = p.queues.get(key)
                    if req > 0:
                        dfr = min(req, dbk)
                        bk_send -= dfr
                        if qd is None:
                            qd = p.queues[key] = deque()
                        qd.append([t, dfr])
                    elif req < 0 and qd:
                        want = -req
                        while qd and want > 1e-12:
                            x = min(want, qd[0][1])
                            qd[0][1] -= x
                            want -= x
                            bk_send += x
                            if qd[0][1] <= 1e-12:
                                qd.popleft()
                units = (v_bb, drt, bk_send)
                tot_bb = v_bb + drt + bk_send
                for k in range(3):
                    u = units[k]
                    if u > 0:
                        for i, s in routes[k]:
                            pl[i] += u * s
                pt[a] = pt.get(a, 0.0) + tot_bb
                if ag is None:
                    pt[b] = pt.get(b, 0.0) + tot_bb
                else:
                    peer_units[ag] = peer_units.get(ag, 0.0) + tot_bb
                gl.append((e, v_tot, v_bb, drt, bk_send, dbk))
            flows.append((pl, gl, peer_units))
            for i, ld in enumerate(pl):
                if ld <= 0:
                    continue
                own, sell, seller = plan["paths"][i]
                L = loads[p.seat]
                for l in own:
                    L[l] = L.get(l, 0.0) + ld
                if sell:
                    L2 = loads[seller]
                    for l in sell:
                        L2[l] = L2.get(l, 0.0) + ld
        # 链路状态
        lat = self.lat
        over = [0.0] * m
        eff = [dict(), dict()]
        lease_part = [dict(), dict()]
        for s in (0, 1):
            lu = P[s].lease_units
            for l, ld in loads[s].items():
                cf = self.capf(l)
                el = lu[l] * cf
                lp = ld if ld < el else el
                lease_part[s][l] = (lp, el)
                over[l] += ld - lp
        spot_rho = [0.0] * m
        for l in range(m):
            cf = self.capf(l)
            ra = self.lreg[l][0]
            bg = self.spotcap[l] * self.bg_frac[l] * F[ra]
            sc = self.spotcap[l] * cf
            spot_rho[l] = (bg + over[l]) / sc if sc > 0 else 99.0
        for s in (0, 1):
            E = eff[s]
            for l, ld in loads[s].items():
                lp, el = lease_part[s][l]
                if self.down_cnt[l]:
                    E[l] = (lat[l], 1.0, 0.0)
                    continue
                f = lp / ld if ld > 0 else 1.0
                rho = spot_rho[l]
                ds = lat[l] / (1 - min(rho, 0.95))
                ls_ = (rho - 1) / rho if rho > 1 else 0.0
                u = lp / el if el > 0 else 0.0
                dl = lat[l] + 0.5 * u / (1 - min(u, 0.95))
                E[l] = (f * dl + (1 - f) * ds, (1 - f) * ls_, (1 - f) * (ds - lat[l]))
        self._spot_now = spot_rho
        self._loads = loads
        # 端口
        pfac = [dict(), dict()]
        for p in P:
            s = p.seat
            for c, q in p.pops.items():
                ld = port[s].get(c, 0.0)
                if not self.pop_ok(p, c):
                    pfac[s][c] = 0.0
                    continue
                if (s, c) in self.ddos and c not in p.scrub:
                    ld = ld * DDOS_MULT if ld > 0 else q["port"] * 0.5
                pfac[s][c] = 1.0 if ld <= q["port"] else q["port"] / ld
            self._port_now = port
        # 路径指标 + 计费
        for p, (pl, gl, peer_units) in zip(P, flows):
            s = p.seat
            plan = p.plan
            pm = [None] * len(plan["paths"])
            E = eff[s]
            for i, (own, sell, seller) in enumerate(plan["paths"]):
                d = 0.0
                sv = 1.0
                jt = 0.0
                for l in own:
                    x = E.get(l)
                    if x is None:
                        x = (lat[l], 1.0 if self.down_cnt[l] else 0.0, 0.0)
                    d += x[0]
                    sv *= 1 - x[1]
                    jt += x[2]
                if sell:
                    E2 = eff[seller]
                    for l in sell:
                        x = E2.get(l) or (lat[l], 1.0 if self.down_cnt[l] else 0.0, 0.0)
                        d += x[0]
                        sv *= 1 - x[1]
                        jt += x[2]
                pm[i] = (d, sv, jt)
            pscale = {}
            for ag, u in peer_units.items():
                k = self.agreements.get(ag)
                pscale[ag] = 1.0 if (k is None or u <= k["units"]) else k["units"] / u
            pf = pfac[s]
            rev = 0.0
            stats = [[0.0, 0.0, 0.0] for _ in range(3)]  # dem, good, rev
            hour = p.hour
            num, den = p.sla_num, p.sla_den
            cg, cd = {}, {}
            for (e, v_tot, v_bb, drt, bk_send, dbk) in gl:
                a, b, ri, rj, base, hit, evs, cv, routes, pv, ag = e
                fa = pf.get(a, 0.0)
                fb = pf.get(b, 1.0) if ag is not None else pf.get(b, 0.0)
                port_f = fa * fb * (pscale.get(ag, 1.0) if ag is not None else 1.0)
                local = v_tot - v_bb
                dems = (v_tot, drt, dbk)
                goods = [0.0, 0.0, 0.0]
                lat_acc = [0.0, 0.0, 0.0]
                loss_acc = [0.0, 0.0, 0.0]
                for k in range(3):
                    u = (v_bb, drt, bk_send)[k]
                    lim = CLS_LIMIT[k]
                    g = 0.0
                    if u > 0:
                        rk = routes[k]
                        if not rk:
                            g = 0.0
                            lat_acc[k] = 999.0
                            loss_acc[k] = 1.0
                        else:
                            mean_l = 0.0
                            jsum = 0.0
                            for i, sp in rk:
                                d, sv, jt = pm[i]
                                f = 1.0
                                if lim is not None and d > lim:
                                    f = 2 - d / lim
                                    if f < 0:
                                        f = 0.0
                                g += sp * sv * f
                                mean_l += sp * d
                                jsum += sp * jt
                                loss_acc[k] += sp * (1 - sv)
                            lat_acc[k] = mean_l
                            if k == 1:
                                var = sum(sp * (pm[i][0] - mean_l) ** 2 for i, sp in rk)
                                if math.sqrt(var) + 0.5 * jsum > JITTER_LIMIT:
                                    g *= 0.8
                        g *= u * port_f
                    if k == 0:
                        g += local * (1.0 if fa > 0 and (fb > 0) else 0.0)
                    goods[k] = g
                gs = goods[0] + goods[1] + goods[2]
                ds = v_tot + drt + bk_send
                cg[a] = cg.get(a, 0.0) + gs
                cd[a] = cd.get(a, 0.0) + ds
                if ag is None:
                    cg[b] = cg.get(b, 0.0) + gs
                    cd[b] = cd.get(b, 0.0) + ds
                for k in range(3):
                    dm = dems[k] if k != 2 else bk_send
                    if dm <= 0 and goods[k] <= 0:
                        continue
                    r = goods[k] * pv[k]
                    rev += r
                    st = stats[k]
                    st[0] += dems[k]
                    st[1] += goods[k]
                    st[2] += r
                    num[a][k] += goods[k]
                    den[a][k] += dm
                    if ag is None:
                        num[b][k] += goods[k]
                        den[b][k] += dm
                    hk = (a, b, k)
                    h = hour.get(hk)
                    if h is None:
                        h = hour[hk] = [0.0, 0.0, 0.0, 0.0]
                    h[0] += dm
                    h[1] += goods[k]
                    h[2] += lat_acc[k] * dm
                    h[3] += loss_acc[k] * dm
                # 合同质量
                if any(cv):
                    for kc in p.contracts:
                        if kc["a"] == a and kc["b"] == b and kc["start"] <= t < kc["end"]:
                            k = CLS.index(kc["cls"])
                            if dems[k] > 0:
                                kc["dem"] += 1.0
                                kc["good"] += goods[k] / dems[k]
                # 视频热度日志（只有自己 PoP 的流量看得到）
                if v_tot > 0 and ag is None:
                    vv = p.vidvol
                    vv[a] = vv.get(a, 0.0) + v_tot
                    vv[b] = vv.get(b, 0.0) + v_tot
                    if evs:
                        for c in (a, b):
                            for em, cat in self.ev_mult.get(c, ()):
                                ex = p.evheat.setdefault(c, {})
                                ex[cat] = ex.get(cat, 0.0) + v_tot * em / (1 + em)
            # 断线：城市本 tick 送达不足一半记为"断"，由通转断计一次
            for c, dsum in cd.items():
                down = dsum > 0 and cg[c] < 0.5 * dsum
                if down and not p.down_state.get(c):
                    p.outages[c] += 1
                p.down_state[c] = down
            p.cash += rev
            p.day_rev += rev
            p.tot["rev"] += rev
            tt = p.tot
            for k in range(3):
                tt["dem_" + CLS[k]] += stats[k][0]
                tt["good_" + CLS[k]] += stats[k][1]
                tt["rev_" + CLS[k]] += stats[k][2]
            p.tick_stats = stats
        # 备份队列超时
        for p in P:
            for key, qd in list(p.queues.items()):
                while qd and qd[0][0] <= t - DEFER_MAX:
                    _, u = qd.popleft()
                    a, b = key
                    p.sla_den[a][2] += u
                    p.sla_den[b][2] += u
                if not qd:
                    del p.queues[key]

    def _costs(self):
        t = self.t
        for p in self.players:
            if p.bankrupt:
                p.scrub = set()          # 破产后动作作废，清洗也停
            op = 0.0
            for c, q in p.pops.items():
                if q["ready"] <= t:
                    op += self.cities[c]["land"] * POP_OP_K * self.cities[c]["elec"] / DAY
                op += q["cache"] * CACHE_OP_DAY / DAY
                if c in p.scrub:
                    op += SCRUB_COST
            amort = p.amort
            p.cash -= op
            p.tot["opex"] += op
            p.day_opex += op + amort
            for ag in self.agreements.values():
                if ag["buyer"] == p.seat and ag["start"] <= t < ag["end"]:
                    x = ag["units"] * ag["price"]
                    p.cash -= x
                    p.day_opex += x
                    p.tot["peer_out"] += x
                    s = self.players[ag["seller"]]
                    s.cash += x
                    s.day_rev += x
                    s.tot["peer_in"] += x
            # 合同结算
            for k in list(p.contracts):
                if k["start"] <= t < k["end"] and (t - k["start"]) % DAY == DAY - 1:
                    x = k["price"] / CONTRACT_DAYS
                    p.cash += x
                    p.day_rev += x
                    p.tot["contract_in"] += x
                if t == k["end"] - 1 or (t == self.T - 1 and t < k["end"]):
                    q = k["good"] / k["dem"] if k["dem"] else 1.0
                    k["quality"] = q
                    if q < CONTRACT_SLA:
                        frac = min(1.0, (t + 1 - k["start"]) / (k["end"] - k["start"]))
                        pen = CONTRACT_PENALTY * k["price"] * frac
                        k["penalty"] = pen
                        p.cash -= pen
                        p.day_opex += pen
                        p.tot["penalty"] += pen
                    p.contracts.remove(k)
                    p.dirty = True
            if p.cash < CREDIT and not p.bankrupt:
                p.bankrupt = True
                p.res.append({"bankrupt": True})

    def _daily(self):
        t = self.t
        P = self.players
        for p in P:
            gp = p.day_rev - p.day_opex
            p.gp_hist.append(gp)
            p.day_rev = p.day_opex = 0.0
            p.sla_hist.append(([row[:] for row in p.sla_num], [row[:] for row in p.sla_den], p.outages[:]))
            for c in range(self.n):
                p.sla_num[c] = [0.0, 0.0, 0.0]
                p.sla_den[c] = [0.0, 0.0, 0.0]
            p.outages = [0] * self.n
            p.price_changes = [[0, 0, 0] for _ in range(6)]
        # 份额迁移（logit）
        cities = set()
        for p in P:
            cities.update(c for c in p.pops)
            cities.update(c for c in range(self.n) if max(p.share[c]) > 0)
        last3 = [list(p.sla_hist)[-3:] for p in P]
        for c in sorted(cities):
            r = self.region[c]
            for k in range(3):
                s_np = 1 - P[0].share[c][k] - P[1].share[c][k]
                us = []
                for p in P:
                    if p.bankrupt or not self.pop_built(p, c):
                        us.append(None)
                        continue
                    hs = last3[p.seat]
                    num = sum(h[0][c][k] for h in hs)
                    den = sum(h[1][c][k] for h in hs)
                    sla = num / den if den > 1e-9 else 0.9
                    outs = sum(h[2][c] for h in hs)
                    us.append(-BETA * p.prices[r][k] + GAMMA * sla - DELTA * outs + ETA * p.share[c][k])
                u_np = -BETA * 1.0 + GAMMA * NPC_SLA + ETA * s_np
                ex = [math.exp(u) if u is not None else 0.0 for u in us]
                en = math.exp(u_np)
                tot = sum(ex) + en
                for i, p in enumerate(P):
                    cur = p.share[c][k]
                    if us[i] is None:
                        new = max(0.0, cur - MIG_MAX) if p.bankrupt else (cur if c in p.pops else 0.0)
                    else:
                        tgt = ex[i] / tot
                        new = cur + max(-MIG_MAX, min(MIG_MAX, tgt - cur))
                    p.share[c][k] = max(0.0, new)
                ssum = P[0].share[c][k] + P[1].share[c][k]
                if ssum > 1:
                    P[0].share[c][k] /= ssum
                    P[1].share[c][k] /= ssum
        for p in P:
            p.heat_out = {}
            for c, v in p.vidvol.items():
                w = self.popw[self.region[c]]
                hv = [v * w[k] * (1 + self.rng_obs.gauss(0, 0.1)) for k in range(N_CAT)]
                for cat, x in p.evheat.get(c, {}).items():
                    hv[cat] += x
                p.heat_out[str(c)] = [round(max(0.0, x), 1) for x in hv]
            p.vidvol, p.evheat = {}, {}
        self._drift_popularity()
        for p in P:
            p.dirty = True
        self._day_record()

    # ---------------------------------------------------------- 净资产
    def net_worth(self, seat):
        p = self.players[seat]
        t = self.t
        book = sum(cost * max(0.0, 1 - (t - t0) / YEAR) for cost, t0 in p.assets)
        lease = sum(ls["cost"] * max(0, ls["end"] - t) / ls["term_ticks"] for ls in p.leases.values())
        last = p.gp_hist[-7:]
        cust = max(0.0, sum(last) / len(last)) * 14 if last else 0.0
        return {"nw": p.cash + book + RELEASE_REFUND * lease + cust, "cash": p.cash, "book": book,
                "lease": RELEASE_REFUND * lease, "customer": cust}

    def _day_record(self):
        day = self.t // DAY
        rec = {"day": day, "t": self.t}
        for s, p in enumerate(self.players):
            nw = self.net_worth(s)
            shares = [sum(p.share[c][k] * self.cities[c]["pop"] for c in range(self.n)) for k in range(3)]
            rec["AB"[s]] = {"nw": round(nw["nw"], 1), "cash": round(p.cash, 1),
                            "customer": round(nw["customer"], 1), "gp": round(p.gp_hist[-1], 1),
                            "pops": sorted(p.pops), "leases": len(p.leases),
                            "lease_units": round(sum(p.lease_units), 1),
                            "share_popw": [round(x, 4) for x in shares], "bankrupt": p.bankrupt,
                            "prices": [[round(v, 3) for v in row] for row in p.prices]}
        self.replay["days"].append(rec)

    # ---------------------------------------------------------- 观测
    def init_msg(self, seat):
        g = self.map
        me, op = ("A", "B") if seat == 0 else ("B", "A")
        return {"type": "init", "seat": seat, "ticks": self.T,
                "map": {"cities": g["cities"], "links": g["links"], "regions": g["regions"]},
                "start": {"you": g["start"][me], "opp": g["start"][op]},
                "rules": {k: v for k, v in RULE_CONSTS.items()}}

    def _obs(self, seat, anns):
        t = self.t
        p = self.players[seat]
        o = self.players[1 - seat]
        rng = self.rng_obs
        spot = [round(x * (1 + rng.uniform(-0.1, 0.1)), 3) for x in self.spot_prev]
        ev = list(anns)
        for off in self.offers.values():
            if off.get("_shown_" + str(seat)) is None and off["seller"] != seat:
                off["_shown_" + str(seat)] = True
                ev.append({"type": "peer_offer", **{k: off[k] for k in ("id", "city", "units", "price", "ticks", "expires")}})
        L = self._loads[seat]
        ob = {
            "t": t, "cash": round(p.cash, 2), "bankrupt": p.bankrupt,
            "spot": spot,
            "lp": [round(self.lease_price(l), 6) for l in range(self.m)],
            "avail": [round(self.leasable[l] - self.leased_total[l], 2) for l in range(self.m)],
            "down": [l for l in range(self.m) if self.down_cnt[l]],
            "ev": ev, "res": p.res,
            "pops": [[c, q["ready"], q["port"], q["cache"], int(q["ups"]), int(c in p.scrub)]
                     for c, q in sorted(p.pops.items())],
            "opp_pops": sorted(o.pops),
            "load": {str(l): round(v, 2) for l, v in L.items() if v > 0.005},
            "own_lease": {str(l): round(p.lease_units[l], 2) for l in range(self.m) if p.lease_units[l] > 0},
            "port": {str(c): round(v, 2) for c, v in self._port_now[seat].items()},
            "stat": [[round(x, 3) for x in st] for st in p.tick_stats],
            "ddos": [c for (s, c) in self.ddos if s == seat],
            "queued": {f"{a}-{b}": round(sum(x[1] for x in q), 2) for (a, b), q in p.queues.items()},
        }
        if p.leases_changed:
            ob["leases"] = [[ls["id"], ls["link"], round(ls["units"], 3), ls["end"]] for ls in p.leases.values()]
            p.leases_changed = False
        if t % 6 == 5:
            ob["groups"] = [[a, b, CLS[k], round(h[0] / 6, 3), round(h[1] / h[0], 4) if h[0] else 0,
                             round(h[2] / h[0], 2) if h[0] else 0, round(h[3] / h[0], 4) if h[0] else 0]
                            for (a, b, k), h in p.hour.items()]
            p.hour = {}
        if t % DAY == DAY - 1:
            sh = {}
            for c in range(self.n):
                a, b = self.players[0].share[c], self.players[1].share[c]
                if max(a) > 0 or max(b) > 0:
                    sh[str(c)] = [[round(x, 4) for x in a], [round(x, 4) for x in b]]
            heat = p.heat_out
            ob["day"] = {"shares": sh,
                         "prices": [[[round(v, 4) for v in row] for row in q.prices] for q in self.players],
                         "heat": heat, "gp": round(p.gp_hist[-1], 2),
                         "sla": round(self.sla7(p), 4)}
        return ob


RULE_CONSTS = {
    "DAY": DAY, "TICKS": TICKS, "CLS": CLS, "CLS_FRAC": CLS_FRAC, "CLS_LIMIT": CLS_LIMIT,
    "CLS_PRICE": CLS_PRICE, "REV_DIV": REV_DIV, "DEFER_MAX": DEFER_MAX, "START_CASH": START_CASH,
    "CREDIT": CREDIT, "MAX_POPS": MAX_POPS, "POP_BUILD_K": POP_BUILD_K, "BUILD_TICKS": BUILD_TICKS,
    "POP_OP_K": POP_OP_K, "PORT0": PORT0, "EXPAND_UNITS": EXPAND_UNITS, "EXPAND_COST": EXPAND_COST,
    "EXPAND_TICKS": EXPAND_TICKS, "LEASE_TERMS": {str(k): v for k, v in LEASE_TERMS.items()},
    "LEASE_SHARE": LEASE_SHARE, "SPOT_SHARE": SPOT_SHARE, "CACHE_COST": CACHE_COST,
    "CACHE_OP_DAY": CACHE_OP_DAY, "N_CAT": N_CAT, "CAT_HALF": CAT_HALF, "UPS_COST": UPS_COST,
    "SCRUB_COST": SCRUB_COST, "INIT_SHARE": INIT_SHARE, "NEW_SHARE": NEW_SHARE, "MIG_MAX": MIG_MAX,
    "BETA": BETA, "GAMMA": GAMMA, "DELTA": DELTA, "ETA": ETA, "NPC_SLA": NPC_SLA,
    "PRICE_MIN": PRICE_MIN, "PRICE_MAX": PRICE_MAX, "PRICE_STEP": PRICE_STEP,
    "PRICE_CHANGES": PRICE_CHANGES, "DEM_K": DEM_K, "DEM_ALPHA": DEM_ALPHA,
    "TREND_PER_DAY": TREND_PER_DAY, "WEEKLY": WEEKLY, "CONTRACT_SLA": CONTRACT_SLA,
    "CONTRACT_PENALTY": CONTRACT_PENALTY, "STEP_TIMEOUT": STEP_TIMEOUT, "TIME_POOL": TIME_POOL, "TOTAL_BUDGET": TOTAL_BUDGET,
}


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
        r = self.ask(init, timeout=INIT_TIMEOUT)
        if r is None and not self.dead:
            pass

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


# ================================================================= 整局
def play(path_a: str, path_b: str, seed: int, ticks: int = TICKS, gmap: dict | None = None,
         inproc: bool = False, log=None) -> dict:
    """跑一局。path_a 坐 A 位，path_b 坐 B 位。返回结果 dict（含 replay）。

    inproc=True：把 bot 文件当模块导入、在本进程里直接调用 make_bot()（仅供开发调参，
    正式评测一律子进程）。"""
    gmap = gmap or mapgen.generate(seed)
    g = Game(gmap, seed, ticks)
    wall0 = time.perf_counter()
    bots = []
    for seat, path in enumerate((path_a, path_b)):
        if inproc:
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
            r = bots[seat].ask(obs[seat])
            acts.append(r if isinstance(r, dict) else {})
        if log and t % DAY == DAY - 1:
            log(t, g)
    for seat in (0, 1):
        bots[seat].ask({"type": "end", "t": ticks})
        bots[seat].kill()
    wall = time.perf_counter() - wall0
    nw = [g.net_worth(s) for s in (0, 1)]
    res = {
        "seed": seed, "ticks": ticks, "A": os.path.basename(path_a), "B": os.path.basename(path_b),
        "nw": [round(x["nw"], 2) for x in nw], "nw_detail": [{k: round(v, 2) for k, v in x.items()} for x in nw],
        "faults": [{"timeouts": b.timeouts, "bad_json": b.bad, "dead": b.dead,
                    "illegal": g.players[i].faults["illegal"], "think_s": round(b.used, 2), "pool_left_s": round(getattr(b, "pool", TIME_POOL), 2),
                    "max_step_s": round(getattr(b, "max_step", 0.0), 3)}
                   for i, b in enumerate(bots)],
        "totals": [{k: round(v, 1) for k, v in p.tot.items()} for p in g.players],
        "pops": [sorted(p.pops) for p in g.players],
        "engine_s": round(g.engine_time, 2), "wall_s": round(wall, 2),
        "replay": g.replay,
    }
    return res


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

    def ask(self, msg):
        if msg.get("type") == "end":
            return {}
        t0 = time.perf_counter()
        r = self.bot.step(json.loads(json.dumps(msg)))
        dt = time.perf_counter() - t0
        self.used += dt
        self.max_step = max(getattr(self, "max_step", 0.0), dt)
        return json.loads(json.dumps(r))

    def kill(self):
        pass


def s_share(nw_x, nw_r):
    return 0.5 + 0.5 * math.tanh((nw_x - nw_r) / NW_SCALE)
