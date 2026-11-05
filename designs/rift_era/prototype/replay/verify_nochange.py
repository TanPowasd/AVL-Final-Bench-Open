"""校验 Recorder 零行为改变：同 seed 同座次，不录 vs 录（frame_every 可改），逐国比较 TP、现金、人口、净资产、汇率。
用法：python3 replay/verify_nochange.py [seed=1000] [seating=Super,Advanced,Medium,Super,Advanced,Medium,Super,Advanced] [every=10]
两局并行（2 进程）。全部逐位相同则打印 IDENTICAL 并退出 0。"""
import sys, os, multiprocessing as mp
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import sim, tiers  # noqa: E402
from replay.record import Recorder  # noqa: E402


def run(a):
    seed, names, every = a
    g = sim.Game(seed, [getattr(tiers, c)() for c in names])
    rec = Recorder(g, frame_every=every, seating=names) if every else None
    for _ in range(sim.CYCLES):
        g.step()
        if rec: rec.after_step()
    nf = rec.finish()['meta']['n_frames'] if rec else None
    return [(sim.tp(n), n.money, n.pop, g.net_worth(n), n.e, len(n.known), n.cap) for n in g.nations], dict(g.stats), nf


if __name__ == '__main__':
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
    names = (sys.argv[2] if len(sys.argv) > 2 else 'Super,Advanced,Medium,Super,Advanced,Medium,Super,Advanced').split(',')
    every = int(sys.argv[3]) if len(sys.argv) > 3 else 10
    with mp.Pool(2) as p: (a, sa, _), (b, sb, nf) = p.map(run, [(seed, names, 0), (seed, names, every)])
    for i, (x, y) in enumerate(zip(a, b)):
        print(f'N{i} {names[i]:9s} TP {x[0]:>8,} / {y[0]:>8,}  {"=" if x == y else "DIFF " + str((x, y))}')
    same = a == b and sa == sb
    print('IDENTICAL' if same else 'DIFFERENT', f'(seed {seed}, frame_every {every}, frames {nf})')
    sys.exit(0 if same else 1)
