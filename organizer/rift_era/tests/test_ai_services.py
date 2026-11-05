import unittest
from unittest.mock import patch

from organizer.rift_era.engine.ai import Model
from organizer.rift_era.engine.ai_services import AIServiceCommands
from organizer.rift_era.engine.assets import Asset
from organizer.rift_era.engine.compute_operations import Deployment, DC_RECIPES
from organizer.rift_era.engine.consumption import Consumption
from organizer.rift_era.engine.determinism import fingerprint
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.economy_state import OperatingWorld
from organizer.rift_era.engine.errors import InvariantViolation, RuleViolation
from organizer.rift_era.engine.ledger import CashAccount
from organizer.rift_era.engine.numeric import Q
from organizer.rift_era.engine.public_systems import InstalledSystem
from organizer.rift_era.engine.research import LabOutput
from organizer.rift_era.tests import test_compute_operations as compute_fixture


class AIServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compute_fixture.ComputeOperationsTests.setUpClass();cls.helper=compute_fixture.ComputeOperationsTests();cls.catalog=cls.helper.catalog

    def initial(self):
        initial=self.helper.initial()
        for owner in ('n0','n1','n2'):
            initial.assets['radio:'+owner]=Asset('radio:'+owner,owner,owner+'-h0',{'communication_equipment':1000000})
        return initial

    def configure(self,state):
        for owner in ('n0','n1','n2'):
            state.research[owner].known.update({'AI01','AI02','AI03','AI04','AI06','AI07','AI09','C02'})
            host=next(f.id for f in state.initial.facilities.values() if f.owner==owner and f.hub==owner+'-h0')
            state.systems.instances['radio:'+owner]=InstalledSystem('radio:'+owner,owner,owner+'-h0',host,'radio_backbone',online=True)
            state.slot_use['radio:'+owner]='system:radio:'+owner
        state.ai.models['n0']['source']=Model('source','n0',0,Q(9000),Q(9000));state.ai.active['n0']='source'
        state.compute.deployments['n0','n0-h0']=Deployment('n0','n0-h0',{'market':1000})
        state.ai_services.begin_tick(self.helper.ctx(0))

    def fixture(self):
        state=OperatingWorld(self.catalog,self.initial());self.configure(state);return state

    def offer(self,qty=1000,price=1000):
        return {'id':'1','kind':'ai','op':'service_offer','qty_mU':qty,'price_ru_mU':price}

    def bid(self,seq=1,qty=1000,price=1000,use='research'):
        return {'id':str(seq),'kind':'ai','op':'service_bid','qty_mU':qty,'max_price_ru_mU':price,'use':use}

    def fund(self,state,owner,amount):
        state.initial.cash.transfer(CashAccount('world','pool','RU'),CashAccount(owner,'reserves','RU'),amount)

    def post(self,state,owner,order):
        AIServiceCommands(state).prepare(owner,order,self.helper.ctx(0)).commit()

    def test_actual_compute_country_priority_and_same_tick_research_effect(self):
        state=self.fixture();self.fund(state,'n1',1000)
        self.post(state,'n0',self.offer());self.post(state,'n1',self.bid())
        self.assertFalse(state.ai_services.events)
        state.compute.effective={('n0','n0-h0'):{'market':Q(100)}}
        state.ai_services.clear(self.helper.ctx(0))
        event,=state.ai_services.events
        self.assertFalse(state.ai_services.scoring_events())
        self.assertEqual((event.buyer,event.quantity_mU,event.paid_ru_mU),('n1',100,100))
        self.assertEqual(state.initial.cash.balance(CashAccount('n0','corp:tech','RU')).native_mU,100)
        self.assertEqual(state.initial.cash.balance(CashAccount('n1','ai_service:1','RU')).native_mU,900)
        outputs={'n1':{'lab':LabOutput('lab','n1-h0',1000)}}
        state.ai_deployment.research(outputs,10)
        self.assertEqual(state.ai_services.scoring_events(),(event,))
        self.assertEqual(outputs['n1']['lab'].research_mU,1200)
        self.assertFalse(state.ai.models['n1'])
        self.assertFalse(any(a.asset=='compute' for a in state.initial.inventory.balances()))
        state.audit()

    def test_no_power_or_no_communication_cannot_sell_remote_compute(self):
        state=self.fixture();self.fund(state,'n1',1000)
        self.post(state,'n0',self.offer());self.post(state,'n1',self.bid())
        state.ai_services.clear(self.helper.ctx(0))
        self.assertFalse(state.ai_services.events)
        state.systems.instances['radio:n0'].online=False
        state.compute.effective={('n0','n0-h0'):{'market':Q(100)}}
        state.ai_services.clear(self.helper.ctx(0,1))
        self.assertFalse(state.ai_services.events)
        self.assertEqual(state.initial.cash.balance(CashAccount('n0','corp:tech','RU')).native_mU,0)
        state.audit()

    def test_higher_country_limit_precedes_lower_limit_and_npc(self):
        state=self.fixture()
        for owner in ('n1','n2'):self.fund(state,owner,1000)
        self.post(state,'n0',self.offer(qty=100,price=1000))
        self.post(state,'n1',self.bid(qty=100,price=1000))
        self.post(state,'n2',self.bid(qty=100,price=2000))
        state.compute.effective={('n0','n0-h0'):{'market':Q(100)}}
        state.ai_services.clear(self.helper.ctx(0))
        self.assertEqual([(e.buyer,e.quantity_mU) for e in state.ai_services.events],[('n2',100)])
        state.audit()

    def test_npc_cash_and_cycle_quote_cap_are_finite(self):
        state=self.fixture();self.post(state,'n0',self.offer(qty=100,price=1000))
        pool=CashAccount('world','pool','RU')
        state.initial.cash.transfer(pool,CashAccount('world','test_sequestered','RU'),state.initial.cash.balance(pool).native_mU-37)
        state.compute.effective={('n0','n0-h0'):{'market':Q(100)}}
        state.ai_services.clear(self.helper.ctx(0))
        self.assertEqual(sum(e.quantity_mU for e in state.ai_services.events),37)
        self.assertEqual(state.initial.cash.balance(pool).native_mU,0)
        state.ai_services.clear(self.helper.ctx(0,1))
        self.assertFalse(state.ai_services.events)
        state.initial.cash.transfer(CashAccount('world','test_sequestered','RU'),pool,1000)
        state.ai_services.clear(self.helper.ctx(0,2))
        self.assertEqual(sum(e.quantity_mU for e in state.ai_services.events),63)
        self.assertEqual(state.ai_services.used['n0'],100)
        state.audit()

    def test_stable_invoice_remainder_is_independent_of_bid_splitting(self):
        outcomes=[]
        for parts in ((100,),(50,50)):
            state=self.fixture();self.fund(state,'n1',1000)
            self.post(state,'n0',self.offer(qty=100,price=333))
            for i,q in enumerate(parts,1):self.post(state,'n1',self.bid(seq=i,qty=q,price=333))
            state.compute.effective={('n0','n0-h0'):{'market':Q(100)}}
            state.ai_services.clear(self.helper.ctx(0))
            outcomes.append((state.initial.cash.balance(CashAccount('n0','corp:tech','RU')).native_mU,
                             state.ai_services.residuals['n1','n0'],sum(e.quantity_mU for e in state.ai_services.events)))
            state.audit()
        self.assertEqual(*outcomes)
        self.assertEqual(outcomes[0],(33,Q(3,10),100))

    def test_expiry_refunds_unused_budget_and_rejected_bid_is_pure(self):
        state=self.fixture();self.fund(state,'n1',1000)
        before=fingerprint(state.snapshot())
        with self.assertRaises(RuleViolation):self.post(state,'n1',self.bid(qty=1001))
        self.assertEqual(before,fingerprint(state.snapshot()))
        self.post(state,'n1',self.bid())
        state.ai_services.clear(self.helper.ctx(0,9))
        self.assertFalse(state.ai_services.bids)
        self.assertEqual(state.initial.cash.balance(CashAccount('n1','reserves','RU')).native_mU,1000)
        state.audit()

    def test_only_paid_nonhostile_single_service_delivery_gets_export_receipt(self):
        state=self.fixture();self.fund(state,'n1',1000)
        self.post(state,'n0',self.offer());self.post(state,'n1',self.bid())
        state.compute.effective={('n0','n0-h0'):{'market':Q(100)}}
        state.ai_services.clear(self.helper.ctx(0))
        event,=state.ai_services.events
        consumption=Consumption()
        consumption.advance(0,0,(),weights=state.catalog.weights,shortest=lambda a,b:0,services=(event,))
        expected=Q(100,1000)*state.catalog.weights['compute']*state.credit['n0']/20
        self.assertEqual(consumption.receipt_mean20('n0','export'),expected)
        before=fingerprint(consumption.snapshot())
        with self.assertRaises(InvariantViolation):
            consumption.advance(0,0,(),weights=state.catalog.weights,shortest=lambda a,b:0,services=(event,))
        self.assertEqual(before,fingerprint(consumption.snapshot()))
        from dataclasses import replace
        for e in (replace(event,use='cyber'),replace(event,paid_ru_mU=0),replace(event,buyer=event.seller)):
            c=Consumption();c.advance(0,0,(),weights=state.catalog.weights,shortest=lambda a,b:0,services=(e,))
            self.assertEqual(c.receipt_mean20('n0','export'),0)

    def test_imported_production_compute_saves_local_labor_without_model_copy(self):
        state=self.fixture();ctx=self.helper.ctx(0);self.fund(state,'n1',1000)
        lids=[lid for lid,line in state.initial.lines.items() if line.owner=='n1' and line.hub=='n1-h0'
              and state.ai_deployment.eligible(lid)][:2]
        self.assertEqual(len(lids),2)
        state.compute.deployments['n1','n1-h0']=Deployment('n1','n1-h0',{},tuple(lids))
        self.post(state,'n0',self.offer());self.post(state,'n1',self.bid(use='production'))
        state.ai_deployment.begin_tick(ctx)
        state.ai_deployment.plan_labor(ctx,{lid:{'general_worker':100} for lid in lids},{lid:100 for lid in lids})
        state.frame.labor.update({lid:{'general_worker':100} for lid in lids})
        state.compute.effective={('n0','n0-h0'):{'market':Q(100)}}
        state.ai_services.clear(ctx);state.ai_deployment.apply_services(ctx)
        worked=[state.ai_deployment.worked(lid,100,ctx)['general_worker'] for lid in lids]
        self.assertEqual(sum(worked),180)
        self.assertEqual(sum(e.quantity_mU for e in state.ai_services.scoring_events()),100)
        self.assertEqual(state.ai_deployment.held_at('n1-h0','general_worker'),20)
        self.assertFalse(state.ai.models['n1'])
        state.audit()

    def test_finance_discount_is_frozen_from_completed_cycle_and_paid_by_real_fx(self):
        state=self.fixture();ctx=self.helper.ctx(0)
        state.ai_deployment.begin_tick(ctx)
        state.compute.cycle_effective={('n0','n0-h0'):{'finance':Q(20000)}}
        self.assertEqual(state.ai_deployment.finance_discount('n0','n0-h0'),0)
        state.ai_deployment.end_cycle(self.helper.ctx(0,9))
        state.ai_deployment.begin_tick(self.helper.ctx(1))
        self.assertEqual(state.fx.fee_discounts['n0'],Q(15,100))
        from organizer.rift_era.engine.payments import FXRequest
        request=FXRequest('actual-discount',state.native('n0','treasury'),CashAccount('n0','reserves','RU'),10017,Q(1))
        state.fx.execute(1000,10,(request,))
        self.assertEqual(state.fx.last_fills['actual-discount'].fee_mU,17)
        self.assertEqual(state.fx.last_fills['actual-discount'].received_mU,10000)
        state.ai.suspended_until['n0']=20
        self.assertEqual(state.ai_deployment.finance_discount('n0','n0-h0'),0)

    def test_real_economy_delivers_only_actual_powered_market_compute(self):
        game=opening_economy(self.catalog,self.initial(),total_ticks=10,commerce=True,debug=False)
        state=game.state;self.configure(state);self.fund(state,'n1',1000)
        state.paused_lines.update(lid for lid,line in state.initial.lines.items()
            if lid not in state.initial.plants and line.recipe.id not in DC_RECIPES
            and state.catalog.recipes[line.recipe.id]['facility'] not in ('农场','受控农场','水厂'))
        delivered=0;paid_delivered=0
        with patch('organizer.rift_era.engine.ai_operations.uniform',return_value=.999):
            for tick in range(10):
                orders={'n0':[self.offer()],'n1':[self.bid()]} if tick==0 else {}
                game.step(orders)
                events=state.ai_services.events
                quantity=sum(e.quantity_mU for e in events)
                self.assertLessEqual(quantity,state.compute.effective.get(('n0','n0-h0'),{}).get('market',0))
                delivered+=quantity
                paid_delivered+=sum(e.quantity_mU for e in state.ai_services.scoring_events() if e.paid_ru_mU)
        self.assertGreater(delivered,0)
        self.assertLessEqual(delivered,1000)
        self.assertFalse(state.ai_services.bids)
        self.assertFalse(any(a.asset=='compute' for a in state.initial.inventory.balances()))
        self.assertEqual(state.consumption.receipt_mean20('n0','export'),
                         Q(paid_delivered,1000)*state.catalog.weights['compute']*state.credit['n0']/20)
        state.audit()
