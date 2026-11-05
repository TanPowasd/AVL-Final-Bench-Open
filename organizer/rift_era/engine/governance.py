"""Fiscal custody, resident training and the timed domestic policy state machines.

Training residents remain in their local food, housing and mortality denominator,
but leave the employable skill pools until completion or cancellation. Policy
changes do not rewrite the tax rate frozen at the beginning of a cycle.
"""

from dataclasses import dataclass, field
from fractions import Fraction

from .command_schema import fields
from .determinism import digest
from .errors import InvariantViolation, RuleViolation
from .ledger import Account, CashTransfer, DeliveryPayment, Transfer
from .numeric import Q, ceil, clip, floor, split_payment
from .payments import Obligation
from .protocol import PreparedCommand
from .provenance import Use


TRACKS = {'general_to_skilled': ('general_worker', 'skilled_worker', 6, Q(1, 5)),
          'skilled_to_engineer': ('skilled_worker', 'engineer', 12, Q(1, 2))}
REGIMES = {'science_doctrine': (frozenset(('R10', 'R12')), 40),
           'economic_doctrine': (frozenset(('F07', 'F08')), 60)}


@dataclass
class DomesticPolicy:
    nominal_tax: Fraction
    welfare_budget_mU: int = 0
    maintenance_budget_mU: int | None = None
    maintenance_remaining_mU: int | None = None
    mobilized: bool = False
    mobilized_tick: int = -1
    mobilization: Fraction = Q(0)
    fatigue: Fraction = Q(0)
    welfare_need_mU: Fraction = Q(0)
    welfare_paid_mU: int = 0
    regimes: dict[str, str] = field(default_factory=dict)
    transitions: dict[str, tuple[str, int]] = field(default_factory=dict)


@dataclass
class Training:
    id: str
    command_id: str
    owner: str
    hub: str
    track: str
    duration_ticks: int
    supplies_mU: int
    price: Fraction
    elapsed: int = 0
    used_mU: int = 0
    waiting: bool = True


class Governance:
    def __init__(self, state):
        self.state = state
        self.policies = {p: DomesticPolicy(state.tax[p]) for p in state.initial.known}
        self.autonomy = {}
        self.training: dict[str, Training] = {}
        self.remediation_mU = {}
        self.events = {}
        self.last_cycle = -1

    def participant_fraction(self, owner, hub):
        gene = self.state.last_satisfaction.get(hub, {}).get('gene_therapy', Q(0))
        return Q(65, 100) + Q(5, 100) * min(Q(1), gene) + Q(13, 100) * self.policies[owner].mobilization

    def active(self, owner, choice):
        return choice in self.policies[owner].regimes.values()

    def begin_tick(self, ctx):
        state = self.state
        for policy in self.policies.values():
            for group, (choice, until) in tuple(policy.transitions.items()):
                if ctx.tick >= until:
                    policy.regimes[group] = choice
                    del policy.transitions[group]
        if ctx.tick % ctx.clock.cycle_ticks:
            return
        if ctx.cycle <= self.last_cycle:
            raise InvariantViolation('domestic cycle advanced twice')
        self.last_cycle = ctx.cycle
        self.remediation_mU.clear()
        for owner, policy in self.policies.items():
            known = state.research[owner].known
            state.tax[owner] = min(Q(55, 100), policy.nominal_tax * (Q(11, 10) if 'F01' in known else 1))
            residents = sum(p.total_mU for h, p in state.initial.populations.items()
                            if state.initial.map.hubs[h].owner == owner)
            policy.welfare_need_mU = residents * Q(5, 100) * min(Q(3), state.cpi[owner]) if 'F05' in known else Q(0)
            policy.welfare_paid_mU = 0
            policy.maintenance_remaining_mU = policy.maintenance_budget_mU
            lock = state.native(owner, 'welfare_reserved')
            source = state.native(owner, 'treasury')
            state.payments.release(lock, source)
            amount = min(policy.welfare_budget_mU, ceil(policy.welfare_need_mU), state.initial.cash.balance(source).native_mU)
            state.initial.cash.transfer(source, lock, amount)

    def region_for(self, hub):
        return self.state.initial.map.hubs[hub].region

    def collect_tax(self, payer, hub, exact_mU, *, kind, allow_arrears=False,settle_arrears=False,apply_collection=True):
        """Tax is paid once, then divided; autonomy cannot mint a second payment."""
        state, owner = self.state, payer.owner
        if apply_collection:exact_mU *= self.collection_factor(hub)
        pool = state.native(owner, 'tax_receipts')
        receipt = state.payments.pay(payer, pool, exact_mU, kind=kind, allow_arrears=allow_arrears,settle_arrears=settle_arrears)
        local = self.autonomy.get(self.region_for(hub), Q(0))
        grant = split_payment(receipt.paid_mU, {'local': local, 'treasury': 1 - local},
                              key=(state.initial.map.seed, 'tax_retention', self.last_cycle, hub, kind))
        state.initial.cash.transfer_many((CashTransfer(pool, state.native(owner, f'public:{hub}'), grant['local']),
                                         CashTransfer(pool, state.native(owner, 'treasury'), grant['treasury'])))
        state.cycles[owner].taxes_mU += grant['treasury']
        return receipt

    def collection_factor(self, hub):
        population = self.state.initial.populations.get(hub)
        return Q(1, 2) if population and population.warning == 'GOV_ALERT_STRIKE' else Q(1)

    def quote_tax(self, payer, hub, exact_mU, kind):
        return self.state.payments.quote(payer, self.state.native(payer.owner, 'tax_receipts'),
                                         exact_mU * self.collection_factor(hub), kind)

    def social_ratio(self, owner):
        policy = self.policies[owner]
        if 'F05' not in self.state.research[owner].known:
            return None
        return min(Q(1), Q(policy.welfare_paid_mU) / policy.welfare_need_mU) if policy.welfare_need_mU else Q(1)

    def settle_welfare(self, ctx):
        if not ctx.clock.is_cycle_end(ctx.tick):
            return
        state = self.state
        for owner, policy in self.policies.items():
            weights = {h: p.total_mU for h, p in state.initial.populations.items()
                       if state.initial.map.hubs[h].owner == owner and p.total_mU}
            lock = state.native(owner, 'welfare_reserved')
            amount = state.initial.cash.balance(lock).native_mU
            if weights:
                grants = split_payment(amount, weights, key=(ctx.seed, 'social_security', ctx.cycle, owner))
                state.initial.cash.transfer_many(CashTransfer(lock, state.native(owner, f'household:{h}'), q)
                                                 for h, q in grants.items())
                policy.welfare_paid_mU = amount
            else:
                state.payments.release(lock, state.native(owner, 'treasury'))

    def end_cycle(self, ctx):
        if not ctx.clock.is_cycle_end(ctx.tick):
            return
        for policy in self.policies.values():
            policy.mobilization = clip(policy.mobilization + (Q(1, 2) if policy.mobilized else -Q(1, 2)), Q(0), Q(1))
            policy.fatigue = clip(policy.fatigue + (Q(35, 1000) if policy.mobilized else -Q(25, 1000)), Q(0), Q(1))
        self.events.clear()

    def training_stock(self, job):
        return Account(job.owner, job.hub, f'locked_training:{job.track}', 'lab_supplies')

    def training_cash(self, job):
        return self.state.native(job.owner, f'training:{job.hub}:{job.track}')

    def finish_training(self, job, *, completed):
        state = self.state
        source, destination, _, _ = TRACKS[job.track]
        population = state.initial.populations[job.hub]
        quantity = population.training_mU.pop(job.id)
        population.receive({destination if completed else source: quantity})
        account = self.training_stock(job)
        state.initial.inventory.transfer(account, Account(job.owner, job.hub, 'available', 'lab_supplies'),
                                         state.initial.inventory.balance(account))
        state.payments.release(self.training_cash(job), state.native(job.owner, 'treasury'))
        del self.training[job.id]

    def advance_training(self, ctx):
        state = self.state
        for job in tuple(self.training.values()):
            if not state.initial.populations[job.hub].training_mU[job.id]:
                self.finish_training(job, completed=False)
                continue
            job.waiting = not state.systems.online(job.hub, 'academy', owner=job.owner)
            if job.waiting:
                continue
            amount = job.supplies_mU * (job.elapsed + 1) // job.duration_ticks - job.used_mU
            stock = state.initial.inventory.prepare_transform({self.training_stock(job): amount}, {},
                                                               reason='academy_training', use=Use.CIVILIAN)
            from .economic_ownership import prepare_purchase,record_income
            payment,recipients=prepare_purchase(state.payments,self.training_cash(job),stock.materials.events,
                {'lab_supplies':job.price},kind='training_supplies',key=(ctx.seed,ctx.tick,'training_supplies',job.id),
                buyer=state.native(job.owner,'treasury'))
            payment.commit(stock)
            record_income(state,recipients,payment.amounts)
            job.used_mU += amount
            job.elapsed += 1
            if job.elapsed == job.duration_ticks:
                self.finish_training(job, completed=True)

    def audit(self):
        resident_jobs = {j for p in self.state.initial.populations.values() for j in p.training_mU}
        if resident_jobs != self.training.keys() | self.state.recruitment.training_ids():
            raise InvariantViolation('training pool and resident custody disagree')
        for job in self.training.values():
            population = self.state.initial.populations[job.hub]
            if (population.training_mU[job.id] < 0 or not 0 <= job.elapsed < job.duration_ticks
                    or job.used_mU != job.supplies_mU * job.elapsed // job.duration_ticks
                    or self.state.initial.inventory.balance(self.training_stock(job)) != job.supplies_mU - job.used_mU):
                raise InvariantViolation('training progress, custody or material conservation failure')

    def snapshot(self):
        return {'policies': self.policies, 'autonomy': self.autonomy, 'training': self.training,
                'remediation_mU': self.remediation_mU, 'events': self.events, 'last_cycle': self.last_cycle}

    def visible(self, owner):
        return {'policy': self.policies[owner], 'effective_tax': self.state.tax[owner],
                'autonomy': {r: a for r, a in self.autonomy.items() if self.state.initial.map.regions[r].sovereign == owner},
                'training': [j for j in self.training.values() if j.owner == owner],
                **({'conditional_plans': list(self.state.conditional_plans[owner].values())}
                   if self.state.conditional_plans.get(owner) else {})}


class GovernanceCommands:
    OPERATIONS = frozenset({('governance', op) for op in ('fiscal_policy', 'set_regime', 'academy_train', 'mobilize', 'autonomy')} |
                           {('industry', 'remediate_pollution')})

    def __init__(self, state):
        self.state = state

    def prepare(self, owner, order, ctx):
        state, gov = self.state, self.state.governance
        policy, op = gov.policies[owner], order['op']
        if op == 'fiscal_policy':
            fields(order, {'tax_rate_bp': int, 'welfare_budget_mU': int, 'maint_budget_mU': int})
            if not 500 <= order['tax_rate_bp'] <= 5000 or min(order['welfare_budget_mU'], order['maint_budget_mU']) < 0:
                raise RuleViolation('ERR_SCHEMA')
            def commit():
                policy.nominal_tax = Q(order['tax_rate_bp'], 10000)
                policy.welfare_budget_mU = order['welfare_budget_mU']
                policy.maintenance_budget_mU = order['maint_budget_mU']
            return PreparedCommand(commit)
        if op == 'mobilize':
            fields(order, {'enabled': bool})
            if policy.mobilized and not order['enabled'] and ctx.tick < policy.mobilized_tick + 4 * ctx.clock.cycle_ticks:
                raise RuleViolation('ERR_NO_PERMISSION', 'mobilization is locked for four cycles')
            def commit():
                if order['enabled'] and not policy.mobilized:
                    policy.mobilized_tick = ctx.tick
                policy.mobilized = order['enabled']
            return PreparedCommand(commit)
        if op == 'set_regime':
            fields(order, {'group': str, 'choice_tech_id': str})
            group, choice = order['group'], order['choice_tech_id']
            if group not in REGIMES or choice not in REGIMES[group][0]:
                raise RuleViolation('ERR_SCHEMA')
            if choice not in state.research[owner].known:
                raise RuleViolation('ERR_TECH_LOCKED')
            if group in policy.transitions:
                raise RuleViolation('ERR_NO_PERMISSION', 'regime transition already in progress')
            def commit():
                if policy.regimes.get(group) != choice:
                    policy.regimes.pop(group, None)
                    policy.transitions[group] = (choice, ctx.tick + REGIMES[group][1] * ctx.clock.cycle_ticks)
            return PreparedCommand(commit)
        if op in ('autonomy', 'remediate_pollution'):
            fields(order, {'region_id': str, 'autonomy_mU' if op == 'autonomy' else 'amount_mU': int})
            region = state.initial.map.regions.get(order['region_id'])
            if region is None or region.sovereign != owner:
                raise RuleViolation('ERR_NO_PERMISSION')
            if op == 'autonomy':
                if not 0 <= order['autonomy_mU'] <= 500:
                    raise RuleViolation('ERR_SCHEMA')
                return PreparedCommand(lambda: gov.autonomy.__setitem__(region.id, Q(order['autonomy_mU'], 1000)))
            if 'V02' not in state.research[owner].known:
                raise RuleViolation('ERR_TECH_LOCKED')
            if order['amount_mU'] < 0:
                raise RuleViolation('ERR_SCHEMA')
            hubs = sorted(h.id for h in state.initial.map.hubs.values() if h.region == region.id and h.owner == owner)
            if not hubs:
                raise RuleViolation('ERR_SITE_INCOMPATIBLE')
            inventory = state.initial.inventory
            source = Account(owner, hubs[0], 'available', 'remediation_agent')
            plan = inventory.prepare_transform({source: order['amount_mU']}, {}, reason='environment_remediation', use=Use.CIVILIAN)
            def commit():
                plan.commit()
                gov.remediation_mU[region.id] = gov.remediation_mU.get(region.id, 0) + order['amount_mU']
            return PreparedCommand(commit)
        fields(order, {'hub_id': str, 'track': str, 'trainees_mU': int})
        hub, track, quantity = order['hub_id'], order['track'], order['trainees_mU']
        if track not in TRACKS or not 0 <= quantity <= 5000:
            raise RuleViolation('ERR_SCHEMA')
        if hub not in state.initial.populations or state.initial.map.hubs[hub].owner != owner:
            raise RuleViolation('ERR_NO_PERMISSION')
        existing = [j for j in gov.training.values() if j.owner == owner and j.hub == hub and j.track == track]
        if quantity == 0:
            return PreparedCommand(lambda: [gov.finish_training(j, completed=False) for j in existing])
        if 'R02' not in state.research[owner].known:
            raise RuleViolation('ERR_TECH_LOCKED')
        academies = sum(s.hub == hub and s.owner == owner and s.kind == 'academy' and gov.state.systems.availability(s) > 0
                        for s in state.systems.instances.values())
        population = state.initial.populations[hub]
        if not academies:
            raise RuleViolation('ERR_SITE_INCOMPATIBLE')
        if existing or quantity + sum(population.training_mU[j.id] for j in gov.training.values() if j.hub == hub) > academies * 5000:
            raise RuleViolation('ERR_CAPACITY_EXCEEDED')
        source_skill, _, cycles, supplies = TRACKS[track]
        if population.skills_mU.get(source_skill, 0) < quantity:
            raise RuleViolation('ERR_CAPACITY_EXCEEDED')
        remaining_capacity = floor(Q(population.skills_mU[source_skill] - quantity, ctx.clock.cycle_ticks) *
                                   state.labor_participation(owner, hub))
        committed = sum(workers.get(source_skill, 0) for entity, workers in state.frame.labor.items()
                        if state.frame.work_context.get(entity, (None, None, None))[1] == hub)
        committed += ceil(state.ai_deployment.held_at(hub,source_skill))
        if remaining_capacity < committed:
            raise RuleViolation('ERR_CAPACITY_EXCEEDED', 'residents already reserved for this tick')
        jid = 'training:' + digest(owner, order['id']).hex()[:24]
        job = Training(jid, str(order['id']), owner, hub, track, cycles * ctx.clock.cycle_ticks,
                       ceil(quantity * supplies), state.price('lab_supplies', hub))
        cash_lock = gov.training_cash(job)
        from .economic_ownership import stock_purchase_ceiling
        amount=stock_purchase_ceiling(state.initial.inventory,state.payments,cash_lock,state.native(owner,'treasury'),
            {Account(owner,hub,'available','lab_supplies'):job.supplies_mU},{'lab_supplies':job.price},kind='training_supplies')
        transaction = DeliveryPayment.prepare(state.initial.inventory, state.initial.cash,
            (Transfer(Account(owner, hub, 'available', 'lab_supplies'), gov.training_stock(job), job.supplies_mU),),
            (CashTransfer(state.native(owner, 'treasury'), cash_lock, amount),))
        def commit():
            transaction.commit()
            population.reserve_people({source_skill: quantity})
            population.training_mU[jid] = quantity
            gov.training[jid] = job
        return PreparedCommand(commit)
