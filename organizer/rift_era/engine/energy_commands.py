"""Energy policies with actual escrow-backed price-cap subsidies."""

from .command_schema import fields, version
from .errors import RuleViolation
from .power import PLANT_CALENDAR
from .protocol import PreparedCommand


class EnergyCommands:
    OPERATIONS = frozenset({('governance', 'power_priority'), ('governance', 'power_price_cap'),
                            ('logistics', 'plant_maintenance')})

    def __init__(self, state):
        self.state = state

    def prepare(self, owner, order, ctx):
        state = self.state
        op = order['op']
        if op == 'power_priority':
            fields(order, {'priority_order': list})
            priorities = order['priority_order']
            if len(priorities) != 4 or any(type(p) is not str for p in priorities) or set(priorities) != {'B1', 'B2', 'B3', 'B4'}:
                raise RuleViolation('ERR_SCHEMA')
            return PreparedCommand(lambda: state.power_priorities.__setitem__(owner, tuple(priorities)))
        if op == 'power_price_cap':
            fields(order, {'cap_price_mU': int})
            cap = order['cap_price_mU']
            if not 5000 <= cap <= 50000:
                raise RuleViolation('ERR_SCHEMA')
            # Already-created P1 life-support reservations retain their terms;
            # new reservations use this policy immediately, all do next tick.
            return PreparedCommand(lambda: state.power_price_caps.__setitem__(owner, cap))
        fields(order, {'facility_id': str, 'shift_cycles': int}, {'expected_version': int})
        asset = state.initial.assets.get(order['facility_id'])
        if asset is None or asset.owner != owner:
            raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        version(order, asset)
        shift = order['shift_cycles']
        if not -20 <= shift <= 20:
            raise RuleViolation('ERR_SCHEMA')
        ids = (asset.id,) if asset.id in state.initial.plants else state.initial.facilities.get(asset.id).line_ids if asset.id in state.initial.facilities else ()
        plants = [state.initial.plants[lid] for lid in ids if lid in state.initial.plants]
        if not plants or any(not PLANT_CALENDAR[p.spec.recipe][1] for p in plants):
            raise RuleViolation('ERR_SITE_INCOMPATIBLE')

        def commit():
            for plant in plants:
                plant.maintenance_shift = shift
            asset.version += 1

        return PreparedCommand(commit)
