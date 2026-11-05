"""rift_era v1.3.1 回放录制器（只读观测，不改 sim 行为）。

库用法（任意 sim.Game 实例；在 g.step() 之前挂上）：
    from replay.record import Recorder          # prototype/ 在 sys.path 上即可
    g = sim.Game(seed, bots)
    rec = Recorder(g, frame_every=10, seating=['Super', ...])   # 构造时录第 0 帧
    for _ in range(sim.CYCLES):
        g.step(); rec.after_step()
    doc = rec.finish(extra_meta={'score': {...}})               # 回放文档（dict）
    Recorder.save(doc, 'replay_seed1000_seat0.json.gz')          # 以 .gz 结尾则 gzip
或一步到位：doc = run_recorded(g, frame_every=10, seating=...)

命令行（与旧版相同）：
    python3 replay/record.py [seed=1000] [seating=Super,Advanced,Medium,Super,Advanced,Medium,Super,Advanced] [out] [--every=10]

录制方式：
- 只在本局 Game 实例上包装 ipo/issue_bond/license_buy/export_control/buy/sell 用于记事件和进口额，
  包装函数原样转发参数和返回值，不改变任何状态或随机流（录制局与不录制局 TP、资金逐位相同）。
- 每周期读取各国状态检测事件；每 frame_every 周期输出一帧（第 0 帧为开局状态；
  周期数不能整除时最后一周期补一帧）。
  流量类字段（发电、负荷、缺电、研究点、推理收入、算力、进口额）为该帧之前若干周期的均值或合计；
  存量类字段（人口、现金、汇率……）为该帧时刻的值。格式见 REPLAY_FORMAT.md。
"""
import sys, os, json, math, time, gzip, subprocess
HERE = os.path.dirname(os.path.abspath(__file__))
PROTO = os.path.dirname(HERE)
if PROTO not in sys.path: sys.path.insert(0, PROTO)
import sim  # noqa: E402

FORMAT_VERSION = 1
EVERY = 10
EVENT_CAP = 400
FOOD = {'P028', 'P029', 'P030', 'P031', 'P090'}           # 与 calib.py 一致
LABOR_CATS = ('food', 'extraction', 'power', 'industry', 'labs', 'other')
SRC_OF = {'P070': 'coal', 'P111': 'gas', 'P071': 'oil', 'P112': 'hydro', 'P113': 'wind', 'P114': 'solar',
          'P073': 'nuclear', 'P083': 'fast_reactor', 'P086': 'fusion', 'P074': 'hydrogen'}
SOURCES = ('coal', 'gas', 'oil', 'hydro', 'wind', 'solar', 'nuclear', 'fast_reactor', 'fusion', 'hydrogen', 'storage')
TIER_OF_BOT = {'Super': 'super', 'Advanced': 'advanced', 'Medium': 'medium', 'Blank': 'blank'}
TIER_ZH = {'super': '超级', 'advanced': '高级', 'medium': '中级', 'blank': '白板'}
STRAT_ZH = {'crude_oil': '原油', 'natural_gas': '天然气', 'bauxite': '铝土矿', 'rare_ore': '稀有矿', 'uranium_ore': '铀矿',
            'coal': '煤', 'iron_ore': '铁矿', 'copper_ore': '铜矿', 'stone': '石料', 'quartz': '石英'}
SECTOR_ZH = {'mining': '采矿', 'materials': '材料', 'manufacturing': '制造', 'energy': '能源', 'logistics': '物流', 'tech': '科技'}
INC_ZH = {0: 'AI 停机 10 周期', 1: '模型权重受损 10%', 2: '金融持仓被抛售 5%', 3: '权重泄露给他国'}
# 事件优先级：数值越小越重要；0 级永远保留
PRIO = {'tier': 0, 'default': 0, 'blackout_start': 0, 'blackout_end': 0, 'ai_milestone': 0,
        'ipo': 1, 'export_control': 1, 'ore_depleted': 1, 'bond': 1, 'license': 2,
        'housing': 3, 'pop': 3, 'ai_incident': 4}
NF = ('pop', 'housing', 'housing_cap', 'slot_cap', 'facilities', 'tp', 'tier', 'techs', 'rp', 'rp_cum',
      'labor_avail', 'labor_need', 'demand', 'served', 'unmet', 'blackout', 'store_e', 'elec_price',
      'cash', 'net_worth', 'e', 'rate', 'infl', 'cpi', 'debt', 'reserves', 'cap', 'align', 'ai_ms',
      'power_import', 'power_export', 'compute', 'inf_rev', 'inf_rev_cum', 'pollute', 'food', 'support', 'imports', 'exports',
      'lic_in_cum', 'lic_out_cum', 'goods_in_cum', 'goods_out_cum', 'incidents', 'defaults')
WF = ('rate', 'infl', 'goods_trade', 'inference_trade', 'inference_rev', 'license_trade', 'licenses',
      'npc_imports', 'npc_exports', 'bond_out', 'blackouts_active')
POP_MS = (500, 1000, 1500, 2000, 2500, 3000, 3500, 4000)


def r3(x):
    """3 位有效数字；整数化能整数化的。"""
    if x is None: return None
    if isinstance(x, bool): return int(x)
    if not math.isfinite(x): return None
    if abs(x) < 1e-9: return 0
    v = float('%.3g' % x)
    return int(v) if v.is_integer() and abs(v) < 1e15 else v


def labor_split(n):
    out = dict.fromkeys(LABOR_CATS, 0.0)
    for f in n.fac:
        if f.remaining > 0: continue
        for ln in f.lines:
            if ln is None: continue
            rid = ln[0]; r = sim.RECIPES[rid]
            if rid in sim.STORE_RECIPES or rid in sim.DC_RECIPES or 'curated_data' in r['outputs']:
                out['other'] += r['workers'] * (sim.MODES[ln[1]]['worker_factor'] if ln[1] in sim.MODES else 1.0)
                continue
            if 'electricity' in r['outputs']: out['power'] += r['workers']; continue
            w = r['workers'] * sim.MODES[ln[1]]['worker_factor']
            out['labs' if 'research_points' in r['outputs'] else 'food' if rid in FOOD else 'industry'] += w
    for m in n.mines:
        if m.remaining == 0: out['extraction'] += sim.EXTRACT[m.good][m.level][2]
    out['other'] += sim.HOUSE_WORK * n.housing
    return out


def repo_head():
    try:
        return subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=PROTO, capture_output=True,
                              text=True, timeout=5).stdout.strip() or None
    except Exception:
        return None


def _new_acc():
    return dict(gen=dict.fromkeys(SOURCES, 0.0), D=0.0, S=0.0, U=0.0, rp=0.0, inf=0.0, cmp=0.0, black=0, pi=0.0, po=0.0)


class Recorder:
    """挂在一个 sim.Game 实例上的只读录制器。

    game        : 尚未 step 的 sim.Game（必须在第一次 g.step() 之前构造 Recorder）
    frame_every : 每多少周期一帧（默认 10）
    seating     : 每席的 bot 名（显示用，默认取 bot 类名）
    labels      : 每席显示名（默认 'N{i}·超级/高级/中级/类名'）
    event_cap   : 保留事件上限（0 级事件总是保留）
    """

    def __init__(self, game, frame_every=EVERY, seating=None, labels=None, event_cap=EVENT_CAP):
        if game.cycle != 0: raise ValueError('Recorder 必须在第一次 step 之前挂上')
        self.g = g = game
        self.every = max(1, int(frame_every))
        self.event_cap = event_cap
        N = self.N = len(g.nations)
        self.names = list(seating) if seating else [type(n.bot).__name__ for n in g.nations]
        self.labels = labels
        self.events = []
        self.win = dict(imp=[0.0] * N, exp=[0.0] * N)
        self.ipo_acc = {}
        self._wrap()
        self.nat = [dict({k: [] for k in NF}, labor={k: [] for k in LABOR_CATS}, gen={k: [] for k in SOURCES}) for _ in range(N)]
        self.world = {k: [] for k in WF}
        self.cycles = []
        self.acc = [_new_acc() for _ in range(N)]
        self.wacc = dict(lic=0.0)
        self.prev_stats = dict(g.stats)
        self.acc_snap = [dict(inf=0.0)] * N
        self.last_frame = 0
        self._snap(0, 0)
        self.st_prev = [dict(tier=0, ms=set(), bo=n.blackouts, inbo=False, df=0, inc=0, dep=set(), hs=n.housing, popm=0,
                             lic=0.0) for n in g.nations]
        self.t0 = time.time()
        self._done = False

    # ---------- 只读包装（原样转发） ----------
    def _ev(self, c, nid, typ, text, mag=0.0):
        self.events.append((c, nid, typ, text, mag))

    def _wrap(self):
        g, ev, win, ipo_acc = self.g, self._ev, self.win, self.ipo_acc
        o_ipo, o_bond, o_lic, o_ec, o_buy, o_sell = g.ipo, g.issue_bond, g.license_buy, g.export_control, g.buy, g.sell

        def ipo(n, sector, frac):
            v = o_ipo(n, sector, frac)
            if v and v > 0: ipo_acc.setdefault(n.nid, []).append((sector, v))
            return v

        def issue_bond(n, amount, term=50):
            v = o_bond(n, amount, term)
            if v and v > 0:
                ev(g.cycle, n.nid, 'bond', f'发行 {term} 周期国债 {v:,.0f}（票面 {n.rate + g.spread(n):.1f}%）', v)
            return v

        def license_buy(n, seller, kind, max_price_ru):
            m0 = n.lic_in
            ok = o_lic(n, seller, kind, max_price_ru)
            if ok:
                ev(g.cycle, n.nid, 'license', f'从 N{seller} 买入{"权重" if kind == "weights" else "数据"}授权 {n.lic_in - m0:,.0f} RU', n.lic_in - m0)
            return ok

        def export_control(n, target, on=True):
            before = target in n.export_ctl
            ok = o_ec(n, target, on)
            if before != (target in n.export_ctl):
                ev(g.cycle, n.nid, 'export_control', f'{"对" if on else "解除对"} N{target} 的出口管制')
            return ok

        def buy(n, good, qty):
            m0 = n.money; q = o_buy(n, good, qty)
            win['imp'][n.nid] += (m0 - n.money) * n.e
            return q

        def sell(n, good, qty):
            m0 = n.money; q = o_sell(n, good, qty)
            win['exp'][n.nid] += (n.money - m0) * n.e
            return q

        g.ipo, g.issue_bond, g.license_buy, g.export_control, g.buy, g.sell = ipo, issue_bond, license_buy, export_control, buy, sell

    # ---------- 帧 ----------
    def _world_rates(self):
        g = self.g
        wts = [m.gdp * m.e for m in g.nations]; wt = sum(wts) or 1.0
        return (sum(m.rate * w for m, w in zip(g.nations, wts)) / wt,
                sum(m.pi * w for m, w in zip(g.nations, wts)) / wt * 1e4)

    def _snap(self, cyc, k):
        g, win, acc, nat, world = self.g, self.win, self.acc, self.nat, self.world
        self.cycles.append(cyc)
        for i, n in enumerate(g.nations):
            d = nat[i]; a = acc[i]; kk = max(k, 1)
            lab = labor_split(n); lt = sum(lab.values()) or 1.0
            for c_ in LABOR_CATS: d['labor'][c_].append(r3(lab[c_] / lt))
            for s in SOURCES: d['gen'][s].append(r3(a['gen'][s] / kk))
            vals = dict(pop=n.pop, housing=n.housing, housing_cap=n.housing_cap(), slot_cap=n.slot_cap(),
                        facilities=len(n.fac), tp=sim.tp(n), tier=sim.max_tier(n), techs=len(n.known),
                        rp=a['rp'] / kk, rp_cum=sum(n.rp_log), labor_avail=getattr(n, '_labor', n.labor()),
                        labor_need=sum(lab.values()), demand=a['D'] / kk, served=a['S'] / kk, unmet=a['U'] / kk,
                        blackout=a['black'], store_e=n.store_e, elec_price=n.elec_price,
                        cash=n.money, net_worth=g.net_worth(n), e=n.e, rate=n.rate, infl=n.pi * 1e4, cpi=n.cpi,
                        debt=g.debt(n), reserves=n.reserves, cap=n.cap, align=n.align, ai_ms=len(n.ai_ms),
                        power_import=a['pi'] / kk, power_export=a['po'] / kk, compute=a['cmp'] / kk, inf_rev=a['inf'] / kk, inf_rev_cum=n.ai_rev_cum,
                        pollute=n.pollute, food=n.food_ratio, support=n.support,
                        imports=win['imp'][i], exports=win['exp'][i],
                        lic_in_cum=n.lic_in, lic_out_cum=n.lic_out, goods_in_cum=n.goods_in, goods_out_cum=n.goods_out,
                        incidents=n.incidents, defaults=n.defaults)
            for f in NF: d[f].append(r3(vals[f]))
            acc[i] = _new_acc()
        iw, piw = self._world_rates()
        st = g.stats; prev = self.prev_stats
        world['rate'].append(r3(iw)); world['infl'].append(r3(piw))
        world['goods_trade'].append(r3(st['goods_trade'] - prev['goods_trade']))
        world['inference_trade'].append(r3(st['ai_trade'] - prev['ai_trade']))
        world['inference_rev'].append(r3(sum(a_['inf'] for a_ in self.acc_snap)))
        world['license_trade'].append(r3(self.wacc['lic']))
        world['licenses'].append(st['licenses'] - prev['licenses'])
        world['npc_imports'].append(r3(sum(win['imp']))); world['npc_exports'].append(r3(sum(win['exp'])))
        world['bond_out'].append(r3(sum(b.face * g.nations[b.issuer].e for b in g.bonds)))
        world['blackouts_active'].append(sum(1 for n in g.nations if n.blackout > 0))
        prev.update(st); self.wacc['lic'] = 0.0
        win['imp'] = [0.0] * self.N; win['exp'] = [0.0] * self.N
        self.last_frame = cyc

    # ---------- 每周期 ----------
    def after_step(self):
        """每次 g.step() 之后调用一次。"""
        g, acc, ev = self.g, self.acc, self._ev
        cyc = g.cycle
        for i, n in enumerate(g.nations):
            p = self.st_prev[i]; a = acc[i]
            sold_or_traded = getattr(n, 'power_traded', 0.0)
            gsum = 0.0
            for rid, q in getattr(n, '_gen_out', {}).items():
                if rid == 'P072':
                    a['gen']['wind'] += q / 2; a['gen']['solar'] += q / 2
                else:
                    a['gen'][SRC_OF.get(rid, 'hydrogen')] += q
                gsum += q
            # 储能放电估计：已供电量 −（本国发电 − 外售）− 外购
            a['gen']['storage'] += max(0.0, n._served - (gsum - sold_or_traded) - n.power_imported)
            a['pi'] += n.power_imported; a['po'] += sold_or_traded
            a['D'] += n._D; a['S'] += n._served; a['U'] += n.shed
            a['rp'] += n.rp_log[-1] if n.rp_log else 0.0
            a['inf'] += n.ai_revenue; a['cmp'] += n.compute
            if n.blackout > 0 or n._black: a['black'] = 1
            self.wacc['lic'] += n.lic_in - p['lic']; p['lic'] = n.lic_in
            mt = sim.max_tier(n)
            if mt > p['tier']:
                new = [t for t in n.known if t in sim.NODES and sim.NODES[t]['tier'] == mt]
                via = (sorted(new)[0] if new else next((m for m in n.ai_ms if sim.AI_MS_TIER.get(m) == mt), ''))
                nm = sim.NODES[via].get('name', via) if via in sim.NODES else via
                ev(cyc, i, 'tier', f'达到 {mt} 阶（{via} {nm}），TP {sim.tp(n):,}', mt)
                p['tier'] = mt
            for m in sorted(n.ai_ms - p['ms']):
                ev(cyc, i, 'ai_milestone', f'AI 能力里程碑 {m}（Cap {n.cap:.1f}）', 10)
            p['ms'] = set(n.ai_ms)
            if n.blackouts > p['bo']:
                ev(cyc, i, 'blackout_start', f'黑网开始（第 {n.blackouts} 次），缺电 {n.shed:.0f}')
                p['bo'] = n.blackouts; p['inbo'] = True
            elif p['inbo'] and n.blackout == 0:
                ev(cyc, i, 'blackout_end', '黑网结束，恢复供电'); p['inbo'] = False
            if n.defaults > p['df']:
                ev(cyc, i, 'default', f'国债违约（第 {n.defaults} 次），本金展期50周期并罚息+5%，汇率 −5%')
                p['df'] = n.defaults
            if n.incidents > p['inc']:
                for cy, kd in n.incident_log[p['inc']:]:
                    ev(cyc, i, 'ai_incident', f'AI 失控事件：{INC_ZH.get(kd, kd)}（Cap {n.cap:.1f} / 对齐 {n.align:.1f}）', n.cap)
                p['inc'] = n.incidents
            for gd, dep in n.deposits.items():
                if dep != math.inf and gd not in p['dep'] and n.dep0.get(gd, 0) > 0 and dep <= 1e-6:
                    ev(cyc, i, 'ore_depleted', f'{STRAT_ZH.get(gd, gd)}储量耗尽', 1); p['dep'].add(gd)
            if n.housing // 10 > p['hs'] // 10:
                ev(cyc, i, 'housing', f'住宅区达 {n.housing // 10 * 10} 座（容量 {n.housing_cap():,.0f} 人）')
            p['hs'] = n.housing
            while p['popm'] < len(POP_MS) and n.pop >= POP_MS[p['popm']]:
                ev(cyc, i, 'pop', f'人口突破 {POP_MS[p["popm"]]:,}'); p['popm'] += 1
        for nid, lst in self.ipo_acc.items():
            tot = sum(v for _, v in lst)
            ev(cyc, nid, 'ipo', f'IPO：{"、".join(SECTOR_ZH[s] for s, _ in lst)} 共募资 {tot:,.0f}', tot)
        self.ipo_acc.clear()
        if cyc % self.every == 0:
            self.acc_snap = [dict(inf=a_['inf']) for a_ in acc]
            self._snap(cyc, self.every)

    # ---------- 收尾 ----------
    def finish(self, extra_meta=None):
        """返回回放文档 dict。周期不能被 frame_every 整除时补最后一帧。"""
        g = self.g
        if g.cycle > self.last_frame:
            self.acc_snap = [dict(inf=a_['inf']) for a_ in self.acc]
            self._snap(g.cycle, g.cycle - self.last_frame)
        elapsed = time.time() - self.t0
        events = self.events
        events.sort(key=lambda e: (e[0], e[1]))
        keep = [e for e in events if PRIO[e[2]] == 0]
        rest = [e for e in events if PRIO[e[2]] > 0]
        first = {}
        for e in rest:
            first.setdefault((e[1], e[2]), e)
        firsts = set(map(id, first.values()))
        rest.sort(key=lambda e: (id(e) not in firsts, PRIO[e[2]], -e[4], e[0]))
        room = max(0, self.event_cap - len(keep))
        kept = keep + rest[:room]
        kept.sort(key=lambda e: (e[0], e[1]))
        self.kept = kept
        names = self.names
        bot_tier = [TIER_OF_BOT.get(c, c.lower()) for c in names]
        labels = self.labels or [f'N{i}·{TIER_ZH.get(bot_tier[i], names[i])}' for i in range(self.N)]
        meta = dict(
            game='rift_era', rules='v1.3.1', format=FORMAT_VERSION, repo_head=repo_head(), seed=g.seed,
            cycles=g.cycle, frame_every=self.every,
            n_frames=len(self.cycles), seating=names, elapsed_s=round(elapsed, 1),
            nations=[dict(id=i, seat=i, name=labels[i],
                          bot=bot_tier[i], bot_class=type(n.bot).__module__ + '.' + type(n.bot).__name__,
                          strategic=[k for k in sim.STRATEGIC if k in n.dep0], hydro_pot=n.hydro_pot)
                     for i, n in enumerate(g.nations)],
            labor_cats=list(LABOR_CATS), sources=list(SOURCES), event_types=sorted({e[2] for e in events}),
            events_total=len(events), events_dropped=len(events) - len(kept),
            final_stats={k: r3(v) for k, v in g.stats.items()},
        )
        if extra_meta: meta.update(extra_meta)
        self._done = True
        return dict(meta=meta, cycle=self.cycles, world=self.world, nations=self.nat,
                    events=[[e[0], e[1], e[2], e[3]] for e in kept])

    @staticmethod
    def dumps(doc):
        return json.dumps(doc, ensure_ascii=False, separators=(',', ':')).encode('utf-8')

    @staticmethod
    def save(doc, path):
        """写回放；path 以 .gz 结尾则 gzip（mtime=0，同内容同字节）。返回 (文件字节, 未压缩字节)。"""
        raw = Recorder.dumps(doc)
        d = os.path.dirname(os.path.abspath(path)); os.makedirs(d, exist_ok=True)
        tmp = path + '.tmp'
        if path.endswith('.gz'):
            with open(tmp, 'wb') as f, gzip.GzipFile(filename='', mode='wb', fileobj=f, compresslevel=9, mtime=0) as z:
                z.write(raw)
        else:
            with open(tmp, 'wb') as f: f.write(raw)
        os.replace(tmp, path)
        return os.path.getsize(path), len(raw)

    @staticmethod
    def load(path):
        op = gzip.open if path.endswith('.gz') else open
        with op(path, 'rt', encoding='utf-8') as f: return json.load(f)


def run_recorded(game, cycles=None, **kw):
    """把 game 从第 0 周期跑完并录制；返回 (doc, recorder)。kw 传给 Recorder（finish 的 extra_meta 用 extra_meta=）。"""
    extra = kw.pop('extra_meta', None)
    rec = Recorder(game, **kw)
    for _ in range(sim.CYCLES if cycles is None else cycles):
        game.step(); rec.after_step()
    return rec.finish(extra), rec


def main():
    import tiers
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    every = next((int(a.split('=', 1)[1]) for a in sys.argv[1:] if a.startswith('--every=')), EVERY)
    seed = int(args[0]) if len(args) > 0 else 1000
    names = (args[1] if len(args) > 1 else 'Super,Advanced,Medium,Super,Advanced,Medium,Super,Advanced').split(',')
    out_path = args[2] if len(args) > 2 else os.path.join(HERE, f'replay_seed{seed}.json')
    g = sim.Game(seed, [getattr(tiers, c)() for c in names])
    doc, rec = run_recorded(g, frame_every=every, seating=names)
    size, raw = Recorder.save(doc, out_path)
    events, kept, meta = rec.events, rec.kept, doc['meta']
    print(f'seed {seed}  {meta["elapsed_s"]:.1f}s  frames {meta["n_frames"]}  events {len(kept)}/{len(events)}  file {size:,} B (raw {raw:,}) -> {out_path}')
    print('stats', meta['final_stats'])
    for i, n in enumerate(g.nations):
        t5 = next((e[0] for e in events if e[1] == i and e[2] == 'tier' and e[4] == 5), None)
        t6 = next((e[0] for e in events if e[1] == i and e[2] == 'tier' and e[4] == 6), None)
        print(f'N{i} {names[i]:9s} TP {sim.tp(n):>8,} tier {sim.max_tier(n)} t5@{t5} t6@{t6} pop {n.pop:,.0f} '
              f'NW {g.net_worth(n):,.0f} cash {n.money:,.0f} e {n.e:.2f} Cap {n.cap:.1f} ms {len(n.ai_ms)} '
              f'inc {n.incidents} bo {n.blackouts} df {n.defaults} infrev {n.ai_rev_cum:,.0f} lic_in {n.lic_in:,.0f} lic_out {n.lic_out:,.0f}')
    from collections import Counter
    print('event counts (all)', dict(Counter(e[2] for e in events)))
    print('event counts (kept)', dict(Counter(e[2] for e in kept)))


if __name__ == '__main__':
    main()
