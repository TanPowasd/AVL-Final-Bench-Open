import unittest
from types import SimpleNamespace as NS
from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.clock import ClockConfig
from organizer.rift_era.engine.determinism import fingerprint
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.world import generate_candidate
from organizer.rift_era.engine.military_training import ReinforcementCommands
from organizer.rift_era.engine.errors import RuleViolation


class MilitaryTrainingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog,cls.world=Catalog(),generate_candidate(1000)

    def fixture(self):
        game=opening_economy(self.catalog,initialize(self.catalog,self.world),total_ticks=30,commerce=True,debug=False)
        state=game.state
        formation=next(f for f in state.formations.rows.values() if f.owner=='n0')
        unit=state.initial.units[formation.unit_ids[0]]
        unit.crew_mU-=500
        formation.crew_skills[unit.id]['general_worker']-=500
        return game,formation,unit

    def order(self,formation):
        return {'id':'1','kind':'military','op':'reinforce','formation_id':formation.id,'unit_index':0,'crew_mU':500}

    def test_training_consumes_no_extra_people_and_cannot_join_before_twenty_ticks(self):
        game,formation,unit=self.fixture();state=game.state
        before=state.initial.populations['n0-h0'].total_mU
        crew=unit.crew_mU
        game.step({'n0':[self.order(formation)]})
        self.assertEqual(state.command_streams['n0'].recent[-1].status,'ACCEPTED')
        self.assertEqual(unit.crew_mU,crew)
        self.assertEqual(state.initial.populations['n0-h0'].total_mU,before)
        job,=state.recruitment.jobs.values()
        for _ in range(19):
            game.step()
        self.assertEqual(unit.crew_mU,crew)
        survivors=sum(state.initial.populations[job.hub].training_mU[job.pool_key(s)] for s in job.original_skills)
        game.step()
        self.assertEqual(unit.crew_mU,crew+survivors)
        self.assertFalse(state.recruitment.jobs)
        self.assertFalse(state.initial.populations[job.hub].training_mU)
        state.audit()

    def test_duplicate_target_rejects_atomically_and_cancel_returns_only_survivors(self):
        game,formation,unit=self.fixture();state=game.state
        handler=ReinforcementCommands(state);ctx=NS(tick=0,clock=ClockConfig())
        handler.prepare('n0',self.order(formation),ctx).commit()
        job,=state.recruitment.jobs.values()
        before=fingerprint(state.snapshot())
        with self.assertRaises(RuleViolation):
            handler.prepare('n0',dict(self.order(formation),id='2'),ctx)
        self.assertEqual(before,fingerprint(state.snapshot()))
        pop=state.initial.populations[job.hub]
        # A mortality fixture removes a trainee, never creates replacement crew.
        pop.training_mU[job.pool_key('general_worker')]-=1
        old=pop.skills_mU['general_worker']
        state.recruitment.prepare_cancel('n0','1').commit()
        self.assertEqual(pop.skills_mU['general_worker'],old+499)
        self.assertFalse(pop.training_mU)
        state.audit()

    def test_trained_residents_cannot_teleport_to_a_departed_formation(self):
        game,formation,unit=self.fixture();state=game.state
        ctx=NS(tick=0,clock=ClockConfig())
        ReinforcementCommands(state).prepare('n0',self.order(formation),ctx).commit()
        crew=unit.crew_mU
        formation.hub='n0-h1'
        state.recruitment.advance(NS(tick=20))
        self.assertEqual(unit.crew_mU,crew)
        self.assertTrue(state.recruitment.jobs)
        formation.hub='n0-h0'
        state.recruitment.advance(NS(tick=21))
        self.assertEqual(unit.crew_mU,crew+500)
        state.audit()
