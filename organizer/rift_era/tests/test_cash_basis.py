"""Independent exact-rational oracle for the bounded auxiliary cost book."""

import random
import unittest
from fractions import Fraction as Q

from organizer.rift_era.engine.determinism import canonical
from organizer.rift_era.engine.errors import RuleViolation
from organizer.rift_era.engine.ledger import CASH_BOOK_SCALE, CashAccount, CashExchange, CashLedger, CashTransfer


class CashBasisTests(unittest.TestCase):
    def test_mixed_costs_conserve_basis_and_remain_close_to_exact_oracle(self):
        cash = CashLedger()
        accounts = [CashAccount(str(i), 'a', 'currency') for i in range(4)]
        native, exact = {}, {}
        for i, account in enumerate(accounts):
            native[account], exact[account] = 1000, Q((i + 1) * 1000)
            cash.initialize(account, 1000, rate=Q(i + 1))
        cash.seal()
        rng = random.Random(31)
        for _ in range(600):
            source, destination = rng.sample(accounts, 2)
            amount = rng.randrange(min(13, native[source]) + 1)
            basis = exact[source] * amount / native[source] if native[source] else Q(0)
            native[source] -= amount
            native[destination] += amount
            exact[source] -= basis
            exact[destination] += basis
            cash.transfer(source, destination, amount)
        self.assertEqual(sum(b.book_ru_mU for b in cash.balances().values()), 10000)
        error = sum(abs(cash.balance(a).book_ru_mU - exact[a]) for a in accounts)
        self.assertLessEqual(error, Q(2 * cash.basis_rounding_steps, CASH_BOOK_SCALE))
        self.assertTrue(all(CASH_BOOK_SCALE % b.book_ru_mU.denominator == 0 for b in cash.balances().values()))
        cash.audit()

    def test_repeated_mixing_has_bounded_serialized_state_and_full_refund_preserves_basis(self):
        cash = CashLedger()
        a, b, escrow = (CashAccount('p', name, 'p') for name in ('a', 'b', 'escrow'))
        cash.initialize(a, 1000)
        cash.initialize(b, 1000, rate=Q(2))
        cash.seal()
        for _ in range(4000):
            cash.transfer(a, b, 1)
            cash.transfer(b, a, 1)
        before = cash.balance(a)
        cash.transfer(a, escrow, before.native_mU)
        cash.transfer(escrow, a, before.native_mU)
        self.assertEqual(cash.balance(a), before)
        self.assertEqual(cash.owner_book('p'), 3000)
        self.assertLess(len(canonical(cash.snapshot())), 1024)
        cash.audit()

    def test_split_transfer_and_failed_batch_do_not_change_books(self):
        def run(split):
            cash = CashLedger()
            a, b = CashAccount('a', 'a', 'q'), CashAccount('b', 'b', 'q')
            cash.initialize(a, 1000, rate=Q(3, 2))
            cash.seal()
            cash.transfer_many([CashTransfer(a, b, 7)] * 9 if split else [CashTransfer(a, b, 63)])
            before = cash.snapshot()
            with self.assertRaises(RuleViolation):
                cash.prepare_transfers([CashTransfer(a, b, 600), CashTransfer(a, b, 600)])
            self.assertEqual(before, cash.snapshot())
            return before
        self.assertEqual(run(True), run(False))

    def test_fx_batch_basis_is_frozen_and_incoming_money_cannot_finance_outgoing(self):
        cash = CashLedger()
        a, b = CashAccount('a', 'a', 'a'), CashAccount('b', 'b', 'RU')
        ap, bp = CashAccount('world', 'pool', 'a'), CashAccount('world', 'pool', 'RU')
        ar, bn = CashAccount('a', 'b', 'RU'), CashAccount('b', 'a', 'a')
        cash.initialize(a, 100)
        cash.initialize(b, 100)
        cash.initialize(ap, 1)
        cash.initialize(bp, 1)
        cash.seal()
        before = cash.snapshot()
        with self.assertRaises(RuleViolation):
            cash.prepare_exchanges((CashExchange(a, ap, bp, ar, 100, 100, Q(100)),
                                    CashExchange(b, bp, ap, bn, 100, 100, Q(100))))
        self.assertEqual(cash.snapshot(), before)


if __name__ == '__main__':
    unittest.main()
