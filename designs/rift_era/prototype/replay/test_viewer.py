"""viewer.html 自检（不需要浏览器）。

用法（在 prototype/ 下）：python3 replay/test_viewer.py [回放或 index.json ...]
默认检查 replay/replay_seed1000.json 与 results/*/ 下现有的 .json.gz 与 index.json。

1. viewer.html < 80 KB，无外部资源（src/href 指向 http(s) 或 //）；恰有一个 <!--DEMO--> 标记；
2. 两段 <script> 抽出后 `node --check` 语法检查（有 node 时）；
3. 用 Python 实现与 viewer 相同的 schema 规则（RV.validateReplay / validateIndex），校验样本，
   并对若干人为损坏的文档断言报错；
4. 有 node 时把 viewer 的 core 脚本（id="core"）原样在 node 里执行：用同一个 parseBytes（含 gzip 解压）
   与 validateReplay 读同一批文件，断言错误 / 警告条数与 Python 规则一致。
"""
import glob, gzip, json, math, os, re, shutil, subprocess, sys, tempfile, copy

HERE = os.path.dirname(os.path.abspath(__file__))
PROTO = os.path.dirname(HERE)
VIEWER = os.path.join(HERE, 'viewer.html')


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def load(path):
    b = open(path, 'rb').read()
    if b[:2] == b'\x1f\x8b':
        b = gzip.decompress(b)
    t = b.decode('utf-8')
    if t.startswith('\ufeff'):
        t = t[1:]
    return json.loads(t)


def classify(d):
    if isinstance(d, dict) and d.get('kind') == 'rift_era_score_index':
        return 'index'
    if isinstance(d, dict) and ('meta' in d or 'cycle' in d or 'nations' in d):
        return 'replay'
    return None


def _check_arr(a, nF, where, W):
    if not isinstance(a, list):
        W.append(f'{where} 不是数组'); return False
    if len(a) != nF:
        W.append(f'{where} 长度 {len(a)} ≠ 帧数 {nF}'); return False
    for i, v in enumerate(a):
        if v is not None and not is_num(v):
            W.append(f'{where}[{i}] 不是数字'); break
    return True


def validate_replay(d):
    """与 viewer.html 中 RV.validateReplay 相同的规则；返回 (errors, warnings)。"""
    E, W = [], []
    if not isinstance(d, dict):
        return ['顶层不是 JSON 对象'], W
    m = d.get('meta')
    if not isinstance(m, dict):
        E.append('缺少 meta 对象')
    else:
        if 'game' in m and m['game'] != 'rift_era':
            E.append('meta.game 不是 rift_era')
        if is_num(m.get('format')) and m['format'] > 1:
            W.append('format 高于 1')
    cy = d.get('cycle')
    if not isinstance(cy, list) or not cy:
        E.append('cycle 必须是非空数组')
    else:
        for i, c in enumerate(cy):
            if not is_num(c):
                E.append(f'cycle[{i}] 不是数字'); break
            if i and c <= cy[i - 1]:
                E.append('cycle 必须严格递增'); break
    ns = d.get('nations')
    if not isinstance(ns, list) or not ns:
        E.append('nations 必须是非空数组')
    else:
        for i, n in enumerate(ns):
            if not isinstance(n, dict):
                E.append(f'nations[{i}] 不是对象')
    if E:
        return E, W
    nF = len(cy)
    if is_num(m.get('n_frames')) and m['n_frames'] != nF:
        W.append('n_frames 不符')
    for i, n in enumerate(ns):
        for k, v in list(n.items()):
            if isinstance(v, dict):
                for s, a in list(v.items()):
                    if not _check_arr(a, nF, f'nations[{i}].{k}.{s}', W): del v[s]
            elif not _check_arr(v, nF, f'nations[{i}].{k}', W):
                del n[k]
        if 'tp' not in n:
            W.append(f'nations[{i}] 缺 tp')
    w = d.get('world')
    if 'world' in d and not isinstance(w, dict):
        W.append('world 不是对象'); w = {}
    for k, a in (w or {}).items():
        _check_arr(a, nF, f'world.{k}', W)
    mn = m.get('nations')
    if not isinstance(mn, list):
        W.append('meta.nations 缺失'); mn = []
    if mn and len(mn) != len(ns):
        W.append('meta.nations 数量不符')
    ev = d.get('events')
    if 'events' in d and not isinstance(ev, list):
        W.append('events 不是数组'); ev = []
    bad = 0
    for e in ev or []:
        ok = (isinstance(e, list) and len(e) >= 3 and is_num(e[0]) and is_num(e[1]) and isinstance(e[2], str)
              and 0 <= e[1] < len(ns))
        bad += not ok
    if bad:
        W.append(f'{bad} 条事件格式不对')
    if 'score' in m and not isinstance(m['score'], dict):
        W.append('meta.score 不是对象')
    return E, W


def validate_index(d):
    E, W = [], []
    g = d.get('games')
    if not isinstance(g, list):
        E.append('缺 games')
    else:
        for i, x in enumerate(g):
            if not isinstance(x, dict):
                E.append(f'games[{i}] 不是对象')
            elif not is_num(x.get('seed')):
                W.append(f'games[{i}] 缺 seed')
    if 'score' in d and not isinstance(d['score'], dict):
        W.append('score 不是对象')
    return E, W


def check(d):
    k = classify(d)
    if k == 'index':
        return k, validate_index(d)
    if k == 'replay':
        return k, validate_replay(d)
    return None, (['无法识别'], [])


def broken_cases(sample):
    cases = {}
    cases['not_object'] = [1, 2, 3]
    x = copy.deepcopy(sample); del x['meta']; cases['no_meta'] = x
    x = copy.deepcopy(sample); x['meta']['game'] = 'farm'; cases['wrong_game'] = x
    x = copy.deepcopy(sample); x['cycle'] = []; cases['empty_cycle'] = x
    x = copy.deepcopy(sample); x['cycle'][5] = x['cycle'][4]; cases['cycle_not_increasing'] = x
    x = copy.deepcopy(sample); x['nations'] = []; cases['no_nations'] = x
    x = copy.deepcopy(sample); x['nations'][2] = 'oops'; cases['nation_not_object'] = x
    return cases


def soft_cases(sample):
    """应能加载（无错误）但有警告的文档。"""
    cases = {}
    x = copy.deepcopy(sample); x['nations'][0]['pop'] = x['nations'][0]['pop'][:-3]; x['events'].append(['bad']); cases['short_series+bad_event'] = x
    x = copy.deepcopy(sample)
    for n in x['nations']:
        for k in ('gen', 'labor', 'cash', 'e', 'cap'):
            n.pop(k, None)
    x.pop('world'); x['meta'].pop('nations'); x.pop('events'); cases['many_missing'] = x
    x = copy.deepcopy(sample); x['nations'] = x['nations'][:3]; x['meta']['nations'] = x['meta']['nations'][:3]
    x['events'] = [e for e in x['events'] if e[1] < 3]; cases['3_nations'] = x
    x = copy.deepcopy(sample); x['cycle'] = x['cycle'][:40]
    for n in x['nations']:
        for k, v in list(n.items()):
            if isinstance(v, dict):
                for s in v: v[s] = v[s][:40]
            else:
                n[k] = v[:40]
    x['world'] = {k: v[:40] for k, v in x['world'].items()}; x['meta']['n_frames'] = 40; cases['40_frames'] = x
    return cases


NODE_HARNESS = r'''
const fs=require('fs'),RV=require(process.argv[2]);
(async()=>{const out={};for(const p of process.argv.slice(3)){try{const d=await RV.parseBytes(new Uint8Array(fs.readFileSync(p)));const k=RV.classify(d);
 const v=k==='index'?RV.validateIndex(d):k==='replay'?RV.validateReplay(d):{errors:['无法识别'],warnings:[]};
 out[p]={kind:k,errors:v.errors.length,warnings:v.warnings.length,n:k==='replay'&&!v.errors.length?d.nations.length:null,ev:k==='replay'&&!v.errors.length?d.events.length:null}}
 catch(e){out[p]={kind:null,errors:1,warnings:0,exc:e.message}}}
 console.log(JSON.stringify(out))})();
'''


def main():
    ok = True
    def fail(s):
        nonlocal ok; ok = False; print('FAIL', s)
    html = open(VIEWER, encoding='utf-8').read()
    size = len(html.encode('utf-8'))
    print(f'viewer.html {size:,} B')
    if size >= 80 * 1024: fail('viewer.html ≥ 80 KB')
    ext = re.findall(r'''(?:src|href)\s*=\s*["'](?:https?:)?//''', html) + re.findall(r'@import|url\(\s*["\']?https?:', html)
    if ext: fail(f'外部资源引用 {ext}')
    if html.count('<!--DEMO-->') != 1: fail('DEMO 标记数 ≠ 1')
    scripts = dict(re.findall(r'<script id="(\w+)">(.*?)</script>', html, re.S))
    if set(scripts) != {'core', 'app'}: fail(f'脚本块 {list(scripts)}')
    node = shutil.which('node')
    tmp = tempfile.mkdtemp()
    if node:
        for k, s in scripts.items():
            p = os.path.join(tmp, f'{k}.js'); open(p, 'w', encoding='utf-8').write(s)
            r = subprocess.run([node, '--check', p], capture_output=True, text=True)
            print(f'node --check {k}: {"OK" if r.returncode == 0 else r.stderr}')
            if r.returncode: fail(f'{k} 语法错误')
    else:
        print('无 node，跳过 JS 语法检查与 node 端一致性检查')

    files = sys.argv[1:] or ([os.path.join(HERE, 'replay_seed1000.json')]
                             + sorted(glob.glob(os.path.join(PROTO, 'results', '*', '*.json.gz')))
                             + sorted(glob.glob(os.path.join(PROTO, 'results', '*', 'index.json'))))
    sample = load(os.path.join(HERE, 'replay_seed1000.json'))
    expect = {}
    for p in files:
        k, (E, W) = check(load(p))
        expect[p] = (k, len(E), len(W))
        print(f'{os.path.relpath(p, PROTO)}: {k} 错误 {len(E)} 警告 {len(W)}')
        if E: fail(f'{p}: {E}')
    for name, doc in broken_cases(sample).items():
        p = os.path.join(tmp, f'broken_{name}.json'); json.dump(doc, open(p, 'w'), ensure_ascii=False)
        k, (E, W) = check(doc); expect[p] = (k, len(E), len(W))
        print(f'损坏样例 {name}: 错误 {E}')
        if not E: fail(f'{name} 应报错')
    for name, doc in soft_cases(sample).items():
        p = os.path.join(tmp, f'soft_{name}.json.gz'); open(p, 'wb').write(gzip.compress(json.dumps(doc, ensure_ascii=False).encode()))
        k, (E, W) = check(doc); expect[p] = (k, len(E), len(W))
        print(f'缺字段样例 {name}: 错误 {len(E)} 警告 {len(W)}')
        if E: fail(f'{name} 不应报错: {E}')
    p = os.path.join(tmp, 'garbage.json.gz'); open(p, 'wb').write(b'\x1f\x8b\x08\x00garbage'); expect[p] = (None, 1, 0)
    if node:
        core = os.path.join(tmp, 'core.js')
        h = os.path.join(tmp, 'h.js'); open(h, 'w').write(NODE_HARNESS)
        r = subprocess.run([node, h, core] + list(expect), capture_output=True, text=True)
        if r.returncode: fail(f'node 端运行失败 {r.stderr}')
        else:
            got = json.loads(r.stdout)
            for p, (k, ne, nw) in expect.items():
                g = got[p]
                same = g['kind'] == k and (g['errors'] > 0) == (ne > 0) and (ne > 0 or g['warnings'] == nw)
                if not same: fail(f'node/py 不一致 {os.path.basename(p)}: node {g} py {(k, ne, nw)}')
            print(f'node 端（viewer 原样 core 脚本，含 gzip 解压）与 Python 规则一致：{len(expect)} 个文件')
            if got[p].get('exc'): print(f'  损坏 gzip → {got[p]["exc"]}')
    shutil.rmtree(tmp, ignore_errors=True)
    print('全部通过' if ok else '有失败')
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
