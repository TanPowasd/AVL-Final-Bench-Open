"""Financial face custody using the shared, cash-backed DVP auction service.

The face ledger deliberately has no MaterialBook: debt claims are securities,
not physical goods, and can never earn industrial consumption/transit credit.
"""
from collections import defaultdict
from dataclasses import replace

from .command_schema import fields
from .errors import InvariantViolation, RuleViolation
from .ledger import Account, CashAccount, Inventory, CASH_BOOK_SCALE
from .numeric import Q, ceil, floor, proportional, split_payment
from .spot import SpotExchange, Venue, RestingOrder
from .production import commit_buffers


class FinancialExchange(SpotExchange):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.buyer_books={}

    def _after_settlement(self,result,cash_plan,participating):
        # Read the actual conserved CashLedger movements, including the
        # multi-seller allocation/refund tails.  Multiplying the average cost
        # by the nominal invoice would round a different quantity.
        books={}
        for oid,paid in result.buyer_payments.items():
            order=participating[oid]
            source=order.cash_lock
            refunds=order.cash_account
            books[oid]=sum((m.book_ru_mU for m in cash_plan.movements
                if m.source==source and m.destination!=refunds),Q(0))
        self.buyer_books=books

    def snapshot(self):
        return super().snapshot() | {'buyer_books':dict(self.buyer_books)}

    def _quote(self, payer, venue, quantity, price, residuals):
        # A new or retired security must not reset the payer's sub-mU debt.
        # This book has only bond principal, so one currency-wide carry is
        # sufficient and bounds storage independently of issued series count.
        scope = replace(venue, id='bond_principal:' + venue.currency)
        return super()._quote(payer, scope, quantity, price, residuals)


class BondMarket:
    def __init__(self, state):
        self.s = state
        self.faces = Inventory()
        self.faces.seal()
        # Nation limits are enforced in the adapter. Temporary world quotes
        # share actual pool cash and are not a strategy's persistent orders.
        self.exchange = FinancialExchange(self.faces, state.initial.cash, max_orders=2**31, cash_namespace="bond_cash:")
        self.reports = {p: [] for p in state.initial.known}
        self.last_cycle = -1
        self.position_books={}

    @staticmethod
    def account(owner, bond_id):
        return Account(owner, 'bond_clearing', 'available', bond_id)

    def register(self, bond, *, initial_books=None):
        if bond.id in self.exchange.venues:
            raise InvariantViolation('bond face registered twice')
        self.exchange.add_venue(Venue(bond.id, 'bond_clearing', bond.id, bond.issuer, 1000))
        self.faces.transform({}, {self.account(h,bond.id):q for h,q in bond.holders_face_mU.items()}, reason='bond_face_issued')
        for holder,book in (initial_books or {}).items():
            if holder!=bond.issuer:self.position_books[holder,bond.id]=Q(book)

    def add_face(self, bond, owner, amount, *, book=Q(0)):
        if amount:
            self.faces.transform({}, {self.account(owner,bond.id):amount}, reason='bond_face_issued')
            if owner!=bond.issuer:self.position_books[owner,bond.id]=self.position_books.get((owner,bond.id),Q(0))+book

    def ownership(self, bond_id):
        result = defaultdict(int)
        for account, amount in self.faces.balances().items():
            if account.asset == bond_id:
                result[account.owner] += amount
        return dict(result)

    def _dispose_book(self,holder,bond_id,face,before_face):
        key=holder,bond_id
        book=self.position_books.get(key,Q(0))
        removed=book if face==before_face else Q(round(book*face/before_face*CASH_BOOK_SCALE),CASH_BOOK_SCALE)
        if removed==book:self.position_books.pop(key,None)
        else:self.position_books[key]=book-removed

    def asset_book(self,owner):
        return sum((book for (holder,_),book in self.position_books.items() if holder==owner),Q(0))

    def apply_service(self, bond, before, *, principal_paid=None, capitalized_face=None, capitalized_book=Q(0)):
        consumed, produced = {}, {}
        for holder,old in before.items():
            paid=(principal_paid or {}).get(holder,0)
            if paid:self._dispose_book(holder,bond.id,paid,old)
        for holder in before.keys() | bond.holders_face_mU.keys():
            change = bond.holders_face_mU.get(holder,0) - before.get(holder,0)
            if change > 0:produced[self.account(holder,bond.id)] = change
            elif change < 0:consumed[self.account(holder,bond.id)] = -change
        self.faces.transform(consumed,produced,reason='bond_service_face_change')
        if capitalized_book and bond.face:
            added={h:q for h,q in (capitalized_face or {}).items() if q>0}
            if added:
                allocation=split_payment(int(round(capitalized_book*CASH_BOOK_SCALE)),{h:Q(q) for h,q in added.items()},key=('bond_capitalized_book',bond.id))
                for holder,q in allocation.items():
                    if holder!=bond.issuer:self.position_books[holder,bond.id]=self.position_books.get((holder,bond.id),Q(0))+Q(q,CASH_BOOK_SCALE)
        if not bond.face:
            self.retire_venue(bond.id)

    def retire_venue(self, bond_id):
        for order in tuple(self.exchange.orders.values()):
            if order.venue == bond_id:
                self.exchange.prepare_cancel(order.owner,order.id).commit()
        self.exchange.venues.pop(bond_id)
        self.exchange.prices.pop(bond_id)
        self.exchange.last_clear_tick.pop(bond_id,None)
        self.exchange.residuals = {k:v for k,v in self.exchange.residuals.items() if k[3] != bond_id}

    def before_service(self, ctx):
        # Redemption ends resting offers; all promised face is first unlocked.
        # A defaulted/rolled series can receive new orders next decision window.
        for order in tuple(self.exchange.orders.values()):
            bond = self.s.sovereign_finance.bonds[order.venue]
            if bond.due_cycle <= ctx.cycle:
                self.exchange.prepare_cancel(order.owner,order.id).commit()

    def reference_price(self, bond, cycle):
        remaining = max(0,bond.due_cycle-cycle)
        if not remaining:return 1000
        discount = Q(1,1) / (1 + Q(self.s.sovereign_finance.rate(bond.issuer,cycle),1000000))
        last = discount**remaining
        annuity = Q(remaining) if discount == 1 else discount*(1-last)/(1-discount)
        coupons = annuity * Q(bond.annual_rate_bp,1000000)
        return max(1,round(1000*(coupons+last)))

    def _order(self, owner, command_id, bond, side, quantity, price, tick, *, world=False):
        oid = f'bond_npc:{side}:{bond.id}' if world else f'bond_order:{owner}:{command_id}'
        return RestingOrder(oid,command_id,owner,bond.id,side,quantity,price,
            tick+1 if world else 2**63,
            CashAccount(owner,'pool' if world else 'treasury',bond.issuer),
            Account(owner,'bond_clearing','available' if side=='sell' else 'next_available',bond.id), cash_namespace='bond_cash:')

    def clear(self, ctx):
        if not ctx.clock.is_cycle_end(ctx.tick):return
        if ctx.cycle <= self.last_cycle:raise InvariantViolation('bond secondary auction repeated')
        self.last_cycle = ctx.cycle
        state = self.s
        quotes, desired = {}, defaultdict(dict)
        for bond in state.sovereign_finance.bonds.values():
            fair = self.reference_price(bond,ctx.cycle)
            offers = [o for o in self.exchange.orders.values() if o.venue==bond.id]
            demand = sum(o.remaining_mU for o in offers if o.side=='sell' and o.limit_price_mU<=fair)
            if demand:desired[bond.issuer][bond.id] = ceil(Q(demand*fair,1000))
            quotes[bond.id] = fair
        budgets = {}
        for currency,requests in desired.items():
            cash = state.initial.cash.balance(CashAccount('world','pool',currency)).native_mU
            budgets.update(proportional(cash,requests,key=(ctx.seed,'bond_secondary_pool',ctx.cycle,currency)))
        # Reserve the whole currency batch before any venue can spend receipts
        # from another venue. NPC proceeds cannot create intra-batch credit.
        for bond in state.sovereign_finance.bonds.values():
            fair = quotes[bond.id]
            quantity = floor(Q(budgets.get(bond.id,0)*1000,fair))
            if quantity:
                order = self._order('world','world',bond,'buy',quantity,fair,ctx.tick,world=True)
                self.exchange.prepare_order(order,tick=ctx.tick).commit()
            quantity = self.faces.balance(self.account('world',bond.id))
            if quantity:
                order = self._order('world','world',bond,'sell',quantity,fair,ctx.tick,world=True)
                self.exchange.prepare_order(order,tick=ctx.tick).commit()
        self.reports = {p:[] for p in state.initial.known}
        for bond in tuple(state.sovereign_finance.bonds.values()):
            previous = self.exchange.prices[bond.id]
            prior=dict(self.exchange.orders)
            before=self.ownership(bond.id)
            result = self.exchange.clear(ctx.seed,ctx.tick,bond.id,
                allowed=lambda a,b:a!=b and state.relations.trade_allowed(a,b),
                price_band=(max(1,ceil(Q(4,5)*previous)),max(1,floor(Q(6,5)*previous))))
            disposals=defaultdict(int)
            for oid,q in result.sell_fills.items():disposals[prior[oid].owner]+=q
            for holder,q in disposals.items():self._dispose_book(holder,bond.id,q,before[holder])
            for oid in result.buy_fills:
                holder=prior[oid].owner
                if holder!=bond.issuer:
                    key=holder,bond.id
                    book=self.exchange.buyer_books[oid]
                    self.position_books[key]=self.position_books.get(key,Q(0))+book
            bond.holders_face_mU = self.ownership(bond.id)
            for buyer,seller,paid in result.cash_flows:
                if buyer == seller:continue
                flow = paid*state.rates[bond.issuer]
                if buyer in state.macro.cycles:state.macro.cycles[buyer].portfolio_inflow_ru_mU -= flow
                if seller in state.macro.cycles:state.macro.cycles[seller].portfolio_inflow_ru_mU += flow
            for trade in result.trades:
                for owner,side in ((trade.buyer,'buy'),(trade.seller,'sell')):
                    if owner in self.reports:
                        self.reports[owner].append({'bond_id':state.sovereign_finance.aliases[owner][bond.id],
                            'side':side,'face_mU':trade.quantity_mU,'price_mU':result.price_mU})
        for order in tuple(self.exchange.orders.values()):
            if order.owner=='world':self.exchange.prepare_cancel('world',order.id).commit()
        commit_buffers(self.faces)
        for bond in tuple(state.sovereign_finance.bonds.values()):
            repurchased = bond.holders_face_mU.pop(bond.issuer,0)
            if repurchased:
                before_face=bond.face+repurchased
                removed=bond.proceeds_ru_mU if repurchased==before_face else Q(round(bond.proceeds_ru_mU*repurchased/before_face*CASH_BOOK_SCALE),CASH_BOOK_SCALE)
                bond.proceeds_ru_mU-=removed
                self.faces.transform({self.account(bond.issuer,bond.id):repurchased},{},reason='sovereign_buyback')
                if not bond.face:
                    self.retire_venue(bond.id)
                    del state.sovereign_finance.bonds[bond.id]
        state.sovereign_finance.publish()

    def cancel(self, owner, command_id):
        oid = f'bond_order:{owner}:{command_id}'
        if oid not in self.exchange.orders:return None
        return self.exchange.prepare_cancel(owner,oid)

    def visible(self, owner):
        aliases = self.s.sovereign_finance.aliases[owner]
        return {'prices':{aliases[bid]:price for bid,price in self.exchange.prices.items() if bid in aliases},
                'orders':[{'command_id':o.command_id,'bond_id':aliases[o.venue],'side':o.side,
                           'remaining_mU':o.remaining_mU,'limit_price_mU':o.limit_price_mU}
                          for o in self.exchange.orders.values() if o.owner==owner and o.venue in aliases],
                'fills':[dict(row) for row in self.reports[owner]]}

    def audit(self):
        self.faces.audit();self.exchange.audit()
        if any(book<0 or holder==self.s.sovereign_finance.bonds[bid].issuer or not self.ownership(bid).get(holder,0)
               for (holder,bid),book in self.position_books.items()):
            raise InvariantViolation('orphan or negative historical bond asset basis')
        for bond in self.s.sovereign_finance.bonds.values():
            if self.ownership(bond.id) != bond.holders_face_mU:
                raise InvariantViolation('bond claim custody differs from outstanding face')
        if set(self.exchange.venues) != set(self.s.sovereign_finance.bonds):
            raise InvariantViolation('bond venue outlived its liability')

    def snapshot(self):
        return {'faces':self.faces.snapshot(),'exchange':self.exchange.snapshot(),
                'reports':self.reports,'last_cycle':self.last_cycle,
                'position_books':[[*key,book] for key,book in sorted(self.position_books.items())]}


class BondOrderCommands:
    OPERATIONS = frozenset({('finance','bond_order')})
    def __init__(self,state):self.state=state

    def prepare(self,owner,order,ctx):
        fields(order,{'bond_id':str,'side':str,'qty_mU':int,'limit_price_mU':int})
        if order['side'] not in ('buy','sell') or order['qty_mU']<=0 or order['limit_price_mU']<=0:
            raise RuleViolation('ERR_SCHEMA')
        state=self.state;market=state.bond_market
        bid=next((key for key,alias in state.sovereign_finance.aliases[owner].items() if alias==order['bond_id']),None)
        bond=state.sovereign_finance.bonds.get(bid)
        if bond is None:raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        if state.active_order_count(owner)>=state.catalog.runtime['limits']['active_orders_per_nation']:
            raise RuleViolation('ERR_ENTITY_CAP_EXCEEDED')
        row=market._order(owner,str(int(order['id'])),bond,order['side'],order['qty_mU'],order['limit_price_mU'],ctx.tick)
        return market.exchange.prepare_order(row,tick=ctx.tick)
