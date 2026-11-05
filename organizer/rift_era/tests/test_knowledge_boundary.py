import unittest
from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.clock import Phase
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.kernel import System, TickContext, order_systems
from organizer.rift_era.engine.ledger import Account
from organizer.rift_era.engine.materials import Origin
from organizer.rift_era.engine.research_commands import ResearchCommands
from organizer.rift_era.engine.world import generate_candidate


class KnowledgeBoundaryTests(unittest.TestCase):
    def test_real_p6_validation_cannot_retroactively_change_p7_technology(self):
        catalog=Catalog();world=generate_candidate(1000)
        game=opening_economy(catalog,initialize(catalog,world),total_ticks=40,commerce=True,debug=False)
        state=game.state;research=state.research['n0']
        lab=next(lid for lid,line in state.initial.lines.items() if line.owner=='n0' and line.recipe.id=='P075')
        research.set_focus(lab,'R01')
        project=research.projects['R01'];project.theory_mU=project.target_mU;project.theory_complete=True
        tier=next(t for t in catalog.tables['technology']['research_tiers'] if t['tier']==1)
        source=Account('n0','n0-h0','available',tier['validation_good'])
        state.initial.inventory.transform({}, {source:10000},reason='test_initial_validation_supplies',
            origins={source:Origin('n0','n0-h0')})
        ResearchCommands(state).prepare('n0',{'id':'1','kind':'research','op':'validate_project',
            'line_id':lab,'tech_id':'R01'},TickContext(1000,0,game.clock,{})).commit()
        job=next(j for j in state.validation_jobs.values() if j.tech_id=='R01')
        project.validation_progress_mU=(job.required_ticks-1)*1000
        observations=[]
        def observe_p7(state,ctx):
            if project.verified:
                observations.append(('R01' in research.known,'R01' in state.pending_knowledge['n0']))
        game.systems=order_systems(game.systems+(System('test_p7_probe',Phase.SETTLEMENT,observe_p7,
                                                       after=('consumption_receipts',)),))
        for _ in range(40):
            game.step()
            if project.verified:break
        self.assertTrue(project.verified)
        self.assertEqual(observations,[(False,True)])
        self.assertIn('R01',research.known)
        self.assertIn('R01',state.initial.known['n0'])
        self.assertFalse(state.pending_knowledge['n0'])
        state.audit()
