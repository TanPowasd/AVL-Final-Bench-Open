"""merchant：中等基线（"只抢市场"）。

经营部分和 ref 一样（竞拍买卡、建机房、招人、每季度融资、logit 最优定价、按需分配服务卡），
但训练用默认值：不跑探针、默认学习率、按 Chinchilla 比例 D = 20N、8 个域均匀配比；
工程上只做最基本的（存档间隔按故障率、batch = 卡数/2），全部卡都拿去训练、不排查慢节点、不提前申请审核；
研究只做推理优化 I 和冷却改造。由 ref.py 换一组 CFG 生成。
"""
import json
import math
import sys

CFG = {
    "name": "ref",
    "probes": True,          # 学习率探针 + 缩放律探针
    "ckpt_opt": True,        # 存档间隔 = √(2·0.2/f)，f = 卡数/MTBF（否则用 ckpt_fixed）
    "batch_opt": True,       # 每天把 batch 调到 卡数/2
    "card_select": True,     # 训练只用净增有效算力的型号（旧卡多了故障太频繁）
    "ops": True,             # 尖峰/发散回滚、慢节点排查
    "audit_early": True,     # 下一代提前建好（暂停）并申请监管审核，和上一代训练重叠
    "smart_mix": True,       # 按数据量/质量/污染配比
    "lr_mult": 0.92,         # 主训练学习率 = lr_mult × η̂
    "buy": True,             # 拍卖买卡、建机房
    "bid_mult": 1.55,        # 出价 = 基准价 × 热度 × bid_mult
    "spend_frac": 0.30,      # 每周可花掉「可支配现金」的比例
    "cash_floor": 40.0,      # 保留现金
    "hire": True,
    "staff_cap": [40, 70, 90],   # 第 1/2/3 年目标人数上限
    "research": ["filter:web", "filter:multiling", "filter:code", "filter:books", "algo_eff_1", "cooling",
                 "filter:science", "filter:dialog", "filter:math", "infer_opt_1", "stable", "algo_eff_2",
                 "early_supply", "infer_opt_2", "algo_eff_3"],
    "price": "opt",          # opt / fixed
    "fixed_price": 10.0,
    "raise": True,
    "raise_frac": 0.15,
    "raise_cap": 500.0,
    "raise_below": 150.0,    # 可支配现金低于此值才融资
    "data": True,
    "gens": [2600, 7500, 13000, 19500, 26150],
    "growth_guess": 1.0,     # 规划时对未来买卡的估计（按最近 8 周到货速度外推的倍数）
    "serve_share": 0.45,     # 服务卡最多占总算力的比例
    "chinchilla": 20.0,      # 不做探针时 D = 20 N
    "ckpt_fixed": 0,         # >0 时固定存档间隔
    "batch_fixed": 0,        # >0 时固定 batch
}

# 与 ref 不同的设置
CFG.update({
    'name': 'merchant',
    'probes': False,
    'smart_mix': False,
    'card_select': False,
    'ops': False,
    'audit_early': False,
    'staff_cap': [30, 40, 50],
    'research': ['infer_opt_1', 'cooling'],
    'data': False,
})

REAL = ["web", "code", "math", "books", "science", "dialog", "multiling"]
GPUS = ["G1", "G2", "G3", "G4", "G5"]
C_PRIOR = 0.06


def lsq3(rows):
    """最小二乘 y = a + b x1 + c x2，rows=[(x1,x2,y)]。返回 (a,b,c,sse)。"""
    n = len(rows)
    S = [[0.0] * 4 for _ in range(3)]
    for x1, x2, y in rows:
        v = (1.0, x1, x2)
        for i in range(3):
            for j in range(3):
                S[i][j] += v[i] * v[j]
            S[i][3] += v[i] * y
    # 高斯消元
    for i in range(3):
        piv = max(range(i, 3), key=lambda r: abs(S[r][i]))
        S[i], S[piv] = S[piv], S[i]
        if abs(S[i][i]) < 1e-15:
            return None
        for r in range(3):
            if r != i:
                f = S[r][i] / S[i][i]
                for c in range(i, 4):
                    S[r][c] -= f * S[i][c]
    a, b, c = (S[i][3] / S[i][i] for i in range(3))
    sse = sum((y - a - b * x1 - c * x2) ** 2 for x1, x2, y in rows)
    return a, b, c, sse


class Lab:
    def __init__(self, init):
        self.cfg = CFG
        self.seat = init["seat"]
        self.me = "AB"[self.seat]
        self.R = init["rules"]
        self.gpu = {g["model"]: g for g in init["gpus"]}
        self.regions = init["regions"]
        self.dom = {d["name"]: d for d in init["domains"]}
        self.U = {d: self.dom[d]["public_b"] for d in self.dom}
        self.U["distill"] = 0.0
        self.qhat = {d: self.dom[d]["q_hint"] for d in self.dom}
        self.chat = {d: C_PRIOR for d in self.dom}
        self.filtered = set()
        self.techs = init["techs"]
        self.tech = set()
        self.models = {m["id"]: m for m in init["models"]}
        self.my_models = []
        self.t = 0
        self.cash = init["start"]["cash"]
        self.g = {}
        self.runs = {}
        self.eta_c = None
        self.fit = None
        self.probe_stage = 0 if CFG["probes"] else 9
        self.probes = {}
        self.gen_i = 0
        self.main = None
        self.main_N = None
        self.next_run = None
        self.serve = None
        self.share = 0.0
        self.shares = {}
        self.demand = 1.0
        self.heat = 1.0
        self.rep = 0.0
        self.drop = 0.0
        self.staff = init["start"]["staff"]
        self.sites = [{"id": 0, "region": 0, "mw": init["start"]["site"]["mw"], "ready": 0, "cards": {"G1": 512}}]
        self.pend_mw = 0.0
        self.deliv = []
        self.arrived = []
        self.rev_ring = [0.0] * 730
        self.tpt_hist = []
        self.raised_q = set()
        self.released_runs = set()
        self.run_seq = 0
        self.pending_price = None
        self.last_price_day = -1
        self.price_now = None
        self.built_regions = {0}
        self.last_build = -10 ** 9
        self.research_set = None

    # ------------------------------------------------------------ 工具
    def eta(self, n):
        c = self.eta_c if self.eta_c else self.R["DEFAULT_LR_C"]
        return c * n ** -0.3

    def compute_of(self, d):
        return sum(self.gpu[m]["power"] * n for m, n in d.items())

    def usable(self):
        return {m: (v[0] + v[3]) for m, v in self.g.items()}

    def total_compute(self):
        return self.compute_of(self.usable())

    def kw_installed(self):
        pue = 1.15 if "cooling" in self.tech else 1.3
        tot = 0.0
        for m, v in self.g.items():
            tot += (v[0] + v[1] + v[2]) * self.gpu[m]["kw"] * pue
        for d in self.deliv:
            tot += d[2] * self.gpu[d[1]]["kw"] * pue
        return tot

    def mw_total(self):
        return sum(s["mw"] for s in self.sites) + self.pend_mw

    # ------------------------------------------------------------ 主入口
    def step(self, o):
        self.to_delist = []
        self.ingest(o)
        act = {}
        if self.to_delist:
            act["delist"] = self.to_delist
        t = self.t
        if self.probe_stage < 9:
            self.do_probes(act)
        else:
            self.do_main(act)
        self.do_ops(act)
        if t % 168 == 0:
            self.weekly(act)
        if t % 24 == 0:
            self.daily(act)
        return act

    def ingest(self, o):
        self.t = o["t"]
        self.cash = o["cash"]
        self.rep = o.get("rep", 0.0)
        self.g = o.get("gpu", {})
        self.runs = {r[0]: r for r in o.get("runs", [])}
        api = o.get("api", [0, 0, 0])
        self.rev_ring[self.t % 730] = api[2]
        self.ev = o.get("ev", [])
        for e in self.ev:
            ty = e.get("type")
            if ty == "release":
                m = e["model"]
                self.models[m["id"]] = m
                if m["owner"] == self.me:
                    # 新模型上线：旧模型全部下架，价格从新模型的发布价重新定
                    self.to_delist = [x for x in self.my_models if x != m["id"] and x in self.models]
                    for x in self.to_delist:
                        self.models.pop(x, None)
                    self.price_now = m["price"]
                    self.last_price_day = -1
            elif ty == "tech":
                self.tech.add(e["tech"])
                if "q" in e:
                    d = e["tech"][7:]
                    self.qhat[d] = e["q"]
                    self.chat[d] = e["c"]
                    self.filtered.add(d)
                self.research_set = None
            elif ty == "audit_fail":
                if e["model"] in self.models:
                    self.models[e["model"]]["pub"] = e["pub"]
            elif ty == "done":
                self.on_done(e)
            elif ty == "delivered":
                self.arrived.append((self.t, e["model"], e["n"]))
        for r in o.get("res", []):
            if r.get("act") == "release" and r.get("ok"):
                self.my_models.append(r["model"])
            if r.get("act") == "expand_site" and r.get("ok"):
                self.pend_mw += r["mw"]
            if r.get("act") == "build_site" and r.get("ok"):
                pass
        if "wk" in o:
            wk = o["wk"]
            self.wk = wk
            self.heat = wk.get("heat", 1.0)
            self.shares = wk.get("shares", {})
            self.share = self.shares.get(self.me, 0.0)
            self.demand = wk.get("demand", self.demand)
            for m in wk.get("models", []):
                self.models[m["id"]] = m
            my = wk["my"]
            self.staff = [{"id": s[0], "ab": s[1], "sal": s[2]} for s in my["staff"]]
            self.sites = my["sites"]
            self.U.update(my["data"])
            self.tech = set(my["tech"])
            self.drop = my.get("drop", 0.0)
            self.deliv = my.get("deliv", [])
            self.pend_mw = sum(s.get("pend_mw", 0) for s in self.sites)
        else:
            self.wk = None
        if "loss" in o:
            self.last_loss = o["loss"]

    # ------------------------------------------------------------ 探针
    def do_probes(self, act):
        c0 = self.R["DEFAULT_LR_C"]
        if self.probe_stage == 0:
            runs = []
            N, D = 0.5, 8.0
            for i, k in enumerate([1.0, 1.2, 1.45, 1.75, 2.1, 2.5, 3.0, 3.6]):
                name = f"lr{i}"
                self.probes[name] = {"k": k, "loss": None}
                runs.append({"name": name, "params_b": N, "tokens_b": D, "lr": c0 * k * N ** -0.3,
                             "batch": 32, "gpus": {"G1": 64}, "ckpt_every": 2, "mix": self.mix(D)})
            act["run"] = runs
            self.probe_stage = 1
        elif self.probe_stage == 1:
            ps = [v for k, v in self.probes.items() if k.startswith("lr")]
            if all(v["loss"] is not None for v in ps):
                rows = [(math.log(v["k"]), 0.0, v["loss"] - 0.08 * math.log(v["k"]) ** 2) for v in ps]
                # y = b0 - 0.16 lnc · lnk  → 用 lsq3 的前两项
                n = len(rows)
                mx = sum(r[0] for r in rows) / n
                my = sum(r[2] for r in rows) / n
                sxx = sum((r[0] - mx) ** 2 for r in rows)
                sxy = sum((r[0] - mx) * (r[2] - my) for r in rows)
                slope = sxy / sxx
                lnc = -slope / 0.16
                k = min(3.2, max(1.0, math.exp(lnc)))
                self.eta_c = c0 * k
                # 缩放律网格
                runs = []
                grid = [(n_, d_) for n_ in (0.3, 1.0, 3.0) for d_ in (5.0, 20.0, 80.0)]
                tot = sum(n_ * d_ for n_, d_ in grid)
                for i, (n_, d_) in enumerate(grid):
                    name = f"sc{i}"
                    cards = max(8, int(440 * n_ * d_ / tot))
                    self.probes[name] = {"N": n_, "D": d_, "loss": None}
                    runs.append({"name": name, "params_b": n_, "tokens_b": d_, "lr": 0.95 * self.eta(n_),
                                 "batch": max(4, cards // 2), "gpus": {"G1": cards}, "ckpt_every": 6,
                                 "mix": self.mix(d_)})
                act["run"] = runs
                self.probe_stage = 2
        elif self.probe_stage == 2:
            ps = [v for k, v in self.probes.items() if k.startswith("sc")]
            if all(v["loss"] is not None for v in ps):
                self.fit_scaling(ps)
                self.probe_stage = 9

    def fit_scaling(self, ps):
        best = None
        for i in range(25):
            a = 0.28 + 0.005 * i
            for j in range(21):
                b = 0.25 + 0.005 * j
                rows = [(v["N"] ** -a, v["D"] ** -b, v["loss"]) for v in ps]
                r = lsq3(rows)
                if r is None or r[1] < 0 or r[2] < 0:
                    continue
                if best is None or r[3] < best[0]:
                    best = (r[3], a, b, r[0], r[1], r[2])
        if best:
            _, a, b, E, A, B = best
            self.fit = {"alpha": a, "beta": b, "E": E, "A": A, "B": B}

    def on_done(self, e):
        name = e["run"]
        if name in self.probes:
            self.probes[name]["loss"] = sum(e["loss"]) / len(e["loss"])

    # ------------------------------------------------------------ 配比
    def mix(self, D):
        if not CFG["smart_mix"]:
            return {d: 1.0 for d in list(self.dom)}
        w = {}
        for d in REAL:
            q = self.qhat[d]
            w[d] = max(0.2, q)
        # 去污染：估计的公开榜加成（Σ 配比 × 污染）超过 0.035 就把最脏的域减半，最多 6 次
        for _ in range(6):
            tot = sum(w.values())
            est = sum(w[d] / tot * (self.chat[d] if d in self.filtered else C_PRIOR) for d in REAL)
            if est <= 0.035:
                break
            worst = max(REAL, key=lambda d: (self.chat[d] if d in self.filtered else C_PRIOR) * w[d])
            w[worst] *= 0.5
        # 每域最多 3 轮
        frac = {}
        tot = sum(w.values())
        for d in REAL:
            frac[d] = w[d] / tot
        for _ in range(6):
            capped = {}
            free = 0.0
            for d in REAL:
                cap = 3.0 * self.U.get(d, 0) / max(D, 1e-9)
                if frac[d] > cap:
                    free += frac[d] - cap
                    frac[d] = cap
                    capped[d] = True
            if free <= 1e-9:
                break
            rest = [d for d in REAL if not capped.get(d) and frac[d] < 3.0 * self.U.get(d, 0) / max(D, 1e-9)]
            if not rest:
                break
            z = sum(w[d] for d in rest)
            for d in rest:
                frac[d] += free * w[d] / z
        return {d: round(v, 5) for d, v in frac.items() if v > 1e-5}

    def contamination(self, mix):
        z = sum(mix.values())
        return sum(v / z * (self.chat[d] if d in self.filtered else C_PRIOR) for d, v in mix.items() if d in REAL)

    def eff_D(self, D, mix):
        """估计有效 token（重复衰减 + 质量），用于缩放律预测。"""
        z = sum(mix.values())
        tot = 0.0
        for d, f in mix.items():
            S = D * f / z
            U = max(1e-6, self.U.get(d, 0))
            tot += U * 4 * (1 - math.exp(-S / U / 4)) * self.qhat.get(d, 1.0)
        return tot

    def choose_N(self, budget):
        """budget: G1·h。返回 (N, D)。"""
        KC = self.R["KC"]
        if not self.fit:
            N = math.sqrt(budget / KC / CFG["chinchilla"])
            return N, budget / KC / N
        f = self.fit
        best = None
        for i in range(-60, 61):
            N = math.sqrt(budget / KC / 20.0) * math.exp(i * 0.04)
            D = budget / KC / N
            mix = self.mix(D)
            # 把有效 token 折算回"无重复、质量 1 的 token"：探针阶段数据量小，几乎无重复
            de = self.eff_D(D, mix) / max(1e-9, self.eff_D(min(D, 80.0), mix) / min(D, 80.0))
            L = f["E"] + f["A"] * N ** -f["alpha"] + f["B"] * max(de, 1e-6) ** -f["beta"]
            if best is None or L < best[0]:
                best = (L, N, D)
        return best[1], best[2]

    # ------------------------------------------------------------ 主训练
    def serve_need(self):
        """服务需要的 G1 算力。"""
        fl = self.flag_model()
        if not fl:
            return 0.0
        mult = 0.6 if "infer_opt_2" in self.tech else 0.8 if "infer_opt_1" in self.tech else 1.0
        s = max(self.share, self.est_share(fl, self.price_now or fl["price"])) + 0.03
        return self.demand * 1.12 * s * self.R["SK"] * fl["N"] * mult * 1.15

    def flag_model(self):
        best, bu = None, -1e9
        for mid in self.my_models:
            m = self.models.get(mid)
            if not m or m.get("N") is None:
                continue
            u = 4 * m["pub"] - 1.5 * math.log(m["price"])
            if u > bu:
                best, bu = m, u
        return best

    def split_cards(self):
        """返回 (serve dict, train dict)：服务优先用最旧的卡；训练只用「加进来净增有效算力」的型号
        （卡越多故障越频繁，大量旧卡反而拖慢训练）。"""
        us = self.usable()
        tot = self.compute_of(us)
        need = min(self.serve_need(), tot * CFG["serve_share"])
        serve = {}
        got = 0.0
        for m in GPUS:
            if got >= need or m not in us:
                continue
            k = min(us[m], int(math.ceil((need - got) / self.gpu[m]["power"])))
            if k > 0:
                serve[m] = k
                got += k * self.gpu[m]["power"]
        rest = {m: us[m] - serve.get(m, 0) for m in us if us[m] - serve.get(m, 0) > 0}
        if not CFG["card_select"]:
            return serve, rest
        train, best = {}, 0.0
        for m in reversed(GPUS):
            if m not in rest:
                continue
            trial = dict(train)
            trial[m] = rest[m]
            v = self.compute_of(trial) * self.eff_guess(sum(trial.values()))
            if v > best * 1.01:
                train, best = trial, v
        return serve, train

    def gen_end(self):
        g = CFG["gens"]
        return g[min(self.gen_i, len(g) - 1)]

    def projected_compute(self, t0, t1, train_now):
        """[t0,t1) 内训练可用 G1·h 的估计：当前训练卡 + 已知到货 + 按最近到货速度外推。"""
        base = self.compute_of(train_now) * (t1 - t0)
        for d in self.deliv:
            if d[0] < t1:
                base += self.gpu[d[1]]["power"] * d[2] * (t1 - max(t0, d[0]))
        recent = [a for a in self.arrived if a[0] > self.t - 8 * 168]
        rate = sum(self.gpu[a[1]]["power"] * a[2] for a in recent) / (8 * 168)
        if CFG["buy"]:
            base += CFG["growth_guess"] * rate * (t1 - t0) ** 2 / 2 * 0.5
        return base

    def eff_guess(self, ncards):
        f = ncards / self.R["MTBF"]
        k = self.ckpt_every(ncards)
        # 每 k tick 存档一次：存档开销 0.2/k；故障率 f：平均丢 k/2 tick + 重启 2 tick
        return max(0.05, (1 - 0.2 / k) * (1 - f) ** (k / 2 + 2) / (1 + f * 2))

    def ckpt_every(self, ncards):
        if CFG["ckpt_fixed"] or not CFG["ckpt_opt"]:
            return CFG["ckpt_fixed"] or 24
        f = max(1e-6, ncards / self.R["MTBF"])
        return int(max(1, min(48, round(math.sqrt(0.4 / f)))))

    def batch_for(self, ncards):
        if CFG["batch_fixed"]:
            return CFG["batch_fixed"]
        return max(4, int(math.ceil(ncards / 2)))

    def plan(self, t0, end, train):
        n = max(1, sum(train.values()))
        budget = self.projected_compute(t0, end, train) * self.eff_guess(n) * 0.97
        N, D = self.choose_N(budget)
        return round(max(0.5, N), 3), round(max(1.0, D), 2), budget

    def spec(self, name, N, D, train):
        n = max(1, sum(train.values()))
        sp = {"name": name, "params_b": N, "tokens_b": D, "gpus": train, "mix": self.mix(D), "sched": "cosine",
              "batch": self.batch_for(n), "ckpt_every": self.ckpt_every(n)}
        if self.eta_c:
            sp["lr"] = CFG["lr_mult"] * self.eta(N)
        return sp

    def start_main(self, act):
        t = self.t
        end = self.gen_end()
        if end - t < 200:
            return
        serve, train = self.split_cards()
        if sum(train.values()) <= 0:
            return
        self.serve = serve
        act["serve"] = [{"gpus": serve}] if serve else []
        if self.next_run:
            # 提前建好、审核过（或正在审核）的下一代：恢复训练
            name, N = self.next_run
            self.next_run = None
            ctl = act.setdefault("run_ctl", [])
            ctl.append({"name": name, "op": "set_gpus", "arg": train})
            ctl.append({"name": name, "op": "resume"})
            self.main, self.main_N, self.main_end = name, N, end
            return
        N, D, budget = self.plan(t, end, train)
        self.run_seq += 1
        name = f"gen{self.run_seq}"
        act.setdefault("run", []).append(self.spec(name, N, D, train))
        self.main, self.main_N, self.main_end = name, N, end

    def prepare_next(self, act):
        """在本代结束前约 450 tick 建好下一代（暂停）并申请审核，审核和本代训练重叠。"""
        g = CFG["gens"]
        if self.gen_i + 1 >= len(g):
            return
        t0, end = self.main_end, g[self.gen_i + 1]
        _, train = self.split_cards()
        N, D, budget = self.plan(t0, end, train)
        if budget < self.R["REG_COMPUTE"] * 0.9:
            return
        self.run_seq += 1
        name = f"gen{self.run_seq}"
        sp = self.spec(name, N, D, train)
        sp["paused"] = True
        act.setdefault("run", []).append(sp)
        act.setdefault("audit_request", []).append({"run": name})
        self.next_run = (name, N)

    def do_main(self, act):
        t = self.t
        if self.main is None or (self.main not in self.runs and self.main in self.released_runs):
            self.start_main(act)
            return
        r = self.runs.get(self.main)
        if r is None:
            return
        status = r[1]
        if status == "done":
            self.release_main(act)
            return
        if CFG["audit_early"] and self.next_run is None and not getattr(self, "prep_done", {}).get(self.gen_i) \
                and t >= self.main_end - 450:
            self.prep_done = {self.gen_i: True}
            self.prepare_next(act)
        # 每天调整：卡、batch、存档、目标 token
        if t % 24 == 12 and status in ("running", "review"):
            serve, train = self.split_cards()
            n = sum(train.values())
            ctl = act.setdefault("run_ctl", [])
            ctl.append({"name": self.main, "op": "set_gpus", "arg": train})
            if serve != self.serve:
                act["serve"] = [{"gpus": serve}] if serve else []
                self.serve = serve
            if CFG["batch_opt"]:
                ctl.append({"name": self.main, "op": "set_batch", "arg": self.batch_for(n)})
            if CFG["ckpt_opt"]:
                ctl.append({"name": self.main, "op": "set_ckpt", "arg": self.ckpt_every(n)})
            rem = self.main_end - t
            if rem > 72 and status == "running":
                comp = self.compute_of(train) * self.eff_guess(n)
                more = comp * rem / (self.main_N * self.R["KC"])
                for d in self.deliv:
                    if d[0] < self.main_end:
                        more += self.gpu[d[1]]["power"] * d[2] * (self.main_end - d[0]) * 0.8 / (
                            self.main_N * self.R["KC"])
                tgt = r[2] + more
                if abs(tgt - r[3]) / max(1.0, r[3]) > 0.02:
                    ctl.append({"name": self.main, "op": "set_tokens", "arg": round(max(tgt, r[2] + 0.01), 3)})
        if status in ("running", "review") and t >= self.main_end - 24:
            # 时间到了：发布最新存档，任务停掉
            act.setdefault("run_ctl", []).append({"name": self.main, "op": "stop"})
            self.release_main(act)

    def release_main(self, act):
        if self.main in self.released_runs:
            return
        self.released_runs.add(self.main)
        act.setdefault("release", []).append({"run": self.main, "price": CFG["fixed_price"]})
        self.gen_i += 1
        self.main_released = self.main
        self.main = None if self.t < CFG["gens"][-1] - 200 else self.main

    # ------------------------------------------------------------ 运维（每 tick）
    def do_ops(self, act):
        for e in self.ev:
            ty = e.get("type")
            run = e.get("run")
            if not run:
                continue
            if ty in ("spike", "diverged") and (CFG["ops"] or ty == "diverged"):
                act.setdefault("run_ctl", []).append({"name": run, "op": "rollback"})
            elif ty == "slow_node" and CFG["ops"]:
                act.setdefault("run_ctl", []).append({"name": run, "op": "diagnose"})

    # ------------------------------------------------------------ 每日
    def daily(self, act):
        self.research(act)
        if self.main is None or self.main not in self.runs or self.runs[self.main][1] in ("done", "stopped"):
            # 没有主训练在跑：剩余的卡全部自动服务旗舰模型
            if self.serve != "auto":
                act["serve"] = []
                self.serve = "auto"
        elif self.t % 168 == 0:
            serve, _ = self.split_cards()
            if serve != self.serve:
                act["serve"] = [{"gpus": serve}] if serve else []
                self.serve = serve
        fl = self.flag_model()
        if fl and CFG["price"] == "opt" and self.t // 24 != self.last_price_day:
            p = self.choose_price(fl)
            if self.price_now is None or abs(p - self.price_now) / self.price_now > 0.05:
                act.setdefault("price", []).append({"model": fl["id"], "value": p})
                self.price_now = p
                fl["price"] = p
                self.last_price_day = self.t // 24

    def research(self, act):
        if not self.staff:
            return
        order = [k for k in CFG["research"] if k not in self.tech]
        avail = []
        for k in order:
            pre = self.techs.get(k, {}).get("pre")
            if pre and pre not in self.tech:
                continue
            avail.append(k)
        pick = avail[:2]
        if pick != self.research_set:
            self.research_set = pick
            act["research"] = [{"tech": k, "share": s} for k, s in zip(pick, (0.6, 0.4))]

    def est_share(self, fl, price):
        others = []
        for m in self.models.values():
            if m["owner"] == self.me:
                continue
            others.append((m["owner"], 4 * m["pub"] - 1.5 * math.log(max(0.5, m["price"]))))
        best = {}
        for o, u in others:
            best[o] = max(best.get(o, -1e9), u)
        u_me = 4 * fl["pub"] - 1.5 * math.log(price) + self.rep - 5 * self.drop
        mx = max([u_me] + list(best.values()))
        z = math.exp(u_me - mx) + sum(math.exp(u - mx) for u in best.values())
        return math.exp(u_me - mx) / z

    def choose_price(self, fl):
        if CFG["price"] != "opt":
            return CFG["fixed_price"]
        if fl is None:
            # 发布时：用即将发布的模型估计不到 pub，先按对手均价
            ps = [m["price"] for m in self.models.values() if m["owner"] != self.me]
            return round(sum(ps) / len(ps), 3) if ps else 10.0
        best = None
        for i in range(60):
            p = 1.0 * 1.08 ** i
            s = self.est_share(fl, p)
            v = p * s
            if best is None or v > best[0]:
                best = (v, p)
        return round(min(80.0, max(1.0, best[1])), 3)

    # ------------------------------------------------------------ 每周
    def weekly(self, act):
        if not self.wk:
            return
        if CFG["buy"]:
            self.buy(act)
            self.power(act)
        if CFG["hire"]:
            self.hire(act)
        if CFG["data"]:
            self.data(act)
        if CFG["raise"]:
            self.do_raise(act)

    def spendable(self):
        sal = sum(s["sal"] for s in self.staff)
        reserve = CFG["cash_floor"] + sal * 24 * 90
        return self.cash - reserve

    def buy(self, act):
        offer = self.wk.get("offer", {})
        cands = []
        for m, o in offer.items():
            if o["lots"] <= 0:
                continue
            if o["early"] and "early_supply" not in self.tech:
                continue
            price = max(o["reserve"] * 1.02, self.gpu[m]["price"] * self.heat * CFG["bid_mult"])
            cands.append((self.gpu[m]["power"] / price, m, price, o["lots"]))
        if not cands:
            return
        cands.sort(reverse=True)
        budget = self.spendable() * CFG["spend_frac"]
        if budget <= 0:
            return
        pue = 1.15 if "cooling" in self.tech else 1.3
        room_kw = self.mw_total() * 1000 - self.kw_installed()
        bids = []
        for _, m, price, lots in cands[:2]:
            lot_cost = price * 256
            kw_lot = 256 * self.gpu[m]["kw"] * pue
            k = int(min(budget // lot_cost, lots, max(0, room_kw + 4000) // kw_lot))
            if k <= 0:
                continue
            bids.append({"model": m, "lots": k, "price": round(price, 5)})
            budget -= k * lot_cost
            room_kw -= k * kw_lot
        if bids:
            act["bid_gpu"] = bids

    def power(self, act):
        need_kw = self.kw_installed()
        cap_kw = self.mw_total() * 1000
        s0 = self.sites[0]
        r0 = self.regions[s0["region"]]
        if s0["mw"] + s0.get("pend_mw", 0) < r0["cap_mw"] - 0.01 and self.cash > 30:
            add = r0["cap_mw"] - s0["mw"] - s0.get("pend_mw", 0)
            act["expand_site"] = [{"site": 0, "mw": round(add, 2)}]
            self.pend_mw += add
            return
        if need_kw > 0.6 * cap_kw and self.t - self.last_build > 6 * 168:
            built = {s["region"] for s in self.sites}
            opts = [r for r in self.regions if r["id"] not in built]
            if not opts:
                return
            r = min(opts, key=lambda r: r["price"] * 1000 + r["build_weeks"] * 0.01 - r["cap_mw"] * 0.00001)
            if self.spendable() > r["build_cost"] + 20:
                act["build_site"] = [{"region": r["id"]}]
                self.last_build = self.t

    def hire(self, act):
        year = min(2, self.t // 8760)
        cap = CFG["staff_cap"][year]
        n = len(self.staff)
        if n >= cap or self.spendable() < 30:
            return
        cands = sorted(self.wk.get("talent", []), key=lambda c: -(c["lo"] + 1) / c["ask"])
        hires = []
        for c in cands:
            if c["lo"] < 2:
                continue
            if n + len(hires) >= cap or len(hires) >= 4:
                break
            hires.append({"cand": c["id"], "salary": round(c["ask"] * 1.31, 7)})
        if hires:
            act["hire"] = hires

    def data(self, act):
        if self.t - getattr(self, "last_data", -10 ** 9) < 2 * 168:
            return
        sp = self.spendable()
        for d in self.wk.get("deals", []):
            if d["domain"] in ("web", "multiling"):
                continue
            if d["q_hint"] < 1.0 or d["tokens_b"] < 0.5 * self.U.get(d["domain"], 0):
                continue
            pr = round(d["reserve"] * 2.55, 3)
            if sp > 8 * pr:
                act["bid_data"] = [{"deal": d["id"], "price": pr}]
                self.last_data = self.t
                return

    def do_raise(self, act):
        q = self.t // 2190
        if q in self.raised_q or q == 0:
            return
        rev30 = sum(self.rev_ring)
        mine = [self.models[m]["pub"] for m in self.my_models if m in self.models]
        other = [m["pub"] for m in self.models.values() if m["owner"] != self.me]
        lead = max(0.0, (max(mine) if mine else 0) - (max(other) if other else 0))
        V = 20 * rev30 * 12 + 300 * lead + self.cash
        if V > 2.5 * max(self.cash, 1.0) and self.spendable() < CFG["raise_below"]:
            self.raised_q.add(q)
            act["raise"] = [{"amount": round(min(CFG["raise_cap"], V * CFG["raise_frac"]), 2)}]


def make_bot(init):
    return Lab(init)


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
