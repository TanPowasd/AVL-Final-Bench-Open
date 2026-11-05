"""Atomic integer inventories and multi-currency historical-cost cash books.

Credits never fund other debits within the same transaction. Production and
consumption have explicit source/sink entries; a transfer cannot create money.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from fractions import Fraction

from .errors import InvariantViolation, RuleViolation
from .numeric import Q, floor, nonnegative, split_payment
from .materials import MaterialBook, MaterialPlan, Origin
from .provenance import Leg, Use

# The auxiliary historical-cost book has finer resolution than spendable mU.
# All allocations conserve this integer grid. This is a versioned precision
# choice, not an FX revaluation; see IMPLEMENTATION_CONTRACT.md.
CASH_BOOK_SCALE = 10**12


@dataclass(frozen=True, order=True)
class Account:
    owner: str
    location: str
    bucket: str
    asset: str


@dataclass(frozen=True)
class Transfer:
    source: Account
    destination: Account
    quantity: int
    legs: tuple[Leg, ...] = ()
    destination_hub: str | None = None  # loading a mobile container, not teleportation
    origin_nation: str | None = None  # organizer-only selection from real provenance
    retitle: bool = False  # a real purchase replaces the previous economic title


class Inventory:
    """All quantities are mU; warehouse, locks and transit are distinct accounts."""

    def __init__(self) -> None:
        self._balances: dict[Account, int] = {}
        self._sources: dict[str, int] = defaultdict(int)
        self._sinks: dict[str, int] = defaultdict(int)
        self._sealed = False
        self._revision = 0
        self.materials: MaterialBook | None = None
        self._untracked_activity = False

    def enable_provenance(self, *, seed: int = 0, credits: dict[str, Fraction] | None = None) -> MaterialBook:
        """Only at genesis, before transfers/production under the tracked ruleset."""
        if self.materials is not None or self._untracked_activity:
            raise InvariantViolation('provenance must be enabled once, before simulation activity')
        self.materials = MaterialBook(self._balances, seed=seed, credits=credits)
        self._revision += 1
        return self.materials

    def balance(self, account: Account) -> int:
        return self._balances.get(account, 0)

    def balances(self) -> Mapping[Account, int]:
        return dict(self._balances)

    def initialize(self, account: Account, quantity: int) -> None:
        if self._sealed:
            raise InvariantViolation("initial endowments already sealed")
        self.transform({}, {account: nonnegative(quantity)}, reason="initial_endowment")

    def seal(self) -> None:
        self._sealed = True

    def transfer(self, source: Account, destination: Account, quantity: int) -> None:
        self.transfer_many((Transfer(source, destination, quantity),))

    def transfer_many(self, transfers: Iterable[Transfer]) -> None:
        self.prepare_transfers(transfers).commit()

    def prepare_transfers(self, transfers: Iterable[Transfer], *, material_plan: MaterialPlan | None = None) -> InventoryTransferPlan:
        """Validate without changing stock, including aggregate source debits."""
        transfers = tuple(transfers)
        if self.materials is None and any(t.origin_nation is not None for t in transfers):
            raise InvariantViolation('origin-selected transfer requires a provenance ledger')
        debits, credits = defaultdict(int), defaultdict(int)
        for t in transfers:
            nonnegative(t.quantity)
            if t.source.asset != t.destination.asset:
                raise InvariantViolation("transfer cannot rename an asset")
            if t.source == t.destination:
                continue
            debits[t.source] += t.quantity
            credits[t.destination] += t.quantity
        balances = self._prepare(debits, credits)
        materials = material_plan or (self.materials.transfers(transfers) if self.materials else None)
        if materials:
            if materials.book is not self.materials:
                raise InvariantViolation('material transfer plan belongs to another inventory')
            materials.check_projection(self, balances)
        return InventoryTransferPlan(self, self._revision, tuple(balances.items()), materials)

    def transform(self, consumed: Mapping[Account, int], produced: Mapping[Account, int], *,
                  reason: str, use: Use = Use.DESTRUCTION,
                  origins: dict[Account, Origin] | None = None,
                  material_plan: MaterialPlan | None = None) -> None:
        self.prepare_transform(consumed, produced, reason=reason, use=use,
                               origins=origins, material_plan=material_plan).commit()

    def prepare_transform(self, consumed: Mapping[Account, int], produced: Mapping[Account, int], *,
                          reason: str, use: Use = Use.DESTRUCTION,
                          origins: dict[Account, Origin] | None = None,
                          material_plan: MaterialPlan | None = None) -> InventoryTransferPlan:
        """Only simulation systems call this for named physical transformations."""
        if not reason:
            raise InvariantViolation("unnamed source/sink")
        balances = self._prepare(consumed, produced)
        if self.materials:
            material_plan = material_plan or self.materials.transform(consumed, produced, reason=reason, use=use, origins=origins)
            if material_plan.book is not self.materials:
                raise InvariantViolation('material plan belongs to a different inventory')
            material_plan.check_projection(self, balances)
        elif material_plan:
            raise InvariantViolation('material plan on untracked inventory')
        return InventoryTransferPlan(self, self._revision, tuple(balances.items()), material_plan,
            tuple(consumed.items()), tuple(produced.items()), reason == 'initial_endowment' and not self._sealed)

    def record_transport(self, accounts: tuple[Account, ...], leg: Leg) -> None:
        """Called only after a physical vehicle actually finishes this edge."""
        if self.materials:
            plan = self.materials.travel(accounts, leg)
            plan.check_projection(self, {})
            plan.commit()
            self._revision += 1

    def _commit(self, debits: Mapping[Account, int], credits: Mapping[Account, int]) -> None:
        self._apply(self._prepare(debits, credits))

    def _prepare(self, debits: Mapping[Account, int], credits: Mapping[Account, int]) -> dict[Account, int]:
        for a, q in debits.items():
            nonnegative(q)
            if q > self.balance(a):
                raise RuleViolation("ERR_INSUFFICIENT_STOCK", a.asset)
        for q in credits.values():
            nonnegative(q)
        return {a: self.balance(a) - debits.get(a, 0) + credits.get(a, 0)
                for a in debits.keys() | credits.keys()}

    def _apply(self, balances: Mapping[Account, int]) -> None:
        for a, balance in balances.items():
            if balance:
                self._balances[a] = balance
            else:
                self._balances.pop(a, None)
        self._revision += 1

    def audit(self) -> None:
        if self.materials:
            self.materials.audit(self._balances)
        totals = defaultdict(int)
        for a, q in self._balances.items():
            if q < 0:
                raise InvariantViolation("negative inventory")
            totals[a.asset] += q
        for g in totals.keys() | self._sources.keys() | self._sinks.keys():
            if totals.get(g, 0) != self._sources.get(g, 0) - self._sinks.get(g, 0):
                raise InvariantViolation(f"inventory conservation: {g}")

    def snapshot(self) -> dict:
        return {"balances": [[a.owner, a.location, a.bucket, a.asset, q]
                              for a, q in sorted(self._balances.items())],
                "sources": dict(self._sources), "sinks": dict(self._sinks), "sealed": self._sealed,
                "materials": self.materials.snapshot() if self.materials else None,
                "untracked_activity": self._untracked_activity}


@dataclass(frozen=True, order=True)
class CashAccount:
    owner: str
    name: str
    currency: str


@dataclass(frozen=True)
class CashTransfer:
    source: CashAccount
    destination: CashAccount
    amount: int


@dataclass(frozen=True)
class CashExchange:
    payer: CashAccount
    counter_receipt: CashAccount
    counter_payment: CashAccount
    recipient: CashAccount
    give_mU: int
    receive_mU: int
    executed_ru_mU: Fraction


@dataclass(frozen=True)
class CashBalance:
    native_mU: int = 0
    book_ru_mU: Fraction = Q(0)


@dataclass(frozen=True)
class CashBookMovement:
    source: CashAccount
    destination: CashAccount | None
    native_mU: int
    book_ru_mU: Fraction


class CashLedger:
    """Global native supply and RU historical basis are separate ledgers."""

    def __init__(self) -> None:
        self._accounts: dict[CashAccount, CashBalance] = {}
        self._initial_supply: dict[str, int] = defaultdict(int)
        self._issued: dict[str, int] = defaultdict(int)
        self.issue_book: dict[str, Fraction] = defaultdict(Fraction)
        self.initial_owner_book: dict[str, Fraction] = defaultdict(Fraction)
        self._sealed = False
        self.fee_remainders: dict[tuple[CashAccount, CashAccount, str], Fraction] = {}
        self._revision = 0
        self.basis_rounding_steps = 0

    def balance(self, account: CashAccount) -> CashBalance:
        return self._accounts.get(account, CashBalance())

    def balances(self) -> Mapping[CashAccount, CashBalance]:
        return dict(self._accounts)

    def supply(self, currency: str) -> int:
        """Includes all custody accounts; transfers never change this quantity."""
        return self._initial_supply.get(currency, 0) + self._issued.get(currency, 0)

    def initialize(self, account: CashAccount, amount: int, *, rate: Fraction = Q(1)) -> None:
        if self._sealed:
            raise InvariantViolation("cash genesis already sealed")
        nonnegative(amount)
        if rate <= 0:
            raise ValueError("nonpositive FX rate")
        old = self.balance(account)
        book = Q(round(amount * rate * CASH_BOOK_SCALE), CASH_BOOK_SCALE)
        self._accounts[account] = CashBalance(old.native_mU + amount, old.book_ru_mU + book)
        self._initial_supply[account.currency] += amount
        self.initial_owner_book[account.owner] += book
        self.basis_rounding_steps += int(book != amount * rate)
        self._revision += 1

    def seal(self) -> None:
        self._sealed = True

    def transfer(self, source: CashAccount, destination: CashAccount, amount: int) -> None:
        self.transfer_many((CashTransfer(source, destination, amount),))

    def transfer_many(self, transfers: Iterable[CashTransfer]) -> None:
        self.prepare_transfers(transfers).commit()

    def prepare_transfers(self, transfers: Iterable[CashTransfer]) -> CashTransferPlan:
        """Freeze source average-cost basis before any leg is applied."""
        return self.prepare_exchanges((), transfers=transfers)

    def prepare_exchanges(self, exchanges: Iterable[CashExchange], *,
                          transfers: Iterable[CashTransfer] = ()) -> CashTransferPlan:
        """Preflight an entire FX batch and its fees against opening balances.

        Pool receipts cannot finance payments in the same batch. Every disposal
        uses the same opening average cost, regardless of request iteration order.
        """
        debits: dict[CashAccount, int] = defaultdict(int)
        credits: dict[CashAccount, int] = defaultdict(int)
        books: dict[CashAccount, Fraction] = defaultdict(Fraction)

        dispositions: dict[CashAccount, dict[CashAccount | None, int]] = defaultdict(lambda: defaultdict(int))
        rounding_steps = 0
        movements=[]

        def dispose(account: CashAccount, amount: int, destination: CashAccount | None = None) -> None:
            nonnegative(amount)
            old = self.balance(account)
            if amount > old.native_mU:
                raise RuleViolation('ERR_INSUFFICIENT_CASH', account.currency)
            debits[account] += amount
            dispositions[account][destination] += amount

        for t in transfers:
            nonnegative(t.amount)
            if t.source.currency != t.destination.currency:
                raise InvariantViolation("cash transfer cannot rename currency")
            if t.source == t.destination or not t.amount:
                continue
            dispose(t.source, t.amount, t.destination)
            credits[t.destination] += t.amount
        for e in exchanges:
            nonnegative(e.give_mU)
            nonnegative(e.receive_mU)
            if (not e.give_mU or not e.receive_mU or e.executed_ru_mU <= 0
                    or e.payer.currency != e.counter_receipt.currency
                    or e.counter_payment.currency != e.recipient.currency
                    or e.payer.currency == e.recipient.currency
                    or len({e.payer, e.counter_receipt, e.counter_payment, e.recipient}) != 4):
                raise RuleViolation('ERR_SCHEMA', 'invalid FX legs')
            dispose(e.payer, e.give_mU)
            dispose(e.counter_payment, e.receive_mU)
            credits[e.counter_receipt] += e.give_mU
            credits[e.recipient] += e.receive_mU
            acquired = Q(round(e.executed_ru_mU * CASH_BOOK_SCALE), CASH_BOOK_SCALE)
            books[e.counter_receipt] += acquired
            books[e.recipient] += acquired
            rounding_steps += 2 * int(acquired != e.executed_ru_mU)
        for source, destinations in dispositions.items():
            old = self.balance(source)
            total = debits[source]
            if total > old.native_mU:
                raise RuleViolation('ERR_INSUFFICIENT_CASH', source.currency)
            if not total:
                continue
            exact = old.book_ru_mU * total * CASH_BOOK_SCALE / old.native_mU
            outgoing = round(exact)
            # Coalesce by actual cash destination: changing command IDs or
            # splitting one payment cannot win additional cost-basis crumbs.
            targets = sorted(destinations, key=lambda a: ('', '', '') if a is None else (a.owner, a.name, a.currency))
            allocation = split_payment(outgoing, {str(i): Q(destinations[a]) for i, a in enumerate(targets)},
                key=('cash_book_v1', source.owner, source.name, source.currency))
            books[source] -= Q(outgoing, CASH_BOOK_SCALE)
            for i, destination in enumerate(targets):
                movements.append(CashBookMovement(source,destination,destinations[destination],Q(allocation[str(i)],CASH_BOOK_SCALE)))
                if destination is not None:
                    books[destination] += Q(allocation[str(i)], CASH_BOOK_SCALE)
            # Conservative L1 bound relative to exact proportional arithmetic:
            # <= 2 / scale per destination. Full one-leg moves are exact.
            rounding_steps += len(targets) if len(targets) > 1 or exact != outgoing else 0
        balances = self._prepare(debits, credits, books)
        return CashTransferPlan(self, self._revision, tuple(balances.items()), rounding_steps,tuple(movements))

    def _commit(self, debits, credits, book_delta) -> None:
        self._apply(self._prepare(debits, credits, book_delta))

    def _prepare(self, debits, credits, book_delta) -> dict[CashAccount, CashBalance]:
        for a, amount in debits.items():
            if amount > self.balance(a).native_mU:
                raise RuleViolation("ERR_INSUFFICIENT_CASH", a.currency)
        prepared = {}
        for a in debits.keys() | credits.keys() | book_delta.keys():
            old = self.balance(a)
            new = CashBalance(old.native_mU - debits.get(a, 0) + credits.get(a, 0),
                              old.book_ru_mU + book_delta.get(a, Q(0)))
            if new.native_mU < 0 or new.book_ru_mU < 0 or (not new.native_mU and new.book_ru_mU):
                raise InvariantViolation("invalid cash basis")
            prepared[a] = new
        return prepared

    def _apply(self, balances: Mapping[CashAccount, CashBalance]) -> None:
        for account, balance in balances.items():
            if balance.native_mU:
                self._accounts[account] = balance
            else:
                self._accounts.pop(account, None)
        self._revision += 1

    def issue(self, account: CashAccount, amount: int, *, rate: Fraction) -> None:
        nonnegative(amount)
        if account.currency == "RU" or account.currency != account.owner or rate <= 0:
            raise RuleViolation("ERR_NO_PERMISSION", "only own sovereign currency may be issued")
        old = self.balance(account)
        book = Q(round(amount * rate * CASH_BOOK_SCALE), CASH_BOOK_SCALE)
        self._accounts[account] = CashBalance(old.native_mU + amount, old.book_ru_mU + book)
        self._issued[account.currency] += amount
        self.issue_book[account.owner] += book
        self.basis_rounding_steps += int(book != amount * rate)
        self._revision += 1

    def exchange(self, payer: CashAccount, counter_receipt: CashAccount,
                 counter_payment: CashAccount, recipient: CashAccount,
                 give_mU: int, receive_mU: int, *, executed_ru_mU: Fraction) -> None:
        """Atomic delivery-versus-payment. Both received legs acquire trade basis.

        Fees are explicit additional same-currency transfers, not implicit minting.
        Callers must aggregate competing requests before calling settlement.
        """
        self.prepare_exchanges((CashExchange(payer, counter_receipt, counter_payment, recipient,
                                             give_mU, receive_mU, executed_ru_mU),)).commit()

    def fractional_fee(self, payer: CashAccount, payee: CashAccount, exact_mU: Fraction,
                       *, kind: str) -> int:
        if exact_mU < 0:
            raise ValueError("negative fee")
        key = (payer, payee, kind)
        total = self.fee_remainders.get(key, Q(0)) + exact_mU
        amount = floor(total)
        self.transfer(payer, payee, amount)
        self.fee_remainders[key] = total - amount
        return amount

    def distribute(self, payer: CashAccount, recipients: Mapping[CashAccount, Fraction],
                   total_mU: int, *, key: tuple[str | int, ...]) -> None:
        accounts = sorted(recipients)
        weights = {str(i): recipients[a] for i, a in enumerate(accounts)}
        quantities = split_payment(total_mU, weights, key=key)
        self.transfer_many(CashTransfer(payer, a, quantities[str(i)]) for i, a in enumerate(accounts))

    def owner_book(self, owner: str) -> Fraction:
        return sum((v.book_ru_mU for a, v in self._accounts.items() if a.owner == owner), Q(0))

    @property
    def basis_error_bound_mU(self) -> Fraction:
        """Conservative global L1 distance from unrounded proportional books."""
        return Q(2 * self.basis_rounding_steps, CASH_BOOK_SCALE)

    def audit(self) -> None:
        totals = defaultdict(int)
        for a, b in self._accounts.items():
            if b.native_mU < 0 or b.book_ru_mU < 0 or (b.native_mU == 0 and b.book_ru_mU):
                raise InvariantViolation("negative or orphan cash basis")
            if CASH_BOOK_SCALE % b.book_ru_mU.denominator:
                raise InvariantViolation('cash basis outside declared precision grid')
            totals[a.currency] += b.native_mU
        for c in totals.keys() | self._initial_supply.keys() | self._issued.keys():
            if totals.get(c, 0) != self._initial_supply.get(c, 0) + self._issued.get(c, 0):
                raise InvariantViolation(f"cash conservation: {c}")

    def snapshot(self) -> dict:
        return {"basis_scale_per_mU": CASH_BOOK_SCALE, "basis_rounding_steps": self.basis_rounding_steps,
                "accounts": [[a.owner, a.name, a.currency, b.native_mU, b.book_ru_mU]
                              for a, b in sorted(self._accounts.items())],
                "initial_supply": dict(self._initial_supply), "issued": dict(self._issued),
                "issue_book": dict(self.issue_book), "initial_owner_book": dict(self.initial_owner_book),
                "fee_remainders": [[list((a.owner, a.name, a.currency)),
                                    list((b.owner, b.name, b.currency)), k, v]
                                   for (a, b, k), v in sorted(self.fee_remainders.items())],
                "sealed": self._sealed}


@dataclass(frozen=True)
class InventoryTransferPlan:
    ledger: Inventory
    revision: int
    balances: tuple[tuple[Account, int], ...]
    materials: MaterialPlan | None = None
    consumed: tuple[tuple[Account, int], ...] = ()
    produced: tuple[tuple[Account, int], ...] = ()
    initial_endowment: bool = False

    def check_current(self) -> None:
        if self.ledger._revision != self.revision:
            raise InvariantViolation('stale or repeated inventory transaction')
        if self.materials:
            self.materials.check_current()

    def commit(self) -> None:
        self.check_current()
        if self.materials is None and not self.initial_endowment and (
                any(q != self.ledger.balance(a) for a, q in self.balances) or
                any(q for _, q in self.consumed) or any(q for _, q in self.produced)):
            self.ledger._untracked_activity = True
        self.ledger._apply(dict(self.balances))
        if self.materials:
            self.materials.commit()
        for account, quantity in self.consumed:
            self.ledger._sinks[account.asset] += quantity
        for account, quantity in self.produced:
            self.ledger._sources[account.asset] += quantity


@dataclass(frozen=True)
class CashTransferPlan:
    ledger: CashLedger
    revision: int
    balances: tuple[tuple[CashAccount, CashBalance], ...]
    basis_rounding_steps: int = 0
    movements: tuple[CashBookMovement, ...] = ()

    def check_current(self) -> None:
        if self.ledger._revision != self.revision:
            raise InvariantViolation('stale or repeated cash transaction')

    def commit(self) -> None:
        self.check_current()
        self.ledger._apply(dict(self.balances))
        self.ledger.basis_rounding_steps += self.basis_rounding_steps


@dataclass(frozen=True)
class DeliveryPayment:
    """Two-ledger transaction for the single-threaded simulation kernel.

    Both ledgers are preflighted before either mutates. A stale plan fails before
    either write; there are no callbacks or business validations between writes.
    This is not a thread-safe transaction manager: only the kernel owns ledgers.
    """
    stock: InventoryTransferPlan
    cash: CashTransferPlan

    @classmethod
    def prepare(cls, inventory: Inventory, cash: CashLedger,
                goods: Iterable[Transfer], money: Iterable[CashTransfer], *,
                material_plan: MaterialPlan | None = None) -> DeliveryPayment:
        return cls(inventory.prepare_transfers(goods, material_plan=material_plan), cash.prepare_transfers(money))

    def commit(self) -> None:
        self.stock.check_current()
        self.cash.check_current()
        self.stock.commit()
        self.cash.commit()
