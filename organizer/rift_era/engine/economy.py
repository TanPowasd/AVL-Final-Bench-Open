"""P1/P2/P3/P6/P7 operating economy over real initial-world entities.

Amounts are never synthesized to rescue a shortage. The fixed opening policy
funds working capital from the treasury, which is a transfer rather than income.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, replace
from fractions import Fraction

from .clock import Phase, ClockConfig
from .determinism import uniform
from .economy_state import OperatingWorld, TickEconomy, CycleEconomy, BASKET, WAGES
from .errors import InfrastructureFault, InvariantViolation
from .kernel import Kernel, System, TickContext
from .ledger import Account, CashAccount, Transfer, CashTransfer
from .numeric import Q, ceil, floor, clip, units, proportional, max_feasible
from .payments import FXRequest, Obligation
from .population import Demography, grow, support_delta, update_support
from .power import Supply, Load, dispatch, PlantMode
from .production import Production, commit_buffers, labor_sources
from .research import LabOutput
from .materials import Origin
from .provenance import Use
from . import industry_effects
from . import power_operations
from .compute_operations import DC_RECIPES
from .cargo import warehouse_load
from .local_economy import settle_residents
from .own_observation import owned_entities
from .economic_ownership import EconomicOwner, prepare_purchase, purchase_quote, record_income, income_account
from .life_replenishment import replenish_households,release_unused_grain,OUTDOOR_LOCK,MAIN_LOCK


class Economy:
    def __init__(self, state: OperatingWorld):
        self.s = state
        self.production = Production(state.initial.map.seed, state.initial.inventory, weights=state.cargo_weights,
            economic_owner=lambda line:EconomicOwner(line.owner,state.operating_account(line.id).name),
            output_titles=lambda line,pieces:state.facility_ownership.split_output(line.id,pieces)
                if line.id in state.facility_ownership.entities else pieces)
        self._input_title_cache={}

    def initialize_yields(self) -> None:
        state = self.s
        # Yield modifies the recipe coefficient, never the warehouse balance.
        for lid, line in state.initial.lines.items():
            policy = state.initial.line_policies[lid]
            if policy.fertility_mU is not None:
                line.recipe = replace(line.recipe, outputs={g: round(q * Q(policy.fertility_mU, 1000))
                                                            for g, q in line.recipe.outputs.items()})

    @property
    def inventory(self):
        return self.s.initial.inventory

    def _hub(self, entity: str) -> str:
        initial = self.s.initial
        if entity.startswith('weapon:'):
            return self.s.battles.power_context[entity][1]
        if entity.startswith('haul_labor:'):
            context = self.s.frame.work_context.get(entity)
            if context is not None:
                return context[1]
            return self.s.cargo.hauls[self.s.cargo.vehicle_jobs[entity.split(':', 1)[1]]].origin
        if entity.startswith('storage:'):
            return self.s.storage.stores[entity.split(':', 1)[1]].hub
        if entity.startswith('climate:'):
            return entity.split(':', 1)[1] + '-h0'
        if entity in self.s.work.jobs:
            return self.s.work.jobs[entity].hub
        if entity in self.s.frame.work_context:
            return self.s.frame.work_context[entity][1]
        if entity.startswith('sys:'):
            return self.s.systems.instances[entity.split(':', 1)[1]].hub
        if entity in self.s.validation_jobs:
            return self.s.validation_jobs[entity].line.hub
        if entity in initial.lines:
            return initial.lines[entity].hub
        if entity in initial.mines:
            return initial.mines[entity].hub
        if entity.startswith('municipal:'):
            return entity.split(':', 1)[1]
        if entity.startswith('big_science:'):
            return entity.split(':', 1)[1]
        if entity.startswith('ccs:'):
            return self._hub(entity.split(':', 1)[1])
        if entity.startswith(('pump:', 'ignition:')):
            return self._hub(entity.split(':', 1)[1])
        if entity.startswith('command:'):
            return f'{entity.split(":", 1)[1]}-h0'
        return initial.assets[entity].hub

    def _owner(self, entity: str) -> str:
        return self.s.operating_account(entity).owner

    def _reserve_material(self, ctx: TickContext, requests: dict[str, dict[str, int]]) -> None:
        pools = defaultdict(dict)
        for entity, goods in requests.items():
            for good, quantity in goods.items():
                pools[Account(self._owner(entity), self._hub(entity), 'available', good)][entity] = quantity
        transfers = []
        for source, demand in pools.items():
            allocated = proportional(self.inventory.balance(source), demand,
                key=(ctx.seed, 'subsistence', ctx.tick, source.owner, source.location, source.asset))
            for entity, quantity in allocated.items():
                bucket = f'locked_subsistence:{entity}'
                self.s.frame.material_accounts[entity] = bucket
                transfers.append(Transfer(source, Account(source.owner, source.location, bucket, source.asset), quantity))
        self.inventory.transfer_many(transfers)

    def _stock_account(self, entity: str, good: str) -> Account:
        return Account(self._owner(entity), self._hub(entity), self.s.frame.material_accounts.get(entity, 'available'), good)

    def _input_quote(self,entity,good,quantity,payer):
        """Own stock is not a sale to oneself. Mixed titles reserve a ceiling.

        Actual invoices always use the exact drawn titles. A pure own-credit
        or own-stock input needs no fictitious circulating purchase cash.
        """
        if not quantity:return 0
        book=self.inventory.materials
        prefix='generator_credit:' if entity in self.s.initial.plants else 'input_credit:'
        credit=Account(self._owner(entity),self._hub(entity),prefix+entity,good)
        buyer=self.s.operating_account(entity,payer.currency)
        key=(entity,good,buyer)
        cached=self._input_title_cache.get(key)
        if cached is None or cached[0]!=book._revision:
            previous=book.composition(credit)
            source=book.composition(self._stock_account(entity,good))
            credit_qty=sum(previous.values(),Q(0))
            own_credit=all(income_account(o,self._owner(entity),payer.currency)==buyer for o in previous)
            own_source=all(income_account(o,self._owner(entity),payer.currency)==buyer for o in source)
            cached=(book._revision,credit_qty,bool(previous),own_credit,bool(source),own_source)
            self._input_title_cache[key]=cached
        _,credit_qty,has_credit,own_credit,has_source,own_source=cached
        pure=own_credit and (own_source if quantity>credit_qty else True)
        if pure and (has_credit or quantity>credit_qty and has_source):return 0
        return purchase_quote(self.s.payments,payer,quantity*self.s.price(good,self._hub(entity)),f'input:{good}')

    def _worked(self, entity: str, amount_mU: int, ctx: TickContext) -> None:
        automated = self.s.ai_deployment.worked(entity,amount_mU,ctx)
        self.s.frame.worked[entity] = (automated if automated is not None else
            proportional(max(0, amount_mU), self.s.frame.labor.get(entity, {}),
                         key=(ctx.seed, 'actual_labor', ctx.tick, entity)))

    def _fund_companies(self, ctx: TickContext, required: dict[CashAccount, int], *, convert_foreign=True) -> None:
        fx=[]
        for account,amount in required.items():
            if not convert_foreign or not account.name.startswith('corp:'):continue
            missing=max(0,amount-self.s.initial.cash.balance(account).native_mU)
            foreign=CashAccount(account.owner,account.name,'RU')
            if missing and self.s.initial.cash.balance(foreign).native_mU:
                limit=ceil(missing*self.s.rates[account.owner])
                fx.append(FXRequest('corporate_income:'+account.owner+':'+account.name,foreign,account,
                    min(self.s.initial.cash.balance(foreign).native_mU,ceil(limit*(1+self.s.fx.fee))+1),
                    1/self.s.rates[account.owner],principal_limit_mU=limit))
        if fx:
            self.s.fx.execute(ctx.seed,ctx.tick,tuple(fx))
            self.s.corporate_finance._record_fx_costs(fx)
        needs = defaultdict(dict)
        for account, amount in required.items():
            if account.name.startswith('corp:'):
                missing = max(0, amount - self.s.initial.cash.balance(account).native_mU)
                needs[account.owner][account.name] = missing
        transfers = []
        for owner, demand in needs.items():
            treasury = self.s.native(owner, 'treasury')
            granted = proportional(self.s.initial.cash.balance(treasury).native_mU, demand,
                                  key=(ctx.seed, 'working_capital', ctx.tick, owner))
            for name, amount in granted.items():
                transfers.append(CashTransfer(treasury, self.s.native(owner, name), amount))
        self.s.initial.cash.transfer_many(transfers)

    def _reserve_cash(self, ctx: TickContext, requests: dict[str, tuple[CashAccount, int]], kind: str) -> dict[str, CashAccount]:
        grouped = defaultdict(dict)
        for entity, (account, amount) in requests.items():
            grouped[account][entity] = amount
        result, transfers = {}, []
        for account, demand in grouped.items():
            granted = proportional(self.s.initial.cash.balance(account).native_mU, demand,
                key=(ctx.seed, 'cash_reserve', ctx.tick, account.owner, account.name, account.currency, kind))
            for entity, amount in granted.items():
                reservation = CashAccount(account.owner, f'{kind}:{entity}', account.currency)
                if self.s.initial.cash.balance(reservation).native_mU:
                    raise InvariantViolation('unreleased previous-tick reservation')
                result[entity] = reservation
                transfers.append(CashTransfer(account, reservation, amount))
                self.s.frame.reservations.append((reservation, account))
        self.s.initial.cash.transfer_many(transfers)
        return result

    def _line(self, entity):
        if entity in self.s.validation_jobs:
            return self.s.validation_jobs[entity].line
        return self.s.initial.lines[entity]

    def _operation(self, entity):
        """Labor, energy, mode and load priority from one authoritative recipe."""
        if entity in self.s.validation_jobs:
            return 2000, 2000, None, 2
        line = self.s.initial.lines[entity]
        row = self.s.catalog.recipes[line.recipe.id]
        policy = self.s.initial.line_policies[entity]
        mode = self.s.catalog.modes[policy.manufacturing_mode] if policy.manufacturing_mode else None
        power = units(row['power'])
        if entity in self.s.heat_supported and line.recipe.id in industry_effects.HEAT_POWER:
            power = round(power * Q(4, 5))
        return units(row['workers']), power, mode, 2 if row['facility'] == '实验室' else 3

    def _labor_supply(self, hub):
        state = self.s
        pop = state.initial.populations[hub]
        owner = state.initial.map.hubs[hub].owner
        supply = {skill: state.ai_deployment.labor_supply(hub,skill,floor(Q(amount, 10) * state.labor_participation(owner, hub)))
                  for skill, amount in pop.skills_mU.items()}
        for entity, workers in state.frame.labor.items():
            if self._hub(entity) == hub:
                for skill, amount in workers.items():
                    supply[skill] -= amount
        if any(q < 0 for q in supply.values()):
            raise InvariantViolation('labor already reserved beyond physical population')
        return supply

    def _plan_labor(self, ctx, *, core):
        state, initial, f = self.s, self.s.initial, self.s.frame
        demands, batches, critical = {}, {}, set()
        validating = {job.lab_id for job in state.validation_jobs.values() if job.active}
        for lid, line in initial.lines.items():
            if line.recipe.id in DC_RECIPES:
                continue
            if line.recipe.id == '__idle__' or lid in state.paused_lines and lid not in initial.plants:
                continue
            row = state.catalog.recipes[line.recipe.id]
            if lid in initial.plants or row['facility'] in ('农场', '受控农场', '水厂'):
                critical.add(lid)
            if not industry_effects.demanded(state, line) and lid not in initial.plants:
                continue
            if lid in validating:
                continue
            research = state.research[line.owner]
            if 'research_points' in line.recipe.outputs and (
                    lid not in research.focus or research.projects[research.focus[lid]].theory_complete):
                continue
            workers, _, mode, _ = self._operation(lid)
            health = initial.assets[lid].availability * initial.assets[lid.rsplit('/line', 1)[0]].availability
            if lid in state.paused_lines:
                health = Q(0)
            f.availability[lid] = health
            batch = floor(Q(initial.line_policies[lid].load_mU, 10) * (mode['capacity'] if mode else 1) * health)
            labor = workers * Q(batch, 1000) * (mode['worker_factor'] if mode else 1)
            if lid in initial.plants:
                plant = initial.plants[lid]
                service = plant.mode == PlantMode.SCRAM and plant.timer <= 1
                if power_operations.warming(plant) or service:
                    labor = Q(workers, 10) if health > 0 else Q(0)
                elif plant.mode in (PlantMode.TRIPPED, PlantMode.MAINTENANCE, PlantMode.SCRAM) and plant.timer > 1:
                    labor = Q(0)
                else:
                    labor = Q(workers, 10) * health * plant.spec.maximum if plant.online else Q(0)
            batches[lid] = batch
            demands[lid] = {'engineer' if row['facility'] == '实验室' else 'general_worker': ceil(labor)}
        for entity, job in state.validation_jobs.items():
            if not job.active:
                continue
            available = initial.assets[job.lab_id].availability * initial.assets[job.lab_id.rsplit('/line', 1)[0]].availability
            f.availability[entity] = available
            batches[entity] = floor(100 * available)
            demands[entity] = {'engineer': ceil(200 * available)}
        for mid, mine in initial.mines.items():
            if mine.good == 'water':
                critical.add(mid)
            table = state.catalog.runtime['extraction'][mine.good]
            demands[mid] = {'general_worker': ceil(Q(units(table['labor']), 10) * initial.assets[mid].availability *
                                                   (Q(5, 4) if mine.level == 'G2' else 1))}
        for fid, facility in initial.facilities.items():
            if facility.kind == 'housing' and facility.commissioned:
                critical.add(fid)
                demands[fid] = {'general_worker': 50 if initial.assets[fid].health_mU else 0}
        for jid, job in state.work.jobs.items():
            _, _, labor, _ = job.due(state.work.whole)
            demands[jid] = {'general_worker': labor}
            f.work_context[jid] = (job.owner, job.hub, 'treasury')
        for entity, system, spec, available in state.systems.operations():
            if spec.rank <= 1:
                critical.add(entity)
            demands[entity] = {'general_worker': ceil(Q(spec.workers_mU_cycle, ctx.clock.cycle_ticks)) if available else 0}
        for entity, owner, hub, labor in state.cargo.labor_demands():
            demands[entity] = {'general_worker': labor}
            f.work_context[entity] = (owner, hub, 'treasury')
        for entity, owner, hub, labor in state.passengers.labor_demands():
            demands[entity] = {'general_worker': labor}
            f.work_context[entity] = (owner, hub, 'treasury')
        if not core:
            demands.update(state.compute.labor_demands())
            demands.update(state.ai_deployment.alignment_demands(ctx))
        if core:
            f.core_entities = critical
            demands = {e: d for e, d in demands.items() if e in critical}
        else:
            demands = {e: d for e, d in demands.items() if e not in f.core_entities}
        state.ai_deployment.plan_labor(ctx,demands,batches)
        by_hub = defaultdict(dict)
        for entity, demand in demands.items():
            by_hub[self._hub(entity)][entity] = demand
        for hub, requests in by_hub.items():
            allocation = labor_sources(ctx.seed, ctx.tick, hub, self._labor_supply(hub), requests)
            for entity, workers in allocation.items():
                # Dedicated engineer demand is considered before lower-skill
                # substitution. Only critical reservations survive the P1 fence.
                if core and entity not in critical:
                    continue
                f.labor[entity] = workers
                account = state.operating_account(entity)
                f.work_context[entity] = (account.owner, hub, account.name)
                state.industry_activity._row(entity)
                demand = sum(requests[entity].values())
                f.demand_labor[entity] = demand
                if entity in batches:
                    f.batches[entity] = floor(batches[entity] * min(Q(1), Q(sum(workers.values()), demand))) if demand else batches[entity]
                    f.batches[entity] = state.ai_deployment.planned_batch(entity,sum(workers.values()),demand,f.batches[entity])
        if core:
            f.core_entities = set(f.labor)

    def _reserve_operating_budgets(self, ctx, entities):
        state, initial, f = self.s, self.s.initial, self.s.frame
        required, wage_requests, input_requests = defaultdict(int), {}, {}
        for entity in entities:
            workers = f.labor[entity]
            account = state.operating_account(entity)
            wage = ceil(sum((q * WAGES[skill] * state.wage_indices[account.owner] for skill, q in workers.items()), Q(0)))
            wage_requests[entity] = (account, wage)
            required[account] += wage
            if entity in initial.lines or entity in state.validation_jobs:
                line = self._line(entity)
                need = f.essential_inputs[entity] if entity in initial.plants else {
                    g: Q(q * f.batches.get(entity, 0), 1000) for g, q in line.recipe.inputs.items()}
                reservation = CashAccount(account.owner, f'input:{entity}', account.currency)
                amount = sum(self._input_quote(entity,g,q,reservation) for g,q in need.items())
                input_requests[entity] = (account, amount)
                required[account] += amount
        self._fund_companies(ctx, required)
        f.wage_locks.update(self._reserve_cash(ctx, wage_requests, 'wage'))
        f.input_locks.update(self._reserve_cash(ctx, input_requests, 'input'))

    def reserve(self, state: OperatingWorld, ctx: TickContext) -> None:
        """P1a: current-tick life support, before player discretionary orders."""
        state.frame = TickEconomy(prices=dict(state.spot.prices))
        self._input_title_cache.clear()
        f, initial = state.frame, state.initial
        self._plan_labor(ctx, core=True)
        # Current garrisons draw their actual food/water first. No civilian count
        # includes their crew, and no military consumption is an export receipt.
        wage_requests = {}
        for fid, formation in state.formations.rows.items():
            owner = formation.owner
            crew = state.formations.crew(formation)
            location = formation.hub or formation.id
            f.military_context[fid] = (owner, location, crew)
            goods = {g: state.accrue_quantity(fid, g, Q(crew, 10) * rate)
                     for g, rate in (('food', Q(6, 100)), ('water', Q(4, 100)))}
            taken = {}
            for g, needed in goods.items():
                sources=[Account(owner,fid,'available',g)]
                from .nonland_navigation import base_online
                if base_online(state,owner,formation.hub,formation.domain):
                    sources.append(Account(owner,formation.hub,'available',g))
                taken[g]=0
                for source in sources:
                    qty=min(needed-taken[g],self.inventory.balance(source))
                    self.inventory.transfer_many((Transfer(source,Account(owner,location,f'locked_military:{fid}',g),qty,
                        destination_hub=state.movement.location(formation)),))
                    taken[g]+=qty
            f.military_payroll[fid]=formation.home_hub or f'{owner}-h0'
            f.military[fid] = taken
            f.military_need[fid] = goods
            wage_requests[fid] = (state.native(owner, 'treasury'), ceil(Q(crew * 15, 10)))
        f.military_wage_locks = self._reserve_cash(ctx, wage_requests, 'military_wage')


        essential = {}
        for lid in f.batches:
            line = self._line(lid)
            if lid in initial.plants:
                plant = initial.plants[lid]
                upper = min(Q(1), plant.output_fraction + plant.spec.ramp)
                if plant.mode == PlantMode.STARTING:
                    upper = Q(1, 10) if plant.timer else max(plant.spec.minimum, upper)
                if not plant.online or not sum(f.labor[lid].values()):
                    upper = Q(0)
                essential[lid] = {g: ceil(Q(q, 10) * upper) for g, q in line.recipe.inputs.items()}
                if power_operations.pump_expected(plant):
                    essential[lid]['water'] = max(essential[lid].get('water', 0), ceil(Q(line.recipe.inputs.get('water', 0), 10)))
                if plant.mode == PlantMode.SCRAM and plant.timer <= 1:
                    essential[lid].update(precision_parts=2000, pure_chemicals=1000)
            else:
                essential[lid] = line.required(f.batches.get(lid, 0))
        f.essential_inputs = essential
        self._reserve_material(ctx, essential)
        # Requests within the same priority band share a frozen cash pool.
        # Buying food first must not consume the entire water entitlement.
        for hub, population in initial.populations.items():
            if not population.total_mU:
                continue
            owner = initial.map.hubs[hub].owner
            available_cash = initial.cash.balance(state.native(owner, f'household:{hub}')).native_mU
            reservation = CashAccount(owner, f'household_purchase:{hub}', owner)
            basket = dict(BASKET)
            if 'A11' in state.research[owner].known:
                basket['gene_therapy'] = Q(1, 1000)
            wanted, quantities, needs = {}, {}, {}
            for good, rate in basket.items():
                need = state.accrue_quantity(hub, good, Q(population.total_mU, 10) * rate)
                needs[good] = need
                state.record_need(owner, hub, good, need)
                wanted[good] = min(need, self.inventory.balance(Account(owner, hub, 'available', good)))
            if wanted['food'] < Q(3, 4) * needs['food']:
                desired = floor((Q(3, 4) * needs['food'] - wanted['food']) * Q(27, 20))
                wanted['grain'] = min(desired, self.inventory.balance(Account(owner, hub, 'available', 'grain')))

            def cost(good, quantity):
                base = quantity * state.price(good, hub)
                return (purchase_quote(state.payments,reservation,base,f'civilian:{good}') +
                        state.governance.quote_tax(reservation, hub, base * state.tax[owner], f'consumption_tax:{good}'))

            bands = (('food', 'grain', 'water'), ('medicine', 'medical_supplies', 'gene_therapy'), ('consumer_goods',))
            for index, band in enumerate(bands):
                demand = {g: cost(g, wanted[g]) for g in band if g in wanted}
                grants = proportional(available_cash, demand, key=(ctx.seed, 'household_budget', ctx.tick, hub, index))
                for good, grant in grants.items():
                    amount = max_feasible(wanted[good], lambda q, g=good, budget=grant: cost(g, q) <= budget)
                    available_cash -= cost(good, amount)
                    quantities[good] = amount
                    self.inventory.transfer(Account(owner, hub, 'available', good),
                                            Account(owner, hub, 'locked_subsistence:civilian', good), amount)
            f.household[hub] = quantities
            f.household_need[hub] = dict(needs)
        household_requests = {}
        for hub, goods in f.household.items():
            owner = initial.map.hubs[hub].owner
            reservation = CashAccount(owner, f'household_purchase:{hub}', owner)
            amount = sum(purchase_quote(state.payments,reservation,q*state.price(g,hub),f'civilian:{g}') +
                         state.governance.quote_tax(reservation, hub, q * state.price(g, hub) * state.tax[owner], f'consumption_tax:{g}')
                         for g, q in goods.items())
            household_requests[hub] = (state.native(owner, f'household:{hub}'), amount)
        f.household_locks = self._reserve_cash(ctx, household_requests, 'household_purchase')


        self._reserve_operating_budgets(ctx, tuple(f.labor))
        self._reserve_power(state, ctx, core=True)

    def reserve_operations(self, state: OperatingWorld, ctx: TickContext) -> None:
        """P1c: ordinary plans use only resources left after accepted commands."""
        industry_effects.refresh(state, ctx)
        self._plan_labor(ctx, core=False)
        self._reserve_operating_budgets(ctx, tuple(e for e in state.frame.labor if e not in state.frame.core_entities))
        self._reserve_power(state, ctx, core=False)
        self._reserve_classified(state, ctx)

    def replenish_life(self,state,ctx):
        replenish_households(state,ctx)
        requests={}
        for entity,goods in state.frame.essential_inputs.items():
            if entity not in state.frame.core_entities:continue
            requests[entity]={g:max(0,q-self.inventory.balance(self._stock_account(entity,g))) for g,q in goods.items()}
        self._reserve_material(ctx,requests)
        extra={};required={}
        for entity in requests:
            lock=state.frame.input_locks[entity]
            if entity in state.initial.plants:
                inputs={g:Q(q) for g,q in state.frame.essential_inputs[entity].items()}
            else:
                inputs={g:Q(q*state.frame.batches[entity],1000) for g,q in self._line(entity).recipe.inputs.items()}
            need=sum(self._input_quote(entity,g,q,lock) for g,q in inputs.items())
            short=max(0,need-state.initial.cash.balance(lock).native_mU)
            payer=state.operating_account(entity)
            extra.setdefault(payer,{})[entity]=short
            required[payer]=required.get(payer,0)+short
        self._fund_companies(ctx,required)
        transfers=[]
        for payer,demands in extra.items():
            grants=proportional(state.initial.cash.balance(payer).native_mU,demands,
                key=(ctx.seed,'life_input_replenishment',ctx.tick,payer.owner,payer.name))
            transfers.extend(CashTransfer(payer,state.frame.input_locks[entity],amount) for entity,amount in grants.items())
        state.initial.cash.transfer_many(transfers)

    def _reserve_classified(self, state: OperatingWorld, ctx: TickContext) -> None:
        f = state.frame
        requests, required = {}, defaultdict(int)
        for entity in f.batches:
            physical = state.validation_jobs[entity].lab_id if entity in state.validation_jobs else entity
            owner = self._owner(entity)
            if state.research[owner].security.get(physical, 'standard') != 'classified':
                continue
            payer = state.operating_account(entity)
            budget = sum(state.initial.cash.balance(a).native_mU for a in (f.wage_locks[entity], f.input_locks[entity]))
            budget += Q(state.initial.cash.balance(f.power_locks[entity]).native_mU) / state.rates[owner]
            amount = ceil(Q(budget, 5))
            requests[entity] = (payer, amount)
            required[payer] += amount
        self._fund_companies(ctx, required)
        f.classified_locks = self._reserve_cash(ctx, requests, 'classified')
        unfunded = {e for e, (_, amount) in requests.items()
                    if state.initial.cash.balance(f.classified_locks[e]).native_mU < amount}
        # Security operations do not proceed on an unfunded surcharge promise.
        # Base cash remains escrowed and is refunded in P7; no free research.
        for entity in unfunded:
            f.batches[entity] = 0
            f.power_demands[entity] = 0
        f.power_loads = tuple(load for load in f.power_loads if load.id not in unfunded)

    def reserve_maintenance(self, state: OperatingWorld, ctx: TickContext) -> None:
        requests = state.maintenance.requests(state.price)
        self._fund_companies(ctx, state.maintenance.funding_requests(requests))
        limits = {state.native(p, 'treasury'): policy.maintenance_remaining_mU
                  for p, policy in state.governance.policies.items() if policy.maintenance_remaining_mU is not None}
        state.maintenance.reserve(requests, spending_limits=limits)

    def maintain(self, state: OperatingWorld, ctx: TickContext) -> None:
        for receipt in state.maintenance.settle():
            payer = receipt.payer
            policy = state.governance.policies[payer.owner]
            if payer.name == 'treasury' and policy.maintenance_remaining_mU is not None:
                policy.maintenance_remaining_mU -= receipt.paid_mU
            metric = state.cycles[payer.owner]
            metric.costs[payer.name] = metric.costs.get(payer.name, Q(0)) + receipt.paid_mU
            state.industry_activity.maintenance(receipt.asset_id,receipt.paid_mU)
            for supplier, amount in receipt.suppliers_paid:
                record_income(state,(supplier,),(amount,))
            for good, quantity in receipt.consumed_mU.items():
                state.credit_consumption(state.initial.assets[receipt.asset_id].owner, good, Q(quantity))
                state.macro.input(payer.owner, state.initial.assets[receipt.asset_id].hub, good, quantity)
        self.refresh_capacity(state, ctx)

    def refresh_capacity(self, state: OperatingWorld, ctx: TickContext) -> None:
        housing = dict.fromkeys(state.initial.housing_mU, 0)
        for fid, facility in state.initial.facilities.items():
            if facility.kind == 'housing' and facility.commissioned:
                housing[facility.hub] += floor(100000 * state.initial.assets[fid].availability)
        state.initial.housing_mU = housing
        for hub, warehouses in state.initial.warehouse_modules.items():
            state.initial.warehouse_mU[hub] = sum(floor((300000 + 500000 * count) * state.initial.assets[aid].availability)
                                                  for aid, count in warehouses.items())

    def environment(self, state: OperatingWorld, ctx: TickContext) -> None:
        from .weather import weather_step
        climate = state.climate
        climate.begin_cycle(ctx)
        for region in state.initial.map.regions.values():
            previous_wind = climate.raw_wind.get(region.id, state.wind_mU.get(region.id, region.wind_mean_mU))
            wind, cloud, sun = weather_step(ctx.seed, state.initial.map.attempt, ctx.tick, region,
                previous_wind, state.cloud_mU.get(region.id, region.cloud_mean_mU), state.global_emissions_mU)
            if climate.interventions:
                factor = climate.multiplier(region.id)
                if factor != 1:
                    climate.raw_wind[region.id] = wind  # V08 shifts realized wind/solar, not the AR state
                    wind, sun = round(wind * factor), round(sun * factor)
                else:
                    climate.raw_wind.pop(region.id, None)
            elif climate.raw_wind:
                climate.raw_wind.pop(region.id, None)
            state.wind_mU[region.id], state.cloud_mU[region.id], state.sun_mU[region.id] = wind, cloud, sun
        if ctx.tick % 10 == 0:
            for mid, deposit in state.deposits.items():
                if deposit.good == 'biomass':
                    account = Account(*state.resource_sources[mid], deposit.good)
                    stock = self.inventory.balance(account)
                    ratio = Q(stock, deposit.initial_mU)
                    rate = Q(4 if ratio < Q(2, 5) else 8, 100)
                    growth = floor(rate * stock * (1 - ratio))
                    self.inventory.transform({}, {account: growth}, reason='biomass_regeneration',
                                             origins={account: Origin('nature', state.resource_sites[mid].hub)})
                    deposit.remaining_mU = stock + growth
        state.hydrology.advance(ctx)
        for mid, deposit in state.deposits.items():
            deposit.remaining_mU = self.inventory.balance(Account(*state.resource_sources[mid], deposit.good))
        for lid, line in state.initial.lines.items():
            policy = state.initial.line_policies[lid]
            if policy.fertility_mU is None:
                continue
            pollution = Q(state.pollution[line.hub], state.pollution[line.hub] + 100000)
            # Explicit rainfall-to-crop mapping for this generator version.
            weather, gene = industry_effects.gene_weather(state, line, state.rain[line.hub])
            factor = Q(policy.fertility_mU, 1000) * weather * gene * (1 - Q(1, 5) * pollution)
            original = state.catalog.recipes[line.recipe.id]['outputs']
            line.recipe = replace(line.recipe, outputs={g: round(units(q) * factor) for g, q in original.items()})

    def _material_events(self, owner: str, good: str, bucket: str) -> list:
        """This tick's material events for (owner, good, source_bucket), in event order.

        The event list only grows within a tick and is cleared by begin_tick, so
        an index extended from the last scanned position equals a full scan.
        """
        book = self.inventory.materials
        events = book.events
        cache = getattr(self, '_event_index', None)
        if cache is None or cache[0] != book.tick or cache[1] is not events or cache[2] > len(events):
            cache = self._event_index = [book.tick, events, 0, defaultdict(list)]
        index = cache[3]
        for e in events[cache[2]:]:
            index[e.owner, e.good, e.source_bucket].append(e)
        cache[2] = len(events)
        return index.get((owner, good, bucket), ())

    def _input_payment(self, entity: str, good: str, quantity_mU: Fraction) -> None:
        self.s.macro.input(self._owner(entity), self._hub(entity), good, quantity_mU)
        account = self.s.frame.input_locks.get(entity)
        if account is None or quantity_mU == 0:
            return
        owner = account.owner
        bucket=('generator_credit:' if entity in self.s.initial.plants else 'input_credit:')+entity
        seen=self.s.frame.procurement_seen
        events=tuple(e for e in self._material_events(owner,good,bucket) if e.id not in seen)
        if sum((e.quantity_mU for e in events),Q(0))!=quantity_mU:
            raise InvariantViolation('input payment does not match actual material draw')
        payment,recipients=prepare_purchase(self.s.payments,account,events,{good:self.s.price(good,self._hub(entity))},
            kind=f'input:{good}',key=(self.inventory.materials.tick,'input_payment',entity,good),
            buyer=self.s.operating_account(entity,account.currency))
        payment.commit()
        self.s.frame.procurement_seen.update(e.id for e in events)
        paid=sum(payment.amounts)
        self.s.frame.operating_paid_mU[entity] = self.s.frame.operating_paid_mU.get(entity, Q(0)) + paid
        metric = self.s.cycles[owner]
        buyer_name = self.s.operating_account(entity).name
        metric.costs[buyer_name] = metric.costs.get(buyer_name, Q(0)) + paid
        record_income(self.s,recipients,payment.amounts)

    def _consume_fractional(self, entity: str, good: str, exact_mU: Fraction, *, use: Use = Use.INDUSTRY) -> None:
        key = (entity, good)
        credit = self.s.input_credits.get(key, Q(0))
        take = max(0, ceil(exact_mU - credit))
        source = self._stock_account(entity, good)
        material_plan = None
        if self.inventory.materials:
            draft = self.inventory.materials.draft()
            credit_account = Account(self._owner(entity), self._hub(entity), f'generator_credit:{entity}', good)
            draft.add(credit_account, draft.take(source, Q(take)), virtual=True)
            draft.consume(credit_account, exact_mU, use=use, reason='generator_fuel_and_cooling' if use == Use.INDUSTRY else 'reactor_scram_service')
            material_plan = draft.finish()
        self.inventory.transform({source: take}, {}, reason='generator_fuel_and_cooling', material_plan=material_plan)
        self.s.input_credits[key] = credit + take - exact_mU
        self._input_payment(entity, good, exact_mU)
        self.s.credit_consumption(self._owner(entity), good, exact_mU)
        if use==Use.INDUSTRY:self.s.industry_activity.generator_input(entity,good,exact_mU)

    def _reserve_power(self, state: OperatingWorld, ctx: TickContext, *, core: bool) -> None:
        f, initial = state.frame, state.initial
        demands = {}
        for entity, batch in f.batches.items():
            if (entity in f.core_entities) != core:
                continue
            if entity in initial.plants:
                continue
            line = self._line(entity)
            _, power, mode, rank = self._operation(entity)
            demand = ceil(power * Q(batch, 1000) * (mode['power_factor'] if mode else 1))
            demands[entity] = (line.hub, rank, demand)
            if industry_effects.ccs_installed(state, entity) and batch:
                demands[f'ccs:{entity}'] = (line.hub, 3, 150)
        for mid, mine in initial.mines.items():
            if (mid in f.core_entities) != core:
                continue
            actual = sum(f.labor.get(mid, {}).values())
            ratio = min(Q(1), Q(actual, max(1, f.demand_labor[mid])))
            demands[mid] = (mine.hub, 1 if mine.good == 'water' else 3,
                            ceil(Q(units(state.catalog.runtime['extraction'][mine.good]['power']), 10) * ratio *
                                 (Q(8, 5) if mine.level == 'G2' else 1)))
        if core:
            for lid, plant in initial.plants.items():
                hub = initial.lines[lid].hub
                if power_operations.pump_expected(plant):
                    demands[f'pump:{lid}'] = (hub, 0, 50)
                if plant.spec.ignition_mU_tick and power_operations.warming(plant):
                    demands[f'ignition:{lid}'] = (hub, 0, plant.spec.ignition_mU_tick)
        if not core:
            demands.update(state.compute.power_demands())
            demands.update(state.cargo.power_demands())
            demands.update(state.utilities.power_demands())
            demands.update(state.storage.charge_demands())
            demands.update(state.battles.power_demands)
            demands.update(state.climate.power_demands())
            for jid, job in state.work.jobs.items():
                _, _, labor, power = job.due(state.work.whole)
                demands[jid] = (job.hub, 3, power if sum(f.labor.get(jid, {}).values()) >= labor else 0)
        for hub, population in initial.populations.items():
            if core and population.total_mU:
                demand = state.accrue_quantity(hub, 'electricity', Q(population.total_mU, 500))
                demands[f'municipal:{hub}'] = (hub, 1, demand)
                state.record_need(initial.map.hubs[hub].owner, hub, 'electricity', demand)
        for owner in initial.known if core else ():
            demands[f'command:{owner}'] = (f'{owner}-h0', 0, 50)
        for entity, system, spec, available in state.systems.operations():
            if (entity in f.core_entities) != core:
                continue
            staffed = sum(f.labor.get(entity, {}).values()) >= ceil(Q(spec.workers_mU_cycle, ctx.clock.cycle_ticks))
            demands[entity] = (system.hub, spec.rank, ceil(Q(spec.power_mU_cycle, ctx.clock.cycle_ticks)) if available and staffed else 0)
        f.power_demands.update({entity: d for entity, (_, _, d) in demands.items()})

        # Existing fractional fees are real obligations. Keep enough actual
        # currency behind them before admitting any new electricity purchase.
        user_requests, subsidy_requests, required_ru = {}, {}, defaultdict(int)
        unpaid = state.payments.unpaid_by_source()  # residuals are not touched in this loop
        for entity, (_, band, demand) in demands.items():
            owner = self._owner(entity)
            limit = (state.storage.stores[entity.split(':', 1)[1]].quote_ru if entity.startswith('storage:') else
                     state.compute.bid_ru(entity) if entity in state.compute.frame_centers else 50 * state.rates[owner])
            limit_native = min(Q(50), limit / state.rates[owner])
            cap = min(Q(state.power_price_caps[owner], 1000), limit_native)
            f.power_contract_caps[entity] = state.power_price_caps[owner]
            user_source = state.operating_account(entity, 'RU')
            user_lock = CashAccount(owner, f'power:{entity}', 'RU')
            need = ceil(demand * cap * state.rates[owner] + unpaid.get(user_lock, Q(0)))
            user_requests[entity] = (user_source, need)
            required_ru[user_source] += need
            subsidy_source = CashAccount(owner, 'reserves', 'RU')
            subsidy_lock = CashAccount(owner, f'power_subsidy:{entity}', 'RU')
            subsidy = ceil(demand * (limit_native - cap) * state.rates[owner] + unpaid.get(subsidy_lock, Q(0)))
            if subsidy:
                subsidy_requests[entity] = (subsidy_source, subsidy)
                required_ru[subsidy_source] += subsidy
        native_needs, requests = defaultdict(int), []
        for index, (account, required) in enumerate(sorted(required_ru.items())):
            missing = max(0, required - initial.cash.balance(account).native_mU)
            native = state.native(account.owner, 'treasury' if account.name == 'reserves' else account.name)
            offered = ceil(Q(missing) / state.rates[account.owner] * (1 + state.fx.fee))
            native_needs[native] += offered
            if offered:
                requests.append(FXRequest(f'power-fx-{int(core)}-{index}', native, account, offered, state.rates[account.owner]))
        # These native amounts are already earmarked to BUY the missing RU.
        # Selling the same company's existing RU to fund this request would
        # merely buy it back, charge two fees and still not cover the deficit.
        self._fund_companies(ctx,native_needs,convert_foreign=False)
        state.fx.execute(ctx.seed, ctx.tick, tuple(requests))
        state.corporate_finance._record_fx_costs(requests)
        f.power_locks.update(self._reserve_cash(ctx, user_requests, 'power'))
        f.power_subsidy_locks.update(self._reserve_cash(ctx, subsidy_requests, 'power_subsidy'))
        loads = []
        unpaid = state.payments.unpaid_by_source()  # refreshed after FX and reservations
        for entity, (hub, band, demand) in demands.items():
            if not demand:
                continue
            owner = self._owner(entity)
            user = max(Q(0), initial.cash.balance(f.power_locks[entity]).native_mU - unpaid.get(f.power_locks[entity], Q(0)))
            subsidy_lock = f.power_subsidy_locks.get(entity)
            subsidy = max(Q(0), initial.cash.balance(subsidy_lock).native_mU - unpaid.get(subsidy_lock, Q(0))) if subsidy_lock else Q(0)
            rank = state.power_priorities[owner].index(f'B{band}') + 1 if 1 <= band <= 4 else band
            limit = (state.storage.stores[entity.split(':', 1)[1]].quote_ru if entity.startswith('storage:') else
                     state.compute.bid_ru(entity) if entity in state.compute.frame_centers else 50 * state.rates[owner])
            loads.append(Load(entity, hub, owner, demand, rank, min(50 * state.rates[owner], limit), user + subsidy,
                              f'B{band}', user, Q(state.power_price_caps[owner], 1000) * state.rates[owner]))
        f.power_loads += tuple(loads)

    def power(self, state: OperatingWorld, ctx: TickContext) -> None:
        f, initial = state.frame, state.initial
        loads = tuple(replace(load, rank=state.power_priorities[load.owner].index(load.band) + 1)
                      if load.band in ('B1', 'B2', 'B3', 'B4') else load for load in f.power_loads)
        loads += state.city_economy.power_loads()
        supplies, plant_available, net_factors, deferred_ignition = [], {}, {}, {}
        for lid, plant in initial.plants.items():
            line = initial.lines[lid]
            row = state.catalog.recipes[line.recipe.id]
            actual_labor = sum(f.labor.get(lid, {}).values())
            availability = initial.assets[lid].availability * initial.assets[lid.rsplit('/line', 1)[0]].availability
            region = initial.map.hubs[line.hub].region
            if line.recipe.id == 'P113':
                availability *= Q(state.wind_mU[region], 1000)
            elif line.recipe.id == 'P114':
                availability *= Q(state.sun_mU[region], 1000)
            elif line.recipe.id == 'P072':
                availability *= Q(state.wind_mU[region] + state.sun_mU[region], 2000)
            elif line.recipe.id == 'P112':
                availability *= state.hydrology.hydro_multiplier(line.hub)
            nominal_staff = Q(units(row['workers']), 10)
            availability = min(availability, Q(actual_labor) / nominal_staff) if nominal_staff else availability
            upper_request = min(Q(1), plant.output_fraction + plant.spec.ramp)
            if plant.mode == PlantMode.STARTING and plant.timer == 0:
                upper_request = max(plant.spec.minimum, upper_request)
            required = {g: Q(q, 10) * upper_request for g, q in line.recipe.inputs.items()}
            material_ratio = min((Q(self.inventory.balance(self._stock_account(lid, g)), 1) / amount
                                  for g, amount in required.items() if amount), default=Q(1))
            locked_cash = initial.cash.balance(f.input_locks[lid]).native_mU
            input_cost = sum((q * state.price(g, line.hub) for g, q in required.items()), Q(0))
            cash_ratio = min(Q(1), Q(locked_cash) / input_cost) if input_cost else Q(1)
            supply_ratio = min(Q(1), material_ratio, cash_ratio)
            if not line.recipe.inputs:
                supply_ratio = plant.spec.maximum
            debt = max(initial.assets[lid].maintenance_debt, initial.assets[lid.rsplit('/line', 1)[0]].maintenance_debt)
            power_operations.calendar(plant, debt, ctx, outage_multiplier=state.ai_deployment.grid_multiplier(line.owner,line.hub))
            scram_paid = False
            if plant.mode == PlantMode.SCRAM and plant.timer <= 1:
                parts = {'precision_parts': 2000, 'pure_chemicals': 1000}
                cost = sum(self._input_quote(lid,g,q,f.input_locks[lid])
                           for g, q in parts.items())
                scram_paid = (availability > 0 and actual_labor >= nominal_staff and cost <= locked_cash and
                              all(self.inventory.balance(self._stock_account(lid, g)) >= q for g, q in parts.items()))
                if scram_paid:
                    for good, quantity in parts.items():
                        self._consume_fractional(lid, good, Q(quantity), use=Use.REPAIR)
                    locked_cash = initial.cash.balance(f.input_locks[lid]).native_mU
            preheat = power_operations.warming(plant)
            warm_inputs = power_operations.preheat_inputs(plant, line.recipe.inputs)
            preheat_ready = False
            if preheat:
                preheat_ready = (availability > 0 and actual_labor >= nominal_staff and
                    all(ceil(max(Q(0), q - state.input_credits.get((lid, g), Q(0)))) <= self.inventory.balance(self._stock_account(lid, g))
                        for g, q in warm_inputs.items()) and
                    sum(self._input_quote(lid,g,q,f.input_locks[lid])
                        for g, q in warm_inputs.items()) <= locked_cash)
                supply_ratio = Q(int(preheat_ready))
            water_ratio = (min(Q(1), Q(self.inventory.balance(self._stock_account(lid, 'water'))) /
                           max(Q(1), Q(line.recipe.inputs.get('water', 0), 10))) if plant.spec.nuclear else supply_ratio)
            if preheat and plant.spec.ignition_mU_tick:
                deferred_ignition[lid] = (availability, preheat_ready)
                bounds = (Q(0), Q(0))
            else:
                bounds = plant.prepare(availability, supply_ratio, water_ratio, scram_materials_paid=scram_paid)
            if preheat and preheat_ready and not plant.spec.ignition_mU_tick and plant.mode == PlantMode.STARTING:
                for good, amount in warm_inputs.items():
                    self._consume_fractional(lid, good, amount)
                self._worked(lid, actual_labor, ctx)
            elif scram_paid:
                self._worked(lid, actual_labor, ctx)
            else:
                self._worked(lid, 0, ctx)
            low, high = (round(Q(plant.spec.nameplate_mU, 10) * x) for x in bounds)
            high = max_feasible(high, lambda output:
                all(max(0, ceil(Q(coefficient * output, plant.spec.nameplate_mU) - state.input_credits.get((lid, g), Q(0)))) <= self.inventory.balance(self._stock_account(lid, g))
                    for g, coefficient in line.recipe.inputs.items()) and
                sum(self._input_quote(lid,g,Q(coefficient*output,plant.spec.nameplate_mU),f.input_locks[lid])
                    for g, coefficient in line.recipe.inputs.items()) <= locked_cash)
            low = min(low, high)
            f.plant_bounds[lid] = (Q(low * 10, plant.spec.nameplate_mU), Q(high * 10, plant.spec.nameplate_mU))
            plant_available[lid] = (low, high)
            fuel_cost = sum((Q(q) * state.price(g, line.hub) for g, q in line.recipe.inputs.items()), Q(0))
            net = Q(85, 100) if industry_effects.ccs_installed(state, lid) else Q(1)
            net_factors[lid] = net
            operating_margin = Q(1, 20) if line.recipe.id in ('P112', 'P113', 'P114', 'P072') else Q(1, 5)
            marginal = (fuel_cost / plant.spec.nameplate_mU + operating_margin) * state.rates[line.owner] / net
            if low:
                supplies.append(Supply(f'{lid}:minimum', line.hub, line.owner, floor(low * net), Q(0)))
            if high > low:
                supplies.append(Supply(f'{lid}:dispatch', line.hub, line.owner, floor(high * net) - floor(low * net), marginal))
        loads = tuple(load for load in loads if not load.id.startswith(('pump:', 'ignition:')) or
            load.id.startswith('pump:') and initial.plants[load.id.split(':', 1)[1]].mode in (PlantMode.ON, PlantMode.STARTING) or
            load.id.startswith('ignition:') and load.id.split(':', 1)[1] in deferred_ignition)
        f.power_loads = loads
        state.ai_deployment.record_grid_loads(loads)
        supplies.extend(state.storage.supplies())
        supplies.extend(state.city_economy.power.supplies(ctx))
        for hub, grid in state.grids.items():
            capacity = sum(s.quantity_mU for s in supplies if s.hub == hub)
            critical = sum(l.demand_mU for l in loads if l.hub == hub and l.band in ('B0', 'B1'))
            blackout = grid.update(capacity, critical)
            if hub in state.local_cycles and grid.recovering:
                state.local_cycles[hub].blackout_ticks += 1
            if blackout:
                for lid, plant in initial.plants.items():
                    if initial.lines[lid].hub == hub and plant.mode == PlantMode.ON:
                        plant.mode, plant.timer = PlantMode.TRIPPED, 1
                        plant_available[lid] = (0, 0)
                        f.plant_bounds[lid] = (Q(0), Q(0))
                supplies = [s for s in supplies if s.hub != hub or s.id.startswith('storage:')]
        result = dispatch(ctx.seed, ctx.tick, tuple(supplies), loads, state.utilities.dispatch_edges(ctx))
        city_payees=state.city_economy.power.payees()
        payees={s.id:city_payees[s.id] if s.id in city_payees else
                state.operating_account(s.id.rsplit(':',1)[0],'RU') for s in supplies}
        state.city_economy.record_power(ctx,result,payees)
        state.city_economy.power.complete(ctx,result)
        f.power_receipts = result.receipts
        f.power_received = dict(result.received)
        for lid, (availability, ready) in deferred_ignition.items():
            plant = initial.plants[lid]
            paid = f.power_received.get(f'ignition:{lid}', 0) >= plant.spec.ignition_mU_tick
            plant.prepare(availability, Q(int(ready)), Q(1), start_power_paid=paid)
            if ready and paid:
                self._worked(lid, sum(f.labor.get(lid, {}).values()), ctx)
        allocated, bills, received_by_hub, cost_by_hub = defaultdict(int), defaultdict(lambda: defaultdict(Fraction)), defaultdict(int), defaultdict(Fraction)
        for receipt in result.receipts:
            lid = receipt.source.rsplit(':', 1)[0]
            if receipt.source in city_payees:
                lid=receipt.source
            allocated[lid] += receipt.sent_mU
            if receipt.load in state.city_economy.frame_power:
                continue
            bills[receipt.load][(payees[receipt.source], 'electricity')] += receipt.source_payment_ru_mU
            for operator, amount in receipt.tolls_ru_mU:
                bills[receipt.load][(CashAccount(operator, 'corp:energy', 'RU'), 'grid_toll')] += amount
            hub = self._hub(receipt.load)
            received_by_hub[hub] += receipt.received_mU
            state.macro.electricity_quantity(self._owner(receipt.load), payees[receipt.source].owner, receipt.received_mU,
                                             intermediate=not receipt.load.startswith('municipal:'))
        load_map = {load.id: load for load in loads}
        for entity, recipients in sorted(bills.items()):
            load = load_map[entity]
            total = sum(recipients.values(), Q(0))
            user_part = min(total, load.user_budget_ru_mU, load.user_cap_ru * result.received[entity])
            subsidy_part = total - user_part
            obligations, roles = [], []
            for (recipient, kind), exact in sorted(recipients.items()):
                if not total:
                    continue
                if user_part:
                    obligations.append(Obligation(f.power_locks[entity], recipient, exact * user_part / total, kind))
                    roles.append(False)
                if subsidy_part:
                    obligations.append(Obligation(f.power_subsidy_locks[entity], recipient, exact * subsidy_part / total, kind))
                    roles.append(True)
            payment = state.payments.prepare_invoice(tuple(obligations), kind='power',
                key=(ctx.seed, 'power_invoice', ctx.tick, entity))
            payment.commit()
            owner = self._owner(entity)
            paid_total = sum(payment.amounts)
            f.power_paid_ru_mU[entity] = paid_total
            cost_by_hub[self._hub(entity)] += paid_total
            state.cycles[owner].power_cost_mU += paid_total
            for obligation, amount, subsidy in zip(obligations, payment.amounts, roles):
                buyer = 'treasury' if subsidy else state.operating_account(entity).name
                metric = state.cycles[owner]
                metric.costs[buyer] = metric.costs.get(buyer, Q(0)) + Q(amount) / state.rates[owner]
                if not subsidy:
                    f.operating_paid_mU[entity] = f.operating_paid_mU.get(entity, Q(0)) + Q(amount) / state.rates[owner]
                seller = obligation.target
                record_income(state,(seller,),(amount,))
                state.macro.electricity(owner, seller.owner, amount, intermediate=not entity.startswith('municipal:'))
        for hub, quantity in received_by_hub.items():
            if quantity:
                state.grid_prices_ru[hub] = cost_by_hub[hub] / quantity
        for lid, plant in initial.plants.items():
            physical = max(plant_available[lid][0], ceil(Q(allocated[lid]) / net_factors[lid]))
            f.generation[lid] = physical
            plant.commit(Q(physical * 10, plant.spec.nameplate_mU), f.plant_bounds[lid])
            if plant.spec.nuclear:
                pump = f'pump:{lid}'
                plant.pump_previous = min(Q(1), Q(f.power_received.get(pump, 0), 50)) if any(load.id == pump for load in loads) else None
            line = initial.lines[lid]
            if physical:
                labor_needed = ceil(Q(units(state.catalog.recipes[line.recipe.id]['workers']) * physical, plant.spec.nameplate_mU))
                self._worked(lid, labor_needed, ctx)
            for good, coefficient in line.recipe.inputs.items():
                self._consume_fractional(lid, good, Q(coefficient * physical, plant.spec.nameplate_mU))
            state.cycles[line.owner].generation_mU += allocated[lid]
            emission = state.catalog.runtime['environment']['emissions'].get(line.recipe.id, 0)
            if industry_effects.ccs_running(state, lid):
                emission *= Q(1, 5)
            state.record_emissions(line.owner, line.hub, round(Q(physical * 10, plant.spec.nameplate_mU) * units(emission) / 10))
        for load in loads:
            if load.owner not in state.cycles:
                continue
            metric = state.cycles[load.owner]
            metric.power_received_mU += result.received[load.id]
            metric.power_needed_mU += load.demand_mU
            if load.band == 'B1' and load.hub in state.local_cycles:
                local = state.local_cycles[load.hub]
                local.b1_needed_mU += load.demand_mU
                local.b1_received_mU += result.received[load.id]
            if load.id.startswith('municipal:'):
                state.credit_consumption(load.owner, 'electricity', Q(result.received[load.id]))
                state.record_civilian(load.owner, load.hub, 'electricity', result.received[load.id])
        state.last_generation_mU = dict(f.generation)
        state.last_generation_kind = {lid: initial.lines[lid].recipe.id for lid in f.generation}
        state.systems.update_power(ctx)
        state.storage.settle_dispatch(ctx, allocated, loads)
        state.macro.record_grid()
        state.compute.receive(ctx)
        state.industry_activity.compute_frame(ctx)
        state.ai.consume_compute(ctx)
        state.ai_deployment.receive(ctx)

    def production_phase(self, state: OperatingWorld, ctx: TickContext) -> None:
        f, initial = state.frame, state.initial
        lines, caps = [], {}
        for lid, maximum in f.batches.items():
            if lid in initial.plants:
                continue
            line = self._line(lid)
            physical = state.validation_jobs[lid].lab_id if lid in state.validation_jobs else lid
            current = initial.assets[physical].availability * initial.assets[physical.rsplit('/line', 1)[0]].availability
            planned = f.availability.get(lid, Q(0))
            maximum = floor(maximum * min(Q(1), current / planned)) if planned else 0
            demand = f.power_demands.get(lid, 0)
            power_ratio = min(Q(1), Q(f.power_received.get(lid, 0), demand)) if demand else Q(1)
            maximum = floor(maximum * power_ratio)
            cash = initial.cash.balance(f.input_locks[lid]).native_mU
            caps[lid] = max_feasible(maximum, lambda x, line=line, cash=cash:
                sum(self._input_quote(line.id,g,Q(a*x,1000),f.input_locks[line.id]) for g,a in line.recipe.inputs.items()) <= cash)
            lines.append(line)
        for mid, mine in initial.mines.items():
            deposit = state.deposits[state.mine_deposit[mid]]
            actual = sum(f.labor.get(mid, {}).values())
            labor_ratio = min(Q(1), Q(actual, max(1, f.demand_labor[mid])))
            demand = f.power_demands.get(mid, 0)
            power_ratio = min(Q(1), Q(f.power_received.get(mid, 0), demand)) if demand else Q(1)
            ratio = min(labor_ratio, power_ratio) * initial.assets[mid].availability
            source = Account(*state.extraction_sources[mid], mine.good)
            efficiency = Q(1)
            if not deposit.renewable:
                remaining = self.inventory.balance(source)
                rho = Q(remaining, max(1, deposit.initial_mU))
                efficiency = Q(1) if rho >= Q(3, 10) else Q(1, 4) + Q(3, 4) * rho / Q(3, 10)
                ratio *= efficiency
            f.mining_efficiency[mid] = efficiency
            caps[mid] = floor(100 * ratio)
            lines.append(state.extraction_lines[mid])
        state.ai_deployment.apply_services(ctx)
        state.ai_deployment.cap_batches(caps)
        state.ai.constrain_data_lines(caps, lines, ctx)
        occupied = state.warehouse_load()
        capacity = {hub: max(0, floor(cap - occupied[hub])) for hub, cap in initial.warehouse_mU.items()}
        # One shared warehouse allocation covers both mines and manufacturers.
        results = self.production.advance(ctx.tick, lines, caps, free_capacity_mU=capacity,
            input_buckets=f.material_accounts, input_sources=state.extraction_sources)
        research_outputs = defaultdict(dict)
        research_networks = {p: state.systems.communication_components(p) for p in state.research}
        for result in results:
            if result.line_id in initial.mines:
                mid = result.line_id
                mine, deposit = initial.mines[mid], state.deposits[state.mine_deposit[mid]]
                deposit.remaining_mU = self.inventory.balance(Account(*state.extraction_sources[mid], mine.good))
                amount = result.released_mU.get(mine.good, 0)
                if mine.good == 'water':
                    state.water_used_mU[mine.hub] += amount
                metric = state.cycles[mine.owner]
                metric.extracted[mine.good] = metric.extracted.get(mine.good, 0) + amount
                if state.national_power is not None:
                    state.national_power.record_extraction(mine.hub, mine.good, amount)
                state.macro.output(mine.owner, mine.hub, result.released_mU)
                table = state.catalog.runtime['extraction'][mine.good]
                labor_needed = ceil(units(table['labor']) * (Q(5, 4) if mine.level == 'G2' else 1) *
                                    Q(result.batch_mU, 1000) / f.mining_efficiency[mid])
                self._worked(mid, labor_needed, ctx)
                power_used=ceil(Q(units(table['power'])*result.batch_mU,1000)/f.mining_efficiency[mid]*
                    (Q(8,5) if mine.level=='G2' else 1))
                state.industry_activity.use_power(mid,min(f.power_received.get(mid,0),power_used))
                continue
            line = self._line(result.line_id)
            state.industry_activity.production(line,result)
            state.ai.production(line, result)
            metric = state.cycles[line.owner]
            metric.line_batches[line.id] = metric.line_batches.get(line.id, 0) + result.batch_mU
            state.macro.output(line.owner, line.hub, result.released_mU)
            workers, required_power, mode, _ = self._operation(line.id)
            labor_needed = ceil(workers * Q(result.batch_mU, 1000) *
                                (mode['worker_factor'] if mode else 1))
            self._worked(line.id, labor_needed, ctx)
            power_used=ceil(required_power*Q(result.batch_mU,1000)*(mode['power_factor'] if mode else 1))
            state.industry_activity.use_power(line.id,min(f.power_received.get(line.id,0),power_used))
            if result.batch_mU and industry_effects.ccs_running(state,line.id):
                state.industry_activity.use_power('ccs:'+line.id,min(150,f.power_received.get('ccs:'+line.id,0)))
            for good, quantity in result.consumed_nmU.items():
                self._input_payment(line.id, good, Q(quantity, 1000))
                state.credit_consumption(line.owner, good, Q(quantity, 1000))
            if 'research_points' in result.released_mU:
                amount = result.released_mU['research_points']
                target = state.research[line.owner].focus.get(line.id)
                if (target and state.catalog.technologies[target]['tier'] >= 5 and line.recipe.id in ('P077', 'P079')
                        and 'quantum_module' in state.line_options.get(line.id, {}).get('addons', ())):
                    amount = round(Q(3, 2) * amount)
                research_outputs[line.owner][line.id] = LabOutput(line.id, line.hub, amount,
                    autonomous=line.recipe.id == 'P102', connected=line.hub in research_networks[line.owner],
                    network_id=research_networks[line.owner].get(line.hub))
            if line.id in state.validation_jobs:
                job = state.validation_jobs[line.id]
                research = state.research[line.owner]
                project = research.projects[job.tech_id]
                fully_supplied = result.batch_mU == 100
                if state.catalog.technologies[job.tech_id]['tier'] == 6:
                    fully_supplied &= state.systems.online(line.hub, 'big_science', owner=line.owner)
                if fully_supplied or not job.interrupted:
                    project.validate_tick(job.required_ticks, fully_supplied=fully_supplied,
                                          standard_records='R01' in research.known)
                job.interrupted = not fully_supplied
                if project.verified:
                    state.pending_knowledge[line.owner].add(job.tech_id)
                    research.focus.pop(job.lab_id, None)
                    job.active = False
                    initial.assets[job.lab_id].version += 1
            emission = state.catalog.runtime['environment']['emissions'].get(line.recipe.id, 0)
            if industry_effects.ccs_running(state, line.id):
                emission *= Q(1, 5)
            state.record_emissions(line.owner, line.hub, round(emission * result.batch_mU))
        state.ai_deployment.research(research_outputs,ctx.clock.cycle_ticks)
        state.ai_deployment.align_engineers(ctx)
        research_outputs=state.contracts.joint_ventures.route_research(research_outputs,ctx)
        for owner, outputs in research_outputs.items():
            state.research[owner].advance(outputs, open_allowed=not state.governance.active(owner, 'R12'))
        for did, deposit in state.deposits.items():
            deposit.remaining_mU = self.inventory.balance(Account(*state.resource_sources[did], deposit.good))

    def settle(self, state: OperatingWorld, ctx: TickContext) -> None:
        f, initial = state.frame, state.initial
        for hub, quantities in f.household.items():
            release_unused_grain(state,hub)
            owner = initial.map.hubs[hub].owner
            for good, amount in quantities.items():
                outside=f.household_outdoor.get(hub,{}).get(good,0)
                stock=self.inventory.prepare_transform({Account(owner,hub,MAIN_LOCK,good):amount-outside,
                    Account(owner,hub,OUTDOOR_LOCK,good):outside},{},
                    reason='civilian_consumption',use=Use.CIVILIAN)
                base = amount * state.price(good, hub)
                paid,recipients=prepare_purchase(state.payments,f.household_locks[hub],stock.materials.events,
                    {good:state.price(good,hub)},kind=f'civilian:{good}',key=(ctx.seed,ctx.tick,'civilian_purchase',hub,good))
                paid.commit(stock)
                state.governance.collect_tax(f.household_locks[hub], hub, base * state.tax[owner], kind=f'consumption_tax:{good}')
                record_income(state,recipients,paid.amounts)
                state.credit_consumption(owner, good, Q(amount))
                state.record_civilian(owner, hub, good, amount)
        for fid, quantities in f.military.items():
            owner, location, crew = f.military_context[fid]
            for good, amount in quantities.items():
                self.inventory.transform({Account(owner, location, f'locked_military:{fid}', good): amount}, {},
                                         reason='military_subsistence', use=Use.MILITARY_SUPPORT)
            payroll_hub = f.military_payroll[fid]
            paid = state.payments.pay(f.military_wage_locks[fid], state.native(owner, f'payroll:{payroll_hub}'),
                                      Q(crew * 15, 10), kind='military_wage', allow_arrears=True)
            state.cycles[owner].wages_due_mU += paid.due_mU
            state.cycles[owner].unpaid_wages_mU += paid.unpaid_mU
        for entity, reserved_workers in f.labor.items():
            workers = f.worked.get(entity, reserved_workers)
            owner, hub = self._owner(entity), self._hub(entity)
            metric = state.cycles[owner]
            for skill, quantity in workers.items():
                metric.labor[skill] = metric.labor.get(skill, 0) + quantity
                local = state.local_cycles[hub]
                local.labor[skill] = local.labor.get(skill, 0) + quantity
            amount = sum((quantity * WAGES[skill] * state.wage_indices[owner] for skill, quantity in workers.items()), Q(0))
            payroll = state.native(owner, f'payroll:{hub}')
            payment = state.payments.pay(f.wage_locks[entity], payroll, amount, kind='wage', allow_arrears=True)
            metric.wages_due_mU += payment.due_mU
            metric.unpaid_wages_mU += payment.unpaid_mU
            name = state.operating_account(entity).name
            metric.costs[name] = metric.costs.get(name, Q(0)) + payment.paid_mU
            state.industry_activity.wages(entity,payment.paid_mU)
            f.operating_paid_mU[entity] = f.operating_paid_mU.get(entity, Q(0)) + payment.paid_mU
        for entity, lock in f.classified_locks.items():
            owner, hub = self._owner(entity), self._hub(entity)
            amount = f.operating_paid_mU.get(entity, Q(0)) / 5
            paid = state.payments.pay(lock, state.native(owner, f'household:{hub}'), amount, kind='classified_operations')
            name = state.operating_account(entity).name
            state.cycles[owner].costs[name] = state.cycles[owner].costs.get(name, Q(0)) + paid.paid_mU
        for reservation, source in f.reservations:
            state.payments.release(reservation, source)
        self.inventory.transfer_many(Transfer(a, Account(a.owner, a.location, 'available', a.asset), q)
            for a, q in self.inventory.balances().items() if a.bucket.startswith('locked_subsistence:'))
        commit_buffers(self.inventory)
        if not ctx.clock.is_cycle_end(ctx.tick):
            return
        for hub, population in initial.populations.items():
            if not population.total_mU:
                continue
            owner = initial.map.hubs[hub].owner
            payroll = state.native(owner, f'payroll:{hub}')
            wages = initial.cash.balance(payroll).native_mU
            household = state.native(owner, f'household:{hub}')
            initial.cash.transfer(payroll, household, wages)
            state.governance.collect_tax(household, hub, wages * state.tax[owner], kind='wage_tax')
        for owner, metric in state.cycles.items():
            state.last_unpaid[owner] = Q(metric.unpaid_wages_mU, max(1, metric.wages_due_mU))

    def settle_population(self, state: OperatingWorld, ctx: TickContext) -> None:
        """Demography reads this cycle's settled inflation and actual welfare."""
        if not ctx.clock.is_cycle_end(ctx.tick):
            return
        initial = state.initial
        for owner, metric in state.cycles.items():
            state.history[owner].append(metric)
        settle_residents(state, ctx)
        state.global_emissions_mU = round(Q(99, 100) * state.global_emissions_mU + sum(m.emitted_mU for m in state.cycles.values()))
        state.cycles = {p: CycleEconomy() for p in initial.known}
        state.local_cycles = {h: CycleEconomy() for h in initial.populations}
        state.completed_cycles += 1

    def observe(self, state: OperatingWorld, ctx: TickContext) -> None:
        if state.consumption.tick != ctx.tick:
            raise InvariantViolation('P8 reached before actual consumption recognition')
        if not ctx.clock.is_window_end(ctx.tick):
            return
        state.spot.publish_offers(tuple(state.initial.known))
        for owner in state.initial.known:
            state.observations[owner] = self.observation(owner, tick=ctx.tick + 1, window=ctx.window + 1)

    def observation(self, owner: str, *, tick: int, window: int) -> dict:
        """Owner-only schema shared by the initial handshake and P8."""
        state = self.s
        if owner not in state.initial.known:
            raise InvariantViolation('unknown observation owner')
        result = {'tick': tick, 'window': window,
                'population_mU': sum(p.total_mU for h, p in state.initial.populations.items() if state.initial.map.hubs[h].owner == owner),
                'treasury_mU': state.initial.cash.balance(state.native(owner, 'treasury')).native_mU,
                'markets': state.spot.visible(owner),
                'food_synthetic_shares_mU': self.inventory.materials.food_shares(owner),
                'inventory_economic_titles_nmU': self.inventory.materials.economic_positions(owner),
                'market_fills': state.spot.fill_reports(owner, state.market_events),
                'command_receipts': [asdict(receipt) for receipt in state.command_streams[owner].recent],
                'macroeconomy': state.macro.visible(owner),
                'logistics': state.cargo.visible(owner),
                'passengers': state.passengers.visible(owner),
                'intermodal': state.intermodal.visible(owner),
                'utilities': state.utilities.visible(owner),
                'city_states': state.city_economy.visible(owner),
                'military_control': state.military_governance.visible(owner),
                'shared_intelligence': state.intelligence_sharing.visible(owner,tick),
                'formations': state.formations.visible(owner),
                **state.blockades.observation(owner),
                **({'fx_shorts': state.fx_shorts.visible(owner)} if state.fx_shorts.visible(owner) else {}),
                'diplomacy': state.relations.visible(owner),
                'export_controls': state.export_controls.visible(),
                'contracts': state.contracts.visible(owner),
                'military_contacts': state.vision.visible(owner),
                'issued_decoys': state.deception.visible(owner),
                'orbital': state.orbit.visible(owner),
                'orbital_contacts': state.orbital_observation.visible(owner),
                'asat': state.asat.visible(owner),
                'sovereign_finance': state.sovereign_finance.visible(owner),
                'bond_market': state.bond_market.visible(owner),
                'weather_forecast': state.forecasts.visible(owner),
                'compute': state.compute.visible(owner, tick//10),
                'ai': state.ai.visible(owner),
                'ai_deployment': state.ai_deployment.visible(owner),
                'ai_licenses': state.ai_licenses.visible(owner),
                'ai_services': state.ai_services.visible(owner),
                'corporate_finance': state.corporate_finance.visible(owner),
                'stock_market': state.stock_market.visible(owner),
                'external_payables': state.payables.visible(owner),
                'reinforcements': state.recruitment.visible(owner),
                'military_refits': state.refits.visible(owner),
                'military_movement': state.movement.visible(owner),
                'stored_military_equipment': [{'id': u.unit.id, 'unit_id': u.unit.unit_type,
                    'hub_id': u.hub, 'hp_mU': u.unit.hp_mU} for u in state.formations.stored_units.values()
                    if u.unit.owner == owner],
                'work': state.work.visible(owner),
                'systems': [{'id': s.id, 'host': s.host, 'hub_id': s.hub, 'kind': s.kind,
                    'flags': sorted(s.flags), 'online': s.online, 'power_received_mU': s.power_received_mU}
                    for s in state.systems.instances.values() if s.owner == owner],
                'power_policy': {'priority_order': state.power_priorities[owner],
                                 'cap_price_mU': state.power_price_caps[owner]},
                'power_contract_caps_mU': {entity: cap for entity, cap in state.frame.power_contract_caps.items()
                                          if self._owner(entity) == owner},
                'research': {'known': sorted(state.research[owner].known),
                    'focus': dict(state.research[owner].focus), 'security': dict(state.research[owner].security),
                    'projects': {tid: {'theory_mU': p.theory_mU, 'theory_target_mU': p.target_mU,
                        'theory_complete': p.theory_complete, 'validation_progress_mU': p.validation_progress_mU,
                        'verified': p.verified} for tid, p in state.research[owner].projects.items()},
                    'validation': [{'id': j.id, 'lab_id': j.lab_id, 'tech_id': j.tech_id, 'active': j.active,
                        'waiting_resource': j.interrupted} for j in state.validation_jobs.values() if j.line.owner == owner]},
                'fx_fills': {oid.split('/', 1)[1]: amount for oid, amount in state.fx_fills.items()
                             if oid.startswith(f'{owner}/')},
                'inventory': [[a.location, a.bucket, a.asset, q] for a, q in sorted(self.inventory.balances().items()) if a.owner == owner]}
        result.update(owned_entities(state, owner))
        return result

    def recognize_consumption(self, state: OperatingWorld, ctx: TickContext) -> None:
        state.consumption.advance(ctx.tick, ctx.cycle, tuple(self.inventory.materials.events),
            weights=state.catalog.weights, shortest=state.initial_paths.distance,services=state.ai_services.scoring_events())
        for event in state.asset_lifecycle.events:
            if event.owner in state.cycles:
                # A cycle-end settlement has already archived its metric row.
                metric = state.history[event.owner][-1] if ctx.clock.is_cycle_end(ctx.tick) else state.cycles[event.owner]
                metric.destroyed_value += event.value


def opening_economy(catalog, initial, *, total_ticks: int = 150, debug: bool = True,
                    commerce: bool = False) -> Kernel:
    if total_ticks > 150:
        raise InfrastructureFault('opening_economy_v1 covers at most 150 ticks; use full_match for a complete game')
    return _assemble(catalog, initial, total_ticks=total_ticks, debug=debug, commerce=commerce, scoring=False)


def full_match(catalog, initial, *, total_ticks: int = 28_800, debug: bool = False,
               commerce: bool = True) -> Kernel:
    """P0-1: the complete game (28,800 ticks by default) with P0-2 national power.

    Same systems as the opening; adds the per-cycle five-pillar collector as the
    final SETTLEMENT system. `finalize_match` turns the finished kernel into V.
    """
    return _assemble(catalog, initial, total_ticks=total_ticks, debug=debug, commerce=commerce, scoring=True)


def _assemble(catalog, initial, *, total_ticks: int, debug: bool, commerce: bool, scoring: bool) -> Kernel:
    from .research_continuity import settle_research_continuity
    state = OperatingWorld(catalog, initial)
    economy = Economy(state)
    economy.initialize_yields()
    from .commands import EngineCommands
    commands = EngineCommands(state)
    def process_commands(state, ctx):
        if ctx.orders and not commerce:
            raise InfrastructureFault('fixed bootstrap does not accept player commands')
        commands.commands(state, ctx)

    systems = (System('material_tick', Phase.COMMANDS, lambda state, ctx: state.initial.inventory.materials.begin_tick(ctx.tick)),
               System('asset_tick', Phase.COMMANDS, lambda state, ctx: state.asset_lifecycle.begin_tick(ctx.tick), after=('material_tick',)),
               System('recruitment_ready', Phase.COMMANDS, lambda state, ctx: state.recruitment.advance(ctx), after=('material_tick',)),
               System('ai_clock', Phase.COMMANDS, lambda state, ctx: state.ai.begin_tick(ctx), after=('material_tick',)),
               System('ai_deployment_clock', Phase.COMMANDS, lambda state, ctx: state.ai_deployment.begin_tick(ctx), after=('ai_clock',)),
               System('ai_services_clock', Phase.COMMANDS, lambda state, ctx: state.ai_services.begin_tick(ctx), after=('ai_deployment_clock',)),
               System('contract_expiry', Phase.COMMANDS, lambda state, ctx: state.contracts.begin_tick(ctx), after=('material_tick',)),
               System('sovereign_cycle', Phase.COMMANDS, lambda state, ctx: state.sovereign_finance.begin_tick(ctx), after=('material_tick',)),
               System('orbital_observation_window', Phase.COMMANDS, lambda state, ctx: state.orbital_observation.begin_tick(ctx), after=('material_tick',)),
               System('orbital_expiry', Phase.COMMANDS, lambda state, ctx: state.orbit.begin_tick(ctx), after=('material_tick',)),
               System('deception_expiry', Phase.COMMANDS, lambda state, ctx: state.deception.begin_tick(ctx), after=('material_tick',)),
               System('vision_window', Phase.COMMANDS, lambda state, ctx: state.vision.begin_tick(ctx), after=('material_tick',)),
               System('relation_expiry', Phase.COMMANDS, lambda state, ctx: state.initial.diplomacy.phase1(ctx.tick), after=('material_tick',)),
               System('expire_orders', Phase.COMMANDS, lambda state, ctx: state.spot.expire(ctx.tick), after=('material_tick',)),
               System('city_reservations', Phase.COMMANDS, lambda state, ctx: state.city_economy.begin_tick(ctx), after=('expire_orders',)),
               System('maintenance_timers', Phase.COMMANDS, lambda state, ctx: state.maintenance.begin_tick(ctx.tick), after=('material_tick',)),
               System('domestic_policy', Phase.COMMANDS, lambda state, ctx: state.governance.begin_tick(ctx), after=('material_tick',)),
               System('storage_policy', Phase.COMMANDS, lambda state, ctx: state.storage.begin_tick(ctx), after=('material_tick',)),
               System('monetary_policy', Phase.COMMANDS, lambda state, ctx: state.macro.begin_tick(ctx), after=('material_tick',)),
               System('industry_activity_clock', Phase.COMMANDS, lambda state, ctx: state.industry_activity.begin_tick(ctx), after=('monetary_policy',)),
               System('stock_cycle', Phase.COMMANDS, lambda state, ctx: state.stock_market.begin_tick(ctx), after=('industry_activity_clock','ai_deployment_clock')),
               System('reserve_sovereign', Phase.COMMANDS, lambda state, ctx: state.debt_funding.reserve(ctx), after=('sovereign_cycle','monetary_policy','ai_deployment_clock','domestic_policy')),
               System('reserve', Phase.COMMANDS, economy.reserve, after=('city_reservations', 'maintenance_timers', 'asset_tick', 'domestic_policy', 'storage_policy', 'monetary_policy', 'recruitment_ready','ai_services_clock','reserve_sovereign','industry_activity_clock')),
               System('commands', Phase.COMMANDS, process_commands, after=('reserve', 'fx_short_cycle', 'relation_expiry', 'vision_window', 'deception_expiry', 'orbital_expiry', 'contract_expiry', 'orbital_observation_window','stock_cycle')),
               System('fx_short_cycle', Phase.COMMANDS, lambda state, ctx: state.fx_shorts.begin_tick(ctx), after=('material_tick',)),
               System('fx_short_settle', Phase.COMMANDS, lambda state, ctx: state.fx_shorts.after_commands(ctx), after=('commands',)),
               System('contract_payments', Phase.COMMANDS, lambda state, ctx: state.contracts.settle(ctx), after=('commands',)),
               System('ai_license_payments', Phase.COMMANDS, lambda state, ctx: state.ai_licenses.settle(ctx), after=('contract_payments',)),
               System('authority_registration', Phase.COMMANDS, lambda state, ctx: state.authority.sync_assets(state.initial.assets), after=('ai_license_payments',)),
               System('reserve_monetary', Phase.COMMANDS, lambda state, ctx: state.macro.reserve(ctx), after=('authority_registration',)),
               System('compute_budget', Phase.COMMANDS, lambda state, ctx: state.compute.reserve(ctx), after=('reserve_monetary','ai_clock')),
               System('reserve_launch', Phase.COMMANDS, lambda state, ctx: state.orbit.reserve(ctx), after=('compute_budget',)),
               System('reserve_march', Phase.COMMANDS, lambda state, ctx: state.movement.reserve(ctx), after=('reserve_launch',)),
               System('reserve_battle', Phase.COMMANDS, lambda state, ctx: state.battles.reserve(ctx), after=('reserve_march',)),
               System('reserve_unit_repair', Phase.COMMANDS, lambda state, ctx: state.military_repairs.reserve(ctx), after=('reserve_battle',)),
               System('reserve_intermodal', Phase.COMMANDS, lambda state, ctx: state.intermodal.reserve(ctx), after=('reserve_unit_repair',)),
               System('reserve_cargo', Phase.COMMANDS, lambda state, ctx: state.cargo.reserve(ctx), after=('reserve_intermodal',)),
               System('reserve_passengers', Phase.COMMANDS, lambda state, ctx: state.passengers.reserve(ctx), after=('reserve_cargo',)),
               System('reserve_utilities', Phase.COMMANDS, lambda state, ctx: state.utilities.reserve(ctx), after=('reserve_passengers',)),
               System('reserve_work', Phase.COMMANDS, lambda state, ctx: state.work.reserve(ctx), after=('reserve_utilities',)),
               System('reserve_maintenance', Phase.COMMANDS, economy.reserve_maintenance, after=('reserve_work',)),
               System('reserve_operations', Phase.COMMANDS, economy.reserve_operations, after=('reserve_maintenance',)),
               System('environment', Phase.ENVIRONMENT_LOGISTICS, economy.environment),
               System('cargo', Phase.ENVIRONMENT_LOGISTICS, lambda state, ctx: state.cargo.advance(ctx), after=('environment',)),
               System('intermodal', Phase.ENVIRONMENT_LOGISTICS, lambda state, ctx: state.intermodal.advance(ctx), after=('cargo',)),
               System('passengers', Phase.ENVIRONMENT_LOGISTICS, lambda state, ctx: state.passengers.advance(ctx), after=('cargo',)),
               System('life_replenishment', Phase.ENVIRONMENT_LOGISTICS, economy.replenish_life, after=('intermodal','passengers')),
               System('power', Phase.POWER, economy.power),
               System('climate_power', Phase.POWER, lambda state, ctx: state.climate.meter_power(ctx), after=('power',)),
               System('electric_cargo', Phase.POWER, lambda state, ctx: state.cargo.advance_electric(ctx), after=('power',)),
               System('water_canals', Phase.POWER, lambda state, ctx: state.utilities.advance(ctx), after=('power',)),
               System('asat', Phase.COMBAT, lambda state, ctx: state.asat.advance(ctx)),
               System('export_policy_events', Phase.COMBAT, lambda state, ctx: state.export_controls.record_policy_events(ctx)),
               System('corridor_policy_events', Phase.COMBAT, lambda state, ctx: state.cargo.record_policy_events(ctx)),
               System('battle', Phase.COMBAT, lambda state, ctx: state.battles.advance(ctx)),
               System('military_control', Phase.COMBAT, lambda state, ctx: state.military_governance.advance(ctx), after=('battle',)),
               System('blockade', Phase.COMBAT, lambda state, ctx: state.blockades.advance(ctx), after=('battle',)),
               System('bond_primary', Phase.MARKETS, lambda state, ctx: state.sovereign_finance.clear_primary(ctx)),
               System('ai_service_market', Phase.MARKETS, lambda state, ctx: state.ai_services.clear(ctx)),
               System('bond_secondary', Phase.MARKETS, lambda state, ctx: state.bond_market.clear(ctx), after=('bond_primary',)),
               System('stock_market', Phase.MARKETS, lambda state, ctx: state.stock_market.clear(ctx)),
               System('spot_markets', Phase.MARKETS, commands.clear),
               System('city_market_deliveries', Phase.MARKETS, lambda state, ctx: state.city_economy.after_markets(ctx), after=('spot_markets',)),
               System('structural_capacity', Phase.PRODUCTION, economy.refresh_capacity),
               System('production', Phase.PRODUCTION, economy.production_phase, after=('structural_capacity',)),
               System('city_production', Phase.PRODUCTION, lambda state, ctx: state.city_economy.produce(ctx), after=('production',)),
               System('joint_construction', Phase.PRODUCTION, lambda state, ctx: state.contracts.joint_ventures.advance(ctx), after=('city_production',)),
               System('work', Phase.PRODUCTION, lambda state, ctx: state.work.advance(ctx), after=('production',)),
               System('compute_registry', Phase.PRODUCTION, lambda state, ctx: state.compute.cleanup(), after=('work',)),
               System('authority_completion', Phase.PRODUCTION, lambda state, ctx: state.authority.sync_assets(state.initial.assets), after=('work',)),
               System('maintenance', Phase.PRODUCTION, economy.maintain, after=('authority_completion',)),
               System('unit_repairs', Phase.PRODUCTION, lambda state, ctx: state.military_repairs.advance(ctx), after=('maintenance',)),
               System('orbital_launches', Phase.PRODUCTION, lambda state, ctx: state.orbit.advance(ctx), after=('maintenance',)),
               System('military_refits', Phase.PRODUCTION, lambda state, ctx: state.refits.advance(ctx), after=('unit_repairs',)),
               System('academy_training', Phase.PRODUCTION, lambda state, ctx: state.governance.advance_training(ctx), after=('maintenance',)),
               System('settlement', Phase.SETTLEMENT, economy.settle),
               System('research_continuity', Phase.SETTLEMENT, settle_research_continuity, after=('settlement',)),
               System('city_settlement', Phase.SETTLEMENT, lambda state, ctx: state.city_economy.settle(ctx), after=('settlement',)),
               System('defense_obligations', Phase.SETTLEMENT, lambda state, ctx: state.contracts.defense_advance(ctx), after=('city_settlement',)),
               System('corporate_tax', Phase.SETTLEMENT, lambda state, ctx: state.corporate_finance.taxes(ctx), after=('settlement',)),
               System('equity_fee_accrual', Phase.SETTLEMENT, lambda state, ctx: state.stock_market.finance.accrue(ctx), after=('corporate_tax',)),
               System('fund_hard_debt', Phase.SETTLEMENT, lambda state, ctx: state.debt_funding.reserve(ctx), after=('equity_fee_accrual',)),
               System('sovereign_service', Phase.SETTLEMENT, lambda state, ctx: state.sovereign_finance.settle(ctx, fund_service=False), after=('fund_hard_debt',)),
               System('payable_service', Phase.SETTLEMENT, lambda state, ctx: state.payables.settle(ctx), after=('fund_hard_debt',)),
               System('bilateral_loan_service', Phase.SETTLEMENT, lambda state, ctx: state.contracts.loans.settle(ctx), after=('fund_hard_debt',)),
               System('equity_fee_service', Phase.SETTLEMENT, lambda state, ctx: state.stock_market.finance.settle(ctx), after=('fund_hard_debt',)),
               System('corporate_dividends', Phase.SETTLEMENT, lambda state, ctx: state.corporate_finance.dividends(ctx), after=('sovereign_service',)),
               System('macroeconomy', Phase.SETTLEMENT, lambda state, ctx: state.macro.settle(ctx), after=('corporate_dividends','payable_service','bilateral_loan_service','equity_fee_service')),
               System('social_security', Phase.SETTLEMENT, lambda state, ctx: state.governance.settle_welfare(ctx), after=('macroeconomy',)),
               System('population', Phase.SETTLEMENT, economy.settle_population, after=('social_security',)),
               System('domestic_end', Phase.SETTLEMENT, lambda state, ctx: state.governance.end_cycle(ctx), after=('population',)),
               System('migration_requests', Phase.SETTLEMENT, lambda state, ctx: state.passengers.end_cycle(ctx), after=('domestic_end',)),
               System('storage_prices', Phase.SETTLEMENT, lambda state, ctx: state.storage.end_cycle(ctx), after=('macroeconomy',)),
               System('ai_settlement', Phase.SETTLEMENT, lambda state, ctx: state.ai.settle(ctx), after=('domestic_end',)),
               System('ai_effect_history', Phase.SETTLEMENT, lambda state, ctx: state.ai_deployment.end_cycle(ctx), after=('ai_settlement',)),
               System('compute_lifetime', Phase.SETTLEMENT, lambda state, ctx: state.compute.age(ctx), after=('population',)),
               System('consumption_receipts', Phase.SETTLEMENT, economy.recognize_consumption, after=('population','compute_lifetime','ai_settlement','defense_obligations')),
               System('industry_activity', Phase.SETTLEMENT, lambda state, ctx: state.industry_activity.finish_tick(ctx), after=('consumption_receipts','ai_effect_history')),
               System('knowledge_boundary', Phase.OBSERVATION, lambda state, ctx: state.promote_knowledge()),
               System('contract_views', Phase.OBSERVATION, lambda state, ctx: state.contracts.publish(), after=('knowledge_boundary',)),
               System('ai_license_views', Phase.OBSERVATION, lambda state, ctx: state.ai_licenses.publish(), after=('knowledge_boundary',)),
               System('weather_forecasts', Phase.OBSERVATION, lambda state, ctx: state.forecasts.advance(ctx), after=('knowledge_boundary',)),
               System('orbital_observation', Phase.OBSERVATION, lambda state, ctx: state.orbital_observation.advance(ctx), after=('knowledge_boundary',)),
               System('military_vision', Phase.OBSERVATION, lambda state, ctx: state.vision.advance(ctx), after=('knowledge_boundary',)),
               System('shared_intelligence', Phase.OBSERVATION, lambda state, ctx: state.intelligence_sharing.advance(ctx), after=('military_vision',)),
               System('city_views', Phase.OBSERVATION, lambda state, ctx: state.city_economy.publish(), after=('knowledge_boundary',)),
               System('observation', Phase.OBSERVATION, economy.observe, after=('shared_intelligence','city_views', 'weather_forecasts', 'contract_views','orbital_observation','ai_license_views')))
    if scoring:
        from .national_power import NationalPower
        state.national_power = NationalPower(state)
        settlement = tuple(s.id for s in systems if s.phase == Phase.SETTLEMENT)
        systems += (System('national_power', Phase.SETTLEMENT, lambda state, ctx: state.national_power.advance(ctx),
                           after=settlement),)
    return Kernel(initial.map.seed, state, systems, clock=ClockConfig(total_ticks=total_ticks),
                  debug=debug, fingerprint_each_tick=debug)
