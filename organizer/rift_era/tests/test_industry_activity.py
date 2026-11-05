import unittest

from organizer.rift_era.engine.ai_operations import AICommands
from organizer.rift_era.engine.assets import Asset
from organizer.rift_era.engine.economy_state import OperatingWorld,TickEconomy
from organizer.rift_era.engine.energy_storage import Store
from organizer.rift_era.engine.ledger import CashAccount
from organizer.rift_era.engine.numeric import Q,floor
from organizer.rift_era.engine.power import PowerReceipt,Load
from organizer.rift_era.engine.production import ProductionResult
from organizer.rift_era.engine.provenance import Use
from organizer.rift_era.tests import test_compute_operations as compute_fixture


class IndustryActivityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compute_fixture.ComputeOperationsTests.setUpClass();cls.helper=compute_fixture.ComputeOperationsTests()

    def fixture(self):return self.helper.fixture()[0]

    def generator(self,state):
        return next(lid for lid,line in state.initial.lines.items() if line.owner=='n0' and lid in state.initial.plants)

    def test_c17_catalog_unit_conversion_paid_costs_and_no_cash_creation(self):
        state=self.fixture();state.rates['n0']=Q(2);book=state.industry_activity
        book.begin_tick(self.helper.ctx(0));gen=self.generator(state)
        load='municipal:n0-h0'
        state.frame.power_receipts=(PowerReceipt(gen+':dispatch',load,1000,1000,(),Q(0),()),)
        state.frame.power_received[load]=1000
        state.frame.power_loads=(Load(load,'n0-h0','n0',1000,1,Q(50),Q(100000)),)
        payer=state.native('n0','corp:energy')
        state.initial.cash.transfer(state.native('n0','treasury'),payer,3100)
        state.initial.cash.transfer(payer,state.native('n0','household:n0-h0'),3000)
        state.initial.cash.transfer(payer,state.native('n0','corp:manufacturing'),100)
        book.generator_input(gen,'coal',Q(100));book.wages(gen,3000);book.maintenance(gen,100)
        book.compute_frame(self.helper.ctx(0))
        state.rates['n0']=Q(4) # End-of-cycle rate is for NEXT operation, not a revaluation of past activity.
        cash=state.initial.cash.snapshot()
        book.finish_tick(self.helper.ctx(0,9))
        shadow=state.catalog.weights['electricity']-Q(1,10)*state.catalog.weights['coal']
        native=shadow*10*1000/2
        reference=max(Q(15,100)*native,native-3000)-100
        self.assertEqual(book.last_company_va['n0-energy'],shadow)
        self.assertEqual(book.last_company_value['n0-energy'],reference)
        self.assertEqual(book.mean20('n0-energy'),reference/20)
        self.assertEqual(state.initial.cash.snapshot(),cash)

    def test_extraction_and_virtual_research_outputs_cannot_create_positive_va(self):
        state=self.fixture();book=state.industry_activity;book.begin_tick(self.helper.ctx(0))
        mid=next(mid for mid,mine in state.initial.mines.items() if mine.owner=='n0')
        mine=state.extraction_lines[mid]
        book.production(mine,ProductionResult(mid,100,{mine.recipe.id:0},{},{'coal':1000000}))
        lab=next(line for line in state.initial.lines.values() if line.owner=='n0' and line.recipe.id=='P075')
        consumed={g:q*100 for g,q in lab.recipe.inputs.items()}
        book.production(lab,ProductionResult(lab.id,100,{},consumed,{'research_points':10**9}))
        book.finish_tick(self.helper.ctx(0,9))
        self.assertEqual(book.last_company_va.get('n0-mining',0),0)
        expected=-sum((Q(q,1000000)*state.catalog.weights[g] for g,q in consumed.items()),Q(0))
        self.assertEqual(book.last_company_va['n0-tech'],expected)
        self.assertFalse(book.rows[lab.id,'n0-tech'].outputs_mU)

    def test_allocated_but_unconsumed_compute_has_no_positive_flow_va(self):
        for real_run in (False,True):
            state=self.fixture();ctx=self.helper.ctx(0);book=state.industry_activity;book.begin_tick(ctx)
            if real_run:
                AICommands(state).prepare('n0',{'id':'1','kind':'ai','op':'train','hub_id':'n0-h0',
                    'alloc_compute_mU':1000,'cycles':5,'base_model':None,'max_price_mU':50000},ctx).commit()
            else:
                from organizer.rift_era.engine.compute_operations import Deployment
                state.compute.deployments['n0','n0-h0']=Deployment('n0','n0-h0',{'research':1000})
            state.compute.reserve(ctx)
            lid='test-dc:n0-h0/line';state.frame.labor[lid]={'general_worker':4}
            entity,=state.compute.frame_centers
            state.frame.power_received[entity]=200
            state.frame.power_receipts=(PowerReceipt(self.generator(state)+':dispatch',entity,200,200,(),Q(0),()),)
            state.compute.receive(ctx);book.compute_frame(ctx);state.ai.consume_compute(ctx)
            book.finish_tick(ctx)
            self.assertEqual(book.rows[lid,'n0-tech'].outputs_mU.get('compute',0),100 if real_run else 0)

    def test_two_storage_ring_cannot_create_net_va(self):
        initial=self.helper.initial()
        for sid in ('store-a','store-b'):initial.assets[sid]=Asset(sid,'n0','n0-h0',{'battery':8000000})
        state=OperatingWorld(self.helper.catalog,initial);book=state.industry_activity
        host=next(f.id for f in state.initial.facilities.values() if f.owner=='n0' and f.hub=='n0-h0')
        for sid in ('store-a','store-b'):
            state.storage.stores[sid]=Store(sid,'n0','n0-h0',host,'battery')
            state.slot_use[sid]='storage:'+sid
        gen=self.generator(state)
        for tick,source,target,quantity in ((0,gen,'store-a',1000),(1,'store-a','store-b',850),(9,'store-b','store-a',722)):
            ctx=self.helper.ctx(0,tick);state.frame=TickEconomy();book.begin_tick(ctx)
            for store in state.storage.stores.values():store.direction='idle'
            state.storage.stores[target].direction='charge'
            if source.startswith('store-'):state.storage.stores[source].direction='discharge'
            source_id=('storage:'+source if source.startswith('store-') else source)+':dispatch'
            target_id='storage:'+target
            state.frame.power_received[target_id]=quantity
            state.frame.power_receipts=(PowerReceipt(source_id,target_id,quantity,quantity,(),Q(0),()),)
            loads=(Load(target_id,'n0-h0','n0',quantity,5,Q(50),Q(100000)),)
            state.frame.power_loads=loads
            state.storage.settle_dispatch(ctx,{'storage:'+source:quantity} if source.startswith('store-') else {},loads)
            book.compute_frame(ctx)
            if tick==0:
                book.generator_input(gen,'coal',200);book.generator_input(gen,'water',200)
            book.finish_tick(ctx)
        expected=-Q(1,5)*(state.catalog.weights['coal']+state.catalog.weights['water'])
        self.assertEqual(book.last_company_va['n0-energy'],expected)
        self.assertLess(book.last_company_va['n0-energy'],0)
        state.storage.audit();state.initial.cash.audit()
