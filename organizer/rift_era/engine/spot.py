"""Custodied spot trading, cash-backed bids and same-hub delivery versus payment.

Permission, technology and embargo decisions belong to the command/domain layer.
This service owns custody and settlement. It never creates a counterparty, moves
goods between hubs, converts currencies, or recognizes terminal-use score.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from fractions import Fraction

from .errors import InvariantViolation, RuleViolation
from .determinism import fingerprint
from .ledger import Account, CashAccount, CashLedger, CashTransfer, DeliveryPayment, Inventory, Transfer
from .market import Auction, Order, clear_auction
from .numeric import Q, ceil, floor, max_feasible, proportional, split_payment
from .protocol import PreparedCommand


@dataclass(frozen=True)
class Venue:
    id: str
    hub: str
    good: str
    currency: str
    initial_price_mU: int
    quantum_mU: int = 1
    tariff: Fraction = Q(0)
    tax_account: CashAccount | None = None

    def __post_init__(self):
        if (self.initial_price_mU <= 0 or self.quantum_mU <= 0 or not 0 <= self.tariff <= Q(3, 10)
                or (self.tariff and self.tax_account is None)
                or (self.tax_account and self.tax_account.currency != self.currency)):
            raise ValueError('invalid spot venue')


@dataclass(frozen=True)
class RestingOrder:
    id: str  # globally unique owner/command sequence; user IDs stay nation-local
    command_id: str
    owner: str
    venue: str
    side: str
    remaining_mU: int
    limit_price_mU: int
    expires_tick: int
    cash_account: CashAccount  # original payer / seller proceeds
    stock_account: Account  # original seller / buyer next_available at SAME hub
    bilateral: bool = False
    offer_id: str | None = None  # one-shot acceptance, matched after all P1 commands
    cash_namespace: str = "order:"
    priority: int = 1

    @property
    def cash_lock(self) -> CashAccount:
        return CashAccount(self.owner, f'{self.cash_namespace}{self.id}', self.cash_account.currency)

    @property
    def stock_lock(self) -> Account:
        bucket = 'locked_escrow' if self.bilateral else 'locked_order'
        return Account(self.owner, self.stock_account.location, f'{bucket}:{self.id}', self.stock_account.asset)


@dataclass(frozen=True)
class Trade:
    buyer: str
    seller: str
    hub: str
    good: str
    quantity_mU: int
    buy_order: str
    sell_order: str


@dataclass(frozen=True)
class Clearing:
    venue: str
    tick: int
    price_mU: int
    quantity_mU: int
    paid_mU: int
    tariff_mU: int
    buy_fills: dict[str, int]
    sell_fills: dict[str, int]
    trades: tuple[Trade, ...]
    seller_receipts: dict[str, int]
    buyer_payments: dict[str, int]
    buyer_tariffs: dict[str, int]
    cash_flows: tuple[tuple[str, str, int], ...] = ()  # actual buyer -> seller principal
    income_accounts: tuple[tuple[str,str,str,int], ...] = ()


def _pair_totals(sources: dict[str, int], targets: dict[str, int]):
    """Deterministic sparse transport matrix, with exact row/column totals."""
    if sum(sources.values()) != sum(targets.values()):
        raise InvariantViolation('unbalanced settlement totals')
    remaining = dict(targets)
    keys = iter(sorted(k for k, q in targets.items() if q))
    target = next(keys, None)
    for source in sorted(sources):
        amount = sources[source]
        while amount:
            quantity = min(amount, remaining[target])
            yield source, target, quantity
            amount -= quantity
            remaining[target] -= quantity
            if not remaining[target]:
                target = next(keys, None)


class SpotExchange:
    def __init__(self, inventory: Inventory, cash: CashLedger, *, max_orders: int = 256, cash_namespace: str = "order:"):
        self.inventory, self.cash = inventory, cash
        self.max_orders = max_orders
        if not cash_namespace or not cash_namespace.endswith(":"):
            raise ValueError("invalid order cash namespace")
        self.cash_namespace = cash_namespace
        self.venues: dict[str, Venue] = {}
        self.orders: dict[str, RestingOrder] = {}
        self.prices: dict[str, int] = {}
        # Stable payer-account/venue keys, NEVER command IDs: cancelling/reposting cannot
        # reset a sub-mU obligation. No per-fill or per-window historical log.
        self.residuals: dict[tuple[str, str, str, str, str], Fraction] = {}
        self._revision = 0
        self.last_clear_tick: dict[str, int] = {}
        self.offer_aliases: dict[str, dict[str, str]] = {}
        self.offer_sequence: dict[str, int] = {}

    def add_venue(self, venue: Venue) -> None:
        if venue.id in self.venues:
            raise InvariantViolation('duplicate market venue')
        self.venues[venue.id] = venue
        self.prices[venue.id] = venue.initial_price_mU
        self._revision += 1

    def _guard(self, revision: int) -> None:
        if revision != self._revision:
            raise InvariantViolation('stale or repeated market mutation')

    def prepare_order(self, order: RestingOrder, *, tick: int, new_venue: Venue | None = None) -> PreparedCommand:
        if new_venue is not None and (new_venue.id != order.venue or new_venue.id in self.venues):
            raise InvariantViolation('invalid new venue')
        venue = new_venue or self.venues.get(order.venue)
        if venue is None:
            raise RuleViolation('ERR_SCHEMA', 'unknown market')
        if (order.cash_namespace != self.cash_namespace or order.id in self.orders or order.side not in ('buy', 'sell')
                or type(order.remaining_mU) is not int or order.remaining_mU < (0 if order.bilateral else 1)
                or order.remaining_mU % venue.quantum_mU
                or type(order.limit_price_mU) is not int or order.limit_price_mU <= 0
                or order.expires_tick <= tick or order.cash_account.owner != order.owner
                or order.cash_account.currency != venue.currency
                or order.stock_account.owner != order.owner
                or order.stock_account.location != venue.hub or order.stock_account.asset != venue.good
                or order.stock_account.bucket != ('available' if order.side == 'sell' else 'next_available')
                or (order.bilateral and order.side == 'buy' and not order.offer_id)
                or (order.offer_id is not None and (not order.bilateral or order.side != 'buy'))):
            raise RuleViolation('ERR_SCHEMA', 'invalid spot order')
        if sum(o.owner == order.owner for o in self.orders.values()) >= self.max_orders:
            raise RuleViolation('ERR_ENTITY_CAP_EXCEEDED')
        if self.cash.balance(order.cash_lock).native_mU or self.inventory.balance(order.stock_lock):
            raise InvariantViolation('reused live custody account')
        if order.side == 'buy':
            principal = Q(order.remaining_mU * order.limit_price_mU, 1000)
            budget = ceil(principal) + ceil(principal * venue.tariff)
            plan = self.cash.prepare_transfers((CashTransfer(order.cash_account, order.cash_lock, budget),))
        else:
            plan = self.inventory.prepare_transfers((Transfer(order.stock_account, order.stock_lock, order.remaining_mU),))
        revision = self._revision

        def commit():
            self._guard(revision)
            plan.commit()
            if new_venue is not None:
                self.venues[venue.id] = venue
                self.prices[venue.id] = venue.initial_price_mU
            self.orders[order.id] = order
            self._revision += 1

        return PreparedCommand(commit)

    def prepare_cancel(self, owner: str, order_id: str) -> PreparedCommand:
        order = self.orders.get(order_id)
        if order is None or order.owner != owner:
            raise RuleViolation('ERR_UNKNOWN_TARGET_ID')
        goods, money = self._refund(order)
        plan = DeliveryPayment.prepare(self.inventory, self.cash, goods, money)
        revision = self._revision

        def commit():
            self._guard(revision)
            plan.commit()
            del self.orders[order_id]
            self._revision += 1

        return PreparedCommand(commit)

    def _refund(self, order: RestingOrder) -> tuple[list[Transfer], list[CashTransfer]]:
        if order.side == 'buy':
            return [], [CashTransfer(order.cash_lock, order.cash_account, self.cash.balance(order.cash_lock).native_mU)]
        return [Transfer(order.stock_lock, order.stock_account, order.remaining_mU)], []

    def expire(self, tick: int) -> tuple[str, ...]:
        expired = sorted(o.id for o in self.orders.values() if o.expires_tick <= tick)
        goods, money = [], []
        for oid in expired:
            g, m = self._refund(self.orders[oid])
            goods.extend(g)
            money.extend(m)
        if expired:
            DeliveryPayment.prepare(self.inventory, self.cash, goods, money).commit()
            for oid in expired:
                del self.orders[oid]
            self._revision += 1
        return tuple(expired)

    def _quote(self, payer: CashAccount, venue: Venue, quantity: int, price: int,
               residuals: dict[tuple[str, str, str, str, str], Fraction]) -> tuple[int, int]:
        exact = Q(quantity * price, 1000)
        due = []
        for kind, value in (('principal', exact), ('tariff', exact * venue.tariff)):
            key = (payer.owner, payer.name, payer.currency, venue.id, kind)
            total = value + residuals.get(key, Q(0))
            amount = floor(total)
            residuals[key] = total - amount
            due.append(amount)
        return due[0], due[1]

    def clear(self, seed: int, tick: int, venue_id: str, *, allowed=None, price_band=None, origin_allowed=None) -> Clearing:
        venue = self.venues[venue_id]
        if price_band is not None and not 0 < price_band[0] <= self.prices[venue_id] <= price_band[1]:
            raise InvariantViolation('invalid auction price band')
        if tick <= self.last_clear_tick.get(venue_id, -1):
            raise InvariantViolation('market already cleared or backdated')
        if any(o.expires_tick <= tick for o in self.orders.values() if o.venue == venue_id):
            raise InvariantViolation('expire orders at P1 before clearing')
        eligible = []
        for order in self.orders.values():
            if order.venue != venue_id or order.bilateral:
                continue
            quantity = order.remaining_mU
            if order.side == 'buy':
                balance = self.cash.balance(order.cash_lock).native_mU
                # On partial fills the shared rounding carry may have advanced.
                # Never spend another order's custody to cover a one-mU shortfall.
                quantity = venue.quantum_mU * max_feasible(quantity // venue.quantum_mU,
                    lambda n: (ceil(Q(n * venue.quantum_mU * order.limit_price_mU, 1000)) +
                               ceil(Q(n * venue.quantum_mU * order.limit_price_mU, 1000) * venue.tariff)) <= balance)
            if quantity:
                limit = order.limit_price_mU
                if price_band is not None:
                    limit = min(limit,price_band[1]) if order.side=='buy' else max(limit,price_band[0])
                eligible.append(Order(order.id, order.owner, order.side, quantity, limit, order.priority))
        matches, material_plan = None, None
        buys = {o.owner for o in eligible if o.side == 'buy'}
        sells = {o.owner for o in eligible if o.side == 'sell'}
        from .origin_market import needs_origin_clearing, clear_origins
        if origin_allowed is not None and needs_origin_clearing(self.inventory, self.orders, eligible, origin_allowed):
            auction, matches, material_plan = clear_origins(self.inventory, self.orders, tuple(eligible),
                seed=seed, tick=tick, venue=venue, previous_price=self.prices[venue.id],
                allowed=allowed, origin_allowed=origin_allowed)
        elif allowed is not None and any(not allowed(b,s) for b in buys for s in sells):
            from .constrained_market import clear_compatible
            matched = clear_compatible(seed,tick,venue.id,tuple(eligible),self.prices[venue.id],allowed,
                                       quantum=venue.quantum_mU)
            auction, matches = matched.auction, matched.matches
        else:
            auction = clear_auction(seed, tick, venue.id, tuple(eligible), self.prices[venue.id], quantum=venue.quantum_mU)
        result, commit = self._prepare_settlement(seed, tick, venue, auction, matches=matches, material_plan=material_plan)
        commit()
        self.prices[venue.id] = auction.price
        self.last_clear_tick[venue.id] = tick
        return result

    def clear_offers(self, seed: int, tick: int, *, allowed=None, origin_allowed=None) -> tuple[Clearing, ...]:
        """Competing acceptances share real escrow, independent of seat order.

        Offers cancelled in this P1 no longer trade. Unfilled acceptance budgets
        are refunded in this same phase; they never become anonymous market bids.
        """
        targets = sorted({o.offer_id for o in self.orders.values() if o.offer_id is not None})
        results = []
        for offer_id in targets:
            bids = {o.id: o for o in self.orders.values() if o.offer_id == offer_id}
            offer = self.orders.get(offer_id)
            if offer is not None:
                venue = self.venues[offer.venue]
                if (not offer.bilateral or offer.side != 'sell' or any(
                        bid.venue != offer.venue or bid.limit_price_mU < offer.limit_price_mU
                        for bid in bids.values())):
                    raise InvariantViolation('invalid cross-venue or underpriced bilateral request')
                fills = proportional(offer.remaining_mU // venue.quantum_mU,
                    {oid: o.remaining_mU // venue.quantum_mU for oid, o in bids.items()
                     if allowed is None or allowed(o.owner,offer.owner,venue.good)},
                    key=(seed, 'accept_export_offer', tick, offer_id))
                fills = {oid: q * venue.quantum_mU for oid, q in fills.items() if q}
                quantity = sum(fills.values())
                eligible_bids = [Order(o.id,o.owner,'buy',o.remaining_mU,offer.limit_price_mU)
                    for o in bids.values() if allowed is None or allowed(o.owner,offer.owner,venue.good)]
                from .origin_market import needs_origin_clearing, clear_origins
                pools = [o for o in self.orders.values() if o.side=='sell' and o.bilateral
                         and (o.owner,o.venue,o.limit_price_mU)==(offer.owner,offer.venue,offer.limit_price_mU)]
                eligible = tuple(eligible_bids+[Order(o.id,o.owner,'sell',o.remaining_mU,o.limit_price_mU)
                                              for o in pools if o.remaining_mU])
                if origin_allowed is not None and needs_origin_clearing(self.inventory,self.orders,eligible,origin_allowed):
                    auction,matches,material_plan=clear_origins(self.inventory,self.orders,eligible,
                        seed=seed,tick=tick,venue=venue,previous_price=offer.limit_price_mU,
                        allowed=lambda b,s:allowed is None or allowed(b,s,venue.good),
                        origin_allowed=origin_allowed,charged_offer=offer)
                    if auction.quantity:
                        result,commit=self._prepare_settlement(seed,tick,venue,auction,matches=matches,
                                                              material_plan=material_plan)
                        commit();results.append(result)
                elif quantity:
                    result, commit = self._prepare_settlement(seed, tick, venue,
                        Auction(offer.limit_price_mU, quantity, fills, {offer_id: quantity}))
                    commit()
                    results.append(result)
            for oid, bid in sorted(bids.items()):
                if oid in self.orders:
                    self.prepare_cancel(bid.owner, oid).commit()
        return tuple(results)

    def _matched_payments(self, seed, tick, venue, matches, participating, due):
        """One actual payer invoice, allocated only to legal matched suppliers.

        Coalescing real accounts before integer tails prevents duplicate orders
        from increasing a supplier's share of rounding pennies.
        """
        demand = defaultdict(dict)
        edges = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
        for bid, amount in due.items():
            demand[participating[bid].cash_account][bid] = amount
        for bid, sid, quantity in matches:
            edges[participating[bid].cash_account][participating[sid].cash_account][sid] += quantity
        receipts, legs = defaultdict(int), []
        for payer, sources in sorted(demand.items()):
            suppliers = edges[payer]
            names = {fingerprint(account): account for account in suppliers}
            weights = {name: Q(sum(suppliers[account].values())) for name, account in names.items()}
            allocated = split_payment(sum(sources.values()), weights,
                key=(seed,'matched_proceeds',tick,venue.id,payer.owner,payer.name,payer.currency))
            representative = {name:min(suppliers[account]) for name,account in names.items()}
            for bid, name, amount in _pair_totals(sources, allocated):
                legs.append((bid,representative[name],amount))
            for name, amount in allocated.items():
                split = split_payment(amount, {sid:Q(q) for sid,q in suppliers[names[name]].items()},
                                      key=(seed,'matched_receipt',tick,venue.id,payer.owner,name))
                for sid,value in split.items():
                    receipts[sid] += value
        return dict(receipts), legs

    def _prepare_settlement(self, seed: int, tick: int, venue: Venue, auction: Auction,
                            immediate_buyer: RestingOrder | None = None, *, matches=None, material_plan=None):
        participating = dict(self.orders)
        if immediate_buyer:
            participating[immediate_buyer.id] = immediate_buyer
        residuals = dict(self.residuals)
        due, tariffs = {}, {}
        money, goods, trades = [], [], []
        for oid, quantity in sorted(auction.buy_fills.items()):
            order = participating[oid]
            due[oid], tariffs[oid] = self._quote(order.cash_account, venue, quantity, auction.price, residuals)
            payer = order.cash_account if order is immediate_buyer else order.cash_lock
            if tariffs[oid]:
                money.append(CashTransfer(payer, venue.tax_account, tariffs[oid]))
        paid = sum(due.values())
        if matches is None:
            receipts = split_payment(paid, {oid: Q(q) for oid, q in auction.sell_fills.items()},
                                      key=(seed, 'spot_proceeds', tick, venue.id))
            cash_pairs = _pair_totals(due, receipts)
        else:
            check_buys, check_sells = defaultdict(int), defaultdict(int)
            for bid,sid,quantity in matches:
                if type(quantity) is not int or quantity <= 0 or quantity % venue.quantum_mU:
                    raise InvariantViolation('invalid compatible trade quantum')
                check_buys[bid] += quantity
                check_sells[sid] += quantity
            if dict(check_buys) != dict(auction.buy_fills) or dict(check_sells) != dict(auction.sell_fills):
                raise InvariantViolation('compatible matching differs from quoted fills')
            receipts, cash_pairs = self._matched_payments(seed,tick,venue,matches,participating,due)
        for buyer, seller, quantity in (_pair_totals(auction.buy_fills, auction.sell_fills) if matches is None else matches):
            buy, sell = participating[buyer], participating[seller]
            goods.append(Transfer(sell.stock_lock, buy.stock_account, quantity,retitle=True))
            trades.append(Trade(buy.owner, sell.owner, venue.hub, venue.good, quantity, buyer, seller))
        if material_plan is None and self.inventory.materials is not None:
            material_plan=self.inventory.materials.transfers(tuple(goods))
        cash_flows = []
        income_accounts=defaultdict(int)
        if material_plan is not None and any(d.origin.economic_owner is not None for d in material_plan.deliveries):
            cash_pairs=self._title_payments(seed,tick,venue,participating,due,material_plan.deliveries)
        else:
            cash_pairs=((buyer,participating[seller].cash_account,amount) for buyer,seller,amount in cash_pairs)
        for buyer, recipient, amount in cash_pairs:
            order = participating[buyer]
            cash_flows.append((order.owner,recipient.owner,amount))
            income_accounts[recipient]+=amount
            money.append(CashTransfer(order.cash_account if order is immediate_buyer else order.cash_lock,
                                      recipient,amount))
        updated = {}
        for fills in (auction.buy_fills, auction.sell_fills):
            for oid, quantity in fills.items():
                order = participating[oid]
                remaining = order.remaining_mU - quantity
                if remaining < 0:
                    raise InvariantViolation('auction overfilled custody')
                if order is immediate_buyer:
                    continue
                updated[oid] = replace(order, remaining_mU=remaining) if remaining else None
                if not remaining and order.side == 'buy':
                    refund = self.cash.balance(order.cash_lock).native_mU - due[oid] - tariffs[oid]
                    if refund < 0:
                        raise InvariantViolation('auction exceeded reserved cash')
                    money.append(CashTransfer(order.cash_lock, order.cash_account, refund))
        transaction = DeliveryPayment.prepare(self.inventory, self.cash, goods, money, material_plan=material_plan)
        revision = self._revision

        def commit():
            self._guard(revision)
            transaction.commit()
            self._after_settlement(result, transaction.cash, participating)
            self.residuals = {k: v for k, v in residuals.items() if v}
            for oid, order in updated.items():
                if order:
                    self.orders[oid] = order
                else:
                    del self.orders[oid]
            self._revision += 1

        result = Clearing(venue.id, tick, auction.price, auction.quantity, paid, sum(tariffs.values()),
                        dict(auction.buy_fills), dict(auction.sell_fills), tuple(trades), receipts, due, tariffs,
                        tuple(cash_flows),tuple((a.owner,a.name,a.currency,q) for a,q in sorted(income_accounts.items()) if q))
        return result, commit

    def _after_settlement(self, result, cash_plan, participating):
        """Post-commit accounting hook for specialized exchanges.

        The base commodity exchange has no historical security basis to update.
        Subclasses may inspect the already committed cash-plan movements; this
        hook is intentionally after DVP commit so a failed or stale prepare
        cannot mutate an auxiliary book.
        """
        return None

    def _title_payments(self,seed,tick,venue,participating,due,deliveries):
        """Allocate each buyer invoice to the real pre-sale economic titles.

        Price is uniform; identical buyer custody may represent several source
        cash accounts. Their combined stock is fungible, while each actual
        payer keeps its own invoice and rounding key.
        """
        from .economic_ownership import income_account
        sellers={o.stock_lock:o for o in participating.values() if o.side=='sell'}
        weights=defaultdict(lambda:defaultdict(lambda:Q(0)))
        for delivery in deliveries:
            seller=sellers[delivery.source]
            recipient=income_account(delivery.origin,seller.owner,venue.currency,
                                     government_account=seller.cash_account.name)
            weights[delivery.destination.owner][recipient]+=delivery.quantity_mU
        invoices=defaultdict(dict)
        for bid,amount in due.items():invoices[participating[bid].cash_account][bid]=amount
        for payer,sources in sorted(invoices.items()):
            names={fingerprint(a):a for a in weights[payer.owner]}
            allocation=split_payment(sum(sources.values()),{name:weights[payer.owner][a] for name,a in names.items()},
                key=(seed,'title_proceeds',tick,venue.id,payer.owner,payer.name,payer.currency))
            for bid,name,amount in _pair_totals(sources,allocation):yield bid,names[name],amount

    def prepare_accept(self, seed: int, tick: int, buyer: RestingOrder, offer_id: str) -> tuple[PreparedCommand, Clearing]:
        offer = self.orders.get(offer_id)
        if offer is None or not offer.bilateral or offer.expires_tick <= tick:
            raise RuleViolation('ERR_UNKNOWN_TARGET_ID')
        venue = self.venues[offer.venue]
        if (buyer.owner == offer.owner or buyer.venue != offer.venue or buyer.side != 'buy'
                or buyer.id in self.orders or buyer.cash_account.owner != buyer.owner
                or buyer.cash_account.currency != venue.currency
                or buyer.stock_account != Account(buyer.owner, venue.hub, 'next_available', venue.good)
                or type(buyer.remaining_mU) is not int or buyer.remaining_mU <= 0
                or buyer.remaining_mU % venue.quantum_mU
                or buyer.limit_price_mU < offer.limit_price_mU):
            raise RuleViolation('ERR_SCHEMA', 'invalid bilateral acceptance')
        if buyer.remaining_mU > offer.remaining_mU:
            raise RuleViolation('ERR_INSUFFICIENT_STOCK')
        quantity = buyer.remaining_mU
        result, commit = self._prepare_settlement(seed, tick, venue,
            Auction(offer.limit_price_mU, quantity, {buyer.id: quantity}, {offer.id: quantity}), buyer)
        return PreparedCommand(commit), result

    def publish_offers(self, observers: tuple[str, ...]) -> None:
        """P8 public listings get observer-local IDs, never issuer command seqs.

        Retired aliases are pruned without resetting the permanent sequence.
        Queries below are read-only and cannot mint aliases or revive offers.
        """
        offers = sorted(o.id for o in self.orders.values() if o.bilateral and o.side == 'sell')
        for observer in observers:
            previous = self.offer_aliases.get(observer, {})
            aliases = {oid: previous[oid] for oid in offers if oid in previous}
            sequence = self.offer_sequence.get(observer, 0)
            for oid in offers:
                if oid not in aliases:
                    sequence += 1
                    aliases[oid] = f'offer-{sequence}'
            self.offer_aliases[observer] = aliases
            self.offer_sequence[observer] = sequence

    def resolve_offer(self, observer: str, alias: str) -> str | None:
        return next((oid for oid, public in self.offer_aliases.get(observer, {}).items() if public == alias), None)

    def visible(self, owner: str) -> dict:
        aliases = self.offer_aliases.get(owner, {})
        return {'prices': dict(self.prices),
                'orders': [o for _, o in sorted(self.orders.items()) if o.owner == owner],
                'export_offers': [{'id': aliases[o.id], 'seller': o.owner, 'hub_id': self.venues[o.venue].hub,
                    'good': self.venues[o.venue].good, 'qty_mU': o.remaining_mU, 'price_ru_mU': o.limit_price_mU}
                    for _, o in sorted(self.orders.items()) if o.bilateral and o.side == 'sell' and o.id in aliases]}

    def fill_reports(self, owner: str, clearings: list[Clearing]) -> list[dict]:
        """Own fills only; do not serialize another nation's private order IDs."""
        result = []
        for clearing in clearings:
            venue = self.venues[clearing.venue]
            for side, fills in (('buy', clearing.buy_fills), ('sell', clearing.sell_fills)):
                for oid, quantity in sorted(fills.items()):
                    # NPC/city-state custody IDs are engine-owned and may not
                    # use the nation-local ``owner/sequence`` form.  They are
                    # never exposed through a player's fill report.
                    if '/' not in oid:
                        continue
                    nation, command_id = oid.split('/', 1)
                    if nation != owner:
                        continue
                    amount = clearing.buyer_payments[oid] if side == 'buy' else clearing.seller_receipts[oid]
                    result.append({'id': command_id, 'tick': clearing.tick, 'side': side,
                        'status': 'EXECUTING' if oid in self.orders else 'COMPLETED',
                        'hub_id': venue.hub, 'good': venue.good, 'qty_mU': quantity,
                        'price_mU': clearing.price_mU, 'currency': venue.currency,
                        'cash_mU': amount, 'tariff_mU': clearing.buyer_tariffs.get(oid, 0)})
        return result

    def snapshot(self) -> dict:
        return {'cash_namespace': self.cash_namespace, 'venues': self.venues, 'orders': self.orders, 'prices': self.prices,
                'residuals': [[*k, v] for k, v in sorted(self.residuals.items())],
                'last_clear_tick': self.last_clear_tick,
                'offer_aliases': self.offer_aliases, 'offer_sequence': self.offer_sequence}

    def audit(self) -> None:
        counts = defaultdict(int)
        stock_locks, cash_locks = set(), set()
        for order in self.orders.values():
            if order.cash_namespace != self.cash_namespace:
                raise InvariantViolation('order belongs to a different cash custody namespace')
            counts[order.owner] += 1
            venue = self.venues[order.venue]
            if order.remaining_mU < (0 if order.bilateral else 1) or order.remaining_mU % venue.quantum_mU:
                raise InvariantViolation('invalid resting order quantity')
            if order.side == 'sell':
                stock_locks.add(order.stock_lock)
                if self.inventory.balance(order.stock_lock) != order.remaining_mU:
                    raise InvariantViolation('sell order differs from physical custody')
            else:
                cash_locks.add(order.cash_lock)
        if any(count > self.max_orders for count in counts.values()):
            raise InvariantViolation('active order cap exceeded')
        if any(not 0 <= r < 1 for r in self.residuals.values()):
            raise InvariantViolation('invalid settlement remainder')
        for account in self.inventory.balances():
            if account.bucket.startswith(('locked_order:', 'locked_escrow:')) and account not in stock_locks:
                raise InvariantViolation('orphan stock order lock')
        # Other systems use their own lock namespace; do not infer their owners.
        for account in self.cash.balances():
            if account.name.startswith(self.cash_namespace) and account not in cash_locks:
                raise InvariantViolation('orphan cash order lock')
