import unittest
from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.economy import opening_economy, Economy
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.world import generate_candidate


class LifeLaborPriorityTests(unittest.TestCase):
    def test_ordinary_jobs_do_not_dilute_life_reservations_under_labor_scarcity(self):
        catalog=Catalog();world=generate_candidate(1000)
        game=opening_economy(catalog,initialize(catalog,world),total_ticks=10,commerce=True,debug=False)
        state=game.state
        # Deliberately scarce component genesis; the normal economic fixture
        # otherwise has enough workforce to hide wrong priority arbitration.
        state.initial.populations['n0-h0'].skills_mU={'general_worker':1000,'skilled_worker':0,'engineer':0}
        game.step()
        economy=Economy(state)
        core=sum(sum(workers.values()) for entity,workers in state.frame.labor.items()
                 if entity in state.frame.core_entities and economy._hub(entity)=='n0-h0')
        ordinary=sum(sum(workers.values()) for entity,workers in state.frame.labor.items()
                     if entity not in state.frame.core_entities and economy._hub(entity)=='n0-h0')
        self.assertEqual(core,65) # 1 person * .65 participation / 10 ticks.
        self.assertEqual(ordinary,0)
        state.audit()
