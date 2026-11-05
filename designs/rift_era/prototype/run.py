#!/usr/bin/env python3
import sys, json, statistics, itertools
import sim
from sim import Game, NODES
from bots import roster

def match(seed, names):
    R = roster(seed)
    g = Game(seed, [R[k]() for k in names])
    dt = g.run()
    res = []
    for k, n in zip(names, g.nations):
        p = g.power(n)
        tiers = [NODES[t]['tier'] for t in n.known]
        res.append(dict(bot=k, total=p['total'], tech=p['tech'], industry=p['industry'], known=len(n.known),
                        max_tier=max(tiers, default=0), t5=sum(1 for x in tiers if x == 5), t6=sum(1 for x in tiers if x == 6),
                        workers=n.pop, fac=len(n.fac), rp=sum(n.rp_log), money=n.money, err=n.errors,
                        project=n.project))
    return dt, res

if __name__ == '__main__':
    seeds = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    names = sys.argv[2].split(',') if len(sys.argv) > 2 else ['industry', 'cheap', 'rush_fusion', 'rush_cyber']
    agg = {k: [] for k in names}; times = []
    for s in range(seeds):
        dt, res = match(1000 + s, names); times.append(dt)
        tot = sum(r['total'] for r in res) or 1
        for r in res:
            r['share'] = r['total'] / tot; agg[r['bot']].append(r)
        print(f'seed {1000+s}  {dt:.2f}s', ' | '.join(f"{r['bot']}: share {r['share']:.3f} known {r['known']} T{r['max_tier']} t5 {r['t5']} t6 {r['t6']} fac {r['fac']} pop {r['workers']:.0f} RP {r['rp']:.0f} proj {r['project'][0] if r['project'] else '-'}" for r in res), flush=True)
    print(f'\n平均 (每局 {sim.CYCLES} 周期, {len(names)} 国):', f'{statistics.mean(times):.2f}s/局')
    for k, rs in agg.items():
        print(f"{k:12s} share {statistics.mean(r['share'] for r in rs):.3f}  known {statistics.mean(r['known'] for r in rs):5.1f}  "
              f"maxT {statistics.mean(r['max_tier'] for r in rs):.1f}  t5 {statistics.mean(r['t5'] for r in rs):.1f}  t6 {statistics.mean(r['t6'] for r in rs):.1f}  "
              f"ind {statistics.mean(r['industry'] for r in rs):7.1f}  pop {statistics.mean(r['workers'] for r in rs):5.0f}  RP {statistics.mean(r['rp'] for r in rs):6.0f}  err {statistics.mean(r['err'] for r in rs):.0f}")
