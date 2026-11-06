"""04/02 V08 regional climate intervention and 06 grant_climate_consent (climate_v1).

`governance/climate_intervention {target_region, mode}` (needs V08) runs for 60
cycles from the next cycle; the same nation may start another 120 cycles after
the previous run ended. Every cycle it needs 2 remediation_agent, taken at the
cycle start from the nation's capital `available` stock, and 10 electricity of
B3 load during the previous cycle (a load `climate:<nation>` at the capital).
A cycle whose inputs are short has no effect and is not made up later.

Effect while active: the target region's rain and wind/solar coefficients
change by +15% (enhance) or -15% (suppress); the 3 regions downwind change by
the opposite amount. The map carries no `wind_dir`, so the prevailing wind is
read as west-to-east (same row, columns +1..+3); author to confirm. Several
interventions add, clipped per region to [-25%, +25%]. Rain multiplies the
natural water inflow and the crop rain factor; wind/solar multiply the
realized weather, while the weather AR(1) state keeps its raw value.

Consent: `diplomacy/grant_climate_consent {target_nation, region_ids,
duration_cycles}` lets `target_nation` affect regions the granter controls.
Cancelling the grant (cancel_by_id) ends it from the next cycle. An
intervention whose affected regions include any region controlled by another
actor without a valid grant is hostile: on its first effective cycle the
initiator's credit drops by .10 once, and its inputs are recorded as
ACTION_LOSS (07) rather than eligible remediation use.
"""
from dataclasses import dataclass

from .command_schema import fields
from .errors import RuleViolation
from .ledger import Account
from .numeric import Q, clip
from .provenance import Use
from .protocol import PreparedCommand
from .scoring import action_loss

DURATION, COOLDOWN, SHIFT, LIMIT = 60, 120, Q(15, 100), Q(1, 4)
AGENT_MU, POWER_MU_CYCLE = 2000, 10000


@dataclass
class Intervention:
    owner: str
    command_id: str
    region: str
    mode: str
    start_cycle: int
    end_cycle: int
    affected: tuple
    active: bool = False
    energized_mU: int = 0
    effective_cycles: int = 0
    hostile_charged: bool = False


@dataclass
class Consent:
    grantor: str
    grantee: str
    command_id: str
    regions: tuple
    start_cycle: int
    end_cycle: int


class Climate:
    def __init__(self, state):
        self.s = state
        self.interventions = {}  # owner -> Intervention (one at a time)
        self.last_end = {}       # owner -> cycle the last run ended
        self.consents = []
        self.raw_wind = {}       # region -> raw AR wind while shifted
        self.action_cycles = {}  # cycle -> owner -> action loss
        self.last_cycle = -1
        world = state.initial.map
        index = {(r.row, r.column): rid for rid, r in world.regions.items()}
        self.downwind = {rid: tuple(index[(r.row, r.column + k)] for k in (1, 2, 3) if (r.row, r.column + k) in index)
                         for rid, r in world.regions.items()}

    # -- effects ------------------------------------------------------------------
    def offset(self, region):
        total = Q(0)
        for run in self.interventions.values():
            if not run.active:
                continue
            sign = 1 if run.mode == 'enhance' else -1
            if region == run.region:
                total += sign * SHIFT
            elif region in run.affected:
                total -= sign * SHIFT
        return clip(total, -LIMIT, LIMIT)

    def multiplier(self, region):
        return 1 + self.offset(region)

    def controller(self, region):
        row = self.s.authority.regions[region]
        return row.controller or row.sovereign

    def consented(self, owner, region, cycle):
        holder = self.controller(region)
        if holder is None or holder == owner:
            return True
        return any(c.grantor == holder and c.grantee == owner and region in c.regions
                   and c.start_cycle <= cycle < c.end_cycle for c in self.consents)

    # -- cycle start --------------------------------------------------------------
    def begin_cycle(self, ctx):
        if ctx.tick % ctx.clock.cycle_ticks or ctx.cycle <= self.last_cycle:
            return
        self.last_cycle = ctx.cycle
        state = self.s
        self.consents = [c for c in self.consents if ctx.cycle < c.end_cycle]
        self.action_cycles = {c: v for c, v in self.action_cycles.items() if c >= ctx.cycle - 19}
        for owner in sorted(self.interventions):
            run = self.interventions[owner]
            if ctx.cycle >= run.end_cycle:
                self.last_end[owner] = run.end_cycle
                del self.interventions[owner]
                continue
            energized = run.energized_mU >= POWER_MU_CYCLE
            run.energized_mU = 0
            run.active = False
            if ctx.cycle < run.start_cycle or not energized:
                continue
            source = Account(owner, f'{owner}-h0', 'available', 'remediation_agent')
            if state.initial.inventory.balance(source) < AGENT_MU:
                continue
            hostile = not all(self.consented(owner, r, ctx.cycle) for r in (run.region, *run.affected))
            state.initial.inventory.transform({source: AGENT_MU}, {}, reason='climate_intervention',
                                              use=Use.ACTION_LOSS if hostile else Use.REMEDIATION)
            run.active = True
            run.effective_cycles += 1
            if hostile:
                losses = self.action_cycles.setdefault(ctx.cycle, {})
                losses[owner] = losses.get(owner, Q(0)) + action_loss({'remediation_agent': AGENT_MU, 'electricity': POWER_MU_CYCLE},
                                                                       state.catalog.weights)
                if not run.hostile_charged:
                    state.credit[owner] = max(Q(1, 10), state.credit[owner] - Q(1, 10))
                    run.hostile_charged = True

    def power_demands(self):
        return {f'climate:{owner}': (f'{owner}-h0', 3, POWER_MU_CYCLE // 10)
                for owner, run in sorted(self.interventions.items())}

    def meter_power(self, ctx):
        received = self.s.frame.power_received
        for owner, run in self.interventions.items():
            run.energized_mU += received.get(f'climate:{owner}', 0)

    # -- views --------------------------------------------------------------------
    def visible(self, owner):
        return {'interventions': [r for r in self.interventions.values() if r.owner == owner],
                'next_allowed_cycle': self.last_end.get(owner, -COOLDOWN) + COOLDOWN,
                'consents': [c for c in self.consents if owner in (c.grantor, c.grantee)]}

    def snapshot(self):
        return {'interventions': self.interventions, 'last_end': self.last_end, 'consents': self.consents,
                'raw_wind': self.raw_wind, 'action_cycles': {str(c): v for c, v in self.action_cycles.items()},
                'last_cycle': self.last_cycle}

    def empty(self):
        return not (self.interventions or self.last_end or self.consents or self.raw_wind)


class ClimateCommands:
    OPERATIONS = frozenset({('governance', 'climate_intervention'), ('diplomacy', 'grant_climate_consent')})

    def __init__(self, state):
        self.state = state

    def prepare(self, owner, order, ctx):
        state = self.state
        climate = state.climate
        command = str(int(order['id']))
        if order['op'] == 'grant_climate_consent':
            fields(order, {'target_nation': str, 'region_ids': list, 'duration_cycles': int})
            target, regions = order['target_nation'], order['region_ids']
            if (target == owner or target not in state.initial.known or not 1 <= order['duration_cycles'] <= 720
                    or not regions or any(type(r) is not str or r not in state.initial.map.regions for r in regions)
                    or len(set(regions)) != len(regions)):
                raise RuleViolation('ERR_SCHEMA')
            if any(climate.controller(r) != owner for r in regions):
                raise RuleViolation('ERR_NO_PERMISSION', 'only regions the granter controls')
            consent = Consent(owner, target, command, tuple(sorted(regions)), ctx.cycle, ctx.cycle + order['duration_cycles'])
            return PreparedCommand(lambda: climate.consents.append(consent), persistent=True)
        fields(order, {'target_region': str, 'mode': str})
        region, mode = order['target_region'], order['mode']
        if mode not in ('enhance', 'suppress') or region not in state.initial.map.regions:
            raise RuleViolation('ERR_SCHEMA')
        if 'V08' not in state.research[owner].known:
            raise RuleViolation('ERR_TECH_LOCKED')
        if owner in climate.interventions or ctx.cycle < climate.last_end.get(owner, -COOLDOWN) + COOLDOWN:
            raise RuleViolation('ERR_CAPACITY_EXCEEDED', 'climate intervention running or cooling down')
        run = Intervention(owner, command, region, mode, ctx.cycle + 1, ctx.cycle + 1 + DURATION, climate.downwind[region])
        return PreparedCommand(lambda: climate.interventions.__setitem__(owner, run))


def cancel_consent(state, owner, target):
    climate = state.climate
    consent = next((c for c in climate.consents if c.grantor == owner and c.command_id == target), None)
    if consent is None:
        return None

    def revoke():
        cycle = state.climate.last_cycle + 1
        climate.consents = [c if c is not consent else Consent(c.grantor, c.grantee, c.command_id, c.regions,
                            c.start_cycle, min(c.end_cycle, cycle)) for c in climate.consents]

    return PreparedCommand(revoke)
