import unittest
from types import SimpleNamespace as NS
from organizer.rift_era.engine.authority import Authority
from organizer.rift_era.engine.diplomacy import Diplomacy
from organizer.rift_era.engine.military_governance import MilitaryGovernance
from organizer.rift_era.engine.march import Position


class MilitaryGovernanceTests(unittest.TestCase):
    def state(self):
        world=NS(regions={'r':NS(sovereign='n1')},hubs={})
        dip=Diplomacy(frozenset(('n0','n1','n2')))
        formations={'f':NS(id='f',owner='n0',region='r',domain='Land',unit_ids=('u',),refitting=False,task='garrison')}
        state=NS(initial=NS(diplomacy=dip,units={'u':NS(hp_mU=100,crew_mU=100)}),
                 authority=Authority(world,{},dip.actors), formations=NS(rows=formations),
                 movement=NS(position=lambda f:Position('hub'),planned=lambda fid:False,retreating={}))
        return state

    def advance(self,state,ticks=10):
        gov=MilitaryGovernance(state)
        for tick in range(ticks):gov.advance(NS(tick=tick))
        return gov

    def test_neutral_stationary_force_cannot_occupy(self):
        state=self.state();self.advance(state)
        self.assertEqual(state.authority.regions['r'].controller,'n1')
        self.assertEqual(state.authority.regions['r'].garrison_ticks,0)

    def test_ten_surviving_uncontested_ticks_change_only_control(self):
        state=self.state();state.initial.diplomacy.phase1(0,declarations=(('n0','n1'),))
        gov=self.advance(state)
        self.assertEqual(state.authority.regions['r'].controller,'n0')
        self.assertEqual(state.authority.regions['r'].sovereign,'n1')
        self.assertEqual(state.authority.regions['r'].support_mU,280)
        self.assertEqual(gov.events,[('r','n1','n0')])
        state.authority.audit()

    def test_air_sea_moving_refitting_and_dead_units_cannot_hold_land(self):
        for condition in ('Air','Sea-Surface','Sea-Sub','moving','refitting','dead'):
            with self.subTest(condition=condition):
                state=self.state();state.initial.diplomacy.phase1(0,declarations=(('n0','n1'),))
                formation=state.formations.rows['f']
                if condition in ('Air','Sea-Surface','Sea-Sub'):formation.domain=condition
                elif condition=='moving':state.movement.planned=lambda fid:True
                elif condition=='refitting':formation.refitting=True
                else:state.initial.units['u'].hp_mU=0
                self.advance(state)
                self.assertEqual(state.authority.regions['r'].controller,'n1')

    def test_live_hostile_defender_resets_claim_without_neutral_defender_veto(self):
        state=self.state();state.initial.diplomacy.phase1(0,declarations=(('n0','n1'),))
        state.initial.units['v']=NS(hp_mU=100,crew_mU=100)
        state.formations.rows['g']=NS(id='g',owner='n1',region='r',domain='Land',unit_ids=('v',),refitting=False,task='garrison')
        self.advance(state)
        self.assertEqual(state.authority.regions['r'].garrison_ticks,0)
        self.assertEqual(state.authority.regions['r'].controller,'n1')
