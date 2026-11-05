import unittest
from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.world import generate_candidate
from organizer.rift_era.engine.capital_book import proportional_loss
from organizer.rift_era.engine.numeric import Q
from organizer.rift_era.engine.ledger import Account
from organizer.rift_era.engine.materials import Origin


class MilitaryRepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.catalog,cls.world=Catalog(),generate_candidate(1000)

    def game(self):
        return opening_economy(self.catalog,initialize(self.catalog,self.world),total_ticks=30,commerce=True,debug=False)

    def supplies(self,state,hub,goods):
        accounts={Account('n0',hub,'available',g):q for g,q in goods.items()}
        state.initial.inventory.transform({},accounts,reason='test_initial_repair_supplies',
                                          origins={a:Origin('n0',hub) for a in accounts})

    def damage(self,unit,amount):
        for good,value in proportional_loss(unit.book_nmU,amount,unit.hp_mU).items():unit.book_nmU[good]-=value
        unit.hp_mU-=amount

    def test_hub_repair_restores_at_most_quarter_hp_without_resurrecting_crew(self):
        game=self.game();state=game.state
        formation=next(f for f in state.formations.rows.values() if f.owner=='n0')
        unit=state.initial.units[formation.unit_ids[0]]
        self.supplies(state,'n0-h0',{'medical_supplies':10000})
        maximum=unit.hp_mU
        self.damage(unit,maximum//2)
        unit.crew_mU-=500;formation.crew_skills[unit.id]['general_worker']-=500
        crew=unit.crew_mU
        for _ in range(10):game.step()
        self.assertEqual(unit.hp_mU,maximum*3//4)
        self.assertEqual(unit.crew_mU,crew)
        self.assertGreater(state.consumption.sink_debt['n0','basic_parts'],0)
        self.assertFalse(state.military_repairs.requests)
        state.audit()

    def test_fractional_inputs_are_prepaid_once_and_reused_next_cycle(self):
        game=self.game();state=game.state
        unit=next(u for u in state.initial.units.values() if u.owner=='n0' and u.unit_type=='infantry')
        self.supplies(state,'n0-h0',{'medical_supplies':10000})
        maximum=unit.hp_mU
        self.damage(unit,1)
        for _ in range(10):game.step()
        self.assertEqual(unit.hp_mU,maximum)
        self.assertEqual(state.military_repairs.credits[unit.id,'basic_parts'],Q(99,100))
        self.assertEqual(state.military_repairs.credits[unit.id,'medical_supplies'],Q(499,500))
        self.damage(unit,1)
        for _ in range(10):game.step()
        self.assertEqual(state.military_repairs.credits[unit.id,'basic_parts'],Q(98,100))
        self.assertEqual(state.military_repairs.credits[unit.id,'medical_supplies'],Q(498,500))
        state.audit()

    def test_dead_patient_releases_reserved_parts_and_is_not_recreated(self):
        game=self.game();state=game.state
        for _ in range(9):game.step()
        victim=next(f for f in state.formations.rows.values() if f.owner=='n0')
        enemy=next(f for f in state.formations.rows.values() if f.owner=='n1')
        victim.hub='n0-h1';victim.region=state.initial.map.hubs[victim.hub].region
        self.supplies(state,victim.hub,{'basic_parts':1000,'medical_supplies':1000})
        for uid in victim.unit_ids:state.initial.units[uid].region=victim.region
        for uid in victim.unit_ids:self.damage(state.initial.units[uid],state.initial.units[uid].hp_mU-1)
        dead=victim.unit_ids
        enemy.hub=victim.hub;enemy.region=victim.region
        for uid in enemy.unit_ids:state.initial.units[uid].region=victim.region
        state.initial.diplomacy.phase1(9,declarations=(('n0','n1'),))
        game.step()
        self.assertTrue(all(uid not in state.initial.units for uid in dead))
        self.assertFalse(any(a.bucket.startswith('locked_unit_repair:') for a in state.initial.inventory.balances()))
        self.assertFalse(state.military_repairs.requests)
        state.audit()

    def test_missing_medical_supplies_prevents_free_repair(self):
        game=self.game();state=game.state
        unit=next(u for u in state.initial.units.values() if u.owner=='n0')
        self.damage(unit,1)
        before=unit.hp_mU
        for _ in range(10):game.step()
        self.assertEqual(unit.hp_mU,before)
        self.assertFalse(state.military_repairs.credits)
        state.audit()
