"""P1 command adapter and P5 clearing for physical spot markets.

This adapter implements only the listed operations. The development assembly
does not advertise unsupported military, construction or financial commands.
"""

from __future__ import annotations

from .errors import InfrastructureFault, RuleViolation
from .ledger import Account, CashAccount, CashTransfer
from .numeric import units
from .payments import FXRequest
from .protocol import PreparedCommand
from .spot import RestingOrder, Venue
from .command_schema import fields as _schema
from .economic_ownership import record_income


def _positive(*values: int) -> None:
    if any(value <= 0 for value in values):
        raise RuleViolation('ERR_SCHEMA', 'expected positive quantity or price')


class CommerceCommands:
    OPERATIONS = frozenset({('logistics', 'market_bid'), ('logistics', 'market_ask'),
                            ('trade', 'export_offer'), ('trade', 'buy_from'), ('cancel', 'cancel_by_id'),
                            ('finance', 'fx')})

    def __init__(self, state):
        self.state = state
        self.visible_offers = {}

    def _venue(self, hub: str, good: str, *, bilateral: bool) -> Venue:
        state = self.state
        site = state.initial.map.hubs.get(hub)
        row = state.catalog.goods.get(good)
        if site is None or row is None or row['kind'] != 'inventory':
            raise RuleViolation('ERR_SCHEMA', 'unknown hub or non-inventory good')
        currency = 'RU' if bilateral or site.owner not in state.initial.known else site.owner
        return Venue(f'{"export" if bilateral else "spot"}:{hub}:{good}', hub, good, currency,
                     units(10 * state.catalog.weights[good]), 1000 if row['indivisible'] else 1)

    def prepare(self, owner: str, order: dict, ctx) -> PreparedCommand:
        state, exchange = self.state, self.state.spot
        kind, operation = order.get('kind'), order.get('op')
        if not isinstance(kind, str) or not isinstance(operation, str) or (kind, operation) not in self.OPERATIONS:
            raise RuleViolation('ERR_SCHEMA', 'operation unavailable in economic development profile')
        cid = str(int(order['id']))
        oid = f'{owner}/{cid}'
        if kind == 'finance':
            _schema(order, {'side': str, 'amount_mU': int, 'currency': str})
            _positive(order['amount_mU'])
            currency, side = order['currency'], order['side']
            if currency not in state.rates or side not in ('buy_ru', 'sell_ru'):
                raise RuleViolation('ERR_SCHEMA')
            if currency != owner and 'F06' not in state.research[owner].known:
                raise RuleViolation('ERR_TECH_LOCKED')
            native, reserves = CashAccount(owner, 'treasury', currency), CashAccount(owner, 'reserves', 'RU')
            source, destination = (native, reserves) if side == 'buy_ru' else (reserves, native)
            rate = state.rates[currency] if side == 'buy_ru' else 1 / state.rates[currency]
            # Stable custody key preserves sub-mU principal/fee remainders across
            # all windows. Several same-side orders share this real cash pool.
            locked = CashAccount(owner, f'fx_command:{side}:{currency}', source.currency)
            plan = state.initial.cash.prepare_transfers((CashTransfer(source, locked, order['amount_mU']),))
            request = FXRequest(oid, locked, destination, order['amount_mU'], rate)

            def commit():
                plan.commit()
                state.fx_commands.append((request, source))

            return PreparedCommand(commit)
        if kind == 'cancel':
            _schema(order, {'target_id': str})
            target = order['target_id']
            if not target.isascii() or not target.isdecimal():
                raise RuleViolation('ERR_UNKNOWN_TARGET_ID')
            return exchange.prepare_cancel(owner, f'{owner}/{int(target)}')
        if operation in ('market_bid', 'market_ask', 'export_offer'):
            bilateral = operation == 'export_offer'
            buy = operation == 'market_bid'
            hub_field = 'region_market' if buy else 'hub_id'
            price_field = 'price_ru_mU' if bilateral else 'limit_price_mU'
            fields = {hub_field: str, 'good': str, 'qty_mU': int, price_field: int}
            if not bilateral:
                fields['expire_cycles'] = int
            _schema(order, fields)
            _positive(order[price_field])
            if order['qty_mU'] < (0 if bilateral else 1):
                raise RuleViolation('ERR_SCHEMA')
            if not bilateral and not 1 <= order['expire_cycles'] <= 20:
                raise RuleViolation('ERR_SCHEMA')
            venue = self._venue(order[hub_field], order['good'], bilateral=bilateral)
            # Existing foreign-owned goods may be sold at their actual location.
            # Hub sovereignty is NOT inventory ownership. Physical entry/ACL is
            # enforced by transport, rather than by teleporting purchased goods.
            account = CashAccount(owner, 'reserves' if venue.currency == 'RU' else 'treasury', venue.currency)
            stock = Account(owner, venue.hub, 'next_available' if buy else 'available', venue.good)
            expires = ctx.clock.total_ticks if bilateral else ctx.tick + order['expire_cycles'] * ctx.clock.cycle_ticks
            resting = RestingOrder(oid, cid, owner, venue.id, 'buy' if buy else 'sell', order['qty_mU'],
                                   order[price_field], expires, account, stock, bilateral)
            return exchange.prepare_order(resting, tick=ctx.tick,
                                           new_venue=venue if venue.id not in exchange.venues else None)
        _schema(order, {'offer_id': str, 'seller': str, 'good': str, 'qty_mU': int, 'max_price_ru_mU': int})
        _positive(order['qty_mU'], order['max_price_ru_mU'])
        target = order['offer_id']
        offer_id = exchange.resolve_offer(owner, target)
        offer = self.visible_offers.get(offer_id)
        if offer is None or not offer.bilateral or offer.owner != order['seller']:
            raise RuleViolation('ERR_UNKNOWN_TARGET_ID')
        venue = exchange.venues[offer.venue]
        if venue.good != order['good']:
            raise RuleViolation('ERR_SCHEMA')
        if offer.owner == owner or not state.relations.trade_allowed(owner,offer.owner,venue.good):
            raise RuleViolation('ERR_NO_PERMISSION')
        if order['max_price_ru_mU'] < offer.limit_price_mU:
            raise RuleViolation('ERR_SCHEMA')
        buyer = RestingOrder(oid, cid, owner, venue.id, 'buy', order['qty_mU'], order['max_price_ru_mU'],
            ctx.tick + 1, CashAccount(owner, 'reserves', 'RU'), Account(owner, venue.hub, 'next_available', venue.good),
            bilateral=True, offer_id=offer_id)
        return exchange.prepare_order(buyer, tick=ctx.tick)

    def commands(self, state, ctx, *, prepare=None,before_orders=None,pool_payments=None,on_pool_cleared=None) -> None:
        state.spot.expire(ctx.tick)
        if not ctx.clock.is_decision(ctx.tick):
            state.relations.settle(ctx)
            return
        state.relations.preview(ctx)
        state.market_events = []
        state.fx_fills = {}
        if state.fx_commands:
            raise InfrastructureFault('unsettled FX commands from previous P1')
        # Only previous-window offers were visible to the submitting strategies.
        # Freeze these before handling any seat so same-P1 posting/cancellation
        # cannot make another country's business validation depend on seat order.
        self.visible_offers = {oid: o for oid, o in state.spot.orders.items() if o.bilateral and o.side == 'sell'}
        if set(ctx.orders) - state.command_streams.keys():
            raise InfrastructureFault('unknown seat passed to command system')
        # Namespace order is explicit and deterministic. A later domain arbiter
        # batches contested world-pool/physical claims; these local commands do
        # not create or silently consume a shared NPC cash pool.
        if before_orders is not None:
            before_orders()
        for owner in sorted(state.command_streams):
            state.command_streams[owner].process(list(ctx.orders.get(owner, ())),
                lambda order, owner=owner: (prepare or self.prepare)(owner, order, ctx))
        state.relations.settle(ctx)
        requests = tuple(request for request, _ in state.fx_commands)
        claims=pool_payments() if pool_payments else ()
        if requests or claims:
            state.fx_fills = state.fx.execute(ctx.seed,ctx.tick,requests,pool_payments=claims)
        if requests:
            refunds = {request.source: source for request, source in state.fx_commands}
            state.initial.cash.transfer_many(CashTransfer(lock, source, state.initial.cash.balance(lock).native_mU)
                                              for lock, source in sorted(refunds.items()))
            state.fx_commands.clear()
        if on_pool_cleared:on_pool_cleared()
        clearings = state.spot.clear_offers(ctx.seed, ctx.tick, allowed=state.relations.trade_allowed,
                                           origin_allowed=state.export_controls.permits)
        state.market_events.extend(clearings)
        state.macro.trades(clearings)
        self.record_income(clearings)

    def clear(self, state, ctx) -> None:
        if not ctx.clock.is_window_end(ctx.tick):
            return
        active = sorted({o.venue for o in state.spot.orders.values()
                         if not o.bilateral and not o.venue.startswith('city_export:')})
        for venue_id in active:
            clearing = state.spot.clear(ctx.seed, ctx.tick, venue_id,
                allowed=lambda a,b:state.relations.trade_allowed(a,b,state.spot.venues[venue_id].good),
                origin_allowed=state.export_controls.permits)
            state.market_events.append(clearing)
            state.macro.trades((clearing,))
            self.record_income((clearing,))

    def record_income(self,clearings):
        for clearing in clearings:
            recipients=tuple(CashAccount(owner,name,currency) for owner,name,currency,_ in clearing.income_accounts)
            record_income(self.state,recipients,tuple(amount for *_,amount in clearing.income_accounts))
