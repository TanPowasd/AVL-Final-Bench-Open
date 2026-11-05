import unittest
from copy import deepcopy
from types import SimpleNamespace as NS
from organizer.rift_era.engine.intelligence_sharing import IntelligenceSharing
from organizer.rift_era.engine.contracts import IntelligenceTreaty
from organizer.rift_era.engine.diplomacy import Diplomacy
from organizer.rift_era.engine.numeric import Q


class IntelligenceSharingTests(unittest.TestCase):
    def fixture(self):
        row={'id':'contact-1','source':'visual','observed_tick':4,'last_region':'r','quality_mU':600,
             'estimated_low_mU':90,'estimated_high_mU':110,'estimated_center_mU':100,'domain':'Land','owner':'n3','version':1}
        owners=('n0','n1','n2')
        state=NS(initial=NS(known=dict.fromkeys(owners),diplomacy=Diplomacy(frozenset((*owners,'n3')))),
                 contracts=NS(intelligence=[IntelligenceTreaty(('n0','n1'),('r',),200,0,10),
                                            IntelligenceTreaty(('n1','n2'),('r',),200,0,10)]),
                 catalog=NS(runtime={'limits':{'visible_tracks_per_observer':8192}}),
                 vision=NS(views={p:{'tracks':[]} for p in owners},observers={p:NS(_tracks={}) for p in owners},speeds={'Land':Q(1)}))
        state.vision.views['n0']['tracks']=[dict(row,currently_visible=True)]
        state.vision.observers['n0']._tracks['contact-1']=row
        return state,IntelligenceSharing(state)

    def ctx(self,tick=9):return NS(tick=tick,cycle=tick//10,clock=NS(is_cycle_end=lambda t:t%10==9,cycle_ticks=10))

    def test_shared_track_preserves_age_does_not_recursively_reach_ally_of_ally(self):
        state,sharing=self.fixture();sharing.advance(self.ctx())
        self.assertFalse(sharing.visible('n2',10))
        row=sharing.visible('n1',10)[0]
        self.assertEqual(row['observed_tick'],4)
        self.assertEqual(row['quality_mU'],375)
        self.assertEqual(row['shared_by'],'n0')
        self.assertEqual(row['id'],'intel-1')
        self.assertFalse(row['currently_visible'])
        self.assertNotIn('contact-1',str(row))

    def test_hidden_sender_mutation_cannot_update_frozen_received_track(self):
        state,sharing=self.fixture();sharing.advance(self.ctx())
        before=sharing.visible('n1',10)
        state.vision.observers['n0']._tracks['contact-1']['estimated_center_mU']=999999
        state.vision.views['n0']['tracks']=[]
        self.assertEqual(before,sharing.visible('n1',10))
        sharing.advance(self.ctx(19))
        self.assertLess(sharing.visible('n1',20)[0]['quality_mU'],before[0]['quality_mU'])

    def test_war_pauses_forwarding_and_wrong_region_or_quality_never_leaks(self):
        for condition in ('war','region','quality'):
            with self.subTest(condition=condition):
                state,sharing=self.fixture()
                if condition=='war':state.initial.diplomacy.phase1(0,declarations=(('n0','n1'),))
                elif condition=='region':state.vision.views['n0']['tracks'][0]['last_region']='hidden'
                else:state.vision.views['n0']['tracks'][0]['quality_mU']=100
                sharing.advance(self.ctx())
                self.assertFalse(sharing.visible('n1',10))

    def test_zero_future_time_refresh_and_exactly_one_cycle_forward(self):
        from organizer.rift_era.engine.errors import InvariantViolation
        state,sharing=self.fixture();sharing.advance(self.ctx(8))
        self.assertFalse(sharing.visible('n1',9))
        sharing.advance(self.ctx())
        with self.assertRaises(InvariantViolation):sharing.advance(self.ctx())

class IntelligenceTreatyCommandTests(unittest.TestCase):
    def test_private_previous_window_signatures_create_only_fixed_duration_sharing(self):
        from organizer.rift_era.tests.test_contracts import LicenseContractTests
        from organizer.rift_era.engine.contracts import ContractCommands
        from organizer.rift_era.engine.errors import RuleViolation
        LicenseContractTests.setUpClass();state=LicenseContractTests().fixture().state
        ctx=NS(cycle=0,tick=0);handler=ContractCommands(state)
        region=state.initial.map.hubs['n0-h0'].region
        order={'id':'1','kind':'diplomacy','op':'propose_contract','counterparty':'n1','clause_type':8,'duration_cycles':10,
               'params':{'region_ids':[region],'min_quality_mU':250}}
        handler.prepare('n0',order,ctx).commit()
        with self.assertRaises(RuleViolation):handler.prepare('n1',{'id':'1','kind':'diplomacy','op':'accept_contract','proposal_id':'proposal-1'},ctx)
        state.contracts.publish()
        self.assertFalse(state.contracts.visible('n2')['proposals'])
        proposal=state.contracts.visible('n1')['proposals'][0]
        self.assertEqual(proposal['clause_type'],8)
        handler.prepare('n1',{'id':'1','kind':'diplomacy','op':'accept_contract','proposal_id':proposal['id']},ctx).commit()
        treaty=state.contracts.intelligence[0]
        self.assertEqual(treaty.parties,('n0','n1'))
        self.assertEqual(treaty.end_cycle,10)
        self.assertFalse(state.contracts.visible('n2')['intel_sharing'])
        state.contracts.begin_tick(NS(cycle=10,tick=100))
        self.assertFalse(state.contracts.intelligence)
        state.audit()

class SharedCombatSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from organizer.rift_era.engine.catalog import Catalog
        cls.catalog=Catalog()

    def test_stale_area_shot_spends_ammo_but_does_not_follow_hidden_target(self):
        from organizer.rift_era.engine.combat import Unit,TargetContact,resolve
        dip=Diplomacy(frozenset(('n0','n1')));dip.phase1(0,declarations=(('n0','n1'),))
        attacker=Unit('a','n0','infantry','old',100000,1000)
        snapshot=Unit('b','n1','infantry','old',100000,1000)
        hidden=Unit('b','n1','infantry','new',100000,1000)
        contacts={'a':(TargetContact('b',Q(3,5),0,'old',snapshot),)}
        result=resolve(self.catalog,dip,{'a':attacker,'b':hidden},contacts,{'a':{'ammunition':1000}},
                       {'old':'plain','new':'mountain'})
        self.assertGreater(result.consumed_mU['a']['ammunition'],0)
        self.assertTrue(result.shots)
        self.assertEqual(result.damage_mU['b'],0)
        self.assertNotIn('new',result.combat_regions)
        hidden.region='old'
        result=resolve(self.catalog,dip,{'a':attacker,'b':hidden},contacts,{'a':{'ammunition':1000}},
                       {'old':'plain','new':'mountain'})
        self.assertGreater(result.damage_mU['b'],0)

    def test_destroyed_hidden_target_still_consumes_old_area_shot_without_new_damage(self):
        from organizer.rift_era.engine.combat import Unit,TargetContact,resolve
        dip=Diplomacy(frozenset(('n0','n1')));dip.phase1(0,declarations=(('n0','n1'),))
        attacker=Unit('a','n0','infantry','old',100000,1000)
        old=Unit('b','n1','infantry','old',100000,1000)
        result=resolve(self.catalog,dip,{'a':attacker},{'a':(TargetContact('b',Q(3,5),0,'old',old),)},
            {'a':{'ammunition':1000}},{'old':'plain'})
        self.assertGreater(result.consumed_mU['a']['ammunition'],0)
        self.assertNotIn('b',result.damage_mU)
