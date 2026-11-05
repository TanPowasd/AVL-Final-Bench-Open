"""Cash-backed reservations, fractional obligations and finite-pool FX batches."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from fractions import Fraction

from .ledger import CashAccount, CashLedger, CashTransfer, CashExchange, CashTransferPlan, InventoryTransferPlan,CASH_BOOK_SCALE
from .numeric import Q, floor, max_feasible, proportional, split_payment
from .determinism import fingerprint
from .errors import InvariantViolation, RuleViolation
from .rational_carry import RationalCarry


@dataclass(frozen=True)
class Payment:
    due_mU: int
    paid_mU: int
    unpaid_mU: int


@dataclass(frozen=True)
class Obligation:
    source: CashAccount
    target: CashAccount
    amount_mU: Fraction
    kind: str


@dataclass(frozen=True)
class PaidResources:
    payments: Payments
    revision: int
    cash: CashTransferPlan
    residuals: tuple[tuple[tuple[CashAccount, CashAccount, str], Fraction], ...]
    amounts: tuple[int, ...]
    transfers: tuple[CashTransfer, ...] = ()

    def with_fixed_transfers(self,transfers):
        """Preflight fixed cash legs alongside this invoice and stock debit."""
        combined=self.transfers+tuple(transfers)
        return replace(self,cash=self.payments.ledger.prepare_transfers(combined),transfers=combined)

    def commit(self, stock: InventoryTransferPlan | None = None) -> None:
        """All validations precede every physical, financial and residual write."""
        if self.payments._revision != self.revision:
            raise InvariantViolation('stale fractional payment plan')
        self.cash.check_current()
        if stock is not None:
            stock.check_current()
        if stock is not None:
            stock.commit()
        self.cash.commit()
        for key, value in self.residuals:
            if value:
                self.payments.residuals[key] = value
            else:
                self.payments.residuals.pop(key, None)
        self.payments._revision += 1


class Payments:
    def __init__(self, ledger: CashLedger):
        self.ledger = ledger
        self.residuals: dict[tuple[CashAccount, CashAccount, str], Fraction] = {}
        self.arrears: dict[tuple[CashAccount, CashAccount, str], int] = {}
        self._revision = 0

    def prepare(self, obligations: tuple[Obligation, ...]) -> PaidResources:
        """Exact prepaid bills; callers can commit them with a material debit."""
        residuals, transfers, amounts = {}, [], []
        for row in obligations:
            if row.amount_mU < 0 or row.source.currency != row.target.currency or not row.kind:
                raise InvariantViolation('invalid prepaid obligation')
            key = (row.source, row.target, row.kind)
            exact = row.amount_mU + residuals.get(key, self.residuals.get(key, Q(0)))
            amount = floor(exact)
            residuals[key] = exact - amount
            transfers.append(CashTransfer(row.source, row.target, amount))
            amounts.append(amount)
        return PaidResources(self, self._revision, self.ledger.prepare_transfers(transfers),
                             tuple(residuals.items()), tuple(amounts),tuple(transfers))

    def prepare_invoice(self, obligations: tuple[Obligation, ...], *, kind: str,
                        key: tuple[str | int, ...],payer_scope: CashAccount | None = None) -> PaidResources:
        """One integer debit per payer, then conserved multi-payee allocation.

        Use for a single service bill (such as a dispatched electrical load).
        Independent bilateral fees continue to use prepare()/pay(). The
        aggregate invoice residual has its own non-spendable accounting key.
        """
        if not kind:
            raise InvariantViolation('unnamed multi-party invoice')
        grouped = defaultdict(list)
        for index, row in enumerate(obligations):
            if row.amount_mU < 0 or row.source.currency != row.target.currency:
                raise InvariantViolation('invalid invoice obligation')
            grouped[row.source].append((index, row))
        if payer_scope is not None and (len(grouped)>1 or any(a.currency!=payer_scope.currency for a in grouped)):
            raise InvariantViolation('invoice economic payer has incompatible custody sources')
        transfers, residuals, amounts = [], [], [0] * len(obligations)
        for source, rows in sorted(grouped.items()):
            residual_key = (payer_scope or source, CashAccount('clearing', kind, source.currency), f'invoice:{kind}')
            exact = sum((row.amount_mU for _, row in rows), Q(0))
            total = exact + self.residuals.get(residual_key, Q(0))
            debit = floor(total)
            residuals.append((residual_key, total - debit))
            recipients = defaultdict(Fraction)
            for _, row in rows:
                recipients[(row.target, row.kind)] += row.amount_mU
            targets = sorted(recipients)
            allocation = split_payment(debit, {str(i): recipients[t] for i, t in enumerate(targets)},
                key=(*key, source.owner, source.name, source.currency))
            for i, (target, fee_kind) in enumerate(targets):
                transfers.append(CashTransfer(source, target, allocation[str(i)]))
                members = {str(j): row.amount_mU for j, row in rows if (row.target, row.kind) == (target, fee_kind)}
                parts = split_payment(allocation[str(i)], members, key=(*key, 'invoice_attribution', str(i)))
                for j, amount in parts.items():
                    amounts[int(j)] = amount
        return PaidResources(self, self._revision, self.ledger.prepare_transfers(transfers),
                             tuple(residuals), tuple(amounts),tuple(transfers))

    def quote(self, source: CashAccount, target: CashAccount, amount: Fraction, kind: str) -> int:
        if amount < 0 or source.currency != target.currency:
            raise InvariantViolation('invalid obligation')
        return floor(amount + self.residuals.get((source, target, kind), Q(0)))

    def pay(self, source: CashAccount, target: CashAccount, amount: Fraction, *, kind: str,
            allow_arrears: bool = False,settle_arrears: bool = False) -> Payment:
        key = (source, target, kind)
        exact = amount + self.residuals.get(key, Q(0))
        current_due = self.quote(source, target, amount, kind)
        due=current_due+(self.arrears.get(key,0) if settle_arrears else 0)
        available = self.ledger.balance(source).native_mU
        if due > available and not allow_arrears:
            raise RuleViolation('ERR_INSUFFICIENT_CASH')
        paid = min(due, available)
        self.ledger.transfer(source, target, paid)
        if exact != current_due:
            self.residuals[key] = exact - current_due
        else:
            self.residuals.pop(key, None)
        if settle_arrears:
            if paid<due:self.arrears[key]=due-paid
            else:self.arrears.pop(key,None)
        elif paid < due:
            self.arrears[key] = self.arrears.get(key, 0) + due - paid
        self._revision += 1
        return Payment(due, paid, due - paid)

    def reserve(self, source: CashAccount, name: str, amount: int) -> CashAccount:
        account = CashAccount(source.owner, name, source.currency)
        if self.ledger.balance(account).native_mU:
            raise InvariantViolation('reservation reused before release')
        self.ledger.transfer(source, account, amount)
        return account

    def release(self, reservation: CashAccount, source: CashAccount) -> None:
        self.ledger.transfer(reservation, source, self.ledger.balance(reservation).native_mU)

    def snapshot(self) -> dict:
        def rows(values):
            return [[a.owner, a.name, a.currency, b.owner, b.name, b.currency, kind, value]
                    for (a, b, kind), value in sorted(values.items()) if value]
        return {'residuals': rows(self.residuals), 'arrears': rows(self.arrears)}

    def unpaid_fraction(self, payer: CashAccount) -> Fraction:
        return sum((q for (source, _, _), q in self.residuals.items() if source == payer), Q(0))


@dataclass(frozen=True)
class FXRequest:
    id: str
    source: CashAccount
    destination: CashAccount
    amount_mU: int  # offered source currency, inclusive of fee
    rate: Fraction  # destination units per source unit
    monetary_intervention: bool = False
    principal_limit_mU: int | None = None


@dataclass(frozen=True)
class FXFill:
    given_mU: int = 0
    received_mU: int = 0
    fee_mU: int = 0
    control_mU: int = 0
    executed_ru_mU: int = 0


@dataclass(frozen=True)
class PoolPayment:
    id: str
    recipient: CashAccount
    amount_mU: int


@dataclass(frozen=True)
class PoolFill:
    paid_mU: int = 0
    book_ru_mU: Fraction = Q(0)


class ForeignExchange:
    def __init__(self, ledger: CashLedger, payments: Payments, fee: Fraction = Q(1, 500)):
        self.ledger, self.payments, self.fee = ledger, payments, fee
        self.principal_residuals: dict[tuple[CashAccount, CashAccount], Fraction] = {}
        self.controls: dict[str, int] = {}
        self.fee_discounts: dict[str, Fraction] = {}
        self.last_fills: dict[str, FXFill] = {}
        self.principal_carries = {}
        self.fee_carries = {}
        self.last_pool_fills = {}

    @staticmethod
    def _carry(book,key,legacy):
        if key in book:return book[key]
        carry=RationalCarry()
        carry.add(legacy.get(key,Q(0)))
        return carry

    def _fee_quote(self,source,target,kind,value,updates=None):
        key=(source,target,kind)
        carry=(updates or {}).get(key)
        if carry is None:carry=self._carry(self.fee_carries,key,self.payments.residuals)
        return carry.prepare(value).due

    def control_rate(self, request: FXRequest) -> Fraction:
        # A central bank's own intervention does not tax itself. RU -> native
        # brings capital in; native -> RU is subject to the issuing country's
        # control. All callers, including operating FX, use this same path.
        if request.monetary_intervention or request.source.currency == 'RU':
            return Q(0)
        return Q(self.controls.get(request.source.currency, 0), 10000)

    def fee_rate(self, request: FXRequest) -> Fraction:
        discount=self.fee_discounts.get(request.source.owner,Q(0))
        if not 0<=discount<=Q(1,2):raise InvariantViolation('invalid AI FX fee discount')
        return self.fee*(1-discount)

    def execute(self, seed: int, tick: int, requests: tuple[FXRequest, ...], *,
                pool_payments: tuple[PoolPayment,...] = ()) -> dict[str, int]:
        """Coalesce identical cash legs before sharing a finite currency pool.

        A source/destination remainder is a single claim, not one claim per
        submitted command. Splitting a request must not multiply that remainder.
        Integer output attribution is diagnostic; all members own the same
        actual receiving account.
        """
        if len({r.id for r in requests}) != len(requests):
            raise InvariantViolation('duplicate FX request')
        if len({p.id for p in pool_payments})!=len(pool_payments):raise InvariantViolation('duplicate direct pool claim')
        for payment in pool_payments:
            if (not payment.id or type(payment.amount_mU) is not int or payment.amount_mU<0 or
                    payment.recipient==CashAccount('world','pool',payment.recipient.currency)):
                raise RuleViolation('ERR_SCHEMA','invalid direct pool claim')
        groups = defaultdict(list)
        for r in requests:
            if (type(r.amount_mU) is not int or r.amount_mU < 0 or not isinstance(r.rate, (int, Fraction))
                    or isinstance(r.rate, bool) or r.rate <= 0 or r.source.owner != r.destination.owner
                    or r.source.currency == r.destination.currency or 'RU' not in (r.source.currency, r.destination.currency)
                    or r.principal_limit_mU is not None and (type(r.principal_limit_mU) is not int or r.principal_limit_mU < 0)):
                raise RuleViolation('ERR_SCHEMA', 'invalid FX request')
            groups[(r.source, r.destination)].append(r)
        merged, members = [], {}
        for (source, destination), rows in groups.items():
            if any(r.rate != rows[0].rate or r.monetary_intervention != rows[0].monetary_intervention for r in rows):
                raise InvariantViolation('different spot quotes for identical FX legs')
            gid = 'fx:' + fingerprint((source, destination))[:40]
            merged.append(FXRequest(gid, source, destination, sum(r.amount_mU for r in rows), rows[0].rate,
                                    rows[0].monetary_intervention,
                                    None if any(r.principal_limit_mU is None for r in rows) else sum(r.principal_limit_mU for r in rows)))
            members[gid] = rows
        totals = self._execute_groups(seed, tick, tuple(merged),pool_payments=pool_payments)
        result, details = {}, {}
        for gid, rows in members.items():
            weights = {r.id: Q(r.amount_mU) for r in rows}
            allocations = {field: split_payment(getattr(totals[gid], field), weights,
                key=(seed, 'fx_command_attribution', tick, gid, field)) for field in FXFill.__dataclass_fields__}
            for r in rows:
                details[r.id] = FXFill(**{field: values[r.id] for field, values in allocations.items()})
                result[r.id] = details[r.id].received_mU
        self.last_fills = details
        return result

    def _execute_groups(self, seed: int, tick: int, requests: tuple[FXRequest, ...], *,
                        pool_payments: tuple[PoolPayment,...] = ()) -> dict[str, FXFill]:
        """All requests for a pool currency compete using its frozen balance.

        Only RU/native pairs are admitted. Quotes from this tick do not spend
        cash that a different request will credit later in the same batch.
        """
        if len({r.id for r in requests}) != len(requests):
            raise InvariantViolation('duplicate FX request')
        source_demand: dict[CashAccount, dict[str, int]] = defaultdict(dict)
        for r in requests:
            if (r.amount_mU < 0 or r.rate <= 0 or r.source.owner != r.destination.owner or
                    r.source.currency == r.destination.currency or
                    'RU' not in (r.source.currency, r.destination.currency)):
                raise RuleViolation('ERR_SCHEMA', 'invalid FX request')
            source_demand[r.source][r.id] = r.amount_mU
        offered = {}
        for account, demand in source_demand.items():
            offered.update(proportional(self.ledger.balance(account).native_mU, demand,
                key=(seed, 'FX_source', tick, account.owner, account.name, account.currency)))
        requests = tuple(sorted(requests, key=lambda r: r.id))
        principal, wanted, pools = {}, {}, defaultdict(dict)
        for r in requests:
            control = self.control_rate(r)
            pool = CashAccount('world', 'pool', r.source.currency)
            treasury = CashAccount(r.source.currency, 'treasury', r.source.currency)
            maximum = min(offered[r.id], r.principal_limit_mU) if r.principal_limit_mU is not None else offered[r.id]
            principal[r.id] = max_feasible(maximum, lambda q:
                q + self._fee_quote(r.source,pool,'fx_fee',q*self.fee_rate(r)) +
                (self._fee_quote(r.source,treasury,'capital_control',q*control) if control else 0)
                <= offered[r.id])
            carry=self._carry(self.principal_carries,(r.source,r.destination),self.principal_residuals)
            wanted[r.id] = carry.prepare(principal[r.id]*r.rate).due
            pools[r.destination.currency][r.id] = wanted[r.id]
        granted = {}
        direct_groups=defaultdict(list)
        for payment in pool_payments:direct_groups[payment.recipient].append(payment)
        direct_keys={account:'pool:'+fingerprint(account) for account in direct_groups}
        for account,rows in direct_groups.items():
            pools[account.currency][direct_keys[account]]=sum(p.amount_mU for p in rows)
        for currency, demand in pools.items():
            pool = CashAccount('world', 'pool', currency)
            granted.update(proportional(self.ledger.balance(pool).native_mU, demand,
                                       key=(seed, 'FX_pool', tick, currency)))
        received, exchanges, transfers, principal_updates, fee_updates = {}, [], [], {}, {}
        direct_grants={}
        for account,rows in direct_groups.items():
            direct_grants.update(proportional(granted[direct_keys[account]],{p.id:p.amount_mU for p in rows},
                key=(seed,'pool_claim_attribution',tick,account.owner,account.name,account.currency)))
        for payment in pool_payments:
            transfers.append(CashTransfer(CashAccount('world','pool',payment.recipient.currency),payment.recipient,
                                           direct_grants[payment.id]))
        for r in requests:
            key = (r.source, r.destination)
            carry=self._carry(self.principal_carries,key,self.principal_residuals)
            give=max_feasible(principal[r.id],lambda q:carry.prepare(q*r.rate).due<=granted[r.id])
            control = self.control_rate(r)
            pool = CashAccount('world', 'pool', r.source.currency)
            treasury = CashAccount(r.source.currency, 'treasury', r.source.currency)
            fee_specs = ((pool, self.fee_rate(r), 'fx_fee'), (treasury, control, 'capital_control'))
            # Other destinations using this same source may have advanced its
            # fee residual. Recheck before constructing any mutation.
            give = max_feasible(give, lambda q: q + sum(
                self._fee_quote(r.source,target,kind,q*rate,fee_updates)
                for target, rate, kind in fee_specs if rate) <= offered[r.id])
            take = carry.prepare(give*r.rate).due
            if give == 0 or take == 0:
                received[r.id] = FXFill()
                continue
            counter_receipt = CashAccount('world', 'pool', r.source.currency)
            counter_payment = CashAccount('world', 'pool', r.destination.currency)
            amounts = []
            control = self.control_rate(r)
            treasury = CashAccount(r.source.currency, 'treasury', r.source.currency)
            for target, rate, kind in ((counter_receipt, self.fee_rate(r), 'fx_fee'),
                                       (treasury, control, 'capital_control')):
                fee_key = (r.source, target, kind)
                amount=0
                if rate:
                    previous=fee_updates.get(fee_key)
                    previous=previous if previous is not None else self._carry(self.fee_carries,fee_key,self.payments.residuals)
                    updated=previous.clone()
                    amount=updated.add(give*rate)
                    fee_updates[fee_key]=updated
                    transfers.append(CashTransfer(r.source, target, amount))
                amounts.append(amount)
            if give + sum(amounts) > offered[r.id]:
                raise InvariantViolation('FX preflight failed to reserve its fees')
            value_ru = Q(give) if r.source.currency == 'RU' else Q(take)
            exchanges.append(CashExchange(r.source, counter_receipt, counter_payment, r.destination, give, take, value_ru))
            updated=carry.clone();updated.add(give*r.rate)
            principal_updates[key] = updated
            received[r.id] = FXFill(give, take, *amounts, int(value_ru))
        # The only mutation point: source budgets, destination liquidity and all
        # fees have been checked against one frozen set of opening balances.
        plan=self.ledger.prepare_exchanges(exchanges,transfers=transfers)
        recipients=defaultdict(dict)
        for payment in pool_payments:
            recipients[payment.recipient][payment.id]=direct_grants[payment.id]
        direct_fills={}
        for move in plan.movements:
            if move.source!=CashAccount('world','pool',move.source.currency) or move.destination not in recipients:continue
            claims=recipients[move.destination]
            cost=move.book_ru_mU*CASH_BOOK_SCALE
            if cost.denominator!=1:raise InvariantViolation('direct pool cost left auxiliary grid')
            allocation=split_payment(int(cost),{pid:Q(q) for pid,q in claims.items()},
                key=(seed,'pool_claim_basis',tick,move.destination.owner,move.destination.name,move.destination.currency))
            for pid,q in claims.items():direct_fills[pid]=PoolFill(q,Q(allocation[pid],CASH_BOOK_SCALE))
        plan.commit()
        self.last_pool_fills={p.id:direct_fills.get(p.id,PoolFill()) for p in pool_payments}
        self.principal_carries.update(principal_updates)
        for key in principal_updates:self.principal_residuals.pop(key,None)
        for key, value in fee_updates.items():
            self.fee_carries[key]=value
            self.payments.residuals.pop(key,None)
        self.payments._revision += 1
        return received

    def snapshot(self) -> dict:
        return {'principal_residuals': [[a.owner, a.name, a.currency, b.owner, b.name, b.currency, value]
                for (a, b), value in sorted(self.principal_residuals.items()) if value],
                'principal_carries':[[a.owner,a.name,a.currency,b.owner,b.name,b.currency,c.snapshot()]
                    for (a,b),c in sorted(self.principal_carries.items())],
                'fee_carries':[[a.owner,a.name,a.currency,b.owner,b.name,b.currency,kind,c.snapshot()]
                    for (a,b,kind),c in sorted(self.fee_carries.items())],
                'controls': dict(self.controls),'fee_discounts':dict(self.fee_discounts)}
