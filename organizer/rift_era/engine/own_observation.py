"""Explicit owner-visible entity schemas, with no global counters or seeds."""

from .numeric import Q
from .determinism import primitive


def owned_entities(state, owner):
    initial = state.initial
    def asset_fields(aid):
        asset = initial.assets[aid]
        return {'id': aid, 'version': asset.version, 'hub_id': asset.hub, 'health_mU': asset.health_mU,
                'availability_mU': round(1000 * asset.availability),
                'maintenance_debt_mU': round(1000 * asset.maintenance_debt),
                'under_construction': asset.under_construction, 'work_locked': asset.work_locked,
                'book_nmU': primitive(asset.book_nmU)}
    facilities, lines, mines = [], [], []
    for fid, facility in sorted(initial.facilities.items()):
        if facility.owner == owner:
            facilities.append(asset_fields(fid) | {'facility_type': facility.kind, 'profile_id': facility.profile,
                                                   'line_ids': facility.line_ids, 'commissioned': facility.commissioned})
    for lid, line in sorted(initial.lines.items()):
        if line.owner != owner:
            continue
        plant = initial.plants.get(lid)
        policy = initial.line_policies[lid]
        row = asset_fields(lid) | {'recipe': None if line.recipe.id == '__idle__' else line.recipe.id,
              'mode': policy.manufacturing_mode, 'load_mU': policy.load_mU,
              'slot_use': state.slot_use[lid], 'paused': lid in state.paused_lines,
              'options': dict(state.line_options.get(lid, {})),
              'input_credit_nmU': dict(line.credit_nmU), 'output_accum_nmU': dict(line.output_nmU),
              'power_received_mU': state.frame.power_received.get(lid, 0)}
        if plant:
            row['plant'] = {'mode': plant.mode.value, 'online': plant.online, 'timer_ticks': plant.timer,
                            'generation_mU': state.frame.generation.get(lid, 0),
                            'next_maintenance_cycle': plant.next_maintenance_cycle,
                            'maintenance_shift_cycles': plant.maintenance_shift}
        lines.append(row)
    for mid, mine in sorted(initial.mines.items()):
        if mine.owner == owner:
            deposit = state.deposits[state.mine_deposit[mid]]
            mines.append(asset_fields(mid) | {'good': mine.good, 'level': mine.level, 'grade_mU': round(1000 * mine.grade),
                'deposit_id': state.mine_deposit[mid], 'remaining_mU': deposit.remaining_mU, 'renewable': deposit.renewable})
    hubs = []
    for hid, population in sorted(initial.populations.items()):
        if initial.map.hubs[hid].owner != owner:
            continue
        hubs.append({'id': hid, 'region_id': initial.map.hubs[hid].region,
            'population_mU': population.total_mU, 'skills_mU': dict(population.skills_mU),
            'training_population_mU': sum(population.training_mU.values()),
            'housing_mU': initial.housing_mU[hid], 'warehouse_mU': initial.warehouse_mU[hid],
            'support_mU': population.support_mU, 'warning': population.warning, 'red_streak': population.red_streak,
            'pollution_mU': state.pollution[hid],
            'satisfaction_mU': {g: round(1000 * q) for g, q in state.last_satisfaction.get(hid, {}).items()},
            'grid_price_ru_mU': round(1000 * state.grid_prices_ru[hid]),
            'household_cash_mU': initial.cash.balance(state.native(owner, f'household:{hid}')).native_mU,
            'public_cash_mU': initial.cash.balance(state.native(owner, f'public:{hid}')).native_mU})
    policy = state.governance.policies[owner]
    governance = {'nominal_tax_bp': round(10000 * policy.nominal_tax), 'effective_tax_bp': round(10000 * state.tax[owner]),
        'welfare_budget_mU': policy.welfare_budget_mU, 'welfare_need_mU': round(policy.welfare_need_mU),
        'welfare_paid_mU': policy.welfare_paid_mU, 'maintenance_budget_mU': policy.maintenance_budget_mU,
        'mobilized': policy.mobilized, 'mobilization_mU': round(1000 * policy.mobilization),
        'fatigue_mU': round(1000 * policy.fatigue), 'regimes': dict(policy.regimes),
        'transitions': {g: {'choice': target, 'until_tick': tick} for g, (target, tick) in policy.transitions.items()},
        'autonomy_mU': {r: round(1000 * q) for r, q in state.governance.autonomy.items() if initial.map.regions[r].sovereign == owner},
        'training': [{'id': j.command_id, 'hub_id': j.hub, 'track': j.track,
                      'residents_mU': initial.populations[j.hub].training_mU[j.id], 'elapsed_ticks': j.elapsed,
                      'duration_ticks': j.duration_ticks, 'waiting_resource': j.waiting}
                     for j in state.governance.training.values() if j.owner == owner]}
    storage = [asset_fields(s.id) | {'host_id': s.host, 'storage_kind': s.kind, 'mode': s.mode,
                'direction': s.direction, 'quote_ru_mU': round(1000 * s.quote_ru), 'soc_mU': s.soc_mU,
                'battery_health_mU': s.battery_health_mU, 'capacity_mU': state.storage.capacity(s),
                'power_mU': s.requested_power_mU} for s in state.storage.stores.values() if s.owner == owner]
    return {'facilities': facilities, 'lines': lines, 'mines': mines, 'hubs': hubs, 'governance': governance, 'storage': storage,
            'cash_accounts': [[a.name, a.currency, b.native_mU] for a, b in sorted(initial.cash.balances().items()) if a.owner == owner]}
