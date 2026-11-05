"""Materialize the R6 starting template without silently granting technology.

This is state construction, not bootstrap acceptance. Deposit reserves and the
dynamic economy still have to be supplied by the complete world assembly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

from .assets import Asset
from .catalog import Catalog
from .combat import Unit
from .diplomacy import Diplomacy
from .ledger import Inventory, Account, CashLedger, CashAccount
from .numeric import Q, units
from .population import Population
from .power import Plant, PlantMode, PLANTS
from .production import Line, Recipe
from .world import SpatialWorld


@dataclass(frozen=True)
class MineSite:
    id: str
    owner: str
    hub: str
    good: str
    grade: Fraction
    level: str = 'G1'


@dataclass
class Company:
    id: str
    issuer: str
    sector: str
    shares: dict[str, int]
    listed: bool = False


@dataclass(frozen=True)
class Facility:
    id: str
    owner: str
    hub: str
    profile: str
    kind: str
    line_ids: tuple[str, ...]
    commissioned: bool = True


@dataclass(frozen=True)
class InitialLinePolicy:
    load_mU: int
    manufacturing_mode: str | None
    fertility_mU: int | None = None


@dataclass
class InitialState:
    map: SpatialWorld
    inventory: Inventory
    cash: CashLedger
    populations: dict[str, Population]
    assets: dict[str, Asset]
    lines: dict[str, Line]
    plants: dict[str, Plant]
    mines: dict[str, MineSite]
    units: dict[str, Unit]
    companies: dict[str, Company]
    facilities: dict[str, Facility]
    line_policies: dict[str, InitialLinePolicy]
    known: dict[str, frozenset[str]]
    warehouse_mU: dict[str, int]
    housing_mU: dict[str, int]
    diplomacy: Diplomacy
    warehouse_modules: dict[str, dict[str, int]] = field(default_factory=dict)
    resource_atlas: dict = field(default_factory=dict)

    def audit(self) -> None:
        self.inventory.audit()
        self.cash.audit()
        if any(sum(company.shares.values()) != 1000000 for company in self.companies.values()):
            from .errors import InvariantViolation
            raise InvariantViolation('initial company shares do not sum to one million')
        for line in self.lines.values():
            line.audit()

    def snapshot(self) -> dict:
        return {'map': self.map.manifest(), 'inventory': self.inventory.snapshot(),
                'cash': self.cash.snapshot(), 'populations': self.populations, 'assets': self.assets,
                'lines': self.lines, 'plants': self.plants, 'mines': self.mines, 'units': self.units,
                'companies': self.companies, 'known': self.known, 'warehouse_mU': self.warehouse_mU,
                'facilities': self.facilities, 'line_policies': self.line_policies,
                'housing_mU': self.housing_mU,
                'warehouse_modules': self.warehouse_modules,
                'resource_atlas': self.resource_atlas,
                'relations': [[list(pair), value] for pair, value in sorted(self.diplomacy.pairs.items())]}


def initialize(catalog: Catalog, world: SpatialWorld) -> InitialState:
    template = catalog.runtime['initial_world']
    financial = catalog.runtime['financial']
    inventory, cash = Inventory(), CashLedger()
    populations, assets, lines, plants, mines, army, companies, known = {}, {}, {}, {}, {}, {}, {}, {}
    facilities, policies = {}, {}
    warehouses = {hid: units(300) for hid in world.hubs}
    housing = {hid: 0 for hid in world.hubs}
    sectors = ('mining', 'materials', 'manufacturing', 'energy', 'logistics', 'tech')
    for country in sorted(world.country_regions):
        hub = f'{country}-h0'
        known[country] = frozenset()  # BASE is implicit, L02 is NOT granted.
        for index in range(5):
            populations[f'{country}-h{index}'] = Population(f'{country}-h{index}', {
                skill: units(amount) if index == 0 else 0
                for skill, amount in zip(('general_worker', 'skilled_worker', 'engineer'), template['civilian_skills'])})
        warehouses[hub] = units(template['warehouse_capacity'])
        housing[hub] = units(100 * template['housing'])
        for good, amount in template['inventory'].items():
            inventory.initialize(Account(country, hub, 'available', good), units(amount))
        cash.initialize(CashAccount(country, 'treasury', country), units(template['treasury']))
        cash.initialize(CashAccount(country, f'household:{hub}', country), units(template['households']))
        cash.initialize(CashAccount('world', 'pool', country), units(financial['world_native_each']))
        for sector in sectors:
            cid = f'{country}-{sector}'
            companies[cid] = Company(cid, country, sector, {country: financial['company_shares']})
        for rid, count in template['productive_lines'].items():
            recipe = catalog.recipes[rid]
            for index in range(count):
                fid = f'{country}/{rid}/{index}'
                assets[fid] = Asset(fid, country, hub,
                    {g: units(q) * 1000 for g, q in catalog.profiles['S1']['build'].items()})
                lid = f'{fid}/line'
                assets[lid] = Asset(lid, country, hub,
                    {g: units(q) * 1000 for g, q in recipe['installed'].items()})
                lines[lid] = Line(lid, country, hub, Recipe.from_catalog(catalog, rid))
                facilities[fid] = Facility(fid, country, hub, 'S1', recipe['facility'], (lid,))
                policies[lid] = InitialLinePolicy(units(template['load_overrides'].get(rid, 1)),
                    'general' if recipe['facility'] in catalog.tables['industry']['mode_eligible_facilities'] else None,
                    units(template['farm_fertility']) if rid in ('P028', 'P029') else None)
                if rid in PLANTS:
                    plants[lid] = Plant(lid, PLANTS[rid], PlantMode.ON, True, Q(3, 4))
        for index in range(template['housing']):
            fid = f'{country}/housing/{index}'
            assets[fid] = Asset(fid, country, hub, {'cement': 20000000, 'steel': 5000000})
            facilities[fid] = Facility(fid, country, hub, 'S1', 'housing', ())
        for good, row in template['mines'].items():
            for index in range(row['count']):
                mid = f'{country}/mine/{good}/{index}'
                mines[mid] = MineSite(mid, country, hub, good, Q(row['grade']))
                geological = world.hidden_resources.get(mid)
                if geological is not None:
                    mines[mid] = MineSite(mid, country, hub, good, Q(geological.grade_mU, 1000))
                assets[mid] = Asset(mid, country, hub, {'steel': 4000000, 'cement': 4000000, 'basic_parts': 1000000})
        for formation_index, formation in enumerate(template['formations']):
            for copy in range(formation['count']):
                for unit_type, count in formation['units'].items():
                    spec = catalog.units[unit_type]
                    for index in range(count):
                        uid = f'{country}/garrison/{formation_index}/{copy}/{unit_type}/{index}'
                        army[uid] = Unit(uid, country, unit_type, world.hubs[hub].region,
                                         units(spec['hp']), units(spec['crew']))
                        for good, quantity in spec['installed'].items():
                            inventory.initialize(Account(country, hub, f'installed_military:{uid}', good), units(quantity))
    # One public warehouse entity per Hub; expansion changes its module count,
    # not the number of permanently retained entities. Historical modules have
    # their actual paid material book, and even the base capacity has health.
    for hid, hub in world.hubs.items():
        if hub.owner in known and hid not in populations:
            populations[hid] = Population(hid, {'general_worker': 0, 'skilled_worker': 0, 'engineer': 0})
        modules = (warehouses[hid] - units(300)) // units(500)
        aid = f'warehouse:{hid}'
        assets[aid] = Asset(aid, hub.owner or 'world', hid,
                            {'steel': 4000000 * modules, 'cement': 6000000 * modules} if modules else {})
    cash.initialize(CashAccount('world', 'pool', 'RU'), units(financial['world_ru']))
    for city in world.city_states:
        hub = f'{city}-h0'
        cash.initialize(CashAccount(city, 'treasury', 'RU'), units(catalog.runtime['city_states']['initial_cash_ru']))
        cash.initialize(CashAccount(city, f'household:{hub}', 'RU'), units(financial['city_state_household_ru']))
    for good, definition in catalog.goods.items():
        if definition['kind'] == 'inventory':
            inventory.initialize(Account('world', 'world_pool', 'available', good), units(financial['world_inventory_per_inventory_good']))
    inventory.seal()
    cash.seal()
    state = InitialState(world, inventory, cash, populations, assets, lines, plants, mines, army,
                         companies, facilities, policies, known, warehouses, housing,
                         Diplomacy(frozenset(world.country_regions) | frozenset(world.city_states)))
    state.warehouse_modules = {h: {f'warehouse:{h}': (warehouses[h] - units(300)) // units(500)} for h in world.hubs}
    state.resource_atlas = dict(world.hidden_resources)
    state.audit()
    return state
