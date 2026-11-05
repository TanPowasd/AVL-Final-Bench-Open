#!/usr/bin/env python3
"""Check and render design catalogs. This is not a game or balance simulator."""

import argparse
import json
import math
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def keyed(items, label):
    result = {}
    for item in items:
        key = item['id']
        require(key not in result, f'{label}: duplicate ID {key}')
        result[key] = item
    return result


def positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def validate(tech, industry):
    require(tech['schema_version'] == industry['schema_version'] == 1, 'Unknown schema')
    branches = keyed(tech['branches'], 'branch')
    research = keyed(tech['technologies'], 'technology')
    # v1.3: capability milestones share the technology ID namespace but are unlocked by
    # model capability Cap, not by theory points; they carry no research tier.
    milestones = keyed(tech.get('capability_milestones', []), 'capability milestone')
    require(not research.keys() & milestones.keys(), f'ID used twice: {research.keys() & milestones.keys()}')
    nodes = {**research, **milestones}
    goods = keyed(industry['goods'], 'good')
    recipes = keyed(industry['recipes'], 'recipe')
    profiles = keyed(industry['facility_profiles'], 'facility')
    modes = keyed(industry['line_modes'], 'mode')
    tiers = {t['tier']: t for t in tech['research_tiers']}
    require(len(tiers) == len(tech['research_tiers']), 'Duplicate research tier')

    def references(ids, label):
        require(len(ids) == len(set(ids)), f'{label}: duplicate prerequisite')
        require(set(ids) <= nodes.keys(), f'{label}: unknown technology {set(ids) - nodes.keys()}')

    def amounts(values, label, inventory_only=True):
        require(set(values) <= goods.keys(), f'{label}: unknown goods {set(values) - goods.keys()}')
        for key, value in values.items():
            require(positive(value), f'{label}: invalid amount of {key}')
            if inventory_only:
                require(goods[key]['kind'] == 'inventory', f'{label}: flow used as inventory: {key}')

    for node in nodes.values():
        require(node['branch'] in branches, f"Unknown branch for {node['id']}")
        if node['id'] in milestones:
            require(node.get('unlock') == 'capability', f"{node['id']}: milestone must have unlock=capability")
            require('tier' not in node, f"{node['id']}: capability milestone has no research tier")
            require(positive(node.get('cap_threshold')), f"{node['id']}: invalid cap_threshold")
            require(node.get('milestone') in ('algorithm', 'deployment', 'frontier'), f"{node['id']}: unknown milestone kind")
            require(node['requires_all'], f"{node['id']}: capability milestone needs a prerequisite")
        else:
            require(node.get('unlock', 'research') == 'research' and 'cap_threshold' not in node,
                    f"{node['id']}: research node cannot carry capability fields")
            require(node['tier'] in tiers, f"Unknown tier for {node['id']}")
        require(node['unlocks'], f"Missing effect for {node['id']}")
        references(node['requires_all'], node['id'])
        references(node['requires_any'], node['id'])
    groups = keyed(tech.get('exclusive_groups', []), 'exclusive group')
    for gid, group in groups.items():
        require(len(group['members']) >= 2, f'{gid}: exclusive group needs at least two members')
        references(group['members'], gid)
        require(group.get('rule'), f'{gid}: missing switch rule')
        for member in group['members']:
            require(nodes[member].get('exclusive_group') == gid, f'{member}: exclusive_group does not name {gid}')
    for node in nodes.values():
        if 'exclusive_group' in node:
            require(node['exclusive_group'] in groups and node['id'] in groups[node['exclusive_group']]['members'],
                    f"{node['id']}: unknown or inconsistent exclusive group")
    visited, active = set(), []

    def visit(tid):
        require(tid not in active, 'Technology cycle: ' + ' -> '.join(active + [tid]))
        if tid in visited:
            return
        active.append(tid)
        for parent in nodes[tid]['requires_all'] + nodes[tid]['requires_any']:
            visit(parent)
        active.pop()
        visited.add(tid)

    for tid in nodes:
        visit(tid)
    for mid, node in milestones.items():
        for parent in node['requires_all'] + node['requires_any']:
            if parent in milestones:
                require(milestones[parent]['cap_threshold'] < node['cap_threshold'],
                        f'{mid}: cap_threshold must exceed that of prerequisite {parent}')
    for tier in tiers.values():
        amounts({tier['validation_good']: tier['validation_amount']}, 'Research validation')
        require(positive(tier['theory_points']) and positive(tier['validation_cycles']), 'Bad research cost')
    for profile in profiles.values():
        amounts(profile['build'], profile['id'] + ' build')
        amounts(profile['maintenance'], profile['id'] + ' maintenance')
        require(positive(profile['lines']) and positive(profile['build_cycles']), 'Bad facility parameters')
    for mode in modes.values():
        references(mode['requires_all'], mode['id'])
        amounts(mode['installation'], mode['id'] + ' installation')
        require(all(positive(mode[key]) for key in ('capacity', 'worker_factor', 'power_factor', 'switch_cycles')), 'Bad line mode')
    for recipe in recipes.values():
        references(recipe['requires_all'], recipe['id'])
        amounts(recipe['inputs'], recipe['id'] + ' inputs')
        amounts(recipe['installed'], recipe['id'] + ' installed')
        amounts(recipe['outputs'], recipe['id'] + ' outputs', inventory_only=False)
        require(recipe['outputs'], f"Empty outputs for {recipe['id']}")
        require(type(recipe['power']) in (int, float) and math.isfinite(recipe['power']) and recipe['power'] >= 0, 'Bad power')
        require(positive(recipe['workers']), 'Bad worker count')
        require(any(recipe['grade'] <= p['recipe_grade'] for p in profiles.values()), 'No supporting facility grade')
    source_ids = [s['good'] for s in industry['sources']]
    require(len(source_ids) == len(set(source_ids)), 'Duplicate source definition')
    require(set(source_ids) <= goods.keys(), 'Unknown source goods')
    require(all(goods[k]['kind'] == 'inventory' for k in source_ids), 'Source is not inventory')
    require(all(s['kind'] in ('finite_deposit', 'renewable_limited', 'salvage') for s in industry['sources']), 'Unknown source kind')
    producers = set(source_ids) | {g for r in recipes.values() for g in r['outputs']}
    require(goods.keys() <= producers, f'Goods without producer or source: {goods.keys() - producers}')

    # Potential availability only: ignores geography, quantities, costs, and access rights.
    # Assumes the starter world has base facilities and a working seed power supply.
    available = {s['good'] for s in industry['sources'] if s['kind'] != 'salvage'} | {'electricity'}
    known, built, usable = set(), {'S1'}, set()
    require('S1' in profiles, 'Missing starter facility profile S1')
    while True:
        before = (frozenset(available), frozenset(known), frozenset(built), frozenset(usable))
        for raw, scrap in (('steel', 'scrap_steel'), ('copper', 'scrap_copper')):
            if raw in available and scrap in source_ids:
                available.add(scrap)
        for fid, facility in profiles.items():
            if set(facility['build']) <= available:
                built.add(fid)
        grade = max(profiles[fid]['recipe_grade'] for fid in built)
        for rid, recipe in recipes.items():
            if (set(recipe['requires_all']) <= known and recipe['grade'] <= grade
                    and set(recipe['inputs']) <= available and set(recipe['installed']) <= available
                    and (recipe['power'] == 0 or 'electricity' in available)):
                available.update(recipe['outputs'])
                usable.add(rid)
        for tid, node in nodes.items():
            if not (set(node['requires_all']) <= known
                    and (not node['requires_any'] or set(node['requires_any']) & known)):
                continue
            if tid in milestones:
                # Reachable once some recipe can supply compute for training.
                if 'compute' in available:
                    known.add(tid)
            elif tiers[node['tier']]['validation_good'] in available and 'research_points' in available:
                known.add(tid)
        after = (frozenset(available), frozenset(known), frozenset(built), frozenset(usable))
        if before == after:
            break
    require(nodes.keys() <= known, f'Unreachable technologies: {nodes.keys() - known}')
    require(recipes.keys() <= usable, f'Unreachable recipes: {recipes.keys() - usable}')
    require(goods.keys() <= available, f'Unreachable goods: {goods.keys() - available}')
    return nodes, milestones, goods, recipes, modes, profiles


def table(headers, rows):
    def cell(value):
        return str(value).replace('|', '\\|').replace('\n', '<br>')
    return '\n'.join(['| ' + ' | '.join(map(cell, headers)) + ' |',
                      '| ' + ' | '.join('---' for _ in headers) + ' |'] +
                     ['| ' + ' | '.join(map(cell, row)) + ' |' for row in rows])


def generated(tech, industry):
    names = {g['id']: g['name'] for g in industry['goods']}

    def quantities(items):
        return '；'.join(f'{names[k]} × {v:g}' for k, v in items.items()) or '—'

    all_nodes = tech['technologies'] + tech.get('capability_milestones', [])

    def tier_label(node):
        return f"能力 Cap≥{node['cap_threshold']:g}" if node.get('unlock') == 'capability' else node['tier']

    tech_parts = ['## 研究成本表', table(
        ['阶', '理论点', '验证投入', '验证周期'],
        [[t['tier'], t['theory_points'], f"{names[t['validation_good']]} × {t['validation_amount']}", t['validation_cycles']]
         for t in tech['research_tiers']])]
    for branch in tech['branches']:
        tech_parts.append('## ' + branch['name'])
        tech_parts.append(table(['ID', '阶', '科技', '全部前置', '任一前置', '解锁与作用'], [
            [t['id'], tier_label(t), t['name'], ' + '.join(t['requires_all']) or 'BASE',
             ' / '.join(t['requires_any']) or '—', '；'.join(t['unlocks'])]
            for t in all_nodes if t['branch'] == branch['id']]))
    if tech.get('exclusive_groups'):
        tech_parts.append('## 互斥制度组')
        tech_parts.append(table(['组', '名称', '成员', '施行与切换规则'], [
            [g['id'], g['name'], ' / '.join(g['members']), g['rule']] for g in tech['exclusive_groups']]))
    if tech.get('capability_milestones'):
        tech_parts.append('“能力 Cap≥x”行是能力里程碑：不消耗理论点与验证包，模型能力 Cap 首次达到阈值且前置已满足时自动解锁，规则见下文“AI 科技线”。')
    tech_parts.append('完整依赖图源文件：[technology_tree.mmd](diagrams/technology_tree.mmd)。实线表示必要前置，虚线表示任一前置。')

    parts = ['## 设施档位', table(['ID', '档位', '配方等级上限', '产线数', '建设投入', '工期/周期', '每 20 周期维护'], [
        [p['id'], p['name'], p['recipe_grade'], p['lines'], quantities(p['build']), p['build_cycles'], quantities(p['maintenance'])]
        for p in industry['facility_profiles']]),
        '## 制造产线模式', table(['ID', '模式', '前置', '容量', '单位劳动系数', '单位电力系数', '转产/周期', '额外装机'], [
        [m['id'], m['name'], ' + '.join(m['requires_all']) or 'BASE', m['capacity'], m['worker_factor'], m['power_factor'], m['switch_cycles'], quantities(m['installation'])]
        for m in industry['line_modes']]),
        '适用厂类：' + '、'.join(industry['mode_eligible_facilities']) + '。',
        '## 原料与回收物来源', table(['物资', '来源类型', '约束'], [
        [names[s['good']], s['kind'], s['rule']] for s in industry['sources']]),
        '## 物品与流量字典', table(['ID', '名称', '类型'], [
        [g['id'], g['name'], '网络/项目流量' if g['kind'] == 'flow' else '地点库存'] for g in industry['goods']]),
        '## 全部配方', '电/人列为基准耗电与当期劳动占用；装机列只在安装时消耗。所有数量均为抽象游戏单位。',
        table(['ID / 名称', '厂类 / 等级', '科技前置', '每周期投入', '每周期产出', '电 / 人', '一次性装机'], [
        [r['id'] + ' ' + r['name'], r['facility'] + ' / ' + str(r['grade']),
         ' + '.join(r['requires_all']) or 'BASE', quantities(r['inputs']), quantities(r['outputs']),
         f"{r['power']} / {r['workers']}", quantities(r['installed'])] for r in industry['recipes']]),
        '## 配方特别条件']
    parts.extend('- ' + r['id'] + '：' + r['condition'] for r in industry['recipes'] if r['condition'])

    diagram = ['flowchart LR']
    for branch in tech['branches']:
        diagram.append(f'  subgraph branch_{branch["id"]}["{branch["name"]}"]')
        for node in all_nodes:
            if node['branch'] == branch['id']:
                diagram.append(f'    {node["id"]}["{node["id"]} {node["name"]}"]')
        diagram.append('  end')
    for node in all_nodes:
        diagram.extend(f'  {p} --> {node["id"]}' for p in node['requires_all'])
        diagram.extend(f'  {p} -.-> {node["id"]}' for p in node['requires_any'])
    return '\n\n'.join(tech_parts), '\n\n'.join(parts), '\n'.join(diagram) + '\n'


def replace_section(path, label, body, render):
    text = path.read_text(encoding='utf-8')
    start = f'<!-- BEGIN GENERATED {label} -->'
    end = f'<!-- END GENERATED {label} -->'
    require(text.count(start) == text.count(end) == 1, f'Missing or duplicate markers in {path.name}')
    left, rest = text.split(start)
    _, right = rest.split(end)
    desired = left + start + '\n\n' + body + '\n\n' + end + right
    if render:
        path.write_text(desired, encoding='utf-8')
    else:
        require(text == desired, f'Stale generated content: {path.name}; run render')


def check_links():
    for path in ROOT.rglob('*.md'):
        for target in re.findall(r'\]\(([^)]+)\)', path.read_text(encoding='utf-8')):
            if '://' in target or target.startswith('#'):
                continue
            local = target.split('#', 1)[0]
            resolved = (path.parent / local).resolve()
            require(resolved == ROOT or ROOT in resolved.parents, f'Nonportable link: {path.name}: {target}')
            require(resolved.exists(), f'Broken link: {path.name}: {target}')


def check_rule_versions():
    versions = {name: json.loads((ROOT / 'data' / f'{name}.json').read_text(encoding='utf-8'))['version']
                for name in ('coupling', 'scoring', 'runtime_rules', 'military')}
    require(len(set(versions.values())) == 1, f'Rule table version mismatch: {versions}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['check', 'render'])
    args = parser.parse_args()
    tech = json.loads((ROOT / 'data/technology.json').read_text(encoding='utf-8'))
    industry = json.loads((ROOT / 'data/industry.json').read_text(encoding='utf-8'))
    scoring_path = ROOT / 'data/scoring.json'
    require(scoring_path.exists(), 'Missing data/scoring.json')
    scoring = json.loads(scoring_path.read_text(encoding='utf-8'))
    nodes, milestones, goods, recipes, modes, profiles = validate(tech, industry)
    w_cat = scoring.get('shadow_weights', {}).get('w_cat', {})
    require(set(w_cat.keys()) == set(goods.keys()), f'scoring.json w_cat mismatch: {set(w_cat.keys()) ^ set(goods.keys())}')
    require(all(positive(v) for v in w_cat.values()), 'scoring.json w_cat must be positive finite numbers')
    active_rules = scoring.get('pillars', {}).get('tech', {}).get('active_scale_rules', {})
    require(set(active_rules.keys()) == set(nodes.keys()), f'scoring.json active_scale_rules mismatch: {set(active_rules.keys()) ^ set(nodes.keys())}')
    require(all(positive(r.get('theta')) for r in active_rules.values()), 'scoring.json theta must be positive')
    valid_kinds = {
        'active_recipe_batches', 'active_mode_lines', 'active_lab_lines',
        'active_grid_or_storage', 'active_branch_installation', 'active_logistics_or_s2plus',
        'active_military_or_orbital', 'active_finance_institution', 'active_compute_deployed'
    }
    lab_recipes = {rid for rid, r in recipes.items() if r['facility'] == '实验室'}
    all_valid_refs = set(nodes.keys()) | set(recipes.keys()) | set(profiles.keys()) | set(modes.keys())
    for tid, rule in active_rules.items():
        kind = rule.get('kind')
        refs = set(rule.get('ref_ids', []))
        require(kind in valid_kinds, f'scoring.json {tid}: unknown kind {kind}')
        require(refs, f'scoring.json {tid}: empty ref_ids')
        if kind == 'active_lab_lines':
            require(refs <= lab_recipes, f'scoring.json {tid}: non-lab recipes in active_lab_lines: {refs - lab_recipes}')
        elif kind == 'active_mode_lines':
            require(refs <= set(modes.keys()), f'scoring.json {tid}: unknown mode IDs: {refs - set(modes.keys())}')
        elif kind == 'active_recipe_batches':
            require(refs <= set(recipes.keys()), f'scoring.json {tid}: unknown recipe IDs: {refs - set(recipes.keys())}')
        else:
            require(refs <= all_valid_refs, f'scoring.json {tid}: unknown ref_ids: {refs - all_valid_refs}')
    for vec in scoring.get('relative_scoring', {}).get('test_vectors', []):
        v_p, v_s = float(vec['V_p']), float(vec['V_s'])
        vt_p = (v_p + 10.0) if v_p >= 0.0 else 100.0 / (10.0 - v_p)
        vt_s = (v_s + 10.0) if v_s >= 0.0 else 100.0 / (10.0 - v_s)
        r_k = vt_p / vt_s
        s_k = 50.0 + 50.0 * math.tanh(0.6 * math.log(r_k))
        require(abs(r_k - float(vec['R_k'])) < 1e-5, f'scoring.json test_vector R_k mismatch: {vec}')
        require(abs(s_k - float(vec['s_k'])) < 1e-5, f'scoring.json test_vector s_k mismatch: {vec}')
    tech_md, industry_md, diagram = generated(tech, industry)
    render = args.command == 'render'
    replace_section(ROOT / '04_technology_tree.md', 'TECHNOLOGY', tech_md, render)
    replace_section(ROOT / '05_industry_system.md', 'INDUSTRY', industry_md, render)
    diagram_path = ROOT / 'diagrams/technology_tree.mmd'
    if render:
        diagram_path.parent.mkdir(parents=True, exist_ok=True)
        diagram_path.write_text(diagram, encoding='utf-8')
    else:
        require(diagram_path.read_text(encoding='utf-8') == diagram, 'Stale technology diagram')
    check_links()
    check_rule_versions()
    print(f'PASS: {len(nodes)} technologies ({len(nodes) - len(milestones)} research + {len(milestones)} capability milestones), '
          f'{len(goods)} goods/flows, {len(recipes)} recipes.')
    print('References, technology DAG, potential catalog reachability, generated files, and local links checked.')
    print('This does not validate gameplay, quantities, initial worlds, balance, or runtime performance.')


if __name__ == '__main__':
    main()
