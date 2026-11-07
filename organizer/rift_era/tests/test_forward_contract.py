"""06/11 logistics/forward_contract (forward_v1)."""
import unittest

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.ledger import Account, CashAccount
from organizer.rift_era.engine.world import generate_candidate


class ForwardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog, cls.world = Catalog(), generate_candidate(1000)

    def game(self, ticks=110):
        game = opening_economy(self.catalog, initialize(self.catalog, self.world), total_ticks=ticks, commerce=True, debug=False)
        state = game.state
        for p in ('n0', 'n1'):
            state.initial.cash.transfer(CashAccount('world', 'pool', 'RU'), CashAccount(p, 'reserves', 'RU'), 50_000_000)
            state.research[p].known.add('F03')
        return game, state

    @staticmethod
    def order(i, counterparty, side, delivery=10, qty=5000):
        return {'id': i, 'kind': 'logistics', 'op': 'forward_contract', 'counterparty': counterparty, 'good': 'stone',
                'qty_mU': qty, 'strike_price_ru_mU': 20000, 'delivery_cycle': delivery, 'delivery_hub': 'n1-h0',
                'side': side}

    def last(self, state, p):
        return state.command_streams[p].recent[-1]

    def test_terms_and_f03_are_checked(self):
        game, state = self.game(20)
        game.step({'n0': (self.order(1, 'n1', 'buy', delivery=11),)})
        self.assertEqual(self.last(state, 'n0').code, 'ERR_SCHEMA')
        state.research['n0'].known.discard('F03')
        for _ in range(4):
            game.step()
        game.step({'n0': (self.order(2, 'n1', 'buy', delivery=10),)})
        self.assertEqual(self.last(state, 'n0').code, 'ERR_TECH_LOCKED')

    def test_mirror_order_signs_locks_margin_and_delivers_dvp(self):
        game, state = self.game()
        inv, cash = state.initial.inventory, state.initial.cash
        buyer_stock = Account('n0', 'n1-h0', 'available', 'stone')
        seller_stock = Account('n1', 'n1-h0', 'available', 'stone')
        self.assertGreaterEqual(inv.balance(seller_stock), 5000)
        game.step({'n0': (self.order(1, 'n1', 'buy'),), 'n1': (self.order(1, 'n0', 'sell'),)})
        row = state.forwards.rows['forward:n0:1']
        self.assertEqual(row.status, 'ACTIVE')
        margin = 5000 * 20000 // 1000 // 5
        for p in ('n0', 'n1'):
            self.assertGreaterEqual(cash.balance(CashAccount(p, 'forward_margin:forward:n0:1', 'RU')).native_mU, margin)
        seller_ru = cash.balance(CashAccount('n1', 'reserves', 'RU')).native_mU
        seller_before = inv.balance(seller_stock)
        while game.tick < 101:
            game.step()
        self.assertNotIn('forward:n0:1', state.forwards.rows)
        self.assertEqual(state.forwards.recent['n0'][-1]['status'], 'DELIVERED')
        self.assertEqual(inv.balance(buyer_stock), 5000)
        # Seller received the strike (100 RU) plus its own margin back.
        self.assertGreaterEqual(cash.balance(CashAccount('n1', 'reserves', 'RU')).native_mU, seller_ru + 100_000)
        for p in ('n0', 'n1'):
            self.assertEqual(cash.balance(CashAccount(p, 'forward_margin:forward:n0:1', 'RU')).native_mU, 0)
        state.audit()


if __name__ == '__main__':
    unittest.main()
