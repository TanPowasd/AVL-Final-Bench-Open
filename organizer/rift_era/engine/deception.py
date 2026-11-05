"""S06 signals backed by real consumables, observer-local exposure and lifetime.

A decoy is a target-only combat proxy, never a physical unit or an asset. Its
reported strength attracts fire but cannot create casualties or destruction
credit. Public contact serialization belongs exclusively to MilitaryVision.
"""
from dataclasses import dataclass, field

from .command_schema import fields
from .combat import Unit
from .determinism import digest, uniform
from .errors import RuleViolation, InvariantViolation
from .military_sensors import Signal
from .numeric import Q
from .protocol import PreparedCommand
from .provenance import Use
from .scoring import action_loss
from .world import SIZE, COLS, ROWS


@dataclass
class Decoy:
    id: str
    owner: str
    region: str
    cell: int
    unit_type: str
    strength_mU: int
    created_window: int
    expires_window: int
    exposed_to: set[str] = field(default_factory=set)
    credit_penalized: bool = False


class Deception:
    def __init__(self, state):
        self.s = state
        self.rows = {}
        self.window_counts = {}
        self.action_cycles = {}

    def begin_tick(self, ctx):
        self.rows = {key: row for key, row in self.rows.items() if ctx.window < row.expires_window}
        self.window_counts = {p: value for p, value in self.window_counts.items() if value[0] == ctx.window}
        self.action_cycles = {c: v for c, v in self.action_cycles.items() if c >= ctx.cycle - 19}

    def signal(self, row):
        return Signal(row.id, row.owner, self.s.catalog.units[row.unit_type]['domain'], row.region,
                      row.cell, row.strength_mU, emitting=True)

    def proxies(self):
        return {key: Unit(key, row.owner, row.unit_type, row.region, row.strength_mU, 0)
                for key, row in self.rows.items()}

    def exposed(self, owner, target):
        row = self.rows.get(target)
        return row is not None and owner in row.exposed_to

    def investigate(self, owner, target, window, alias):
        """Caller invokes once per legal observer trajectory per window.

        Draw even for real contacts using the same alias-local stream. Never
        return real/false classification except after a successful exposure.
        """
        enhanced = 'S04' in self.s.research[owner].known
        draw = uniform(self.s.initial.map.seed, 'counterintelligence:' + owner, window, alias)
        row = self.rows.get(target)
        if row is None or draw >= (.65 if enhanced else .25):
            return False
        row.exposed_to.add(owner)
        if enhanced and not row.credit_penalized:
            self.s.credit[row.owner] = max(Q(1,10), self.s.credit[row.owner] - Q(3,100))
            row.credit_penalized = True
        return True

    def visible(self, owner):
        # Only the author knows its own issued signals. No exposure feedback.
        return [{'id': row.id, 'target_region': row.region, 'reported_type': row.unit_type,
                 'reported_strength_mU': row.strength_mU, 'expires_window': row.expires_window}
                for row in self.rows.values() if row.owner == owner]

    def audit(self):
        actors = self.s.initial.diplomacy.actors
        for key, row in self.rows.items():
            if (key != row.id or row.owner not in actors or row.region not in self.s.initial.map.regions
                    or row.unit_type not in self.s.catalog.units or not 0 <= row.strength_mU <= 10000000
                    or row.expires_window != row.created_window + 10 or not row.exposed_to <= actors):
                raise InvariantViolation('invalid deception signal')
            if key in self.s.initial.units or key in self.s.formations.rows:
                raise InvariantViolation('deception signal became a physical combatant')
        if any(not 0 <= count <= 4 for _, count in self.window_counts.values()):
            raise InvariantViolation('deception window cap exceeded')

    def snapshot(self):
        return {'rows': self.rows, 'window_counts': self.window_counts,
                'action_cycles': {str(c): value for c, value in self.action_cycles.items()}}


class IntelligenceCommands:
    OPERATIONS = frozenset({('intelligence', 'intel_op')})

    def __init__(self, state):
        self.state = state

    def prepare(self, owner, order, ctx):
        fields(order, {'target_region': str, 'op_kind': str},
               {'reported_strength_mU': int, 'reported_type': str})
        state = self.state
        if order['op_kind'] != 'S06_decoy':
            raise RuleViolation('ERR_SCHEMA', 'intelligence operation not implemented')
        if 'S06' not in state.research[owner].known:
            raise RuleViolation('ERR_TECH_LOCKED')
        region = state.initial.map.regions.get(order['target_region'])
        if region is None:
            raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        unit_type = order.get('reported_type', 'motorized')
        strength = order.get('reported_strength_mU', 0)
        if unit_type not in state.catalog.units or not 0 <= strength <= 10000000:
            raise RuleViolation('ERR_SCHEMA')
        used_window, used = state.deception.window_counts.get(owner, (ctx.window, 0))
        count = used if used_window == ctx.window else 0
        if count >= 4:
            raise RuleViolation('ERR_CAPACITY_EXCEEDED')
        # The command supplies no launch Hub. Pick one controlled stocked Hub;
        # do not combine remote fractions or debit the foreign target region.
        sources = [a for a, quantity in state.initial.inventory.balances().items()
                   if a.owner == owner and a.bucket == 'available' and a.asset == 'cyber_suite'
                   and quantity >= 100 and state.authority.controls_hub(owner, a.location)]
        if not sources:
            raise RuleViolation('ERR_INSUFFICIENT_STOCK')
        command_id = str(int(order['id']))
        source = min(sources, key=lambda a: digest(ctx.seed, 'decoy_source', ctx.tick, owner, command_id, a.location))
        stock = state.initial.inventory.prepare_transform({source: 100}, {}, reason='S06_decoy', use=Use.ACTION_LOSS)
        x = (2 * region.column + 1) * SIZE // (2 * COLS)
        y = (2 * region.row + 1) * SIZE // (2 * ROWS)
        row = Decoy('decoy:'+digest(owner,command_id).hex()[:24], owner, region.id, y * SIZE + x,
                    unit_type, strength, ctx.window, ctx.window + 10)

        def commit():
            stock.commit()
            state.deception.rows[row.id] = row
            state.deception.window_counts[owner] = (ctx.window, count + 1)
            losses = state.deception.action_cycles.setdefault(ctx.cycle, {})
            losses[owner] = losses.get(owner, Q(0)) + action_loss({'cyber_suite': 100}, state.catalog.weights)

        return PreparedCommand(commit)
