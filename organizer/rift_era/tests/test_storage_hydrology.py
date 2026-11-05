import unittest
from dataclasses import replace
from fractions import Fraction as Q
from types import SimpleNamespace

from organizer.rift_era.engine.assets import Asset
from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.clock import ClockConfig
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.energy_storage import Store
from organizer.rift_era.engine.hydrology import Hydrology
from organizer.rift_era.engine.initialization import Facility, InitialLinePolicy, initialize
from organizer.rift_era.engine.kernel import TickContext
from organizer.rift_era.engine.ledger import Account, Inventory
from organizer.rift_era.engine.materials import Origin
from organizer.rift_era.engine.production import Line, Recipe
from organizer.rift_era.engine.world import generate_candidate


class StorageHydrologyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog, cls.world = Catalog(), generate_candidate(1000)

    def battery_fixture(self):
        initial = initialize(self.catalog, self.world)
        # Explicit component genesis: a paid S2 shell, empty slot and 8 batteries.
        fid, lid = 'fixture-storage', 'fixture-storage/line0'
        initial.facilities[fid] = Facility(fid, 'n0', 'n0-h0', 'S2', 'public', (lid,))
        initial.assets[fid] = Asset(fid, 'n0', 'n0-h0', {'steel': 16000000, 'cement': 16000000, 'basic_parts': 8000000})
        initial.assets[lid] = Asset(lid, 'n0', 'n0-h0', {})
        initial.lines[lid] = Line(lid, 'n0', 'n0-h0', Recipe('__idle__', {}, {}, frozenset(), frozenset()))
        initial.line_policies[lid] = InitialLinePolicy(1000, None)
        game = opening_economy(self.catalog, initial, total_ticks=40, debug=False, commerce=True)
        state = game.state
        state.slot_use[lid] = 'unassigned'
        state.research['n0'].known.add('E04')
        source = Account('n0', 'n0-h0', 'available', 'battery')
        initial.inventory.transform({}, {source: 8000}, reason='fixture_delivery', origins={source: Origin('n0', 'n0-h0')})
        return game, fid, lid

    def test_install_charge_and_discharge_use_actual_materials_and_dispatch(self):
        game, fid, lid = self.battery_fixture()
        state = game.state
        game.step({'n0': ({'id': 1, 'kind': 'industry', 'op': 'install_storage', 'facility_id': fid, 'storage_kind': 'battery'},)})
        self.assertFalse(state.storage.stores)
        self.assertEqual(state.initial.inventory.balance(Account('n0', 'n0-h0', 'available', 'battery')), 0)
        for _ in range(19):
            game.step()
        store = state.storage.stores[lid]
        self.assertEqual(store.soc_mU, 0)
        game.step({'n0': ({'id': 2, 'kind': 'logistics', 'op': 'storage_mode', 'facility_id': fid, 'mode': 'charge'},)})
        for _ in range(4):
            game.step()
        self.assertGreater(store.charged_mU, 0)
        charged = store.charged_mU
        before = store.soc_mU
        game.step({'n0': ({'id': 3, 'kind': 'logistics', 'op': 'storage_mode', 'facility_id': fid, 'mode': 'discharge'},)})
        for _ in range(4):
            game.step()
        self.assertGreater(store.discharged_mU, 0)
        self.assertEqual(store.charged_mU, charged)
        self.assertLess(store.soc_mU, before)
        self.assertEqual(charged, store.soc_mU + store.discharged_mU + store.conversion_loss_mU + store.spill_mU)
        state.audit()

    def test_auto_direction_uses_only_completed_cycle_history(self):
        game, fid, lid = self.battery_fixture()
        storage = game.state.storage
        store = Store(lid, 'n0', 'n0-h0', fid, 'battery', mode='auto')
        storage.stores[lid] = store
        storage.prices[store.hub].extend([Q(10)] * 9 + [Q(8)])
        storage.begin_tick(TickContext(1000, 0, ClockConfig(), {}))
        self.assertEqual(store.direction, 'charge')
        self.assertEqual(store.quote_ru, Q(17, 2))
        game.state.grid_prices_ru[store.hub] = Q(40)
        storage.begin_tick(TickContext(1000, 5, ClockConfig(), {}))
        self.assertEqual(store.direction, 'charge')
        self.assertEqual(store.quote_ru, Q(17, 2))

    def test_offline_storage_retains_energy_and_cannot_offer_it(self):
        game, fid, lid = self.battery_fixture()
        state = game.state
        store = Store(lid, 'n0', 'n0-h0', fid, 'battery', soc_mU=50000, charged_mU=50000, direction='discharge')
        state.storage.stores[lid] = store
        state.initial.assets[fid].maintenance_offline = True
        self.assertEqual(state.storage.capacity(store), 80000)
        self.assertEqual(state.storage.supplies(), [])

    def test_reservoirs_and_branches_conserve_water_and_drought_cannot_mint_flow(self):
        regions = {rid: SimpleNamespace(id=rid, terrain=terrain, adjacent=adj, elevation=height)
                   for rid, terrain, adj, height in [('up', 'mountain', ('down', 'sea'), 100),
                       ('down', 'plain', ('up', 'sea'), 10), ('sea', 'ocean', ('up', 'down'), 0)]}
        inv = Inventory()
        inv.seal()
        inv.enable_provenance()
        reservoir = SimpleNamespace(id='dam', kind='reservoir', hub='h', online=True)
        state = SimpleNamespace(initial=SimpleNamespace(map=SimpleNamespace(seed=7, attempt=0, regions=regions,
            hubs={'h': SimpleNamespace(region='up')}), inventory=inv, mines={}, assets={}),
            systems=SimpleNamespace(instances={'dam': reservoir}, availability=lambda _: Q(1), structural_factor=lambda _: Q(1)),
            endowments={'local_natural_water_per_cycle': 100}, global_emissions_mU=0,
            rain={'h': Q(1)}, water_used_mU={'h': 0}, natural_water_mU={'h': 0})
        hydro = Hydrology(state)
        hydro.natural_cycle_mU = {'up': 1000000, 'down': 0}
        inv.materials.begin_tick(1)
        hydro.advance(TickContext(7, 1, ClockConfig(), {}))
        self.assertGreater(hydro.reservoir_mU['dam'], 0)
        prior = hydro.reservoir_mU['dam']
        hydro.natural_cycle_mU = {'up': 0, 'down': 0}
        reservoir.online = False
        state.systems.structural_factor = lambda _: Q(1, 10)
        inv.materials.begin_tick(2)
        hydro.advance(TickContext(7, 2, ClockConfig(), {}))
        self.assertEqual(hydro.reservoir_mU['dam'], 20000)
        hydro.audit()
        reservoir.online = True
        for tick in range(3, 10):
            inv.materials.begin_tick(tick)
            hydro.advance(TickContext(7, tick, ClockConfig(), {}))
        self.assertLess(hydro.reservoir_mU['dam'], prior)
        self.assertEqual(hydro.natural_total_mU, hydro.ocean_total_mU + sum(hydro.reservoir_mU.values()))
        state.systems.instances.clear()
        inv.materials.begin_tick(11)
        hydro.advance(TickContext(7, 11, ClockConfig(), {}))
        self.assertEqual(hydro.natural_total_mU, hydro.ocean_total_mU)
        self.assertFalse(any(hydro.outflow_cycle_mU[r] for r in ('down',) if r != 'up'))
        hydro.audit()

    def test_storage_weighted_costs_remain_bounded_under_partial_mixing(self):
        from organizer.rift_era.engine.energy_storage import EnergyStorage
        from organizer.rift_era.engine.ledger import CASH_BOOK_SCALE
        store = Store('s', 'n0', 'n0-h0', 'host', 'battery', input_energy_mU=Q(100), charge_cost_ru_mU=Q(200))
        for i in range(2000):
            store.input_energy_mU += 1
            store.charge_cost_ru_mU += 2 + i % 3
            EnergyStorage.retain_basis(store, Q(100, 101))
        self.assertEqual(CASH_BOOK_SCALE % store.charge_cost_ru_mU.denominator, 0)
        self.assertEqual(CASH_BOOK_SCALE % store.input_energy_mU.denominator, 0)
        EnergyStorage.retain_basis(store, Q(0))
        self.assertEqual(store.input_energy_mU, 0)
        self.assertEqual(store.charge_cost_ru_mU, 0)


if __name__ == '__main__':
    unittest.main()
