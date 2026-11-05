import unittest
from types import SimpleNamespace as NS
from organizer.rift_era.tests.test_contracts import LicenseContractTests
from organizer.rift_era.engine.contracts import ContractCommands
from organizer.rift_era.engine.numeric import Q


class JointEmbargoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):LicenseContractTests.setUpClass()

    def sign(self):
        state=LicenseContractTests().fixture().state;handler=ContractCommands(state);ctx=NS(tick=0,cycle=0)
        handler.prepare('n0',{'id':'1','kind':'diplomacy','op':'propose_contract','counterparty':'n1','clause_type':9,'duration_cycles':10,
            'params':{'target_nation':'n2','good_ids':['food']}},ctx).commit();state.contracts.publish()
        self.assertFalse(state.contracts.visible('n2')['proposals'])
        alias=state.contracts.visible('n1')['proposals'][0]['id']
        handler.prepare('n1',{'id':'1','kind':'diplomacy','op':'accept_contract','proposal_id':alias},ctx).commit()
        return state

    def test_parties_block_only_signed_goods_and_target_without_inventing_war(self):
        from organizer.rift_era.engine.diplomacy import Relation
        state=self.sign()
        for seller in ('n0','n1'):
            self.assertFalse(state.export_controls.direct_allowed('n2',seller,'food'))
            self.assertTrue(state.export_controls.direct_allowed('n2',seller,'water'))
            self.assertTrue(state.export_controls.direct_allowed('n3',seller,'food'))
        self.assertEqual(state.initial.diplomacy.relation('n0','n2'),Relation.PEACE)
        self.assertTrue(state.export_controls.direct_allowed('n0','n2','food'))
        state.audit()

    def test_persistent_event_is_deduplicated_per_party_target_and_expires(self):
        state=self.sign();state.export_controls.record_policy_events(NS(tick=0,cycle=0))
        self.assertEqual(state.export_controls.action_cycles[0],{'n0':Q(1,1000),'n1':Q(1,1000)})
        state.contracts.begin_tick(NS(tick=100,cycle=10))
        self.assertTrue(state.export_controls.direct_allowed('n2','n0','food'))
