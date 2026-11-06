"""hark_strong：骨干争夺（Backbone Wars）校准用强对手。单文件、仅标准库，协议与正式提交相同。

只使用 init 与每 tick 观测里给出的信息（地图、规则常量、公告事件、自己的负载/租约/份额等），
不读文件、不联网、不起子进程。

策略要点
  1. 选址：按引力需求 × 业务可达性（rt 受延迟上限约束、跨半图海缆只算 video/backup）
     × 竞争折扣 贪心选城，现金允许就建，尽快铺满 20 座 PoP（建站支出计入账面，只损失折旧）。
  2. 容量感知多路径流量工程：按城市对峰值需求从大到小，在“自有租量 + 可租余量”的残量图上
     最多找 4 条低延迟路径分流；rt 单独走首条路径（无抖动），video/backup 按残量分流。
  3. 租约：按规划峰值流量 × 余量一次性成批租入；期限按剩余局长在 28/7/1 天之间选成本最低者；
     每 6 tick 再按实测负载补租，避免溢出到拥塞的现货池。
  4. 端口按预测/实测峰值提前扩容；DDoS 时按损失估计决定是否开清洗。
  5. 计划维护提前绕行，故障即时局部重路由。
  6. 大客户合同：两端都有自有 PoP、路径延迟满足类别上限、近 7 天 SLA 充足时投标；
     中标后把合同流量计入规划并优先保障。
  7. 拥塞定价：按规划的峰值溢出率分大区调价。拥塞时先把单位容量收入最低的 backup 提价
     （最高 4.0）、video 小步提价（最高 1.6），把稀缺容量让给 rt/video；不拥塞时回落到 0.95/1.0。
  8. 终局：倒数第 12 天起 4 天把 video/rt 降到 0.6 抬份额；倒数第 8 天起每天两次 ×1.3 提价直至 5.0
     （终局净资产含最后 7 天日均毛利 × 14，最后一周每天的毛利实际按 3 倍计入）；
     最后阶段按实测峰值退掉多余租约（退款 50%，并减少计入毛利的租约摊销）。
"""
import heapq
import json
import math
import sys

DAY = 144
CLS = ("video", "rt", "backup")
FRAC = (0.60, 0.15, 0.25)
CPRICE = (1.0, 4.0, 0.4)
WEEKLY = (1.0, 1.0, 1.0, 1.0, 1.05, 1.15, 1.10)


def diurnal(h):
    return 1.0 + 0.55 * math.cos(2 * math.pi * (h - 21.0) / 24.0)


class Bot:
    P = {"vid_base": 0.95, "rt_base": 0.95, "bk_base": 1.0, "bk_max": 4.0, "vid_max": 1.6,
         "cong_hi": 0.05, "cong_lo": 0.01, "harvest_days": 8, "harvest_cap": 5.0,
         "pre_days": 4, "pre_price": 0.6, "pre_cls": (0, 1), "k_deliv": 0.995,
         "term28_cash": 15000, "release": 1}

    def __init__(self, init):
        m = init["map"]
        self.R = init["rules"]
        self.T = init.get("ticks", 8064)
        self.seat = init["seat"]
        C = self.cities = m["cities"]
        L = self.links = m["links"]
        self.n, self.m = len(C), len(L)
        self.tz = {r["id"]: r["tz"] for r in m["regions"]}
        self.reg = [c["region"] for c in C]
        self.adj = [[] for _ in range(self.n)]
        self.edge = {}
        for l in L:
            self.adj[l["u"]].append((l["v"], l["id"]))
            self.adj[l["v"]].append((l["u"], l["id"]))
            self.edge[(l["u"], l["v"])] = l["id"]
            self.edge[(l["v"], l["u"])] = l["id"]
        self.lat = [l["lat"] for l in L]
        self.cap = [float(l["cap"]) for l in L]
        self.leasable = [0.7 * c for c in self.cap]
        self.is_mw = [l["type"] == "mw" for l in L]
        self.base = [l["base"] for l in L]
        self.w0 = [self.lat[i] + 0.4 + (3.0 if self.is_mw[i] else 0.0) for i in range(self.m)]
        # 静态最短延迟（选址用）
        self.slat = [self._dij_static(s) for s in range(self.n)]
        # 区域对的日内峰值因子
        self.pk = {}
        for ra in self.tz:
            for rb in self.tz:
                best = 0.0
                for k in range(DAY):
                    h = k / 6.0
                    v = 0.5 * (diurnal((h + self.tz[ra]) % 24) + diurnal((h + self.tz[rb]) % 24))
                    best = max(best, v)
                self.pk[(ra, rb)] = best
        self.start = list(init["start"]["you"])
        self.opp_start = list(init["start"]["opp"])
        self.opp_pops = set(self.opp_start)
        self.share = {c: [0.35, 0.35, 0.35] for c in self.start}
        self.opp_share = {c: [0.35, 0.35, 0.35] for c in self.opp_start}
        self.prices = [[1.0, 1.0, 1.0] for _ in range(6)]
        self.pops = {}            # city -> [ready, port, cache, ups, scrub]
        self.port_pend = {}       # city -> [(ready_t, units)]
        self.port_peak = {}
        self.load_peak = {}
        self.routes = {}          # (a,b) -> (paths(list of node lists), xs, rt_path_idx)
        self.route_links = {}     # (a,b) -> set(links)
        self.sent = {}            # (a,b) -> json key of last route sent
        self.flow = [0.0] * self.m
        self.down = set()
        self.maint = []           # (link, start, end)
        self.last_plan = -10**9
        self.ready_set = ()
        self.contracts = {}       # id -> event
        self.won = []             # [a,b,k,units,end]
        self.sla7 = 1.0
        self.price_changes_day = -1
        self.scrubbing = set()
        self.harvest_from = self.T - int(self.P["harvest_days"] * DAY)
        self.cong = {}
        self.gdel = {}
        self.my_leases = {}
        self.dmax = {}
        self.dmax_prev = {}
        self.capl = [0.0] * self.m
        self.pending_build = set()
        self.cash = 20000.0
        self.need_replan = True

    # ------------------------------------------------------------ 工具
    def _dij_static(self, s):
        dist = [math.inf] * self.n
        dist[s] = 0.0
        pq = [(0.0, s)]
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist[u]:
                continue
            for v, l in self.adj[u]:
                nd = d + self.lat[l] + (2.0 if self.is_mw[l] else 0.0)
                if nd < dist[v]:
                    dist[v] = nd
                    heapq.heappush(pq, (nd, v))
        return dist

    def grav(self, a, b):
        ca, cb = self.cities[a], self.cities[b]
        d = math.hypot(ca["x"] - cb["x"], ca["y"] - cb["y"]) / 1000.0
        return 50000.0 * ca["pop"] * cb["pop"] / max(d, 0.3) ** 1.2

    def pair_weight(self, a, b):
        """每单位基准需求的期望收入权重（相对 video=1）。"""
        lt = self.slat[a][b] * 1.15 + 1.0
        vf = 1.0 if lt <= 150 else max(0.0, 2 - lt / 150)
        rf = 1.0 if lt <= 36 else max(0.0, 2 - lt / 36) * 0.8
        return 0.6 * vf + 0.15 * 4.0 * rf + 0.25 * 0.4

    def city_value(self, c, S):
        sf = 0.55 if c in self.opp_pops else 1.0
        v = 0.0
        for p in S:
            if p == c:
                continue
            sp = 0.55 if p in self.opp_pops else 1.0
            v += self.grav(c, p) * self.pair_weight(c, p) * sf * sp
        return v

    def term_for(self, t):
        rem = self.T - t
        if rem > 2420:
            return 28
        if rem > 672:
            return 7
        return 1

    def peak_factor(self, a, b, t):
        day = t // DAY
        wk = max(WEEKLY[(day + i) % 7] for i in range(2))
        return self.pk[(self.reg[a], self.reg[b])] * wk * (1 + 0.004 * (day + 1)) * 1.07

    def proj_share(self, c):
        s = self.share.get(c)
        if s is None:
            return [0.05, 0.05, 0.05]
        return [min(0.8, x + 0.04) for x in s]

    def pair_need(self, a, b, t):
        """(video_bb, rt, backup) 峰值单位。"""
        D = self.grav(a, b)
        sa, sb = self.proj_share(a), self.proj_share(b)
        pf = self.peak_factor(a, b, t)
        out = [D * FRAC[k] * 0.5 * (sa[k] + sb[k]) * pf for k in range(3)]
        lo, hi = min(a, b), max(a, b)
        for (x, y, k, u, end) in self.won:
            if x == lo and y == hi and t < end:
                out[k] += u
        return out

    def down_at(self, t):
        d = set(self.down)
        for (l, st, en) in self.maint:
            if st <= t + 1 < en + 1:
                d.add(l)
        return d

    def dij_path(self, a, b, weight):
        dist = {a: 0.0}
        prev = {}
        pq = [(0.0, a)]
        adj = self.adj
        while pq:
            d, u = heapq.heappop(pq)
            if u == b:
                break
            if d > dist.get(u, math.inf):
                continue
            for v, l in adj[u]:
                w = weight(l)
                if w is None:
                    continue
                nd = d + w
                if nd < dist.get(v, math.inf):
                    dist[v] = nd
                    prev[v] = (u, l)
                    heapq.heappush(pq, (nd, v))
        if b not in prev:
            return None, None
        nodes, ls = [b], []
        v = b
        while v != a:
            u, l = prev[v]
            nodes.append(u)
            ls.append(l)
            v = u
        return nodes[::-1], ls[::-1]

    # ------------------------------------------------------------ 路由
    def sp(self, a, b, resid, thr, downb):
        """在 resid >= thr 且未断开的链路上求 a->b 的加权最短路，返回 (nodes, links)。"""
        n = self.n
        dist = [math.inf] * n
        prev = [-1] * n
        prevl = [-1] * n
        dist[a] = 0.0
        pq = [(0.0, a)]
        W = self.w0
        adj = self.adj
        hp, hq = heapq.heappush, heapq.heappop
        while pq:
            d, u = hq(pq)
            if u == b:
                break
            if d > dist[u]:
                continue
            for v, l in adj[u]:
                if downb[l] or resid[l] < thr:
                    continue
                nd = d + W[l]
                if nd < dist[v]:
                    dist[v] = nd
                    prev[v] = u
                    prevl[v] = l
                    hp(pq, (nd, v))
        if prev[b] < 0:
            return None, None
        nodes, ls = [b], []
        v = b
        while v != a:
            ls.append(prevl[v])
            v = prev[v]
            nodes.append(v)
        return nodes[::-1], ls[::-1]

    def route_pair(self, a, b, need, resid, downb, dump=True):
        tot = need[0] + need[1] + need[2]
        if tot <= 1e-9:
            tot = 1e-3
        paths = []
        rem = tot
        lat = self.lat
        for it in range(4):
            thr = max(0.5, 0.08 * rem) if it < 3 else 0.5
            nodes, ls = self.sp(a, b, resid, thr, downb)
            if nodes is None:
                break
            plat = sum(lat[l] for l in ls)
            if paths and plat > 140:
                break
            bott = min(resid[l] for l in ls)
            x = min(bott, rem)
            for l in ls:
                resid[l] -= x
            paths.append([nodes, ls, x, plat])
            rem -= x
            if rem <= 1e-6:
                break
        if not dump:
            return paths, tot - rem
        if not paths:
            nodes, ls = self.sp(a, b, resid, -1e18, downb)
            if nodes is None:
                return None
            paths.append([nodes, ls, 0.0, sum(lat[l] for l in ls)])
        self._rem = rem
        if rem > 1e-6:
            # 剩余流量按比例摊到已找到的路径上（溢出至各自的现货池）
            xs = sum(p[2] for p in paths)
            for p in paths:
                add = rem * (p[2] / xs if xs > 0 else 1.0 / len(paths))
                p[2] += add
                for l in p[1]:
                    resid[l] -= add
        return paths

    def plan(self, obs, t, partial=None):
        ready = sorted(c for c, q in self.pops.items() if q[0] <= t)
        down = self.down_at(t)
        downb = [False] * self.m
        for l in down:
            downb[l] = True
        own = obs["own_lease"]
        avail = obs["avail"]
        resid = [0.0] * self.m
        for l in range(self.m):
            c = own.get(str(l), 0.0) + max(0.0, avail[l])
            if self.is_mw[l]:
                c *= 0.5
            resid[l] = c
        if partial is None:
            self.capl = resid[:]
        pairs = []
        for i, a in enumerate(ready):
            for b in ready[i + 1:]:
                nd = self.pair_need(a, b, t)
                pri = sum(nd)
                for (x, y, k, u, end) in self.won:
                    if x == a and y == b and t < end:
                        pri += 1e6
                pairs.append((pri, a, b, nd))
        pairs.sort(reverse=True)
        if partial is not None:
            # 只重算受影响城市对：其它城市对的流量先从残量里扣掉
            for (a, b), (paths, _) in self.routes.items():
                if (a, b) in partial:
                    continue
                for nodes, ls, x, plat in paths:
                    for l in ls:
                        resid[l] -= x
            pairs = [p for p in pairs if (p[1], p[2]) in partial]
        flow = [0.0] * self.m if partial is None else None
        out = []
        newroutes = {}
        cong = {}
        for pri, a, b, nd in pairs:
            self._rem = 0.0
            paths = self.route_pair(a, b, nd, resid, downb)
            if paths is None:
                continue
            if partial is None:
                for r in {self.reg[a], self.reg[b]}:
                    cg = cong.setdefault(r, [0.0, 0.0])
                    cg[0] += self._rem
                    cg[1] += sum(nd)
            newroutes[(a, b)] = (paths, nd)
        if partial is None:
            self.routes = newroutes
            self.cong = {r: v[0] / v[1] for r, v in cong.items() if v[1] > 0}
        else:
            self.routes.update(newroutes)
        flow = [0.0] * self.m
        for (a, b), (paths, nd) in self.routes.items():
            for nodes, ls, x, plat in paths:
                for l in ls:
                    flow[l] += x
        self.flow = flow
        for (a, b), (paths, nd) in newroutes.items():
            tot = sum(p[2] for p in paths)
            msgs = []
            if len(paths) == 1:
                msgs.append({"src": a, "dst": b, "cls": "all", "paths": [paths[0][0]], "split": [1]})
            else:
                rt = nd[1]
                sp = [p[2] for p in paths]
                sp[0] = max(0.0, sp[0] - rt)
                if sum(sp) <= 1e-9:
                    sp = [1.0] + [0.0] * (len(paths) - 1)
                keep = [(p[0], s) for p, s in zip(paths, sp) if s > 1e-6 * max(1.0, tot)]
                if not keep:
                    keep = [(paths[0][0], 1.0)]
                ssum = sum(s for _, s in keep)
                msgs.append({"src": a, "dst": b, "cls": "all", "paths": [k[0] for k in keep],
                             "split": [round(s / ssum, 5) for _, s in keep]})
                msgs.append({"src": a, "dst": b, "cls": "rt", "paths": [paths[0][0]], "split": [1]})
            key = json.dumps(msgs, separators=(",", ":"))
            if self.sent.get((a, b)) != key:
                self.sent[(a, b)] = key
                out.extend(msgs)
        self.route_links = {k: {l for p in v[0] for l in p[1]} for k, v in self.routes.items()}
        return out

    # ------------------------------------------------------------ 主循环
    def step(self, obs):
        t = obs["t"]
        act = {}
        cash = obs["cash"]
        self.cash = cash
        if obs.get("bankrupt"):
            return {}
        self.opp_pops = set(obs.get("opp_pops", [])) | set(self.opp_pops)
        newpops = {}
        for c, ready, port, cache, ups, scrub in obs["pops"]:
            newpops[c] = [ready, port, cache, ups, scrub]
        self.pops = newpops
        for c in list(self.port_pend):
            self.port_pend[c] = [(tt, u) for tt, u in self.port_pend[c] if tt > t]
        # 事件
        for e in obs.get("ev", []):
            ty = e.get("type")
            if ty == "maint":
                self.maint.append((e["link"], e["start"], e["end"]))
            elif ty == "contract":
                self.contracts[e["id"]] = e
            elif ty == "award":
                k = self.contracts.get(e["contract"])
                if k is not None and e.get("winner") == self.seat:
                    self.won.append((k["a"], k["b"], CLS.index(k["cls"]), k["units"], t + 28 * DAY))
                    self.need_replan = True
        self.maint = [m for m in self.maint if m[2] > t]
        self.won = [w for w in self.won if w[4] > t]
        if "day" in obs:
            d = obs["day"]
            me, op = self.seat, 1 - self.seat
            for c, v in d["shares"].items():
                c = int(c)
                if c in self.pops:
                    self.share[c] = v[me]
                self.opp_share[c] = v[op]
            self.prices = d["prices"][me]
            self.sla7 = d.get("sla", 1.0)
        for c in self.pops:
            if c not in self.share and self.pops[c][0] <= t:
                self.share[c] = [0.05, 0.05, 0.05]
        if "groups" in obs:
            for a, b, cl, dem, dr, lt, ls in obs["groups"]:
                key = (min(a, b), max(a, b), cl)
                old = self.gdel.get(key)
                self.gdel[key] = dr if old is None else 0.7 * old + 0.3 * dr
        if "leases" in obs:
            self.my_leases = {x[0]: x for x in obs["leases"]}
        if t % DAY == 0:
            self.dmax_prev = self.dmax
            self.dmax = {}
        for k, v in obs["load"].items():
            l = int(k)
            if v > self.dmax.get(l, 0.0):
                self.dmax[l] = v
        # 峰值跟踪
        for k, v in obs["load"].items():
            l = int(k)
            self.load_peak[l] = max(self.load_peak.get(l, 0.0) * 0.997, v)
        for k, v in obs["port"].items():
            c = int(k)
            self.port_peak[c] = max(self.port_peak.get(c, 0.0) * 0.997, v)

        # ---- 路由 ----
        new_down = set(obs["down"])
        ready = tuple(sorted(c for c, q in self.pops.items() if q[0] <= t + 1))
        routes_out = []
        maint_now = {l for (l, st, en) in self.maint if st == t + 1}
        if ready != self.ready_set or self.need_replan or t - self.last_plan >= 72:
            self.ready_set = ready
            self.down = new_down
            self.need_replan = False
            self.last_plan = t
            routes_out = self.plan(obs, t)
            if t < self.harvest_from:
                act.update(self.lease_plan(obs, t, cash))
        else:
            changed = (new_down - self.down) | maint_now
            self.down = new_down
            if changed:
                aff = {k for k, ls in self.route_links.items() if ls & changed}
                if aff:
                    routes_out = self.plan(obs, t, partial=aff)
                    act.update(self.lease_plan(obs, t, cash))
        if routes_out:
            act["routes"] = routes_out
        # ---- 终局退租：负载随份额下滑，退掉多余租约（省下摊销，计入最后 7 天毛利） ----
        if t >= self.harvest_from and t % 6 == 4 and self.P.get("release", 1):
            rel = self.release_surplus(obs, t)
            if rel:
                act["release"] = rel
        # ---- 实测补租 ----
        if t % 6 == 3 and "lease" not in act:
            lz = self.reactive_lease(obs, t, cash)
            if lz:
                act["lease"] = lz
        # ---- 端口 ----
        if t % 6 == 1:
            ex = self.ports(obs, t)
            if ex:
                act["expand"] = ex
        # ---- 建站 ----
        if t % 6 == 2:
            b = self.build(obs, t)
            if b:
                act["build"] = b
        # ---- DDoS ----
        sc = self.scrub(obs)
        if sc is not None:
            act["scrub"] = sc
        # ---- 合同 ----
        bids = self.bids(obs, t)
        if bids:
            act["bid"] = bids
        # ---- 定价 ----
        pr = self.pricing(obs, t)
        if pr:
            act["price"] = pr
        return act

    # ------------------------------------------------------------ 租约
    def _lease_items(self, obs, t, cash, want):
        lp = obs["lp"]
        avail = obs["avail"]
        term = self.term_for(t)
        if term == 28 and cash < self.P.get("term28_cash", 15000):
            term = 7
        disc = {1: 1.0, 7: 0.8, 28: 0.6}[term]
        budget = cash + 10000 - 500
        items = []
        for l, u in sorted(want.items(), key=lambda x: -x[1]):
            u = min(u, avail[l])
            if u < 0.5:
                continue
            cost = lp[l] * disc * u * term * DAY * 1.02
            if cost > budget:
                u = budget / (lp[l] * disc * term * DAY * 1.02)
                if u < 0.5:
                    continue
                cost = budget
            budget -= cost
            items.append({"link": l, "units": round(u, 2), "term": term,
                          "max_price": round(max(lp[l] * 2.0, self.base[l] * 6.0), 6)})
        return items

    def lease_plan(self, obs, t, cash):
        own = obs["own_lease"]
        want = {}
        for l in range(self.m):
            f = self.flow[l]
            if f <= 0.3:
                continue
            capf = 0.5 if self.is_mw[l] else 1.0
            have = own.get(str(l), 0.0) * capf
            tgt = f * 1.05
            if tgt - have > max(1.0, 0.04 * tgt):
                want[l] = (f * 1.15 - have) / capf
        items = self._lease_items(obs, t, cash, want)
        return {"lease": items} if items else {}

    def release_surplus(self, obs, t):
        own = obs["own_lease"]
        by_link = {}
        for lid, (_, l, u, end) in self.my_leases.items():
            if end > t + 1:
                by_link.setdefault(l, []).append((u, lid))
        out = []
        for l, lst in by_link.items():
            have = own.get(str(l), 0.0)
            need = max(self.dmax_prev.get(l, 0.0), self.dmax.get(l, 0.0)) * 1.08
            if self.is_mw[l]:
                need *= 2.0
            surplus = have - need
            for u, lid in sorted(lst):
                if u <= surplus:
                    out.append(lid)
                    surplus -= u
                    self.my_leases.pop(lid, None)
        return out

    def reactive_lease(self, obs, t, cash):
        own = obs["own_lease"]
        want = {}
        for k, v in obs["load"].items():
            l = int(k)
            have = own.get(k, 0.0) * (0.5 if self.is_mw[l] else 1.0)
            if v > have + 1.0 and v > 1.02 * have:
                want[l] = self.load_peak.get(l, v) * 1.2 - have
        return self._lease_items(obs, t, cash, want)

    # ------------------------------------------------------------ 端口
    def ports(self, obs, t):
        pred = {}
        for (a, b), (paths, nd) in self.routes.items():
            tot = sum(p[2] for p in paths)
            pred[a] = pred.get(a, 0.0) + tot
            pred[b] = pred.get(b, 0.0) + tot
        out = []
        budget = self.cash + 10000 - 2500
        for c, q in self.pops.items():
            cap = q[1] + sum(u for _, u in self.port_pend.get(c, []))
            need = max(pred.get(c, 0.0) * 0.75, self.port_peak.get(c, 0.0) * 1.3)
            if need > cap and self.port_peak.get(c, 0.0) * 1.15 > cap or need > cap * 1.3:
                units = int(math.ceil((need * 1.15 - cap) / 100.0)) * 100
                cost = units / 100 * 800
                if cost > budget:
                    units = int(budget // 800) * 100
                    cost = units / 100 * 800
                if units >= 100:
                    budget -= cost
                    out.append({"pop": c, "units": units})
                    self.port_pend.setdefault(c, []).append((t + 1 + 24, units))
        return out

    # ------------------------------------------------------------ 建站
    def lat_value(self, plat):
        vf = 1.0 if plat <= 150 else max(0.0, 2 - plat / 150)
        rf = 1.0 if plat <= 38 else max(0.0, 2 - plat / 38) * 0.8
        return (0.6 * vf + 0.6 * rf + 0.1) / 1.3

    def build(self, obs, t):
        if len(self.pops) >= 20 or t > self.T - 6 * DAY:
            return None
        floor = -10000 + 3000 + 200 * len(self.pops)
        if self.cash - 1500 < floor:
            return None
        own = obs["own_lease"]
        avail = obs["avail"]
        down = self.down_at(t)
        downb = [False] * self.m
        for l in down:
            downb[l] = True
        resid = [(own.get(str(l), 0.0) + max(0.0, avail[l])) * (0.5 if self.is_mw[l] else 1.0)
                 for l in range(self.m)]
        S = sorted(self.pops)
        pfm = 1.0 + 0.004 * (t // DAY + 7)

        def need_of(a, b):
            D = self.grav(a, b) * 0.6 * self.pk[(self.reg[a], self.reg[b])] * 1.15 * pfm
            return [D * FRAC[0], D * FRAC[1], D * FRAC[2]]
        base_pairs = sorted(((self.grav(a, b), a, b) for i, a in enumerate(S) for b in S[i + 1:]), reverse=True)
        for _, a, b in base_pairs:
            self.route_pair(a, b, need_of(a, b), resid, downb, dump=False)
        cands = sorted((c for c in range(self.n) if c not in self.pops),
                       key=lambda c: -self.city_value(c, S))[:24]
        best, bc = 0.0, None
        for c in cands:
            r = resid[:]
            val = 0.0
            for p in sorted(S, key=lambda p: -self.grav(c, p)):
                paths, served = self.route_pair(c, p, need_of(c, p), r, downb, dump=False)
                sp_ = 0.55 if p in self.opp_pops else 1.0
                for pth in paths:
                    val += pth[2] * self.lat_value(pth[3]) * sp_
            if c in self.opp_pops:
                val *= 0.55
            if val > best:
                best, bc = val, c
        if bc is None:
            return None
        cost = self.cities[bc]["land"] * 3000.0
        rev_day = best / (1.15 * pfm * 1.3) * 1.3 / 35.0 * 144 * 0.9
        opex_day = self.cities[bc]["land"] * 40 * self.cities[bc]["elec"]
        if rev_day < 1.5 * opex_day:
            return None
        if self.cash - cost < floor:
            return None
        return [{"city": bc}]

    # ------------------------------------------------------------ DDoS
    def scrub(self, obs):
        att = set(obs.get("ddos", []))
        want = set()
        if att:
            rev = sum(s[2] for s in obs["stat"])
            tot_port = sum(obs["port"].values()) or 1.0
            for c in att:
                q = self.pops.get(c)
                if q is None:
                    continue
                ld = obs["port"].get(str(c), 0.0)
                frac = ld / tot_port * 2.0
                loss = rev * min(1.0, frac) * max(0.0, 1 - q[1] / max(1e-6, 5 * ld))
                contract = any(c in (w[0], w[1]) for w in self.won)
                if (loss > 35 and self.cash > 0 and obs["t"] < self.harvest_from) or contract:
                    want.add(c)
        if want != self.scrubbing:
            self.scrubbing = want
            return sorted(want)
        return None

    # ------------------------------------------------------------ 合同
    def bids(self, obs, t):
        out = []
        for cid, k in list(self.contracts.items()):
            if k.get("_done") or k["deadline"] <= t:
                continue
            a, b = k["a"], k["b"]
            qa, qb = self.pops.get(a), self.pops.get(b)
            if qa is None or qb is None or qa[0] > k["deadline"] - 1 or qb[0] > k["deadline"] - 1:
                continue
            if self.sla7 < 0.93 or t > self.T - 3 * DAY:
                continue
            ki = CLS.index(k["cls"])
            lt = self.slat[a][b]
            if ki == 1 and lt > 30:
                k["_done"] = True
                continue
            if ki == 0 and lt > 120:
                k["_done"] = True
                continue
            lo, hi = min(a, b), max(a, b)
            if qa[0] <= t and qb[0] <= t:
                gd = self.gdel.get((lo, hi, k["cls"]))
                if gd is None or gd < self.P["k_deliv"]:
                    continue
                rt_ = self.routes.get((lo, hi))
                if rt_ is None:
                    continue
                ok = True
                for pth in rt_[0]:
                    for l in pth[1]:
                        if self.flow[l] + k["units"] > self.capl[l] * 0.97:
                            ok = False
                if not ok:
                    continue
            else:
                continue
            contested = a in self.opp_pops and b in self.opp_pops
            price = k["reserve"] * (0.8 if contested else 1.0)
            out.append({"contract": cid, "price": round(price, 1)})
            k["_done"] = True
        return out

    # ------------------------------------------------------------ 定价
    def pricing(self, obs, t):
        regs = sorted({self.reg[c] for c in self.pops})
        out = []
        if t >= self.harvest_from - 1:
            # 终局收割：每天开头两次 ×1.3，直到 5.0
            if t % DAY in (DAY - 1, 0) and t + 1 >= self.harvest_from:
                for r in regs:
                    for k in range(3):
                        v = self.prices[r][k]
                        if v < self.P["harvest_cap"] - 0.01:
                            nv = min(self.P["harvest_cap"], v * 1.299)
                            out.append({"region": r, "cls": CLS[k], "value": round(nv, 4)})
                            self.prices[r][k] = nv
            return out
        if self.P["pre_days"] > 0 and t >= self.harvest_from - self.P["pre_days"] * DAY:
            if t % DAY in (5, 6):
                for r in regs:
                    for k in self.P.get("pre_cls", (0, 1)):
                        tg = self.P["pre_price"]
                        if abs(self.prices[r][k] - tg) > 1e-6:
                            out.append({"region": r, "cls": CLS[k], "value": tg})
                            self.prices[r][k] = max(tg, self.prices[r][k] * 0.7)
            return out
        if t % DAY == 5:
            P = self.P
            for r in regs:
                cg = self.cong.get(r, 0.0)
                cur = self.prices[r]
                tgt = [P["vid_base"], P["rt_base"], P["bk_base"]]
                if cg > P["cong_hi"]:
                    tgt[2] = min(P["bk_max"], max(cur[2], P["bk_base"]) * 1.3)
                    tgt[0] = min(P["vid_max"], max(cur[0], P["vid_base"]) * P.get("vid_step", 1.08))
                    tgt[1] = min(P.get("rt_max", P["rt_base"]), max(cur[1], P["rt_base"]) * 1.05)
                elif cg > P["cong_lo"]:
                    tgt[2] = max(cur[2], P["bk_base"])
                    tgt[0] = max(cur[0], P["vid_base"])
                    tgt[1] = max(cur[1], P["rt_base"])
                else:
                    tgt[2] = max(P["bk_base"], cur[2] * 0.85)
                    tgt[0] = max(P["vid_base"], cur[0] * 0.95)
                    tgt[1] = max(P["rt_base"], cur[1] * 0.95)
                for k in range(3):
                    if abs(cur[k] - tgt[k]) > 1e-6:
                        out.append({"region": r, "cls": CLS[k], "value": round(tgt[k], 4)})
                        self.prices[r][k] = tgt[k]
        return out


def make_bot(init):
    return Bot(init)


if __name__ == "__main__":
    bot = make_bot(json.loads(sys.stdin.readline()))
    print("{}", flush=True)
    for line in sys.stdin:
        msg = json.loads(line)
        if msg.get("type") == "end":
            print("{}", flush=True)
            break
        try:
            act = bot.step(msg)
        except Exception:
            act = {}
        sys.stdout.write(json.dumps(act, separators=(",", ":")) + "\n")
        sys.stdout.flush()
