import heapq
import random
import unittest
from fractions import Fraction as Q

from organizer.rift_era.engine.assets import Asset
from organizer.rift_era.engine.determinism import digest
from organizer.rift_era.engine.errors import InvariantViolation
from organizer.rift_era.engine.ledger import Account, CashAccount, CashLedger, Inventory
from organizer.rift_era.engine.maintenance import Maintenance
from organizer.rift_era.engine.numeric import fair_service
from organizer.rift_era.engine.payments import ForeignExchange, FXRequest, Payments


class AllocationRegressions(unittest.TestCase):
    def test_bulk_fair_service_matches_independent_token_oracle(self):
        rng = random.Random(714)
        cases = [({'a': 1, 'b': 9, 'c': 21}, {'a': 0, 'b': 0, 'c': 0}, {'a': 1, 'b': 9, 'c': 21}, 3)]
        for _ in range(1000):
            demands = {str(i): rng.randrange(1, 80) for i in range(rng.randrange(1, 12))}
            received = {k: rng.randrange(d + 1) for k, d in demands.items()}
            caps = {k: rng.randrange(d - received[k] + 1) for k, d in demands.items()}
            cases.append((demands, received, caps, rng.randrange(200)))
        for demand, received, caps, capacity in cases:
            key = (17, 'oracle')
            expected = dict.fromkeys(demand, 0)
            queue = [(Q(received[k], d), digest(*key, k), k) for k, d in demand.items() if caps[k]]
            heapq.heapify(queue)
            for _ in range(min(capacity, sum(caps.values()))):
                _, tie, k = heapq.heappop(queue)
                expected[k] += 1
                if expected[k] < caps[k]:
                    heapq.heappush(queue, (Q(received[k] + expected[k], demand[k]), tie, k))
            self.assertEqual(fair_service(capacity, demand, received, caps, key=key), expected)

    def test_many_tiny_maintenance_invoices_share_one_cash_pool(self):
        inv, cash = Inventory(), CashLedger()
        stock = Account('A', 'h', 'available', 'parts')
        payer, seller = CashAccount('A', 'treasury', 'A'), CashAccount('A', 'seller', 'A')
        inv.initialize(stock, 20)
        cash.initialize(payer, 2)
        inv.seal()
        cash.seal()
        assets = {str(i): Asset(str(i), 'A', 'h', {'steel': 1000000}) for i in range(20)}
        maintenance = Maintenance(inv, cash, Payments(cash), assets, seed=7)
        for aid in assets:
            maintenance.register(aid, {'parts': 1}, payer, {'parts': seller})
        maintenance.begin_tick(199)
        requests = maintenance.requests(lambda g, h: Q(1, 10))
        self.assertEqual(maintenance.funding_requests(requests), {payer: 2})
        maintenance.reserve(requests)
        receipts = maintenance.settle()
        self.assertTrue(all(r.coverage == 1 for r in receipts))
        self.assertEqual(cash.balance(seller).native_mU, 2)
        self.assertEqual(inv.balance(stock), 0)
        maintenance.audit()
        cash.audit()

    def test_split_fx_does_not_duplicate_a_shared_principal_remainder(self):
        def execute(split):
            cash = CashLedger()
            sources = {p: CashAccount(p, 'treasury', p) for p in ('A', 'B')}
            targets = {p: CashAccount(p, 'reserve', 'RU') for p in sources}
            for source in sources.values():
                cash.initialize(source, 500)
            cash.initialize(CashAccount('world', 'pool', 'RU'), 9)
            cash.seal()
            fx = ForeignExchange(cash, Payments(cash), fee=Q(0))
            fx.principal_residuals[(sources['A'], targets['A'])] = Q(9, 10)
            rows = [FXRequest(str(i), sources['A'], targets['A'], 1, Q(1, 10)) for i in range(80)] if split else [
                FXRequest('combined', sources['A'], targets['A'], 80, Q(1, 10))]
            rows.append(FXRequest('competitor', sources['B'], targets['B'], 80, Q(1, 10)))
            fx.execute(12, 5, tuple(rows))
            cash.audit()
            return cash.snapshot(), fx.snapshot()
        self.assertEqual(execute(True), execute(False))

    def test_inconsistent_fx_rates_reject_before_any_cash_or_fee_mutation(self):
        cash = CashLedger()
        source, target = CashAccount('A', 'a', 'A'), CashAccount('A', 'b', 'RU')
        cash.initialize(source, 100)
        cash.initialize(CashAccount('world', 'pool', 'RU'), 100)
        cash.seal()
        payments = Payments(cash)
        fx = ForeignExchange(cash, payments)
        before = cash.snapshot(), payments.snapshot(), fx.snapshot()
        with self.assertRaises(InvariantViolation):
            fx.execute(1, 0, (FXRequest('1', source, target, 10, Q(1)), FXRequest('2', source, target, 10, Q(2))))
        self.assertEqual(before, (cash.snapshot(), payments.snapshot(), fx.snapshot()))


if __name__ == '__main__':
    unittest.main()
