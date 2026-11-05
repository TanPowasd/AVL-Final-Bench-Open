import unittest
from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.world import generate_candidate
from organizer.rift_era.engine.ledger import Account


class MilitaryMovementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.catalog,cls.world=Catalog(),generate_candidate(1000)

    def game(self):
        return opening_economy(self.catalog,initialize(self.catalog,self.world),total_ticks=30,commerce=True,debug=False)

    def deploy(self,game):
        game.step({'n0':[{'id':'1','kind':'military','op':'deploy_formation','hub_id':'n0-h0',
                         'domain':'Land','units':{'motorized':1}}]})
        for _ in range(4):game.step()
        return next(f for f in game.state.formations.rows.values() if f.id.startswith('formation:'))

    def command(self,state,formation):
        return {'id':'2','kind':'military','op':'formation_task','formation_id':formation.id,'task':'move',
                'target_region':state.initial.map.hubs['n0-h1'].region,'posture':'preserve',
                'supply_hub':'n0-h0','retreat_loss_mU':500}

    def test_real_supplies_and_terrain_time_carry_a_formation_to_another_hub(self):
        game=self.game();formation=self.deploy(game);state=game.state
        game.step({'n0':[self.command(state,formation)]})
        self.assertEqual(state.command_streams['n0'].recent[-1].status,'ACCEPTED')
        self.assertIsNone(formation.hub)
        self.assertGreater(state.movement.position(formation).progress,0)
        self.assertLess(state.movement.position(formation).progress,1000)
        for _ in range(12):
            game.step()
            state.audit()
        self.assertEqual(formation.hub,'n0-h1')
        self.assertGreater(sum(c.get('n0',0) for c in state.battles.loss_cycles.values()),0)
        uid=formation.unit_ids[0]
        equipment=state.formations.equipment_account('n0',uid,'vehicle')
        origins=state.initial.inventory.materials.composition(equipment)
        self.assertTrue(all(o.location=='n0-h1' for o in origins))
        self.assertTrue(all(not leg.eligible for o in origins for leg in o.path))
        self.assertEqual(state.initial.inventory.balance(equipment),1000)

    def test_cancel_while_on_a_road_cannot_teleport_back_to_a_hub(self):
        game=self.game();formation=self.deploy(game);state=game.state
        game.step({'n0':[self.command(state,formation)]})
        before=state.movement.position(formation)
        state.movement.cancel('n0','2').commit()
        game.step()
        self.assertEqual(state.movement.position(formation),before)
        self.assertIsNone(formation.hub)
        state.audit()

    def test_future_declaration_in_same_window_is_visible_to_target_validation(self):
        game=self.game();formation=self.deploy(game);state=game.state
        command=self.command(state,formation)
        command['task']='attack'
        command['target_region']=formation.region
        game.step({'n0':[command,{'id':'3','kind':'diplomacy','op':'declare_war','target_nation':'n1'}]})
        receipts=state.command_streams['n0'].recent[-2:]
        self.assertTrue(all(r.status=='ACCEPTED' for r in receipts),receipts)
