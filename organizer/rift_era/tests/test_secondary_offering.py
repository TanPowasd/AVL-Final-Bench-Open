"""06 C30 finance/secondary_offering (secondary_offering_v1)."""
import unittest

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.construction import WorkOrder
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.ledger import CashAccount
from organizer.rift_era.engine.world import generate_candidate


class SecondaryOfferingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog, cls.world = Catalog(), generate_candidate(1000)

    def listed(self):
        game = opening_economy(self.catalog, initialize(self.catalog, self.world), total_ticks=40, commerce=True, debug=False)
        state = game.state
        state.research['n0'].known.add('F01')
        game.step({'n0': [{'id': '1', 'kind': 'finance', 'op': 'ipo', 'company': 'n0-tech', 'qty': 1000, 'min_price_mU': 100}]})
        self.assertTrue(state.initial.companies['n0-tech'].listed)
        lab = next(f.id for f in state.initial.facilities.values() if f.owner == 'n0' and f.kind == '实验室')
        farm = next(f.id for f in state.initial.facilities.values() if f.owner == 'n0' and f.kind == '农场')
        while game.tick < 5:
            game.step()
        return game, state, lab, farm

    @staticmethod
    def order(i, qty, targets, cid='n0-tech'):
        return {'id': str(i), 'kind': 'finance', 'op': 'secondary_offering', 'company': cid, 'qty': qty,
                'min_price_mU': 100, 'target_facilities': list(targets)}

    def last(self, state):
        return state.command_streams['n0'].recent[-1]

    def test_limits_and_targets(self):
        game, state, lab, farm = self.listed()
        game.step({'n0': [self.order(2, 1000, [lab], cid='n0-mining')]})
        self.assertEqual(self.last(state).code, 'ERR_NO_PERMISSION')          # not listed
        for _ in range(4):
            game.step()
        game.step({'n0': [self.order(3, 1000, [farm])]})
        self.assertEqual(self.last(state).code, 'ERR_SITE_INCOMPATIBLE')      # other industry
        for _ in range(4):
            game.step()
        game.step({'n0': [self.order(4, 100001, [lab])]})
        self.assertEqual(self.last(state).code, 'ERR_SCHEMA')
        for _ in range(4):
            game.step()
        game.step({'n0': [self.order(5, 60000, [lab])]})
        self.assertEqual(self.last(state).status, 'ACCEPTED')
        for _ in range(4):
            game.step()
        game.step({'n0': [self.order(6, 50000, [lab])]})
        self.assertEqual(self.last(state).code, 'ERR_CAPACITY_EXCEEDED')      # 100k per 100 cycles

    def test_proceeds_go_to_escrow_and_fund_only_listed_facilities(self):
        game, state, lab, farm = self.listed()
        game.step({'n0': [self.order(2, 20000, [lab])]})
        self.assertEqual(self.last(state).status, 'ACCEPTED')
        market = state.stock_market
        self.assertIn('secondary', market.issue_orders.values())
        self.assertEqual(market.escrow_targets['n0-tech'], [lab])
        escrow = CashAccount('n0', 'capex_escrow:n0-tech', 'n0')
        before_treasury = state.initial.cash.balance(state.native('n0', 'treasury')).native_mU
        while game.tick < 21:
            game.step()
        raised = state.initial.cash.balance(escrow).native_mU
        self.assertEqual(sum(market.external_issues['n0-tech'][i].shares for i in range(len(market.external_issues['n0-tech']))),
                         1000000 - state.initial.companies['n0-tech'].shares['n0'])
        if raised:
            work = state.work
            on_lab = WorkOrder('w1', '9', 'n0', 'n0-h0', 'upgrade_facility', lab, 10, {})
            elsewhere = WorkOrder('w2', '9', 'n0', 'n0-h0', 'upgrade_facility', farm, 10, {})
            work._assign_escrow(on_lab)
            work._assign_escrow(elsewhere)
            self.assertEqual(work.payer(on_lab), escrow)
            self.assertEqual(work.payer(elsewhere), state.native('n0', 'treasury'))
        state.audit()


if __name__ == '__main__':
    unittest.main()
