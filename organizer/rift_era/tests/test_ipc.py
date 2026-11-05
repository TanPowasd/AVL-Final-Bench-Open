"""Real Linux pipe tests using only fixed, trusted fixture programs.

SIGSTOP below is a test double for the managed-process interface. The production
entry point always launches the kernel sandbox and freezes its entire cgroup.
These tests do not assert target-host sandbox acceptance.
"""

import os
import fcntl
import signal
import subprocess
import sys
import textwrap
import unittest
from fractions import Fraction as Q

from organizer.rift_era.engine.errors import InfrastructureFault
from organizer.rift_era.engine.evaluation import StrategyClock, TimeBudget
from organizer.rift_era.engine.ipc import StrategySession, TransportLimits, StderrTail, downstream_pages, input_pipe_capacity
from organizer.rift_era.engine.protocol import MAX_SNAPSHOT


HELPER = '''
import sys, json, base64, time
def messages():
    chunks = []
    for line in sys.stdin.buffer:
        row = json.loads(line)
        if row.get('type') == 'snapshot_chunk':
            chunks.append(base64.b64decode(row['payload']))
            if len(chunks) != row['count']:
                continue
            row = json.loads(b''.join(chunks))
            chunks = []
        yield row
def send(row):
    print(json.dumps(row), flush=True)
'''


class TrustedFixture:
    def __init__(self, source, input_capacity_bytes=0):
        self.process = subprocess.Popen([sys.executable, '-u', '-c', HELPER + textwrap.dedent(source)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self.input_capacity_bytes = (fcntl.fcntl(self.process.stdin.fileno(), fcntl.F_SETPIPE_SZ, input_capacity_bytes)
                                     if input_capacity_bytes else 0)

    def pause(self):
        if self.process.poll() is None:
            try:
                os.kill(self.process.pid, signal.SIGSTOP)
                os.waitpid(self.process.pid, os.WUNTRACED)
            except ProcessLookupError:
                pass

    def resume(self):
        if self.process.poll() is None:
            os.kill(self.process.pid, signal.SIGCONT)

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=3)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()


class StrategyPipeTests(unittest.TestCase):
    def session(self, source, *, delivery=Q(2), window=Q(1), staged=False):
        clock = StrategyClock(TimeBudget(base_window_seconds=window, maximum_window_seconds=window,
                                        initial_pool_seconds=Q(5), total_seconds=Q(20)))
        session = StrategySession(TrustedFixture(source, 65536 if staged else 0), clock=clock,
            limits=TransportLimits(initialization_seconds=Q(2), delivery_seconds=delivery))
        self.addCleanup(session.close)
        return session

    def ready(self, session, **extra):
        result = session.initialize({'type': 'init'} | extra)
        self.assertEqual(result.status, 'ready', result)
        self.assertFalse(result.custodial)

    def test_paged_input_ready_orders_and_end_over_real_pipes(self):
        session = self.session('''
            for row in messages():
                if row['type'] == 'init':
                    assert len(row['map']) == 700000
                    send({'type': 'ready'})
                elif row['type'] == 'obs':
                    assert len(row['entities']) == 800000
                    send({'type': 'orders', 'window': row['window'], 'orders': []})
                elif row['type'] == 'end':
                    break
        ''')
        self.ready(session, map='x' * 700000)
        result = session.decide({'type': 'obs', 'window': 0, 'entities': 'y' * 800000})
        self.assertEqual(result.status, 'orders')
        self.assertEqual(result.orders, ())
        self.assertGreater(result.delivery_ns, 0)
        session.finish({'type': 'end'})
        self.assertTrue(session.closed)

    def test_timeout_partial_line_is_discarded_and_next_window_recovers(self):
        session = self.session('''
            for row in messages():
                if row['type'] == 'init':
                    send({'type': 'ready'})
                elif row['window'] == 0:
                    sys.stdout.write('{"type":"orders","window":0,"orders":')
                    sys.stdout.flush()
                    time.sleep(.23)
                    print('[]}', flush=True)
                else:
                    send({'type': 'orders', 'window': row['window'], 'orders': []})
        ''', window=Q(15, 100))
        self.ready(session)
        first = session.decide({'type': 'obs', 'window': 0})
        self.assertEqual(first.status, 'timeout')
        self.assertTrue(session.framer.discard_until_newline)
        second = session.decide({'type': 'obs', 'window': 1})
        self.assertEqual(second.status, 'orders', second)
        self.assertEqual(session.clock.timeouts, 1)
        self.assertFalse(second.custodial)

    def test_stale_frame_then_valid_frame_and_future_frame_rejection(self):
        session = self.session('''
            for row in messages():
                if row['type'] == 'init':
                    send({'type': 'ready'})
                elif row['window'] == 0:
                    send({'type': 'orders', 'window': 0, 'orders': []})
                elif row['window'] == 1:
                    print('{"type":"orders","window":0,"orders":[]}\\n'
                          '{"type":"orders","window":1,"orders":[]}', flush=True)
                else:
                    send({'type': 'orders', 'window': row['window'] + 1, 'orders': []})
        ''')
        self.ready(session)
        self.assertEqual(session.decide({'type': 'obs', 'window': 0}).status, 'orders')
        second = session.decide({'type': 'obs', 'window': 1})
        self.assertEqual(second.status, 'orders')
        self.assertIn('ERR_STALE_WINDOW', second.errors)
        third = session.decide({'type': 'obs', 'window': 2})
        self.assertEqual(third.status, 'invalid')
        self.assertIn('ERR_FUTURE_WINDOW', third.errors)
        self.assertFalse(third.custodial)

    def test_overlong_output_does_not_grow_buffer_or_shift_next_window(self):
        session = self.session('''
            for row in messages():
                if row['type'] == 'init':
                    send({'type': 'ready'})
                elif row['window'] == 0:
                    sys.stdout.write('x' * 300000 + '\\n')
                    sys.stdout.flush()
                else:
                    send({'type': 'orders', 'window': row['window'], 'orders': []})
        ''')
        self.ready(session)
        first = session.decide({'type': 'obs', 'window': 0})
        self.assertIn('ERR_LINE_TOO_LONG', first.errors)
        self.assertEqual(len(session.framer.buffer), 0)
        self.assertEqual(session.decide({'type': 'obs', 'window': 1}).status, 'orders')

    def test_stderr_flood_is_drained_and_private_tail_is_bounded(self):
        session = self.session('''
            for row in messages():
                if row['type'] == 'init':
                    send({'type': 'ready'})
                else:
                    sys.stderr.buffer.write(b'x' * (5 * 1024**2) + b'end')
                    sys.stderr.flush()
                    send({'type': 'orders', 'window': row['window'], 'orders': []})
        ''', window=Q(3))
        self.ready(session)
        result = session.decide({'type': 'obs', 'window': 0})
        self.assertEqual(result.status, 'orders', result)
        self.assertEqual(len(session.stderr.read()), 4 * 1024**2)
        self.assertTrue(session.stderr.read().endswith(b'end'))
        self.assertGreater(session.stderr.dropped, 0)

    def test_crash_and_unread_input_enter_custody_without_hanging_judge(self):
        crash = self.session('''
            for row in messages():
                if row['type'] == 'init':
                    send({'type': 'ready'})
                else:
                    raise RuntimeError('fixture crash')
        ''')
        self.ready(crash)
        result = crash.decide({'type': 'obs', 'window': 0})
        self.assertEqual(result.status, 'crashed')
        self.assertTrue(result.custodial)
        self.assertEqual(crash.decide({'type': 'obs', 'window': 1}).status, 'custodial')
        stalled = self.session('time.sleep(5)', delivery=Q(1, 10))
        result = stalled.initialize({'type': 'init', 'large': 'z' * 700000})
        self.assertEqual(result.status, 'delivery_failed')
        self.assertTrue(result.custodial)

    def test_oversized_downstream_is_organizer_fault_and_tail_exact(self):
        with self.assertRaises(InfrastructureFault):
            downstream_pages({'x': 'x' * MAX_SNAPSHOT})
        tail = StderrTail(7)
        for part in (b'12', b'34567', b'89abc'):
            tail.append(part)
        self.assertEqual(tail.read(), b'6789abc')
        self.assertEqual(tail.dropped, 5)

    def test_frozen_complete_input_staging_charges_computation_before_read(self):
        self.assertEqual(input_pipe_capacity(StrategyClock()), 512 * 1024**2)
        session = self.session('''
            stream = messages()
            assert next(stream)['type'] == 'init'
            send({'type': 'ready'})
            time.sleep(.20)
            for row in stream:
                send({'type': 'orders', 'window': row['window'], 'orders': []})
        ''', window=Q(1, 10), staged=True)
        self.ready(session)
        result = session.decide({'type': 'obs', 'window': 0})
        self.assertEqual(result.status, 'timeout', result)
        self.assertGreaterEqual(result.think_ns, 100000000)
        # Delaying stdin is charged execution, not free "delivery" time.
        self.assertLess(result.delivery_ns, result.think_ns)

    def test_staged_unread_input_and_output_flood_spend_timeout_budget(self):
        session = self.session('''
            stream = messages()
            assert next(stream)['type'] == 'init'
            send({'type': 'ready'})
            time.sleep(.03)
            sys.stdout.write('x' * 300000)
            sys.stdout.flush()
            time.sleep(5)
        ''', window=Q(1, 10), staged=True)
        self.ready(session)
        result = session.decide({'type': 'obs', 'window': 0})
        self.assertEqual(result.status, 'timeout', result)
        self.assertEqual(session.clock.timeouts, 1)
        self.assertIn('ERR_LINE_TOO_LONG', result.errors)

    def test_delayed_selector_event_cannot_accept_orders_after_deadline(self):
        import selectors
        import time
        from unittest.mock import patch
        session = self.session("""
            for row in messages():
                if row['type'] == 'init':
                    send({'type': 'ready'})
                else:
                    send({'type': 'orders', 'window': row['window'], 'orders': []})
        """, window=Q(1, 20), staged=True)
        self.ready(session)
        base = selectors.DefaultSelector
        class DelayedSelector(base):
            def select(self, timeout=None):
                events = super().select(timeout)
                if any(key.data == 'out' for key, _ in events):
                    time.sleep(.08)
                return events
        with patch('organizer.rift_era.engine.ipc.selectors.DefaultSelector', DelayedSelector):
            decision = session.decide({'type': 'obs', 'window': 0})
        self.assertEqual(decision.status, 'timeout')
        self.assertFalse(decision.orders)
        self.assertEqual(session.clock.timeouts, 1)

    def test_staged_end_waits_for_delivery_without_using_decision_allowance(self):
        session = self.session("""
            for row in messages():
                if row['type'] == 'init':
                    send({'type': 'ready'})
                elif row['type'] == 'end':
                    break
        """, staged=True)
        self.ready(session)
        result = session._exchange({'type': 'end'}, None, Q(0), expect_response=False)
        self.assertEqual(result.status, 'ended')
        self.assertEqual(result.think_ns, 0)


if __name__ == '__main__':
    unittest.main()
