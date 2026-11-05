"""8 席同局：每席一个同类 bot（或 mix 模式下按列表混坐），各席战略矿不同。
用法：python3 seats.py tiers Super [1000,2000]
      python3 seats.py mix Super,Advanced,Medium,Super,Advanced,Medium,Super,Advanced [1000]   （类名取自 tiers）
输出每席：seed 席位 bot 最高阶 5阶首达 6阶首达 累计研究点 人口 战略矿 已知节点数 TP 净资产RU AI_Cap 汇率 黑网次数 五支柱V 违约次数
另报 v1.3.1 国家间撮合：买外国证券RU、持有外国证券RU、累计发债、买入外国国债面值、推理服务收入/买入RU、授权收/付RU、国家间货物买/卖RU、售电量。
--replay-dir DIR：每个 seed 录一份回放 DIR/replay_seed<seed>_<mix|类名x8>.json.gz（replay/record.py Recorder，不改对局）。"""
import sim, sys, os, time, importlib, statistics
argv = sys.argv[1:]
replay_dir = None
if '--replay-dir' in argv:
    k = argv.index('--replay-dir'); replay_dir = argv[k + 1]; del argv[k:k + 2]
if replay_dir:
    from replay.record import Recorder
mod, cls = argv[0], argv[1]
seeds = [int(x) for x in (argv[2] if len(argv) > 2 else '1000').split(',')]
if mod == 'mix':
    M = importlib.import_module('tiers'); names = cls.split(',')
else:
    M = importlib.import_module(mod); names = [cls] * 8
rows = []; fin = []
for seed in seeds:
    g = sim.Game(seed, [getattr(M, c)() for c in names]); t0 = time.time()
    marks = [dict() for _ in g.nations]
    rec = Recorder(g, seating=names) if replay_dir else None
    for c in range(sim.CYCLES):
        g.step()
        if rec: rec.after_step()
        for i, n in enumerate(g.nations):
            mt = sim.max_tier(n)
            if mt not in marks[i]: marks[i][mt] = c
    for i, n in enumerate(g.nations):
        strat = [k for k in sim.STRATEGIC if k in n.deposits]
        rows.append((seed, i, names[i], max(marks[i]), marks[i].get(5), marks[i].get(6), round(sum(n.rp_log)), round(n.pop), '+'.join(strat), len(n.known), sim.tp(n), round(g.net_worth(n)), round(n.cap, 1), round(n.e, 2), n.blackouts, round(g.national_power(n)['V'], 1), n.defaults))
        held = sum(q * g.companies[k[1]].price * g.fx(g.companies[k[1]].nid, n.nid) * n.e for k, q in g.holdings.items() if k[0] == n.nid and q > 0 and g.companies[k[1]].nid != n.nid)
        fbond = sum(b.holders.get(n.nid, 0.0) for b in g.bonds if b.issuer != n.nid)
        fin.append((seed, i, names[i], round(n.sec_bought), round(held), round(n.bond_issued), round(fbond), round(n.ai_rev_cum), round(n.ai_buy_cum), round(n.lic_out), round(n.lic_in), round(n.goods_in), round(n.goods_out), round(n.power_sold)))
    if rec:
        fn = os.path.join(replay_dir, f'replay_seed{seed}_{"mix" if mod == "mix" else cls + "x8"}.json.gz')
        print(f'replay {fn} {Recorder.save(rec.finish(), fn)[0]:,} B', flush=True)
    print(f'seed {seed}: {time.time()-t0:.1f}s  stats {({k: round(v) for k, v in g.stats.items()})}', flush=True)
for r in rows: print(*r)
print('国家间撮合：seed 席位 bot 买外国证券RU 持有外国证券RU 累计发债 外国国债面值 推理收入RU 推理买入RU 授权收RU 授权付RU 货物买RU 货物卖RU 售电')
for r in fin: print(*r)
for nm in sorted(set(names)):
    tps = [r[10] for r in rows if r[2] == nm]
    vs = [r[15] for r in rows if r[2] == nm]
    print(nm, 'TP 均值', round(sum(tps) / len(tps)), '最小', min(tps), '最大', max(tps), '最高阶分布', {t: sum(1 for r in rows if r[2] == nm and r[3] == t) for t in range(7)},
          'V 均值', round(sum(vs) / len(vs), 1), '最小', min(vs), '最大', max(vs))
