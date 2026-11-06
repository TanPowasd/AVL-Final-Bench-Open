"""Explicit recipe modifiers; inputs stay real inventory and paid power.

Heat is a one-tick delayed flow budget from actual nuclear/hydrogen generation,
not nameplate capacity. This breaks the demand/generation feedback cycle.
"""

from collections import defaultdict
from dataclasses import replace

from .determinism import digest
from .ledger import Account
from .numeric import Q, floor, units
from .production import Recipe


QUALITY_RECIPES = frozenset({
    'P013', 'P015', 'P016', 'P019', 'P020', 'P040', 'P045', 'P047', 'P048', 'P049',
    'P050', 'P053', 'P058', 'P060', 'P061', 'P065', 'P078', 'P088', 'P093', 'P100',
    'P101', 'P104', 'P108', 'P115',
})
HEAT_COAL = frozenset({'P001', 'P002', 'P004', 'P005', 'P066', 'P067'})
HEAT_POWER = frozenset({'P008', 'P014'})


def demanded(state, line) -> bool:
    if line.id in state.paused_lines or line.recipe.id == '__idle__':
        return False
    target = state.line_options.get(line.id, {}).get('target_stock_mU')
    goods = [g for g in line.recipe.outputs if state.catalog.goods[g]['kind'] == 'inventory']
    return target is None or not goods or any(state.initial.inventory.balance(
        Account(line.owner, line.hub, 'available', good)) < target for good in goods)


def refresh(state, ctx) -> None:
    heat = defaultdict(int)
    for lid, quantity in state.last_generation_mU.items():
        plant = state.initial.plants.get(lid)
        if (plant is not None and plant.online and state.last_generation_kind.get(lid) in ('P073', 'P083', 'P074')
                and state.initial.assets[lid].availability > 0):
            heat[state.initial.lines[lid].hub] += quantity
    requests = defaultdict(list)
    for line in state.initial.lines.values():
        options = state.line_options.get(line.id, {})
        if (options.get('high_temp') and 'E08' in state.research[line.owner].known and demanded(state, line)
                and state.initial.assets[line.id].availability > 0):
            requests[line.hub].append(line.id)
    state.heat_supported = set()
    for hub, candidates in requests.items():
        slots = heat[hub] // 500  # 5 units/cycle = 500 mU/tick per supported line.
        state.heat_supported.update(sorted(candidates, key=lambda lid: digest(ctx.seed, 'heat_slots', ctx.tick, hub, lid))[:slots])
    for lid, line in state.initial.lines.items():
        if line.recipe.id == '__idle__':
            continue
        recipe = Recipe.from_catalog(state.catalog, line.recipe.id)
        options = state.line_options.get(lid, {})
        inputs, outputs = dict(recipe.inputs), dict(recipe.outputs)
        if lid in state.heat_supported and recipe.id in HEAT_COAL:
            inputs['coal'] = round(inputs['coal'] * Q(1, 2))
        molecular = recipe.id in ('P013', 'P016', 'P087') and molecular_line(state, line)
        if molecular:
            # 04 I12: S5 line with ai_core, inputs -30% and no defects.
            inputs = {g: round(q * Q(7, 10)) for g, q in inputs.items()}
        elif recipe.id in QUALITY_RECIPES and 'sensor' not in options.get('addons', ()):
            outputs = {g: round(q * Q(94, 100)) for g, q in outputs.items()}
        if recipe.id in ('P029', 'P030') and precision_farm(state, line):
            # 04 A07: water and fertilizer -25% on a sensor-equipped farm with C08 telemetry.
            for good in ('water', 'fertilizer'):
                if good in inputs:
                    inputs[good] = round(inputs[good] * Q(3, 4))
        if state.governance.active(line.owner, 'F07') and 'consumer_goods' in outputs:
            outputs['consumer_goods'] = round(outputs['consumer_goods'] * Q(85, 100))
        line.recipe = replace(recipe, inputs=inputs, outputs=outputs)


def molecular_line(state, line):
    """04 I12: only in an S5 facility, with I12 known and the line's resident ai_core."""
    if 'I12' not in state.research[line.owner].known or 'ai_core' not in state.line_options.get(line.id, {}).get('addons', ()):
        return False
    facility = state.initial.facilities.get(line.id.rsplit('/line', 1)[0])
    if facility is None:
        facility = next((f for f in state.initial.facilities.values() if line.id in f.line_ids), None)
    return facility is not None and facility.profile == 'S5'


def scrap_molecular_on_outage(state, production, line):
    """04 I12: a molecular line short of its power this tick scraps its whole
    work in progress (input credit and unreleased output), as a recorded loss."""
    if not (line.credit_nmU or line.output_nmU) or line.recipe.id not in ('P013', 'P016', 'P087'):
        return False
    if not molecular_line(state, line):
        return False
    production.discard_and_switch(line, line.recipe)
    return True


def precision_farm(state, line):
    """04 A07 condition: A07 known, the line carries a sensor addon, and the owner
    has C08 telemetry (an online fusion_sensor system of its own; implementation reading)."""
    if 'A07' not in state.research[line.owner].known or 'sensor' not in state.line_options.get(line.id, {}).get('addons', ()):
        return False
    return any(s.owner == line.owner and s.kind == 'fusion_sensor' and s.online for s in state.systems.instances.values())


def gene_weather(state, line, weather):
    options = state.line_options.get(line.id, {})
    if line.recipe.id in ('P029', 'P030') and weather < 1 and precision_farm(state, line):
        weather = min(Q(1), weather + Q(15, 100))  # 04 A07: bad-weather floor +.15
    if not options.get('gene_edited'):
        return weather, Q(1)
    basin = state.initial.map.hubs[line.hub].region
    farms = [l for l in state.initial.lines.values() if l.recipe.id in ('P028', 'P029')
             and state.initial.map.hubs[l.hub].region == basin]
    count = sum(bool(state.line_options.get(l.id, {}).get('gene_edited')) for l in farms)
    if farms and Q(count, len(farms)) > Q(3, 5) and weather < 1:
        weather = max(Q(0), 1 - Q(13, 10) * (1 - weather))
    return weather, Q(7, 5)


def ccs_installed(state, lid):
    return 'carbon_capture_unit' in state.line_options.get(lid, {}).get('addons', ())


def ccs_running(state, lid):
    if not ccs_installed(state, lid):
        return False
    return lid in state.initial.plants or state.frame.power_received.get(f'ccs:{lid}', 0) >= 150
