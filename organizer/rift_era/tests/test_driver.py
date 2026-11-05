import io
import json
from pathlib import Path
import tempfile
import unittest

from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.driver import (MatchDriver, economic_development_init,
                                            economic_development_messages, replay_decisions)
from organizer.rift_era.engine.economy import Economy, opening_economy
from organizer.rift_era.engine.errors import InfrastructureFault
from organizer.rift_era.engine.initialization import initialize
from organizer.rift_era.engine.ipc import StrategySession, downstream_pages
from organizer.rift_era.engine.replay import verify_replay
from organizer.rift_era.engine.world import generate_candidate
from organizer.rift_era.sdk import Client
from organizer.rift_era.tests.test_ipc import TrustedFixture


class MatchDriverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog, cls.world = Catalog(), generate_candidate(1000)

    def game(self):
        return opening_economy(self.catalog, initialize(self.catalog, self.world), total_ticks=10,
                               commerce=True, debug=False)

    def test_all_seats_receive_filtered_initial_state_and_replay_reexecutes(self):
        game = self.game()
        economy = Economy(game.state)  # observation adapter must not reset yields
        sessions = {}
        for owner in game.state.initial.known:
            session = StrategySession(TrustedFixture('''
                for row in messages():
                    if row['type'] == 'init':
                        assert 'seed' not in row
                        assert row['scope'] == 'opening_economy_development'
                        owner = row['owner']
                        own = row['initial_observation']
                        locations = {h['id'] for h in own['hubs']}
                        locations.update(u['id'] for f in own['formations'] for u in f['units'])
                        assert all(a[0] in locations for a in own['inventory'])
                        send({'type': 'ready'})
                    elif row['type'] == 'obs':
                        commands = ([{'id': '1', 'kind': 'governance', 'op': 'fiscal_policy',
                                      'tax_rate_bp': 1000, 'welfare_budget_mU': 0, 'maint_budget_mU': 0}]
                                    if row['window'] == 0 else [])
                        send({'type': 'orders', 'window': row['window'], 'orders': commands})
                    else:
                        break
            '''))
            sessions[owner] = session
            self.addCleanup(session.close)
        messages = {p: economic_development_init(economy, p, game.clock) for p in sessions}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'replay.jsonl'
            driver = MatchDriver(game, sessions, init_messages=messages,
                observe=lambda p, t: economic_development_messages(economy, p, t, game.clock),
                finalize=lambda: {'scope': 'opening_economy_development', 'full_rules_accepted': False},
                replay=path, manifest={'profile': 'opening_economy_development', 'rules_hash': self.catalog.fingerprint})
            report = driver.run()
            self.assertTrue(report['valid'])
            self.assertFalse(report['official'])
            self.assertFalse(report['full_rules_accepted'])
            self.assertFalse(any(report['faults'].values()))
            self.assertEqual(report['ticks'], 10)
            self.assertTrue(all(s.closed for s in sessions.values()))
            self.assertEqual(len([r for r in verify_replay(path) if r['kind'] == 'decision']), 2)
            self.assertEqual(replay_decisions(path, self.game()), report['state_hash'])
            with self.assertRaises(InfrastructureFault):
                driver.run()

    def test_official_driver_cannot_bypass_pending_implementation_gates(self):
        game = self.game()
        with self.assertRaises(InfrastructureFault):
            MatchDriver(game, {}, init_messages={}, observe=lambda p, t: {}, finalize=lambda: {},
                        replay=Path('/unused'), manifest={}, official=True)

    def test_standalone_client_reassembles_pages_and_generates_monotonic_ids(self):
        message = {'type': 'init', 'text': '世' * 200000}
        target = io.BytesIO()
        client = Client(io.BytesIO(b''.join(downstream_pages(message))), target)
        self.assertEqual(client.receive(), message)
        client.ready()
        a, b = client.command('research', 'set_focus'), client.command('finance', 'fx')
        self.assertEqual((a['id'], b['id']), ('1', '2'))
        client.orders(0, [a, b])
        lines = target.getvalue().splitlines()
        self.assertEqual(json.loads(lines[0]), {'type': 'ready'})
        self.assertEqual(json.loads(lines[1])['orders'], [a, b])
        pages = list(downstream_pages(message))
        damaged = json.loads(pages[-1])
        damaged['snapshot_id'] = '0' * 64
        pages[-1] = json.dumps(damaged).encode() + b'\n'
        with self.assertRaises(ValueError):
            Client(io.BytesIO(b''.join(pages)), io.BytesIO()).receive()


if __name__ == '__main__':
    unittest.main()
