import unittest

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.clock import ClockConfig
from organizer.rift_era.engine.commands import EngineCommands
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.kernel import TickContext
from organizer.rift_era.engine.ledger import CashAccount
from organizer.rift_era.engine.numeric import Q
from organizer.rift_era.engine.world import generate_candidate


class StockMarketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.catalog,cls.world=Catalog(),generate_candidate(1000)

    def game(self):
        return opening_economy(self.catalog,initialize(self.catalog,self.world),total_ticks=30,commerce=True,debug=False)

    def ipo(self,cid='n0-tech',qty=1000):
        return {'id':'1','kind':'finance','op':'ipo','company':cid,'qty':qty,'min_price_mU':100}

    def trade(self,seq=1,side='buy',qty=100,price=100):
        return {'id':str(seq),'kind':'finance','op':'stock_order','company':'n0-tech','side':side,
                'qty':qty,'limit_price_mU':price,'margin':False,'expire_cycles':2}

    def npc_cash(self,state,cid,kind):
        owner=state.stock_market.npc(cid,kind)
        return sum(b.native_mU for a,b in state.initial.cash.balances().items() if a.owner==owner)

    def test_real_ipo_and_secondary_auction_use_existing_shares_and_cash(self):
        game=self.game();state=game.state;state.research['n0'].known.add('F01')
        game.step({'n0':[self.ipo()]})
        market=state.stock_market
        self.assertTrue(state.initial.companies['n0-tech'].listed)
        self.assertEqual(state.initial.companies['n0-tech'].shares,{'n0':1000000})
        for kind,fraction in (('Fund',Q(3,10)),('Mom',Q(1,10)),('Noise',Q(1,20))):
            self.assertEqual(self.npc_cash(state,'n0-tech',kind),100*1000000*fraction)
        state.initial.cash.transfer(CashAccount('world','pool','n0'),CashAccount('n1','treasury','n0'),1000000)
        for _ in range(4):game.step()
        game.step({'n1':[self.trade()]})
        for _ in range(4):game.step()
        company=state.initial.companies['n0-tech']
        self.assertEqual(company.shares['n1'],100)
        self.assertGreaterEqual(company.shares['n0'],510000)
        self.assertEqual(sum(company.shares.values()),1000000)
        lot,=tuple(lot for (p,cid,_),lot in market.long_lots.items() if p=='n1' and cid=='n0-tech')
        self.assertEqual(lot.shares,100)
        self.assertGreater(lot.book_ru_mU,0)
        self.assertEqual(sum(row.shares for row in market.external_issues['n0-tech']),1000000-company.shares['n0'])
        state.audit()

    def test_same_p1_cannot_guess_foreign_new_listing_even_if_issuer_processed_first(self):
        game=self.game();state=game.state;state.research['n0'].known.add('F01')
        state.initial.cash.transfer(CashAccount('world','pool','n0'),CashAccount('n1','treasury','n0'),1000000)
        game.step({'n0':[self.ipo()],'n1':[self.trade()]})
        self.assertEqual(state.command_streams['n0'].recent[-1].status,'ACCEPTED')
        self.assertEqual(state.command_streams['n1'].recent[-1].code,'ERR_NO_VISIBLE_TARGET')
        self.assertEqual(state.initial.companies['n0-tech'].shares.get('n1',0),0)
        state.audit()

    def test_empty_finite_seed_pool_cannot_create_npc_principal(self):
        game=self.game();state=game.state;state.research['n0'].known.add('F01')
        pool=CashAccount('world','pool','n0')
        state.initial.cash.transfer(pool,CashAccount('world','seed_liquidity_test','n0'),state.initial.cash.balance(pool).native_mU)
        ctx=TickContext(1000,0,ClockConfig(total_ticks=30),{'n0':(self.ipo(),)})
        state.stock_market.begin_tick(ctx);EngineCommands(state).commands(state,ctx)
        self.assertTrue(state.initial.companies['n0-tech'].listed)
        self.assertEqual(sum(self.npc_cash(state,'n0-tech',k) for k in ('Fund','Mom','Noise')),0)
        self.assertEqual(state.initial.companies['n0-tech'].shares,{'n0':1000000})
        state.audit()

    def test_cancelled_unfilled_ipo_does_not_unlock_unissued_shares_for_free_sale(self):
        game=self.game();state=game.state;state.research['n0'].known.add('F01')
        game.step({'n0':[self.ipo(qty=1000)]})
        market=state.stock_market
        commands=EngineCommands(state);ctx=TickContext(1000,5,ClockConfig(total_ticks=30),{})
        commands.prepare('n0',{'id':'2','kind':'cancel','op':'cancel_by_id','target_id':'1'},ctx).commit()
        self.assertEqual(market.shares.balance(market.account('n0','n0-tech','unissued')),490000000)
        before=state.initial.cash.snapshot()
        receipts=state.command_streams['n0'].process([self.trade(seq=3,side='sell',qty=1)],lambda o:commands.prepare('n0',o,ctx))
        self.assertEqual(receipts[0].code,'ERR_INSUFFICIENT_STOCK')
        self.assertEqual(before,state.initial.cash.snapshot())
        state.audit()

    def test_screen_fx_changes_revalue_neither_old_cash_nor_financial_entry_basis(self):
        game=self.game();state=game.state;state.research['n0'].known.add('F01')
        game.step({'n0':[self.ipo()]})
        state.initial.cash.transfer(CashAccount('world','pool','n0'),CashAccount('n1','treasury','n0'),1000000)
        for _ in range(4):game.step()
        game.step({'n1':[self.trade()]})
        for _ in range(4):game.step()
        market=state.stock_market
        expected=market.long_book('n1',0),market.external_equity_book('n0',0),state.initial.cash.snapshot()
        for rate in (Q(1,20),Q(20),Q(1,2)):
            state.rates['n0']=rate
            self.assertEqual((market.long_book('n1',0),market.external_equity_book('n0',0),state.initial.cash.snapshot()),expected)

    def test_marked_up_price_and_second_ipo_cannot_refill_npc_cash(self):
        game=self.game();state=game.state;state.research['n0'].known.add('F01')
        game.step({'n0':[self.ipo()]})
        market=state.stock_market;before=[self.npc_cash(state,'n0-tech',k) for k in ('Fund','Mom','Noise')]
        market.exchange.prices['n0-tech']=1000000
        ctx=TickContext(1000,5,ClockConfig(total_ticks=30),{})
        market.after_commands(ctx)
        receipts=state.command_streams['n0'].process([self.ipo()|{'id':'2'}],lambda o:EngineCommands(state).prepare('n0',o,ctx))
        self.assertEqual(receipts[0].code,'ERR_NO_PERMISSION')
        self.assertEqual([self.npc_cash(state,'n0-tech',k) for k in ('Fund','Mom','Noise')],before)


class StockOrderF03GateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.catalog,cls.world=Catalog(),generate_candidate(1000)
    game=StockMarketTests.game;ipo=StockMarketTests.ipo;trade=StockMarketTests.trade
    """06 finance/stock_order：融资买入（margin=true）和融券卖空（short）需本国已解锁 F03。"""
    def _listed(self):
        game=self.game();state=game.state;state.research['n0'].known.add('F01')
        game.step({'n0':[self.ipo()]})
        state.initial.cash.transfer(CashAccount('world','pool','n0'),CashAccount('n1','treasury','n0'),1000000)
        for _ in range(4):game.step()
        return game,state

    def test_margin_buy_and_short_are_tech_locked_without_f03(self):
        game,state=self._listed()
        self.assertNotIn('F03',state.research['n1'].known)
        margin=dict(self.trade(seq=2),margin=True)
        short=dict(self.trade(seq=3,side='short'))
        game.step({'n1':[margin,short]})
        codes=[r.code for r in state.command_streams['n1'].recent[-2:]]
        self.assertEqual(codes,['ERR_TECH_LOCKED','ERR_TECH_LOCKED'])
        self.assertFalse(any(k[0]=='n1' for k in state.stock_market.finance.positions))
        state.audit()

    def test_margin_buy_passes_gate_with_f03(self):
        game,state=self._listed();state.research['n1'].known.add('F03')
        game.step({'n1':[dict(self.trade(seq=2),margin=True)]})
        self.assertNotEqual(state.command_streams['n1'].recent[-1].code,'ERR_TECH_LOCKED')
        state.audit()
