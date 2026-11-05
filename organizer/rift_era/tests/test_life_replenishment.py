import unittest

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.economy import Economy,opening_economy
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.ledger import Account,CashAccount
from organizer.rift_era.engine.numeric import Q
from organizer.rift_era.engine.world import generate_candidate
from organizer.rift_era.tests import test_compute_operations as compute_fixture


class LifeReplenishmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.catalog,cls.world=Catalog(),generate_candidate(1000)

    def game(self,initial=None):
        return opening_economy(self.catalog,initial or initialize(self.catalog,self.world),total_ticks=30,commerce=True,debug=False)

    def outdoor_food(self,state):
        source=Account('n0','n0-h0','available','food');inv=state.initial.inventory
        inv.transfer(source,Account('n0','n0-h0','outdoor','food'),1000)
        inv.transfer(source,Account('n0','n0-h0','test_hold','food'),inv.balance(source))
        # Explicit unloading-capacity boundary: P2 must retain this food
        # outdoors instead of silently promoting it into spare main storage.
        state.initial.warehouse_mU['n0-h0']=int(state.warehouse_load()['n0-h0'])
        state.paused_lines.update(lid for lid,line in state.initial.lines.items() if line.owner=='n0' and lid not in state.initial.plants)

    def test_real_cargo_arrival_can_feed_residents_in_the_same_tick(self):
        initial=initialize(self.catalog,self.world)
        people={'general_worker':10000}
        initial.populations['n0-h0'].reserve_people(people);initial.populations['n0-h1'].receive(people)
        initial.cash.transfer(CashAccount('n0','household:n0-h0','n0'),CashAccount('n0','household:n0-h1','n0'),500000)
        game=self.game(initial);state=game.state;inv=state.initial.inventory
        order={'id':'1','kind':'logistics','op':'route_shipment','origin_hub':'n0-h0','dest_hub':'n0-h1',
               'corridor_path':['road:n0-h0:n0-h1'],'good':'food','qty_mU':1000,'carrier_recipe':'P036'}
        eaten=False
        for tick in range(20):
            before=inv.balance(Account('n0','n0-h1','available','food'))
            game.step({'n0':[order]} if tick==0 else {})
            meals=[e for e in inv.materials.events if e.reason=='civilian_consumption' and e.hub=='n0-h1' and e.good=='food']
            if meals:
                self.assertEqual(before,0)
                self.assertGreater(sum(e.quantity_mU for e in meals),0)
                self.assertTrue(all(e.origin.path and e.origin.path[-1].destination=='n0-h1' for e in meals))
                eaten=True;break
        self.assertTrue(eaten)
        self.assertEqual(state.command_streams['n0'].recent[-1].status,'ACCEPTED')
        state.audit()

    def test_outdoor_food_is_usable_and_new_food_returns_unneeded_grain(self):
        game=self.game();state=game.state;self.outdoor_food(state)
        grain=Account('n0','n0-h0','available','grain');before=state.initial.inventory.balance(grain)
        game.step()
        self.assertEqual(state.frame.household['n0-h0']['food'],1000)
        self.assertEqual(state.frame.household_outdoor['n0-h0']['food'],1000)
        self.assertEqual(state.frame.household['n0-h0'].get('grain',0),0)
        self.assertEqual(state.initial.inventory.balance(grain),before)
        self.assertFalse(any(e.good=='grain' and e.reason=='civilian_consumption' and e.hub=='n0-h0'
                             for e in state.initial.inventory.materials.events))
        self.assertEqual(state.local_cycles['n0-h0'].needed['food'],state.frame.household_need['n0-h0']['food'])
        state.audit()

    def test_replenishment_does_not_buy_food_with_empty_consumer_cash(self):
        game=self.game();state=game.state;self.outdoor_food(state)
        for currency in ('n0','RU'):
            account=CashAccount('n0','household:n0-h0',currency)
            state.initial.cash.transfer(account,CashAccount('world','test_consumer_hold',currency),state.initial.cash.balance(account).native_mU)
        game.step()
        self.assertEqual(state.frame.household['n0-h0'].get('food',0),0)
        self.assertEqual(state.initial.inventory.balance(Account('n0','n0-h0','outdoor','food')),1000)
        state.audit()

    def test_free_household_ru_can_fund_real_native_food_purchase(self):
        game=self.game();state=game.state;self.outdoor_food(state)
        native=CashAccount('n0','household:n0-h0','n0');foreign=CashAccount('n0','household:n0-h0','RU')
        state.initial.cash.transfer(native,CashAccount('world','test_consumer_hold','n0'),state.initial.cash.balance(native).native_mU)
        state.initial.cash.transfer(CashAccount('world','pool','RU'),foreign,100000)
        game.step()
        self.assertEqual(state.frame.household['n0-h0']['food'],1000)
        self.assertLess(state.initial.cash.balance(foreign).native_mU,100000)
        state.initial.cash.audit()

    def test_critical_generator_inputs_are_replenished_before_p3(self):
        game=self.game();state=game.state;economy=Economy(state);inv=state.initial.inventory
        source=Account('n0','n0-h0','available','coal');hold=Account('n0','n0-h0','test_arrival','coal')
        inv.transfer(source,hold,inv.balance(source))
        ctx=compute_fixture.ComputeOperationsTests.ctx(0)
        inv.materials.begin_tick(0);state.asset_lifecycle.begin_tick(0)
        state.governance.begin_tick(ctx);state.ai_deployment.begin_tick(ctx)
        economy.reserve(state,ctx)
        plants=[lid for lid,line in state.initial.lines.items() if line.owner=='n0' and lid in state.initial.plants]
        self.assertTrue(all(inv.balance(economy._stock_account(lid,'coal'))==0 for lid in plants))
        inv.transfer(hold,source,1000) # completed local-delivery fixture, not newly created fuel
        economy.replenish_life(state,ctx)
        self.assertTrue(all(inv.balance(economy._stock_account(lid,'coal'))>0 for lid in plants))
        self.assertTrue(state.frame.life_replenished)
        inv.audit();state.initial.cash.audit()
