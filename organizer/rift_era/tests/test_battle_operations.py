import unittest
from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.world import generate_candidate
from organizer.rift_era.engine.ledger import Account


class BattleOperationsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog, cls.world = Catalog(), generate_candidate(1000)

    def game(self):
        return opening_economy(self.catalog,initialize(self.catalog,self.world),total_ticks=10,commerce=True,debug=False)

    def colocate(self,state,owner):
        formation=next(f for f in state.formations.rows.values() if f.owner==owner)
        formation.hub='n0-h0'
        formation.region=state.initial.map.hubs['n0-h0'].region
        for uid in formation.unit_ids:
            state.initial.units[uid].region=formation.region
        return formation

    def test_peace_neutral_units_do_not_fire_or_consume_ammunition(self):
        game=self.game(); state=game.state
        self.colocate(state,'n1')
        ammo=Account('n0','n0-h0','available','ammunition')
        before=state.initial.inventory.balance(ammo)
        game.step()
        self.assertFalse(state.battles.last_result.shots)
        self.assertFalse(state.battles.loss_cycles[0])
        self.assertEqual(state.initial.inventory.balance(ammo),before)
        state.audit()

    def test_actual_war_uses_local_stock_and_spares_third_party(self):
        game=self.game(); state=game.state
        enemy=self.colocate(state,'n1')
        neutral=self.colocate(state,'n2')
        neutral_hp={u:state.initial.units[u].hp_mU for u in neutral.unit_ids}
        state.initial.diplomacy.phase1(0,declarations=(('n0','n1'),))
        before_people=sum(u.crew_mU for u in state.initial.units.values())
        ammo=Account('n0','n0-h0','available','ammunition')
        before_ammo=state.initial.inventory.balance(ammo)
        remote=Account('n1','n1-h0','available','ammunition')
        before_remote=state.initial.inventory.balance(remote)
        game.step()
        result=state.battles.last_result
        self.assertTrue(result.shots)
        self.assertLess(sum(u.crew_mU for u in state.initial.units.values()),before_people)
        self.assertLess(state.initial.inventory.balance(ammo),before_ammo)
        self.assertEqual(state.initial.inventory.balance(remote),before_remote)
        self.assertEqual({u:state.initial.units[u].hp_mU for u in neutral.unit_ids},neutral_hp)
        self.assertTrue(all(s.target not in neutral.unit_ids and s.source not in neutral.unit_ids for s in result.shots))
        self.assertGreater(state.battles.loss_cycles[0]['n0'],0)
        self.assertGreater(state.battles.loss_cycles[0]['n1'],0) # zero-ammo melee still costs an action
        self.assertFalse(state.battles.locks)
        state.audit()

    def test_dead_units_destroy_real_equipment_and_do_not_return_civilian_crew(self):
        game=self.game(); state=game.state
        enemy=self.colocate(state,'n1')
        for uid in enemy.unit_ids:
            state.initial.units[uid].hp_mU=1
        dead_ids=enemy.unit_ids
        population=state.initial.populations['n1-h0'].total_mU
        state.initial.diplomacy.phase1(0,declarations=(('n0','n1'),))
        game.step()
        self.assertTrue(all(uid not in state.initial.units for uid in dead_ids))
        self.assertEqual(state.initial.populations['n1-h0'].total_mU,population)
        self.assertNotIn(enemy.id,state.formations.rows)
        for uid in dead_ids:
            self.assertFalse(any(a.location==uid for a in state.initial.inventory.balances()))
        state.audit()
