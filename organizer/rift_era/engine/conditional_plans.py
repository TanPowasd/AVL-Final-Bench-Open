"""06 C10 governance/conditional_plan: cross-system trigger rules (conditional_plan_v1).

A plan is registered by a normal command and lives until cancelled
(`cancel/cancel_by_id` with the plan's command id). At most 32 per nation.

Evaluation: once at the first tick of every decision window, inside P1 and
before the seats' own orders, on the state left by the previous window (the
snapshot the seats observed). For each plan, in (owner, command id) order:
the host Hub must still be controlled by the owner and carry an owned,
non-destroyed line with the `ai_core` addon (and not be frozen by S03/S08);
the cooldown since the last actual execution must have elapsed; the metric
must satisfy the comparator. The action is then prepared and committed
through the ordinary command path; a rejection is recorded on the plan and
still counts as the window's one execution attempt (no retries, no back-fill).

Metrics: `market_price` = spot venue price (mU) of good `ref_id` at the Hub;
`grid_price` = the Hub's last grid price in RU mU; `hub_stock` = the owner's
`available` stock (mU) of good `ref_id` at the Hub. A metric with no value
(no venue yet) never triggers.
"""
from dataclasses import dataclass, field

from .command_schema import fields
from .errors import RuleViolation
from .ledger import Account
from .protocol import PreparedCommand, validate_shape

METRICS = ('market_price', 'grid_price', 'hub_stock')
LIMIT = 32
FORBIDDEN_ACTIONS = {('governance', 'conditional_plan'), ('cancel', 'cancel_by_id')}


@dataclass
class ConditionalPlan:
    id: str
    owner: str
    hub: str
    metric: str
    ref_id: str
    comparator: str
    threshold_mU: int
    action: dict
    cooldown_ticks: int
    last_executed_tick: int | None = None
    executions: int = 0
    last_result: dict = field(default_factory=dict)


def _host_online(state, owner, hub, tick):
    if not state.authority.controls_hub(owner, hub):
        return False
    if state.cyber_frozen_until.get(hub, -1) >= tick:
        return False
    for line in state.initial.lines.values():
        if line.owner == owner and line.hub == hub and 'ai_core' in state.line_options.get(line.id, {}).get('addons', ()):
            asset = state.initial.assets.get(line.id)
            if asset is not None and asset.health_mU > 0:
                return True
    return False


def metric_value(state, plan):
    if plan.metric == 'market_price':
        return state.spot.prices.get(f'spot:{plan.hub}:{plan.ref_id}')
    if plan.metric == 'grid_price':
        price = state.grid_prices_ru.get(plan.hub)
        return None if price is None else price * 1000
    return state.initial.inventory.balance(Account(plan.owner, plan.hub, 'available', plan.ref_id))


class ConditionalPlanCommands:
    OPERATIONS = frozenset({('governance', 'conditional_plan')})

    def __init__(self, state):
        self.state = state

    def prepare(self, owner, order, ctx):
        state = self.state
        fields(order, {'hub_id': str, 'metric': str, 'ref_id': str, 'comparator': str, 'threshold_mU': int,
                       'action': dict, 'cooldown_ticks': int})
        if (order['metric'] not in METRICS or order['comparator'] not in ('<=', '>=')
                or order['cooldown_ticks'] < ctx.clock.decision_ticks):
            raise RuleViolation('ERR_SCHEMA')
        action = order['action']
        if not isinstance(action.get('kind'), str) or not isinstance(action.get('op'), str) or 'id' in action:
            raise RuleViolation('ERR_SCHEMA', 'action must be a command body without its own id')
        if (action['kind'], action['op']) in FORBIDDEN_ACTIONS:
            raise RuleViolation('ERR_SCHEMA', 'a plan cannot create, cancel or chain plans')
        validate_shape(action)
        if order['metric'] in ('market_price', 'hub_stock') and order['ref_id'] not in state.catalog.goods:
            raise RuleViolation('ERR_SCHEMA')
        if order['hub_id'] not in state.initial.map.hubs:
            raise RuleViolation('ERR_SCHEMA')
        if 'C10' not in state.research[owner].known:
            raise RuleViolation('ERR_TECH_LOCKED')
        if not _host_online(state, owner, order['hub_id'], ctx.tick):
            raise RuleViolation('ERR_SITE_INCOMPATIBLE', 'hub needs an owned online ai_core')
        plans = state.conditional_plans.setdefault(owner, {})
        if len(plans) >= LIMIT:
            raise RuleViolation('ERR_ENTITY_CAP_EXCEEDED')
        pid = str(int(order['id']))
        plan = ConditionalPlan(pid, owner, order['hub_id'], order['metric'], order['ref_id'], order['comparator'],
                               order['threshold_mU'], dict(action), order['cooldown_ticks'])
        return PreparedCommand(lambda: plans.__setitem__(pid, plan), persistent=True)


def cancel(state, owner, target):
    plan = state.conditional_plans.get(owner, {}).get(target)
    if plan is None:
        return None
    return PreparedCommand(lambda: state.conditional_plans[owner].pop(target))


def run_plans(state, ctx, prepare):
    """P1, first tick of the window, before seat orders."""
    if not ctx.clock.is_decision(ctx.tick):
        return
    for owner in sorted(state.conditional_plans):
        for pid in sorted(state.conditional_plans[owner], key=int):
            plan = state.conditional_plans[owner].get(pid)
            if plan is None:
                continue
            if plan.last_executed_tick is not None and ctx.tick - plan.last_executed_tick < plan.cooldown_ticks:
                continue
            if not _host_online(state, owner, plan.hub, ctx.tick):
                continue
            value = metric_value(state, plan)
            if value is None or not (value <= plan.threshold_mU if plan.comparator == '<=' else value >= plan.threshold_mU):
                continue
            plan.executions += 1
            plan.last_executed_tick = ctx.tick
            # Generated orders never collide with seat command ids (ids < 10**30 in practice).
            order = dict(plan.action, id=str(10**30 * plan.executions + int(pid)))
            try:
                prepared = prepare(owner, order, ctx)
                prepared.commit()
                plan.last_result = {'tick': ctx.tick, 'status': 'ACCEPTED'}
            except RuleViolation as exc:
                plan.last_result = {'tick': ctx.tick, 'status': 'REJECTED', 'code': exc.code}
