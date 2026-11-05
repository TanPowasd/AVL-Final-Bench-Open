import unittest
from unittest.mock import patch

from organizer.rift_era.tests import test_compute_operations as compute_fixture
from organizer.rift_era.engine.ai import Model
from organizer.rift_era.engine.ai_deployment import DeploymentCommands, TASKS
from organizer.rift_era.engine.commands import EngineCommands
from organizer.rift_era.engine.compute_operations import Deployment
from organizer.rift_era.engine.determinism import fingerprint
from organizer.rift_era.engine.economy import opening_economy, Economy
from organizer.rift_era.engine.errors import RuleViolation
from organizer.rift_era.engine.ledger import CashAccount
from organizer.rift_era.engine.numeric import Q
from organizer.rift_era.engine.research import LabOutput


class DeploymentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compute_fixture.ComputeOperationsTests.setUpClass();cls.helper=compute_fixture.ComputeOperationsTests();cls.catalog=cls.helper.catalog

    def fixture(self):
        state,_=self.helper.fixture()
        state.research['n0'].known.update({'AI03','AI04','AI06','AI07','AI09'})
        state.ai.models['n0']['model']=Model('model','n0',0,Q(9000),Q(9000))
        state.ai.active['n0']='model'
        return state

    def order(self,seq=1,**values):
        return {'id':str(seq),'kind':'ai','op':'deploy','hub_id':'n0-h0','lines':[],
                **dict.fromkeys(TASKS,0),**values}

    def target(self,state):
        return next(lid for lid,l in state.initial.lines.items() if l.hub=='n0-h0' and l.recipe.id=='P002')

    def test_local_atomic_budget_and_target_validation(self):
        state=self.fixture();handler=DeploymentCommands(state);ctx=self.helper.ctx(0)
        for order in (self.order(research=50001),self.order(research=-1),self.order(lines=['test-dc:n0-h1/line']),
                      self.order(lines=['test-dc:n0-h0/line']),self.order(align_engineers_mU=True)):
            before=fingerprint(state.snapshot())
            with self.assertRaises(RuleViolation):handler.prepare('n0',order,ctx)
            self.assertEqual(before,fingerprint(state.snapshot()))
        state.compute.training_requests['n0','n0-h0']=50000
        with self.assertRaises(RuleViolation):handler.prepare('n0',self.order(research=1),ctx)

    def test_unpowered_compute_does_not_grant_research_or_automation(self):
        state=self.fixture();ctx=self.helper.ctx(0);lid=self.target(state)
        DeploymentCommands(state).prepare('n0',self.order(research=1000,production=1000,lines=[lid]),ctx).commit()
        state.ai_deployment.begin_tick(ctx)
        state.ai_deployment.plan_labor(ctx,{lid:{'general_worker':100}},{lid:100})
        state.compute.reserve(ctx);state.compute.receive(ctx);state.ai_deployment.receive(ctx)
        self.assertEqual(state.ai_deployment.actual.get(lid,0),0)
        outputs={'n0':{'lab':LabOutput('lab','n0-h0',1000)}}
        state.ai_deployment.research(outputs,10)
        self.assertEqual(outputs['n0']['lab'].research_mU,1000)

    def test_research_uses_same_hub_actual_compute_once(self):
        state=self.fixture()
        state.compute.effective={('n0','n0-h0'):{'research':Q(100)}}
        outputs={'n0':{'a':LabOutput('a','n0-h0',1000),'b':LabOutput('b','n0-h0',1000),
                       'far':LabOutput('far','n0-h1',1000)}}
        state.ai_deployment.research(outputs,10)
        self.assertEqual([outputs['n0'][k].research_mU for k in ('a','b','far')],[1200,1200,1000])

    def test_automation_cannot_rehire_displaced_people_in_same_cycle(self):
        state=self.fixture();ctx=self.helper.ctx(0);lid=self.target(state)
        DeploymentCommands(state).prepare('n0',self.order(production=1000,lines=[lid]),ctx).commit()
        state.ai_deployment.begin_tick(ctx)
        state.ai_deployment.plan_labor(ctx,{lid:{'general_worker':100}},{lid:100})
        state.frame.labor[lid]={'general_worker':100}
        state.compute.effective={('n0','n0-h0'):{'production':Q(100)}}
        state.ai_deployment.receive(ctx)
        worked=state.ai_deployment.worked(lid,100,ctx)
        self.assertEqual(worked,{'general_worker':80})
        self.assertEqual(state.ai_deployment.labor_supply('n0-h0','general_worker',100),80)
        state.ai_deployment.begin_tick(self.helper.ctx(0,1))
        self.assertEqual(state.ai_deployment.labor_supply('n0-h0','general_worker',100),80)
        state.ai_deployment.begin_tick(self.helper.ctx(1))
        self.assertEqual(state.ai_deployment.labor_supply('n0-h0','general_worker',100),100)

    def test_production_cap_rechecks_actual_compute_after_p1_ceiling(self):
        state=self.fixture();ctx=self.helper.ctx(0);lid=self.target(state)
        DeploymentCommands(state).prepare('n0',self.order(production=1000,lines=[lid]),ctx).commit()
        state.ai_deployment.begin_tick(ctx)
        state.ai_deployment.plan_labor(ctx,{lid:{'general_worker':100}},{lid:100})
        self.assertEqual(state.ai_deployment.planned_batch(lid,80,100,80),100)
        state.frame.labor[lid]={'general_worker':80}
        caps={lid:100};state.ai_deployment.cap_batches(caps)
        self.assertEqual(caps[lid],80)
        state.compute.effective={('n0','n0-h0'):{'production':Q(100)}}
        state.ai_deployment.receive(ctx)
        caps={lid:100};state.ai_deployment.cap_batches(caps)
        self.assertEqual(caps[lid],100)
        self.assertEqual(state.ai_deployment.worked(lid,100,ctx),{'general_worker':80})

    def test_alignment_requires_reserved_cash_before_accumulating_engineers(self):
        state=self.fixture();ctx=self.helper.ctx(0)
        DeploymentCommands(state).prepare('n0',self.order(align_engineers_mU=100),ctx).commit()
        entity,=state.ai_deployment.alignment_demands(ctx)
        state.frame.labor[entity]={'engineer':10}
        lock=CashAccount('n0','wage:'+entity,'n0')
        state.frame.wage_locks[entity]=lock
        state.ai_deployment.align_engineers(ctx)
        self.assertEqual(state.ai.align_accum['n0'],0)
        state.initial.cash.transfer(state.native('n0','treasury'),lock,160)
        state.ai_deployment.align_engineers(ctx)
        self.assertEqual(state.frame.worked[entity],{'engineer':5})
        self.assertEqual(state.ai.align_accum['n0'],Q(25,1000))

    def test_grid_effect_reads_previous_cycle_only(self):
        state=self.fixture();ctx=self.helper.ctx(0)
        state.ai_deployment.begin_tick(ctx)
        state.compute.cycle_effective={('n0','n0-h0'):{'grid':100000}}
        self.assertEqual(state.ai_deployment.grid_multiplier('n0','n0-h0'),1)
        state.ai_deployment.end_cycle(self.helper.ctx(0,9))
        state.ai_deployment.begin_tick(self.helper.ctx(1))
        state.compute.previous_effective=state.compute.cycle_effective
        self.assertEqual(state.ai_deployment.grid_multiplier('n0','n0-h0'),Q(9,10))
        self.assertEqual(state.ai_deployment.grid_multiplier('n0','n0-h1'),1)

    def test_real_deploy_replace_cancel_and_engineer_payroll(self):
        game=opening_economy(self.catalog,self.helper.initial(),total_ticks=20,commerce=True,debug=False)
        state=game.state;state.research['n0'].known.update({'AI03','AI04'})
        with patch('organizer.rift_era.engine.ai_operations.uniform',return_value=.999):
            game.step({'n0':[self.order(research=1000,align_engineers_mU=100)]})
            self.assertEqual(state.command_streams['n0'].recent[-1].status,'ACCEPTED')
            self.assertGreater(state.ai.align_accum['n0'],0)
            self.assertGreater(state.compute.effective['n0','n0-h0'].get('research',0),0)
            for _ in range(4):game.step()
            game.step({'n0':[self.order(seq=2,research=500,replaces_id='1')]})
            self.assertEqual(set(state.command_streams['n0'].plans),{'2'})
            for _ in range(4):game.step()
            game.step({'n0':[{'id':'3','kind':'cancel','op':'cancel_by_id','target_id':'2'}]})
            self.assertNotIn(('n0','n0-h0'),state.compute.deployments)
            self.assertFalse(state.command_streams['n0'].plans)
        state.audit()
