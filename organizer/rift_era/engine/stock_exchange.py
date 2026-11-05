"""Whole-share DVP with exact, account-scoped fees and historical cash basis.

Custody and four-stage auction rules reuse SpotExchange. Financial settlement
keeps stock principal, buyer fee, seller fee and refund as distinct purposes;
their RU costs are allocated from the ACTUAL CashLedger transaction, never
reconstructed from a live exchange rate.
"""
from collections import defaultdict
from dataclasses import dataclass,field,replace

from .errors import InvariantViolation,RuleViolation
from .determinism import fingerprint
from .ledger import CashAccount,CashTransfer,DeliveryPayment,Transfer,CASH_BOOK_SCALE
from .numeric import Q,ceil,split_payment
from .protocol import PreparedCommand
from .rational_carry import RationalCarry
from .spot import SpotExchange,Clearing,Trade,_pair_totals


@dataclass(frozen=True)
class StockClearing(Clearing):
    seller_gross_receipts: dict[str,int] = field(default_factory=dict)
    seller_fees: dict[str,int] = field(default_factory=dict)
    buyer_cost_book_ru_mU: dict[str,Q] = field(default_factory=dict)
    seller_capital_book_ru_mU: dict[str,Q] = field(default_factory=dict)


class StockExchange(SpotExchange):
    def __init__(self,inventory,cash,*,discounts=lambda owner:Q(0)):
        if inventory.materials is not None:raise ValueError('stock claims cannot be material goods')
        super().__init__(inventory,cash,max_orders=2**31,cash_namespace='stock_cash:')
        self.discounts=discounts
        self.fee_carries={}

    def prepare_issue(self,order,*,tick):
        """Trusted issuer adapter releases restricted stock directly to custody.

        A cancellation returns unissued shares to unissued custody. It cannot
        turn an abandoned IPO into freely resellable government inventory.
        """
        venue=self.venues.get(order.venue)
        if (venue is None or order.side!='sell' or order.stock_account.bucket!='unissued' or
                order.stock_account.owner!=order.owner or order.stock_account.asset!=venue.good or
                order.stock_account.location!=venue.hub or order.cash_namespace!=self.cash_namespace or
                order.remaining_mU<=0 or order.remaining_mU%1000 or order.limit_price_mU<=0 or
                order.id in self.orders or order.expires_tick<=tick or order.cash_account.owner!=order.owner or
                order.cash_account.currency!=venue.currency):
            raise InvariantViolation('invalid trusted government share release')
        stock=self.inventory.prepare_transfers((Transfer(order.stock_account,order.stock_lock,order.remaining_mU),))
        revision=self._revision
        def commit():
            self._guard(revision);stock.commit()
            self.orders[order.id]=order;self._revision+=1
        return PreparedCommand(commit)

    def _prepare_settlement(self,seed,tick,venue,auction,immediate_buyer=None,*,matches=None,material_plan=None):
        if immediate_buyer is not None or material_plan is not None or venue.quantum_mU!=1000:
            raise InvariantViolation('stocks require custodied whole-share orders')
        orders=self.orders
        context={}
        def fee(owner,nominal):
            key=(owner,venue.currency)
            if key not in context:context[key]=self.fee_carries.get(key,RationalCarry()).clone()
            discount=self.discounts(owner)
            if not 0<=discount<=Q(1,2):raise InvariantViolation('invalid financial fee discount')
            return context[key].add(nominal*Q(1,500)*(1-discount))
        due={};buyer_fees={};gross={};seller_fees=defaultdict(int)
        for oid,quantity in sorted(auction.buy_fills.items()):
            if quantity%1000:raise InvariantViolation('fractional stock share fill')
            due[oid]=quantity//1000*auction.price
        # Coalesce the real participant before fee rounding. Splitting orders
        # cannot reset carry or buy an extra rounding entitlement.
        buyer_groups=defaultdict(dict)
        for oid,amount in due.items():buyer_groups[orders[oid].owner][oid]=amount
        for owner,rows in sorted(buyer_groups.items()):
            total=fee(owner,sum(rows.values()))
            buyer_fees.update(split_payment(total,{oid:Q(q) for oid,q in rows.items()},
                key=(seed,'stock_buyer_fee',tick,venue.id,owner)))
        seller_groups=defaultdict(dict)
        for oid,quantity in sorted(auction.sell_fills.items()):
            if quantity%1000:raise InvariantViolation('fractional stock share fill')
            gross[oid]=quantity//1000*auction.price
            seller_groups[orders[oid].owner][oid]=gross[oid]
        for owner,rows in sorted(seller_groups.items()):
            total=fee(owner,sum(rows.values()))
            accounts=defaultdict(dict)
            for oid,amount in rows.items():accounts[orders[oid].cash_account][oid]=amount
            names={fingerprint(a):a for a in accounts}
            account_fees=split_payment(total,{name:Q(sum(accounts[a].values())) for name,a in names.items()},
                key=(seed,'stock_seller_fee',tick,venue.id,owner))
            for name,account in names.items():
                seller_fees.update(split_payment(account_fees[name],{oid:Q(q) for oid,q in accounts[account].items()},
                    key=(seed,'stock_seller_fee_attribution',tick,venue.id,owner,name)))
        pairs=tuple(_pair_totals(auction.buy_fills,auction.sell_fills) if matches is None else matches)
        buys=defaultdict(int);sells=defaultdict(int)
        for bid,sid,q in pairs:
            if q<=0 or q%1000:raise InvariantViolation('invalid stock matching')
            buys[bid]+=q;sells[sid]+=q
        if dict(buys)!=dict(auction.buy_fills) or dict(sells)!=dict(auction.sell_fills):
            raise InvariantViolation('stock matching differs from its auction')
        # Split seller fee only among the buyers actually buying those shares.
        fee_parts={}
        for sid,amount in seller_fees.items():
            members={str(i):Q(q) for i,(_,s,q) in enumerate(pairs) if s==sid}
            fee_parts[sid]=split_payment(amount,members,key=(seed,'stock_fee_legs',tick,venue.id,sid))
        money=[];goods=[];trades=[];cash_flows=[];income=defaultdict(int)
        roles=defaultdict(lambda:defaultdict(int))
        world=CashAccount('world','pool',venue.currency)
        def leg(source,target,amount,role):
            if not amount:return
            money.append(CashTransfer(source,target,amount));roles[source,target][role]+=amount
        for oid,amount in buyer_fees.items():leg(orders[oid].cash_lock,world,amount,('buyer_fee',oid,''))
        for index,(bid,sid,q) in enumerate(pairs):
            buy,sell=orders[bid],orders[sid];principal=q//1000*auction.price
            seller_fee=fee_parts[sid].get(str(index),0)
            leg(buy.cash_lock,sell.cash_account,principal-seller_fee,('principal',bid,sid))
            leg(buy.cash_lock,world,seller_fee,('principal',bid,sid))
            income[sell.cash_account]+=principal-seller_fee
            goods.append(Transfer(sell.stock_lock,buy.stock_account,q))
            trades.append(Trade(buy.owner,sell.owner,venue.hub,venue.good,q,bid,sid))
            cash_flows.append((buy.owner,sell.owner,principal))
        updated={}
        for fills in (auction.buy_fills,auction.sell_fills):
            for oid,q in fills.items():
                order=orders[oid];remaining=order.remaining_mU-q
                if remaining<0:raise InvariantViolation('stock auction overfilled custody')
                updated[oid]=replace(order,remaining_mU=remaining) if remaining else None
                if not remaining and order.side=='buy':
                    refund=self.cash.balance(order.cash_lock).native_mU-due[oid]-buyer_fees[oid]
                    if refund<0:raise InvariantViolation('stock invoice exceeds cash custody')
                    leg(order.cash_lock,order.cash_account,refund,('refund',oid,''))
        transaction=DeliveryPayment.prepare(self.inventory,self.cash,goods,money)
        buyer_basis=defaultdict(lambda:Q(0));seller_basis=defaultdict(lambda:Q(0))
        for move in transaction.cash.movements:
            components=roles[move.source,move.destination]
            if sum(components.values())!=move.native_mU:raise InvariantViolation('stock cost purposes do not balance')
            total=move.book_ru_mU*CASH_BOOK_SCALE
            if total.denominator!=1:raise InvariantViolation('stock cost left the cash auxiliary grid')
            keys=sorted(components)
            parts=split_payment(int(total),{str(i):Q(components[k]) for i,k in enumerate(keys)},
                key=(seed,'stock_cost_basis',tick,venue.id,move.source.owner,move.source.name,
                     move.destination.owner,move.destination.name))
            for i,(purpose,bid,sid) in enumerate(keys):
                if purpose=='principal':
                    cost=Q(parts[str(i)],CASH_BOOK_SCALE)
                    buyer_basis[bid]+=cost;seller_basis[sid]+=cost
        revision=self._revision
        def commit():
            self._guard(revision);transaction.commit()
            self.fee_carries.update(context)
            for oid,order in updated.items():
                if order:self.orders[oid]=order
                else:del self.orders[oid]
            self._revision+=1
        return StockClearing(venue=venue.id,tick=tick,price_mU=auction.price,quantity_mU=auction.quantity,
            paid_mU=sum(due.values()),tariff_mU=sum(buyer_fees.values()),buy_fills=dict(auction.buy_fills),
            sell_fills=dict(auction.sell_fills),trades=tuple(trades),seller_receipts={oid:q-seller_fees[oid] for oid,q in gross.items()},
            buyer_payments=due,buyer_tariffs=buyer_fees,cash_flows=tuple(cash_flows),
            income_accounts=tuple((a.owner,a.name,a.currency,q) for a,q in sorted(income.items()) if q),
            seller_gross_receipts=gross,seller_fees=dict(seller_fees),buyer_cost_book_ru_mU=dict(buyer_basis),
            seller_capital_book_ru_mU=dict(seller_basis)),commit

    def audit(self):
        super().audit()
        for carry in self.fee_carries.values():carry.audit()

    def snapshot(self):
        return super().snapshot()|{'stock_fee_carries':[[*key,carry.snapshot()] for key,carry in sorted(self.fee_carries.items())]}
