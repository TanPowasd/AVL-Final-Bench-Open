"""03 section 4 intelligence/cyber_op and intelligence/steal_model (cyber_v1).

Ops: `cyber_op {target_nation, target_hub, op_kind}` with op_kind
S03_infiltrate | S03_steal_tech | S08_paralyze, and `steal_model
{target_nation, target_hub}`. The target Hub must be controlled by the target
nation, which must be a major nation other than the attacker.

S03 family (infiltrate, steal_tech, steal_model): needs S03. Cost
cyber_suite x1 plus the basic-action substitute computing_equipment x1 from
the attacker's capital `available` stock (the 10 compute / 10 B4 electricity
part is not metered; author to confirm). Cooldown 10 cycles per attacker and
target Hub. Success p = clip(.6 + .08*(L_att - L_def) - .4*I[S04], .05, .9),
L = highest tier among the side's known S01-S08 (effective AI network strength
is not added yet), I[S04] = an online S04 counterintel system of the target at
the Hub. Attribution .25, or .65 with S04; an attributed operation costs the
attacker .05 credit (E85) and is reported to the target.

Effects on success:
- S03_infiltrate: the target Hub's C06/C10/E07/L07/L11 systems and its C10
  conditional plans are frozen for ceil(5p) windows (a repeat refreshes, never
  stacks). Against the target's capital it also suspends the target's next
  stock auction once (C33), at most once per 40 cycles per target.
- S03_steal_tech: the attacker gets a one-time -20% theory cost (new or own running project) for the target's
  most advanced non-classified tech in research there (once per tech).
- steal_model: needs a target data centre at the Hub; the attacker receives a
  gamma .7 copy of the target's best model (if it has a free model slot).

S08_paralyze: needs S08; cyber_suite x2 + ew_module x1 + computing_equipment x2;
once per 40 cycles per attacker; always attributed: credit -.1, coercion event
recorded for 20 cycles, and the Hub's conditional plans frozen for 5 ticks.

All inputs are ACTION_LOSS (07) whether or not the operation succeeds.
"""
from dataclasses import dataclass

from .command_schema import fields
from .determinism import digest, uniform
from .errors import RuleViolation
from .ledger import Account
from .numeric import Q, ceil, clip
from .protocol import PreparedCommand
from .provenance import Use
from .scoring import action_loss

S03_COST = {'cyber_suite': 1000, 'computing_equipment': 1000}
S08_COST = {'cyber_suite': 2000, 'ew_module': 1000, 'computing_equipment': 2000}
FROZEN_TECHS = frozenset({'C06', 'C10', 'E07', 'L07', 'L11'})


@dataclass
class CyberOp:
    id: str
    command_id: str
    owner: str
    target: str
    hub: str
    kind: str
    cycle: int


class Cyber:
    def __init__(self, state):
        self.s = state
        self.cooldown = {}            # 'attacker|hub' -> cycle the S03 cooldown ends
        self.s08_next = {}            # attacker -> next allowed S08 cycle
        self.finance_next = {}        # target -> next allowed C33 suspension cycle
        self.suspended_auctions = {}  # target -> pending stock-auction suspensions (0/1)
        self.coercion = {}            # 'attacker|target' -> last cycle of the coercion event
        self.action_cycles = {}
        self.recent = {}
        self.pending = []

    def level(self, nation):
        catalog = self.s.catalog
        return max((catalog.technologies[t]['tier'] for t in self.s.research[nation].known
                    if t in catalog.technologies and t.startswith('S') and t[1:].isdigit()), default=0)

    def probability(self, owner, target, hub):
        s04 = self.s.systems.online(hub, 'counterintel', owner=target)
        return clip(Q(3, 5) + Q(8, 100) * (self.level(owner) - self.level(target)) - Q(2, 5) * int(s04), Q(1, 20), Q(9, 10)), s04

    def _log(self, nation, row):
        self.recent.setdefault(nation, []).append(row)
        self.recent[nation] = self.recent[nation][-64:]

    def advance(self, ctx):
        """P4: resolve this tick's operations with one seeded draw each."""
        state = self.s
        self.action_cycles = {c: v for c, v in self.action_cycles.items() if c >= ctx.cycle - 19}
        for op in self.pending:
            p, s04 = self.probability(op.owner, op.target, op.hub)
            cost = S08_COST if op.kind == 'S08_paralyze' else S03_COST
            losses = self.action_cycles.setdefault(ctx.cycle, {})
            losses[op.owner] = losses.get(op.owner, Q(0)) + action_loss(cost, state.catalog.weights)
            if op.kind == 'S08_paralyze':
                success, attributed = True, True
                state.credit[op.owner] = max(Q(1, 10), state.credit[op.owner] - Q(1, 10))
                self.coercion[f'{op.owner}|{op.target}'] = ctx.cycle + 20
                state.city_economy.record_coercion(op.owner, ctx.cycle)  # 03: coercion +.15 for 20 cycles
                state.cyber_frozen_until[op.hub] = max(state.cyber_frozen_until.get(op.hub, -1), ctx.tick + 5)
            else:
                success = uniform(ctx.seed, 'cyber_success', ctx.tick, op.id) < float(p)
                attributed = uniform(ctx.seed, 'cyber_attribution', ctx.tick, op.id) < (.65 if s04 else .25)
                if attributed:
                    state.credit[op.owner] = max(Q(1, 10), state.credit[op.owner] - Q(5, 100))
                if success:
                    self._effect(op, p, ctx)
            self._log(op.owner, {'command_id': op.command_id, 'tick': ctx.tick, 'op_kind': op.kind,
                                 'success': success, 'attributed': attributed})
            if attributed:
                self._log(op.target, {'tick': ctx.tick, 'op_kind': op.kind, 'hub_id': op.hub, 'attacker': op.owner})
        self.pending = []

    def _effect(self, op, p, ctx):
        state = self.s
        if op.kind == 'S03_infiltrate':
            until = ctx.tick + ceil(5 * p) * ctx.clock.decision_ticks
            state.cyber_frozen_until[op.hub] = max(state.cyber_frozen_until.get(op.hub, -1), until)
            if op.hub == f'{op.target}-h0' and ctx.cycle >= self.finance_next.get(op.target, 0):
                self.suspended_auctions[op.target] = 1
                self.finance_next[op.target] = ctx.cycle + 40
        elif op.kind == 'S03_steal_tech':
            research = state.research[op.owner]
            victim = state.research[op.target]
            candidates = []
            for lid, tech in victim.focus.items():
                line = state.initial.lines.get(lid)
                if line is None or line.hub != op.hub or victim.security.get(lid) == 'classified':
                    continue
                if tech in research.known or research.projects.get(tech) is not None and (
                        research.projects[tech].cost_modifiers.get('stolen') or research.projects[tech].theory_complete):
                    continue
                candidates.append((-state.catalog.technologies[tech]['tier'], tech))
            if candidates:
                tech = min(candidates)[1]
                from .research import Project
                project = research.projects.get(tech)
                if project is None:
                    modifiers = {'licensed': tech in research.licenses, 'open_spillover': False, 'stolen': True,
                                 'source_blockade': research.licenses.get(tech, False)}
                    research.projects[tech] = Project(tech, research.target_cost(tech, modifiers), cost_modifiers=modifiers)
                else:  # an own project in progress gets the one-time -20% too
                    project.cost_modifiers = dict(project.cost_modifiers, stolen=True)
                    project.target_mU = max(project.theory_mU, research.target_cost(tech, project.cost_modifiers))
                    project.theory_complete = project.theory_mU >= project.target_mU
        elif op.kind == 'steal_model':
            ai = state.ai
            model = ai.best(op.target)
            if model is not None and ai.model_slots_used(op.owner) < 16:
                mid = 'model:' + digest('steal', op.owner, op.target, ctx.tick).hex()[:24]
                ai.models[op.owner][mid] = model.copy_for(mid, op.owner, ctx.cycle, Q(7, 10))

    def system_frozen(self, system, tick):
        from .public_systems import SYSTEMS
        return (SYSTEMS[system.kind].technology in FROZEN_TECHS
                and self.s.cyber_frozen_until.get(system.hub, -1) >= tick)

    def take_auction_suspension(self, nation):
        if self.suspended_auctions.get(nation):
            del self.suspended_auctions[nation]
            return True
        return False

    def visible(self, owner):
        return {'recent': list(self.recent.get(owner, ()))}

    def snapshot(self):
        return {'cooldown': self.cooldown, 's08_next': self.s08_next, 'finance_next': self.finance_next,
                'suspended_auctions': self.suspended_auctions, 'coercion': self.coercion,
                'action_cycles': {str(c): v for c, v in self.action_cycles.items()}, 'recent': self.recent,
                'pending': self.pending}

    def empty(self):
        return not any((self.cooldown, self.s08_next, self.finance_next, self.suspended_auctions, self.coercion,
                        self.action_cycles, self.recent, self.pending))


class CyberCommands:
    OPERATIONS = frozenset({('intelligence', 'cyber_op'), ('intelligence', 'steal_model')})

    def __init__(self, state):
        self.state = state

    def prepare(self, owner, order, ctx):
        state = self.state
        book = state.cyber
        if order['op'] == 'steal_model':
            fields(order, {'target_nation': str, 'target_hub': str})
            kind = 'steal_model'
        else:
            fields(order, {'target_nation': str, 'target_hub': str, 'op_kind': str})
            kind = order['op_kind']
            if kind not in ('S03_infiltrate', 'S03_steal_tech', 'S08_paralyze'):
                raise RuleViolation('ERR_SCHEMA')
        target, hub = order['target_nation'], order['target_hub']
        if target == owner or target not in state.initial.known or hub not in state.initial.map.hubs:
            raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        if not state.authority.controls_hub(target, hub):
            raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        known = state.research[owner].known
        needed = 'S08' if kind == 'S08_paralyze' else 'S03'
        if needed not in known:
            raise RuleViolation('ERR_TECH_LOCKED')
        if kind == 'S08_paralyze':
            if ctx.cycle < book.s08_next.get(owner, 0):
                raise RuleViolation('ERR_CAPACITY_EXCEEDED', 'S08 at most once per 40 cycles')
        else:
            if ctx.cycle < book.cooldown.get(f'{owner}|{hub}', 0):
                raise RuleViolation('ERR_CAPACITY_EXCEEDED', 'S03 cooldown for this hub')
        if kind == 'steal_model' and not any(c.owner == target and c.center.hub == hub
                                             for c in state.compute.centers.values()):
            raise RuleViolation('ERR_SITE_INCOMPATIBLE', 'no target data centre at this hub')
        cost = S08_COST if kind == 'S08_paralyze' else S03_COST
        capital = f'{owner}-h0'
        inv = state.initial.inventory
        for good, quantity in cost.items():
            if inv.balance(Account(owner, capital, 'available', good)) < quantity:
                raise RuleViolation('ERR_INSUFFICIENT_STOCK')
        command_id = str(int(order['id']))
        op = CyberOp('cyber:' + digest(owner, command_id).hex()[:24], command_id, owner, target, hub, kind, ctx.cycle)

        def commit():
            inv.transform({Account(owner, capital, 'available', g): q for g, q in cost.items()}, {},
                          reason='cyber_operation', use=Use.ACTION_LOSS)
            if kind == 'S08_paralyze':
                book.s08_next[owner] = ctx.cycle + 40
            else:
                book.cooldown[f'{owner}|{hub}'] = ctx.cycle + 10
            book.pending.append(op)

        return PreparedCommand(commit)
