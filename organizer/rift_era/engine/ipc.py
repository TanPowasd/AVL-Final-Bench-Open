"""Bounded, nonblocking strategy JSONL sessions over a managed kernel sandbox.

Only already-filtered observations enter this module. It has no world state,
seed, score collector or access to other seats. The production constructor always
uses Sandbox.start; raw processes are accepted only by the transport component
so its pipe behavior can be tested independently of host cgroup delegation.
"""

from __future__ import annotations

from array import array
from collections import deque
from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import selectors
import termios
import time
from typing import Protocol

from .determinism import canonical
from .errors import InfrastructureFault, RuleViolation
from .evaluation import StrategyClock
from .numeric import Q, floor
from .protocol import Framer, MAX_LINE, MAX_SNAPSHOT, WindowReader, decode_json, paginate
from .sandbox import Sandbox


class ManagedProcess(Protocol):
    process: object
    def pause(self) -> None: ...
    def resume(self) -> None: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class TransportLimits:
    initialization_seconds: Q = Q(15)
    delivery_seconds: Q = Q(30)
    stderr_bytes: int = 4 * 1024**2

    def __post_init__(self):
        if self.initialization_seconds <= 0 or self.delivery_seconds <= 0 or not 0 < self.stderr_bytes <= 4 * 1024**2:
            raise ValueError('invalid transport limits')


@dataclass(frozen=True)
class Decision:
    window: int | None
    orders: tuple[dict, ...]
    status: str
    errors: tuple[str, ...]
    think_ns: int
    delivery_ns: int
    custodial: bool


class StderrTail:
    """Private diagnostic tail. Never attach it to another seat or a replay."""

    def __init__(self, limit: int):
        self.limit, self.size, self.dropped = limit, 0, 0
        self.chunks = deque()

    def append(self, chunk: bytes) -> None:
        self.chunks.append(chunk)
        self.size += len(chunk)
        while self.size > self.limit:
            oldest = self.chunks.popleft()
            remove = min(len(oldest), self.size - self.limit)
            self.size -= remove
            self.dropped += remove
            if remove < len(oldest):
                self.chunks.appendleft(oldest[remove:])

    def read(self) -> bytes:
        return b''.join(self.chunks)


def downstream_pages(message: dict) -> tuple[bytes, ...]:
    raw = canonical(message)
    if len(raw) > MAX_SNAPSHOT:
        raise InfrastructureFault('filtered snapshot exceeds 16MiB')
    pages = (raw,) if len(raw) <= MAX_LINE else paginate(message)
    return tuple(page + b'\n' for page in pages)


def _queued_input(fd: int) -> int:
    # Linux exposes unread pipe bytes on either endpoint. Unlike successful
    # write(), zero here proves that the complete frame left the kernel pipe.
    count = array('i', [0])
    try:
        fcntl.ioctl(fd, termios.FIONREAD, count, True)
    except OSError as exc:
        raise InfrastructureFault('cannot attest strategy input delivery') from exc
    return count[0]


def input_pipe_capacity(clock: StrategyClock) -> int:
    """Bound outstanding unread frames by the actual timeout/time-pool rules.

    An accepted response must have drained its input. An unread frame can only
    survive a full timeout. With the default clock, custody starts at timeout 16.
    Twice the logical snapshot limit covers base64 and every paging envelope.
    Capacity reserves pipe slots, not resident pages for nonexistent snapshots.
    """
    probe = StrategyClock(clock.budget)
    count = 0
    while not probe.custodial:
        if probe.allowance <= 0:
            break
        probe.charge(max(1, floor(probe.allowance * 10**9)), timed_out=True)
        count += 1
    required = max(1, count) * 2 * MAX_SNAPSHOT
    return 1 << (required - 1).bit_length()


class StrategySession:
    def __init__(self, managed: ManagedProcess, *, clock: StrategyClock | None = None,
                 limits: TransportLimits = TransportLimits()):
        self.managed, self.process = managed, managed.process
        self.clock = clock or StrategyClock()
        self.limits = limits
        self.framer = Framer()
        self.stderr = StderrTail(limits.stderr_bytes)
        self.initialized = False
        self.next_window = 0
        self.closed = False
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is None:
                raise InfrastructureFault('strategy transport requires three pipes')
            os.set_blocking(stream.fileno(), False)
        self.managed.pause()

    @classmethod
    def launch(cls, sandbox: Sandbox, submission: Path, **kwargs) -> StrategySession:
        budget = kwargs.get('clock') or StrategyClock()
        managed = sandbox.start(submission, start_paused=True, input_capacity_bytes=input_pipe_capacity(budget))
        try:
            return cls(managed, **kwargs)
        except Exception:
            managed.close()
            raise

    def initialize(self, message: dict) -> Decision:
        if self.initialized or self.closed or message.get('type') != 'init':
            raise InfrastructureFault('invalid strategy initialization lifecycle')
        result = self._exchange(message, None, self.limits.initialization_seconds)
        self.initialized = True
        self.clock.charge_initialization(result.think_ns, failed=result.status != 'ready')
        return self._result_with_custody(result)

    def decide(self, observation: dict) -> Decision:
        window = observation.get('window')
        if (not self.initialized or self.closed or observation.get('type') != 'obs'
                or type(window) is not int or window != self.next_window):
            raise InfrastructureFault('out-of-order strategy window')
        self.next_window += 1
        if self.clock.custodial:
            return Decision(window, (), 'custodial', (), 0, 0, True)
        result = self._exchange(observation, window, self.clock.allowance)
        self.clock.charge(result.think_ns, timed_out=result.status == 'timeout',
                          crashed=result.status in ('crashed', 'delivery_failed'))
        return self._result_with_custody(result)

    def _result_with_custody(self, result):
        return Decision(result.window, result.orders, result.status, result.errors,
                        result.think_ns, result.delivery_ns, self.clock.custodial)

    def finish(self, message: dict) -> None:
        if not self.initialized or self.closed or message.get('type') != 'end':
            raise InfrastructureFault('invalid strategy end lifecycle')
        try:
            if not self.clock.custodial:
                self._exchange(message, None, Q(0), expect_response=False)
        finally:
            self.close()

    def _exchange(self, message: dict, window: int | None, allowance: Q, *, expect_response: bool = True) -> Decision:
        pages = downstream_pages(message)  # organizer failures precede resume
        reader = WindowReader(window) if window is not None else None
        incoming = self.process.stdin.fileno()
        outgoing = self.process.stdout.fileno()
        diagnostics = self.process.stderr.fileno()
        allowance_ns = max(0, floor(allowance * 10**9))
        delivery_started = time.monotonic_ns()
        delivery_deadline = delivery_started + floor(self.limits.delivery_seconds * 10**9)
        think_started = None
        delivery_ns = 0
        page, offset = 0, 0
        errors, early = [], []
        status, orders, think_ns = 'crashed', (), 0
        buffered = bool(getattr(self.managed, 'input_capacity_bytes', 0))
        deferred_invalid = False
        if buffered:
            if _queued_input(incoming) + sum(map(len, pages)) > self.managed.input_capacity_bytes:
                raise InfrastructureFault('strategy input backlog exceeded its proved timeout bound')
            # Fill the complete snapshot while ALL contestant threads are
            # frozen. No precomputation can hide inside uncharged page delivery.
            try:
                for encoded in pages:
                    cursor = 0
                    while cursor < len(encoded):
                        cursor += os.write(incoming, memoryview(encoded)[cursor:])
            except BrokenPipeError:
                return Decision(window, (), 'crashed', ('STRATEGY_EXIT',), 0,
                                time.monotonic_ns() - delivery_started, False)
            except BlockingIOError as exc:
                raise InfrastructureFault('reserved strategy pipe could not stage a complete snapshot') from exc
            page = len(pages)
        self.managed.resume()
        if buffered:
            think_started = time.monotonic_ns()
            delivery_ns = think_started - delivery_started
        try:
            with selectors.DefaultSelector() as selector:
                if not buffered:
                    selector.register(incoming, selectors.EVENT_WRITE, 'in')
                selector.register(outgoing, selectors.EVENT_READ, 'out')
                selector.register(diagnostics, selectors.EVENT_READ, 'err')
                while True:
                    now = time.monotonic_ns()
                    if think_started is None and page == len(pages) and _queued_input(incoming) == 0:
                        think_started = now
                        delivery_ns = now - delivery_started
                        if not expect_response:
                            status = 'ended'
                            break
                    input_drained = not buffered or _queued_input(incoming) == 0
                    if not expect_response and page == len(pages) and input_drained:
                        status, think_ns = 'ended', 0
                        break
                    deadline = delivery_deadline if think_started is None or not expect_response else think_started + allowance_ns
                    if now >= deadline:
                        status = 'delivery_failed' if think_started is None else 'timeout'
                        errors.append('INPUT_DELIVERY_TIMEOUT' if think_started is None else 'ERR_TIMEOUT')
                        delivery_ns = now - delivery_started if think_started is None else delivery_ns
                        think_ns = 0 if think_started is None else now - think_started
                        self.framer.timeout()
                        break
                    if deferred_invalid and input_drained:
                        status, think_ns = 'invalid', now - think_started
                        break
                    if think_started is not None and early and input_drained:
                        try:
                            for raw in early:
                                if reader is None:
                                    if status == 'ready':
                                        raise RuleViolation('ERR_PIPE_FLOOD')
                                    if decode_json(raw) != {'type': 'ready'}:
                                        raise RuleViolation('ERR_SCHEMA')
                                    status = 'ready'
                                else:
                                    frame = reader.accept(raw)
                                    if frame is None:
                                        errors.append('ERR_STALE_WINDOW')
                                    else:
                                        status, orders = 'orders', tuple(frame['orders'])
                            early.clear()
                            if status in ('orders', 'ready'):
                                think_ns = now - think_started
                                break
                        except RuleViolation as exc:
                            errors.append(exc.code)
                            status, orders, think_ns = 'invalid', (), now - think_started
                            break
                    # After the last write, poll delivery acknowledgement as
                    # well as stdout/stderr. No busy loop on a writable pipe.
                    timeout = (deadline - now) / 1e9
                    if think_started is None and page == len(pages):
                        timeout = min(timeout, .001)
                    if buffered and not input_drained and (early or deferred_invalid):
                        timeout = min(timeout, .001)
                    events = selector.select(timeout)
                    for key, _ in events:
                        if key.data == 'in':
                            try:
                                count = os.write(incoming, memoryview(pages[page])[offset:offset + 65536])
                            except BlockingIOError:
                                continue
                            except BrokenPipeError:
                                status = 'crashed'
                                break
                            offset += count
                            if offset == len(pages[page]):
                                page, offset = page + 1, 0
                                if page == len(pages):
                                    selector.unregister(incoming)
                            continue
                        try:
                            chunk = os.read(key.fd, 65536)
                        except BlockingIOError:
                            continue
                        if not chunk:
                            selector.unregister(key.fd)
                            if key.data == 'out':
                                status = ('ended' if not expect_response and page == len(pages)
                                          and _queued_input(incoming) == 0 else 'crashed')
                                break
                            continue
                        if key.data == 'err':
                            self.stderr.append(chunk)
                            continue
                        lines, framing_errors = self.framer.feed(chunk)
                        early.extend(lines)
                        if framing_errors or len(early) > 17 or sum(map(len, early)) > 2 * MAX_LINE:
                            errors.extend(framing_errors or ['ERR_PIPE_FLOOD'])
                            if buffered and _queued_input(incoming):
                                # The program has not consumed the snapshot.
                                # Keep charging the window until it does or its
                                # timeout expires; guessing an early bad frame
                                # cannot accumulate unbounded unread input.
                                deferred_invalid = True
                                early.clear()
                                selector.unregister(outgoing)
                                continue
                            status, orders = 'invalid', ()
                            break
                    else:
                        continue
                    # Broken pipes, EOF, or framing failures end this exchange.
                    ended = time.monotonic_ns()
                    delivery_ns = ended - delivery_started if think_started is None else delivery_ns
                    think_ns = ended - think_started if think_started is not None and expect_response else 0
                    break
        finally:
            self.managed.pause()
            if status in ('orders', 'ready') and self._has_extra_stdout():
                status, orders = 'invalid', ()
                errors.append('ERR_PIPE_FLOOD')
            if status in ('invalid', 'crashed', 'delivery_failed') or deferred_invalid:
                self._discard_readable_stdout()
            self._drain_stderr()
        if status == 'crashed':
            errors.append('STRATEGY_EXIT')
        if status == 'invalid' and think_started is None:
            # Delivery was interrupted: the next observation cannot be appended
            # after an incomplete frame. End this process, preserving its plans.
            status = 'delivery_failed'
        return Decision(window, orders, status, tuple(errors), think_ns, delivery_ns, False)

    def _has_extra_stdout(self) -> bool:
        extra = bool(self.framer.buffer)
        while True:
            try:
                chunk = os.read(self.process.stdout.fileno(), 65536)
            except BlockingIOError:
                return extra
            if not chunk:
                return extra
            extra = True
            self.framer.feed(chunk)

    def _discard_readable_stdout(self):
        # All strategy threads are frozen, so draining the finite kernel pipe
        # cannot become an unbounded read loop against a flooding writer.
        self.framer.timeout()
        while True:
            try:
                chunk = os.read(self.process.stdout.fileno(), 65536)
            except BlockingIOError:
                return
            if not chunk:
                return
            self.framer.discard_until_newline = not chunk.endswith(b'\n')

    def _drain_stderr(self):
        while True:
            try:
                chunk = os.read(self.process.stderr.fileno(), 65536)
            except BlockingIOError:
                return
            if not chunk:
                return
            self.stderr.append(chunk)

    def close(self):
        if not self.closed:
            self.managed.close()
            self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
