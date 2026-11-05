import unittest
from organizer.rift_era.engine.numeric import ceil
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
            'op': 'corridor_policy', 'corridor_id': eid, 'target_nation': 'n1', 'access_tier': 1,
            'toll_rate_bp': 1000}, self.context())
        self.assertFalse(ops.permitted('n1', eid))
        ready.commit()
        self.assertTrue(ops.permitted('n1', eid))
        # 06: fee = sum_g (q/1000) * w_cat[g] * 10 RU * bp / 10000.
        cargo = {'food': 1500}
        expected = ceil(Q(1500) * state.catalog.weights['food'] * 10 * 1000 / 10000)
        expected = ceil(Q(expected) / state.rates.get('n1', Q(1)))
        self.assertEqual(ops.entry_fee('n1', eid, 1500, cargo), expected)
        before = state.initial.cash.balance(state.native('n1', 'treasury')).native_mU
        self.assertTrue(ops.pay_entry('n1', eid, 1500, cargo))
        self.assertEqual(state.initial.cash.balance(state.native('n1', 'treasury')).native_mU, before - expected)
        # Empty return trips pay nothing; the rate is per target nation.
        self.assertEqual(ops.entry_fee('n1', eid, 0, {}), 0)
        self.assertEqual(ops.corridors[eid].toll_bp.get('n2', 0), 0)
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


class CorridorPolicyTests(unittest.TestCase):
    """06 logistics/corridor_policy: access_tier 0..3, toll_rate_bp 0..2000, per target nation."""
    setUpClass = classmethod(cargo_fixture.CargoCommandTests.setUpClass.__func__)
    fixture = cargo_fixture.CargoCommandTests.fixture
    context = cargo_fixture.CargoCommandTests.context
    EID = 'road:n0-h0:n0-h1'

    def policy(self, state, target, tier, bp=0, owner='n0', **extra):
        order = dict({'id': '9', 'kind': 'logistics', 'op': 'corridor_policy', 'corridor_id': self.EID,
                      'target_nation': target, 'access_tier': tier, 'toll_rate_bp': bp}, **extra)
        return CorridorPolicyCommands(state).prepare(owner, order, self.context())

    def test_schema_and_permission(self):
        state = self.fixture().state
        for bad in ({'access_tier': 4}, {'access_tier': -1}, {'toll_rate_bp': 2001}, {'toll_rate_bp': -1}):
            with self.assertRaises(RuleViolation) as caught:
                self.policy(state, 'n1', **{'tier': 1, **({'tier': bad['access_tier']} if 'access_tier' in bad else {}),
                                            'bp': bad.get('toll_rate_bp', 0)})
            self.assertEqual(caught.exception.code, 'ERR_SCHEMA')
        with self.assertRaises(RuleViolation) as caught:
            self.policy(state, 'n1', 1, allowed=True)
        self.assertEqual(caught.exception.code, 'ERR_SCHEMA')
        with self.assertRaises(RuleViolation) as caught:
            self.policy(state, 'n2', 1, owner='n1')
        self.assertEqual(caught.exception.code, 'ERR_NO_PERMISSION')

    def test_tiers_gate_commerce_military_supplies_and_troops(self):
        state = self.fixture().state
        ops = state.cargo
        military = next(g for g in sorted(state.export_controls.war_goods) if ops.military_cargo(g))
        self.assertFalse(ops.permitted('n1', self.EID, 'food'))
        self.policy(state, 'n1', 1).commit()
        self.assertTrue(ops.permitted('n1', self.EID, 'food'))
        self.assertFalse(ops.permitted('n1', self.EID, military))
        self.policy(state, 'n1', 2).commit()
        self.assertTrue(ops.permitted('n1', self.EID, military))
        self.assertLess(ops.corridors[self.EID].tier('n1'), 3)
        self.policy(state, 'n1', 3).commit()
        self.assertEqual(ops.corridors[self.EID].tier('n1'), 3)
        self.policy(state, 'n1', 0).commit()
        self.assertFalse(ops.permitted('n1', self.EID, 'food'))

    def test_rates_are_per_target_and_bp_scaled(self):
        state = self.fixture().state
        ops = state.cargo
        self.policy(state, 'n1', 1, 2000).commit()
        self.policy(state, 'n2', 1, 0).commit()
        cargo = {'stone': 6000}
        base = ops.toll_base_ru_mU(cargo)
        self.assertEqual(base, 6000 * state.catalog.weights['stone'] * 10)
        self.assertEqual(ops.entry_fee('n1', self.EID, 6000, cargo), ceil(Q(ceil(base * 2000 / 10000)) / state.rates.get('n1', Q(1))))
        self.assertEqual(ops.entry_fee('n2', self.EID, 6000, cargo), 0)
        self.assertEqual(ops.entry_fee('n0', self.EID, 6000, cargo), 0)

    def test_tightening_foreign_access_accrues_hostile_policy_events_until_restored(self):
        state = self.fixture().state
        ops = state.cargo
        ctx = lambda t: self.context(t)
        # Initial natural unreachability is not a player action.
        ops.record_policy_events(ctx(0))
        self.assertFalse(ops.action_cycles.get(0))
        # Granting then tightening is a hostile policy each tick, even without traffic.
        self.policy(state, 'n1', 2).commit()
        ops.record_policy_events(ctx(1))
        self.assertFalse(ops.action_cycles.get(0))
        self.policy(state, 'n1', 1).commit()
        ops.record_policy_events(ctx(2))
        ops.record_policy_events(ctx(3))
        self.assertEqual(ops.action_cycles[0]['n0'], Q(2, 1000))
        # Setting one's own tier or restoring the foreign tier stops accrual.
        self.policy(state, 'n0', 0).commit()
        self.policy(state, 'n1', 2).commit()
        ops.record_policy_events(ctx(4))
        self.assertEqual(ops.action_cycles[0]['n0'], Q(2, 1000))
        self.assertFalse(ops.corridors[self.EID].restricted)
        with self.assertRaises(Exception):
            ops.record_policy_events(ctx(4))
