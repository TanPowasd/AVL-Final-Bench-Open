import tempfile
import unittest
import zipfile
from pathlib import Path
from fractions import Fraction as Q

from organizer.rift_era.engine.archive import extracted_submission
from organizer.rift_era.engine.clock import Phase
from organizer.rift_era.engine.determinism import canonical
from organizer.rift_era.engine.errors import RuleViolation, InfrastructureFault
from organizer.rift_era.engine.evaluation import Batch, ResourceBudget, StrategyClock, GIB
from organizer.rift_era.engine.kernel import System, order_systems
from organizer.rift_era.engine.ledger import Inventory, Account
from organizer.rift_era.engine.logistics import Transport, Corridor, Vehicle, TransitMode
from organizer.rift_era.engine.observation import Observer, Sighting
from organizer.rift_era.engine.protocol import (Framer, WindowReader, Commands, PreparedCommand,
                                             decode_json, paginate, assemble, MAX_LINE)
from organizer.rift_era.engine.release import require_official_readiness
from organizer.rift_era.engine.replay import ReplayWriter, verify_replay
from organizer.rift_era.engine.scenarios import supply_chain
from organizer.rift_era.engine.scenarios import replay_supply_chain
from organizer.rift_era.engine.determinism import primitive, engine_source_fingerprint, runtime_fingerprint
from organizer.rift_era.engine.catalog import Catalog


class BoundaryTests(unittest.TestCase):
    def test_half_line_timeout_does_not_shift_next_window(self):
        f = Framer()
        self.assertEqual(f.feed(b'{"type":"orders","window":0'), ([], []))
        f.timeout()
        lines, errors = f.feed(b',"orders":[]}\n{"type":"orders","window":1,"orders":[]}\n')
        self.assertEqual(len(lines), 1)
        self.assertEqual(WindowReader(1).accept(lines[0])['window'], 1)

    def test_oversized_half_line_has_bounded_retained_state(self):
        f = Framer()
        lines, errors = f.feed(b'x' * (MAX_LINE * 8))
        self.assertEqual(errors, ['ERR_LINE_TOO_LONG'])
        self.assertLessEqual(len(f.buffer), MAX_LINE)
        lines, errors = f.feed(b'junk\n{}\n')
        self.assertEqual(lines, [b'{}'])

    def test_many_short_lines_cannot_expand_receive_buffer(self):
        lines, errors = Framer().feed(b'{}\n' * 100000)
        self.assertEqual(lines, [])
        self.assertEqual(errors, ['ERR_PIPE_FLOOD'])

    def test_stale_window_flood_and_future_window(self):
        reader = WindowReader(5)
        stale = b'{"type":"orders","window":4,"orders":[]}'
        for _ in range(16):
            self.assertIsNone(reader.accept(stale))
        with self.assertRaisesRegex(RuleViolation, 'ERR_PIPE_FLOOD'):
            reader.accept(stale)
        with self.assertRaisesRegex(RuleViolation, 'ERR_FUTURE_WINDOW'):
            WindowReader(5).accept(b'{"type":"orders","window":6,"orders":[]}')

    def test_json_duplicate_keys_nan_deep_nesting_and_bool_quantities(self):
        for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}', b'{"x":1.5}',
                    b'{"x":' + b'[' * 10 + b'0' + b']' * 10 + b'}'):
            with self.assertRaises(RuleViolation):
                decode_json(raw)

    def test_watermark_survives_diagnostic_cache_eviction_and_rejections(self):
        processor = Commands()
        def reject(order):
            raise RuleViolation('ERR_TECH_LOCKED')
        for first in range(1, 600, 100):
            processor.process([{'id': str(i)} for i in range(first, first + 100)], reject)
        self.assertEqual(len(processor.recent), 256)
        self.assertEqual(processor.process([{'id': '1'}], reject)[0].code, 'ERR_DUPLICATE_ID')
        self.assertEqual(processor.watermark, 600)

    def test_invalid_replace_keeps_old_plan_and_chain_is_ordered(self):
        processor, values = Commands(), []
        def prepare(order):
            if order.get('invalid'):
                raise RuleViolation('ERR_SCHEMA')
            return PreparedCommand(lambda: values.append(order['id']), True)
        processor.process([{'id': '1'}], prepare)
        processor.process([{'id': '2', 'replaces_id': '1', 'invalid': True}], prepare)
        self.assertIn('1', processor.plans)
        result = processor.process([{'id': '3', 'replaces_id': '1'}, {'id': '4', 'replaces_id': '1'}], prepare)
        self.assertEqual([r.status for r in result], ['ACCEPTED', 'REJECTED'])
        self.assertEqual(values, ['1', '3'])
        self.assertEqual(set(processor.plans), {'3'})

    def test_commit_failure_is_engine_fault_not_a_player_rejection(self):
        processor = Commands()
        def commit():
            raise RuleViolation('ERR_INSUFFICIENT_CASH')
        with self.assertRaisesRegex(InfrastructureFault, 'failed during commit'):
            processor.process([{'id': '1'}], lambda order: PreparedCommand(commit))

    def test_paged_snapshot_hashes_and_missing_pages(self):
        snapshot = {'map': ['地形' * 100] * 1500, 'version': 1}
        pages = paginate(snapshot)
        self.assertGreater(len(pages), 1)
        self.assertTrue(all(len(p) <= MAX_LINE for p in pages))
        self.assertEqual(assemble(tuple(reversed(pages))), snapshot)
        with self.assertRaises(ValueError):
            assemble(pages[:-1])
        with self.assertRaises(ValueError):
            assemble((pages[0],) + pages[:-1])

    def test_observation_contains_no_internal_id_or_hidden_mutations(self):
        obs = Observer('A')
        sight = Sighting('secret_creator_sequence_18221', 0, 'visual', 'r', 600, 500, 1500, 'Land')
        first = obs.update(0, (sight,))
        self.assertNotIn('secret_creator', canonical(first).decode())
        departed = obs.update(5, ())
        self.assertEqual(departed['lost_contact_ids'], ['contact-1'])
        before = obs.snapshot()
        # A hidden unit dying supplies no legal sighting and must not emit deletion.
        hidden_changed = obs.update(10, ())
        self.assertEqual(hidden_changed['deleted_ids'], [])
        self.assertEqual(before, obs.snapshot())

    def test_observation_queries_are_readonly_and_snapshots_are_copies(self):
        obs = Observer('A')
        obs.update(0, (Sighting('id', 0, 'visual', 'r', 600, 1, 2, 'Land'),))
        snapshot = obs.snapshot()
        snapshot['tracks'][0]['estimated_high_mU'] = 999
        self.assertEqual(obs.snapshot()['tracks'][0]['estimated_high_mU'], 2)

    def test_resource_budget_charges_engine_and_each_strategy(self):
        budget = ResourceBudget(Q(16), 256 * GIB, 256 * GIB, Q(1), GIB, 8, 8)
        self.assertEqual(budget.concurrency(), 1)
        self.assertEqual(ResourceBudget(Q(1), 256 * GIB, 256 * GIB, Q(1), GIB, 1, 8).concurrency(), 0)
        self.assertEqual(ResourceBudget(Q(16), 2 * GIB, 16 * GIB, Q(1), GIB, 1, 8).concurrency(), 0)

    def test_batch_is_shared_frozen_and_never_overwritten(self):
        batch = Batch.create(rules_hash='rules', engine_hash='engine', reference_hash='ref', runtime_hash='image', resource_plan_hash='cpu')
        self.assertEqual(len(set(batch.seeds)), 64)
        self.assertEqual([batch.seats.count(i) for i in range(8)], [8] * 8)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'batch.json'
            batch.write_new(path)
            self.assertEqual(Batch.read(path), batch)
            with self.assertRaises(FileExistsError):
                batch.write_new(path)
        self.assertNotEqual(batch.shadow_key(batch.seeds[0], 0), batch.shadow_key(batch.seeds[0], 1))

    def test_strategy_clock_accounts_only_execution_and_preserves_plan_custody(self):
        clock = StrategyClock()
        self.assertEqual(clock.allowance, 30)
        clock.charge(2_000_000_000)
        self.assertEqual(clock.pool_seconds, Q('448.5'))
        for _ in range(50):
            clock.charge(0, timed_out=True)
        self.assertTrue(clock.custodial)
        self.assertEqual(clock.allowance, 0)

    def _archive(self, temp, entries):
        path = Path(temp) / 'submission.zip'
        with zipfile.ZipFile(path, 'w') as zf:
            for name, text in entries:
                zf.writestr(name, text)
        return path

    def test_archive_traversal_links_and_case_collision(self):
        for entries in ([('main.py', ''), ('../escape', 'x')], [('main.py', ''), ('A', 'x'), ('a', 'y')],
                        [('main.py', ''), ('C:\\escape', 'x')], [('main.py', ''), ('data', 'x'), ('data/child', 'x')]):
            with self.subTest(entries=entries), tempfile.TemporaryDirectory() as temp:
                path = self._archive(temp, entries)
                with self.assertRaisesRegex(RuleViolation, 'ERR_ARCHIVE_SECURITY'):
                    with extracted_submission(path):
                        self.fail('unsafe archive accepted')
        with tempfile.TemporaryDirectory() as temp:
            link = zipfile.ZipInfo('link')
            link.create_system = 3
            link.external_attr = 0o120777 << 16
            path = self._archive(temp, [('main.py', ''), (link, '/etc/passwd')])
            with self.assertRaises(RuleViolation):
                with extracted_submission(path):
                    self.fail('symlink accepted')

    def test_archive_cleanup_and_caller_fault_not_reclassified(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self._archive(temp, [('main.py', 'print(1)'), ('tables/x.json', '{}')])
            with self.assertRaisesRegex(RuntimeError, '^caller failure$'):
                with extracted_submission(path) as extracted:
                    self.assertEqual((extracted / 'main.py').read_text(), 'print(1)')
                    raise RuntimeError('caller failure')
            self.assertFalse(extracted.exists())

    def test_replay_integrity_completeness_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'replay.jsonl'
            with ReplayWriter(path, {'seed': 1}) as replay:
                replay.write('tick', {'tick': 0})
                replay.finish({'ticks': 1})
            self.assertEqual(len(list(verify_replay(path))), 3)
            with self.assertRaises(FileExistsError):
                ReplayWriter(path, {})
            original = path.read_bytes()
            path.write_bytes(original.replace(b'"tick":0', b'"tick":1'))
            with self.assertRaises(InfrastructureFault):
                list(verify_replay(path))
            path.write_bytes(b'\n'.join(original.splitlines()[:-1]) + b'\n')
            with self.assertRaisesRegex(InfrastructureFault, 'incomplete replay'):
                list(verify_replay(path))

    def test_replay_reexecutes_every_phase(self):
        engine = supply_chain(77, total_ticks=10)
        manifest = {'scenario': 'supply_chain_v1', 'official': False, 'seed': 77, 'clock': engine.clock,
                    'rules_hash': Catalog().fingerprint, 'engine_hash': engine_source_fingerprint(),
                    'runtime': runtime_fingerprint()}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'replay.jsonl'
            with ReplayWriter(path, manifest) as replay:
                for _ in range(10):
                    result = engine.step()
                    replay.write('tick', primitive(result))
                replay.finish({'ticks': 10, 'state_hash': result.state_hash})
            self.assertEqual(replay_supply_chain(path), result.state_hash)

    def test_official_mode_cannot_mislabel_component_tests(self):
        with self.assertRaisesRegex(InfrastructureFault, 'OFFICIAL_ENGINE_NOT_READY'):
            require_official_readiness()

    def test_transport_no_teleport_no_vehicle_copy_and_wait_unload(self):
        inv = Inventory()
        for good, amount in [('food', 10000), ('fuel', 10000)]:
            inv.initialize(Account('A', 'a', 'available', good), amount)
        vehicle = Vehicle('v', 'A', 'a', 'vehicle', 15000, Q(8), Q(6))
        transport = Transport(inv, {'e': Corridor('e', 'a', 'b', 16, 30000)}, {'v': vehicle})
        shipment = transport.launch('s', 'A', 'v', 'food', 3000, 'a', 'b', ('e',), 0)
        with self.assertRaises(RuleViolation):
            transport.launch('s2', 'A', 'v', 'food', 3000, 'a', 'b', ('e',), 0)
        transport.advance(0, 's', entry_granted=True, free_capacity_mU=0)
        transport.advance(1, 's', entry_granted=True, free_capacity_mU=0)
        self.assertEqual(inv.balance(Account('A', 'b', 'available', 'food')), 0)
        transport.advance(2, 's', entry_granted=False, free_capacity_mU=0)
        self.assertEqual(shipment.mode, TransitMode.WAIT_UNLOAD)
        self.assertEqual(inv.balance(Account('A', 's', 'in_transit', 'food')), 3000)
        transport.advance(3, 's', entry_granted=False, free_capacity_mU=3000)
        self.assertEqual(vehicle.hub, 'b')
        self.assertEqual(vehicle.assigned, 's')
        transport.advance(4, 's', entry_granted=True, free_capacity_mU=0)
        transport.advance(5, 's', entry_granted=False, free_capacity_mU=0)
        self.assertEqual((vehicle.hub, vehicle.assigned), ('a', None))
        self.assertEqual(inv.balance(Account('A', 'a', 'available', 'fuel')), 10000 - 144)
        inv.audit()

    def test_broken_bridge_cannot_be_crossed(self):
        inv = Inventory()
        for good in ('food', 'fuel'):
            inv.initialize(Account('A', 'a', 'available', good), 10000)
        edge = Corridor('e', 'a', 'b', 32, 30000)
        transport = Transport(inv, {'e': edge}, {'v': Vehicle('v', 'A', 'a', 'vehicle', 15000, Q(8), Q(6))})
        shipment = transport.launch('s', 'A', 'v', 'food', 3000, 'a', 'b', ('e',), 0)
        transport.advance(1, 's', entry_granted=True, free_capacity_mU=3000)
        edge.health_mU = 0
        transport.advance(2, 's', entry_granted=False, free_capacity_mU=3000)
        self.assertEqual(shipment.current_hub, 'a')
        self.assertEqual(shipment.progress_cells, 0)
        self.assertEqual(inv.balance(Account('A', 'b', 'available', 'food')), 0)

    def test_kernel_integrated_replay_and_audits_have_no_effect(self):
        a, b = supply_chain(123, total_ticks=100), supply_chain(123, total_ticks=100)
        b.debug = False
        for tick in range(100):
            order = {'A': ({'id': '1', 'kind': 'industry', 'op': 'fixture_set_batch', 'line_id': 'steel', 'batch_mU': 50},)} if tick == 20 else None
            ra, rb = a.step(order), b.step(order)
            self.assertEqual(ra.state_hash, rb.state_hash)
            self.assertEqual([phase for phase, _ in ra.stage_hashes], [p.name for p in Phase])
        self.assertGreater(a.state.inventory.balance(Account('A', 'port', 'available', 'steel')), 0)

    def test_decision_cadence_does_not_change_no_command_physics(self):
        a = supply_chain(123, total_ticks=100, decision_ticks=1)
        b = supply_chain(123, total_ticks=100, decision_ticks=5)
        for _ in range(100):
            a.step()
            b.step()
        self.assertEqual(a.state.inventory.snapshot(), b.state.inventory.snapshot())
        self.assertEqual(a.state.transport.vehicles, b.state.transport.vehicles)

    def test_kernel_rejects_missing_phases_and_fails_closed(self):
        with self.assertRaises(InfrastructureFault):
            order_systems(())
        engine = supply_chain(1, total_ticks=10)
        def crash(state, context):
            raise RuntimeError('failure')
        engine.systems = tuple(System(s.id, s.phase, crash if s.phase == Phase.POWER else s.advance, s.after) for s in engine.systems)
        with self.assertRaises(InfrastructureFault):
            engine.step()
        with self.assertRaisesRegex(InfrastructureFault, 'cannot resume'):
            engine.step()


if __name__ == '__main__':
    unittest.main()
