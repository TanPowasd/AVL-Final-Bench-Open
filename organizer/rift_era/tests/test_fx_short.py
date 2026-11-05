"""02/06 finance/fx_short and finance/fx_cover (fx_short_v1)."""
import unittest

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.ledger import CashAccount
from organizer.rift_era.engine.world import generate_candidate


class FXShortTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog, cls.world = Catalog(), generate_candidate(1000)

    def game(self):
        game = opening_economy(self.catalog, initialize(self.catalog, self.world), total_ticks=40, commerce=True, debug=False)
        state = game.state
        # Fixture: move real RU from the world pool into n0's reserves (no cash is minted).
        state.initial.cash.transfer(CashAccount('world', 'pool', 'RU'), CashAccount('n0', 'reserves', 'RU'), 100_000_000)
        state.research['n0'].known.add('F03')
        return game, state

    def order(self, i, op, amount, currency='n1'):
        return {'id': i, 'kind': 'finance', 'op': op, 'currency': currency, 'amount_mU': amount}

    def last(self, state):
        return state.command_streams['n0'].recent[-1]

    def balance(self, state, account):
        return state.initial.cash.balance(account).native_mU

    def test_requires_f03_and_respects_the_pool_limit(self):
        game, state = self.game()
        state.research['n0'].known.discard('F03')
        game.step({'n0': (self.order(1, 'fx_short', 1_000_000),)})
        self.assertEqual(self.last(state).code, 'ERR_TECH_LOCKED')
        state.research['n0'].known.add('F03')
        for _ in range(4):
            game.step()
        pool = self.balance(state, CashAccount('world', 'pool', 'n1'))
        game.step({'n0': (self.order(2, 'fx_short', pool // 4 + 1),)})
        self.assertEqual(self.last(state).code, 'ERR_CAPACITY_EXCEEDED')

    def test_short_borrows_real_currency_sells_it_and_cover_repays(self):
        game, state = self.game()
        reserves = CashAccount('n0', 'reserves', 'RU')
        pool_q = CashAccount('world', 'pool', 'n1')
        before_ru, before_pool = self.balance(state, reserves), self.balance(state, pool_q)
        game.step({'n0': (self.order(1, 'fx_short', 10_000_000),)})
        self.assertEqual(self.last(state).status, 'ACCEPTED')
        debt = state.fx_shorts.debts['n0/n1']
        self.assertEqual(debt.principal_mU, 10_000_000)
        self.assertEqual(debt.collateral_mU, 5_000_000)
        self.assertEqual(self.balance(state, CashAccount('n0', 'fx_short_collateral:n1', 'RU')), 5_000_000)
        # Sold for RU: reserves net of collateral rose by about the sale proceeds less the fee.
        self.assertGreater(self.balance(state, reserves), before_ru - 5_000_000 + 9_900_000)
        while game.tick < 10:
            game.step()
        game.step()                                                   # cycle 1 start accrues interest
        self.assertGreater(debt.principal_mU, 10_000_000)
        while game.tick < 15:
            game.step()
        owed = debt.principal_mU
        game.step({'n0': (self.order(2, 'fx_cover', owed),)})
        self.assertEqual(self.last(state).status, 'ACCEPTED')
        remaining = state.fx_shorts.debts.get('n0/n1')
        self.assertTrue(remaining is None or remaining.principal_mU < owed // 100)
        if remaining is None:
            self.assertEqual(self.balance(state, CashAccount('n0', 'fx_short_collateral:n1', 'RU')), 0)
        state.audit()


if __name__ == '__main__':
    unittest.main()
