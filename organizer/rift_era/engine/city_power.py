"""Local BASE coal generation backed by embodied plant, fuel and paid workers."""
from dataclasses import dataclass, field

from .assets import Asset
from .economic_ownership import prepare_purchase, stock_purchase_ceiling, record_income
from .ledger import Account, CashAccount, Transfer
from .materials import Origin
from .numeric import Q, ceil, floor, max_feasible, units
from .payments import Obligation
from .power import Plant, PlantMode, PLANTS, Supply
from .power_operations import calendar, preheat_inputs, warming
from .provenance import Use


@dataclass
class CityGenerator:
    id: str
    city: str
    hub: str
    plant: Plant
    wage_lock: CashAccount | None = None
    fuel_lock: CashAccount | None = None
    labor_mU: int = 0
    bounds: tuple = (Q(0), Q(0))
    generated_mU: int = 0
    lifetime_generated_mU: int = 0
    input_remainders: dict = field(default_factory=dict)
    used_mU: dict = field(default_factory=dict)


class CityPower:
    def __init__(self, cities):
        self.book = cities
        self.s = cities.s
        self.generators = {}
        self.last_power_tick = -1
        self.last_generation_tick = -1
        self.frame_supplies = {}
        self.last_costs = {}
        self._build()

    def _build(self):
        state = self.s
        draft = state.initial.inventory.materials.draft()
        stock = {}
        for city in self.book.cities.values():
            gid = f'{city.id}/power/P070'
            structure = {g: units(q) * 1000 for g, q in state.catalog.profiles['S1']['build'].items()}
            installed = {g: units(q) * 1000 for g, q in state.catalog.recipes['P070']['installed'].items()}
            asset = Asset(gid, city.id, city.hub, {g: structure.get(g, 0) + installed.get(g, 0) for g in structure.keys() | installed.keys()})
            state.initial.assets[gid] = asset
            self.generators[city.id] = CityGenerator(gid, city.id, city.hub,
                Plant(gid, PLANTS['P070'], PlantMode.ON, True, Q(3, 4)))
            for good, quantity in asset.book_nmU.items():
                draft.create(state.asset_lifecycle.account(asset, good), {Origin(city.id, city.hub): quantity / 1000}, virtual=True)
            # Declared genesis buffer, never granted again after initialization.
            for good, quantity in self.book.config['initial_power_buffer_units'].items():
                account = self.book.stock(city, good)
                stock[account] = units(quantity)
                draft.create(account, {Origin(city.id, city.hub): Q(units(quantity))})
        state.initial.inventory.transform({}, stock, reason='declared_city_power_endowment', material_plan=draft.finish())
        state.authority.sync_assets(state.initial.assets)

    def reserve(self, city, ctx):
        """Reserve plant workers before other city production, paying actual work."""
        gen = self.generators[city.id]
        gen.generated_mU = 0; gen.used_mU = {}
        asset = self.s.initial.assets[gen.id]
        calendar(gen.plant, asset.maintenance_debt, ctx)
        desired = ceil(Q(units(self.s.catalog.recipes['P070']['workers']), ctx.clock.cycle_ticks) * asset.availability)
        total = floor(Q(city.population.total_mU * 65, 100 * ctx.clock.cycle_ticks))
        workers = min(desired, total, self.s.initial.cash.balance(city.treasury).native_mU // 5)
        gen.labor_mU = workers
        gen.wage_lock = CashAccount(city.id, f'city_power_wage:{gen.id}', 'RU')
        self.s.initial.cash.transfer(city.treasury, gen.wage_lock, workers * 5)
        fuelstocks = {self.book.stock(city,g):ceil(Q(units(q),ctx.clock.cycle_ticks)) for g,q in self.s.catalog.recipes['P070']['inputs'].items()}
        prices = {a.asset:10*self.s.catalog.weights[a.asset] for a in fuelstocks}
        cost = stock_purchase_ceiling(self.s.initial.inventory,self.s.payments,city.treasury,city.treasury,fuelstocks,prices,kind='city_generator_fuel')
        gen.fuel_lock = CashAccount(city.id,f'city_power_fuel:{gen.id}','RU')
        self.s.initial.cash.transfer(city.treasury,gen.fuel_lock,min(cost,self.s.initial.cash.balance(city.treasury).native_mU))
        return workers

    def supplies(self, ctx):
        if self.last_power_tick == ctx.tick:
            return tuple(self.frame_supplies[k] for k in sorted(self.frame_supplies))
        self.last_power_tick = ctx.tick
        self.frame_supplies = {}
        for city in self.book.cities.values():
            if city.id in self.book.annexed_cities:continue
            gen = self.generators[city.id];asset = self.s.initial.assets[gen.id]
            ratio=min(Q(1),Q(gen.labor_mU*ctx.clock.cycle_ticks,units(self.s.catalog.recipes['P070']['workers'])))
            availability=asset.availability*ratio*self.book.operation_factor(city)
            def ready(output):
                inputs=self._inputs(gen,output)
                if any(q>self.s.initial.inventory.balance(self.book.stock(city,g)) for g,q in inputs.items()):return False
                stocks={self.book.stock(city,g):q for g,q in inputs.items()}
                ceiling=stock_purchase_ceiling(self.s.initial.inventory,self.s.payments,gen.fuel_lock,city.treasury,stocks,
                    {g:10*self.s.catalog.weights[g] for g in inputs},kind='city_generator_fuel')
                return ceiling<=self.s.initial.cash.balance(gen.fuel_lock).native_mU
            cap=max_feasible(1000,ready)
            low,high=gen.plant.prepare(availability,Q(cap,1000),Q(cap,1000))
            gen.bounds=(low,high)
            high_mU=floor(high*1000)
            if high_mU:
                self.frame_supplies[gen.id]=Supply(gen.id,city.hub,city.id,high_mU,
                    sum((units(q)*10*self.s.catalog.weights[g] for g,q in self.s.catalog.recipes['P070']['inputs'].items()),Q(0))/10000+Q(1,5))
        return tuple(self.frame_supplies[k] for k in sorted(self.frame_supplies))

    def _inputs(self,gen,output):
        return {g: floor(Q(units(q)*output,10000)+gen.input_remainders.get(g,Q(0)))
                for g,q in self.s.catalog.recipes['P070']['inputs'].items()}

    def payees(self): return {g.id:self.book.cities[g.city].treasury for g in self.generators.values()}

    def complete(self,ctx,result):
        if self.last_power_tick != ctx.tick or self.last_generation_tick == ctx.tick:
            raise ValueError('city generation requires one fresh dispatch')
        self.last_generation_tick=ctx.tick
        generated={}
        for receipt in result.receipts:
            generated[receipt.source]=generated.get(receipt.source,0)+receipt.sent_mU
        for city in self.book.cities.values():
            if city.id in self.book.annexed_cities:continue
            gen=self.generators[city.id]
            actual=max(ceil(gen.bounds[0]*1000),generated.get(gen.id,0))
            if actual>floor(gen.bounds[1]*1000):raise ValueError('city generation exceeds feasible bound')
            inputs=self._inputs(gen,actual)
            stock=self.s.initial.inventory.prepare_transform({self.book.stock(city,g):q for g,q in inputs.items()},{},
                reason='city_generator_fuel',use=Use.INDUSTRY)
            bill,recipients=prepare_purchase(self.s.payments,gen.fuel_lock,stock.materials.events,
                {g:10*self.s.catalog.weights[g] for g in inputs},kind='city_generator_fuel',key=(ctx.seed,ctx.tick,gen.id),buyer=city.treasury,payer_scope=city.treasury)
            bill.commit(stock);record_income(self.s,recipients,bill.amounts)
            for g,q in self.s.catalog.recipes['P070']['inputs'].items():
                exact=Q(units(q)*actual,10000)+gen.input_remainders.get(g,Q(0))
                gen.input_remainders[g]=exact-inputs[g]
            worked=gen.labor_mU if actual else 0
            self.s.payments.pay(gen.wage_lock,city.household,Q(worked*5),kind='city_generator_wages')
            city.cycle_labor_mU+=worked
            gen.generated_mU=actual;gen.lifetime_generated_mU+=actual;gen.used_mU=inputs
            self.last_costs[gen.id]=sum(bill.amounts)+worked*5
            gen.plant.commit(Q(actual,1000),gen.bounds)
            self.s.payments.release(gen.wage_lock,city.treasury)
            self.s.payments.release(gen.fuel_lock,city.treasury)

    def release_unrun(self):
        for city in self.book.cities.values():
            gen=self.generators[city.id]
            for lock in (gen.wage_lock,gen.fuel_lock):
                if lock is not None:self.s.payments.release(lock,city.treasury)

    def activity_rows(self):
        return [{'asset':g.id,'owner':g.city,'hub':g.hub,'sector':'energy','outputs_mU':{'electricity':g.generated_mU},
            'inputs_mU':dict(g.used_mU),'wages_native_mU':g.labor_mU*5 if g.generated_mU else 0,
            'maintenance_native_mU':0,'direct_shares':dict(self.s.authority.assets[g.id].owner_shares)}
            for g in self.generators.values() if g.city not in self.book.annexed_cities]

    def snapshot(self):
        return {'generators':self.generators,'last_power_tick':self.last_power_tick,
            'last_generation_tick':self.last_generation_tick,'frame_supplies':self.frame_supplies,'last_costs':self.last_costs}

    def audit(self):
        for gen in self.generators.values():
            if gen.generated_mU<0 or any(not 0<=q<1 for q in gen.input_remainders.values()):raise ValueError('invalid city generator accounting')
