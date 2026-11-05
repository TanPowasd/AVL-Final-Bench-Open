import unittest
from organizer.rift_era.tests import test_transport_operations as cargo_fixture
from organizer.rift_era.engine.passenger_operations import PassengerOperations
from organizer.rift_era.engine.determinism import fingerprint
from organizer.rift_era.engine.ledger import Account
from organizer.rift_era.engine.logistics import TransitMode
from organizer.rift_era.engine.logistics import Corridor
from organizer.rift_era.engine.errors import RuleViolation


class PassengerTests(unittest.TestCase):
    setUpClass = classmethod(cargo_fixture.CargoCommandTests.setUpClass.__func__)
    fixture = cargo_fixture.CargoCommandTests.fixture
    context = cargo_fixture.CargoCommandTests.context

    def order(self, **extra):
        return dict({'id': '11', 'kind': 'governance', 'op': 'relocate_population',
                     'origin_hub': 'n0-h0', 'dest_hub': 'n0-h1',
                     'skills_mU': {'general_worker': 500, 'skilled_worker': 100, 'engineer': 0},
                     'path': ['road:n0-h0:n0-h1'], 'allow_unhoused': True}, **extra)

    def test_prepare_is_pure_and_failed_housing_has_no_residue(self):
        state = self.fixture().state
        ops = PassengerOperations(state)
        state.passengers = ops
        before = fingerprint((state.snapshot(), ops.snapshot()))
        ops.prepare('n0', self.order(), self.context())
        self.assertEqual(before, fingerprint((state.snapshot(), ops.snapshot())))
        state.initial.housing_mU['n0-h1'] = 0
        before = fingerprint((state.snapshot(), ops.snapshot()))
        with self.assertRaises(RuleViolation):
            ops.prepare('n0', self.order(allow_unhoused=False), self.context())
        self.assertEqual(before, fingerprint((state.snapshot(), ops.snapshot())))

    def test_people_and_money_arrive_once_and_vehicle_returns(self):
        state = self.fixture().state
        ops = PassengerOperations(state)
        before = sum(p.total_mU for p in state.initial.populations.values())
        origin = state.initial.populations['n0-h0'].total_mU
        destination = state.initial.populations['n0-h1'].total_mU
        ops.prepare('n0', self.order(), self.context()).commit()
        ops.reserve(self.context())
        self.assertEqual(state.initial.populations['n0-h0'].total_mU, origin - 600)
        self.assertEqual(state.initial.populations['n0-h1'].total_mU, destination)
        record, = ops.shipments.values()
        for tick in range(1, 20):
            state.cargo.admitted_mU = {}
            ops.advance(self.context(tick))
            ops.audit(); state.initial.inventory.audit(); state.initial.cash.audit()
        self.assertEqual(state.initial.populations['n0-h1'].total_mU, destination + 600)
        self.assertEqual(sum(p.total_mU for p in state.initial.populations.values()), before)
        self.assertFalse(ops.shipments)
        vehicle, = ops.vehicles.values()
        self.assertEqual(vehicle.hub, 'n0-h0')
        self.assertEqual(state.initial.inventory.balance(vehicle.physical_account), 1000)

    def test_closed_edge_waits_without_adding_people(self):
        state = self.fixture().state
        ops = PassengerOperations(state)
        destination = state.initial.populations['n0-h1'].total_mU
        ops.prepare('n0', self.order(), self.context()).commit()
        ops.reserve(self.context())
        state.cargo.corridors['road:n0-h0:n0-h1'].passage_rights['n0'] = False
        ops.advance(self.context(1))
        trip, = ops.transport.shipments.values()
        self.assertEqual(trip.mode, TransitMode.WAIT_ENTRY)
        self.assertEqual(state.initial.populations['n0-h1'].total_mU, destination)

    def test_concurrent_plans_share_housing_and_large_plan_remains_resident(self):
        state = self.fixture().state
        ops = PassengerOperations(state)
        destination = state.initial.populations['n0-h1']
        state.initial.housing_mU['n0-h1'] = destination.total_mU + 600
        for cid in ('11', '12'):
            ops.prepare('n0', self.order(id=cid, allow_unhoused=False), self.context()).commit()
        ops.reserve(self.context())
        self.assertEqual(sorted(r.initial_people_mU for r in ops.shipments.values()), [300, 300])
        self.assertEqual(ops.housing_reserved['n0-h1'], 600)
        self.assertEqual(sum(sum(o['skills_mU'].values()) for _, o, _ in ops.pending.values()), 600)
        ops.audit()

    def test_no_truck_uses_reserved_manual_labor(self):
        state = self.fixture().state
        ops = PassengerOperations(state)
        inv = state.initial.inventory
        source = Account('n0', 'n0-h0', 'available', 'vehicle')
        inv.transfer(source, Account('n0', 'n0-h0', 'fixture_vault', 'vehicle'), inv.balance(source))
        ops.prepare('n0', self.order(), self.context()).commit()
        ops.reserve(self.context())
        vehicle, = ops.vehicles.values()
        self.assertEqual(vehicle.good, 'manual')
        entity, owner, hub, labor = next(iter(ops.labor_demands()))
        self.assertEqual((owner, hub, labor), ('n0', 'n0-h0', 100))
        ops.advance(self.context(1))
        trip, = ops.transport.shipments.values()
        self.assertEqual(trip.progress_cells, 0)
        state.frame.labor[entity] = {'general_worker': 100}
        ops.advance(self.context(2))
        self.assertEqual(trip.progress_cells, 4)

    def test_large_relocation_sends_real_batches_and_leaves_unsent_people_home(self):
        state = self.fixture().state
        ops = PassengerOperations(state)
        before = state.initial.populations['n0-h0'].total_mU
        skills = {'general_worker': 18000, 'skilled_worker': 0, 'engineer': 0}
        ops.prepare('n0', self.order(skills_mU=skills), self.context()).commit()
        self.assertEqual(state.initial.populations['n0-h0'].total_mU, before)
        ops.reserve(self.context())
        record, = ops.shipments.values()
        # G1 admission 3000 includes 1.5 payload per passenger.
        self.assertEqual(record.initial_people_mU, 2000)
        self.assertEqual(state.initial.populations['n0-h0'].total_mU, before - 2000)
        self.assertEqual(sum(next(iter(ops.pending.values()))[1]['skills_mU'].values()), 16000)

    def test_rail_passengers_use_real_train_capacity_and_fuel(self):
        state = self.fixture().state
        cargo = state.cargo
        cargo.corridors['rail:passengers'] = Corridor('rail:passengers', 'n0-h0', 'n0-h1', 26, 320000, mode='rail')
        cargo.levels['rail:passengers'] = 1
        cargo.paths['rail:passengers'] = cargo.paths['road:n0-h0:n0-h1']
        state.initial.inventory.transform({}, {Account('n0', 'n0-h0', 'available', 'locomotive'): 1000}, reason='test_passenger_train')
        ops = PassengerOperations(state)
        ops.prepare('n0', self.order(path=['rail:passengers']), self.context()).commit()
        ops.reserve(self.context())
        vehicle, = ops.vehicles.values()
        trip, = ops.transport.shipments.values()
        self.assertEqual(vehicle.good, 'locomotive')
        self.assertEqual(trip.fuel_reserved_mU, 195)
        ops.advance(self.context(1))
        self.assertEqual(trip.progress_cells, 20)
        ops.audit()
