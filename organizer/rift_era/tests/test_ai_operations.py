import unittest
from unittest.mock import patch
from types import SimpleNamespace as NS

from organizer.rift_era.tests import test_compute_operations as compute_fixture
from organizer.rift_era.engine.ai import Model
from organizer.rift_era.engine.ai_operations import AICommands
from organizer.rift_era.engine.determinism import fingerprint
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.errors import RuleViolation
from organizer.rift_era.engine.numeric import Q
from organizer.rift_era.engine.compute_operations import DC_RECIPES


class AIOperationsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compute_fixture.ComputeOperationsTests.setUpClass()
        cls.helper=compute_fixture.ComputeOperationsTests()
        cls.catalog=cls.helper.catalog

    def fixture(self,ticks=60):
        game=opening_economy(self.catalog,self.helper.initial(),total_ticks=ticks,commerce=True,debug=False)
        state=game.state
        state.research['n0'].known.update({'AI01','AI02'})
        # Explicit historical-data component genesis, not an in-game grant.
        state.ai.data['n0'].curated_human=Q(1000)
        state.paused_lines.update(lid for lid,line in state.initial.lines.items()
            if lid not in state.initial.plants and line.recipe.id not in DC_RECIPES
            and state.catalog.recipes[line.recipe.id]['facility'] not in ('农场','受控农场','水厂'))
        return game

    @staticmethod
    def order(seq=1):
        return {'id':str(seq),'kind':'ai','op':'train','hub_id':'n0-h0','alloc_compute_mU':1000,
                'cycles':5,'base_model':None,'max_price_mU':50000}

    def test_training_uses_actual_local_paid_compute_and_does_not_invent_stock(self):
        game=self.fixture();state=game.state
        with patch('organizer.rift_era.engine.ai_operations.uniform',return_value=.999):
            game.step({'n0':[self.order()]})
            actual=state.compute.effective.get(('n0','n0-h0'),{}).get('train',Q(0))
            for _ in range(49):
                game.step()
                actual+=state.compute.effective.get(('n0','n0-h0'),{}).get('train',Q(0))
        model,=state.ai.models['n0'].values()
        self.assertEqual(model.raw_compute,actual/1000)
        self.assertGreater(model.raw_compute,0)
        self.assertLessEqual(model.raw_compute,5)
        self.assertNotIn('n0',state.ai.training)
        self.assertNotIn('1',state.command_streams['n0'].plans)
        self.assertIsNone(state.ai.active['n0'])
        self.assertFalse(any(a.asset=='compute' for a in state.initial.inventory.balances()))
        state.audit()

    def test_cancel_checkpoints_half_actual_work_and_model_selection_is_explicit(self):
        game=self.fixture();state=game.state
        game.step({'n0':[self.order()]})
        for _ in range(4):game.step()
        before=state.ai.training['n0'].compute_mU
        game.step({'n0':[{'id':'2','kind':'cancel','op':'cancel_by_id','target_id':'1'}]})
        model,=state.ai.models['n0'].values()
        self.assertEqual(model.raw_compute,before/2000)
        self.assertIsNone(state.ai.active['n0'])
        handler=AICommands(state);ctx=self.helper.ctx(0)
        handler.prepare('n0',{'id':'3','kind':'ai','op':'model','active_model':model.id},ctx).commit()
        before=fingerprint(state.snapshot())
        with self.assertRaises(RuleViolation):
            handler.prepare('n0',{'id':'4','kind':'ai','op':'prune_model','model_id':model.id},ctx)
        self.assertEqual(before,fingerprint(state.snapshot()))
        state.audit()

    def test_local_budget_rejection_and_far_future_deadline_are_safe(self):
        state=self.fixture().state;handler=AICommands(state);ctx=self.helper.ctx(0)
        ctx.clock=self.helper.ctx(0).clock
        before=fingerprint(state.snapshot())
        with self.assertRaises(RuleViolation):handler.prepare('n0',self.order()|{'alloc_compute_mU':50001},ctx)
        self.assertEqual(before,fingerprint(state.snapshot()))
        handler.prepare('n0',self.order()|{'cycles':10**4299},ctx).commit()
        self.assertIsNone(state.ai.training['n0'].deadline_tick)
        fingerprint(state.snapshot()) # No multiplication into an unserializable deadline.
        state.audit()

    def test_incident_uses_frozen_model_and_algorithm_gain_waits_for_boundary(self):
        state=self.fixture().state
        model=Model('known-model','n0',0,Q(10000),Q(10000))
        state.ai.models['n0'][model.id]=model
        def draw(seed,stream,cycle,owner):return .3 if stream=='ai_incident_kind' else 0
        with patch('organizer.rift_era.engine.ai_operations.uniform',side_effect=draw):
            state.ai.settle(self.helper.ctx(0,9))
        changed=state.ai.models['n0'][model.id]
        self.assertEqual((changed.raw_compute,changed.effective_compute),(9000,9000))
        self.assertEqual(state.ai.algorithm['n0'],1)
        self.assertIn('AI05',state.pending_knowledge['n0'])
        state.promote_knowledge()
        self.assertEqual(state.ai.algorithm['n0'],Q(23,20))
        self.assertIn('AI05',state.research['n0'].known)
        state.audit()
