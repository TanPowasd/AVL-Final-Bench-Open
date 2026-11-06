import unittest
from dataclasses import replace
from organizer.rift_era.tests import test_transport_operations as cargo_fixture
from organizer.rift_era.engine.utility_network import UtilityNetwork, UtilityLink
from organizer.rift_era.engine.assets import Asset
from organizer.rift_era.engine.ledger import Account
from organizer.rift_era.engine.power import Supply, Load, dispatch
from organizer.rift_era.engine.numeric import Q
from organizer.rift_era.engine.determinism import fingerprint


class UtilityTests(unittest.TestCase):
    setUpClass = classmethod(cargo_fixture.CargoCommandTests.setUpClass.__func__)
    fixture = cargo_fixture.CargoCommandTests.fixture
    context = cargo_fixture.CargoCommandTests.context

    def network(self, mode='power', level=1):
        state = self.fixture().state
        state.utilities = UtilityNetwork(state)
        state.initial.assets['utility:test'] = Asset('utility:test', 'n0', 'n0-h0', {})
        link = UtilityLink('test', 'n0', 'n0-h0', 'n0-h1', mode, level,
            state.cargo.paths['road:n0-h0:n0-h1'], 'utility:test')
        state.utilities.links['test'] = link
        return state, link

    def test_grid_has_real_length_loss_capacity_and_paid_line_toll(self):
        state, link = self.network()
        edges = state.utilities.dispatch_edges()
        self.assertEqual(edges[0].capacity_mU, 6000)
        self.assertEqual(edges[0].survival, Q(961, 1000))
        result = dispatch(7, 0, (Supply('plant', link.a, 'n0', 10000, Q(1)),),
                          (Load('user', link.b, 'n1', 10000, 1, Q(5), Q(100000)),), edges)
        receipt, = result.receipts
        self.assertLess(receipt.received_mU, receipt.sent_mU)
        self.assertLessEqual(receipt.sent_mU, 6000)
        self.assertEqual(receipt.tolls_ru_mU[0][0], 'n0')
        self.assertEqual(receipt.cost_ru_mU, receipt.source_payment_ru_mU + receipt.tolls_ru_mU[0][1])
        state.research['n0'].known.add('E07')
        self.assertEqual(state.utilities.dispatch_edges()[0].survival, 1 - Q(9, 1000) * Q(26, 10))
        state.research['n0'].known.add('E09')
        self.assertEqual(state.utilities.dispatch_edges()[0].capacity_mU, 24000)
        state.initial.assets[link.asset_id].health_mU = 0
        self.assertFalse(state.utilities.dispatch_edges())

    def test_c15_ai_grid_dispatch_cuts_line_loss_by_the_better_endpoint(self):
        state, link = self.network()
        state.ai_deployment.grid_multiplier = lambda owner, hub: Q(4, 5) if hub == 'n0-h1' else Q(1)
        self.assertEqual(state.utilities.dispatch_edges()[0].survival, Q(961, 1000))  # no AI06
        state.research['n0'].known.add('AI06')
        self.assertEqual(state.utilities.dispatch_edges()[0].survival, 1 - Q(39, 1000) * Q(4, 5))

    def test_e12_undersea_power_link_needs_e12_superconductor_and_caps_loss(self):
        from organizer.rift_era.engine.commands import EngineCommands
        from organizer.rift_era.engine.errors import RuleViolation
        state = self.fixture().state
        state.utilities = UtilityNetwork(state)
        state.research['n0'].known.add('E02')
        world = state.initial.map
        path = list(state.cargo.paths['road:n0-h0:n0-h1'])
        terrain = bytearray(world.terrain_cells)
        terrain[path[5]] = 0                                     # fixture: one sea cell on the route
        original = world.terrain_cells
        object.__setattr__(world, 'terrain_cells', bytes(terrain))
        try:
            order = {'id': '93', 'kind': 'logistics', 'op': 'build_corridor', 'origin_hub': 'n0-h0',
                     'dest_hub': 'n0-h1', 'mode': 'power', 'level': 1, 'path_cells': path}
            with self.assertRaises(RuleViolation) as e:
                EngineCommands(state).prepare('n0', order, self.context())
            self.assertEqual(e.exception.code, 'ERR_TECH_LOCKED')
            with self.assertRaises(RuleViolation):
                EngineCommands(state).prepare('n0', {**order, 'mode': 'canal'}, self.context())
            state.research['n0'].known.add('E12')
            state.initial.inventory.transform({}, {Account('n0', 'n0-h0', 'available', 'superconductor'): 40000},
                                              reason='utility_e12_test_endowment')
            EngineCommands(state).prepare('n0', order, self.context()).commit()
            work, = state.work.jobs.values()
            self.assertEqual(work.materials_mU['superconductor'], 40000)
            state.initial.assets['utility:long'] = Asset('utility:long', 'n0', 'n0-h0', {})
            long_cells = tuple(path) + tuple(range(400))         # 400+ cells would lose 35%
            state.utilities.links['long'] = UtilityLink('long', 'n0', 'n0-h0', 'n0-h1', 'power', 1, long_cells, 'utility:long')
            self.assertEqual(state.utilities.power_rating(state.utilities.links['long'])[1], Q(92, 100))
        finally:
            object.__setattr__(world, 'terrain_cells', original)

    def test_canal_only_moves_real_water_with_loss_and_partial_pump_power(self):
        state, link = self.network('water')
        world = state.initial.map
        target = world.hubs[link.b].region
        source = world.hubs[link.a].region
        world.regions[target] = replace(world.regions[target], elevation=world.regions[source].elevation + 100)
        state.initial.warehouse_mU[link.b] = 10**9
        inv = state.initial.inventory
        water = Account('n0', link.a, 'available', 'water')
        before = inv.balance(water)
        state.utilities.reserve(self.context())
        # Snapshot includes physical reservations and is replay serializable.
        fingerprint(state.utilities.snapshot())
        amount = state.utilities.water_reserved[link.id]
        self.assertGreater(amount, 0)
        self.assertEqual(inv.balance(water), before - amount)
        state.utilities.advance(self.context())
        self.assertEqual(state.utilities.water_sent_mU.get(link.id, 0), 0)
        state.utilities.reserve(self.context(1))
        entity = next(iter(state.utilities.power_demands()))
        demand = state.utilities.power_demands()[entity][2]
        state.frame.power_received[entity] = demand // 2
        state.utilities.advance(self.context(1))
        sent = state.utilities.water_sent_mU[link.id]
        self.assertGreater(sent, 0); self.assertLess(sent, amount)
        self.assertEqual(sent, state.utilities.water_received_mU[link.id] + state.utilities.water_lost_mU[link.id])
        inv.audit(); state.utilities.audit()

    def test_utility_construction_prepares_real_materials_without_capacity(self):
        from organizer.rift_era.engine.commands import EngineCommands
        state = self.fixture().state
        state.utilities = UtilityNetwork(state)
        state.research['n0'].known.add('E02')
        handlers = EngineCommands(state)
        order = {'id': '91', 'kind': 'logistics', 'op': 'build_corridor', 'origin_hub': 'n0-h0',
                 'dest_hub': 'n0-h1', 'mode': 'power', 'level': 1,
                 'path_cells': list(state.cargo.paths['road:n0-h0:n0-h1'])}
        before = fingerprint(state.snapshot())
        prepared = handlers.prepare('n0', order, self.context())
        self.assertEqual(before, fingerprint(state.snapshot()))
        prepared.commit()
        work, = state.work.jobs.values()
        self.assertEqual(work.materials_mU, {'copper': 15600, 'steel': 10400, 'cement': 5200})
        self.assertEqual(work.duration_ticks, 210)
        self.assertFalse(state.utilities.dispatch_edges())
        from organizer.rift_era.engine.economy_state import TickEconomy
        for tick in range(work.duration_ticks):
            state.initial.inventory.materials.begin_tick(tick)
            state.asset_lifecycle.begin_tick(tick)
            state.frame = TickEconomy()
            state.work.reserve(self.context(tick)); state.work.advance(self.context(tick))
        self.assertFalse(state.work.jobs)
        self.assertEqual(state.utilities.dispatch_edges()[0].capacity_mU, 6000)
        state.asset_lifecycle.audit(); state.initial.inventory.audit()
        prepared = handlers.prepare('n0', {**order, 'id': '92', 'level': 2}, self.context(210))
        prepared.commit()
        upgrade, = state.work.jobs.values()
        self.assertEqual(upgrade.materials_mU, work.materials_mU)
        cash = state.initial.cash
        # Two physical builds require two actual service budgets. Move existing
        # national cash into treasury; do not mint a second construction grant.
        treasury = state.native('n0', 'treasury')
        donor = state.native('n0', 'household:n0-h0')
        cash.transfer(donor, treasury, min(1000000, cash.balance(donor).native_mU))
        state.initial.inventory.transform({}, {Account('n0', 'n0-h0', 'available', 'copper'): 20000}, reason='utility_upgrade_test_endowment')
        for tick in range(210, 210 + upgrade.duration_ticks):
            state.initial.inventory.materials.begin_tick(tick)
            state.asset_lifecycle.begin_tick(tick)
            state.frame = TickEconomy()
            state.work.reserve(self.context(tick)); state.work.advance(self.context(tick))
        self.assertEqual(state.utilities.dispatch_edges()[0].capacity_mU, 12000)
        state.asset_lifecycle.audit(); state.initial.inventory.audit()
