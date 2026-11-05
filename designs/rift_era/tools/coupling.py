#!/usr/bin/env python3
"""Render and check the rift_era system coupling matrix (data/coupling.json -> 10_system_coupling.md).

check: ids valid; every non-sink mechanism has >= 2 outgoing and >= 2 incoming links;
the scoring sink has no outgoing link; the full graph minus the sink is strongly connected;
the economy-only graph used by the strategy prototype (econ mechanisms, existing links plus
new links flagged prototype=true) is strongly connected; every new link states a formula,
a cap and a lag; generated section is up to date.
render: rewrite the generated section of 10_system_coupling.md.
Python 3 standard library only. This does not validate dynamics or balance.
"""
import argparse
import json
import sys
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / '10_system_coupling.md'
START = '<!-- BEGIN GENERATED COUPLING -->'
END = '<!-- END GENERATED COUPLING -->'


def fail(msg):
    print('FAIL:', msg)
    sys.exit(1)


def edges(data, econ_only=False):
    layer = {m['id']: m['layer'] for m in data['mechanisms']}
    out = []
    for kind in ('existing', 'new'):
        for link in data[kind]:
            if econ_only and kind == 'new' and not link.get('prototype'):
                continue
            for t in link['to']:
                if econ_only and (layer[link['from']] != 'econ' or layer[t] != 'econ'):
                    continue
                out.append((link['from'], t, link['id']))
    return out


def distances(nodes, es):
    adj = {n: set() for n in nodes}
    for a, b, _ in es:
        if a in adj and b in adj and a != b:
            adj[a].add(b)
    dist = {}
    for s in nodes:
        d = {s: 0}
        q = deque([s])
        while q:
            u = q.popleft()
            for v in adj[u]:
                if v not in d:
                    d[v] = d[u] + 1
                    q.append(v)
        dist[s] = d
    return dist


def validate(data):
    ids = [m['id'] for m in data['mechanisms']]
    layer = {m['id']: m['layer'] for m in data['mechanisms']}
    seen = set()
    for kind in ('existing', 'new'):
        for link in data[kind]:
            if link['id'] in seen:
                fail(f'duplicate link id {link["id"]}')
            seen.add(link['id'])
            if link['from'] not in layer or any(t not in layer for t in link['to']):
                fail(f'unknown mechanism in {link["id"]}')
            if kind == 'new':
                for key in ('formula', 'cap', 'lag'):
                    if not link.get(key):
                        fail(f'{link["id"]} missing {key}')
    es = edges(data)
    sink = [m for m in ids if layer[m] == 'sink']
    for s in sink:
        if any(a == s for a, _, _ in es):
            fail(f'sink {s} must not feed back')
    core = [m for m in ids if layer[m] != 'sink']
    for m in core:
        outs = {b for a, b, _ in es if a == m and b != m}
        ins = {a for a, b, _ in es if b == m and a != m}
        if len(outs) < 2 or len(ins) < 2:
            fail(f'{m} insufficiently coupled: out={sorted(outs)} in={sorted(ins)}')
    dist = distances(core, es)
    for a in core:
        for b in core:
            if b not in dist[a]:
                fail(f'not strongly connected: no path {a} -> {b}')
    econ = [m for m in ids if layer[m] == 'econ']
    edist = distances(econ, edges(data, econ_only=True))
    for a in econ:
        for b in econ:
            if b not in edist[a]:
                fail(f'economy-only graph not strongly connected: no path {a} -> {b}')
    return ids, es, dist, edist


def render_body(data, ids, es, dist, edist):
    names = {m['id']: m['name'] for m in data['mechanisms']}
    cell = {}
    for a, b, lid in es:
        cell.setdefault((a, b), []).append(lid)
    lines = ['图例：`Exx` 既有耦合、`Cxx` 本次新增（0013 已采纳）；`·k` 表示无直接耦合、经 k 步间接传导；行 = 原因，列 = 结果；`SCO` 为只进不出的计分汇点。', '']
    lines.append('| 因 \\ 果 | ' + ' | '.join(ids) + ' |')
    lines.append('|' + '---|' * (len(ids) + 1))
    for a in ids:
        row = []
        for b in ids:
            if (a, b) in cell:
                row.append(' '.join(sorted(set(cell[(a, b)]))))
            elif a == b:
                row.append('—')
            elif a in dist and b in dist[a]:
                row.append(f'·{dist[a][b]}')
            else:
                row.append('·')
        lines.append(f'| **{a}** | ' + ' | '.join(row) + ' |')
    lines.append('')
    lines.append('| ID | 机制 |')
    lines.append('|---|---|')
    for m in data['mechanisms']:
        lines.append(f'| {m["id"]} | {m["name"]}（{ {"econ": "经济层", "mil": "军政层", "sink": "计分"}[m["layer"]] }） |')
    lines.append('')
    core = [m for m in ids if m in dist]
    maxd = max(dist[a][b] for a in core for b in core)
    econ = list(edist)
    emax = max(edist[a][b] for a in econ for b in econ)
    lines.append(f'连通性（`tools/coupling.py check` 自动校验）：除计分外 {len(core)} 个机制强连通，任意两机制最长 {maxd} 步；'
                 f'原型用的纯经济子图（{len(econ)} 个机制，只含既有耦合与 `prototype=true` 的新增耦合）同样强连通，最长 {emax} 步。'
                 f'既有耦合 {len(data["existing"])} 条，新增 {len(data["new"])} 条。')
    lines.append('')
    lines.append('### 既有耦合索引')
    lines.append('')
    lines.append('| ID | 因 → 果 | 内容 | 出处 |')
    lines.append('|---|---|---|---|')
    for e in data['existing']:
        lines.append(f'| {e["id"]} | {e["from"]} → {", ".join(e["to"])} | {e["what"]} | {e["ref"]} |')
    lines.append('')
    lines.append('### 新增耦合（公式、上限、原型适用）')
    lines.append('')
    lines.append('| ID | 因 → 果 | 名称 | 公式 | 有界性 | 原型 |')
    lines.append('|---|---|---|---|---|---|')
    for n in data['new']:
        f = n['formula'].replace('|', '\\|')
        lines.append(f'| {n["id"]} | {n["from"]} → {", ".join(n["to"])} | {n["name"]} | {f} | {n["cap"]} | {"是" if n["prototype"] else "否（军政层）"} |')
    return '\n'.join(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['check', 'render'])
    args = p.parse_args()
    data = json.loads((ROOT / 'data/coupling.json').read_text(encoding='utf-8'))
    ids, es, dist, edist = validate(data)
    body = render_body(data, ids, es, dist, edist)
    text = DOC.read_text(encoding='utf-8')
    if text.count(START) != 1 or text.count(END) != 1:
        fail('missing or duplicate COUPLING markers in 10_system_coupling.md')
    left, rest = text.split(START)
    _, right = rest.split(END)
    desired = left + START + '\n\n' + body + '\n\n' + END + right
    if args.command == 'render':
        DOC.write_text(desired, encoding='utf-8')
        print('rendered 10_system_coupling.md')
    elif text != desired:
        fail('stale generated section in 10_system_coupling.md; run render')
    print(f'PASS: {len(ids)} mechanisms, {len(data["existing"])} existing + {len(data["new"])} new couplings; '
          'non-sink graph and economy-only prototype graph strongly connected; every new coupling has formula, cap and lag.')


if __name__ == '__main__':
    main()
