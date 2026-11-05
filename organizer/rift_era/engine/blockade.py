"""03 section 7 military/blockade (blockade_v1).

`military/blockade {formation_id, edge_id, target_nations}` is a persistent
plan, one edge per formation (a new order for the same formation replaces it;
cancel_by_id ends it). Land formations must sit at an endpoint Hub of a
road/rail edge; Sea-Surface/Sea-Sub formations at an endpoint Hub of a sea or
river edge. Air formations cannot blockade. Every target must be at WAR with
the blockader when the order is given; a target that is no longer at WAR is
skipped from that tick on.

P4, after the battle: a plan is effective this tick only if its formation is
at its endpoint, did not fire or move this tick (one shared action budget), is
not refitting, and each counted unit actually drew its full `combat_tick`
supply (from the formation's onboard stock, else its Hub stock). The draw is
ACTION_LOSS, one event per formation per tick. Position at the endpoint gives
the blockader its own observation of the edge (q_edge >= .2 holds).

A[edge, q] = sum over effective blockaders targeting q of
firepower[channel] * hp_ratio * crew_ratio, channel `ground` for land edges and
`sea` for sea/river edges; D[edge, q] = the same for formations of q or of any
nation at WAR with the blockader that are on an `escort` task at an endpoint of
the edge. Allied blockaders pool fire before one `blockade_fraction`. The
resulting B replaces `corridor.blocked` at the end of P4, so it governs the next
tick's P2/P3 entry capacity; an edge with no effective blockade has B = 0.
"""
from collections import defaultdict
from dataclasses import dataclass

from .combat import blockade_fraction
from .command_schema import fields
from .diplomacy import Relation
from .errors import RuleViolation
from .ledger import Account
from .numeric import Q, units
from .protocol import PreparedCommand
from .provenance import Use
from .scoring import action_loss

LAND_MODES = ('road', 'rail')
WATER_MODES = ('sea', 'river')


@dataclass
class BlockadePlan:
    owner: str
    command_id: str
    formation_id: str
    edge_id: str
    targets: tuple


def _channel(edge):
    return 'ground' if edge.mode in LAND_MODES else 'sea'


def _fire(state, formation, channel):
    total = Q(0)
    for uid in formation.unit_ids:
        unit = state.initial.units[uid]
        spec = state.catalog.units[unit.unit_type]
        if unit.hp_mU <= 0:
            continue
        total += (Q(spec['firepower'][channel]) * min(Q(1), Q(unit.hp_mU, units(spec['hp'])))
                  * min(Q(1), Q(unit.crew_mU, max(1, units(spec['crew'])))))
    return total


def _position_ok(edge, formation):
    if formation.hub not in (edge.a, edge.b):
        return False
    if formation.domain == 'Land':
        return edge.mode in LAND_MODES
    if formation.domain in ('Sea-Surface', 'Sea-Sub'):
        return edge.mode in WATER_MODES
    return False


class Blockades:
    def __init__(self, state):
        self.s = state
        self.plans = {}          # formation id -> plan
        self.effective = {}      # (edge, target) -> B applied for the next tick
        self.action_cycles = {}  # cycle -> owner -> action loss

    def advance(self, ctx):
        state = self.s
        self.action_cycles = {c: v for c, v in self.action_cycles.items() if c >= ctx.cycle - 19}
        diplomacy = state.initial.diplomacy
        result = state.battles.last_result
        fired = {shot.source for shot in result.shots} if result is not None else set()
        attack = defaultdict(lambda: Q(0))
        blockers = defaultdict(set)
        for fid in sorted(self.plans):
            plan = self.plans[fid]
            formation = state.formations.rows.get(fid)
            edge = state.cargo.corridors.get(plan.edge_id)
            if formation is None or edge is None or formation.owner != plan.owner or formation.refitting:
                continue
            if not _position_ok(edge, formation) or state.movement.planned(fid) or fid in state.movement.retreating:
                continue
            if any(uid in fired for uid in formation.unit_ids):
                continue
            targets = [t for t in plan.targets if t in diplomacy.actors and diplomacy.relation(plan.owner, t) == Relation.WAR]
            if not targets:
                continue
            drawn = self._draw(formation, ctx)
            if drawn is None:
                continue
            channel = _channel(edge)
            fire = _fire(state, formation, channel)
            if fire <= 0:
                continue
            for target in targets:
                attack[edge.id, target] += fire
                blockers[edge.id, target].add(plan.owner)
        effective = {}
        for (eid, target), a in sorted(attack.items()):
            edge = state.cargo.corridors[eid]
            channel = _channel(edge)
            d = Q(0)
            for formation in state.formations.rows.values():
                if formation.task != 'escort' or formation.hub not in (edge.a, edge.b):
                    continue
                if formation.owner == target or any(diplomacy.relation(formation.owner, b) == Relation.WAR
                                                    for b in blockers[eid, target] if formation.owner in diplomacy.actors):
                    d += _fire(state, formation, channel)
            b = blockade_fraction(a, d)
            if b:
                effective[eid, target] = b
        for eid, edge in state.cargo.corridors.items():
            if edge.blocked:
                edge.blocked = {}
        for (eid, target), b in effective.items():
            state.cargo.corridors[eid].blocked[target] = b
        self.effective = effective

    def _draw(self, formation, ctx):
        """Take every unit's full combat_tick supply, or nothing (one event)."""
        state = self.s
        inv = state.initial.inventory
        need = defaultdict(int)
        for uid in formation.unit_ids:
            unit = state.initial.units[uid]
            if unit.hp_mU <= 0:
                continue
            for good, quantity in state.catalog.units[unit.unit_type]['combat_tick'].items():
                if not quantity:
                    continue
                if good == 'fuel':
                    quantity *= unit.fuel_factor
                if good == 'ammunition' and unit.precision:
                    good = 'precision_ammunition'
                need[good] += units(quantity)
        taken = {}
        for good, amount in sorted(need.items()):
            onboard = Account(formation.owner, formation.id, 'available', good)
            hub = Account(formation.owner, formation.hub, 'available', good)
            source = onboard if inv.balance(onboard) >= amount else hub
            if inv.balance(source) < amount:
                return None
            taken[source] = amount
        if taken:
            inv.transform(taken, {}, reason='military_blockade', use=Use.ACTION_LOSS)
        losses = self.action_cycles.setdefault(ctx.cycle, {})
        losses[formation.owner] = losses.get(formation.owner, Q(0)) + action_loss(
            {a.asset: q for a, q in taken.items()}, state.catalog.weights)
        return taken

    def region_share(self, region, nation):
        """07 Blockade_r for nation's Qsec: intact rated capacity-weighted B over in-edges."""
        state = self.s
        hubs = state.initial.map.hubs
        total = weighted = 0
        for edge in state.cargo.corridors.values():
            if hubs[edge.a].region != region and hubs[edge.b].region != region:
                continue
            total += edge.capacity_mU_cycle
            weighted += edge.capacity_mU_cycle * edge.blocked.get(nation, Q(0))
        return Q(weighted) / total if total else Q(0)

    def visible(self, owner):
        return {'plans': [p for p in self.plans.values() if p.owner == owner],
                'effective': [{'edge_id': e, 'target_nation': t, 'blocked_mU': int(b * 1000)}
                              for (e, t), b in sorted(self.effective.items()) if t == owner
                              or any(p.owner == owner and p.edge_id == e for p in self.plans.values())]}

    def snapshot(self):
        return {'plans': self.plans, 'effective': [[e, t, b] for (e, t), b in sorted(self.effective.items())],
                'action_cycles': {str(c): v for c, v in self.action_cycles.items()}}

    def empty(self):
        return not (self.plans or self.effective or self.action_cycles)


class BlockadeCommands:
    OPERATIONS = frozenset({('military', 'blockade')})

    def __init__(self, state):
        self.state = state

    def prepare(self, owner, order, ctx):
        state = self.state
        fields(order, {'formation_id': str, 'edge_id': str, 'target_nations': list})
        targets = order['target_nations']
        if (not 1 <= len(targets) <= 8 or any(type(t) is not str for t in targets) or len(set(targets)) != len(targets)
                or owner in targets):
            raise RuleViolation('ERR_SCHEMA')
        formation = state.formations.rows.get(order['formation_id'])
        if formation is None or formation.owner != owner:
            raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        edge = state.cargo.corridors.get(order['edge_id'])
        if edge is None:
            raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        if not _position_ok(edge, formation):
            raise RuleViolation('ERR_SITE_INCOMPATIBLE', 'formation is not at an endpoint of a compatible edge')
        diplomacy = state.initial.diplomacy
        if any(t not in diplomacy.actors for t in targets):
            raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        if any(not state.relations.will_be_at_war(owner, t) for t in targets):
            raise RuleViolation('ERR_NOT_AT_WAR')
        plan = BlockadePlan(owner, str(int(order['id'])), formation.id, edge.id, tuple(sorted(targets)))
        return PreparedCommand(lambda: state.blockades.plans.__setitem__(formation.id, plan), persistent=True)


def cancel(state, owner, target):
    plan = next((p for p in state.blockades.plans.values() if p.owner == owner and p.command_id == target), None)
    if plan is None:
        return None
    return PreparedCommand(lambda: state.blockades.plans.pop(plan.formation_id))
