"""shortest 基线（只走最短路 + 按需短租）。基于 _template.py。

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
    """shortest：不建站、不改价，流量走引擎默认的最短路；
    某条链路上自己的流量超出租量时，按过去一天的峰值补租 1 天短约（按需短租）。"""

    def __init__(self, init):
        self.net = Net(init)
        self.peak = {}

    def step(self, obs):
        t = obs["t"]
        for k, v in obs["load"].items():
            l = int(k)
            self.peak[l] = max(self.peak.get(l, 0.0) * 0.995, v)
        if t % 6 != 0 or obs["cash"] < -5000:
            return {}
        own = obs["own_lease"]
        leases = []
        for l, pk in self.peak.items():
            have = own.get(str(l), 0.0)
            need = pk * 1.15 - have
            if need > 1.0 and obs["avail"][l] > 0.5:
                leases.append({"link": l, "units": round(min(need, obs["avail"][l]), 2), "term": 1,
                               "max_price": obs["lp"][l] * 1.01})
        return {"lease": leases} if leases else {}


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
