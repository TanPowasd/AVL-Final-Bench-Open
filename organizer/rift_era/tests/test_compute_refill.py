import unittest

from organizer.rift_era.engine.compute_refill import ComputeRefillCommands
from organizer.rift_era.engine.determinism import fingerprint
from organizer.rift_era.engine.errors import RuleViolation
from organizer.rift_era.engine.ledger import Account
from organizer.rift_era.engine.materials import Origin
from organizer.rift_era.engine.numeric import Q
from organizer.rift_era.engine.provenance import Use
from organizer.rift_era.tests import test_compute_operations as compute_fixture


class ComputeRefillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compute_fixture.ComputeOperationsTests.setUpClass();cls.helper=compute_fixture.ComputeOperationsTests()

    def fixture(self,cycles=8,stock=1000):
        state,compute=self.helper.fixture()
        for cycle in range(cycles):
            state.initial.inventory.materials.begin_tick(cycle*10+9)
            state.asset_lifecycle.begin_tick(cycle*10+9)
            compute.age(self.helper.ctx(cycle,9))
        state.initial.inventory.materials.begin_tick(cycles*10)
        state.asset_lifecycle.begin_tick(cycles*10)
        source=Account('n0','n0-h0','available','accelerator')
        state.initial.inventory.transform({}, {source:stock},reason='explicit_refill_test_endowment',
                                          origins={source:Origin('n7','n0-h0')})
        state.compute.reserve(self.helper.ctx(cycles))
        return state

    def command(self,qty=1,target='test-dc:n0-h0'):
        return {'id':'1','kind':'industry','op':'refill_accelerators','facility_id':target,'qty':qty}

    def test_actual_refill_preserves_old_installation_and_new_capital_origin(self):
        state=self.fixture();handler=ComputeRefillCommands(state);ctx=self.helper.ctx(8)
        line='test-dc:n0-h0/line';asset=state.initial.assets[line]
        self.assertEqual(state.compute.capacity('n0','n0-h0',8),49000)
        before=fingerprint(state.snapshot());prepared=handler.prepare('n0',self.command(),ctx)
        self.assertEqual(before,fingerprint(state.snapshot()))
        prepared.commit()
        row=state.compute.centers[line+':G1']
        self.assertEqual([(c.installed_cycle,c.quantity) for c in row.center.cohorts],[(0,50),(8,1)])
        self.assertEqual(state.compute.capacity('n0','n0-h0',8),50000)
        self.assertEqual(asset.book_nmU['accelerator'],50000000)
        capital=state.asset_lifecycle.account(asset,'accelerator')
        self.assertEqual(state.initial.inventory.materials.composition(capital)[Origin('n7','n0-h0')],1000)
        self.assertEqual([e.use for e in state.initial.inventory.materials.events],[Use.CONSTRUCTION])
        self.assertEqual(state.initial.inventory.balance(Account('n0','n0-h0','available','accelerator')),0)
        state.audit()

    def test_partial_old_health_is_not_healed_by_refill(self):
        state=self.fixture();line='test-dc:n0-h0/line'
        state.asset_lifecycle.damage(line,250,event_id='physical_test_damage')
        ComputeRefillCommands(state).prepare('n0',self.command(),self.helper.ctx(8)).commit()
        self.assertEqual(state.initial.assets[line].health_mU,750)
        self.assertEqual(state.compute.capacity('n0','n0-h0',8),37500)
        state.audit()

    def test_insufficient_space_stock_and_wrong_generation_are_pure(self):
        for cycles,stock,qty in ((7,1000,1),(8,999,1),(8,1000,2)):
            state=self.fixture(cycles,stock);before=fingerprint(state.snapshot())
            with self.assertRaises(RuleViolation):
                ComputeRefillCommands(state).prepare('n0',self.command(qty),self.helper.ctx(cycles))
            self.assertEqual(before,fingerprint(state.snapshot()))
        state=self.fixture(stock=0)
        other=Account('n0','n0-h0','available','accelerator_g3')
        state.initial.inventory.transform({}, {other:1000},reason='explicit_test_endowment')
        with self.assertRaises(RuleViolation):
            ComputeRefillCommands(state).prepare('n0',self.command(),self.helper.ctx(8))

    def test_same_cycle_refills_merge_only_the_new_cohort(self):
        state=self.fixture(cycles=16,stock=2000);handler=ComputeRefillCommands(state)
        for seq in (1,2):handler.prepare('n0',self.command()|{'id':str(seq)},self.helper.ctx(16)).commit()
        row=state.compute.centers['test-dc:n0-h0/line:G1']
        self.assertEqual([(c.installed_cycle,c.quantity) for c in row.center.cohorts],[(0,50),(16,2)])
        self.assertEqual(state.compute.capacity('n0','n0-h0',16),50000)
        state.audit()
