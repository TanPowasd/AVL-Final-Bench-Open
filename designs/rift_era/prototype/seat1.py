"""单独观察第 i 席：其余 7 席为白板。用法：python3 seat1.py ref4 Super 1 [-v]"""
import sim, sys, importlib, bots
M = importlib.import_module(sys.argv[1]); i = int(sys.argv[3]); seed = 1000
bl = [bots.Blank() for _ in range(8)]; bl[i] = getattr(M, sys.argv[2])()
g = sim.Game(seed, bl); n = g.nations[i]; b = n.bot; marks = {}
for c in range(sim.CYCLES):
    g.step()
    mt = max([sim.NODES[t]['tier'] for t in n.known] + [0])
    if mt not in marks: marks[mt] = c
    if '-v' in sys.argv and c % 240 == 0:
        print('  ', c, 'pop', round(n.pop), 'lab', getattr(b, 'lab', ''), getattr(b, 'labs_now', ''), 'rp', round(n.rp_log[-1], 1), 'known', len(n.known),
              'proj', n.project and (n.project[0], round(n.project[1]), n.project[2]), 'lr', round(n.labor_ratio, 2), 'pr', round(n.power_ratio, 2), 'fr', round(n.food_ratio, 2),
              {k: round(v) for k, v in n.inv.items() if k in ('system_pack', 'lab_supplies', 'chemicals', 'coal', 'battery', 'chip', 'frontier_pack', 'precision_pack', 'food')})
print(i, [k for k in sim.STRATEGIC if k in n.deposits], 'RP', round(sum(n.rp_log)), 'marks', marks, 'known', len(n.known))
