import unittest
from types import SimpleNamespace as NS
from organizer.rift_era.tests import test_contracts as license_tests
from organizer.rift_era.engine.contracts import ContractCommands
from organizer.rift_era.engine.numeric import Q
from organizer.rift_era.engine.ledger import Account
from organizer.rift_era.engine.materials import Origin
from organizer.rift_era.engine.provenance import Use


class DefensePactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):license_tests.LicenseContractTests.setUpClass()

    def fixture(self):
        state=license_tests.LicenseContractTests().fixture().state;handler=ContractCommands(state);ctx=NS(tick=0,cycle=0)
        handler.prepare('n0',{'id':'1','kind':'diplomacy','op':'propose_contract','counterparty':'n1','clause_type':7,'duration_cycles':10,
            'params':{'response_cycles':2,'min_aid_ru_mU':50000}},ctx).commit();state.contracts.publish()
        alias=state.contracts.visible('n1')['proposals'][0]['id']
        handler.prepare('n1',{'id':'1','kind':'diplomacy','op':'accept_contract','proposal_id':alias},ctx).commit()
        state.initial.diplomacy.phase1(1,declarations=(('n2','n1'),))
        state.relations.last_events=[{'actor':'n2','target':'n1','war_id':1}]
        state.contracts.defense_advance(NS(tick=1,cycle=0))
        return state,handler

    def test_failure_has_one_penalty_and_does_not_automatically_declare_war(self):
        from organizer.rift_era.engine.diplomacy import Relation
        state,_=self.fixture();before=state.credit['n0']
        self.assertEqual(state.initial.diplomacy.relation('n0','n2'),Relation.PEACE)
        state.relations.last_events=[]
        state.contracts.defense_advance(NS(tick=21,cycle=2))
        self.assertEqual(state.credit['n0'],before-Q(25,100))
        state.contracts.defense_advance(NS(tick=22,cycle=2))
        self.assertEqual(state.credit['n0'],before-Q(25,100))
        self.assertEqual(state.contracts.defense_obligations[0].status,'DEFAULTED')

    def test_actual_declaration_fulfills_obligation(self):
        state,_=self.fixture();state.initial.diplomacy.phase1(2,declarations=(('n0','n2'),))
        state.relations.last_events=[];state.contracts.defense_advance(NS(tick=2,cycle=0))
        self.assertEqual(state.contracts.defense_obligations[0].status,'FULFILLED')

    def test_real_gift_only_counts_once_after_recipient_consumption(self):
        state,handler=self.fixture();stock=state.initial.inventory;stock.materials.begin_tick(2)
        source=Account('n0','n1-h0','available','fuel');target=Account('n1','n1-h0','available','fuel')
        stock.transform({}, {source:100000},reason='explicit_aid_fixture',origins={source:Origin('n0','n1-h0')})
        handler.prepare('n0',{'id':'2','kind':'diplomacy','op':'military_aid','recipient':'n1','hub_id':'n1-h0','good':'fuel','qty_mU':100000},NS(tick=2,cycle=0)).commit()
        state.relations.last_events=[];state.contracts.defense_advance(NS(tick=2,cycle=0))
        self.assertEqual(state.contracts.defense_obligations[0].aid_value,0)
        stock.transform({target:100000},{},reason='military_fire',use=Use.ACTION_LOSS)
        state.contracts.defense_advance(NS(tick=2,cycle=0))
        value=state.contracts.defense_obligations[0].aid_value
        self.assertGreaterEqual(value,50)
        self.assertEqual(state.contracts.defense_obligations[0].status,'FULFILLED')
        state.contracts.defense_advance(NS(tick=2,cycle=0))
        self.assertEqual(state.contracts.defense_obligations[0].aid_value,value)
