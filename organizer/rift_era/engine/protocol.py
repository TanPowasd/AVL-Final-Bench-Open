"""Bounded JSONL framing, permanent command watermarks and paged snapshots."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from collections.abc import Callable

from .determinism import canonical, fingerprint
from .errors import RuleViolation, InfrastructureFault

MAX_LINE = 262144
MAX_SNAPSHOT = 16777216
MAX_COMMANDS = 256


def _no_constant(value):
    raise ValueError(f'non-finite JSON constant: {value}')


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def decode_json(raw: bytes) -> dict:
    if len(raw) > MAX_LINE:
        raise RuleViolation('ERR_LINE_TOO_LONG')
    try:
        value = json.loads(raw.decode('utf-8'), parse_constant=_no_constant, object_pairs_hook=_object)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise RuleViolation('ERR_SCHEMA', 'invalid JSON') from exc
    validate_shape(value)
    if not isinstance(value, dict):
        raise RuleViolation('ERR_SCHEMA', 'expected JSON object')
    return value


def validate_shape(value, depth: int = 0) -> None:
    if depth > 8:
        raise RuleViolation('ERR_SCHEMA', 'nesting limit')
    if isinstance(value, dict):
        if len(value) > 512:
            raise RuleViolation('ERR_SCHEMA', 'object limit')
        for key, item in value.items():
            if len(key) > 64:
                raise RuleViolation('ERR_SCHEMA', 'key limit')
            validate_shape(item, depth + 1)
    elif isinstance(value, list):
        if len(value) > 512:
            raise RuleViolation('ERR_SCHEMA', 'array limit')
        for item in value:
            validate_shape(item, depth + 1)
    elif isinstance(value, str):
        if len(value) > 64:
            raise RuleViolation('ERR_SCHEMA', 'ID/string limit')
    elif value is not None and type(value) not in (int, bool):
        # All public quantities and prices are integer mU, not binary floats.
        raise RuleViolation('ERR_SCHEMA', 'integer-only protocol')


@dataclass
class Framer:
    buffer: bytearray = field(default_factory=bytearray)
    discard_until_newline: bool = False

    def feed(self, chunk: bytes) -> tuple[list[bytes], list[str]]:
        """At most one partial line is retained, even for a gigabyte of garbage."""
        lines, errors = [], []
        cursor, retained = 0, 0
        while cursor < len(chunk):
            end = chunk.find(b'\n', cursor)
            stop = len(chunk) if end < 0 else end
            size = stop - cursor
            if not self.discard_until_newline:
                if len(self.buffer) + size > MAX_LINE:
                    self.buffer.clear()
                    self.discard_until_newline = True
                    errors.append('ERR_LINE_TOO_LONG')
                else:
                    self.buffer.extend(chunk[cursor:stop])
            if end >= 0:
                if not self.discard_until_newline:
                    if len(lines) >= 17 or retained + len(self.buffer) > 2 * MAX_LINE:
                        self.buffer.clear()
                        self.discard_until_newline = not chunk.endswith(b'\n')
                        return [], ['ERR_PIPE_FLOOD']
                    lines.append(bytes(self.buffer))
                    retained += len(self.buffer)
                self.buffer.clear()
                self.discard_until_newline = False
            cursor = stop + 1
        return lines, errors

    def timeout(self) -> None:
        if self.buffer:
            self.buffer.clear()
            self.discard_until_newline = True


@dataclass
class WindowReader:
    window: int
    stale_lines: int = 0
    stale_bytes: int = 0
    complete: bool = False

    def accept(self, raw: bytes) -> dict | None:
        if self.complete:
            raise RuleViolation('ERR_PIPE_FLOOD', 'second response in same window')
        frame = decode_json(raw)
        if frame.get('type') != 'orders' or type(frame.get('window')) is not int:
            raise RuleViolation('ERR_SCHEMA')
        if frame['window'] < self.window:
            self.stale_lines += 1
            self.stale_bytes += len(raw)
            if self.stale_lines > 16 or self.stale_bytes > 1048576:
                raise RuleViolation('ERR_PIPE_FLOOD')
            return None
        if frame['window'] > self.window:
            raise RuleViolation('ERR_FUTURE_WINDOW')
        if set(frame) != {'type', 'window', 'orders'} or not isinstance(frame['orders'], list):
            raise RuleViolation('ERR_SCHEMA')
        if len(frame['orders']) > MAX_COMMANDS:
            raise RuleViolation('ERR_WINDOW_ORDER_CAP')
        self.complete = True
        return frame


@dataclass(frozen=True)
class Receipt:
    id: str
    status: str
    code: str | None = None


@dataclass(frozen=True)
class PreparedCommand:
    """A validated mutation. prepare() must be pure and commit cannot reject."""
    commit: Callable[[], None]
    persistent: bool = False


def command_sequence(order: dict) -> int:
    """One parser for execution and resource-free simultaneous-policy preview."""
    if not isinstance(order, dict):
        raise RuleViolation('ERR_SCHEMA')
    raw = order.get('id')
    if type(raw) is int:
        if len(str(raw)) > 64:
            raise RuleViolation('ERR_SCHEMA', 'command sequence exceeds 64 decimal characters')
        return raw
    if isinstance(raw, str) and raw.isascii() and raw.isdecimal() and len(raw) <= 64:
        return int(raw)
    raise RuleViolation('ERR_SCHEMA', 'invalid command sequence')


class Commands:
    def __init__(self):
        self.watermark = 0
        self.plans: dict[str, dict] = {}
        self.recent: list[Receipt] = []

    def process(self, orders: list[dict], prepare: Callable[[dict], PreparedCommand]) -> tuple[Receipt, ...]:
        if len(orders) > MAX_COMMANDS:
            raise RuleViolation('ERR_WINDOW_ORDER_CAP')
        result = []
        for order in orders:
            oid = ''
            try:
                seq = command_sequence(order)
                oid = str(seq)
                if seq <= self.watermark:
                    raise RuleViolation('ERR_DUPLICATE_ID')
                # The watermark advances even when subsequent business validation fails.
                self.watermark = seq
                validate_shape(order)
                replacing = order.get('replaces_id')
                if replacing is not None and replacing not in self.plans:
                    raise RuleViolation('ERR_UNKNOWN_TARGET_ID')
                prepared = prepare(order)
                if prepared.persistent and replacing is None and len(self.plans) >= 64:
                    raise RuleViolation('ERR_ENTITY_CAP_EXCEEDED')
                try:
                    prepared.commit()
                except RuleViolation as exc:
                    # A validated commit may already have mutated state. Treat
                    # this as a fatal engine bug, never a recoverable rejection.
                    raise InfrastructureFault('prepared command failed during commit') from exc
                if replacing is not None:
                    del self.plans[replacing]
                if order.get('kind') == 'cancel' and order.get('op') == 'cancel_by_id':
                    target = order.get('target_id')
                    if isinstance(target, str) and target.isascii() and target.isdecimal():
                        self.plans.pop(str(int(target)), None)
                if prepared.persistent:
                    self.plans[oid] = dict(order)
                receipt = Receipt(oid, 'ACCEPTED')
            except RuleViolation as exc:
                receipt = Receipt(oid, 'REJECTED', exc.code)
            result.append(receipt)
        self.recent = (self.recent + result)[-256:]
        return tuple(result)


def paginate(snapshot: dict, *, chunk_bytes: int = MAX_LINE) -> tuple[bytes, ...]:
    """UTF-8 JSON payload fragments; envelope size is included in each limit."""
    payload = canonical(snapshot)
    if len(payload) > MAX_SNAPSHOT:
        raise InfrastructureFault('snapshot exceeds 16MiB; never truncate observations')
    if chunk_bytes < 1024:
        raise ValueError('page budget too small')
    import base64
    # Base64 is unambiguous across UTF-8 and escape boundaries. Versioned envelope.
    raw_limit = (chunk_bytes - 512) * 3 // 4
    chunks = [payload[i:i + raw_limit] for i in range(0, len(payload), raw_limit)] or [b'']
    sid = fingerprint(snapshot)
    result = tuple(canonical({'type': 'snapshot_chunk', 'encoding': 'base64_json_v1',
                  'snapshot_id': sid, 'index': i, 'count': len(chunks), 'total_bytes': len(payload),
                  'payload': base64.b64encode(chunk).decode('ascii')}) for i, chunk in enumerate(chunks))
    if any(len(page) > chunk_bytes for page in result):
        raise InfrastructureFault('pagination envelope exceeded budget')
    return result


def assemble(pages: tuple[bytes, ...]) -> dict:
    """Trusted/client helper: all pages must match, be unique and hash correctly."""
    import base64
    headers = []
    for page in pages:
        if len(page) > MAX_LINE:
            raise ValueError('oversized snapshot page')
        headers.append(json.loads(page))
    if not headers:
        raise ValueError('missing snapshot')
    head = headers[0]
    expected = (head['snapshot_id'], head['count'], head['total_bytes'])
    if not 0 < head['count'] <= MAX_SNAPSHOT or not 0 <= head['total_bytes'] <= MAX_SNAPSHOT:
        raise ValueError('invalid snapshot bounds')
    if len(headers) != head['count'] or {h['index'] for h in headers} != set(range(head['count'])):
        raise ValueError('missing or duplicate snapshot page')
    if any((h['snapshot_id'], h['count'], h['total_bytes']) != expected or h['encoding'] != 'base64_json_v1' for h in headers):
        raise ValueError('mixed snapshots')
    chunks, size = [], 0
    for h in sorted(headers, key=lambda h: h['index']):
        chunk = base64.b64decode(h['payload'], validate=True)
        size += len(chunk)
        if size > head['total_bytes']:
            raise ValueError('oversized snapshot payload')
        chunks.append(chunk)
    raw = b''.join(chunks)
    if len(raw) != head['total_bytes']:
        raise ValueError('snapshot length mismatch')
    value = json.loads(raw, parse_constant=_no_constant, object_pairs_hook=_object)
    if fingerprint(value) != head['snapshot_id']:
        raise ValueError('snapshot hash mismatch')
    return value
