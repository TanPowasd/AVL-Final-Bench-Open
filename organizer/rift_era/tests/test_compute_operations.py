import unittest
from types import SimpleNamespace as NS
from organizer.rift_era.engine.assets import Asset
from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.clock import ClockConfig
from organizer.rift_era.engine.economy_state import OperatingWorld
from organizer.rift_era.engine.initialization import initialize, Facility, InitialLinePolicy
from organizer.rift_era.engine.production import Line, Recipe
from organizer.rift_era.engine.numeric import Q
from organizer.rift_era.engine.errors import RuleViolation
from organizer.rift_era.engine.world import generate_candidate
from organizer.rift_era.engine import compute_operations as module
from organizer.rift_era.engine.economy import opening_economy


class ComputeOperationsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.catalog,cls.world=Catalog(),generate_candidate(1000)

    def initial(self):
        initial=initialize(self.catalog,self.world)
        for h,rid,good in (('n0-h0','P109','accelerator'),('n0-h1','P119','accelerator_g3')):
            fid='test-dc:'+h;lid=fid+'/line'
            initial.assets[fid]=Asset(fid,'n0',h,{})
            initial.assets[lid]=Asset(lid,'n0',h,{good:50000000})
            initial.facilities[fid]=Facility(fid,'n0',h,'S5','数据中心',(lid,))
            initial.lines[lid]=Line(lid,'n0',h,Recipe.from_catalog(self.catalog,rid))
            initial.line_policies[lid]=InitialLinePolicy(1000,None)
        return initial

    def fixture(self):
        state=OperatingWorld(self.catalog,self.initial())
        state.research['n0'].known.update({'AI01','AI02','AI04'})
        compute=state.compute
        return state,compute

    @staticmethod
    def ctx(cycle,tick=0):return NS(seed=1000,cycle=cycle,tick=cycle*10+tick,clock=ClockConfig())

    def test_local_capacity_and_idle_load(self):
        state,compute=self.fixture()
        self.assertEqual(compute.capacity('n0','n0-h0',0),50000)
        self.assertEqual(compute.capacity('n0','n0-h1',0),400000)
        with self.assertRaises(RuleViolation):compute.validate('n0','n0-h0',{},50001,0)
        compute.reserve(self.ctx(0))
        self.assertFalse(compute.frame_power)
        self.assertFalse(compute.frame_labor)

    def test_real_received_power_limits_each_task_locally(self):
        state,compute=self.fixture();lid='test-dc:n0-h0/line'
        compute.deployments['n0','n0-h0']=module.Deployment('n0','n0-h0',{'research':25000})
        compute.training_requests['n0','n0-h0']=25000
        compute.reserve(self.ctx(0))
        self.assertEqual(compute.frame_labor[lid],200)
        state.frame.labor[lid]={'general_worker':200}
        loads=compute.power_demands()
        self.assertEqual(sorted((rank,power) for _,rank,power in loads.values()),[(4,5000),(5,5000)])
        for entity,(_,_,band) in compute.frame_centers.items():
            state.frame.power_received[entity]=2500 if band=='B4' else 0
        compute.receive(self.ctx(0))
        self.assertEqual(compute.effective['n0','n0-h0']['research'],1250)
        self.assertEqual(compute.effective['n0','n0-h0']['train'],0)
        self.assertNotIn(('n0','n0-h1'),compute.effective)

    def test_linear_lifetime_consumes_actual_capital_without_free_reinstall(self):
        state,compute=self.fixture();lid='test-dc:n0-h0/line'
        for cycle in range(400):
            self.assertEqual(compute.capacity('n0','n0-h0',cycle),50000-cycle*125)
            state.initial.inventory.materials.begin_tick(cycle*10+9)
            state.asset_lifecycle.begin_tick(cycle*10+9)
            compute.age(self.ctx(cycle,9))
            self.assertEqual(state.initial.assets[lid].book_nmU['accelerator'],50000000-(cycle+1)*125000)
        self.assertEqual(compute.capacity('n0','n0-h0',400),0)
        compute.installed(lid,{},400)
        self.assertEqual(compute.capacity('n0','n0-h0',400),0)
        state.audit()


    def test_real_economy_idle_center_has_no_load_and_training_uses_paid_b5(self):
        game=opening_economy(self.catalog,self.initial(),total_ticks=20,commerce=True,debug=False)
        state=game.state;lid='test-dc:n0-h0/line'
        game.step()
        self.assertNotIn(lid,state.frame.batches)
        self.assertFalse(any(e.startswith('compute:') for e in state.frame.power_demands))
        state.paused_lines.update(key for key,line in state.initial.lines.items()
            if key not in state.initial.plants and line.recipe.id not in module.DC_RECIPES
            and state.catalog.recipes[line.recipe.id]['facility'] not in ('农场','受控农场','水厂'))
        state.compute.training_requests['n0','n0-h0']=1000
        state.compute.training_prices_mU['n0','n0-h0']=50000
        actual=Q(0)
        for _ in range(19):
            game.step()
            actual+=state.compute.effective.get(('n0','n0-h0'),{}).get('train',0)
            self.assertNotIn(lid,state.frame.batches)
            for entity in state.compute.frame_centers:
                self.assertEqual(next(load.band for load in state.frame.power_loads if load.id==entity),'B5')
        self.assertGreater(actual,0)
        self.assertLessEqual(actual,1900)
        self.assertFalse(any(a.asset=='compute' for a in state.initial.inventory.balances()))
        state.audit()

    def test_training_cost_limit_includes_actual_staff_wages(self):
        state,compute=self.fixture();lid='test-dc:n0-h0/line'
        compute.training_requests['n0','n0-h0']=1000
        compute.training_prices_mU['n0','n0-h0']=1000 # 1 native per compute, not per electricity.
        compute.reserve(self.ctx(0))
        state.frame.labor[lid]={'general_worker':4}
        entity,=compute.frame_centers
        wage=state.wage_cost('n0',state.frame.labor[lid])/100
        expected=(1-wage)*Q(1,2) # G1 needs 2 electricity per compute.
        self.assertEqual(compute.bid_ru(entity),expected*state.rates['n0'])
        state.frame.power_received[entity]=73
        compute.receive(self.ctx(0))
        actual=compute.effective['n0','n0-h0']['train']
        energy_cost=73*compute.bid_ru(entity)/state.rates['n0']
        wages=state.wage_cost('n0',state.frame.worked[lid])
        self.assertLessEqual(energy_cost+wages,actual)
        self.assertGreater(wages,0)

    def test_wages_above_limit_do_not_admit_even_free_electricity(self):
        state,compute=self.fixture();lid='test-dc:n0-h0/line'
        compute.training_requests['n0','n0-h0']=1
        compute.training_prices_mU['n0','n0-h0']=1
        compute.reserve(self.ctx(0))
        state.frame.labor[lid]={'engineer':compute.frame_labor[lid]}
        entity,=compute.frame_centers
        self.assertIsNone(compute.training_electricity_limit(entity))
        self.assertEqual(compute.power_demands()[entity][2],0)
        self.assertEqual(compute.bid_ru(entity),0)

    def test_real_dispatch_keeps_training_wages_plus_energy_within_compute_price(self):
        from organizer.rift_era.engine.economy_state import WAGES
        game=opening_economy(self.catalog,self.initial(),total_ticks=20,commerce=True,debug=False)
        state=game.state;lid='test-dc:n0-h0/line'
        state.paused_lines.update(key for key,line in state.initial.lines.items()
            if key not in state.initial.plants and line.recipe.id not in module.DC_RECIPES
            and state.catalog.recipes[line.recipe.id]['facility'] not in ('农场','受控农场','水厂'))
        state.compute.training_requests['n0','n0-h0']=1000
        state.compute.training_prices_mU['n0','n0-h0']=10000
        completed=Q(0)
        for _ in range(20):
            rate=state.rates['n0'];wage_index=state.wage_indices['n0']
            game.step()
            amount=state.compute.effective.get(('n0','n0-h0'),{}).get('train',Q(0))
            entities=set(state.compute.frame_centers)
            electricity=sum((r.cost_ru_mU for r in state.frame.power_receipts if r.load in entities),Q(0))/rate
            wages=sum((q*WAGES[skill]*wage_index for skill,q in state.frame.worked.get(lid,{}).items()),Q(0))
            self.assertLessEqual(wages+electricity,10*amount)
            completed+=amount
        self.assertGreater(completed,0)
        state.audit()
