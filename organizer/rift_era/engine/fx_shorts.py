"""02/06 finance/fx_short and finance/fx_cover (fx_short_v1).

fx_short {currency q, amount_mU}: needs F03. Borrows `amount` of q from the
world pool's real q stock and sells it for RU in this tick's FX batch (two
real legs: pool -> borrower lock, then the ordinary FX execution). The
borrower locks 50% RU collateral (amount x e_q / 2, from RU reserves). All
outstanding q shorts together are capped at 25% of the pool's q balance at
the start of the cycle. ShortDebt[p, q] accrues interest each cycle at
max(0, i_q + 2)/10000 of principal (i_q = the issuer's policy rate in %),
capitalised in q; its RU book grows at the rate of the cycle it accrues.

fx_cover {currency q, amount_mU}: buys up to `amount` of q with RU reserves in
this tick's FX batch, then (after the batch) repays what was actually bought
to the pool, cancels that share of ShortDebt and its RU book, and releases the
same share of collateral. Unspent RU and any q bought beyond the debt return
to the borrower's reserves. Nothing is repaid by cancelling a liability alone.
"""
from dataclasses import dataclass

from .command_schema import fields
from .errors import RuleViolation
from .ledger import CashAccount, CashTransfer
from .numeric import Q, ceil, floor
from .payments import FXRequest
from .protocol import PreparedCommand


@dataclass
class ShortDebt:
    owner: str
    currency: str
    principal_mU: int
    book_ru_mU: Q
    collateral_mU: int
    interest_carry: Q = Q(0)


class FXShorts:
    def __init__(self, state):
        self.s = state
        self.debts = {}          # 'owner/q' -> ShortDebt
        self.pool_start = {}     # q -> pool balance at cycle start
        self.pending_covers = []
        self.last_cycle = -1

    @staticmethod
    def key(owner, currency):
        return f'{owner}/{currency}'

    @staticmethod
    def pool(currency):
        return CashAccount('world', 'pool', currency)

    @staticmethod
    def collateral(owner, currency):
        return CashAccount(owner, f'fx_short_collateral:{currency}', 'RU')

    def outstanding(self, currency):
        return sum(d.principal_mU for d in self.debts.values() if d.currency == currency)

    def begin_tick(self, ctx):
        if ctx.tick % ctx.clock.cycle_ticks or ctx.cycle <= self.last_cycle:
            return
        self.last_cycle = ctx.cycle
        state = self.s
        cash = state.initial.cash
        self.pool_start = {q: cash.balance(self.pool(q)).native_mU for q in sorted(state.rates) if q != 'RU'}
        for key in sorted(self.debts):
            debt = self.debts[key]
            rate = max(0, Q(state.interest_bp.get(debt.currency, 0), 100) + 2)
            exact = debt.interest_carry + debt.principal_mU * rate / 10000
            whole = floor(exact)
            debt.interest_carry = exact - whole
            if whole:
                debt.principal_mU += whole
                debt.book_ru_mU += whole * state.rates[debt.currency]

    def after_commands(self, ctx):
        state = self.s
        cash = state.initial.cash
        for owner, currency in self.pending_covers:
            key = self.key(owner, currency)
            debt = self.debts.get(key)
            lock = CashAccount(owner, f'fx_cover:{currency}', currency)
            bought = cash.balance(lock).native_mU
            reserves_q = CashAccount(owner, 'reserves', currency)
            if debt is None or not bought:
                if bought:
                    cash.transfer(lock, reserves_q, bought)
                continue
            repay = min(bought, debt.principal_mU)
            share = Q(repay, debt.principal_mU)
            release = floor(debt.collateral_mU * share) if repay < debt.principal_mU else debt.collateral_mU
            transfers = [CashTransfer(lock, self.pool(currency), repay)]
            if bought > repay:
                transfers.append(CashTransfer(lock, reserves_q, bought - repay))
            if release:
                transfers.append(CashTransfer(self.collateral(owner, currency), CashAccount(owner, 'reserves', 'RU'), release))
            cash.transfer_many(transfers)
            debt.book_ru_mU -= debt.book_ru_mU * share
            debt.principal_mU -= repay
            debt.collateral_mU -= release
            if not debt.principal_mU:
                del self.debts[key]
        self.pending_covers = []

    def debt_book(self, owner):
        return sum((d.book_ru_mU for d in self.debts.values() if d.owner == owner), Q(0))

    def visible(self, owner):
        return [d for d in self.debts.values() if d.owner == owner]

    def snapshot(self):
        return {'debts': self.debts, 'pool_start': self.pool_start, 'pending_covers': self.pending_covers,
                'last_cycle': self.last_cycle}

    def empty(self):
        return not (self.debts or self.pending_covers)

    def audit(self):
        cash = self.s.initial.cash
        for debt in self.debts.values():
            if debt.principal_mU <= 0 or debt.collateral_mU < 0 or debt.book_ru_mU < 0:
                raise RuleViolation('ERR_SCHEMA', 'invalid fx short debt')
            if cash.balance(self.collateral(debt.owner, debt.currency)).native_mU != debt.collateral_mU:
                from .errors import InvariantViolation
                raise InvariantViolation('fx short collateral differs from its custody account')


class FXShortCommands:
    OPERATIONS = frozenset({('finance', 'fx_short'), ('finance', 'fx_cover')})

    def __init__(self, state):
        self.state = state

    def prepare(self, owner, order, ctx):
        state = self.state
        book = state.fx_shorts
        fields(order, {'currency': str, 'amount_mU': int})
        currency, amount = order['currency'], order['amount_mU']
        if currency == 'RU' or currency not in state.rates or amount <= 0:
            raise RuleViolation('ERR_SCHEMA')
        if 'F03' not in state.research[owner].known:
            raise RuleViolation('ERR_TECH_LOCKED')
        cash = state.initial.cash
        reserves = CashAccount(owner, 'reserves', 'RU')
        oid = f'{owner}/{int(order["id"])}'
        rate = state.rates[currency]
        if order['op'] == 'fx_short':
            limit = book.pool_start.get(currency, cash.balance(book.pool(currency)).native_mU) // 4
            if book.outstanding(currency) + amount > limit or cash.balance(book.pool(currency)).native_mU < amount:
                raise RuleViolation('ERR_CAPACITY_EXCEEDED', 'world-pool short limit')
            collateral = ceil(amount * rate / 2)
            lock = CashAccount(owner, f'fx_short:{currency}', currency)
            plan = cash.prepare_transfers((CashTransfer(reserves, book.collateral(owner, currency), collateral),
                                           CashTransfer(book.pool(currency), lock, amount)))
            request = FXRequest(oid, lock, reserves, amount, rate)

            def commit():
                plan.commit()
                state.fx_commands.append((request, CashAccount(owner, 'reserves', currency)))
                key = book.key(owner, currency)
                debt = book.debts.get(key)
                if debt is None:
                    book.debts[key] = ShortDebt(owner, currency, amount, amount * rate, collateral)
                else:
                    debt.principal_mU += amount
                    debt.book_ru_mU += amount * rate
                    debt.collateral_mU += collateral

            return PreparedCommand(commit)
        debt = book.debts.get(book.key(owner, currency))
        if debt is None or amount > debt.principal_mU:
            raise RuleViolation('ERR_NO_VISIBLE_TARGET', 'no such short position or amount above its debt')
        offered = ceil(amount * rate * (1 + state.fx.fee)) + 1
        lock = CashAccount(owner, f'fx_cover_ru:{currency}', 'RU')
        plan = cash.prepare_transfers((CashTransfer(reserves, lock, offered),))
        request = FXRequest(oid, lock, CashAccount(owner, f'fx_cover:{currency}', currency), offered, 1 / rate,
                            principal_limit_mU=ceil(amount * rate))

        def commit():
            plan.commit()
            state.fx_commands.append((request, reserves))
            if (owner, currency) not in book.pending_covers:
                book.pending_covers.append((owner, currency))

        return PreparedCommand(commit)
