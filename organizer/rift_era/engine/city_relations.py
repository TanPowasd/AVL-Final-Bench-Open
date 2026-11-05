"""City-state leases, final-use aid evidence and controlled re-export quotas."""
from dataclasses import dataclass
from collections import defaultdict

from .ledger import Account, CashAccount, CashTransfer, DeliveryPayment, Transfer
from .numeric import Q, ceil, floor
from .errors import RuleViolation, InvariantViolation
from .provenance import Use
from .economic_ownership import EconomicOwner
from .materials import Origin
from .protocol import PreparedCommand
from .spot import SpotExchange, Venue, RestingOrder
from .numeric import units


@dataclass(frozen=True)
class CityTransitRight:
    grantor: str
    grantee: str
    corridors: tuple
    maximum_flow_mU: int
    toll_cap_ru_mU: int = 100
    allow_basing: bool = False


class CityRelations:
    def __init__(self, cities):
        self.book=cities;self.s=cities.s
        self.aid_stocks={}
        self.aid_cycles={}
        self.reexport_used={}
        self.reexport_caps={}
        self.leak_cycles={}
        self.last_cycle=-1
        # Reuse the canonical spot custody/clearing book.  A second SpotExchange
        # would leave its locks invisible to the global audit and could permit
        # the same physical city order to clear twice.
        self.exchange=self.s.spot

    def begin_cycle(self,ctx):
        self.exchange.expire(ctx.tick)
        if ctx.tick%ctx.clock.cycle_ticks:return
        if ctx.cycle<=self.last_cycle:raise InvariantViolation('city reexport cycle repeated')
        self.last_cycle=ctx.cycle
        from .export_controls import CONTROLLED_GOODS
        for city in self.book.cities.values():
            for good in sorted(CONTROLLED_GOODS & self.s.catalog.goods.keys()):
                if self.s.catalog.goods[good]['kind']!='inventory':continue
                amount=self.s.initial.inventory.balance(self.book.stock(city,good))
                quantum=1000 if self.s.catalog.goods[good].get('indivisible') else 1
                quantity=amount//(10*quantum)*quantum
                if not quantity:continue
                venue=Venue(f'city_export:{city.id}:{good}',city.hub,good,'RU',units(10*self.s.catalog.weights[good]),quantum)
                sid=f'{city.id}/city_reexport:{good}:{ctx.cycle}'
                order=RestingOrder(sid,'npc',city.id,venue.id,'sell',quantity,venue.initial_price_mU,
                    (ctx.cycle+1)*ctx.clock.cycle_ticks,city.treasury,self.book.stock(city,good))
                self.exchange.prepare_order(order,tick=ctx.tick,new_venue=venue if venue.id not in self.exchange.venues else None).commit()

    def prepare_reexport_order(self,nation,order,ctx):
        city=self.book.cities.get(order['city_state_id']);good=order['good']
        if city is None or good not in self.s.catalog.goods:raise RuleViolation('ERR_SCHEMA')
        venue=self.exchange.venues.get(f'city_export:{city.id}:{good}')
        if venue is None:raise RuleViolation('ERR_NO_VISIBLE_TARGET','no physical city reexport supply')
        if self.s.relations.will_be_at_war(nation,city.id):raise RuleViolation('ERR_NO_PERMISSION')
        quantity=order['qty_mU']
        if quantity<=0 or order['price_ru_mU']!=venue.initial_price_mU:raise RuleViolation('ERR_SCHEMA')
        oid=f'{nation}/{int(order["id"])}'
        bid=RestingOrder(oid,str(int(order['id'])),nation,venue.id,'buy',quantity,venue.initial_price_mU,
            (ctx.cycle+1)*ctx.clock.cycle_ticks,CashAccount(nation,'reserves','RU'),
            Account(nation,city.hub,'next_available',good))
        return self.exchange.prepare_order(bid,tick=ctx.tick)

    def clear(self,ctx):
        """One common P5 auction for actual leakage stock and prelocked bids."""
        if not ctx.clock.is_window_end(ctx.tick):return ()
        clearings=[]
        active=sorted({o.venue for o in self.exchange.orders.values() if o.venue.startswith('city_export:')})
        for venue in active:
            row=self.exchange.clear(ctx.seed,ctx.tick,venue,
                allowed=lambda buyer,seller:not self.s.relations.will_be_at_war(buyer,seller))
            clearings.append(row)
            self.s.market_events.append(row)
            for buyer,seller,amount in row.cash_flows:self.s.macro.trade(buyer,seller,amount)
            from .economic_ownership import record_income
            accounts=tuple(CashAccount(p,n,c) for p,n,c,q in row.income_accounts)
            record_income(self.s,accounts,tuple(q for p,n,c,q in row.income_accounts))
        return tuple(clearings)

    def transit_right(self,nation,edge_id,cycle):
        edge=self.s.cargo.corridors.get(edge_id)
        if edge is None:return None
        cities=[c for c in self.book.cities.values() if c.hub in (edge.a,edge.b) and self.book.transit_lease(c.id,nation,cycle)]
        if not cities:return None
        # Lease covers the city's own shore/approach; another foreign endpoint
        # still requires its independent actual permission.
        for hub in (edge.a,edge.b):
            owner=self.s.authority.regions[self.s.initial.map.hubs[hub].region].controller
            if owner not in (None,nation) and not any(c.id==owner for c in cities):return None
        city=min(cities,key=lambda c:c.id)
        return CityTransitRight(city.id,nation,(edge_id,),edge.capacity_mU_cycle)

    def entry_fee(self,nation,edge_id,weight_mU,cycle):
        right=self.transit_right(nation,edge_id,cycle)
        if right is None:return None
        return (CashAccount(right.grantor,'treasury','RU'),ceil(Q(weight_mU,10)))

    def pay_entry(self,nation,edge_id,weight_mU,cycle):
        fee=self.entry_fee(nation,edge_id,weight_mU,cycle)
        if fee is None:return None
        recipient,amount=fee
        payer=CashAccount(nation,'reserves','RU')
        if self.s.initial.cash.balance(payer).native_mU<amount:return False
        self.s.initial.cash.transfer(payer,recipient,amount)
        self.s.macro.trade(nation,recipient.owner,amount)
        return True

    def grant_aid(self,nation,city_id,good,quantity_mU,*,tick):
        self.prepare_aid(nation,city_id,good,quantity_mU,tick=tick).commit()

    def prepare_aid(self,nation,city_id,good,quantity_mU,*,tick):
        city=self.book.cities.get(city_id)
        if city is None or good not in self.s.catalog.goods or type(quantity_mU)is not int or quantity_mU<=0:
            raise RuleViolation('ERR_SCHEMA')
        source=Account(nation,city.hub,'available',good)
        target=Account(city_id,city.hub,'city_aid:'+nation,good)
        if good not in self.book.NEEDS:raise RuleViolation('ERR_SCHEMA','city aid must be an actual resident basket good')
        if self.s.relations.will_be_at_war(nation,city_id):raise RuleViolation('ERR_NO_PERMISSION')
        for origin in self.s.initial.inventory.materials.composition(source):
            title=origin.economic_owner or EconomicOwner(nation,'treasury')
            if title!=EconomicOwner(nation,'treasury'):raise RuleViolation('ERR_NO_PERMISSION','city gifts require government-owned supplies')
        # Real gift already physically at the recipient; cash aid has no receipt.
        plan=self.s.initial.inventory.prepare_transfers((Transfer(source,target,quantity_mU,retitle=True),))
        def commit():
            plan.commit()
            self.aid_stocks[city_id,nation,good]=self.aid_stocks.get((city_id,nation,good),0)+quantity_mU
        return PreparedCommand(commit)

    def consume_aid(self,city,good,need,ctx):
        candidates={nation:q for (cid,nation,g),q in self.aid_stocks.items() if cid==city.id and g==good and q}
        from .numeric import proportional
        allocation=proportional(need,candidates,key=(ctx.seed,'city_aid_use',ctx.tick,city.id,good))
        used=0
        for nation,quantity in allocation.items():
            if not quantity:continue
            account=Account(city.id,city.hub,'city_aid:'+nation,good)
            # Aid eligibility belongs to this irreversible use.  Marking it as
            # EXTRACTION prevents the common trade pipeline recognizing the
            # same physical receipt again as ordinary export/industry sink.
            self.s.initial.inventory.transform({account:quantity},{},reason='city_aid_consumed',use=Use.EXTRACTION)
            row=self.aid_cycles.setdefault(ctx.cycle,{})
            value=Q(quantity,1000)*self.s.catalog.weights[good]
            row[city.id,nation]=row.get((city.id,nation),Q(0))+value
            self.aid_stocks[city.id,nation,good]-=quantity
            used+=quantity
        self.aid_stocks={k:q for k,q in self.aid_stocks.items() if q}
        self.aid_cycles={c:v for c,v in self.aid_cycles.items() if c>=ctx.cycle-19}
        return used

    def aid_mean20(self,nation):
        return sum((v for row in self.aid_cycles.values() for (cid,p),v in row.items() if p==nation),Q(0))/20

    def aid_value(self,city,nation):
        return sum((row.get((city,nation),Q(0)) for row in self.aid_cycles.values()),Q(0))

    def reexport_limit(self,city_id,buyer,good,cycle):
        city=self.book.cities.get(city_id)
        if city is None:return 0
        # Only actual controlled-origin stock in this local city can leak.
        from .export_controls import CONTROLLED_GOODS
        if good not in CONTROLLED_GOODS:return 0
        stock=self.book.stock(city,good)
        origins=self.s.initial.inventory.materials.composition(stock)
        controlled=sum((q for o,q in origins.items() if not self.s.export_controls.direct_allowed(buyer,o.nation,good)),Q(0))
        quantum=1000 if self.s.catalog.goods[good].get('indivisible') else 1
        # The city has one physical leakage budget for this good; using a
        # second buyer cannot multiply the same stock's ten-percent allowance.
        key=(cycle,city_id,good)
        total=self.reexport_caps.get(key,floor(controlled/10/quantum)*quantum)
        used=self.reexport_used.get(key,0)
        return max(0,total-used)

    def reexport(self,city_id,buyer,good,quantity_mU,price_ru_mU,ctx):
        self.prepare_reexport(city_id,buyer,good,quantity_mU,price_ru_mU,ctx).commit()
        return quantity_mU

    def prepare_reexport(self,city_id,buyer,good,quantity_mU,price_ru_mU,ctx):
        city=self.book.cities.get(city_id)
        if city is None or type(quantity_mU)is not int or quantity_mU<=0 or type(price_ru_mU)is not int or price_ru_mU<=0:
            raise RuleViolation('ERR_SCHEMA')
        if self.s.relations.will_be_at_war(buyer,city_id):raise RuleViolation('ERR_NO_PERMISSION')
        if quantity_mU>self.reexport_limit(city_id,buyer,good,ctx.cycle):raise RuleViolation('ERR_NO_PERMISSION','city controlled re-export exceeds ten percent')
        source=self.book.stock(city,good);target=Account(buyer,city.hub,'next_available',good)
        quantum=1000 if self.s.catalog.goods[good].get('indivisible') else 1
        if quantity_mU%quantum:raise RuleViolation('ERR_SCHEMA')
        total=ceil(Q(quantity_mU*price_ru_mU,1000))
        # Quota is frozen on first actual sale, not on a read-only query.
        cap=self.reexport_limit(city_id,buyer,good,ctx.cycle)+self.reexport_used.get((ctx.cycle,city_id,good),0)
        origins=self.s.initial.inventory.materials.composition(source)
        by_origin=defaultdict(Q)
        for origin,q in origins.items():
            if not self.s.export_controls.direct_allowed(buyer,origin.nation,good):by_origin[origin.nation]+=q
        remaining=quantity_mU;transfers=[]
        for origin,q in sorted(by_origin.items()):
            take=min(remaining,floor(q/quantum)*quantum)
            if take:transfers.append(Transfer(source,target,take,origin_nation=origin,retitle=True));remaining-=take
        if remaining:raise RuleViolation('ERR_INSUFFICIENT_STOCK')
        plan=DeliveryPayment.prepare(self.s.initial.inventory,self.s.initial.cash,
            transfers,
            (CashTransfer(CashAccount(buyer,'reserves','RU'),city.treasury,total),))
        key=(ctx.cycle,city_id,good)
        frozen_used=self.reexport_used.get(key,0)
        def commit():
            if self.reexport_used.get(key,0)!=frozen_used:raise InvariantViolation('city reexport quota changed before commit')
            plan.commit()
            self.reexport_used[key]=self.reexport_used.get(key,0)+quantity_mU
            self.reexport_caps[key]=cap
            self.reexport_used={k:q for k,q in self.reexport_used.items() if k[0]>=ctx.cycle}
            self.reexport_caps={k:q for k,q in self.reexport_caps.items() if k[0]>=ctx.cycle}
            self.s.macro.trade(buyer,city_id,total)
        return PreparedCommand(commit)

    def snapshot(self):
        return {'aid_stocks':[[*k,v] for k,v in sorted(self.aid_stocks.items())],
            'exchange':self.exchange.snapshot(),'last_cycle':self.last_cycle,
            'aid_cycles':{str(c):[[cid,p,v] for (cid,p),v in sorted(row.items())] for c,row in self.aid_cycles.items()},
            'reexport_used':[[*k,v] for k,v in sorted(self.reexport_used.items())],
            'reexport_caps':[[*k,v] for k,v in sorted(self.reexport_caps.items())]}

    def audit(self):
        for (cid,nation,good),quantity in self.aid_stocks.items():
            if quantity<0 or self.s.initial.inventory.balance(Account(cid,self.book.cities[cid].hub,'city_aid:'+nation,good))!=quantity:
                raise InvariantViolation('city aid custody differs from actual stock')
