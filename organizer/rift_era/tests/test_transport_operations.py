import json
import unittest
from types import SimpleNamespace

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.clock import ClockConfig
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.world import generate_candidate
from organizer.rift_era.engine.ledger import Account, Inventory
from organizer.rift_era.engine.logistics import Corridor, Vehicle, Transport, TransitMode
from organizer.rift_era.engine.numeric import Q
from organizer.rift_era.engine.transport_operations import TransportCommands
from organizer.rift_era.engine.errors import RuleViolation
from organizer.rift_era.engine.determinism import fingerprint


class TransportMotionTests(unittest.TestCase):
    def fixture(self, good='food', quantity=3000):
        inv = Inventory()
        inv.initialize(Account('A', 'a', 'available', good), quantity)
        inv.initialize(Account('A', 'a', 'available', 'fuel'), 10000)
        inv.enable_provenance()
        vehicle = Vehicle('v', 'A', 'a', 'vehicle', 15000, Q(8), Q(60))
        t = Transport(inv, {'ab': Corridor('ab', 'a', 'b', 8, 30000)}, {'v': vehicle})
        return inv, t

    def test_partial_unload_keeps_only_remainder_on_vehicle(self):
        inv, t = self.fixture()
        s = t.launch('s', 'A', 'v', 'food', 3000, 'a', 'b', ('ab',), 0)
        t.advance(1, 's', entry_granted=True, free_capacity_mU=1000)
        self.assertEqual(s.mode, TransitMode.WAIT_UNLOAD)
        self.assertEqual(s.quantity_mU, 2000)
        self.assertEqual(inv.balance(Account('A', 'b', 'available', 'food')), 1000)
        t.unload('s', 500, bucket='outdoor')
        self.assertEqual(s.quantity_mU, 1500)
        self.assertEqual(inv.balance(Account('A', 'b', 'outdoor', 'food')), 500)
        self.assertEqual(t.decay('s'), 75)
        self.assertEqual(s.quantity_mU, 1425)
        t.unload('s', 1425)
        self.assertTrue(s.returning)
        t.advance(2, 's', entry_granted=True, free_capacity_mU=0)
        self.assertEqual(t.vehicles['v'].hub, 'a')
        self.assertEqual(inv.balance(Account('A', 'a', 'available', 'fuel')), 9280)
        t.audit(); inv.audit()
        inv.materials.audit_virtual(t.virtual_stock())

    def test_whole_goods_never_split_on_unload(self):
        inv, t = self.fixture('vehicle', 2000)
        s = t.launch('s', 'A', 'v', 'vehicle', 2000, 'a', 'b', ('ab',), 0,
                     cargo_weight=Q(5), indivisible=True)
        t.advance(1, 's', entry_granted=True, free_capacity_mU=7500, cargo_weight=Q(5))
        self.assertEqual(s.quantity_mU, 1000)
        self.assertEqual(inv.balance(Account('A', 'b', 'available', 'vehicle')), 1000)
        self.assertEqual(t.decay('s'), 0)
        t.audit(); inv.audit()

    def test_prepare_launch_has_no_mutation_and_failure_is_atomic(self):
        inv, t = self.fixture()
        before = fingerprint(inv.snapshot())
        commit = t.launch('s', 'A', 'v', 'food', 3000, 'a', 'b', ('ab',), 0, prepare_only=True)
        self.assertEqual(before, fingerprint(inv.snapshot()))
        self.assertFalse(t.shipments)
        self.assertIsNone(t.vehicles['v'].assigned)
        commit()
        snapshot = fingerprint(inv.snapshot())
        with self.assertRaises(RuleViolation):
            t.launch('other', 'A', 'v', 'food', 1, 'a', 'b', ('ab',), 0)
        self.assertEqual(snapshot, fingerprint(inv.snapshot()))


class CargoCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog, cls.world = Catalog(), generate_candidate(1000)

    def fixture(self, ticks=30):
        return opening_economy(self.catalog, initialize(self.catalog, self.world), total_ticks=ticks,
                               commerce=True, debug=False)

    def order(self, **change):
        return dict({'id': '1', 'kind': 'logistics', 'op': 'route_shipment',
                     'origin_hub': 'n0-h0', 'dest_hub': 'n0-h1',
                     'corridor_path': ['road:n0-h0:n0-h1'], 'good': 'stone',
                     'qty_mU': 6000, 'carrier_recipe': 'P036'}, **change)

    def context(self, tick=0):
        return SimpleNamespace(tick=tick, seed=1000, clock=ClockConfig(), cycle=tick // 10)

    def test_command_custody_is_atomic_and_cannot_sell_carrier_as_cargo(self):
        game = self.fixture()
        state, inv = game.state, game.state.initial.inventory
        handler = TransportCommands(state)
        before = fingerprint(state.snapshot())
        prepared = handler.prepare('n0', self.order(), self.context())
        self.assertEqual(before, fingerprint(state.snapshot()))
        prepared.commit()
        self.assertEqual(inv.balance(Account('n0', 'n0-h0', 'available', 'vehicle')), 2000)
        self.assertEqual(inv.balance(Account('n0', 'n0-h0', 'locked_haul:haul:n0:1', 'stone')), 6000)
        state.audit()
        before = fingerprint(state.snapshot())
        with self.assertRaises(RuleViolation):
            handler.prepare('n0', self.order(id='2', qty_mU=1000000), self.context())
        self.assertEqual(before, fingerprint(state.snapshot()))

    def test_tail_load_real_return_and_fleet_reuse_in_tick_pipeline(self):
        game = self.fixture()
        state = game.state
        game.step({'n0': [self.order()]})
        self.assertEqual(state.command_streams['n0'].recent[0].status, 'ACCEPTED')
        job = state.cargo.hauls['haul:n0:1']
        self.assertEqual(job.remaining_mU, 3000)
        self.assertEqual(state.cargo.admitted_mU['road:n0-h0:n0-h1'], 3000)
        vehicle = state.cargo.vehicles[job.vehicle_ids[0]]
        self.assertIsNone(vehicle.hub)
        for _ in range(19):
            game.step()
        self.assertEqual(job.status, 'COMPLETED')
        self.assertEqual(job.serial, 2)
        self.assertEqual(vehicle.hub, 'n0-h0')
        self.assertIsNone(vehicle.assigned)
        self.assertFalse(state.cargo.transport.shipments)
        self.assertEqual(state.initial.inventory.balance(vehicle.physical_account), 1000)
        state.audit()
        json.dumps(state.observations['n0'])

    def test_cancel_refunds_waiting_cargo_without_teleporting_current_trip(self):
        game = self.fixture()
        game.step({'n0': [self.order()]})
        state = game.state
        job = state.cargo.hauls['haul:n0:1']
        vehicle = state.cargo.vehicles[job.vehicle_ids[0]]
        state.cargo.cancel('n0', '1').commit()
        self.assertEqual(job.remaining_mU, 0)
        self.assertIsNotNone(vehicle.assigned)
        self.assertIsNone(vehicle.hub)
        for _ in range(10):
            game.step()
        self.assertEqual(job.serial, 1)
        self.assertEqual(job.status, 'COMPLETED')
        self.assertEqual(vehicle.hub, 'n0-h0')
        state.audit()

    def test_manual_carrier_shares_labor_and_cannot_exceed_manual_subcapacity(self):
        game = self.fixture(10)
        game.step({'n0': [self.order(carrier_recipe='manual', qty_mU=1600)]})
        state = game.state
        self.assertEqual(state.command_streams['n0'].recent[0].status, 'ACCEPTED')
        self.assertLessEqual(state.cargo.admitted_mU['road:n0-h0:n0-h1'], 800)
        entities = [e for e in state.frame.labor if e.startswith('haul_labor:')]
        self.assertEqual(len(entities), 2)
        for e in entities:
            self.assertLessEqual(sum(state.frame.labor[e].values()), 100)
        for _ in range(9):
            game.step()
        state.audit()

    def test_simultaneous_departures_share_capacity_independent_of_container_order(self):
        snapshots = []
        for reverse in (False, True):
            state = self.fixture().state
            handler = TransportCommands(state)
            for cid in ('1', '2'):
                handler.prepare('n0', self.order(id=cid, qty_mU=3000), self.context()).commit()
            if reverse:
                state.cargo.hauls = dict(reversed(tuple(state.cargo.hauls.items())))
                state.cargo.vehicles = dict(reversed(tuple(state.cargo.vehicles.items())))
                state.cargo.transport.vehicles = state.cargo.vehicles
            state.cargo.advance(self.context())
            self.assertEqual(sorted(s.quantity_mU for s in state.cargo.transport.shipments.values()), [1500, 1500])
            self.assertEqual(state.cargo.admitted_mU['road:n0-h0:n0-h1'], 3000)
            state.audit()
            snapshots.append(fingerprint(state.snapshot()))
        self.assertEqual(*snapshots)

    def test_repeated_manual_orders_share_the_800_mU_subquota(self):
        state = self.fixture().state
        handler = TransportCommands(state)
        for cid in ('1', '2'):
            handler.prepare('n0', self.order(id=cid, qty_mU=800, carrier_recipe='manual'), self.context()).commit()
        for entity, owner, hub, labor in state.cargo.labor_demands():
            state.frame.labor[entity] = {'general_worker': labor}
        state.cargo.advance(self.context())
        self.assertEqual(sorted(s.quantity_mU for s in state.cargo.transport.shipments.values()), [400, 400])
        state.audit()

    def test_outdoor_space_is_finite_and_losses_are_not_consumption(self):
        game = self.fixture()
        state, ops = game.state, game.state.cargo
        # Isolated component genesis: no industry step runs with this capacity.
        state.initial.warehouse_mU['n0-h1'] = 1000
        inv = state.initial.inventory
        for a, q in tuple(inv.balances().items()):
            if a.location == 'n0-h1':
                inv.transfer(a, Account(a.owner, 'fixture-vault', a.bucket, a.asset), q)
        TransportCommands(state).prepare('n0', self.order(qty_mU=3000), self.context()).commit()
        for tick in range(5):
            ops.advance(self.context(tick))
        s, = ops.transport.shipments.values()
        self.assertEqual(s.quantity_mU, 1800)
        self.assertEqual(inv.balance(Account('n0', 'n0-h1', 'available', 'stone')), 1000)
        outdoor = Account('n0', 'n0-h1', 'outdoor', 'stone')
        self.assertEqual(inv.balance(outdoor), 200)
        ops.decay_outdoor()
        self.assertEqual(inv.balance(outdoor), 190)
        ops.audit(); inv.audit()


class CorridorConstructionTests(unittest.TestCase):
    setUpClass = classmethod(CargoCommandTests.setUpClass.__func__)
    fixture = CargoCommandTests.fixture
    context = CargoCommandTests.context
    # Reuse only the fixture helpers; inherited gameplay tests remain in the
    # original class, avoiding duplicate full-economic regression runs.
    def test_upgrade_consumes_real_path_scaled_materials_before_capacity_changes(self):
        from organizer.rift_era.engine.commands import EngineCommands
        from organizer.rift_era.engine.economy_state import TickEconomy
        game = self.fixture()
        state = game.state
        handlers = EngineCommands(state)
        edge = 'road:n0-h0:n0-h1'
        cells = state.cargo.paths[edge]
        state.research['n0'].known.update({'L01', 'L02'})
        order = {'id': '1', 'kind': 'logistics', 'op': 'build_corridor',
                 'origin_hub': 'n0-h0', 'dest_hub': 'n0-h1', 'mode': 'road', 'level': 2,
                 'path_cells': list(cells)}
        before = fingerprint(state.snapshot())
        ready = handlers.prepare('n0', order, self.context())
        self.assertEqual(before, fingerprint(state.snapshot()))
        ready.commit()
        job, = state.work.jobs.values()
        self.assertEqual(job.materials_mU, {'steel': 7800, 'cement': 15600, 'stone': 10400})
        self.assertEqual(job.duration_ticks, 210)
        for tick in range(job.duration_ticks):
            self.assertEqual(state.cargo.corridors[edge].capacity_mU_cycle, 30000)
            state.initial.inventory.materials.begin_tick(tick)
            state.asset_lifecycle.begin_tick(tick)
            state.frame = TickEconomy()
            state.work.reserve(self.context(tick))
            state.work.advance(self.context(tick))
        self.assertFalse(state.work.jobs)
        self.assertEqual(state.cargo.corridors[edge].capacity_mU_cycle, 100000)
        self.assertEqual(state.cargo.levels[edge], 2)
        self.assertEqual(job.consumed_mU, job.materials_mU)
        self.assertFalse(state.initial.assets[job.target].under_construction)
        state.audit()
        with self.assertRaises(RuleViolation):
            handlers.prepare('n0', dict(order, id='2'), self.context(210))

    def test_bad_l1_path_rejects_without_creating_work_or_spending(self):
        from organizer.rift_era.engine.commands import EngineCommands
        state = self.fixture().state
        handlers = EngineCommands(state)
        state.research['n0'].known.update({'L01', 'L02'})
        cells = state.cargo.paths['road:n0-h0:n0-h1']
        order = {'id': '1', 'kind': 'logistics', 'op': 'build_corridor',
                 'origin_hub': 'n0-h0', 'dest_hub': 'n0-h1', 'mode': 'road', 'level': 2,
                 'path_cells': [cells[0], cells[-1]]}
        before = fingerprint(state.snapshot())
        with self.assertRaises(RuleViolation):
            handlers.prepare('n0', order, self.context())
        self.assertEqual(before, fingerprint(state.snapshot()))


if __name__ == '__main__':
    unittest.main()
