"""ref5 调试：单席详细轨迹。用法：python3 dbg5.py seat [seed] [every]"""
import sim, sys, bots, importlib, time
from collections import Counter
mod = importlib.import_module(sys.argv[4] if len(sys.argv) > 4 else 'ref5')
i = int(sys.argv[1]); seed = int(sys.argv[2]) if len(sys.argv) > 2 else 1000; ev = int(sys.argv[3]) if len(sys.argv) > 3 else 120
bl = [bots.Blank() for _ in range(8)]; bl[i] = mod.Super()
g = sim.Game(seed, bl); n = g.nations[i]; b = n.bot; marks = {}; t0 = time.time()
for c in range(sim.CYCLES):
    g.step()
    mt = max([sim.NODES[t]['tier'] for t in n.known] + [0])
    if mt not in marks: marks[mt] = c
    if c % ev == 0:
        labs = Counter(l[0] for f in n.fac for l in f.lines if l and l[0] in mod.LAB_RP)
        modes = Counter(l[1][:4] for f in n.fac for l in f.lines if l)
        print(c, 'pop', round(n.pop), 'L', round(getattr(b, 'L', 0)), 'w', round(getattr(b, 'work_est', 0)), 'labs', dict(labs), 'tgt', b.labs_target, 'rp', round(n.rp_log[-1], 1),
              'known', len(n.known), 'proj', n.project and (n.project[0], round(n.project[1])), 'bg', [(x[0], x[1]) for x in n.background],
              'lr %.2f pr %.2f fr %.2f' % (n.labor_ratio, n.power_ratio, n.food_ratio), 'money', round(n.money), 'fac', len(n.fac), 'mines', len(n.mines), dict(modes), 'cs', getattr(b, 'capex_scale', None), 'wb', b.want_build, flush=True)
        print('    inv', {k: round(v) for k, v in n.inv.items() if v >= 1 and k not in ('water',)})
tp = sum(sim.TIERS[sim.NODES[t]['tier']]['theory_points'] for t in n.known)
print('seat', i, [k for k in sim.STRATEGIC if k in n.deposits], 'TP', tp, 'RP', round(sum(n.rp_log)), 'marks', marks, 'known', len(n.known), 'time', round(time.time() - t0, 1))
