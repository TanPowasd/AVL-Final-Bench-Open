import unittest
from types import SimpleNamespace as NS
from organizer.rift_era.tests.test_contracts import LicenseContractTests
from organizer.rift_era.engine.contracts import ContractCommands
from organizer.rift_era.engine.nonland_navigation import base_online
from organizer.rift_era.engine.assets import Asset
from organizer.rift_era.engine.errors import RuleViolation
from organizer.rift_era.engine.determinism import fingerprint


class TransitContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):LicenseContractTests.setUpClass()

    def fixture(self):
        state=LicenseContractTests().fixture().state
        eid=next(e for e,r in state.cargo.corridors.items() if {r.a,r.b}=={'n0-h0','n0-h1'})
        edge=state.cargo.corridors[eid];edge.mode='air'
        for hub in (edge.a,edge.b):
            aid='airport_fixture:'+hub
            state.initial.assets[aid]=Asset(aid,'n0',hub,{})
            state.cargo.terminals[hub,'air']=aid
        state.authority.sync_assets(state.initial.assets)
        return state,eid

    def offer(self,eid):
        return {'id':'1','kind':'diplomacy','op':'propose_contract','counterparty':'n1','clause_type':3,'duration_cycles':10,
                'params':{'corridor_ids':[eid],'max_flow_mU':5000,'toll_cap_ru_mU':100,'allow_basing':True}}

    def sign(self,state,eid):
        command=ContractCommands(state);ctx=NS(tick=0,cycle=0)
        command.prepare('n0',self.offer(eid),ctx).commit();state.contracts.publish()
        proposal=state.contracts.visible('n1')['proposals'][0]
        self.assertEqual(proposal['clause_type'],3)
        command.prepare('n1',{'id':'1','kind':'diplomacy','op':'accept_contract','proposal_id':proposal['id']},ctx).commit()

    def test_signature_grants_only_completed_host_base_without_inventory_transfer(self):
        state,eid=self.fixture();before=fingerprint(state.initial.inventory.snapshot())
        self.assertFalse(base_online(state,'n1','n0-h0','Air'))
        self.sign(state,eid)
        self.assertTrue(base_online(state,'n1','n0-h0','Air'))
        self.assertEqual(state.contracts.transit_right('n1',eid).maximum_flow_mU,5000)
        self.assertIsNone(state.contracts.transit_right('n2',eid))
        self.assertEqual(before,fingerprint(state.initial.inventory.snapshot()))
        self.assertFalse(state.contracts.visible('n2')['transit_rights'])
        state.audit()

    def test_war_expiry_and_destroyed_airport_each_disable_basing(self):
        for reason in ('war','expiry','destroyed'):
            with self.subTest(reason=reason):
                state,eid=self.fixture();self.sign(state,eid)
                if reason=='war':state.initial.diplomacy.phase1(1,declarations=(('n1','n0'),))
                elif reason=='expiry':state.contracts.begin_tick(NS(tick=100,cycle=10))
                else:state.initial.assets[state.cargo.terminals['n0-h0','air']].health_mU=0
                self.assertFalse(base_online(state,'n1','n0-h0','Air'))

    def test_no_grant_on_foreign_endpoints_and_no_mutation_on_failure(self):
        state,eid=self.fixture();before=fingerprint(state.snapshot())
        with self.assertRaises(RuleViolation):ContractCommands(state).prepare('n2',self.offer(eid),NS(cycle=0))
        self.assertEqual(before,fingerprint(state.snapshot()))
