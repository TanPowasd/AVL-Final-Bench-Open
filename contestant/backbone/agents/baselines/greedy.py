"""greedy 基线（贪心建站 + 贪心租瓶颈 + 按份额定价）。基于 _template.py。

协议（详见 ../README.md）：
  第 1 行收到 init（地图、规则常量、你的座位与起始 PoP），回一行任意 JSON（如 {}）作握手；
  之后每 tick 收到一行观测 obs，回一行动作 JSON（{} = 什么都不做）；
  最后收到 {"type":"end"}，回 {} 后退出。
单 tick 0.2 秒、整局 20 分钟总预算；超时/崩溃该 tick 按空动作处理。只能用标准库。

本文件也可被引擎以 make_bot(init) 的方式在进程内导入（仅开发调参用）。
"""
import heapq
import json
import math
import sys


class Net:
    """地图工具：邻接表、最短路、引力需求估计（公式与引擎一致）。"""

    def __init__(self, init):
        m = init["map"]
        self.cities = m["cities"]
        self.links = m["links"]
        self.n, self.m = len(self.cities), len(self.links)
        self.rules = init["rules"]
        self.adj = [[] for _ in range(self.n)]
        self.edge = {}
        for l in self.links:
            self.adj[l["u"]].append((l["v"], l["id"]))
            self.adj[l["v"]].append((l["u"], l["id"]))
            self.edge[(l["u"], l["v"])] = l["id"]
            self.edge[(l["v"], l["u"])] = l["id"]
        self.region = [c["region"] for c in self.cities]

    def other(self, l, u):
        L = self.links[l]
        return L["v"] if L["u"] == u else L["u"]

    def dijkstra(self, src, weight=None, down=()):
        """weight(link_id) -> 非负权（默认传播延迟）。返回 (dist, prev)，prev[v] = (u, link)。"""
        down = set(down)
        dist = [math.inf] * self.n
        prev = [None] * self.n
        dist[src] = 0.0
        pq = [(0.0, src)]
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist[u]:
                continue
            for v, l in self.adj[u]:
                if l in down:
                    continue
                w = self.links[l]["lat"] if weight is None else weight(l)
                if w is None:
                    continue
                nd = d + w
                if nd < dist[v]:
                    dist[v] = nd
                    prev[v] = (u, l)
                    heapq.heappush(pq, (nd, v))
        return dist, prev

    @staticmethod
    def walk(prev, src, dst):
        """从 prev 还原路径：返回 (节点列表, 链路列表)；不可达返回 (None, None)。"""
        if src != dst and prev[dst] is None:
            return None, None
        nodes, links = [dst], []
        v = dst
        while v != src:
            u, l = prev[v]
            nodes.append(u)
            links.append(l)
            v = u
        return nodes[::-1], links[::-1]

    def gravity(self, a, b):
        """城市对 (a,b) 的基准总需求（未乘份额、昼夜、类别占比）。"""
        ca, cb = self.cities[a], self.cities[b]
        d = math.hypot(ca["x"] - cb["x"], ca["y"] - cb["y"]) / 1000.0
        return self.rules["DEM_K"] * ca["pop"] * cb["pop"] / max(d, 0.3) ** self.rules["DEM_ALPHA"]


class Bot:
    """greedy：贪心建站 + 贪心租瓶颈 + 按份额定价。
    - 每天至多建 1 个 PoP：候选城市按 Σ引力(候选, 已有PoP) / 建站费 打分，只考虑离已有 PoP 延迟 ≤35ms 的城市；
    - 每天重算路由：每个 PoP 对走"延迟 + 容量不够罚分"最小的一条路（全部类别同路）；
    - 每 6 tick：任何链路上自己的峰值负载超过租量就补 7 天租约；
    - 每天按各大区自己份额调价（份额低降 5%，高升 5%）；端口吃紧就扩容。"""

    MAX_POPS = 12

    def __init__(self, init):
        self.net = Net(init)
        self.peak = {}
        self.pops = set(init["start"]["you"])
        self.shares = {}
        self.seat = init["seat"]
        self.prices = [[1.0, 1.0, 1.0] for _ in range(6)]
        self.port_peak = {}

    def step(self, obs):
        net = self.net
        t = obs["t"]
        act = {}
        for k, v in obs["load"].items():
            l = int(k)
            self.peak[l] = max(self.peak.get(l, 0.0) * 0.995, v)
        self.pops = {p[0] for p in obs["pops"]}
        for c, v in obs["port"].items():
            self.port_peak[c] = max(self.port_peak.get(c, 0.0), v)
        cash = obs["cash"]
        if "day" in obs:
            self.shares = obs["day"]["shares"]
            self.prices = obs["day"]["prices"][self.seat]
        if t % 6 == 0 and cash > -4000:
            own = obs["own_lease"]
            leases = []
            for l, pk in self.peak.items():
                need = pk * 1.1 - own.get(str(l), 0.0)
                if need > 2.0 and obs["avail"][l] > 0.5:
                    leases.append({"link": l, "units": round(min(need * 1.2, obs["avail"][l]), 2),
                                   "term": 7, "max_price": obs["lp"][l] * 1.01})
            if leases:
                act["lease"] = leases
        ready = sorted(p[0] for p in obs["pops"] if p[1] <= t)
        if t % 144 == 20 or ready != getattr(self, "_ready", None):
            self._ready = ready
            act["routes"] = self.route(obs, ready)
        if t % 144 == 10:
            # 端口
            ex = []
            for c, ready, port, cache, ups, scrub in obs["pops"]:
                if self.port_peak.get(str(c), 0.0) > 0.85 * port and cash > 1000:
                    ex.append({"pop": c, "units": 100})
            self.port_peak = {}
            if ex:
                act["expand"] = ex
            # 建站
            if len(self.pops) < self.MAX_POPS:
                best, bc = 0.0, None
                lat_from = [net.dijkstra(p)[0] for p in self.pops]
                for c in range(net.n):
                    if c in self.pops:
                        continue
                    if min(d[c] for d in lat_from) > 35:
                        continue
                    cost = net.cities[c]["land"] * net.rules["POP_BUILD_K"]
                    if cash - cost < 4000:
                        continue
                    v = sum(net.gravity(c, p) for p in self.pops) / cost
                    if v > best:
                        best, bc = v, c
                if bc is not None:
                    act["build"] = [{"city": bc}]
            # 定价
            pr = []
            reg = {}
            for c in self.pops:
                s = self.shares.get(str(c))
                if s:
                    reg.setdefault(net.region[c], []).append(sum(s[self.seat]) / 3)
            for r, lst in reg.items():
                avg = sum(lst) / len(lst)
                f = 0.95 if avg < 0.3 else (1.05 if avg > 0.5 else None)
                if f:
                    for k, cls in enumerate(("video", "rt", "backup")):
                        pr.append({"region": r, "cls": cls, "value": round(self.prices[r][k] * f, 4)})
            if pr:
                act["price"] = pr
        return act

    def route(self, obs, ready):
        net = self.net
        own = obs["own_lease"]
        down = set(obs["down"])
        out = []
        my = self.seat
        for i, a in enumerate(ready):
            for b in ready[i + 1:]:
                sa = self.shares.get(str(a), [[0.35] * 3, [0.35] * 3])[my]
                sb = self.shares.get(str(b), [[0.35] * 3, [0.35] * 3])[my]
                need = net.gravity(a, b) * (sum(sa) + sum(sb)) / 6 * 1.6

                def w(l, need=need):
                    if l in down:
                        return None
                    cap = obs["avail"][l] + own.get(str(l), 0.0)
                    return net.links[l]["lat"] + 1.0 + (40.0 if cap < need else 0.0)
                _, prev = net.dijkstra(a, w)
                nodes, _ = net.walk(prev, a, b)
                if nodes:
                    out.append({"src": a, "dst": b, "cls": "all", "paths": [nodes], "split": [1]})
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
