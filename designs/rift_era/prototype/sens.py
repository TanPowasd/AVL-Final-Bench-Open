import sys, sim, statistics
from bots import roster
from sim import NODES
bot = sys.argv[1]
for scale in [float(x) for x in sys.argv[2].split(',')]:
    sim.RP_SCALE = scale
    rows = []
    for seed in (1000, 1001):
        g = sim.Game(seed, [roster(seed)[bot]()]); dt = g.run(); n = g.nations[0]
        tiers = [NODES[t]['tier'] for t in n.known]
        rows.append((len(n.known), max(tiers or [0]), sum(1 for t in tiers if t >= 5), round(sum(n.rp_log)), round(n.pop), dt))
    print(f'RP×{scale:<4} {bot:12s}', ' | '.join(f'known {a} maxT {b} t5+ {c} RPsum {d} pop {e} {f:.1f}s' for a, b, c, d, e, f in rows), flush=True)
