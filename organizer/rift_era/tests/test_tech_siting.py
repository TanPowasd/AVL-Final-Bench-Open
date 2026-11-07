"""04 V03: desalination P106 must sit on a coastal hub."""
import unittest
from unittest import mock

from organizer.rift_era.engine import industry_commands
from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.errors import RuleViolation
from organizer.rift_era.engine.industry_commands import IndustryCommands, coastal_hub
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.world import generate_candidate


class DesalinationSitingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = Catalog()
        cls.game = opening_economy(cls.catalog, initialize(cls.catalog, generate_candidate(1000)), total_ticks=10)

    def test_coastal_hub_follows_ocean_adjacency(self):
        world = self.game.state.initial.map
        for hub_id, hub in world.hubs.items():
            ocean = any(world.regions[r].terrain == 'ocean' for r in world.regions[hub.region].adjacent)
            self.assertEqual(coastal_hub(self.game.state, hub_id), ocean or hub_id in world.coastal_hubs)
        self.assertTrue(any(coastal_hub(self.game.state, h) for h in world.hubs))

    def ctx(self):
        from types import SimpleNamespace
        return SimpleNamespace(tick=1, cycle=0, seed=1000, clock=self.game.clock)

    def test_p106_rejected_inland_and_accepted_on_the_coast(self):
        state = self.game.state
        state.research['n0'].known.add('V03')
        import dataclasses
        facility = dataclasses.replace(state.initial.facilities['n0/P070/0'], kind='水厂', profile='S2')
        state.initial.facilities['n0/P070/0'] = facility
        lid = facility.line_ids[0]
        order = {'id': '1', 'kind': 'industry', 'op': 'set_line', 'line_id': lid, 'recipe': 'P106', 'mode': 'general'}
        commands = IndustryCommands(state)
        with mock.patch.object(industry_commands, 'coastal_hub', return_value=False):
            with self.assertRaises(RuleViolation) as caught:
                commands.set_line('n0', dict(order), self.ctx())
            self.assertTrue(str(caught.exception).startswith('ERR_SITE_INCOMPATIBLE'))
        with mock.patch.object(industry_commands, 'coastal_hub', return_value=True):
            self.assertIsNotNone(commands.set_line('n0', dict(order), self.ctx()))


if __name__ == '__main__':
    unittest.main()
