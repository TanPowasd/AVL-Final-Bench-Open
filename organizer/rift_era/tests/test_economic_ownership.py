import unittest

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.assets import Asset
from organizer.rift_era.engine.construction import WorkOrder
from organizer.rift_era.engine.economic_ownership import EconomicOwner, prepare_purchase, purchase_quote
from organizer.rift_era.engine.economy import Economy, opening_economy
from organizer.rift_era.engine.economy_state import OperatingWorld
from organizer.rift_era.engine.errors import InvariantViolation
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.ledger import Account, CashAccount, CashLedger, Inventory
from organizer.rift_era.engine.materials import Origin
from organizer.rift_era.engine.numeric import Q
from organizer.rift_era.engine.payments import Payments
from organizer.rift_era.engine.provenance import Use
from organizer.rift_era.engine.spot import RestingOrder, SpotExchange, Venue
from organizer.rift_era.engine.world import generate_candidate
from organizer.rift_era.tests import test_compute_operations as compute_fixture


class EconomicTitleTests(unittest.TestCase):
    def fixture(self,composition):
        inventory,cash=Inventory(),CashLedger()
        inventory.seal();inventory.enable_provenance(seed=7).begin_tick(0)
        for i,(origin,quantity) in enumerate(composition):
            account=Account('n0','h','part:'+str(i),'accelerator')
            inventory.transform({}, {account:quantity},reason='explicit_title_test_endowment',origins={account:origin})
            inventory.transfer(account,Account('n0','h','available','accelerator'),quantity)
        cash.initialize(CashAccount('n1','treasury','C'),1000);cash.seal()
        return inventory,cash

    def order(self,owner,seq,side,qty):
        return RestingOrder(f'{owner}/{seq}',str(seq),owner,'v',side,qty,1000,100,
            CashAccount(owner,'treasury','C'),Account(owner,'h','available' if side=='sell' else 'next_available','accelerator'))

    def test_restricted_sale_pays_the_allowed_origin_actual_company_owner(self):
        corp=EconomicOwner('n0','corp:tech')
        for parts in ((2,),(1,1)):
            goods,cash=self.fixture([(Origin('free','h',economic_owner=corp),1),(Origin('banned','h'),1)])
            exchange=SpotExchange(goods,cash);exchange.add_venue(Venue('v','h','accelerator','C',1000))
            for seq,q in enumerate(parts):exchange.prepare_order(self.order('n0',seq,'sell',q),tick=0).commit()
            exchange.prepare_order(self.order('n1',0,'buy',2),tick=0).commit()
            clearing=exchange.clear(7,1,'v',origin_allowed=lambda b,s,o,g:o!='banned')
            self.assertEqual(clearing.quantity_mU,1)
            self.assertEqual(cash.balance(CashAccount('n0','corp:tech','C')).native_mU,1)
            self.assertEqual(cash.balance(CashAccount('n0','treasury','C')).native_mU,0)
            bought=goods.materials.composition(Account('n1','h','next_available','accelerator'))
            self.assertEqual(bought,{Origin('free','h'):1})
            self.assertEqual(clearing.income_accounts,(('n0','corp:tech','C',1),))
            goods.audit();cash.audit();exchange.audit()

    def test_same_country_purchase_changes_company_title_to_government_title(self):
        corp=EconomicOwner('n0','corp:tech')
        goods,cash=self.fixture([(Origin('n0','h',economic_owner=corp),10)])
        cash.transfer(CashAccount('n1','treasury','C'),CashAccount('n0','treasury','C'),100)
        exchange=SpotExchange(goods,cash);exchange.add_venue(Venue('v','h','accelerator','C',1000))
        exchange.prepare_order(self.order('n0',0,'sell',10),tick=0).commit()
        exchange.prepare_order(self.order('n0',1,'buy',10),tick=0).commit()
        exchange.clear(7,1,'v')
        self.assertEqual(cash.balance(CashAccount('n0','corp:tech','C')).native_mU,10)
        self.assertEqual(cash.balance(CashAccount('n0','treasury','C')).native_mU,90)
        self.assertEqual(goods.materials.composition(Account('n0','h','next_available','accelerator')),{Origin('n0','h'):10})
        goods.audit();cash.audit();exchange.audit()

    def test_mixed_consumption_has_one_integer_debit_and_real_owners(self):
        goods,cash=self.fixture([(Origin('n0','h'),1),
            (Origin('n0','h',economic_owner=EconomicOwner('n0','corp:tech')),1)])
        source=Account('n0','h','available','accelerator')
        stock=goods.prepare_transform({source:2},{},reason='actual_title_use',use=Use.INDUSTRY)
        payer=CashAccount('n1','treasury','C');payments=Payments(cash)
        plan,recipients=prepare_purchase(payments,payer,stock.materials.events,{'accelerator':Q(3,2)},
            kind='actual_input',key=('test',))
        self.assertEqual(sum(plan.amounts),3)
        before=goods.snapshot(),cash.snapshot()
        self.assertEqual(before,(goods.snapshot(),cash.snapshot()))
        plan.commit(stock)
        self.assertEqual(cash.balance(payer).native_mU,997)
        self.assertEqual(sum(cash.balance(a).native_mU for a in recipients),3)
        self.assertEqual(purchase_quote(payments,payer,Q(1,2),'actual_input'),0)
        goods.audit();cash.audit()

    def test_stale_purchase_is_atomic_and_internal_company_input_needs_no_payment(self):
        corp=EconomicOwner('n0','corp:tech')
        goods,cash=self.fixture([(Origin('n0','h',economic_owner=corp),1)])
        source=Account('n0','h','available','accelerator')
        stock=goods.prepare_transform({source:1},{},reason='actual_input',use=Use.INDUSTRY)
        payments=Payments(cash);payer=CashAccount('n1','treasury','C')
        plan,_=prepare_purchase(payments,payer,stock.materials.events,{'accelerator':Q(1)},kind='input',key=('test',))
        cash.transfer(payer,CashAccount('n0','treasury','C'),1)
        before=goods.snapshot(),cash.snapshot()
        with self.assertRaises(InvariantViolation):plan.commit(stock)
        self.assertEqual(before,(goods.snapshot(),cash.snapshot()))
        own=CashAccount('n0','corp:tech','C')
        plan,_=prepare_purchase(payments,own,stock.materials.events,{'accelerator':Q(1)},kind='input',key=('test',),buyer=own)
        plan.commit(stock)
        self.assertFalse(plan.amounts)
        self.assertEqual(goods.balance(source),0)
        cash.audit();goods.audit()


class OperatingTitleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.catalog,cls.world=Catalog(),generate_candidate(1000)

    def test_real_output_records_company_title_while_genesis_remains_government_stock(self):
        game=opening_economy(self.catalog,initialize(self.catalog,self.world),total_ticks=10,commerce=True,debug=False)
        state=game.state
        initial_food=Account('n0','n0-h0','available','food')
        self.assertTrue(all(o.economic_owner is None for o in state.initial.inventory.materials.composition(initial_food)))
        for _ in range(5):game.step()
        food=state.initial.inventory.materials.composition(initial_food)
        self.assertTrue(any(o.economic_owner==EconomicOwner('n0','corp:manufacturing') for o in food))
        civilian=[e for e in state.initial.inventory.materials.events if e.reason=='civilian_consumption' and e.owner=='n0']
        self.assertTrue(any(e.origin.economic_owner for e in civilian))
        state.audit()

    def test_company_foreign_receipts_fund_real_native_costs_before_government_subsidy(self):
        state=OperatingWorld(self.catalog,initialize(self.catalog,self.world));economy=Economy(state)
        native=state.native('n0','corp:tech');foreign=CashAccount('n0','corp:tech','RU')
        state.initial.cash.transfer(CashAccount('world','pool','RU'),foreign,1000)
        treasury=state.initial.cash.balance(state.native('n0','treasury')).native_mU
        compute_fixture.ComputeOperationsTests.setUpClass()
        economy._fund_companies(compute_fixture.ComputeOperationsTests.ctx(0),{native:100})
        self.assertEqual(state.initial.cash.balance(native).native_mU,100)
        self.assertEqual(state.initial.cash.balance(foreign).native_mU,900)
        self.assertEqual(state.initial.cash.balance(state.native('n0','treasury')).native_mU,treasury)
        state.initial.cash.audit()

    def test_construction_pays_material_owner_and_service_in_one_atomic_invoice(self):
        state=OperatingWorld(self.catalog,initialize(self.catalog,self.world));ctx=compute_fixture.ComputeOperationsTests.ctx(0)
        inventory=state.initial.inventory
        inventory.materials.begin_tick(0);state.asset_lifecycle.begin_tick(0)
        source=Account('n0','n0-h0','available','steel')
        inventory.transfer(source,Account('n0','n0-h0','fixture_hold','steel'),inventory.balance(source))
        inventory.transform({}, {source:1000},reason='explicit_title_construction_fixture',
            origins={source:Origin('n0','n0-h0',economic_owner=EconomicOwner('n0','corp:materials'))})
        aid='title-work';asset=Asset(aid,'n0','n0-h0',{'steel':1000000},book_nmU={'steel':Q(0)},under_construction=True)
        state.work.on_complete=lambda job:state.asset_lifecycle.commission(aid)
        state.work.on_cancel=lambda job:None
        job=WorkOrder('title-work-job','1','n0','n0-h0','test_title_work',aid,1,{'steel':1000},cash_mU=3,temporary_asset=aid)
        treasury=state.native('n0','treasury');household=state.native('n0','household:n0-h0')
        before=state.initial.cash.balance(treasury).native_mU,state.initial.cash.balance(household).native_mU
        state.work.prepare_start(job,lambda:state.initial.assets.__setitem__(aid,asset)).commit()
        state.work.advance(ctx)
        price=1000*state.price('steel','n0-h0')
        self.assertEqual(job.material_paid_mU,price)
        self.assertEqual(job.cash_paid_mU,3)
        self.assertEqual(state.initial.cash.balance(state.native('n0','corp:materials')).native_mU,price)
        self.assertEqual(state.initial.cash.balance(treasury).native_mU,before[0]-price-3)
        self.assertEqual(state.initial.cash.balance(household).native_mU,before[1]+3)
        self.assertFalse(state.work.jobs)
        state.initial.audit();state.asset_lifecycle.audit()

    def test_internal_input_quote_does_not_require_fictitious_purchase_cash(self):
        state=OperatingWorld(self.catalog,initialize(self.catalog,self.world));economy=Economy(state)
        lid=next(lid for lid,line in state.initial.lines.items() if line.owner=='n0' and line.recipe.id=='P001')
        line=state.initial.lines[lid];good='iron_ore';source=Account('n0',line.hub,'available',good)
        state.initial.inventory.transfer(source,Account('n0',line.hub,'fixture_hold',good),state.initial.inventory.balance(source))
        state.initial.inventory.transform({}, {source:1000},reason='explicit_internal_title_fixture',
            origins={source:Origin('n0',line.hub,economic_owner=EconomicOwner('n0','corp:materials'))})
        payer=CashAccount('n0','input:'+lid,'n0')
        self.assertEqual(state.initial.cash.balance(payer).native_mU,0)
        self.assertEqual(economy._input_quote(lid,good,Q(100),payer),0)
        state.initial.inventory.transform({}, {source:1},reason='explicit_external_title_fixture')
        self.assertGreater(economy._input_quote(lid,good,Q(100),payer),0)
