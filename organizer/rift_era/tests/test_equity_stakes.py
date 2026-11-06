"""04 F04 sovereign fund: buying a foreign facility stake (equity_stake_v1)."""
import unittest
from fractions import Fraction as Q
from types import SimpleNamespace as NS

from organizer.rift_era.tests import test_contracts as license_tests
from organizer.rift_era.engine.contracts import ContractCommands
from organizer.rift_era.engine.economic_ownership import EconomicOwner
from organizer.rift_era.engine.errors import RuleViolation
from organizer.rift_era.engine.ledger import CashAccount


class EquityStakeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):license_tests.LicenseContractTests.setUpClass()

    def setUp(self):
        self.state = state = license_tests.LicenseContractTests().fixture().state
        self.handler, self.ctx = ContractCommands(state), NS(tick=0, cycle=0)
        self.aid = next(a for a, f in sorted(state.initial.facilities.items())
                        if f.owner == 'n1' and f.line_ids and a in state.authority.assets
                        and not state.initial.assets[a].under_construction)

    def offer(self, share=400, price=1000, seq='1'):
        return self.handler.prepare('n0', {'id': seq, 'kind': 'diplomacy', 'op': 'propose_contract', 'counterparty': 'n1',
            'clause_type': 4, 'duration_cycles': 10,
            'params': {'mode': 'stake', 'asset_id': self.aid, 'share_mU': share, 'price_ru_mU': price}}, self.ctx)

    def accept(self):
        self.state.contracts.publish()
        alias = self.state.contracts.visible('n1')['proposals'][0]['id']
        self.handler.prepare('n1', {'id': '2', 'kind': 'diplomacy', 'op': 'accept_contract', 'proposal_id': alias}, self.ctx).commit()

    def test_needs_f04_and_caps_one_holder_at_49_percent(self):
        self.state.research['n0'].known.discard('F04')
        with self.assertRaises(RuleViolation) as e:self.offer()
        self.assertEqual(e.exception.code, 'ERR_TECH_LOCKED')
        self.state.research['n0'].known.add('F04')
        with self.assertRaises(RuleViolation) as e:self.offer(share=491)
        self.assertEqual(e.exception.code, 'ERR_SCHEMA')

    def test_accepted_stake_pays_seller_and_splits_output_titles(self):
        state = self.state;state.research['n0'].known.add('F04')
        cash = state.initial.cash
        buyer, seller = CashAccount('n0', 'reserves', 'RU'), CashAccount('n1', 'reserves', 'RU')
        cash.transfer(CashAccount('world', 'pool', 'RU'), buyer, 5000)
        b0, s0 = cash.balance(buyer).native_mU, cash.balance(seller).native_mU
        self.offer().commit();self.accept()
        self.assertEqual((cash.balance(buyer).native_mU, cash.balance(seller).native_mU), (b0 - 1000, s0 + 1000))
        self.assertEqual(state.authority.assets[self.aid].owner_shares, {'n1': 600, 'n0': 400})
        lid = state.initial.facilities[self.aid].line_ids[0]
        shares = state.facility_ownership.shares(lid)
        self.assertEqual(shares, {EconomicOwner('n1', 'treasury'): Q(3, 5), EconomicOwner('n0', 'treasury'): Q(2, 5)})
        with self.assertRaises(RuleViolation) as e:self.offer(share=100, seq='3')   # 400 + 100 > 490
        self.assertEqual(e.exception.code, 'ERR_CAPACITY_EXCEEDED')
        # War freezes the holder's title back to the operator.
        state.initial.diplomacy.phase1(1, declarations=(('n0', 'n1'),))
        self.assertEqual(state.facility_ownership.shares(lid), {EconomicOwner('n1', 'treasury'): Q(1)})


if __name__ == '__main__':
    unittest.main()
