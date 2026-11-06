"""03/04 N10 military/hypersonic_strike (hypersonic_v1).

`military/hypersonic_strike {origin_hub, target_id, missile_qty 1..20}` needs
N10, a controlled origin Hub holding `missile_qty` hypersonic_missile, and
WAR with the target's owner (no undeclared kinetic attack, 03 section 0). The
target is a foreign facility id or a corridor id; its Hub/endpoint region must
be within 8 region hops of the origin and currently observed by the shooter
at q >= .5 (sensor quality of a static Land signal at the Hub cell). The
missiles are locked at P1, launched and consumed (ACTION_LOSS) at this tick's
P4 and arrive at the next tick's P4.

Interception: only G11. Each of the defender's (or its WAR-allies') living
`energy_weapon` units in the target region intercepts at most one missile per
cycle with probability .6 (the 03 electricity reservation is not modelled;
author to confirm). Each missile that gets through: an S1-S3 facility loses
one surviving line (ordered by a seeded hash, full health removed through the
asset lifecycle, so its book loss is the owner's Loss_destroy); a higher shell
or a corridor loses 25% of maximum durability.
"""
from dataclasses import dataclass

from .command_schema import fields
from .determinism import digest, uniform
from .diplomacy import Relation
from .errors import InvariantViolation, RuleViolation
from .ledger import Account, Transfer
from .military_sensors import Signal
from .numeric import Q
from .protocol import PreparedCommand
from .provenance import Use
from .scoring import action_loss

GOOD = 'hypersonic_missile'
RANGE = 8


@dataclass
class HypersonicStrike:
    id: str
    command_id: str
    owner: str
    origin_hub: str
    target: str
    target_owner: str
    qty: int
    launch_tick: int
    arrival_tick: int
    launched: bool = False

    def lock(self):
        return Account(self.owner, self.origin_hub, 'locked_hypersonic:' + self.id, GOOD)


def target_site(state, target):
    """(hub, owner, kind) of a facility or corridor target, or None."""
    facility = state.initial.facilities.get(target)
    if facility is not None:
        return facility.hub, facility.owner, 'facility'
    edge = state.cargo.corridors.get(target)
    if edge is not None:
        return edge.a, state.cargo.operator(target), 'corridor'
    return None


def static_quality(state, owner, hub):
    site = state.initial.map.hubs[hub]
    from .world import SIZE
    signal = Signal('static:' + hub, site.owner or 'world', 'Land', site.region, site.y * SIZE + site.x, 0)
    quality, _ = state.sensors.quality(owner, signal)
    return quality


class Hypersonics:
    def __init__(self, state):
        self.s = state
        self.strikes = {}
        self.intercepted = {}     # unit id -> cycle of its last interception
        self.action_cycles = {}
        self.recent = {}

    def advance(self, ctx):
        state = self.s
        self.action_cycles = {c: v for c, v in self.action_cycles.items() if c >= ctx.cycle - 19}
        for sid in sorted(self.strikes):
            strike = self.strikes[sid]
            if strike.launched and strike.arrival_tick <= ctx.tick:
                self._arrive(strike, ctx)
                del self.strikes[sid]
        for strike in self.strikes.values():
            if strike.launched or strike.launch_tick > ctx.tick:
                continue
            losses = self.action_cycles.setdefault(ctx.cycle, {})
            quantity = strike.qty * 1000
            state.initial.inventory.transform({strike.lock(): quantity}, {}, reason='hypersonic_launch', use=Use.ACTION_LOSS)
            losses[strike.owner] = losses.get(strike.owner, Q(0)) + action_loss({GOOD: quantity}, state.catalog.weights)
            strike.launched = True

    def _defenders(self, strike):
        state = self.s
        site = target_site(state, strike.target)
        if site is None:
            return []
        region = state.initial.map.hubs[site[0]].region
        diplomacy = state.initial.diplomacy
        rows = []
        for formation in state.formations.rows.values():
            if formation.region != region:
                continue
            friendly = formation.owner == strike.target_owner or (
                formation.owner in diplomacy.actors and diplomacy.relation(formation.owner, strike.owner) == Relation.WAR)
            if not friendly:
                continue
            for uid in formation.unit_ids:
                unit = state.initial.units[uid]
                if unit.unit_type == 'energy_weapon' and unit.hp_mU > 0:
                    rows.append(uid)
        return sorted(rows)

    def _arrive(self, strike, ctx):
        state = self.s
        report = {'command_id': strike.command_id, 'tick': ctx.tick, 'missiles': strike.qty, 'intercepted': 0, 'hits': 0}
        self.recent.setdefault(strike.owner, []).append(report)
        self.recent[strike.owner] = self.recent[strike.owner][-64:]
        site = target_site(state, strike.target)
        defenders = [u for u in self._defenders(strike) if self.intercepted.get(u) != ctx.cycle]
        for index in range(strike.qty):
            if defenders:
                unit = defenders.pop(0)
                self.intercepted[unit] = ctx.cycle
                if uniform(ctx.seed, 'g11_intercept', ctx.tick, strike.id, index) < .6:
                    report['intercepted'] += 1
                    continue
            if site is None:
                continue
            if self._hit(strike, site, index):
                report['hits'] += 1

    def _hit(self, strike, site, index):
        state = self.s
        hub, owner, kind = site
        lifecycle = state.asset_lifecycle
        event = f'{strike.id}:{index}'
        if kind == 'facility':
            facility = state.initial.facilities[strike.target]
            if facility.profile in ('S1', 'S2', 'S3'):
                lines = sorted((lid for lid in facility.line_ids
                                if lid in state.initial.assets and state.initial.assets[lid].health_mU > 0),
                               key=lambda lid: digest(state.initial.map.seed, 'hypersonic_line', strike.id, index, lid))
                if not lines:
                    return False
                lifecycle.damage(lines[0], state.initial.assets[lines[0]].health_mU, event_id=event)
                return True
            aid = strike.target
        else:
            aid = state.cargo.road_assets.get(strike.target)
        if aid is None or aid not in state.initial.assets or state.initial.assets[aid].health_mU <= 0:
            return False
        lifecycle.damage(aid, 250, event_id=event)
        return True

    def visible(self, owner):
        return {'in_flight': [{'command_id': s.command_id, 'target_id': s.target, 'arrival_tick': s.arrival_tick}
                              for s in self.strikes.values() if s.owner == owner],
                'recent': list(self.recent.get(owner, ()))}

    def snapshot(self):
        return {'strikes': self.strikes, 'intercepted': self.intercepted,
                'action_cycles': {str(c): v for c, v in self.action_cycles.items()}, 'recent': self.recent}

    def empty(self):
        return not (self.strikes or self.intercepted or self.action_cycles or self.recent)

    def audit(self):
        for strike in self.strikes.values():
            if strike.arrival_tick <= strike.launch_tick:
                raise InvariantViolation('hypersonic strike has no flight time')
            if self.s.initial.inventory.balance(strike.lock()) != (0 if strike.launched else strike.qty * 1000):
                raise InvariantViolation('hypersonic custody differs from flight state')


class HypersonicCommands:
    OPERATIONS = frozenset({('military', 'hypersonic_strike')})

    def __init__(self, state):
        self.state = state

    def prepare(self, owner, order, ctx):
        state = self.state
        fields(order, {'origin_hub': str, 'target_id': str, 'missile_qty': int})
        qty, origin = order['missile_qty'], order['origin_hub']
        if not 1 <= qty <= 20 or origin not in state.initial.map.hubs:
            raise RuleViolation('ERR_SCHEMA')
        if 'N10' not in state.research[owner].known:
            raise RuleViolation('ERR_TECH_LOCKED')
        if not state.authority.controls_hub(owner, origin):
            raise RuleViolation('ERR_NO_PERMISSION')
        site = target_site(state, order['target_id'])
        if site is None or site[1] == owner:
            raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        hub, target_owner, _ = site
        region = state.initial.map.hubs[hub].region
        if state.sensors.distances(state.initial.map.hubs[origin].region).get(region, 99) > RANGE:
            raise RuleViolation('ERR_SITE_INCOMPATIBLE', 'target beyond 8 region hops')
        if static_quality(state, owner, hub) < Q(1, 2):
            raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        if target_owner not in state.initial.diplomacy.actors or not state.relations.will_be_at_war(owner, target_owner):
            raise RuleViolation('ERR_NOT_AT_WAR')
        command_id = str(int(order['id']))
        strike = HypersonicStrike('hypersonic:' + digest(owner, command_id).hex()[:24], command_id, owner, origin,
                                  order['target_id'], target_owner, qty, ctx.tick, ctx.tick + 1)
        stock = state.initial.inventory.prepare_transfers(
            (Transfer(Account(owner, origin, 'available', GOOD), strike.lock(), qty * 1000),))

        def commit():
            stock.commit()
            state.hypersonics.strikes[strike.id] = strike

        return PreparedCommand(commit)
