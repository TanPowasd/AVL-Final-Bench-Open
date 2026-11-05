import unittest
from organizer.rift_era.tests import test_transport_operations as cargo_fixture
from organizer.rift_era.engine.intermodal_operations import IntermodalOperations
from organizer.rift_era.engine.transport_operations import TransportCommands
from organizer.rift_era.engine.logistics import Corridor
from organizer.rift_era.engine.ledger import Account
from organizer.rift_era.engine.determinism import fingerprint


class RelayTests(unittest.TestCase):
    setUpClass = classmethod(cargo_fixture.CargoCommandTests.setUpClass.__func__)
    fixture = cargo_fixture.CargoCommandTests.fixture
    context = cargo_fixture.CargoCommandTests.context

    def setup_relay(self):
        state = self.fixture().state
        state.intermodal = IntermodalOperations(state)
        cargo = state.cargo
        cargo.corridors['rail:relay'] = Corridor('rail:relay', 'n0-h1', 'n0-h2', 10, 10000, mode='rail')
        cargo.levels['rail:relay'] = 1
        cargo.paths['rail:relay'] = state.cargo.paths['road:n0-h1:n0-h2']
        # Plenty of real receiving warehouse room, while movement still uses
        # each actual edge's finite admission and locally stocked fuel.
        state.initial.warehouse_mU['n0-h1'] = 10**9
        state.initial.warehouse_mU['n0-h2'] = 10**9
        for good, amount in (('locomotive', 1000), ('fuel', 100000)):
            state.initial.inventory.transform({}, {Account('n0', 'n0-h1', 'available', good): amount}, reason='relay_test_endowment')
        return state

    def order(self, **extra):
        return {**{'id': '81', 'kind': 'logistics', 'op': 'route_shipment', 'origin_hub': 'n0-h0',
            'dest_hub': 'n0-h2', 'corridor_path': ['road:n0-h0:n0-h1', 'rail:relay'],
            'good': 'stone', 'qty_mU': 6000, 'carrier_recipe': 'auto'}, **extra}

    def test_prepare_pure_and_intermediate_stock_never_becomes_available(self):
        state = self.setup_relay()
        before = fingerprint(state.snapshot())
        prepared = TransportCommands(state).prepare('n0', self.order(), self.context())
        self.assertEqual(before, fingerprint(state.snapshot()))
        prepared.commit()
        plan = state.intermodal.plans['n0:81']
        self.assertEqual(state.initial.inventory.balance(state.intermodal.account(plan, 0)), 6000)
        local_before = state.initial.inventory.balance(Account('n0', 'n0-h1', 'available', 'stone'))
        final_before = state.initial.inventory.balance(Account('n0', 'n0-h2', 'available', 'stone'))
        for tick in range(80):
            ctx = self.context(tick)
            state.intermodal.reserve(ctx)
            state.cargo.reserve(ctx)
            state.cargo.advance(ctx)
            state.intermodal.advance(ctx)
            state.cargo.audit(); state.initial.inventory.audit()
            self.assertEqual(state.initial.inventory.balance(Account('n0', 'n0-h1', 'available', 'stone')), local_before)
            if plan.completed:
                break
        self.assertTrue(plan.completed)
        self.assertEqual(state.initial.inventory.balance(Account('n0', 'n0-h2', 'available', 'stone')), final_before + 6000)
        self.assertEqual(sum(v.good == 'vehicle' for v in state.cargo.vehicles.values()), 1)
        self.assertEqual(sum(v.good == 'locomotive' for v in state.cargo.vehicles.values()), 1)
        self.assertTrue(all(v.hub in ('n0-h0', 'n0-h1') and v.assigned is None for v in state.cargo.vehicles.values()))

    def test_cancel_keeps_stock_at_its_physical_hub(self):
        state = self.setup_relay()
        TransportCommands(state).prepare('n0', self.order(), self.context()).commit()
        plan = state.intermodal.plans['n0:81']
        before = state.initial.inventory.balance(Account('n0', 'n0-h0', 'available', 'stone'))
        state.intermodal.cancel('n0', '81').commit()
        self.assertEqual(state.initial.inventory.balance(Account('n0', 'n0-h0', 'available', 'stone')), before + 6000)
        self.assertEqual(state.initial.inventory.balance(state.intermodal.account(plan, 0)), 0)

    def test_missing_downstream_train_and_blocked_edge_keep_real_hub_stock(self):
        state = self.setup_relay()
        inv = state.initial.inventory
        train = Account('n0', 'n0-h1', 'available', 'locomotive')
        inv.transfer(train, Account('n0', 'n0-h1', 'fixture_vault', 'locomotive'), 1000)
        TransportCommands(state).prepare('n0', self.order(), self.context()).commit()
        plan = state.intermodal.plans['n0:81']
        for tick in range(15):
            ctx = self.context(tick)
            state.intermodal.reserve(ctx); state.cargo.reserve(ctx); state.cargo.advance(ctx); state.intermodal.advance(ctx)
        self.assertEqual(inv.balance(state.intermodal.account(plan, 1)), 6000)
        self.assertFalse(any(v.good == 'locomotive' for v in state.cargo.vehicles.values()))
        state.cargo.corridors['rail:relay'].health_mU = 0
        inv.transfer(Account('n0', 'n0-h1', 'fixture_vault', 'locomotive'), train, 1000)
        state.intermodal.reserve(self.context(15))
        self.assertEqual(inv.balance(state.intermodal.account(plan, 1)), 6000)
        self.assertEqual(inv.balance(train), 1000)
        state.cargo.corridors['rail:relay'].health_mU = 1000
        state.intermodal.reserve(self.context(16))
        self.assertEqual(inv.balance(state.intermodal.account(plan, 1)), 0)
        self.assertEqual(inv.balance(train), 0)

    def test_multiple_incoming_batches_share_one_transfer_warehouse(self):
        state = self.setup_relay()
        inv = state.initial.inventory
        state.initial.warehouse_mU['n0-h1'] = 1000
        for account, quantity in tuple(inv.balances().items()):
            if account.location == 'n0-h1':
                inv.transfer(account, Account(account.owner, account.location, 'fixture_vault', account.asset), quantity)
        # Vault remains physically in this hub and consumes the warehouse too;
        # there is no empty room. Every arriving parcel must remain on its car.
        TransportCommands(state).prepare('n0', self.order(), self.context()).commit()
        for tick in range(8):
            ctx = self.context(tick)
            state.intermodal.reserve(ctx); state.cargo.reserve(ctx); state.cargo.advance(ctx); state.intermodal.advance(ctx)
        self.assertTrue(any(s.mode.value == 'WAIT_UNLOAD' for s in state.cargo.transport.shipments.values()))
        self.assertEqual(inv.balance(state.intermodal.account(state.intermodal.plans['n0:81'], 1)), 0)
        state.cargo.audit()

    def test_parent_order_runs_through_the_phase_pipeline(self):
        game = self.fixture(50)
        state = game.state
        state.initial.warehouse_mU['n0-h1'] = 10**9
        state.initial.warehouse_mU['n0-h2'] = 10**9
        for good, quantity in (('vehicle', 1000), ('fuel', 100000)):
            state.initial.inventory.transform({}, {Account('n0', 'n0-h1', 'available', good): quantity}, reason='relay_pipeline_endowment')
        before = state.initial.inventory.balance(Account('n0', 'n0-h2', 'available', 'stone'))
        order = self.order(corridor_path=['road:n0-h0:n0-h1', 'road:n0-h1:n0-h2'])
        game.step({'n0': [order]})
        self.assertEqual(state.command_streams['n0'].recent[0].status, 'ACCEPTED')
        for _ in range(49):
            game.step()
        self.assertEqual(state.initial.inventory.balance(Account('n0', 'n0-h2', 'available', 'stone')), before + 6000)
        self.assertFalse(state.intermodal.plans)
        state.audit()
