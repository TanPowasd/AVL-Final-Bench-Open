"""Uniform-price clearing with forbidden bilateral counterparties.

The common unrestricted path preserves the existing auction exactly. When its
fills cannot be legally paired, use maximum legal volume and the same four
price criteria, then feasible proportional price bands (buyers before sellers).
Owner counts here are eight strategy seats plus venue-local NPCs, not order IDs.
"""
from collections import defaultdict, deque
from dataclasses import dataclass

from .determinism import digest
from .market import Auction, clear_auction
from .numeric import Q, floor, proportional


@dataclass(frozen=True)
class MatchedAuction:
    auction: Auction
    matches: tuple[tuple[str,str,int], ...]


def owner_flow(buy, sell, allowed):
    """Integer augmenting paths; aggregated owners bound the graph size."""
    graph=defaultdict(dict)
    source=('source',''); sink=('sink','')
    def edge(a,b,capacity):
        graph[a][b]=capacity
        graph[b][a]=0
    total=min(sum(buy.values()),sum(sell.values()))
    for b,q in sorted(buy.items()):
        edge(source,('buy',b),q)
    for s,q in sorted(sell.items()):
        edge(('sell',s),sink,q)
    for b in sorted(buy):
        for s in sorted(sell):
            if allowed(b,s):
                edge(('buy',b),('sell',s),total)
    volume=0
    while True:
        parent={source:None}; queue=deque([source])
        while queue and sink not in parent:
            a=queue.popleft()
            for b,capacity in graph[a].items():
                if capacity and b not in parent:
                    parent[b]=a; queue.append(b)
        if sink not in parent:
            break
        node=sink; amount=total-volume
        while parent[node] is not None:
            amount=min(amount,graph[parent[node]][node]);node=parent[node]
        node=sink
        while parent[node] is not None:
            a=parent[node];graph[a][node]-=amount;graph[node][a]+=amount;node=a
        volume+=amount
        if volume==total:
            break
    flows={(b,s):graph[('sell',s)].get(('buy',b),0) for b in buy for s in sell}
    return volume,{pair:q for pair,q in flows.items() if q}


def _totals(orders, fills=None, quantum=1):
    totals=defaultdict(int)
    for order in orders:
        totals[order.owner]+=(order.quantity if fills is None else fills.get(order.id,0))//quantum
    return dict(totals)


def _matches(buys,sells,buy_fills,sell_fills,allowed,quantum):
    volume,flows=owner_flow(_totals(buys,buy_fills,quantum),_totals(sells,sell_fills,quantum),allowed)
    if volume*quantum != sum(buy_fills.values()):
        return None
    buyers=defaultdict(list);sellers=defaultdict(list)
    for order in sorted(buys,key=lambda o:(o.priority,-o.limit,o.id)):
        if buy_fills.get(order.id,0):
            buyers[order.owner].append([order.id,buy_fills[order.id]])
    for order in sorted(sells,key=lambda o:(o.priority,o.limit,o.id)):
        if sell_fills.get(order.id,0):
            sellers[order.owner].append([order.id,sell_fills[order.id]])
    result=[]
    for (buyer,seller),amount in sorted(flows.items()):
        remaining=amount*quantum
        while remaining:
            b=buyers[buyer][-1];s=sellers[seller][-1]
            q=min(remaining,b[1],s[1])
            result.append((b[0],s[0],q));remaining-=q;b[1]-=q;s[1]-=q
            if not b[1]:buyers[buyer].pop()
            if not s[1]:sellers[seller].pop()
    return tuple(result)


def _feasible_fills(orders, opposite, allowed, quantity, *, seed,tick,market,side,quantum):
    owners=sorted({o.owner for o in orders})
    # Hall bounds may be enumerated on either shore. Origin-labelled supply
    # can have many more groups than the eight buyers: enumerate the smaller
    # shore instead of introducing an exponential dependence on seller lots.
    bounds=[]
    if len(owners) <= len(opposite):
        for mask in range(1,1<<len(owners)):
            subset=frozenset(owners[i] for i in range(len(owners)) if mask>>i&1)
            capacity=min(quantity,sum(q for other,q in opposite.items() if any(allowed(p,other) for p in subset)))
            bounds.append((subset,capacity))
    else:
        neighbors={p:frozenset(other for other in opposite if allowed(p,other)) for p in owners}
        other_side=sorted(opposite)
        closed={}
        for mask in range(1<<len(other_side)):
            reachable=frozenset(other_side[i] for i in range(len(other_side)) if mask>>i&1)
            subset=frozenset(p for p in owners if neighbors[p] <= reachable)
            if subset:
                capacity=min(quantity,sum(opposite[p] for p in reachable))
                closed[subset]=min(capacity,closed.get(subset,capacity))
        bounds=list(closed.items())
    used=dict.fromkeys(owners,0);fills={}
    buy=side=='buy'
    for priority,price_rank in sorted({(o.priority,-o.limit if buy else o.limit) for o in orders}):
        group=[o for o in orders if (o.priority,-o.limit if buy else o.limit)==(priority,price_rank)]
        weights=_totals(group,quantum=quantum)
        active=set(weights);exact={p:Q(0) for p in weights}
        while active:
            limits=[]
            for subset,capacity in bounds:
                denominator=sum(weights[p] for p in active & subset)
                if denominator:
                    occupied=sum(used[p]+(exact.get(p,Q(0)) if p not in active else 0) for p in subset)
                    limits.append((Q(capacity-occupied,denominator),subset))
            level=min(Q(1),min(value for value,_ in limits))
            if level<0:
                raise ValueError('negative feasible market entitlement')
            for p in active:
                exact[p]=level*weights[p]
            tight=set().union(*(subset for value,subset in limits if value==level)) if level<1 else set(active)
            active-=tight
        allocation={p:floor(q) for p,q in exact.items()}
        target=sum(exact.values(),Q(0))
        if target.denominator != 1:
            raise ValueError('nonintegral feasible market band total')
        remaining=int(target)-sum(allocation.values())
        for p in sorted(weights,key=lambda p:(digest(seed,'restricted_tail',tick,market,side,priority,price_rank,p),p)):
            if not remaining:break
            if allocation[p]>=weights[p] or exact[p]==allocation[p]:continue
            if all(sum(used[q]+allocation.get(q,0) for q in subset)<capacity for subset,capacity in bounds if p in subset):
                allocation[p]+=1;remaining-=1
        if remaining:
            raise ValueError('feasible market rounding could not complete its band')
        for p,amount in allocation.items():
            used[p]+=amount
            parts=proportional(amount,{o.id:o.quantity//quantum for o in group if o.owner==p},
                key=(seed,'market_fill',tick,market,side,priority,price_rank,p))
            fills.update({oid:q*quantum for oid,q in parts.items() if q})
    if sum(fills.values()) != quantity*quantum:
        raise ValueError('compatible auction did not fill its maximum volume')
    return fills


def clear_compatible(seed,tick,market,orders,previous_price,allowed,*,quantum=1,quantity_limit=None):
    if quantity_limit is not None and (type(quantity_limit) is not int or quantity_limit<0 or quantity_limit%quantum):
        raise ValueError('invalid compatible auction volume limit')
    ordinary=clear_auction(seed,tick,market,orders,previous_price,quantum=quantum)
    buys=[o for o in orders if o.side=='buy'];sells=[o for o in orders if o.side=='sell']
    matches=_matches(buys,sells,ordinary.buy_fills,ordinary.sell_fills,allowed,quantum)
    if matches is not None and (quantity_limit is None or ordinary.quantity<=quantity_limit):
        return MatchedAuction(ordinary,matches)
    candidates={previous_price}|{o.limit for o in orders}
    ranked=[]
    for price in sorted(candidates):
        buy=_totals([o for o in buys if o.limit>=price],quantum=quantum)
        sell=_totals([o for o in sells if o.limit<=price],quantum=quantum)
        volume,_=owner_flow(buy,sell,allowed)
        if quantity_limit is not None:volume=min(volume,quantity_limit//quantum)
        ranked.append((-volume,abs(sum(buy.values())-sum(sell.values())),abs(price-previous_price),price))
    neg,_,_,price=min(ranked)
    quantity=-neg
    if not quantity:
        return MatchedAuction(Auction(previous_price,0,{},{}),())
    buys=[o for o in buys if o.limit>=price];sells=[o for o in sells if o.limit<=price]
    buy_fills=_feasible_fills(buys,_totals(sells,quantum=quantum),allowed,quantity,
                            seed=seed,tick=tick,market=market,side='buy',quantum=quantum)
    sell_fills=_feasible_fills(sells,_totals(buys,buy_fills,quantum),lambda s,b:allowed(b,s),quantity,
                             seed=seed,tick=tick,market=market,side='sell',quantum=quantum)
    matches=_matches(buys,sells,buy_fills,sell_fills,allowed,quantum)
    if matches is None:
        raise ValueError('feasible market fills have no legal matching')
    return MatchedAuction(Auction(price,quantity*quantum,buy_fills,sell_fills),matches)
