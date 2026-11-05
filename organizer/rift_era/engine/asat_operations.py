"""Paid N12 launches, one-tick flight and simultaneous orbital debris effects."""
from collections import Counter
from dataclasses import dataclass

from .command_schema import fields
from .determinism import digest, uniform
from .diplomacy import Relation
from .errors import InvariantViolation, RuleViolation
from .ledger import Account, Transfer
from .numeric import Q, floor
from .protocol import PreparedCommand
from .provenance import Use
from .scoring import action_loss


@dataclass
class ASATStrike:
    id: str
    command_id: str
    owner: str
    origin_hub: str
    target: str
    target_alias: str
    observed_owner: str
    launch_tick: int
    arrival_tick: int
    launched: bool = False

    def lock(self, good):
        return Account(self.owner, self.origin_hub, 'locked_asat:' + self.id, good)


class ASATOperations:
    MATERIALS = {'hypersonic_missile': 1000, 'guidance_module': 1000}

    def __init__(self, state):
        self.s = state
        self.strikes = {}
        self.debris_until = dict.fromkeys(range(3), 0)
        self.action_cycles = {}
        self.recent = {owner: [] for owner in state.initial.known}
        self.last_tick = -1

    def new_lifetime(self, plane, commissioned_tick, lifetime_ticks):
        return lifetime_ticks * 85 // 100 if commissioned_tick < self.debris_until[plane] else lifetime_ticks

    def _retire(self, satellite, reason):
        state = self.s
        state.initial.inventory.transform({state.orbit.satellite_account(satellite): 1000}, {},
                                          reason=reason, use=Use.DESTRUCTION)
        state.cycles[satellite.owner].destroyed_value += state.catalog.weights['satellite']
        del state.orbit.satellites[satellite.id]

    def advance(self, ctx):
        if ctx.tick <= self.last_tick:
            raise InvariantViolation('ASAT phase repeated or backdated')
        self.last_tick = ctx.tick
        state = self.s
        # Every arriving shot sees the same beginning-of-impact target set.
        targets = dict(state.orbit.satellites)
        arrivals = [shot for shot in self.strikes.values() if shot.launched and shot.arrival_tick <= ctx.tick]
        destroyed = set()
        for shot in sorted(arrivals, key=lambda s: s.id):
            hit = uniform(ctx.seed, 'asat_hit', ctx.tick, shot.id) < .8
            if hit and shot.target in targets:
                destroyed.add(shot.target)
            # No hit/miss, target existence or damage truth in the shooter's receipt.
            self.recent[shot.owner].append({'command_id': shot.command_id, 'status': 'ARRIVED',
                                            'target_id': shot.target_alias, 'arrival_tick': shot.arrival_tick})
            self.recent[shot.owner] = self.recent[shot.owner][-64:]
            del self.strikes[shot.id]
        events = Counter(targets[key].berth[0] for key in destroyed)
        for key in sorted(destroyed):
            self._retire(targets[key], 'asat_destroyed')
        for plane, count in events.items():
            self.debris_until[plane] = max(self.debris_until[plane], ctx.tick + 20 * ctx.clock.cycle_ticks)
            for satellite in tuple(state.orbit.satellites.values()):
                if satellite.berth[0] != plane:
                    continue
                remaining = max(0, satellite.expires_tick - ctx.tick)
                remaining = floor(remaining * Q(7, 10) ** count)
                if remaining:
                    satellite.expires_tick = ctx.tick + remaining
                else:
                    # Lifetime exhaustion does not recursively create another
                    # kinetic destruction/debris event in this same batch.
                    self._retire(satellite, 'orbital_debris_lifetime_exhausted')
        losses = self.action_cycles.setdefault(ctx.cycle, {})
        for shot in self.strikes.values():
            if shot.launched or shot.launch_tick > ctx.tick:
                continue
            state.initial.inventory.transform({shot.lock(g): q for g, q in self.MATERIALS.items()}, {},
                                              reason='asat_launch', use=Use.ACTION_LOSS)
            losses[shot.owner] = losses.get(shot.owner, Q(0)) + action_loss(self.MATERIALS, state.catalog.weights)
            # Accountability follows the legally declared target owner. It must
            # not turn into an oracle for a stale/hidden target's existence.
            if state.initial.diplomacy.relation(shot.owner, shot.observed_owner) != Relation.WAR:
                state.credit[shot.owner] = max(Q(1, 10), state.credit[shot.owner] - Q(15, 100))
            shot.launched = True
        self.action_cycles = {cycle: values for cycle, values in self.action_cycles.items() if cycle >= ctx.cycle - 19}

    def visible(self, owner):
        return {'in_flight': [{'id': s.id, 'command_id': s.command_id, 'target_id': s.target_alias,
                              'arrival_tick': s.arrival_tick} for s in self.strikes.values() if s.owner == owner],
                'recent': [dict(row) for row in self.recent[owner]]}

    def audit(self):
        for shot in self.strikes.values():
            if shot.arrival_tick <= shot.launch_tick:
                raise InvariantViolation('ASAT has no actual flight time')
            for good, quantity in self.MATERIALS.items():
                if self.s.initial.inventory.balance(shot.lock(good)) != (0 if shot.launched else quantity):
                    raise InvariantViolation('ASAT custody differs from flight state')

    def snapshot(self):
        return {'strikes': self.strikes, 'debris_until': {str(p): t for p, t in self.debris_until.items()},
                'action_cycles': {str(c): rows for c, rows in self.action_cycles.items()},
                'recent': self.recent, 'last_tick': self.last_tick}


class ASATCommands:
    OPERATIONS = frozenset({('military', 'asat_strike')})

    def __init__(self, state):
        self.state = state

    def prepare(self, owner, order, ctx):
        fields(order, {'origin_hub': str, 'target_sat_id': str})
        state = self.state
        if 'N12' not in state.research[owner].known:
            raise RuleViolation('ERR_TECH_LOCKED')
        if not state.authority.controls_hub(owner, order['origin_hub']):
            raise RuleViolation('ERR_NO_PERMISSION')
        target, observed = state.orbital_observation.resolve(owner, order['target_sat_id'])
        command_id = str(int(order['id']))
        strike = ASATStrike('asat:' + digest(owner, command_id).hex()[:24], command_id, owner,
                            order['origin_hub'], target, order['target_sat_id'], observed['owner'],
                            ctx.tick, ctx.tick + 1)
        stock = state.initial.inventory.prepare_transfers(
            Transfer(Account(owner, strike.origin_hub, 'available', good), strike.lock(good), quantity)
            for good, quantity in state.asat.MATERIALS.items())

        def commit():
            stock.commit()
            state.asat.strikes[strike.id] = strike

        return PreparedCommand(commit)
