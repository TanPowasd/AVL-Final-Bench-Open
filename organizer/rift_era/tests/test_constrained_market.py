import itertools
import random
import unittest
from organizer.rift_era.engine.market import Order,clear_auction
from organizer.rift_era.engine.constrained_market import clear_compatible


class CompatibleMarketTests(unittest.TestCase):
    def test_unrestricted_prices_and_fills_are_unchanged(self):
        orders=(Order('a','A','buy',100,5),Order('b','B','buy',50,5),Order('c','C','sell',70,3))
        actual=clear_compatible(3,0,'m',orders,4,lambda a,b:True)
        self.assertEqual(actual.auction,clear_auction(3,0,'m',orders,4))
        self.assertEqual(sum(q for _,_,q in actual.matches),70)

    def test_blocked_supply_cannot_trade_and_remaining_buyers_share_real_supply(self):
        orders=(Order('a','A','buy',100,5),Order('b','B','buy',100,5),
                Order('c','C','sell',100,5),Order('d','D','sell',100,5))
        result=clear_compatible(3,0,'m',orders,5,lambda a,b:b=='C')
        self.assertEqual(result.auction.quantity,100)
        self.assertEqual(result.auction.buy_fills,{'a':50,'b':50})
        self.assertEqual(result.auction.sell_fills,{'c':100})
        self.assertTrue(all(s=='c' for _,s,_ in result.matches))

    def test_price_priority_remains_subject_to_real_matching(self):
        orders=(Order('a','A','buy',2,10),Order('b','B','buy',4,5),
                Order('c','C','sell',1,3),Order('d','D','sell',3,3))
        result=clear_compatible(1,0,'m',orders,4,lambda a,b:a!='A' or b=='C')
        self.assertEqual(result.auction.buy_fills,{'a':1,'b':3})
        self.assertEqual(result.auction.quantity,4)
        self.assertEqual(result.auction.price,4)

    def test_random_two_by_two_prices_match_exhaustive_legal_flows(self):
        rng=random.Random(10227)
        for seed in range(150):
            buys=[Order(str(i),chr(65+i),'buy',rng.randint(1,3),rng.randint(1,6)) for i in range(2)]
            sells=[Order(str(i+2),chr(67+i),'sell',rng.randint(1,3),rng.randint(1,6)) for i in range(2)]
            legal={(b.owner,s.owner):bool(rng.randrange(2)) for b in buys for s in sells}
            allowed=lambda a,b:legal[a,b]
            previous=rng.randint(1,6)
            ranked=[]
            for price in {previous}|{o.limit for o in buys+sells}:
                demand=[o.quantity if o.limit>=price else 0 for o in buys]
                supply=[o.quantity if o.limit<=price else 0 for o in sells]
                volume=0
                for flow in itertools.product(range(4),repeat=4):
                    if any(flow[2*i]+flow[2*i+1]>demand[i] for i in range(2)):continue
                    if any(flow[j]+flow[j+2]>supply[j] for j in range(2)):continue
                    if any(flow[2*i+j] and not legal[buys[i].owner,sells[j].owner] for i in range(2) for j in range(2)):continue
                    volume=max(volume,sum(flow))
                ranked.append((-volume,abs(sum(demand)-sum(supply)),abs(price-previous),price))
            expected=min(ranked)
            result=clear_compatible(seed,0,'m',tuple(buys+sells),previous,allowed)
            self.assertEqual(result.auction.quantity,-expected[0])
            self.assertEqual(result.auction.price,expected[-1] if expected[0] else previous)
            byid={o.id:o for o in buys+sells}
            self.assertTrue(all(allowed(byid[b].owner,byid[s].owner) for b,s,q in result.matches))
            self.assertEqual(sum(q for _,_,q in result.matches),result.auction.quantity)
            reverse=clear_compatible(seed,0,'m',tuple(reversed(buys+sells)),previous,allowed)
            self.assertEqual(result,reverse)

class CompatibleSettlementTests(unittest.TestCase):
    def test_stock_and_cash_use_legal_pairs_and_seller_splits_do_not_gain_rounding(self):
        from organizer.rift_era.engine.ledger import Inventory,CashLedger,Account,CashAccount
        from organizer.rift_era.engine.spot import SpotExchange,Venue,RestingOrder
        from organizer.rift_era.engine.determinism import fingerprint
        results=[]
        for split in (False,True):
            inventory,cash=Inventory(),CashLedger()
            for owner in ('A','B','C','D'):
                inventory.initialize(Account(owner,'h','available','steel'),3)
                cash.initialize(CashAccount(owner,'treasury','RU'),100)
            inventory.seal();cash.seal()
            market=SpotExchange(inventory,cash)
            market.add_venue(Venue('m','h','steel','RU',500))
            orders=[('A','buy',3),('B','buy',3),('D','sell',3)]
            orders+= [('C','sell',1),('C','sell',2)] if split else [('C','sell',3)]
            for i,(owner,side,quantity) in enumerate(orders):
                order=RestingOrder(f'{owner}/{i+1}',str(i+1),owner,'m',side,quantity,500,100,
                    CashAccount(owner,'treasury','RU'),Account(owner,'h','available' if side=='sell' else 'next_available','steel'))
                market.prepare_order(order,tick=0).commit()
            allowed=lambda b,s:(b,s) in {('A','D'),('B','C')}
            clearing=market.clear(1,1,'m',allowed=allowed)
            self.assertEqual(clearing.quantity_mU,6)
            self.assertEqual(clearing.paid_mU,2)
            self.assertTrue(all(allowed(t.buyer,t.seller) for t in clearing.trades))
            self.assertTrue(all(allowed(b,s) for b,s,q in clearing.cash_flows))
            market.audit();cash.audit();inventory.audit()
            results.append(fingerprint((inventory.snapshot(),cash.snapshot(),market.snapshot())))
        self.assertEqual(*results)


class ManyOriginGroupsTests(unittest.TestCase):
    def test_forty_supply_groups_use_two_buyer_capacity_constraints(self):
        from organizer.rift_era.engine.constrained_market import _feasible_fills
        sellers=tuple(Order(str(i),f'supplier-{i}','sell',10,1000) for i in range(40))
        opposite={'even':30,'odd':70}
        allowed=lambda seller,buyer:('even' if int(seller.split('-')[1])%2==0 else 'odd')==buyer
        fills=_feasible_fills(sellers,opposite,allowed,100,seed=1,tick=0,market='origin-groups',side='sell',quantum=1)
        self.assertEqual(sum(q for oid,q in fills.items() if int(oid)%2==0),30)
        self.assertEqual(sum(q for oid,q in fills.items() if int(oid)%2==1),70)
        self.assertTrue(all(q<=10 for q in fills.values()))

    def test_unreachable_group_cannot_receive_rounding_tail(self):
        from organizer.rift_era.engine.constrained_market import _feasible_fills
        sellers=tuple(Order(str(i),str(i),'sell',3,1000) for i in range(5))
        fills=_feasible_fills(sellers,{'buyer':7},lambda s,b:s!='0',7,
            seed=1,tick=0,market='closed-empty',side='sell',quantum=1)
        self.assertNotIn('0',fills)
        self.assertEqual(sum(fills.values()),7)
