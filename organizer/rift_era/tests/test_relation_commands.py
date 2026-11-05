import unittest
from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.world import generate_candidate
from organizer.rift_era.engine.diplomacy import Relation,AcceptedPeace
from organizer.rift_era.engine.numeric import Q
from organizer.rift_era.engine.ledger import Account, CashAccount


class RelationCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.catalog,cls.world=Catalog(),generate_candidate(1000)

    def game(self):
        return opening_economy(self.catalog,initialize(self.catalog,self.world),total_ticks=10,commerce=True,debug=False)

    def declaration(self,target,id='1'):
        return {'id':id,'kind':'diplomacy','op':'declare_war','target_nation':target}

    def test_simultaneous_declarations_have_two_aggressors_and_no_alliance_war(self):
        game=self.game();state=game.state
        game.step({'n1':[self.declaration('n0')],'n0':[self.declaration('n1')]})
        pair=state.initial.diplomacy.pairs['n0','n1']
        self.assertEqual(pair.aggressors,frozenset({'n0','n1'}))
        self.assertEqual(state.initial.diplomacy.relation('n0','n2'),Relation.PEACE)
        self.assertEqual(state.relations.action_cycles[0],{'n0':Q(1,1000),'n1':Q(1,1000)})
        self.assertEqual(state.relations.spread_pp['n0'],2)
        for _ in range(4):game.step()
        game.step({'n0':[self.declaration('n1',id='2')]})
        self.assertEqual(state.command_streams['n0'].recent[-1].status,'REJECTED')
        self.assertEqual(state.relations.action_cycles[0]['n0'],Q(1,1000))
        state.audit()

    def test_truce_break_has_exact_single_credit_penalty(self):
        game=self.game();state=game.state
        state.initial.diplomacy.phase1(0,treaties=(AcceptedPeace('n0','n1','armistice'),))
        before=state.credit['n0']
        game.step({'n0':[self.declaration('n1')]})
        self.assertEqual(state.credit['n0'],before-Q(35,100))
        self.assertEqual(state.relations.action_cycles[0]['n0'],Q(1,1000))
        self.assertTrue(state.relations.last_events[0]['truce_broken'])

    def test_c26_same_window_war_preserves_noncontrolled_civilian_trade(self):
        game=self.game();state=game.state
        offer={'id':'1','kind':'trade','op':'export_offer','hub_id':'n1-h0','good':'food',
               'qty_mU':1000,'price_ru_mU':1000}
        game.step({'n1':[offer]})
        for _ in range(4):game.step()
        alias=state.spot.visible('n0')['export_offers'][0]['id']
        purchase={'id':'1','kind':'trade','op':'buy_from','offer_id':alias,'seller':'n1',
                  'good':'food','qty_mU':1000,'max_price_ru_mU':1000}
        state.initial.cash.transfer(CashAccount('world','pool','RU'),CashAccount('n0','reserves','RU'),1000)
        game.step({'n0':[purchase,self.declaration('n1',id='2')]})
        self.assertEqual(state.market_events[0].quantity_mU,1000)
        self.assertNotIn('n1/1',state.spot.orders)
        self.assertNotIn('n0/1',state.spot.orders)
        self.assertEqual(state.initial.inventory.balance(Account('n0','n1-h0','available','food')),1000)
        self.assertEqual(state.initial.inventory.balance(Account('n0','n1-h0','next_available','food')),0)
        self.assertFalse(state.relations.trade_allowed('n0','n1','accelerator'))
        self.assertFalse(state.relations.trade_allowed('n0','n1','ammunition'))
        self.assertTrue(state.relations.trade_allowed('n0','n2','accelerator'))
        state.audit()
