import sim, sys, importlib
from collections import defaultdict, Counter
M = importlib.import_module(sys.argv[1]); seed = int(sys.argv[3]); T = int(sys.argv[4])
g = sim.Game(seed, [getattr(M, sys.argv[2])()]); n = g.nations[0]; b = n.bot
for c in range(T): g.step()
br = defaultdict(float); cnt = Counter()
for f in n.fac:
    for l in f.lines:
        if l:
            r = sim.RECIPES[l[0]]; k = f'{l[0]} {r["name"]} {l[1][:4]}'
            br[k] += r['workers'] * sim.MODES[l[1]]['worker_factor']; cnt[k] += 1
mw = sum(sim.EXTRACT[m.good][m.level][2] for m in n.mines)
print('cycle', T, 'pop', round(n.pop), 'labor', round(n.labor()), 'used', round(sum(br.values()) + mw), 'mines', round(mw), len(n.mines), 'lr', round(n.labor_ratio, 2), 'pr', round(n.power_ratio, 2))
for k, v in sorted(br.items(), key=lambda x: -x[1])[:18]: print(f'  {k:28s} {cnt[k]:3d} 条 {v:6.1f}')
print('known', sorted(n.known)); print('proj', n.project, 'lab', b.lab, 'goal', getattr(b, 'goal_lab', ''), 'labs_now', getattr(b, 'labs_now', ''), 'want', getattr(b, 'want_fac', ''), 'invest', getattr(b, 'invest', ''), 'mult', round(getattr(b, 'mult', 0), 2), 'power_goal', getattr(b, 'power_goal', ''))
print({k: round(v) for k, v in n.inv.items() if v >= 1})
