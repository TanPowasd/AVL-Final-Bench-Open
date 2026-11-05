import json
import unittest
from types import SimpleNamespace as NS

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.determinism import fingerprint
from organizer.rift_era.engine.diplomacy import Relation
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.military_sensors import MilitarySensors, Signal, Source
from organizer.rift_era.engine.numeric import Q
from organizer.rift_era.engine.world import generate_candidate, SIZE


class SensorRulesTests(unittest.TestCase):
    def setUp(self):
        terrain = bytearray(SIZE * SIZE)
        world = NS(regions={'a': NS(adjacent=('b',)), 'b': NS(adjacent=('a',))}, terrain_cells=terrain)
        self.sensors = MilitarySensors(NS(initial=NS(map=world, diplomacy=NS(relation=lambda a,b: Relation.WAR))))

    def quality(self, domain, sources, *, emitting=False, jammers=frozenset()):
        signal = Signal('hidden', 'n1', domain, 'a', 1, 50000, emitting)
        return self.sensors.quality('n0', signal, ({'n0': sources}, jammers))[0]

    def test_submarine_requires_sonar_or_active_communication(self):
        sources = [Source('n0','visual','a',0,0,Q(3,5)), Source('n0','radar','a',0,3,Q(4,5))]
        self.assertEqual(self.quality('Sea-Sub', sources), 0)
        sources.append(Source('n0','sonar','a',0,0,Q(1,5)))
        self.assertEqual(self.quality('Sea-Sub', sources), Q(1,5))
        listener = [Source('n0','listening','a',0,6,Q(55,100))]
        self.assertEqual(self.quality('Sea-Sub', listener), 0)
        self.assertEqual(self.quality('Sea-Sub', listener, emitting=True), Q(55,100))
        self.assertEqual(self.quality('Sea-Surface', [sources[-1]]), 0)

    def test_source_fusion_obstruction_and_hostile_jamming(self):
        visual = Source('n0','visual','a',0,0,Q(3,5))
        radar = Source('n0','radar','a',0,3,Q(4,5))
        self.assertEqual(self.quality('Air',[visual,visual,radar]), Q(92,100))
        self.assertEqual(self.quality('Sea-Surface',[visual]), Q(3,5))
        self.sensors.s.initial.map.terrain_cells[1] = 3
        self.assertEqual(self.quality('Land',[visual]), Q(42,100))
        self.assertEqual(self.quality('Land',[visual],jammers={('n1','a')}), Q(252,1000))
        self.assertEqual(self.quality('Land',[visual],jammers={('n0','a')}), Q(42,100))


class MilitaryVisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog, cls.world = Catalog(), generate_candidate(1000)

    def fixture(self):
        state = opening_economy(self.catalog, initialize(self.catalog,self.world), total_ticks=10, debug=False).state
        target = next(f for f in state.formations.rows.values() if f.owner=='n1')
        target.hub='n0-h0';target.region=self.world.hubs[target.hub].region
        state.vision._observe(1,0)
        alias=next(row['id'] for row in state.vision.visible('n0')['tracks'] if row['owner']=='n1' and row['currently_visible'])
        return state,target,alias

    def test_snapshot_reads_are_pure_and_hidden_changes_do_not_rewrite_lost_track(self):
        state,target,alias=self.fixture()
        target.hub='n1-h0';target.region=self.world.hubs[target.hub].region
        state.vision._observe(2,0)
        row=next(r for r in state.vision.visible('n0')['tracks'] if r['id']==alias)
        self.assertFalse(row['currently_visible'])
        before=fingerprint(state.vision.snapshot())
        view=state.vision.visible('n0')
        for _ in range(100):
            copy=state.vision.visible('n0');copy['tracks'].clear()
        self.assertEqual(before,fingerprint(state.vision.snapshot()))
        for uid in target.unit_ids:state.initial.units[uid].hp_mU=1
        del state.formations.rows[target.id]
        state.vision._observe(2,0)
        self.assertEqual(view,state.vision.visible('n0'))
        self.assertNotIn(target.id,json.dumps(view))
        state.vision._observe(3,0)
        aged=next(r for r in state.vision.visible('n0')['tracks'] if r['id']==alias)
        self.assertLess(aged['quality_mU'],row['quality_mU'])
        self.assertGreaterEqual(aged['estimated_high_mU'],row['estimated_high_mU'])
        self.assertFalse(state.vision.visible('n0')['deleted_ids'])

    def test_only_visible_death_emits_deletion_and_reacquisition_uses_new_alias(self):
        state,target,alias=self.fixture()
        state.vision.record_deaths((target,))
        del state.formations.rows[target.id]
        state.vision._observe(2,0)
        self.assertIn(alias,state.vision.visible('n0')['deleted_ids'])
        self.assertNotIn(alias,[r['id'] for r in state.vision.visible('n0')['tracks']])
        state.formations.rows[target.id]=target
        state.vision._observe(3,0)
        new_alias=next(r['id'] for r in state.vision.visible('n0')['tracks'] if r['owner']=='n1' and r['currently_visible'])
        self.assertNotEqual(alias,new_alias)

    def test_uninstalled_sensor_and_disabled_radar_give_no_extra_sources(self):
        state,target,_=self.fixture()
        formation=next(f for f in state.formations.rows.values() if f.owner=='n0')
        def kinds():return {s.kind for s in state.sensors.frame()[0]['n0']}
        state.research['n0'].known.update({'G05','G01'})
        self.assertNotIn('radar',kinds())
        # Installed-state fixture; production installation is covered by refit tests.
        formation.slot_a='radar';formation.emitter_active=True
        self.assertIn('radar',kinds())
        formation.emitter_active=False
        self.assertNotIn('radar',kinds())
        formation.slot_a='sensor';formation.refitting=True
        self.assertNotIn('sonar',kinds())
        formation.refitting=False
        self.assertIn('sonar',kinds())
