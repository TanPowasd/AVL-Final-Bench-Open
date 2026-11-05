"""三档校准：每个 bot 单独坐 4 种席位（其余白板），2 个种子，报已知科技理论点合计与最高阶。
用法：python3 calib.py [--econ] bots:Blank tiers:Advanced tiers:Medium tiers:Super ...
--econ 另报经济指标（同一批对局）：累计研究点、局末人口与资金、加工增加值（sim.value_log，
按 VALUE 估值，全局均值与末 240 周期均值）、劳动需求结构（民生食品/采掘/工业/电力/实验室，
每 10 周期采样，按已开工产线与采掘场的用工需求计）、平均电力与食品满足率、进出口额（货币）。
v1.3：TP = sim.tp（含 AI 能力里程碑等价理论点）；--econ 另报净资产（RU）、局末人口、AI 能力 Cap 与里程碑数、
局末汇率、股票/债券持有（RU）、IPO 募资、国债余额、AI 失控事件、黑网次数、电价、污染指数、CPI、局末现金。
--replay-dir DIR：每局录一份回放 DIR/replay_seed<seed>_seat<i>_<模块_类>.json.gz（其余席白板；不改对局）。"""
import sys, os, time, importlib, multiprocessing as mp
SEEDS = (1000, 2000)
FOOD = {'P028', 'P029', 'P030', 'P031', 'P090'}
CATS = ('food', 'mine', 'ind', 'power', 'lab')


def labor_split(sim, n):
    out = dict.fromkeys(CATS, 0.0)
    for f in n.fac:
        if f.remaining > 0: continue
        for ln in f.lines:
            if ln is None: continue
            r = sim.RECIPES[ln[0]]
            if 'electricity' in r['outputs']: out['power'] += r['workers']; continue
            w = r['workers'] * sim.MODES[ln[1]]['worker_factor']
            out['lab' if 'research_points' in r['outputs'] else 'food' if ln[0] in FOOD else 'ind'] += w
    for m in n.mines:
        if m.remaining == 0: out['mine'] += sim.EXTRACT[m.good][m.level][2]
    return out


def run(a):
    spec, i, seed, econ, replay_dir = a
    import sim, bots
    m, c = spec.split(':')
    if m == 'H':   # bots.py 里的启发式 Planner 预设
        B = lambda: bots.Planner(c, 'deep' if c != 'cheap' else 'cheap', lab_share=0.40 if c != 'cheap' else 0.15,
                                 target=['R03', 'R07', 'R05', 'E11', 'R11'] if c != 'cheap' else (), seed=seed)
    else:
        B = getattr(importlib.import_module(m), c)
    bl = [bots.Blank() for _ in range(8)]; bl[i] = B()
    g = sim.Game(seed, bl); n = g.nations[i]
    ex = dict(imp=0.0, exp=0.0, pr=0.0, fr=0.0, k=0); lab = dict.fromkeys(CATS, 0.0)
    if econ:   # 只在本局实例上包一层记账，不改 sim
        buy0, sell0 = g.buy, g.sell
        def buy(nn, good, qty):
            m0 = nn.money; q = buy0(nn, good, qty)
            if nn is n: ex['imp'] += m0 - nn.money
            return q
        def sell(nn, good, qty):
            m0 = nn.money; q = sell0(nn, good, qty)
            if nn is n: ex['exp'] += nn.money - m0
            return q
        g.buy, g.sell = buy, sell
    rec = None
    if replay_dir:
        from replay.record import Recorder
        seat_names = ['Blank'] * 8; seat_names[i] = c
        rec = Recorder(g, seating=seat_names)
    t0 = time.perf_counter()
    for c in range(sim.CYCLES):
        g.step()
        if rec: rec.after_step()
        if econ:
            ex['pr'] += n.power_ratio; ex['fr'] += n.food_ratio
            if c % 10 == 0:
                for k, v in labor_split(sim, n).items(): lab[k] += v
                ex['k'] += 1
    if rec:
        Recorder.save(rec.finish(), os.path.join(replay_dir, f'replay_seed{seed}_seat{i}_{spec.replace(":", "_")}.json.gz'))
    tp = sim.tp(n)
    mt = sim.max_tier(n)
    np = g.national_power(n)
    e = None
    if econ:
        tot = sum(lab.values()) or 1.0
        e = dict(rp=sum(n.rp_log), pop=n.pop, money=n.money, V=np['V'], phi=np['phi'],
                 va=sum(n.value_log) / len(n.value_log), va240=sum(n.value_log[-240:]) / 240,
                 pr=ex['pr'] / sim.CYCLES, fr=ex['fr'] / sim.CYCLES, imp=ex['imp'], exp=ex['exp'],
                 nw=g.net_worth(n), cap=n.cap, ms=len(n.ai_ms), e=n.e, nfa=g.nfa(n), ipo=n.ipo_cash, debt=g.debt(n),
                 inc=n.incidents, blk=n.blackouts, elec=n.elec_price, poll=n.pollute, cpi=n.cpi, rate=n.rate,
                 bond=n.bond_issued, dflt=n.defaults, airev=n.ai_rev_cum, aibuy=n.ai_buy_cum, secb=n.sec_bought, lic=n.lic_in + n.lic_out,
                 goods=n.goods_in + n.goods_out, psold=n.power_sold, hous=n.housing, slots=n.slot_cap(), nfac=len(n.fac),
                 sec=sum(q * g.companies[k[1]].price * g.nations[g.companies[k[1]].nid].e for k, q in g.holdings.items() if k[0] == n.nid),
                 secs=time.perf_counter() - t0,
                 labor=lab['food'] / ex['k'] + lab['mine'] / ex['k'] + lab['ind'] / ex['k'] + lab['power'] / ex['k'] + lab['lab'] / ex['k'],
                 **{'sh_' + k: lab[k] / tot for k in CATS})
    return spec, i, seed, tp, mt, e


def stat(xs):
    return sum(xs) / len(xs), min(xs), max(xs)


if __name__ == '__main__':
    args = sys.argv[1:]
    econ = '--econ' in args
    replay_dir = None
    if '--replay-dir' in args:
        k = args.index('--replay-dir'); replay_dir = args[k + 1]; del args[k:k + 2]
    specs = [a for a in args if a != '--econ']
    jobs = [(s, i, sd, econ, replay_dir) for s in specs for sd in SEEDS for i in range(4)]
    with mp.Pool(mp.cpu_count()) as p: res = p.map(run, jobs)
    for s in specs:
        r = [x for x in res if x[0] == s]
        tps = [x[3] for x in r]; mts = [x[4] for x in r]
        print(f'{s:14s} 理论点均值 {sum(tps)/len(tps):8.0f} 最小 {min(tps):6d} 最大 {max(tps):6d} 最高阶 {mts}')
    if econ:
        print('\n经济（均值 [最小–最大]）')
        for s in specs:
            es = [x[5] for x in res if x[0] == s]
            f = lambda k, d=0: '%s [%s–%s]' % tuple(f'{v:,.{d}f}' for v in stat([e[k] for e in es]))
            print(f'{s}:')
            print(f'  累计研究点 {f("rp")}  局末人口 {f("pop")}  局末资金 {f("money")}  五支柱国力V {f("V", 1)} (Φ {f("phi", 2)})')
            print(f'  加工增加值/周期 全局 {f("va", 1)}  末240周期 {f("va240", 1)}')
            print(f'  电力满足率 {f("pr", 3)}  食品满足率 {f("fr", 3)}  进口额 {f("imp")}  出口额 {f("exp")}')
            sh = '  '.join(f'{k} {sum(e["sh_" + k] for e in es) / len(es):.0%}' for k in CATS)
            print(f'  用工需求 {f("labor", 1)}/周期；结构 {sh}')
            print(f'  净资产RU {f("nw")}  NFA {f("nfa")}  IPO募资 {f("ipo")}  国债 {f("debt")}  证券持有RU {f("sec")}')
            print(f'  汇率 {f("e", 3)}  利率 {f("rate", 1)}  CPI {f("cpi", 2)}  电价 {f("elec", 2)}  污染 {f("poll", 2)}')
            print(f'  累计发债 {f("bond")}  违约 {f("dflt", 1)}  推理服务收入RU {f("airev")}  买入推理RU {f("aibuy")}  买外国证券RU {f("secb")}  授权RU {f("lic")}  国家间货物RU {f("goods")}  售电 {f("psold")}')
            print(f'  住宅区 {f("hous")}  槽位上限 {f("slots")}  设施数 {f("nfac")}')
            print(f'  AI Cap {f("cap", 1)}  里程碑 {f("ms", 1)}  失控事件 {f("inc", 1)}  黑网 {f("blk", 1)}  耗时 {f("secs", 1)} s/局')
