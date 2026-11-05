import unittest
from types import SimpleNamespace as NS
from organizer.rift_era.tests.test_contracts import LicenseContractTests
from organizer.rift_era.engine.contracts import ContractCommands
from organizer.rift_era.engine.diplomacy import Relation
from organizer.rift_era.engine.determinism import fingerprint
from organizer.rift_era.engine.errors import RuleViolation
from organizer.rift_era.engine.numeric import Q


class PeaceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        LicenseContractTests.setUpClass();cls.fixture_case=LicenseContractTests()

    def game(self):return self.fixture_case.fixture()

    def offer(self,mode='armistice',regions=None):
        return {'id':'1','kind':'diplomacy','op':'propose_contract','counterparty':'n1','clause_type':6,
                'duration_cycles':10,'params':{'mode':mode,'region_ids':regions or [],'withdraw_cycles':1}}

    def sign(self,state,mode='armistice',regions=None):
        command=ContractCommands(state);ctx=NS(cycle=0,tick=0)
        command.prepare('n0',self.offer(mode,regions),ctx).commit();state.contracts.publish()
        alias=state.contracts.visible('n1')['proposals'][0]['id']
        return {'id':'1','kind':'diplomacy','op':'accept_contract','proposal_id':alias}

    def test_armistice_stops_old_war_and_expires_to_peace(self):
        game=self.game();state=game.state
        state.initial.diplomacy.phase1(0,declarations=(('n0','n1'),))
        accept=self.sign(state)
        game.step({'n1':[accept]})
        self.assertEqual(state.command_streams['n1'].recent[-1].status,'ACCEPTED')
        self.assertEqual(state.initial.diplomacy.relation('n0','n1'),Relation.TRUCE)
        self.assertEqual(state.initial.diplomacy.pairs['n0','n1'].expiry_tick,100)
        state.initial.diplomacy.phase1(100)
        self.assertEqual(state.initial.diplomacy.relation('n0','n1'),Relation.PEACE)
        state.audit()

    def test_same_batch_redeclaration_after_armistice_counts_as_truce_break(self):
        game=self.game();state=game.state
        state.initial.diplomacy.phase1(0,declarations=(('n0','n1'),))
        accept=self.sign(state);before=state.credit['n0']
        game.step({'n0':[{'id':'1','kind':'diplomacy','op':'declare_war','target_nation':'n1'}],'n1':[accept]})
        self.assertEqual(state.initial.diplomacy.relation('n0','n1'),Relation.WAR)
        self.assertEqual(state.credit['n0'],before-Q(35,100))
        self.assertTrue(state.relations.last_events[0]['truce_broken'])
        state.audit()

    def test_cession_changes_sovereignty_without_changing_third_party_assets(self):
        game=self.game();state=game.state;region=state.initial.map.hubs['n0-h0'].region
        rights=fingerprint(state.authority.assets)
        accept=self.sign(state,'cession',[region]);game.step({'n1':[accept]})
        self.assertEqual(state.authority.regions[region].sovereign,'n1')
        self.assertEqual(state.authority.regions[region].controller,'n0')
        self.assertEqual(rights,fingerprint(state.authority.assets))
        state.audit()

    def test_foreign_sovereignty_cannot_be_offered(self):
        state=self.game().state;before=fingerprint(state.snapshot())
        with self.assertRaises(RuleViolation):
            ContractCommands(state).prepare('n0',self.offer('cession',[state.initial.map.hubs['n2-h0'].region]),NS(cycle=0))
        self.assertEqual(before,fingerprint(state.snapshot()))

    def test_dmz_deadline_causes_one_penalty_and_one_new_war(self):
        game=self.game();state=game.state;region=state.initial.map.hubs['n0-h0'].region
        accept=self.sign(state,'peace',[region]);game.step({'n1':[accept]})
        before=state.credit['n0'];ctx=NS(tick=10,cycle=1,clock=NS(cycle_ticks=10))
        state.relations.settle(ctx)
        self.assertEqual(state.initial.diplomacy.relation('n0','n1'),Relation.WAR)
        self.assertEqual(state.credit['n0'],before-Q(35,100))
        war=state.initial.diplomacy.pairs['n0','n1'].war_id
        state.relations.settle(NS(tick=11,cycle=1,clock=ctx.clock))
        self.assertEqual(state.credit['n0'],before-Q(35,100))
        self.assertEqual(state.initial.diplomacy.pairs['n0','n1'].war_id,war)
