import unittest
from organizer.rift_era.tests import test_transport_operations as cargo_fixture
from organizer.rift_era.engine.transport_operations import TransportCommands, CorridorPolicyCommands
from organizer.rift_era.engine.logistics import Corridor, Vehicle
from organizer.rift_era.engine.ledger import Account
from organizer.rift_era.engine.numeric import Q
from organizer.rift_era.engine.errors import RuleViolation
from organizer.rift_era.engine.determinism import fingerprint


class MultimodalTests(unittest.TestCase):
    setUpClass = classmethod(cargo_fixture.CargoCommandTests.setUpClass.__func__)
    fixture = cargo_fixture.CargoCommandTests.fixture
    context = cargo_fixture.CargoCommandTests.context
    order = cargo_fixture.CargoCommandTests.order

    def test_train_requires_real_locomotive_and_rail_and_uses_rail_fuel(self):
        state = self.fixture().state
        ops = state.cargo
        road = 'road:n0-h0:n0-h1'
        ops.corridors['rail:test'] = Corridor('rail:test', 'n0-h0', 'n0-h1', 26, 320000, mode='rail')
        ops.levels['rail:test'] = 1
        ops.paths['rail:test'] = ops.paths[road]
        command = self.order(carrier_recipe='P037', corridor_path=['rail:test'], qty_mU=6000)
        before = fingerprint(state.snapshot())
        with self.assertRaises(RuleViolation):
            TransportCommands(state).prepare('n0', command, self.context())
        self.assertEqual(before, fingerprint(state.snapshot()))
        inv = state.initial.inventory
        inv.transform({}, {Account('n0', 'n0-h0', 'available', 'locomotive'): 1000}, reason='test_fixture_locomotive')
        TransportCommands(state).prepare('n0', command, self.context()).commit()
        job, = ops.hauls.values()
        vehicle = ops.vehicles[job.vehicle_ids[0]]
        self.assertEqual(vehicle.good, 'locomotive')
        self.assertEqual(inv.balance(vehicle.physical_account), 1000)
        self.assertEqual(ops.trip_fuel(vehicle, job), 195)
        self.assertEqual(ops.speed(vehicle, 'rail:test'), 20)
        with self.assertRaises(RuleViolation):
            TransportCommands(state).prepare('n0', self.order(id='2', carrier_recipe='P037'), self.context())
        state.audit()

    def test_air_requires_both_physical_terminals_and_correct_rate(self):
        state = self.fixture().state
        ops = state.cargo
        ops.corridors['air:test'] = Corridor('air:test', 'n0-h0', 'n0-h1', 26, 60000, mode='air')
        self.assertFalse(ops.permitted('n0', 'air:test'))
        # Existing commissioned buildings stand in for paid terminal assets.
        for hub in ('n0-h0', 'n0-h1'):
            aid = next(a.id for a in state.initial.assets.values() if a.hub == hub and not a.under_construction)
            ops.terminals[hub, 'air'] = aid
        self.assertTrue(ops.permitted('n0', 'air:test'))
        fingerprint(ops.snapshot())
        vehicle = Vehicle('test', 'n0', 'n0-h0', 'transport_aircraft', 20000, Q(45), Q(35, 2))
        self.assertEqual(ops.fuel_rate(vehicle, 'air:test'), Q(35, 2))
        self.assertEqual(ops.speed(vehicle, 'air:test'), 45)

    def test_policy_precheck_and_real_fee_transfer_conserve_cash(self):
        state = self.fixture().state
        ops = state.cargo
        eid = 'road:n0-h0:n0-h1'
        ready = CorridorPolicyCommands(state).prepare('n0', {'id': '8', 'kind': 'logistics',
            'op': 'corridor_policy', 'corridor_id': eid, 'target_nation': 'n1', 'allowed': True,
            'toll_ru_mU': 100}, self.context())
        self.assertFalse(ops.permitted('n1', eid))
        ready.commit()
        self.assertTrue(ops.permitted('n1', eid))
        before = state.initial.cash.balance(state.native('n1', 'treasury')).native_mU
        self.assertTrue(ops.pay_entry('n1', eid, 1500))
        self.assertEqual(state.initial.cash.balance(state.native('n1', 'treasury')).native_mU, before - 150)
        state.initial.cash.audit()

    def test_electric_train_waits_for_real_delivered_power(self):
        state = self.fixture().state
        ops = state.cargo
        road = 'road:n0-h0:n0-h1'
        ops.corridors['rail:test'] = Corridor('rail:test', 'n0-h0', 'n0-h1', 26, 320000, mode='rail')
        ops.levels['rail:test'] = 1
        ops.paths['rail:test'] = ops.paths[road]
        state.initial.inventory.transform({}, {Account('n0', 'n0-h0', 'available', 'locomotive'): 1000}, reason='test_fixture_locomotive')
        TransportCommands(state).prepare('n0', self.order(carrier_recipe='P037', corridor_path=['rail:test'],
            qty_mU=6000, propulsion='electric'), self.context()).commit()
        ops.reserve(self.context())
        ops.advance(self.context())
        ops.advance_electric(self.context())
        trip, = ops.transport.shipments.values()
        self.assertEqual(trip.progress_cells, 0)
        ops.reserve(self.context(1))
        ops.advance(self.context(1))
        entity = next(iter(ops.power_demands()))
        state.frame.power_received[entity] = ops.power_demands()[entity][2] // 2
        ops.advance_electric(self.context(1))
        self.assertEqual(trip.progress_cells, 10)
        self.assertEqual(trip.fuel_paid_mU, 0)
        ops.audit()
