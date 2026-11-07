"""06/11 logistics/forward_contract (forward_v1).

Signing: the proposer sends `forward_contract {counterparty, good, qty_mU,
strike_price_ru_mU, delivery_cycle, delivery_hub, side?}`; `side` ("buy" by
default, or "sell") is the proposer's side (06 lists no side field; author to
confirm). The counterparty signs by sending the mirror order: same terms, the
proposer as counterparty, the opposite side, within the proposal's life (until
the delivery cycle). Both need F03; delivery_cycle is c+10, c+20 or c+50 of the
proposing cycle; each side then locks 20% of notional (strike x qty, RU) from
its RU reserves into `forward_margin:<id>`.

Each cycle start before delivery the required margin is 20% x qty x spot,
spot = the delivery Hub's spot reference price in RU (previous clearing; 10 x
w_cat when there has never been one). A short side is topped up from RU
reserves; if it still holds under 10% the contract terminates and that
side's actual margin pays the other side (no second confiscation), credit -.15.
No profit or loss is paid before delivery.

Delivery (start of delivery_cycle): seller delivers min(qty, its available
stock at the Hub) and the buyer pays strike for what it can fund (its margin
first, then RU reserves); goods and cash move as one DVP. Each side's
shortfall is fined 1.2 x spot x shortfall to the other side, from its margin
then its reserves; any rest becomes a payable to the other side. Remaining
margins return to reserves.
"""
from dataclasses import dataclass

from .command_schema import fields
from .errors import RuleViolation
from .ledger import Account, CashAccount, CashTransfer, DeliveryPayment, Transfer
from .numeric import Q, ceil, floor, units
from .protocol import PreparedCommand


@dataclass
class Forward:
    id: str
    buyer: str
    seller: str
    good: str
    qty_mU: int
    strike_ru_mU: int
    delivery_cycle: int
    hub: str
    status: str = 'PROPOSED'      # PROPOSED -> ACTIVE -> DELIVERED | TERMINATED
    proposer: str = ''
    delivered_mU: int = 0
    defaulted_by: str | None = None


class Forwards:
    def __init__(self, state):
        self.s = state
        self.rows = {}
        self.last_cycle = -1
        self.recent = {}

    @staticmethod
    def margin(fid, owner):
        return CashAccount(owner, f'forward_margin:{fid}', 'RU')

    @staticmethod
    def reserves(owner):
        return CashAccount(owner, 'reserves', 'RU')

    def spot_ru(self, row):
        state = self.s
        venue_id = f'spot:{row.hub}:{row.good}'
        venue = state.spot.venues.get(venue_id)
        if venue is None:
            return units(10 * state.catalog.weights[row.good])
        price = state.spot.prices[venue_id]
        return price if venue.currency == 'RU' else price * state.rates[venue.currency]

    def balance(self, account):
        return self.s.initial.cash.balance(account).native_mU

    def _log(self, row, status):
        for party in (row.buyer, row.seller):
            self.recent.setdefault(party, []).append({'forward_id': row.id, 'status': status})
            self.recent[party] = self.recent[party][-64:]

    def _close(self, row, status):
        cash = self.s.initial.cash
        moves = [CashTransfer(self.margin(row.id, p), self.reserves(p), self.balance(self.margin(row.id, p)))
                 for p in (row.buyer, row.seller) if self.balance(self.margin(row.id, p))]
        cash.transfer_many(moves)
        row.status = status
        self._log(row, status)

    def begin_tick(self, ctx):
        if ctx.tick % ctx.clock.cycle_ticks or ctx.cycle <= self.last_cycle:
            return
        self.last_cycle = ctx.cycle
        for fid in sorted(self.rows):
            row = self.rows[fid]
            if row.status == 'PROPOSED' and ctx.cycle >= row.delivery_cycle:
                row.status = 'EXPIRED'
            if row.status != 'ACTIVE':
                continue
            if ctx.cycle >= row.delivery_cycle:
                self._deliver(row)
            else:
                self._mark(row)
        self.rows = {k: v for k, v in self.rows.items() if v.status in ('PROPOSED', 'ACTIVE')}

    def _mark(self, row):
        cash = self.s.initial.cash
        spot = self.spot_ru(row)
        required = ceil(Q(row.qty_mU, 1000) * spot / 5)
        floor10 = ceil(Q(row.qty_mU, 1000) * spot / 10)
        for party in (row.buyer, row.seller):
            account = self.margin(row.id, party)
            short = required - self.balance(account)
            if short > 0:
                top = min(short, self.balance(self.reserves(party)))
                if top:
                    cash.transfer(self.reserves(party), account, top)
            if self.balance(account) < floor10:
                other = row.seller if party == row.buyer else row.buyer
                forfeited = self.balance(account)
                if forfeited:
                    cash.transfer(account, self.reserves(other), forfeited)
                state = self.s
                state.credit[party] = max(Q(1, 10), state.credit[party] - Q(15, 100))
                row.defaulted_by = party
                self._close(row, 'TERMINATED')
                return

    def _pay(self, payer, payee, amount, row, transfers):
        """Pay `amount` RU from payer's margin, then reserves; return the unpaid rest."""
        left = amount
        for source in (self.margin(row.id, payer), self.reserves(payer)):
            available = self.balance(source) - sum(t.amount for t in transfers if t.source == source)
            take = min(left, max(0, available))
            if take:
                transfers.append(CashTransfer(source, self.reserves(payee), take))
                left -= take
        return left

    def _deliver(self, row):
        state = self.s
        inv, cash = state.initial.inventory, state.initial.cash
        spot = self.spot_ru(row)
        stock = inv.balance(Account(row.seller, row.hub, 'available', row.good))
        offered = min(row.qty_mU, stock)
        funds = self.balance(self.margin(row.id, row.buyer)) + self.balance(self.reserves(row.buyer))
        affordable = floor(Q(funds) * 1000 / row.strike_ru_mU) if row.strike_ru_mU else offered
        delivered = min(offered, affordable)
        money = []
        price = ceil(Q(delivered * row.strike_ru_mU, 1000))
        self._pay(row.buyer, row.seller, price, row, money)
        goods = (Transfer(Account(row.seller, row.hub, 'available', row.good),
                          Account(row.buyer, row.hub, 'available', row.good), delivered),) if delivered else ()
        DeliveryPayment.prepare(inv, cash, goods, tuple(money)).commit()
        row.delivered_mU = delivered
        fines = []
        seller_short = row.qty_mU - offered
        buyer_short = offered - delivered
        for debtor, creditor, short in ((row.seller, row.buyer, seller_short), (row.buyer, row.seller, buyer_short)):
            if short <= 0:
                continue
            fine = ceil(Q(short, 1000) * spot * Q(6, 5))
            batch = []
            rest = self._pay(debtor, creditor, fine, row, batch)
            cash.transfer_many(batch)
            if rest:
                state.payables.add(debtor, self.reserves(creditor), 'RU', rest, reason=f'forward:{row.id}')
            fines.append((debtor, fine))
        self._close(row, 'DELIVERED')

    def debt_view(self, owner):
        return [r for r in self.rows.values() if owner in (r.buyer, r.seller)]

    def snapshot(self):
        return {'rows': self.rows, 'last_cycle': self.last_cycle, 'recent': self.recent}

    def empty(self):
        return not (self.rows or self.recent)

    def visible(self, owner):
        return {'contracts': self.debt_view(owner), 'recent': list(self.recent.get(owner, ()))}


class ForwardCommands:
    OPERATIONS = frozenset({('logistics', 'forward_contract')})

    def __init__(self, state):
        self.state = state

    def prepare(self, owner, order, ctx):
        state = self.state
        book = state.forwards
        fields(order, {'counterparty': str, 'good': str, 'qty_mU': int, 'strike_price_ru_mU': int,
                       'delivery_cycle': int, 'delivery_hub': str}, {'side': str})
        side = order.get('side', 'buy')
        other, good, qty, strike = order['counterparty'], order['good'], order['qty_mU'], order['strike_price_ru_mU']
        if (side not in ('buy', 'sell') or other == owner or other not in state.initial.known or qty <= 0 or strike <= 0
                or good not in state.catalog.goods or state.catalog.goods[good]['kind'] != 'inventory'
                or order['delivery_hub'] not in state.initial.map.hubs):
            raise RuleViolation('ERR_SCHEMA')
        if 'F03' not in state.research[owner].known:
            raise RuleViolation('ERR_TECH_LOCKED')
        buyer, seller = (owner, other) if side == 'buy' else (other, owner)
        terms = (buyer, seller, good, qty, strike, order['delivery_cycle'], order['delivery_hub'])
        notional_margin = ceil(Q(qty, 1000) * strike / 5)
        match = next((r for r in book.rows.values() if r.status == 'PROPOSED' and r.proposer == other
                      and (r.buyer, r.seller, r.good, r.qty_mU, r.strike_ru_mU, r.delivery_cycle, r.hub) == terms), None)
        if match is None:
            if order['delivery_cycle'] - ctx.cycle not in (10, 20, 50):
                raise RuleViolation('ERR_SCHEMA', 'delivery cycle must be c+10, c+20 or c+50')
            fid = f'forward:{owner}:{int(order["id"])}'
            row = Forward(fid, *terms, proposer=owner)
            return PreparedCommand(lambda: book.rows.__setitem__(fid, row), persistent=True)
        if 'F03' not in state.research[other].known:
            raise RuleViolation('ERR_TECH_LOCKED', 'both parties need F03')
        if ctx.cycle >= match.delivery_cycle:
            raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        plan = state.initial.cash.prepare_transfers(tuple(
            CashTransfer(book.reserves(p), book.margin(match.id, p), notional_margin) for p in (buyer, seller)))

        def sign():
            plan.commit()
            match.status = 'ACTIVE'
            book._log(match, 'SIGNED')

        return PreparedCommand(sign)


def cancel(state, owner, target):
    row = state.forwards.rows.get(f'forward:{owner}:{target}')
    if row is None or row.status != 'PROPOSED':
        return None

    def withdraw():
        row.status = 'WITHDRAWN'
        del state.forwards.rows[row.id]

    return PreparedCommand(withdraw)
