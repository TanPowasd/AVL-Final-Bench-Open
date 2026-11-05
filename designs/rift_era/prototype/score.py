"""rift_era 原型评分入口（临时口径，不是 07 号正式五支柱公式）：选手 bot 对评分参考 Super，逐 seed 录回放。

用法（在 prototype/ 下）：
    python3 score.py tiers:Advanced                      # 8 个 OS 安全随机 seed（secrets），默认 2 并行，录回放
    python3 score.py advanced.Advanced -n 16 -j 1
    python3 score.py path/to/mybot.py:MyBot --seed 1000,2000   # 指定 seed = 重放测试，标记 label=replay
    python3 score.py tiers:Advanced --replay-dir results/adv_try --shadow-replay
    python3 score.py tiers:Advanced --no-replay          # 不录回放

选手 bot 写法：module:Class、module.Class、文件路径.py:Class，或 tiers 中的类名（Advanced / Medium / Super / Blank）。

每个 seed（第 k 个）：
- 选手局：选手坐席位 k mod 8，其余 7 席 tiers.Super（评分只用 Super，0011）；
- 影子局：同 seed 8 席全 Super；取同席位 TP 作 TP_shadow（按 seed 与引擎/参考源码指纹缓存，可 --no-shadow-cache）。
单局分：
- s      = clamp(50 · TP_player / TP_shadow, 0, 100)        原型临时分（同席 Super = 50）
- s_log2 = clamp(50 + 50 · log2(TP_player / TP_shadow), 0, 100)  log2 倍数提案分（附加列）
总分 = 各 seed 单局分等权平均；另报 TP 比的算术与几何均值。

回放：--replay-dir DIR（默认 results/score_<时间>_<bot>/）下每 seed 一个 replay_seed<seed>_seat<k>.json.gz
（meta.score 见 replay/REPLAY_FORMAT.md），--shadow-replay 另写 replay_seed<seed>_shadow.json.gz，
以及 index.json（全部 seed 汇总与总分）。不录回放时 index.json 仍写入同一目录。

故障：选手 act() 抛异常 → 该周期按空命令处理、继续跑完并计实分（errors 计数，记录首条 traceback）；
参考 Super 或引擎异常 → 整次评测 INVALID_EVALUATION（index.json 记录原因，退出码 2）。
注意：原型 bot 在进程内运行，技术上能读到 g.seed；正式评测需进程隔离（06/07 号）。
"""
import sys, os, json, math, time, argparse, hashlib, importlib, importlib.util, secrets, traceback, statistics
import multiprocessing as mp
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path: sys.path.insert(0, HERE)
import sim, tiers  # noqa: E402
from replay.record import Recorder, repo_head  # noqa: E402

N_SEATS = 8
REF = 'Super'
FINGERPRINT_FILES = (
    'sim.py', 'score.py', 'ref5.py', 'tiers.py', 'bots.py', 'medium.py', 'advanced.py',
    '../data/technology.json', '../data/industry.json', '../data/coupling.json', '../data/scoring.json',
)
DEFAULT_SEEDS = 8
OFFICIAL_SEEDS = 64


def fingerprint():
    h = hashlib.sha1()
    for f in FINGERPRINT_FILES:
        p = os.path.normpath(os.path.join(HERE, f))
        with open(p, 'rb') as fh: h.update(f.encode() + b'\0' + fh.read())
    return h.hexdigest()[:12]


def load_bot(spec):
    """返回 (类, 规范名)。"""
    if ':' in spec:
        mod, cls = spec.rsplit(':', 1)
    elif '.' in spec and not spec.endswith('.py'):
        mod, cls = spec.rsplit('.', 1)
    else:
        mod, cls = 'tiers', spec
    if mod.endswith('.py') or os.sep in mod:
        path = os.path.abspath(mod)
        sys.path.insert(0, os.path.dirname(path))
        name = os.path.splitext(os.path.basename(path))[0]
        sp = importlib.util.spec_from_file_location(name, path); M = importlib.util.module_from_spec(sp)
        sys.modules[name] = M; sp.loader.exec_module(M)
        canon = f'{path}:{cls}'
    else:
        M = importlib.import_module(mod); canon = f'{mod}:{cls}'
    return getattr(M, cls), canon


def guard(bot, log):
    """选手 act 异常 → 本周期空命令。只换实例上的 act，类型不变，不抛异常时行为完全相同。"""
    act = bot.act

    def safe_act(g, n):
        try:
            act(g, n)
        except Exception:
            log['errors'] += 1
            if log['first'] is None: log['first'] = f'cycle {g.cycle}: ' + traceback.format_exc(limit=6)
    bot.act = safe_act
    return bot


def play(job):
    """一局。job: dict(role, seed, seat, spec, record, every)。返回结果 dict（含回放 doc）。"""
    role, seed, seat, spec, record, every = (job[k] for k in ('role', 'seed', 'seat', 'spec', 'record', 'every'))
    try:
        names = [REF] * N_SEATS
        bots_ = [getattr(tiers, REF)() for _ in range(N_SEATS)]
        log = dict(errors=0, first=None)
        if role == 'player':
            B, canon = load_bot(spec)
            bots_[seat] = guard(B(), log); names[seat] = B.__name__
        g = sim.Game(seed, bots_)
        labels = None
        if role == 'player':
            labels = [f'N{i}·超级' for i in range(N_SEATS)]; labels[seat] = f'N{seat}·选手 {names[seat]}'
        rec = Recorder(g, frame_every=every, seating=names, labels=labels) if record else None
        t0 = time.perf_counter()
        for _ in range(sim.CYCLES):
            g.step()
            if rec: rec.after_step()
        el = time.perf_counter() - t0
        tps = [sim.tp(n) for n in g.nations]
        vs = [round(g.national_power(n)['V'], 3) for n in g.nations]
        doc = rec.finish() if rec else None
        return dict(ok=True, role=role, seed=seed, seat=seat, tps=tps, vs=vs, elapsed=el, errors=log['errors'],
                    first_error=log['first'], doc=doc)
    except Exception:
        return dict(ok=False, role=role, seed=seed, seat=seat, error=traceback.format_exc())


def map_log_tanh(r):
    if r is None or r <= 0: return 0.0
    return 50.0 + 50.0 * math.tanh(0.6 * math.log(r))


def score_one(tp_p, tp_s):
    if tp_s <= 0:
        return None, None, None, None
    r = tp_p / tp_s
    s = max(0.0, min(100.0, 50.0 * r))
    s2 = 0.0 if r <= 0 else max(0.0, min(100.0, 50.0 + 50.0 * math.log2(r)))
    st = map_log_tanh(r)
    return r, s, s2, st


def main():
    ap = argparse.ArgumentParser(description='rift_era 原型评分（选手 vs Super，影子局，回放）')
    ap.add_argument('bot', help='选手 bot：module:Class / module.Class / file.py:Class / tiers 类名')
    ap.add_argument('-n', '--seeds', type=int, default=DEFAULT_SEEDS, help=f'随机 seed 数（默认 {DEFAULT_SEEDS}；正式 64）')
    ap.add_argument('--seed', help='逗号分隔的指定 seed（重放测试，label=replay）')
    ap.add_argument('-j', '--jobs', type=int, default=2, help='并行局数（默认 2；2 核机器建议 1–2）')
    ap.add_argument('--replay-dir', help='回放与 index.json 输出目录（默认 results/score_<时间>_<bot>/）')
    ap.add_argument('--no-replay', action='store_true', help='不录回放（仍写 index.json）')
    ap.add_argument('--shadow-replay', action='store_true', help='影子局也录回放（会绕过影子缓存）')
    ap.add_argument('--every', type=int, default=10, help='回放每多少周期一帧（默认 10）')
    ap.add_argument('--no-shadow-cache', action='store_true', help='不读写影子局缓存')
    ap.add_argument('--official', action='store_true', help='正式评测模式（原型无进程沙箱，默认 Fail-Closed 拒绝标为 official_eval）')
    a = ap.parse_args()

    B, canon = load_bot(a.bot)
    if a.official:
        if a.seed or a.seeds != OFFICIAL_SEEDS or a.no_replay:
            sys.exit(f'INFRA_FAULT: official_eval 要求不指定 --seed、seeds=={OFFICIAL_SEEDS} 且开启回放录制（禁止 --no-replay）')
        sys.exit('INFRA_FAULT (SANDBOX_REQUIRED): prototype/score.py 为进程内校准原型，未挂载 Linux namespaces/cgroup v2/seccomp-bpf 隔离沙箱，Fail-Closed 拒绝生成 official=true 报告')
    if a.seed:
        seeds = [int(x) for x in a.seed.split(',')]; label = 'replay'
    else:
        rng = secrets.SystemRandom()
        seeds = rng.sample(range(1, 2 ** 31 - 1), a.seeds)
        label = 'practice_eval'
    if len(set(seeds)) != len(seeds): sys.exit('seed 有重复')
    seats = [k % N_SEATS for k in range(len(seeds))]
    stamp = time.strftime('%Y%m%d-%H%M%S')
    out = a.replay_dir or os.path.join(HERE, 'results', f'score_{stamp}_{B.__name__}' + ('_replay' if label == 'replay' else ''))
    os.makedirs(out, exist_ok=True)
    record = not a.no_replay
    fp = fingerprint()
    cache_path = os.path.join(HERE, 'results', 'shadow_cache.json')
    cache = {}
    if not a.no_shadow_cache and os.path.exists(cache_path):
        with open(cache_path) as f: cache = json.load(f)
    cache_fp = cache.get(fp, {})

    jobs, cached = [], {}
    for seed, seat in zip(seeds, seats):
        jobs.append(dict(role='player', seed=seed, seat=seat, spec=a.bot, record=record, every=a.every))
        ent = cache_fp.get(str(seed))
        if (isinstance(ent, dict) and isinstance(ent.get('tps'), list) and isinstance(ent.get('vs'), list)
                and len(ent['tps']) == N_SEATS and len(ent['vs']) == N_SEATS
                and all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in ent['tps'] + ent['vs'])
                and not a.shadow_replay):
            cached[seed] = ent
        else:
            jobs.append(dict(role='shadow', seed=seed, seat=seat, spec=REF, record=record and a.shadow_replay, every=a.every))
    print(f'[{label}] bot {canon}  seeds {len(seeds)}  games {len(jobs)} (影子缓存命中 {len(cached)})  jobs {a.jobs}  -> {out}', flush=True)
    if label != 'replay':
        print('  seed 由 secrets 抽取，局后写入 index.json', flush=True)

    t0 = time.time()
    res = {}
    invalid = None
    ctx = mp.get_context('fork')
    with ctx.Pool(max(1, a.jobs), maxtasksperchild=1) as pool:
        for r in pool.imap_unordered(play, jobs):
            if not r['ok']:
                invalid = f'{r["role"]} seed {r["seed"]}: {r["error"]}'
                print('INVALID_EVALUATION', invalid, flush=True); pool.terminate(); break
            res[(r['role'], r['seed'])] = r
            print(f'  {r["role"]:6s} seed {r["seed"]} seat {r["seat"]}  TP@seat {r["tps"][r["seat"]]:,}  {r["elapsed"]:.1f}s'
                  + (f'  选手异常 {r["errors"]} 次' if r['errors'] else ''), flush=True)
    elapsed = time.time() - t0

    rows = []
    if invalid is None:
        new_cache = False
        for seed, seat in zip(seeds, seats):
            p = res[('player', seed)]
            if seed in cached and 'vs' in cached[seed]:
                tps_shadow = cached[seed]['tps']; vs_shadow = cached[seed]['vs']; el_s = None
            elif ('shadow', seed) in res:
                sh = res[('shadow', seed)]; tps_shadow = sh['tps']; vs_shadow = sh['vs']; el_s = round(sh['elapsed'], 1)
                cache_fp[str(seed)] = dict(tps=tps_shadow, vs=vs_shadow); new_cache = True
            else:
                tps_shadow = cached[seed]['tps']; vs_shadow = None; el_s = None
            tp_p, tp_s = p['tps'][seat], tps_shadow[seat]
            ratio, s, s2, st = score_one(tp_p, tp_s)
            peers = [t for i, t in enumerate(p['tps']) if i != seat]
            v_p = p['vs'][seat]
            v_s = vs_shadow[seat] if vs_shadow else None
            v_peers = [v for i, v in enumerate(p['vs']) if i != seat]
            v_peers_shadow = [v for i, v in enumerate(vs_shadow) if i != seat] if vs_shadow else None
            if v_s is not None and v_peers_shadow is not None:
                v_ratio, s_v, _, _ = sim.score_v_one(v_p, v_s, v_peers, v_peers_shadow)
            else:
                v_ratio, s_v = None, None
            sc = dict(player_seat=seat, player_bot=canon, tp_player=tp_p, tp_shadow=tp_s,
                      tp_ratio=None if ratio is None else round(ratio, 6),
                      s=None if s is None else round(s, 3), s_log2=None if s2 is None else round(s2, 3),
                      s_tanh=None if st is None else round(st, 3),
                      v_player=v_p, v_shadow=v_s,
                      v_ratio=None if v_ratio is None else round(v_ratio, 6),
                      s_v=None if s_v is None else round(s_v, 3), label=label)
            row = dict(seed=seed, seat=seat, tp_player=tp_p, tp_shadow=tp_s, tp_ratio=sc['tp_ratio'],
                       s=sc['s'], s_log2=sc['s_log2'], s_tanh=sc['s_tanh'],
                       v_player=v_p, v_shadow=v_s, v_ratio=sc['v_ratio'], s_v=sc['s_v'],
                       tp_peers_mean=round(sum(peers) / len(peers), 1), tp_shadow_all=tps_shadow, tp_player_game_all=p['tps'],
                       v_shadow_all=vs_shadow, v_player_game_all=p['vs'],
                       player_errors=p['errors'], player_first_error=p['first_error'],
                       elapsed_player_s=round(p['elapsed'], 1), elapsed_shadow_s=el_s, shadow_cached=seed in cached)
            if record:
                fn = f'replay_seed{seed}_seat{seat}.json.gz'
                doc = p['doc']; doc['meta']['score'] = sc; doc['meta']['label'] = label
                size, raw = Recorder.save(doc, os.path.join(out, fn))
                row.update(replay=fn, replay_bytes=size, replay_raw_bytes=raw)
                if a.shadow_replay and ('shadow', seed) in res and res[('shadow', seed)]['doc']:
                    fn2 = f'replay_seed{seed}_shadow.json.gz'
                    d2 = res[('shadow', seed)]['doc']
                    d2['meta']['score'] = dict(player_seat=seat, player_bot=f'tiers:{REF}', tp_player=tp_s, tp_shadow=tp_s,
                                               tp_ratio=1.0, s=50.0, s_log2=50.0, label=label, role='shadow')
                    d2['meta']['label'] = label
                    size2, raw2 = Recorder.save(d2, os.path.join(out, fn2))
                    row.update(shadow_replay=fn2, shadow_replay_bytes=size2, shadow_replay_raw_bytes=raw2)
            rows.append(row)
        if new_cache and not a.no_shadow_cache:
            cache[fp] = cache_fp
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, 'w') as f: json.dump(cache, f)

    valid = [r for r in rows if r['s'] is not None]
    vs_ok = [r for r in rows if r.get('s_v') is not None]
    if invalid is None and (len(valid) < len(seeds) or len(vs_ok) < len(seeds)):
        invalid = f'有效局不足：valid_s={len(valid)}/{len(seeds)}, valid_s_v={len(vs_ok)}/{len(seeds)}'
        print('INVALID_EVALUATION', invalid, flush=True)
    final = None
    if invalid is None and valid and vs_ok:
        rs = [r['tp_ratio'] for r in valid]
        final = dict(S=round(statistics.mean(r['s'] for r in valid), 3),
                     S_log2=round(statistics.mean(r['s_log2'] for r in valid), 3),
                     S_tanh=round(statistics.mean(r['s_tanh'] for r in valid), 3),
                     S_V=round(statistics.mean(r['s_v'] for r in vs_ok), 3),
                     ratio_mean=round(statistics.mean(rs), 6),
                     ratio_geomean=round(math.exp(statistics.mean(math.log(x) for x in rs)), 6) if all(x > 0 for x in rs) else 0.0,
                     n=len(valid), n_seeds=len(seeds))
    index = dict(kind='rift_era_score_index', format=1, label=label, official=(label == 'official_eval' and invalid is None),
                 note='同时输出五支柱反焦土正式口径 S_V 与原型 TP 比 S',
                 status='INVALID_EVALUATION' if invalid else 'OK', invalid_reason=invalid,
                 created=time.strftime('%Y-%m-%dT%H:%M:%S%z'), rules='v1.3.1', repo_head=repo_head(), engine_fingerprint=fp,
                 player_bot=canon, reference=f'tiers:{REF}', seat_rule='seat = k mod 8，其余 7 席与影子局全为 Super',
                 score_def=dict(s='clamp(50*TP_player/TP_shadow, 0, 100)', s_log2='clamp(50+50*log2(TP_player/TP_shadow), 0, 100)',
                                final='各 seed 等权平均'),
                 seeds=seeds, seats=seats, cycles=sim.CYCLES, frame_every=a.every if record else None,
                 replays=record, shadow_replays=bool(record and a.shadow_replay),
                 jobs=a.jobs, elapsed_s=round(elapsed, 1), score=final, games=rows)
    with open(os.path.join(out, 'index.json'), 'w', encoding='utf-8') as f:
        json.dump(index, f, ensure_ascii=False, indent=1)

    if rows:
        print(f'\n{"seed":>11} seat {"TP_player":>10} {"TP_shadow":>10} {"ratio":>7} {"s":>6} {"s_tanh":>6} {"V_p":>7} {"s_V":>6} {"回放":>9}')
        for r in rows:
            print(f'{r["seed"]:>11} {r["seat"]:>4} {r["tp_player"]:>10,} {r["tp_shadow"]:>10,} {r["tp_ratio"] or 0:>7.3f} {r["s"] or 0:>6.1f} '
                  f'{r.get("s_tanh") or 0:>6.1f} {r.get("v_player") or 0:>7.1f} {r.get("s_v") or 0:>6.1f} {(r.get("replay_bytes") or 0) / 1024:>7.0f}KB')
    if final:
        print(f'总分 S {final["S"]:.2f}  S_tanh {final["S_tanh"]:.2f}  S_V(五支柱) {final["S_V"] or 0:.2f}  S_log2 {final["S_log2"]:.2f}'
              f'  TP 比 均值 {final["ratio_mean"]:.3f} 几何 {final["ratio_geomean"]:.3f}'
              f'  [{label}]  {elapsed:.0f}s  -> {os.path.join(out, "index.json")}')
    sys.exit(2 if invalid else 0)


if __name__ == '__main__':
    main()
