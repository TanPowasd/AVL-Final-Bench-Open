import json
import unittest
from dataclasses import replace

from organizer.rift_era.tests import test_orbital_operations as fixtures
from organizer.rift_era.engine.determinism import fingerprint
from organizer.rift_era.engine.errors import RuleViolation
from organizer.rift_era.engine.numeric import Q
from organizer.rift_era.engine.orbital_operations import OrbitalCommands


class OrbitalObservationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.OrbitalTests.setUpClass()
        cls.helper = fixtures.OrbitalTests()

    def fixture(self):
        state = self.helper.fixture(('n0', 'n1', 'n2')).state
        OrbitalCommands(state).prepare('n1', self.helper.order('n1'), self.helper.ctx(0)).commit()
        state.orbit.reserve(self.helper.ctx(0))
        for tick in range(20):
            state.orbit.advance(self.helper.ctx(tick))
        satellite, = state.orbit.satellites.values()
        return state, satellite

    def control(self, state, owner):
        return next(s for s in state.systems.instances.values() if s.owner == owner)

    def test_technology_and_actual_online_ground_control_both_required(self):
        state, _ = self.fixture()
        observation = state.orbital_observation
        state.research['n0'].known.discard('N06')
        observation.publish(20)
        self.assertFalse(observation.visible('n0')['coverage'])
        state.research['n0'].known.add('N06')
        control = self.control(state, 'n0')
        control.online = False
        observation.publish(21)
        self.assertFalse(observation.visible('n0')['contacts'])
        control.online = True
        control.kind = 'fusion_sensor'
        observation.publish(22)
        row, = observation.visible('n0')['contacts']
        self.assertEqual(row['quality_mU'], 800)
        self.assertFalse(state.orbit.satellites[next(iter(state.orbit.satellites))].owner == 'n0')
        state.initial.assets[control.host].health_mU = 0
        observation.publish(23)
        self.assertFalse(observation.visible('n0')['coverage'])

    def test_only_whitelisted_foreign_fields_and_pure_frozen_reads(self):
        state, satellite = self.fixture()
        observation = state.orbital_observation
        observation.publish(20)
        view = observation.visible('n0')
        row, = view['contacts']
        self.assertEqual(set(row), {'id', 'owner', 'orbit_plane', 'slot', 'berth', 'quality_mU', 'observed_tick'})
        self.assertNotIn(satellite.id, json.dumps(view))
        satellite.expires_tick += 100
        satellite.commissioned_tick -= 1
        observation.publish(20)
        self.assertEqual(view, observation.visible('n0'))
        before = fingerprint(observation.snapshot())
        for _ in range(100):
            copy = observation.visible('n0')
            copy['contacts'].clear()
        self.assertEqual(before, fingerprint(observation.snapshot()))
        self.assertEqual(view, observation.visible('n0'))

    def test_loss_does_not_report_hidden_death_and_reacquisition_has_fresh_alias(self):
        state, satellite = self.fixture()
        observation = state.orbital_observation
        observation.publish(20)
        first = observation.visible('n0')['contacts'][0]['id']
        self.control(state, 'n0').online = False
        observation.publish(21)
        hidden = observation.visible('n0')
        self.assertIn(first, hidden['lost_contact_ids'])
        del state.orbit.satellites[satellite.id]
        replacement = replace(satellite, id='hidden-new-satellite')
        state.orbit.satellites[replacement.id] = replacement
        observation.publish(21)
        self.assertEqual(hidden, observation.visible('n0'))
        self.control(state, 'n0').online = True
        observation.publish(22)
        current = observation.visible('n0')
        self.assertNotEqual(first, current['contacts'][0]['id'])
        self.assertFalse(current['deleted_ids'])

    def test_aliases_are_observer_local_and_resolution_does_not_probe_hidden_state(self):
        state, satellite = self.fixture()
        observation = state.orbital_observation
        self.control(state, 'n0').online = False
        observation.publish(20)
        # Only n2 sees this replacement; n0 never learns the retired identity.
        del state.orbit.satellites[satellite.id]
        replacement = replace(satellite, id='second-hidden-satellite')
        state.orbit.satellites[replacement.id] = replacement
        observation.publish(21)
        self.control(state, 'n0').online = True
        observation.publish(22)
        alias0 = observation.visible('n0')['contacts'][0]['id']
        alias2 = observation.visible('n2')['contacts'][0]['id']
        self.assertEqual(alias0, 'orbital-contact-1')
        self.assertEqual(alias2, 'orbital-contact-2')
        before = observation.resolve('n0', alias0)
        del state.orbit.satellites[replacement.id]
        self.assertEqual(before, observation.resolve('n0', alias0))
        for target in (replacement.id, 'orbital-contact-999'):
            with self.assertRaises(RuleViolation) as error:
                observation.resolve('n0', target)
            self.assertEqual(error.exception.code, 'ERR_NO_VISIBLE_TARGET')
        with self.assertRaises(RuleViolation):
            observation.resolve('n0', alias0, minimum_quality=Q(9, 10))
