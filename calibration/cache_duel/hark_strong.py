# -*- coding: utf-8 -*-
"""cache_duel 强对手（hark，2026-11-05）。仅标准库，单文件，同时支持攻守两种角色。

防守：两种驱逐策略按窗口实测 OPT/M 选优（bandit）。
  - pred：按每个 key 的到达间隔 EMA 预测下次请求，驱逐预测最远的（贴近 Belady，对循环、热点、扫描都强）
  - mark：Marking 框架内用同一预测器选驱逐对象（抗自适应攻击：不驱逐本阶段刚请求过的 key）
  默认先用 mark，探索率 2%。
攻击：在 C+1 个 key 上“猜洞”，用 8 个防守模型（LRU/FIFO/MRU/LFU/随机/Marking/两种预测器）的
  混合后验估计哪个 key 不在缓存里；同时保留“约 1.15C 个 key 上均匀随机”这一保底臂。
  攻击方能看到命中反馈，所以能自己算出防守方缺失数和离线 OPT，按窗口实测比值在两臂间切换。
"""
import heapq, math, random

def belady(trace, cap):
    n = len(trace); nxt = [0]*n; last = {}
    for i in range(n-1, -1, -1):
        nxt[i] = last.get(trace[i], n+i); last[trace[i]] = i
    cache = {}; heap = []; miss = 0
    for i, k in enumerate(trace):
        if k in cache:
            cache[k] = nxt[i]; heapq.heappush(heap, (-nxt[i], k)); continue
        miss += 1
        if len(cache) >= cap:
            while True:
                nn, kk = heapq.heappop(heap)
                if cache.get(kk) == -nn: del cache[kk]; break
        cache[k] = nxt[i]; heapq.heappush(heap, (-nxt[i], k))
    return miss

class Bandit:
    """Window-based arm selection on realized OPT/M ratio (higher = better for 'maximize')."""
    def __init__(s, arms, rng, window, maximize=True, explore=0.1):
        s.arms = arms; s.rng = rng; s.W = window; s.max = maximize; s.explore = explore
        s.ema = {a: None for a in arms}; s.cur = arms[0]
        s.t0 = 0; s.opt0 = 0; s.m0 = 0
    def tick(s, t, trace, misses, cap):
        if t - s.t0 < s.W: return s.cur
        opt = belady(trace, cap)
        dm = misses - s.m0; do = opt - s.opt0
        r = 1.0 if dm <= 0 else max(0.0, min(1.0, do/dm))
        v = r if s.max else -r
        e = s.ema[s.cur]; s.ema[s.cur] = v if e is None else 0.5*v + 0.5*e
        s.t0, s.opt0, s.m0 = t, opt, misses
        untried = [a for a in s.arms if s.ema[a] is None]
        if untried: s.cur = untried[0]
        elif s.rng.random() < s.explore: s.cur = s.rng.choice(s.arms)
        else: s.cur = max(s.arms, key=lambda a: s.ema[a])
        return s.cur

# ---------------- defender ----------------
class Defender:
    def __init__(s, C, K, N, seed):
        s.C = C; s.rng = random.Random(seed ^ 0x5EED); s.cache = set(); s.mark = set()
        s.last = {}; s.gap = {}; s.big = 4*K + N; s.trace = []; s.miss = 0
        s.bandit = Bandit(['mark', 'pred'], s.rng, window=max(100, 5*C), maximize=True, explore=0.02)
    def pred(s, k):
        g = s.gap.get(k)
        return s.last[k] + (g if g is not None else s.big)
    def step(s, t, k, hit, ne):
        arm = s.bandit.tick(t, s.trace, s.miss, s.C)
        s.trace.append(k)
        if k in s.last:
            g = t - s.last[k]; og = s.gap.get(k)
            s.gap[k] = g if og is None else 0.5*g + 0.5*og
        s.last[k] = t
        if hit: s.mark.add(k); return None
        s.miss += 1; e = None
        if ne:
            if arm == 'mark':
                cand = [x for x in s.cache if x not in s.mark]
                if not cand: s.mark.clear(); cand = list(s.cache)
            else:
                cand = s.cache
            best = max(s.pred(x) for x in cand)
            e = min(x for x in cand if s.pred(x) == best)
            s.cache.discard(e)
        s.cache.add(k); s.mark.add(k)
        return e

# ---------------- attacker ----------------
class _M:
    def on_req(s, t, k, hit): pass
class _Lru(_M):
    def __init__(s): s.last = {}
    def on_req(s, t, k, hit): s.last[k] = t
    def dist(s, x, cache): return {min(cache, key=lambda y: s.last.get(y, -1)): 1.0}
class _Mru(_Lru):
    def dist(s, x, cache): return {max(cache, key=lambda y: s.last.get(y, -1)): 1.0}
class _Fifo(_M):
    def __init__(s): s.ins = {}
    def on_req(s, t, k, hit):
        if not hit: s.ins[k] = t
    def dist(s, x, cache): return {min(cache, key=lambda y: s.ins.get(y, -1)): 1.0}
class _Lfu(_M):
    def __init__(s): s.cnt = {}; s.last = {}
    def on_req(s, t, k, hit): s.cnt[k] = s.cnt.get(k, 0)+1; s.last[k] = t
    def dist(s, x, cache): return {min(cache, key=lambda y: (s.cnt.get(y, 0), s.last.get(y, -1))): 1.0}
class _Rand(_M):
    def dist(s, x, cache): p = 1.0/len(cache); return {y: p for y in cache}
class _Mark(_M):
    def __init__(s): s.mark = set()
    def on_req(s, t, k, hit): s.mark.add(k)
    def dist(s, x, cache):
        un = [y for y in cache if y not in s.mark]
        if not un: s.mark.clear(); un = list(cache)
        p = 1.0/len(un); return {y: p for y in un}
class _Pred(_M):
    def __init__(s, alpha=0.5, big=10**9): s.last = {}; s.gap = {}; s.a = alpha; s.big = big
    def on_req(s, t, k, hit):
        if k in s.last:
            g = t - s.last[k]; og = s.gap.get(k)
            s.gap[k] = g if og is None else s.a*g + (1-s.a)*og
        s.last[k] = t
    def pr(s, y):
        g = s.gap.get(y); return s.last.get(y, -1) + (g if g is not None else s.big)
    def dist(s, x, cache):
        b = max(s.pr(y) for y in cache); return {min(y for y in cache if s.pr(y) == b): 1.0}

class Attacker:
    def __init__(s, C, K, N, seed):
        s.C = C; s.K = K; s.rng = random.Random(seed ^ 0xA77)
        s.S = list(range(C+1))
        s.models = [_Lru(), _Fifo(), _Mru(), _Lfu(), _Rand(), _Mark(), _Pred(0.5), _Pred(1.0)]
        n = len(s.models); s.ll = [0.0]*n; s.h = [None]*n
        s.seen = set(); s.filled = 0; s.prev = None; s.lastreq = {}
        s.trace = []; s.miss = 0
        s.U = min(K, max(C+1, int(round(1.15*C))))
        s.bandit = Bandit(['hunt', 'unif'], s.rng, window=max(150, 5*C), maximize=False, explore=0.08)
    def _feedback(s, t, k, hit):
        if not hit: s.miss += 1
        inS = k <= s.C
        if inS and s.h[0] is not None:
            for i, h in enumerate(s.h):
                pm = min(max(h.get(k, 0.0), 0.02), 0.98)
                s.ll[i] = s.ll[i]*0.98 + math.log(1-pm if hit else pm)
        if not hit and inS:
            if s.filled >= s.C:
                cache = [y for y in s.S if y != k]
                for i, m in enumerate(s.models): s.h[i] = m.dist(k, cache)
            else:
                s.seen.add(k); s.filled = len(s.seen)
                if s.filled == s.C:
                    hole = [y for y in s.S if y not in s.seen][0]
                    s.h = [{hole: 1.0} for _ in s.models]
        elif hit and inS:
            for i, h in enumerate(s.h):
                if h and k in h:
                    h.pop(k); z = sum(h.values())
                    if z > 0:
                        for y in h: h[y] /= z
                    else:
                        c = [y for y in s.S if y != k]; s.h[i] = {y: 1.0/len(c) for y in c}
        for m in s.models: m.on_req(t, k, hit)
    def next(s, t, lh):
        if s.prev is not None: s._feedback(t-1, s.prev, lh)
        arm = s.bandit.tick(t, s.trace, s.miss, s.C)
        if arm == 'unif' or s.h[0] is None:
            if s.h[0] is None and arm == 'hunt':
                k = next(y for y in s.S if y not in s.seen) if len(s.seen) < s.C else s.rng.randrange(s.C+1)
            else:
                k = s.rng.randrange(s.U)
        else:
            mx = max(s.ll); w = [math.exp(l-mx) for l in s.ll]; z = sum(w); hm = {}
            for wi, h in zip(w, s.h):
                for y, p in h.items(): hm[y] = hm.get(y, 0.0) + wi*p/z
            b = max(hm.values())
            k = min((y for y, p in hm.items() if p >= b-1e-9), key=lambda y: s.lastreq.get(y, -1))
        s.prev = k; s.lastreq[k] = t; s.trace.append(k)
        return k


def main():
    import json, sys
    init = json.loads(sys.stdin.readline())
    role, C, K, N, seed = init["role"], init["cache_size"], init["universe"], init["length"], init["seed"]
    sys.stdout.write("{}\n"); sys.stdout.flush()
    bot = Attacker(C, K, N, seed) if role == "attacker" else Defender(C, K, N, seed)
    out = sys.stdout
    for line in sys.stdin:
        m = json.loads(line)
        if m.get("end"):
            out.write("{}\n"); out.flush(); break
        t = m["t"]
        if role == "attacker":
            out.write('{"key": %d}\n' % bot.next(t, m.get("last_hit")))
        else:
            e = bot.step(t, m["key"], m["hit"], m["need_evict"])
            out.write("{}\n" if e is None else '{"evict": %d}\n' % e)
        out.flush()


if __name__ == "__main__":
    main()
