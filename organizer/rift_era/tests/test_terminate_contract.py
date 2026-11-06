"""06 diplomacy/terminate_contract (contract_termination_v1)."""
import unittest
from types import SimpleNamespace as NS

from organizer.rift_era.tests import test_contracts as license_tests
from organizer.rift_era.engine.contracts import ContractCommands
from organizer.rift_era.engine.errors import RuleViolation
from organizer.rift_era.engine.numeric import Q


class TerminateContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        license_tests.LicenseContractTests.setUpClass()

    def pact(self):
        state = license_tests.LicenseContractTests().fixture().state
        handler = ContractCommands(state)
        ctx = NS(tick=0, cycle=0)
        handler.prepare('n0', {'id': '1', 'kind': 'diplomacy', 'op': 'propose_contract', 'counterparty': 'n1',
                               'clause_type': 7, 'duration_cycles': 10,
                               'params': {'response_cycles': 2, 'min_aid_ru_mU': 50000}}, ctx).commit()
        state.contracts.publish()
        alias = state.contracts.visible('n1')['proposals'][0]['id']
        handler.prepare('n1', {'id': '1', 'kind': 'diplomacy', 'op': 'accept_contract', 'proposal_id': alias}, ctx).commit()
        cid = state.contracts.visible('n0')['defense_pacts'][0]['contract_id']
        self.assertEqual(cid, state.contracts.visible('n1')['defense_pacts'][0]['contract_id'])
        return state, handler, cid

    def terminate(self, handler, owner, cid, cycle=1):
        return handler.prepare(owner, {'id': '9', 'kind': 'diplomacy', 'op': 'terminate_contract', 'contract_id': cid},
                               NS(tick=cycle * 10, cycle=cycle))

    def test_unilateral_defence_termination_costs_a_quarter_credit_once(self):
        state, handler, cid = self.pact()
        before = state.credit['n0']
        self.terminate(handler, 'n0', cid).commit()
        self.assertEqual(state.credit['n0'], before - Q(25, 100))
        self.assertFalse(state.contracts.defense)
        self.assertEqual(state.contracts.recent['n1'][-1]['status'], 'TERMINATED')
        self.assertTrue(state.contracts.recent['n1'][-1]['attributed'])
        with self.assertRaises(RuleViolation) as caught:
            self.terminate(handler, 'n0', cid)
        self.assertEqual(caught.exception.code, 'ERR_NO_VISIBLE_TARGET')
        self.assertEqual(state.credit['n0'], before - Q(25, 100))

    def test_third_party_cannot_terminate(self):
        state, handler, cid = self.pact()
        with self.assertRaises(RuleViolation) as caught:
            self.terminate(handler, 'n2', cid)
        self.assertEqual(caught.exception.code, 'ERR_NO_VISIBLE_TARGET')
        self.assertEqual(len(state.contracts.defense), 1)

    def test_defender_at_war_is_exempt_but_aggressor_is_not(self):
        state, handler, cid = self.pact()
        state.initial.diplomacy.phase1(1, declarations=(('n0', 'n1'),))
        before = dict(state.credit)
        self.terminate(handler, 'n1', cid).commit()
        self.assertEqual(state.credit['n1'], before['n1'])
        self.assertFalse(state.contracts.recent['n0'][-1]['attributed'])
        state, handler, cid = self.pact()
        state.initial.diplomacy.phase1(1, declarations=(('n0', 'n1'),))
        before = state.credit['n0']
        self.terminate(handler, 'n0', cid).commit()
        self.assertEqual(state.credit['n0'], before - Q(25, 100))

    def test_loan_and_unknown_ids_are_not_silent_noops(self):
        state, handler, _ = self.pact()
        with self.assertRaises(RuleViolation) as caught:
            self.terminate(handler, 'n0', 'dmz:n0:n1:0')
        self.assertEqual(caught.exception.code, 'ERR_NO_PERMISSION')
        with self.assertRaises(RuleViolation) as caught:
            self.terminate(handler, 'n0', 'nonsense')
        self.assertEqual(caught.exception.code, 'ERR_NO_VISIBLE_TARGET')


if __name__ == '__main__':
    unittest.main()
