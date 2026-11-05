import sim, sys, time, importlib
mod, cls = sys.argv[1], sys.argv[2]
seeds = [int(x) for x in (sys.argv[3] if len(sys.argv) > 3 else '1000,1001').split(',')]
M = importlib.import_module(mod)
for seed in seeds:
    g = sim.Game(seed, [getattr(M, cls)()]); n = g.nations[0]; b = n.bot; marks = {}; t0 = time.time()
    for c in range(sim.CYCLES):
        g.step()
        mt = max([sim.NODES[t]['tier'] for t in n.known] + [0])
        if mt not in marks: marks[mt] = c
        if c % 320 == 0 and '-v' in sys.argv:
            print('  ', c, 'pop', round(n.pop), 'lab', getattr(b, 'lab', ''), getattr(b, 'labs_now', ''), 'rp', round(n.rp_log[-1], 1), 'known', len(n.known),
                  'proj', n.project and (n.project[0], round(n.project[1]), n.project[2]),
                  'auto', sum(1 for f in n.fac for l in f.lines if l and l[1] == 'automated'), 'lines', sum(1 for f in n.fac for l in f.lines if l),
                  'lr', round(n.labor_ratio, 2), 'pr', round(n.power_ratio, 2))
    print(seed, 'known', len(n.known), 'RP', round(sum(n.rp_log)), 'pop', round(n.pop), 'marks', marks, round(time.time() - t0, 1), 's')
