"""World-backed operating state and per-tick reservations.

The immutable map and catalog are shared. Every changing balance, residual,
reservation and rolling statistic is included in the dynamic snapshot.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from fractions import Fraction
import json
from pathlib import Path

from .catalog import Catalog, freeze
from .initialization import InitialState, MineSite
from .ledger import Account, CashAccount
from .numeric import Q, units
from .payments import Payments, ForeignExchange
from .research import Research, ValidationJob
from .power import GridRecovery, Load
from .production import Line, Recipe
from .determinism import fingerprint
from .errors import InvariantViolation
from .protocol import Commands
from .spot import SpotExchange
from .materials import Origin
from .consumption import Consumption, InitialPaths
from .maintenance import Maintenance
from .asset_lifecycle import AssetLifecycle
from .construction import Construction
from .public_systems import PublicSystems
from .governance import Governance
from .cargo import cargo_weights, warehouse_load
from .energy_storage import EnergyStorage
from .hydrology import Hydrology
from .macroeconomy import Macroeconomy
from .transport_operations import CargoOperations
from .authority import Authority
from .formations import Formations
from .battle_operations import Battles
from .military_training import MilitaryTraining
from .relation_commands import Relations
from .military_movement import MilitaryMovement
from .military_repairs import MilitaryRepairs
from .military_refits import MilitaryRefits
from .military_sensors import MilitarySensors
from .military_vision import MilitaryVision
from .deception import Deception
from .orbital_operations import OrbitalOperations
from .orbital_observation import OrbitalObservation
from .asat_operations import ASATOperations
from .weather import WeatherForecasts
from .sovereign_finance import SovereignFinance
from .bond_market import BondMarket
from .contracts import Contracts
from .compute_operations import ComputeOperations
from .ai_operations import AIOperations
from .export_controls import ExportControls
from .ai_deployment import AIDeployment
from .ai_licenses import AILicenses
from .ai_services import AIServices
from .economic_ownership import EconomicOwner
from .corporate_finance import CorporateFinance
from .industry_activity import IndustryActivity
from .stock_market import StockMarket
from .debt_funding import DebtFunding
from .payables import Payables
from .passenger_operations import PassengerOperations
from .intermodal_operations import IntermodalOperations
from .military_governance import MilitaryGovernance
from .intelligence_sharing import IntelligenceSharing
from .city_state_economy import CityStateEconomy
from .facility_ownership import FacilityOwnership
from .utility_network import UtilityNetwork


SECTOR = {'冶金厂': 'materials', '材料厂': 'materials', '化工厂': 'materials',
          '机械厂': 'manufacturing', '轻工厂': 'manufacturing', '装备厂': 'manufacturing',
          '船厂': 'manufacturing', '航空厂': 'manufacturing', '制药厂': 'manufacturing',
          '食品厂': 'manufacturing', '农场': 'mining', '受控农场': 'mining',
          '电子厂': 'tech', '实验室': 'tech', '数据中心': 'tech', '电站': 'energy', '水厂': 'energy'}
WAGES = {'general_worker': 10, 'skilled_worker': 18, 'engineer': 32}
BASKET = {'food': Q(6, 100), 'water': Q(4, 100), 'medicine': Q(1, 100),
          'medical_supplies': Q(25, 10000), 'consumer_goods': Q(3, 100)}


@dataclass
class Deposit:
    good: str
    initial_mU: int
    remaining_mU: int
    renewable: bool = False


@dataclass
class CycleEconomy:
    consumed: dict[str, Fraction] = field(default_factory=dict)
    household_consumed: dict[str, Fraction] = field(default_factory=dict)
    needed: dict[str, Fraction] = field(default_factory=dict)
    revenue: dict[str, Fraction] = field(default_factory=dict)
    cash_income_by_currency: dict[str,dict[str,int]] = field(default_factory=dict)
    costs: dict[str, Fraction] = field(default_factory=dict)
    labor: dict[str, int | Fraction] = field(default_factory=dict)
    line_batches: dict[str, int] = field(default_factory=dict)
    extracted: dict[str, int] = field(default_factory=dict)
    power_received_mU: int = 0
    power_needed_mU: int = 0
    power_cost_mU: Fraction = Q(0)
    generation_mU: int = 0
    emitted_mU: int = 0
    unpaid_wages_mU: int = 0
    wages_due_mU: int = 0
    taxes_mU: int = 0
    destroyed_value: Fraction = Q(0)
    # 05 C04/E26: hub-level grid outcomes feeding P7 support events.
    b1_needed_mU: int = 0
    b1_received_mU: int = 0
    blackout_ticks: int = 0


@dataclass
class TickEconomy:
    prices: dict[str, int] = field(default_factory=dict)
    labor: dict[str, dict[str, int]] = field(default_factory=dict)
    worked: dict[str, dict[str, int | Fraction]] = field(default_factory=dict)
    demand_labor: dict[str, int] = field(default_factory=dict)
    batches: dict[str, int] = field(default_factory=dict)
    material_accounts: dict[str, str] = field(default_factory=dict)
    reservations: list[tuple[CashAccount, CashAccount]] = field(default_factory=list)
    wage_locks: dict[str, CashAccount] = field(default_factory=dict)
    input_locks: dict[str, CashAccount] = field(default_factory=dict)
    power_locks: dict[str, CashAccount] = field(default_factory=dict)
    power_subsidy_locks: dict[str, CashAccount] = field(default_factory=dict)
    power_demands: dict[str, int] = field(default_factory=dict)
    power_received: dict[str, int] = field(default_factory=dict)
    power_paid_ru_mU: dict[str, int] = field(default_factory=dict)
    generation: dict[str, int] = field(default_factory=dict)
    household: dict[str, dict[str, int]] = field(default_factory=dict)
    military: dict[str, dict[str, int]] = field(default_factory=dict)
    military_wage_locks: dict[str, CashAccount] = field(default_factory=dict)
    military_context: dict[str, tuple[str, str, int]] = field(default_factory=dict)
    military_need: dict[str, dict[str, int]] = field(default_factory=dict)
    military_payroll: dict[str, str] = field(default_factory=dict)
    household_locks: dict[str, CashAccount] = field(default_factory=dict)
    input_costs: dict[str, Fraction] = field(default_factory=dict)
    plant_bounds: dict[str, tuple[Fraction, Fraction]] = field(default_factory=dict)
    power_loads: tuple[Load, ...] = ()
    mining_efficiency: dict[str, Fraction] = field(default_factory=dict)
    core_entities: set[str] = field(default_factory=set)
    essential_inputs: dict[str, dict[str, int]] = field(default_factory=dict)
    classified_locks: dict[str, CashAccount] = field(default_factory=dict)
    operating_paid_mU: dict[str, Fraction] = field(default_factory=dict)
    work_context: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    availability: dict[str, Fraction] = field(default_factory=dict)
    power_receipts: tuple = ()
    power_contract_caps: dict[str, int] = field(default_factory=dict)
    procurement_seen: set[str] = field(default_factory=set)
    household_need: dict[str,dict[str,int]] = field(default_factory=dict)
    household_outdoor: dict[str,dict[str,int]] = field(default_factory=dict)
    life_replenished: bool = False


class OperatingWorld:
    def __init__(self, catalog: Catalog, initial: InitialState):
        self.catalog, self.initial = catalog, initial
        self.pending_knowledge = {owner: set() for owner in initial.known}
        self.cargo_weights = cargo_weights(catalog)
        # Adopted decision 0007 -> proposal D §2: initial international credit.
        self.credit = {p: Q(85, 100) for p in initial.known}
        initial.inventory.enable_provenance(seed=initial.map.seed, credits=self.credit)
        self.asset_lifecycle = AssetLifecycle(initial.inventory, initial.assets, catalog.weights, initial=True,economic_owner=self.asset_title)
        self.consumption = Consumption()
        self.initial_paths = InitialPaths(tuple((r.a, r.b, r.length) for r in initial.map.routes.values()
                                               if r.initial_operational and r.mode in ('road', 'rail', 'river', 'sea', 'air')))
        self.endowments = freeze(json.loads((Path(__file__).resolve().parents[1] / 'data' / 'opening_endowments_v1.json').read_text()))
        self.endowment_hash = fingerprint(self.endowments)
        self.map_hash = fingerprint(initial.map.manifest())
        self.payments = Payments(initial.cash)
        self.fx = ForeignExchange(initial.cash, self.payments)
        self.spot = SpotExchange(initial.inventory, initial.cash,
                                 max_orders=catalog.runtime['limits']['active_orders_per_nation'])
        self.command_streams = {p: Commands() for p in initial.known}
        self.market_events = []
        self.fx_commands = []
        self.fx_fills = {}
        self.rates = {p: Q(1) for p in initial.known}
        self.tax = {p: Q(self.endowments['opening_tax_rate_bp'], 10000) for p in initial.known}
        self.research = {p: Research(catalog) for p in initial.known}
        self.validation_jobs: dict[str, ValidationJob] = {}
        self.installed_systems: dict[str, dict[str, str]] = {}  # Hub -> system kind -> actual asset ID
        self.work = Construction(self)
        self.interest_bp = {p: 500 for p in initial.known}
        self.power_priorities = {p: ('B1', 'B2', 'B3', 'B4') for p in initial.known}
        self.power_price_caps = {p: 50000 for p in initial.known}
        self.grid_prices_ru = {h: Q(2) for h in initial.map.hubs}
        self.slot_use = {lid: 'production' for lid in initial.lines}
        self.paused_lines: set[str] = set()
        self.line_options: dict[str, dict] = {}
        self.systems = PublicSystems(self)
        self.last_generation_mU: dict[str, int] = {}
        self.last_generation_kind: dict[str, str] = {}
        self.heat_supported: set[str] = set()
        self.frame = TickEconomy()
        self.cycles = {p: CycleEconomy() for p in initial.known}
        self.history = {p: deque(maxlen=40) for p in initial.known}
        self.local_cycles = {h: CycleEconomy() for h in initial.populations}
        self.local_history = {h: deque(maxlen=40) for h in initial.populations}
        self.maintenance = Maintenance(initial.inventory, initial.cash, self.payments, initial.assets, seed=initial.map.seed,title_payments=True)
        for fid, facility in initial.facilities.items():
            costs = {'cement': 200} if facility.kind == 'housing' else {
                g: units(q) for g, q in catalog.profiles[facility.profile]['maintenance'].items()}
            self.maintenance.register(fid, costs, self.operating_account(fid),
                                      {g: self.seller(facility.owner, g) for g in costs})
        for mid, mine in initial.mines.items():
            self.maintenance.register(mid, {'basic_parts': 100 if mine.level == 'G1' else 200},
                                      self.operating_account(mid), {'basic_parts': self.seller(mine.owner, 'basic_parts')})
        self.input_credits: dict[tuple[str, str], Fraction] = {}
        self.quantity_remainders: dict[tuple[str, str], Fraction] = {}
        self.pollution = {h: 0 for h in initial.populations}
        self.global_emissions_mU = 0
        self.rain = {h: Q(1) for h in initial.populations}
        self.wind_mU = {r.id: r.wind_mean_mU for r in initial.map.regions.values() if r.terrain != 'ocean'}
        self.cloud_mU = {r.id: r.cloud_mean_mU for r in initial.map.regions.values() if r.terrain != 'ocean'}
        self.sun_mU = dict.fromkeys(self.wind_mU, 0)
        self.natural_water_mU = {h: 0 for h in initial.populations}
        self.water_used_mU = {h: 0 for h in initial.populations}
        self.grids = {h: GridRecovery() for h in initial.populations}
        self.last_satisfaction: dict[str, dict[str, Fraction]] = {}
        self.last_unpaid = {p: Q(0) for p in initial.known}
        self.wage_indices = {p: Q(1) for p in initial.known}
        self.cpi = {p: Q(1) for p in initial.known}
        self.inflation = {p: Q(0) for p in initial.known}
        self.completed_cycles = 0
        self.governance = Governance(self)
        self.observations: dict[str, dict] = {}
        self.bootstrap_failures: list[str] = []
        self.deposits: dict[str, Deposit] = {}
        self.extraction_lines: dict[str, Line] = {}
        self.extraction_sources: dict[str, tuple[str, str, str]] = {}
        self.resource_sites = dict(initial.mines)
        self.resource_sources = {}
        self.mine_deposit = {mid: mid for mid in initial.mines}
        # Explicit, fingerprinted generator choices, fixed before the run.
        # No balance or coefficient is topped up to rescue a failed bootstrap.
        reserve_units = dict(self.endowments['finite_reserve_units_per_starting_site'])
        reserve_units.update(biomass=self.endowments['biomass_capacity_units_per_starting_site'], water=0)
        atlas = initial.resource_atlas
        for mid, mine in initial.mines.items():
            # world_atlas_v1 declares each starting site's reserve; the legacy
            # candidate map has no atlas and keeps the opening endowment table.
            site = atlas.get(mid)
            reserve = site.reserve_mU if site is not None and site.starting_mine == mid else units(reserve_units[mine.good])
            self.deposits[mid] = Deposit(mine.good, reserve, reserve, mine.good in ('water', 'biomass'))
            source_location = f'basin:{mine.hub}' if mine.good == 'water' else mid
            source = Account('nature', source_location, 'deposit', mine.good)
            initial.inventory.transform({}, {source: reserve}, reason='declared_worldgen_deposit',
                                        origins={source: Origin('nature', mine.hub)})
            rate = round(units(catalog.runtime['extraction'][mine.good]['output']) * mine.grade)
            recipe = Recipe(f'extract:{mine.good}', {mine.good: rate}, {mine.good: rate}, frozenset(), frozenset())
            self.extraction_lines[mid] = Line(mid, mine.owner, mine.hub, recipe)
            self.extraction_sources[mid] = ('nature', source_location, 'deposit')
            self.resource_sources[mid] = self.extraction_sources[mid]
        # Remote atlas deposits (resource_atlas_v1): registered, hidden geology
        # with real nature-custody stock, but no extraction line until a mine
        # is built on them through `build`/`mine` (G1/G2).
        for sid in sorted(atlas):
            site = atlas[sid]
            if site.starting_mine is not None or sid in self.resource_sites:
                continue
            hub_owner = initial.map.hubs[site.hub].owner or 'world'
            self.resource_sites[sid] = MineSite(sid, hub_owner, site.hub, site.good, Q(site.grade_mU, 1000))
            reserve = 0 if site.good == 'water' else site.reserve_mU
            self.deposits[sid] = Deposit(site.good, reserve, reserve, site.renewable)
            source_location = f'basin:{site.hub}' if site.good == 'water' else sid
            source = Account('nature', source_location, 'deposit', site.good)
            if reserve:
                initial.inventory.transform({}, {source: reserve}, reason='declared_worldgen_deposit',
                                            origins={source: Origin('nature', site.hub)})
            self.resource_sources[sid] = ('nature', source_location, 'deposit')
        # Fix the bootstrap plan before execution: first eligible research node.
        for lid, line in initial.lines.items():
            if line.recipe.id == 'P075':
                research = self.research[line.owner]
                target = next(t for t in catalog.technologies if catalog.researchable(t, research.known))
                research.set_focus(lid, target)
        self.hydrology = Hydrology(self)
        self.storage = EnergyStorage(self)
        self.macro = Macroeconomy(self)
        self.cargo = CargoOperations(self)
        self.authority = Authority(initial.map, initial.assets, initial.diplomacy.actors)
        self.facility_ownership = FacilityOwnership(self)
        self.utilities = UtilityNetwork(self)
        self.formations = Formations(self)
        self.battles = Battles(self)
        self.recruitment = MilitaryTraining(self)
        self.relations = Relations(self)
        self.export_controls = ExportControls(self)
        self.contracts = Contracts(self)
        self.sovereign_finance = SovereignFinance(self)
        self.bond_market = BondMarket(self)
        self.movement = MilitaryMovement(self)
        self.military_repairs = MilitaryRepairs(self)
        self.refits = MilitaryRefits(self)
        self.orbit = OrbitalOperations(self)
        self.orbital_observation = OrbitalObservation(self)
        self.asat = ASATOperations(self)
        self.sensors = MilitarySensors(self)
        self.deception = Deception(self)
        self.vision = MilitaryVision(self)
        self.forecasts = WeatherForecasts(self)
        self.compute = ComputeOperations(self)
        self.ai = AIOperations(self)
        self.ai_deployment = AIDeployment(self)
        self.ai_licenses = AILicenses(self)
        self.ai_services = AIServices(self)
        self.corporate_finance = CorporateFinance(self)
        self.industry_activity = IndustryActivity(self)
        self.stock_market = StockMarket(self)
        self.debt_funding = DebtFunding(self)
        self.debt_funding.add_provider(self.sovereign_finance.funding_claims)
        self.payables = Payables(self)
        self.debt_funding.add_provider(self.payables.funding_claims)
        self.debt_funding.add_provider(self.contracts.loans.funding_claims)
        self.debt_funding.add_provider(self.stock_market.finance.funding_claims)
        self.passengers = PassengerOperations(self)
        self.intermodal = IntermodalOperations(self)
        self.military_governance = MilitaryGovernance(self)
        self.intelligence_sharing = IntelligenceSharing(self)
        self.city_economy = CityStateEconomy(self)

    def promote_knowledge(self):
        """P8 is the next-tick boundary; P7 never uses a fresh P6 unlock."""
        for owner, pending in self.pending_knowledge.items():
            if pending:
                self.research[owner].known.update(pending)
                self.initial.known[owner] = frozenset(self.research[owner].known)
                pending.clear()
        self.orbit.discover_capacity()
        self.ai.boundary()

    def active_order_count(self, owner):
        exchanges=[self.spot,self.bond_market.exchange]
        if hasattr(self,'stock_market'):exchanges.append(self.stock_market.exchange)
        count=sum(order.owner == owner for exchange in exchanges
                  for order in exchange.orders.values())
        if hasattr(self,'ai_services'):
            count+=int(owner in self.ai_services.offers)+sum(p==owner for p,_ in self.ai_services.bids)
        return count

    def warehouse_load(self):
        occupied=warehouse_load(self.initial.inventory,self.cargo_weights,self.initial.warehouse_mU)
        for hub,quantity in self.refits.capacity_reserved().items():occupied[hub]+=quantity
        return occupied

    def warehouse_load_at(self,hub):
        """warehouse_load()[hub], without summing every other hub."""
        occupied=warehouse_load(self.initial.inventory,self.cargo_weights,{hub:None})[hub]
        return occupied+self.refits.capacity_reserved().get(hub,Q(0))

    def native(self, owner: str, name: str) -> CashAccount:
        return CashAccount(owner, name, owner if owner in self.rates else 'RU')

    def asset_title(self,aid):
        initial=self.initial;asset=initial.assets[aid]
        if aid in initial.mines:return EconomicOwner(asset.owner,'corp:mining')
        if aid in initial.lines:
            line=initial.lines[aid]
            if line.recipe.id!='__idle__':
                return EconomicOwner(asset.owner,'corp:'+SECTOR[self.catalog.recipes[line.recipe.id]['facility']])
            parent=initial.facilities.get(aid.rsplit('/line',1)[0])
            if parent and parent.kind in SECTOR:return EconomicOwner(asset.owner,'corp:'+SECTOR[parent.kind])
        facility=initial.facilities.get(aid)
        if facility and facility.kind in SECTOR:return EconomicOwner(asset.owner,'corp:'+SECTOR[facility.kind])
        return None

    def operating_account(self, entity: str, currency: str | None = None) -> CashAccount:
        state = self.initial
        if entity.startswith('weapon:'):
            owner, _ = self.battles.power_context[entity]
            return self.native(owner, 'treasury') if currency is None else CashAccount(owner, 'treasury', currency)
        if entity.startswith('haul_labor:') and entity not in self.frame.work_context:
            job = self.cargo.hauls[self.cargo.vehicle_jobs[entity.split(':', 1)[1]]]
            return self.native(job.owner, 'treasury') if currency is None else CashAccount(job.owner, 'treasury', currency)
        if entity.startswith('storage:'):
            storage = self.storage.stores[entity.split(':', 1)[1]]
            return self.native(storage.owner, 'corp:energy') if currency is None else CashAccount(storage.owner, 'corp:energy', currency)
        if entity.startswith('ccs:'):
            return self.operating_account(entity.split(':', 1)[1], currency)
        if entity.startswith(('pump:', 'ignition:')):
            return self.operating_account(entity.split(':', 1)[1], currency)
        if entity in self.work.jobs:
            account = self.work.payer(self.work.jobs[entity])
            return CashAccount(account.owner, account.name, currency or account.currency)
        if entity in self.frame.work_context:
            owner, _, name = self.frame.work_context[entity]
            return CashAccount(owner, name, currency or self.native(owner, name).currency)
        if entity.startswith('sys:'):
            system = self.systems.instances[entity.split(':', 1)[1]]
            name = 'corp:tech' if system.kind == 'big_science' else 'treasury'
            return CashAccount(system.owner, name, currency or self.native(system.owner, name).currency)
        if entity.startswith('big_science:'):
            hub = entity.split(':', 1)[1]
            owner = state.assets[self.installed_systems[hub]['big_science']].owner
            return CashAccount(owner, 'corp:tech', currency or owner)
        if entity in self.validation_jobs:
            owner = self.validation_jobs[entity].line.owner
            return CashAccount(owner, 'corp:tech', currency or owner)
        if entity in state.lines:
            line = state.lines[entity]
            sector = SECTOR[self.catalog.recipes[line.recipe.id]['facility']]
            owner = line.owner
        elif entity in state.mines:
            sector, owner = 'mining', state.mines[entity].owner
        elif entity in state.facilities and state.facilities[entity].kind in SECTOR:
            facility = state.facilities[entity]
            sector, owner = SECTOR[facility.kind], facility.owner
        elif entity.startswith('municipal:'):
            hub = entity.split(':', 1)[1]
            owner = state.map.hubs[hub].owner
            return CashAccount(owner, f'household:{hub}', currency or owner)
        else:
            owner = state.assets[entity].owner if entity in state.assets else entity.split(':', 1)[-1]
            return CashAccount(owner, 'treasury', currency or owner)
        return CashAccount(owner, f'corp:{sector}', currency or owner)

    def seller(self, owner: str, good: str) -> CashAccount:
        # Legacy callers without a physical draw may only name government
        # inventory. Actual consumption resolves the lot's economic title.
        return self.native(owner, 'treasury')

    def price(self, good: str, hub: str | None = None) -> Fraction:
        # Freeze quotes at P1. P5 cannot retroactively increase an invoice whose
        # cash was already reserved for P3/P6/P7 earlier in this tick.
        quote = self.frame.prices.get(f'spot:{hub}:{good}') if hub is not None else None
        return Q(quote, 1000) if quote is not None else 10 * self.catalog.weights[good]

    def wage_cost(self, owner: str, workers: dict[str, int]) -> Fraction:
        return sum((q * WAGES[skill] * self.wage_indices[owner] for skill, q in workers.items()), Q(0))

    def labor_participation(self, owner: str, hub: str) -> Fraction:
        """Common capacity factor for reservations and population withdrawals."""
        pop = self.initial.populations[hub]
        penalty = {'GOV_STABLE': Q(1), 'GOV_WARN_DISCONTENT': Q(9, 10),
                   'GOV_ALERT_STRIKE': Q(65, 100), 'GOV_CRISIS_PARALYSIS': Q(1, 4)}[pop.warning]
        food = self.last_satisfaction.get(hub, {}).get('food', Q(1))
        hunger = Q(2, 5) + Q(3, 4) * food if food < Q(4, 5) else Q(1)
        return (self.governance.participant_fraction(owner, hub) * penalty * hunger *
                (1 - Q(85, 100) * self.last_unpaid[owner]))

    def accrue_quantity(self, entity: str, good: str, exact_mU: Fraction) -> int:
        from .numeric import floor
        key = (entity, good)
        total = self.quantity_remainders.get(key, Q(0)) + exact_mU
        amount = floor(total)
        self.quantity_remainders[key] = total - amount
        return amount

    def credit_consumption(self, owner: str, good: str, quantity_mU: Fraction) -> None:
        metric = self.cycles[owner]
        metric.consumed[good] = metric.consumed.get(good, Q(0)) + quantity_mU

    def metrics(self, owner: str, hub: str):
        return self.cycles[owner], self.local_cycles[hub]

    def record_need(self, owner, hub, good, amount):
        for metric in self.metrics(owner, hub):
            metric.needed[good] = metric.needed.get(good, Q(0)) + amount

    def record_civilian(self, owner, hub, good, amount):
        for metric in self.metrics(owner, hub):
            metric.household_consumed[good] = metric.household_consumed.get(good, Q(0)) + amount
            if good == 'grain':
                metric.household_consumed['food'] = metric.household_consumed.get('food', Q(0)) + Q(amount * 20, 27)

    def record_emissions(self, owner, hub, amount):
        for metric in self.metrics(owner, hub):
            metric.emitted_mU += amount

    def snapshot(self) -> dict:
        def pair_rows(data):
            return [[a, b, value] for (a, b), value in sorted(data.items()) if value]
        return {'map_hash': self.map_hash, 'rules_hash': self.catalog.fingerprint,
                'endowment_hash': self.endowment_hash, 'inventory': self.initial.inventory.snapshot(),
                'cash': self.initial.cash.snapshot(), 'populations': self.initial.populations,
                'assets': self.initial.assets, 'lines': self.initial.lines, 'plants': self.initial.plants,
                'mines': self.initial.mines,
                'units': self.initial.units, 'companies': self.initial.companies,
                'facilities': self.initial.facilities, 'policies': self.initial.line_policies,
                'known': self.initial.known, 'warehouses': self.initial.warehouse_mU, 'housing': self.initial.housing_mU,
                'warehouse_modules': self.initial.warehouse_modules,
                'relations': [[list(pair), value] for pair, value in sorted(self.initial.diplomacy.pairs.items())],
                'deposits': self.deposits, 'frame': self.frame, 'cycles': self.cycles,
                'extraction_lines': self.extraction_lines, 'extraction_sources': self.extraction_sources,
                'resource_sites': self.resource_sites, 'resource_sources': self.resource_sources, 'mine_deposit': self.mine_deposit,
                'history': {p: list(h) for p, h in self.history.items()},
                'local_cycles': self.local_cycles, 'local_history': {h: list(rows) for h, rows in self.local_history.items()},
                'pending_knowledge': self.pending_knowledge,
                'research': {p: {'known': r.known, 'projects': r.projects, 'focus': r.focus, 'security': r.security, 'licenses': r.licenses} for p, r in self.research.items()},
                'payments': self.payments.snapshot(), 'fx': self.fx.snapshot(), 'rates': self.rates,
                'spot': self.spot.snapshot(), 'market_events': self.market_events,
                'fx_commands': self.fx_commands, 'fx_fills': self.fx_fills,
                'consumption': self.consumption.snapshot(), 'credit': self.credit,
                'maintenance': self.maintenance.snapshot(),
                'asset_lifecycle': self.asset_lifecycle.snapshot(),
                'validation_jobs': self.validation_jobs, 'installed_systems': self.installed_systems,
                'systems': self.systems.snapshot(),
                'governance': self.governance.snapshot(),
                'hydrology': self.hydrology.snapshot(), 'storage': self.storage.snapshot(),
                'macro': self.macro.snapshot(), 'cargo': self.cargo.snapshot(),
                'authority': self.authority.snapshot(), 'formations': self.formations.snapshot(),
                'battles': self.battles.snapshot(), 'recruitment': self.recruitment.snapshot(),
                'export_controls': self.export_controls.snapshot(),
                'ai_deployment': self.ai_deployment.snapshot(),
                'ai_licenses': self.ai_licenses.snapshot(),
                'ai_services': self.ai_services.snapshot(),
                'corporate_finance': self.corporate_finance.snapshot(),
                'industry_activity': self.industry_activity.snapshot(),
                'stock_market': self.stock_market.snapshot(),
                'debt_funding': self.debt_funding.snapshot(),
                'payables': self.payables.snapshot(),
                'passengers': self.passengers.snapshot(),
                'intermodal': self.intermodal.snapshot(),
                'military_governance': self.military_governance.snapshot(),
                'intelligence_sharing': self.intelligence_sharing.snapshot(),
                'city_economy': self.city_economy.snapshot(),
                'facility_ownership': self.facility_ownership.snapshot(),
                'utilities': self.utilities.snapshot(),
                'ai': self.ai.snapshot(), 'compute': self.compute.snapshot(), 'contracts': self.contracts.snapshot(), 'bond_market': self.bond_market.snapshot(), 'sovereign_finance': self.sovereign_finance.snapshot(), 'forecasts': self.forecasts.snapshot(), 'asat': self.asat.snapshot(), 'orbital_observation': self.orbital_observation.snapshot(), 'orbit': self.orbit.snapshot(), 'vision': self.vision.snapshot(), 'deception': self.deception.snapshot(), 'refits': self.refits.snapshot(), 'military_repairs': self.military_repairs.snapshot(), 'movement': self.movement.snapshot(), 'relation_policy': self.relations.snapshot(), 'next_war_id': self.initial.diplomacy.next_war,
                'work': self.work.snapshot(), 'interest_bp': self.interest_bp,
                'power_priorities': self.power_priorities, 'power_price_caps': self.power_price_caps,
                'grid_prices_ru': self.grid_prices_ru,
                'slot_use': self.slot_use, 'paused_lines': self.paused_lines, 'line_options': self.line_options,
                'last_generation_mU': self.last_generation_mU, 'last_generation_kind': self.last_generation_kind,
                'heat_supported': self.heat_supported,
                'commands': {p: {'watermark': c.watermark, 'plans': c.plans, 'recent': c.recent}
                             for p, c in self.command_streams.items()},
                'tax': self.tax, 'input_credits': pair_rows(self.input_credits),
                'quantity_remainders': pair_rows(self.quantity_remainders),
                'pollution': self.pollution, 'global_emissions_mU': self.global_emissions_mU,
                'rain': self.rain, 'natural_water_mU': self.natural_water_mU, 'water_used_mU': self.water_used_mU,
                'wind_mU': self.wind_mU, 'cloud_mU': self.cloud_mU, 'sun_mU': self.sun_mU,
                'grids': self.grids, 'last_satisfaction': self.last_satisfaction, 'last_unpaid': self.last_unpaid,
                'wage_indices': self.wage_indices, 'cpi': self.cpi, 'inflation': self.inflation,
                'completed_cycles': self.completed_cycles, 'observations': self.observations,
                'bootstrap_failures': self.bootstrap_failures}

    def audit(self) -> None:
        self.initial.audit()
        self.spot.audit()
        self.consumption.audit()
        self.maintenance.audit()
        self.asset_lifecycle.audit()
        self.work.audit()
        self.systems.audit()
        self.governance.audit()
        self.hydrology.audit()
        self.storage.audit()
        self.cargo.audit()
        self.authority.audit()
        self.formations.audit()
        self.recruitment.audit()
        self.movement.audit()
        self.military_repairs.audit()
        self.refits.audit()
        self.deception.audit()
        self.orbit.audit()
        self.asat.audit()
        self.sovereign_finance.audit()
        self.bond_market.audit()
        self.contracts.audit()
        self.export_controls.audit()
        self.compute.audit()
        self.ai.audit()
        self.ai_deployment.audit()
        self.ai_licenses.audit()
        self.ai_services.audit()
        self.corporate_finance.audit()
        self.industry_activity.audit()
        self.stock_market.audit()
        self.payables.audit()
        self.passengers.audit()
        self.intermodal.audit()
        self.city_economy.audit()
        self.facility_ownership.audit()
        self.utilities.audit()
        for owner, pending in self.pending_knowledge.items():
            for tech in pending:
                if tech in self.catalog.milestones:
                    continue
                project = self.research[owner].projects.get(tech)
                if tech not in self.catalog.technologies or project is None or not project.verified:
                    raise InvariantViolation('unverified technology queued for next-tick use')
            if any(project.verified and tech not in self.research[owner].known and tech not in pending
                   for tech, project in self.research[owner].projects.items()):
                raise InvariantViolation('verified project lost its permanent knowledge grant')
        if any(self.active_order_count(p) > self.catalog.runtime['limits']['active_orders_per_nation']
               for p in self.initial.known):
            raise InvariantViolation('combined commodity and financial order cap exceeded')
        if self.credit is not self.initial.inventory.materials.credits:
            raise InvariantViolation('material use lost its live diplomatic credit source')
        for line in self.extraction_lines.values():
            line.audit()
        from .production import Production
        materials = self.initial.inventory.materials
        for job in self.validation_jobs.values():
            job.line.audit()
        virtual = Production.virtual_stock((*self.initial.lines.values(), *self.extraction_lines.values(),
                                             *(job.line for job in self.validation_jobs.values())))
        virtual.update(self.asset_lifecycle.virtual_stock())
        virtual.update(self.cargo.transport.virtual_stock())
        virtual.update(self.passengers.transport.virtual_stock())
        virtual.update(self.military_repairs.virtual_stock())
        for (entity, good), amount in self.input_credits.items():
            line = self.initial.lines[entity]
            account = Account(line.owner, line.hub, f'generator_credit:{entity}', good)
            virtual[account] = amount
        materials.audit_virtual(virtual)
        if any(d.remaining_mU < 0 for d in self.deposits.values()):
            raise InvariantViolation('negative resource deposit')
        for mid, deposit in self.deposits.items():
            if deposit.remaining_mU != self.initial.inventory.balance(Account(*self.resource_sources[mid], deposit.good)):
                raise InvariantViolation('deposit view differs from physical resource pool')
        if any(not 0 <= credit < 1 for credit in self.input_credits.values()):
            raise InvariantViolation('fractional input credit outside [0,1) mU')
        if any(p.total_mU < 0 for p in self.initial.populations.values()):
            raise InvariantViolation('negative population')
