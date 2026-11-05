"""Pure origin-constrained clearing over fungible, same-owner/price custody.

Only regulated clearings use this path. Public order IDs remain the settlement
IDs; synthetic origin groups are private to the pure matcher. Fractions below
one physical quantum remain in real sell custody, never in a new ledger.
"""
from collections import defaultdict

from .constrained_market import clear_compatible
from .determinism import fingerprint
from .errors import InvariantViolation
from .market import Auction, Order
from .materials import split_origins
from .numeric import Q, floor, proportional


def needs_origin_clearing(inventory, resting, eligible, allowed):
    buys={o.owner for o in eligible if o.side=='buy'}
    sells=[resting[o.id] for o in eligible if o.side=='sell']
    if not buys or not sells:return False
    if inventory.materials is None:
        raise InvariantViolation('origin restrictions require tracked physical inventory')
    return any(not allowed(buyer,sell.owner,origin.nation,sell.stock_account.asset)
               for sell in sells for origin in inventory.materials.composition(sell.stock_lock)
               for buyer in buys)


def _pair(sources,targets):
    """Sparse integer pairing with specified margins, without ownership policy."""
    targets=dict(targets)
    for source,amount in sorted(sources.items()):
        for target in sorted(targets):
            take=min(amount,targets[target])
            if take:yield source,target,take
            amount-=take;targets[target]-=take
            if not amount:break
        if amount:raise InvariantViolation('unbalanced origin-market margins')
    if any(targets.values()):raise InvariantViolation('unfilled origin-market margins')


def clear_origins(inventory,resting,eligible,*,seed,tick,venue,previous_price,
                  allowed,origin_allowed,charged_offer=None):
    """Return real fills, real matches and an uncommitted material overlay.

    Repacking identical goods within one seller/Hub/price is necessary: two
    one-mU locks each holding half an allowed origin can deliver one whole mU.
    Only their composition is pooled; integer custody, limits and cash remain
    attached to the original orders. A bilateral shot charges only its named
    offer, with the same seller's same-price locks providing fungible origin.
    """
    book=inventory.materials
    if book is None:raise InvariantViolation('regulated auction lacks material book')
    groups=defaultdict(list)
    for order in eligible:
        if order.side=='sell':groups[order.owner,order.limit].append(resting[order.id])
    pools={};virtual={};quotes=[o for o in eligible if o.side=='buy']
    for group,orders in sorted(groups.items()):
        pool=defaultdict(lambda:Q(0))
        for order in orders:
            for origin,q in book.composition(order.stock_lock).items():pool[origin]+=q
        pools[group]=dict(pool)
        nations=defaultdict(lambda:Q(0))
        for origin,q in pool.items():nations[origin.nation]+=q
        for nation,quantity in sorted(nations.items()):
            delivered=floor(quantity/venue.quantum_mU)*venue.quantum_mU
            if delivered:
                key='origin:'+fingerprint((group,nation))
                virtual[key]=(group,nation)
                quotes.append(Order(key,key,'sell',delivered,group[1]))
    def compatible(buyer,source):
        (seller,_),nation=virtual[source]
        return (allowed is None or allowed(buyer,seller)) and origin_allowed(buyer,seller,nation,venue.good)
    result=clear_compatible(seed,tick,venue.id,tuple(quotes),previous_price,compatible,
        quantum=venue.quantum_mU,quantity_limit=charged_offer.remaining_mU if charged_offer else None)
    auction=result.auction
    if not auction.quantity:return auction,(),None

    group_buys=defaultdict(lambda:defaultdict(int))
    origin_deliveries=defaultdict(lambda:defaultdict(int))
    for bid,sid,quantity in result.matches:
        group,nation=virtual[sid]
        group_buys[group][bid]+=quantity
        origin_deliveries[group][bid,nation]+=quantity
    draft=book.draft();sell_fills={};matches=[]
    for group,buy_fills in sorted(group_buys.items()):
        orders=sorted(groups[group],key=lambda o:o.id)
        quantity=sum(buy_fills.values())
        if charged_offer is not None:
            fills={charged_offer.id:quantity}
        else:
            fills={oid:q*venue.quantum_mU for oid,q in proportional(quantity//venue.quantum_mU,
                {o.id:o.remaining_mU//venue.quantum_mU for o in orders},
                key=(seed,'origin_sell_fill',tick,venue.id,*group)).items() if q}
        sell_fills.update(fills)
        matches.extend(_pair(buy_fills,fills))
        # Take all same-owner/price locks before adding any buyer or remainder.
        pool=defaultdict(lambda:Q(0))
        for order in orders:
            for origin,q in draft.take(order.stock_lock,Q(order.remaining_mU)).items():pool[origin]+=q
        for (bid,nation),amount in sorted(origin_deliveries[group].items()):
            pieces=split_origins(Q(amount),{o:q for o,q in pool.items() if o.nation==nation},
                capped=True,key=(seed,'controlled_delivery',tick,venue.id,*group,bid,nation))
            destination=resting[bid].stock_account
            if any(o.location!=destination.location for o in pieces):
                raise InvariantViolation('regulated trade cannot teleport origin stock')
            source=resting[charged_offer.id].stock_lock if charged_offer else orders[0].stock_lock
            draft.deliver(source,destination,pieces,retitle=True)
            for origin,q in pieces.items():pool[origin]-=q
        # Restore every untouched quantum and sub-quantum remainder to custody.
        for order in orders:
            remaining=order.remaining_mU-fills.get(order.id,0)
            pieces=split_origins(Q(remaining),dict(pool),capped=True,
                key=(seed,'controlled_remainder',tick,venue.id,*group,order.id))
            draft.add(order.stock_lock,pieces)
            for origin,q in pieces.items():pool[origin]-=q
        if any(pool.values()):raise InvariantViolation('regulated clearance lost an origin remainder')
    return Auction(auction.price,auction.quantity,dict(auction.buy_fills),sell_fills),tuple(matches),draft.finish()
