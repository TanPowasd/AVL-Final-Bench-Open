import random
import unittest

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.clock import ClockConfig
from organizer.rift_era.engine.commands import EngineCommands
from organizer.rift_era.engine.determinism import fingerprint
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.errors import InvariantViolation, RuleViolation
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.kernel import TickContext
from organizer.rift_era.engine.ledger import Account, CashAccount, CashLedger, Inventory
from organizer.rift_era.engine.materials import Origin
from organizer.rift_era.engine.numeric import Q
from organizer.rift_era.engine.origin_market import clear_origins
from organizer.rift_era.engine.market import Order
from organizer.rift_era.engine.spot import RestingOrder, SpotExchange, Venue
from organizer.rift_era.engine.world import generate_candidate


class OriginMarketTests(unittest.TestCase):
    def fixture(self,amounts):
        stock,cash=Inventory(),CashLedger()
        for owner,amount in amounts.items():stock.initialize(Account(owner,'h','available','accelerator'),amount)
        for owner in ('buyer','other'):cash.initialize(CashAccount(owner,'reserves','RU'),100000)
        stock.seal();cash.seal();stock.enable_provenance(seed=17).begin_tick(0)
        for owner,amount in amounts.items():
            stock.transfer(Account(owner,'h','available','accelerator'),Account('merchant','h','available','accelerator'),amount)
        exchange=SpotExchange(stock,cash)
        exchange.add_venue(Venue('v','h','accelerator','RU',1000))
        return exchange

    def post(self,exchange,owner,seq,side,qty,price=1000,**kwargs):
        order=RestingOrder(f'{owner}/{seq}',str(seq),owner,'v',side,qty,price,100,
            CashAccount(owner,'reserves','RU'),Account(owner,'h','available' if side=='sell' else 'next_available','accelerator'),**kwargs)
        exchange.prepare_order(order,tick=0).commit()
        return order

    def allowed(self,buyer,seller,origin,good):return not (buyer=='buyer' and origin=='banned')

    def test_same_price_split_combines_origin_quanta_and_retains_dust(self):
        outputs=[]
        for parts in ((2,),(1,1)):
            exchange=self.fixture({'banned':1,'free':1})
            for i,q in enumerate(parts):self.post(exchange,'merchant',i,'sell',q)
            self.post(exchange,'buyer',0,'buy',2)
            result=exchange.clear(17,1,'v',origin_allowed=self.allowed)
            self.assertEqual(result.quantity_mU,1)
            destination=Account('buyer','h','next_available','accelerator')
            self.assertEqual(exchange.inventory.materials.composition(destination),{Origin('free','h'):1})
            residual={}
            for order in exchange.orders.values():
                if order.side=='sell':
                    for origin,q in exchange.inventory.materials.composition(order.stock_lock).items():
                        residual[origin]=residual.get(origin,0)+q
            self.assertEqual(residual,{Origin('banned','h'):1})
            self.assertEqual(exchange.inventory.materials.events,[])
            outputs.append((result.quantity_mU,residual,exchange.cash.snapshot()))
            exchange.inventory.audit();exchange.cash.audit();exchange.audit()
        self.assertEqual(*outputs)

    def test_different_prices_do_not_combine_subquantum_origins(self):
        exchange=self.fixture({'banned':1,'free':1})
        self.post(exchange,'merchant',0,'sell',1,1000)
        self.post(exchange,'merchant',1,'sell',1,1001)
        self.post(exchange,'buyer',0,'buy',2,2000)
        before=exchange.inventory.snapshot()
        result=exchange.clear(17,1,'v',origin_allowed=self.allowed)
        self.assertEqual(result.quantity_mU,0)
        self.assertEqual(exchange.inventory.snapshot(),before)

    def test_subquantum_remainder_accumulates_after_cancel_and_new_stock(self):
        exchange=self.fixture({'banned':2,'free':2})
        self.post(exchange,'merchant',0,'sell',1)
        self.post(exchange,'buyer',0,'buy',2)
        self.assertEqual(exchange.clear(17,1,'v',origin_allowed=self.allowed).quantity_mU,0)
        exchange.prepare_cancel('merchant','merchant/0').commit()
        self.post(exchange,'merchant',1,'sell',2)
        self.assertEqual(exchange.clear(17,2,'v',origin_allowed=self.allowed).quantity_mU,1)
        exchange.inventory.audit();exchange.audit()

    def test_multiple_buyers_get_compatible_origins_at_maximum_volume(self):
        rng=random.Random(42)
        for _ in range(60):
            a,b=rng.randrange(1,20),rng.randrange(1,20)
            exchange=self.fixture({'banned':a,'free':b})
            self.post(exchange,'merchant',0,'sell',a+b)
            x,y=rng.randrange(1,25),rng.randrange(1,25)
            self.post(exchange,'buyer',0,'buy',x)
            self.post(exchange,'other',0,'buy',y)
            result=exchange.clear(17,1,'v',origin_allowed=self.allowed)
            self.assertEqual(result.quantity_mU,min(a+b,min(x,b)+y))
            pool=exchange.inventory.materials.composition(Account('buyer','h','next_available','accelerator'))
            self.assertTrue(all(o.nation=='free' for o in pool))
            exchange.inventory.audit();exchange.cash.audit();exchange.audit()

    def test_bilateral_named_offer_can_use_same_price_fungible_origin_pool(self):
        exchange=self.fixture({'banned':1,'free':1})
        first=self.post(exchange,'merchant',0,'sell',1,bilateral=True)
        second=self.post(exchange,'merchant',1,'sell',1,bilateral=True)
        self.post(exchange,'buyer',0,'buy',1,bilateral=True,offer_id=first.id)
        results=exchange.clear_offers(17,1,origin_allowed=self.allowed)
        self.assertEqual(sum(r.quantity_mU for r in results),1)
        self.assertNotIn(first.id,exchange.orders)
        self.assertEqual(exchange.orders[second.id].remaining_mU,1)
        self.assertEqual(exchange.inventory.materials.composition(second.stock_lock),{Origin('banned','h'):1})
        exchange.inventory.audit();exchange.cash.audit();exchange.audit()

    def test_origin_overlay_is_pure_and_stale_cash_rejects_before_stock_write(self):
        exchange=self.fixture({'banned':1,'free':1})
        self.post(exchange,'merchant',0,'sell',2)
        buyer=self.post(exchange,'buyer',0,'buy',2)
        before=fingerprint((exchange.snapshot(),exchange.inventory.snapshot(),exchange.cash.snapshot()))
        eligible=tuple(Order(o.id,o.owner,o.side,o.remaining_mU,o.limit_price_mU) for o in exchange.orders.values())
        auction,matches,materials=clear_origins(exchange.inventory,exchange.orders,eligible,seed=17,tick=1,
            venue=exchange.venues['v'],previous_price=1000,allowed=None,origin_allowed=self.allowed)
        _,commit=exchange._prepare_settlement(17,1,exchange.venues['v'],auction,matches=matches,material_plan=materials)
        self.assertEqual(before,fingerprint((exchange.snapshot(),exchange.inventory.snapshot(),exchange.cash.snapshot())))
        exchange.cash.transfer(buyer.cash_account,CashAccount('other','reserves','RU'),1)
        before=fingerprint((exchange.snapshot(),exchange.inventory.snapshot(),exchange.cash.snapshot()))
        with self.assertRaises(InvariantViolation):commit()
        self.assertEqual(before,fingerprint((exchange.snapshot(),exchange.inventory.snapshot(),exchange.cash.snapshot())))

    def test_unrestricted_callback_preserves_existing_clearing_and_materials(self):
        views=[]
        for callback in (None,lambda b,s,o,g:True):
            exchange=self.fixture({'banned':17,'free':21})
            self.post(exchange,'merchant',0,'sell',38)
            self.post(exchange,'buyer',0,'buy',33)
            result=exchange.clear(17,1,'v',origin_allowed=callback)
            views.append(fingerprint((result,exchange.snapshot(),exchange.inventory.snapshot(),exchange.cash.snapshot())))
        self.assertEqual(*views)


class ExportControlIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.catalog,cls.world=Catalog(),generate_candidate(1000)

    def game(self):
        return opening_economy(self.catalog,initialize(self.catalog,self.world),total_ticks=10,commerce=True,debug=False)

    def policy(self,goods=None,targets=None,seq=1):
        return {'id':str(seq),'kind':'finance','op':'export_control',
                'goods':['accelerator'] if goods is None else goods,'targets':['n1'] if targets is None else targets}

    def test_real_spot_policy_checks_original_nation_and_persistent_loss(self):
        game=self.game();s=game.state
        source=Account('n0','n0-h0','available','accelerator')
        s.initial.inventory.transform({}, {source:2000},reason='explicit_test_endowment',origins={source:Origin('n7','n0-h0')})
        s.initial.cash.transfer(CashAccount('world','pool','n0'),CashAccount('n1','treasury','n0'),10000)
        game.step({'n0':[{'id':'1','kind':'logistics','op':'market_ask','hub_id':'n0-h0','good':'accelerator',
                         'qty_mU':2000,'limit_price_mU':1000,'expire_cycles':2}],
                   'n1':[{'id':'1','kind':'logistics','op':'market_bid','region_market':'n0-h0','good':'accelerator',
                         'qty_mU':2000,'limit_price_mU':1000,'expire_cycles':2}],
                   'n7':[self.policy()]})
        for _ in range(4):game.step()
        self.assertEqual(s.market_events[-1].quantity_mU,0)
        self.assertEqual(s.export_controls.action_cycles[0]['n7'],Q(5,1000))
        self.assertEqual(s.spot.orders['n0/1'].remaining_mU,2000)
        game.step({'n7':[self.policy(goods=[],seq=2)]})
        for _ in range(4):game.step()
        self.assertEqual(s.market_events[-1].quantity_mU,2000)
        received=Account('n1','n0-h0','available','accelerator')
        self.assertEqual(s.initial.inventory.materials.composition(received),{Origin('n7','n0-h0'):2000})
        self.assertEqual(s.export_controls.action_cycles[0]['n7'],Q(5,1000))
        s.audit()

    def test_invalid_policies_are_atomic_and_watermarks_advance(self):
        game=self.game();s=game.state;commands=EngineCommands(s)
        ctx=TickContext(1000,0,ClockConfig(total_ticks=10),{})
        for seq,order in enumerate((self.policy(goods=['steel']),self.policy(targets=['n0']),
                                    self.policy(goods=['accelerator','accelerator']),self.policy(targets=[True])),1):
            order['id']=str(seq)
            before=fingerprint(s.export_controls.snapshot())
            receipt=s.command_streams['n0'].process([order],lambda o:commands.prepare('n0',o,ctx))[0]
            self.assertEqual(receipt.code,'ERR_SCHEMA')
            self.assertEqual(s.command_streams['n0'].watermark,seq)
            self.assertEqual(before,fingerprint(s.export_controls.snapshot()))
