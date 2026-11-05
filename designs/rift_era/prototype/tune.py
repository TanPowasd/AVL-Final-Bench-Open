"""并行评估 Super 的参数组合：4 种席位类型各跑一局（其余席位白板）。"""
import sys, json, itertools, multiprocessing as mp
def run(args):
    cfg, i = args
    import sim, bots, ref4
    b = ref4.Super()
    for k, v in cfg.items(): setattr(b, k, v)
    bl = [bots.Blank() for _ in range(8)]; bl[i] = b
    g = sim.Game(1000, bl); n = g.nations[i]; t6 = None; t5 = None
    for c in range(sim.CYCLES):
        g.step()
        mt = max([sim.NODES[t]['tier'] for t in n.known] + [0])
        if mt >= 5 and t5 is None: t5 = c
        if mt >= 6 and t6 is None: t6 = c
    tp = sum(sim.TIERS[sim.NODES[t]['tier']]['theory_points'] for t in n.known)
    return json.dumps(cfg), i, round(sum(n.rp_log)), tp, t5, t6
if __name__ == '__main__':
    cfgs = [json.loads(x) for x in sys.argv[1:]]
    jobs = [(c, i) for c in cfgs for i in range(4)]
    with mp.Pool(min(len(jobs), mp.cpu_count())) as p:
        res = p.map(run, jobs)
    from collections import defaultdict
    agg = defaultdict(list)
    for c, i, rp, tp, t5, t6 in res: agg[c].append((rp, tp, t5, t6))
    for c, v in agg.items():
        print(c, '已知科技理论点合计', [x[1] for x in v], '均值', round(sum(x[1] for x in v) / 4), '5阶', [x[2] for x in v], '6阶', [x[3] for x in v])
