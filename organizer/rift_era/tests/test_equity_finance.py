import unittest
from organizer.rift_era.engine.equity_finance import INITIAL,MAINTENANCE


class EquityFinanceBoundaryTests(unittest.TestCase):
    def test_constants_match_frozen_margin_boundaries(self):
        self.assertEqual(float(INITIAL),.5)
        self.assertEqual(float(MAINTENANCE),.3)

class EquityFinanceFundingTests(unittest.TestCase):
    def fixture(self):
        from types import SimpleNamespace as NS
        from organizer.rift_era.engine.ledger import CashLedger,Inventory,CashAccount,Account
        from organizer.rift_era.engine.equity_finance import EquityFinance
        from organizer.rift_era.engine.stock_exchange import StockExchange
        from organizer.rift_era.engine.spot import Venue,RestingOrder
        from organizer.rift_era.engine.stock_market import StockMarket
        from organizer.rift_era.engine.numeric import Q
        cash=CashLedger();cash.initialize(CashAccount('n0','treasury','n1'),10000,rate=Q(1));cash.initialize(CashAccount('world','pool','n1'),10000,rate=Q(1));cash.seal()
        stock=Inventory();stock.initialize(Account('n1','securities','strategic','c'),510000000);stock.initialize(Account('npc:c:Fund','securities','available','c'),490000000);stock.seal()
        exchange=StockExchange(stock,cash);exchange.add_venue(Venue('c','securities','c','n1',100,1000,Q(1,500),CashAccount('world','pool','n1')))
        state=NS(initial=NS(cash=cash,companies={'c':NS(issuer='n1',listed=True)},map=NS(seed=1)),rates={'n1':Q(1)},interest_bp={'n1':0})
        market=NS(shares=stock,exchange=exchange,account=StockMarket.account,npc=StockMarket.npc,
                  ownership=lambda cid:{'n1':510000,'npc:c:Fund':490000},_sync=lambda:None)
        market._order=lambda owner,cid,side,qty,price,seq,tick,expires=None,proceeds=None:RestingOrder('stock:'+owner+':'+seq,seq,owner,cid,side,qty*1000,price,expires or tick+10,proceeds or CashAccount(owner,'treasury','n1'),Account(owner,'securities','next_available' if side=='buy' else 'available',cid),cash_namespace='stock_cash:')
        state.stock_market=market
        return state,EquityFinance(state)

    def test_margin_prepare_reserves_only_real_half_and_pool_claims_are_batched(self):
        from types import SimpleNamespace as NS
        from organizer.rift_era.engine.ledger import CashAccount
        state,finance=self.fixture();order={'id':'1','company':'c','side':'buy','qty':10,'limit_price_mU':100,'expire_cycles':1}
        finance.prepare('n0',order,NS(tick=0,cycle=0)).commit()
        self.assertEqual(state.initial.cash.balance(CashAccount('n0','treasury','n1')).native_mU,9498)
        claims=finance.pool_claims();self.assertEqual(len(claims),1);self.assertEqual(claims[0].amount_mU,500)
        self.assertFalse(state.stock_market.exchange.orders)
        self.assertEqual(state.initial.cash.balance(CashAccount('world','pool','n1')).native_mU,10000)

    def test_pool_shortfall_reduces_whole_share_order_and_refunds_unused_own_cash(self):
        from types import SimpleNamespace as NS
        from organizer.rift_era.engine.ledger import CashAccount
        state,finance=self.fixture();order={'id':'1','company':'c','side':'buy','qty':10,'limit_price_mU':100,'expire_cycles':1}
        finance.prepare('n0',order,NS(tick=0,cycle=0)).commit();row=finance.pending['stock:n0:1']
        state.initial.cash.transfer(CashAccount('world','pool','n1'),finance.funding(row),200)
        finance.after_commands(NS(seed=1,tick=0,cycle=0))
        resting=state.stock_market.exchange.orders['stock:n0:1']
        self.assertEqual(resting.remaining_mU,4000)
        self.assertEqual(state.initial.cash.balance(resting.cash_lock).native_mU,401)
        self.assertEqual(state.initial.cash.balance(CashAccount('n0','treasury','n1')).native_mU,9799)
        self.assertEqual(finance.orders['stock:n0:1'].borrowed_reserved,200)

    def test_lower_fill_price_returns_unused_borrowed_principal_to_actual_pool(self):
        from types import SimpleNamespace as NS
        from organizer.rift_era.engine.ledger import CashAccount,Account
        from organizer.rift_era.engine.spot import RestingOrder
        state,finance=self.fixture();market=state.stock_market
        finance.prepare('n0',{'id':'1','company':'c','side':'buy','qty':10,'limit_price_mU':100,'expire_cycles':1},NS(tick=0,cycle=0)).commit();row=finance.pending['stock:n0:1']
        state.initial.cash.transfer(CashAccount('world','pool','n1'),finance.funding(row),500);finance.after_commands(NS(seed=1,tick=0,cycle=0))
        seller='npc:c:Fund';market.cash=lambda p,c:CashAccount(p,'portfolio',c);market._remove_long=lambda *a:None
        sell=RestingOrder('sell','2',seller,'c','sell',10000,80,20,CashAccount(seller,'portfolio','n1'),Account(seller,'securities','available','c'),cash_namespace='stock_cash:')
        market.exchange.prepare_order(sell,tick=0).commit();prior=dict(market.exchange.orders)
        result=market.exchange.clear(1,9,'c',allowed=lambda a,b:a!=b)
        finance.on_clear('c',result,prior,NS(seed=1,tick=9,cycle=0))
        pos=finance.positions['n0','c','margin']
        self.assertEqual(pos.debt_mU,result.buyer_payments['stock:n0:1']//2)
        self.assertEqual(state.initial.cash.balance(CashAccount('world','pool','n1')).native_mU,10000-pos.debt_mU+result.tariff_mU+sum(result.seller_fees.values()))

    def test_short_borrow_draws_only_actual_npc_available_and_keeps_total_shares(self):
        from types import SimpleNamespace as NS
        from organizer.rift_era.engine.ledger import Account,CashAccount
        from organizer.rift_era.engine.stock_market import LongLot
        from organizer.rift_era.engine.numeric import Q
        state,finance=self.fixture();market=state.stock_market
        market.long_lots={('npc:c:Fund','c',Q(1)):LongLot(490000,Q(49000000))}
        finance.prepare('n0',{'id':'1','company':'c','side':'short','qty':10,'limit_price_mU':100,'expire_cycles':1},NS(tick=0,cycle=0)).commit()
        before=sum(market.shares.balances().values());finance.after_commands(NS(seed=1,tick=0,cycle=0))
        order=market.exchange.orders['stock:n0:1']
        self.assertEqual(market.shares.balance(order.stock_lock),10000)
        self.assertEqual(market.shares.balance(Account('npc:c:Fund','securities','available','c')),489990000)
        self.assertEqual(sum(market.shares.balances().values()),before)
        self.assertEqual(state.initial.cash.balance(finance.escrow('n0','c')).native_mU,500)
        self.assertFalse(finance.positions)

    def test_partial_short_fill_moves_only_filled_shares_to_return_claim(self):
        from types import SimpleNamespace as NS
        from organizer.rift_era.engine.ledger import CashAccount,Account
        from organizer.rift_era.engine.stock_market import LongLot
        from organizer.rift_era.engine.spot import RestingOrder
        from organizer.rift_era.engine.numeric import Q
        state,finance=self.fixture();market=state.stock_market
        market.long_lots={('npc:c:Fund','c',Q(1)):LongLot(490000,Q(49000000))};market.cash=lambda p,c:CashAccount(p,'portfolio' if p.startswith('npc:') else 'treasury',c)
        finance.prepare('n0',{'id':'1','company':'c','side':'short','qty':10,'limit_price_mU':100,'expire_cycles':1},NS(tick=0,cycle=0)).commit();finance.after_commands(NS(seed=1,tick=0,cycle=0))
        state.initial.cash.transfer(CashAccount('world','pool','n1'),CashAccount('n2','treasury','n1'),501)
        buy=RestingOrder('buy','2','n2','c','buy',5000,100,20,CashAccount('n2','treasury','n1'),Account('n2','securities','next_available','c'),cash_namespace='stock_cash:')
        market.exchange.prepare_order(buy,tick=0).commit();prior=dict(market.exchange.orders)
        result=market.exchange.clear(1,9,'c',allowed=lambda a,b:a!=b);finance.on_clear('c',result,prior,NS(seed=1,tick=9,cycle=0))
        pos=finance.positions['n0','c','short']
        self.assertEqual(pos.shares,5);self.assertEqual(pos.lenders,{'npc:c:Fund':5})
        self.assertEqual(market.long_lots['npc:c:Fund','c',Q(1)].shares,489995)
        finance.finish('stock:n0:1',NS(seed=1,tick=10,cycle=1))
        self.assertEqual(market.shares.balance(Account('npc:c:Fund','securities','available','c')),489995000)
        self.assertEqual(pos.shares,5)
        self.assertEqual(sum(market.shares.balances().values()),1000000000)
        finance.audit()

    def test_mark_breach_waits_until_next_tick_and_forced_orders_keep_priority_band(self):
        from types import SimpleNamespace as NS
        from organizer.rift_era.engine.equity_finance import Position
        from organizer.rift_era.engine.ledger import Account
        from organizer.rift_era.engine.numeric import Q
        state,finance=self.fixture();market=state.stock_market
        market.shares.transfer(Account('npc:c:Fund','securities','available','c'),finance.custody('n0','c','margin'),10000)
        finance.positions['n0','c','margin']=Position('n0','c','margin',10,900,Q(900))
        finance.settle(NS(seed=1,tick=9,cycle=0,clock=NS(is_cycle_end=lambda t:True)))
        self.assertEqual(finance.positions['n0','c','margin'].status,'LIQUIDATING')
        self.assertFalse(market.exchange.orders)
        finance.begin_tick(NS(seed=1,tick=10,cycle=1))
        forced=next(iter(market.exchange.orders.values()))
        self.assertEqual(forced.priority,0)
        self.assertEqual(forced.side,'sell')
        self.assertEqual(market.shares.balance(forced.stock_lock),10000)

    def test_current_interest_provider_is_pure_and_matches_accrual_then_real_pool_payment(self):
        from types import SimpleNamespace as NS
        from organizer.rift_era.engine.equity_finance import Position
        from organizer.rift_era.engine.numeric import Q
        from organizer.rift_era.engine.ledger import CashAccount
        from organizer.rift_era.engine.determinism import fingerprint
        state,finance=self.fixture();finance.positions['n0','c','margin']=Position('n0','c','margin',10,10000,Q(10000))
        ctx=NS(seed=1,tick=9,cycle=0,clock=NS(is_cycle_end=lambda t:True))
        before=fingerprint(finance.snapshot());claims=finance.funding_claims(ctx)
        self.assertEqual(claims[0].amount_mU,2)
        self.assertEqual(before,fingerprint(finance.snapshot()))
        finance.accrue(ctx);self.assertEqual(finance.funding_claims(ctx)[0].amount_mU,2)
        cash=state.initial.cash;cash.transfer(CashAccount('n0','treasury','n1'),finance.deposit('n0','c'),2)
        pool_before=cash.balance(CashAccount('world','pool','n1')).native_mU
        finance.settle(ctx)
        self.assertEqual(cash.balance(CashAccount('world','pool','n1')).native_mU,pool_before+2)
        self.assertFalse(finance.positions['n0','c','margin'].fees)

    def test_multiple_cover_orders_reserve_disjoint_slices_and_cancel_preserves_other_collateral(self):
        from types import SimpleNamespace as NS
        from organizer.rift_era.engine.equity_finance import Position
        from organizer.rift_era.engine.ledger import CashAccount
        from organizer.rift_era.engine.numeric import Q
        state,finance=self.fixture();cash=state.initial.cash
        finance.positions['n0','c','short']=Position('n0','c','short',10,lenders={'npc:c:Fund':10})
        cash.transfer(CashAccount('n0','treasury','n1'),finance.escrow('n0','c'),1500)
        for seq in ('1','2'):
            finance.prepare('n0',{'id':seq,'company':'c','side':'cover','qty':5,'limit_price_mU':100,'expire_cycles':1},NS(tick=0,cycle=0)).commit()
        self.assertEqual(sum(r.collateral_slice for r in finance.orders.values()),1002)
        self.assertEqual(cash.balance(finance.escrow('n0','c')).native_mU,498)
        finance.finish('stock:n0:1',NS(tick=1,cycle=0))
        self.assertEqual(cash.balance(finance.escrow('n0','c')).native_mU,999)
        self.assertIn('stock:n0:2',state.stock_market.exchange.orders)
        finance.finish('stock:n0:2',NS(tick=1,cycle=0))
        self.assertEqual(cash.balance(finance.escrow('n0','c')).native_mU,1500)
        self.assertEqual(finance.positions['n0','c','short'].shares,10)

    def test_partial_cover_returns_only_bought_shares_and_original_lender_basis(self):
        from types import SimpleNamespace as NS
        from organizer.rift_era.engine.equity_finance import Position
        from organizer.rift_era.engine.ledger import CashAccount,Account
        from organizer.rift_era.engine.numeric import Q
        from organizer.rift_era.engine.spot import RestingOrder
        state,finance=self.fixture();market=state.stock_market;cash=state.initial.cash
        market.long_lots={};market.cash=lambda p,c:CashAccount(p,'portfolio' if p.startswith('npc:') else 'treasury',c)
        market.shares.transfer(Account('npc:c:Fund','securities','available','c'),Account('n2','securities','available','c'),5000)
        finance.positions['n0','c','short']=Position('n0','c','short',10,lenders={'npc:c:Fund':10},lender_basis={('npc:c:Fund',Q(2)):(10,Q(600))})
        cash.transfer(CashAccount('n0','treasury','n1'),finance.escrow('n0','c'),1500)
        finance.prepare('n0',{'id':'1','company':'c','side':'cover','qty':10,'limit_price_mU':100,'expire_cycles':1},NS(tick=0,cycle=0)).commit()
        sell=RestingOrder('sell','2','n2','c','sell',5000,100,20,CashAccount('n2','treasury','n1'),Account('n2','securities','available','c'),cash_namespace='stock_cash:')
        market.exchange.prepare_order(sell,tick=0).commit();prior=dict(market.exchange.orders)
        result=market.exchange.clear(1,9,'c',allowed=lambda a,b:a!=b);finance.on_clear('c',result,prior,NS(seed=1,tick=9,cycle=0))
        pos=finance.positions['n0','c','short'];self.assertEqual(pos.shares,5)
        self.assertEqual(market.long_lots['npc:c:Fund','c',Q(2)].book_ru_mU,Q(300))
        self.assertEqual(pos.lender_basis[('npc:c:Fund',Q(2))],(5,Q(300)))
        finance.finish('stock:n0:1',NS(seed=1,tick=10,cycle=1))
        self.assertEqual(cash.balance(finance.escrow('n0','c')).native_mU,999)
        self.assertEqual(pos.shares,5)
        finance.audit()

    def test_separate_fill_fx_lots_preserve_actual_historical_loan_basis(self):
        from types import SimpleNamespace as NS
        from organizer.rift_era.engine.ledger import CashAccount
        from organizer.rift_era.engine.numeric import Q
        state,finance=self.fixture();market=state.stock_market
        for seq,rate in (('1',Q(1)),('2',Q(3))):
            state.rates['n1']=rate
            finance.prepare('n0',{'id':seq,'company':'c','side':'buy','qty':10,'limit_price_mU':100,'expire_cycles':1},NS(tick=0,cycle=0)).commit()
            row=finance.pending['stock:n0:'+seq]
            state.initial.cash.transfer(CashAccount('world','pool','n1'),finance.funding(row),500)
            finance.after_commands(NS(seed=1,tick=int(seq),cycle=0))
            oid='stock:n0:'+seq
            result=NS(buy_fills={oid:4000},sell_fills={},buyer_payments={oid:400})
            finance.on_clear('c',result,dict(market.exchange.orders),NS(seed=1,tick=int(seq),cycle=0))
        pos=finance.positions['n0','c','margin']
        self.assertEqual(set(pos.debt_lots),{Q(1),Q(3)})
        self.assertEqual(pos.debt_lots[Q(1)][0],200)
        self.assertEqual(pos.debt_lots[Q(3)][0],200)
        # Both loans came from the same fixed pool historical-basis account;
        # changing display FX must not rewrite those actual transfer costs.
        self.assertEqual(pos.debt_book_ru_mU,Q(400))
