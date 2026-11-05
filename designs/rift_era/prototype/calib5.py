"""ref5 快速评估：4 席 × 2 种子，单席，其余白板。输出每局 TP、最高阶、6 阶首达周期、RP。
用法：python3 calib5.py [module:Class] [key=value ...]"""
import sys, importlib, multiprocessing as mp
SEEDS = (1000, 2000)
def run(a):
    spec, kw, i, seed = a
    import sim, bots
    m, c = spec.split(':')
    B = getattr(importlib.import_module(m), c)
    b = B()
    for k, v in kw.items(): setattr(b, k, v)
    bl = [bots.Blank() for _ in range(8)]; bl[i] = b
    g = sim.Game(seed, bl); n = g.nations[i]; t6 = None
    for c_ in range(sim.CYCLES):
        g.step()
        if t6 is None and any(sim.NODES[t]['tier'] == 6 for t in n.known): t6 = c_
    tp = sum(sim.TIERS[sim.NODES[t]['tier']]['theory_points'] for t in n.known)
    mt = max([sim.NODES[t]['tier'] for t in n.known] + [0])
    return i, seed, tp, mt, t6, round(sum(n.rp_log)), round(n.pop)
if __name__ == '__main__':
    spec = sys.argv[1] if len(sys.argv) > 1 and ':' in sys.argv[1] else 'ref5:Super'
    kw = {}
    for x in sys.argv[1:]:
        if '=' in x:
            k, v = x.split('='); kw[k] = float(v)
    jobs = [(spec, kw, i, sd) for sd in SEEDS for i in range(4)]
    with mp.Pool(2) as p: res = p.map(run, jobs)
    tps = [r[2] for r in res]
    for r in res: print('  seat', r[0], 'seed', r[1], 'TP', r[2], 'tier', r[3], 't6', r[4], 'RP', r[5], 'pop', r[6])
    print(spec, kw, 'TP mean', round(sum(tps) / len(tps)), 'min', min(tps), 'max', max(tps), 'tier6', sum(1 for r in res if r[3] == 6))
