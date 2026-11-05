"""生成 viewer_demo.html：viewer.html + 内置示例回放（gzip + base64），双击即可离线打开看示例。

用法（在 prototype/ 下）：
    python3 replay/make_demo.py                                  # 默认内置 replay/replay_seed1000.json
    python3 replay/make_demo.py results/x/replay_seed1_seat0.json.gz -o replay/viewer_demo.html

file:// 下浏览器不允许 fetch 本地文件，所以示例直接内联进 HTML；viewer.html 本身不变、不带数据。
内联的是紧凑 JSON 的 gzip（压缩级 9，mtime=0）再 base64；页面用 DecompressionStream('gzip') 解开，
走与「导入回放」相同的解析与校验路径。
"""
import argparse, base64, gzip, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
MARK = '<!--DEMO-->'


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('replay', nargs='?', default=os.path.join(HERE, 'replay_seed1000.json'), help='要内置的回放（.json 或 .json.gz）')
    ap.add_argument('-o', '--out', default=os.path.join(HERE, 'viewer_demo.html'))
    ap.add_argument('--viewer', default=os.path.join(HERE, 'viewer.html'))
    a = ap.parse_args()
    raw = open(a.replay, 'rb').read()
    if raw[:2] == b'\x1f\x8b':
        raw = gzip.decompress(raw)
    doc = json.loads(raw.decode('utf-8'))
    if not isinstance(doc, dict) or 'meta' not in doc or 'cycle' not in doc:
        sys.exit(f'{a.replay} 不是回放文件（缺 meta / cycle）')
    compact = json.dumps(doc, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    gz = gzip.compress(compact, compresslevel=9, mtime=0)
    b64 = base64.b64encode(gz).decode('ascii')
    html = open(a.viewer, encoding='utf-8').read()
    if html.count(MARK) != 1:
        sys.exit(f'{a.viewer} 中应恰有一个 {MARK} 标记')
    name = os.path.basename(a.replay)
    inj = '<script>window.RIFT_DEMO=' + json.dumps({'name': name, 'data': b64}, ensure_ascii=False) + ';</script>'
    out = html.replace(MARK, inj)
    with open(a.out, 'w', encoding='utf-8') as f:
        f.write(out)
    print(f'{a.out}: {len(out.encode()):,} B（viewer {len(html.encode()):,} B + 回放 {len(compact):,} B → gzip {len(gz):,} B → base64 {len(b64):,} B）')


if __name__ == '__main__':
    main()
