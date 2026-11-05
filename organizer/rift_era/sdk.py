"""Standalone standard-library client; copy this file into a submission zip.

No organizer import, world truth or environment access is needed. Public catalog
ratios use {"$rational": [numerator, denominator]}; live command amounts are mU
integers. receive() returns an entire init/obs/end message after checked paging.
"""

import base64
from fractions import Fraction
import hashlib
import json
import sys

MAX_LINE = 262144
MAX_SNAPSHOT = 16777216


def ratio(value):
    if isinstance(value, dict) and set(value) == {'$rational'}:
        return Fraction(*value['$rational'])
    return Fraction(value)


class Client:
    def __init__(self, source=None, target=None):
        self.source = source if source is not None else sys.stdin.buffer
        self.target = target if target is not None else sys.stdout.buffer
        self.next_id = 1

    def _line(self):
        raw = self.source.readline(MAX_LINE + 2)
        if not raw:
            raise EOFError('judge closed input')
        if not raw.endswith(b'\n') or len(raw) - 1 > MAX_LINE:
            raise ValueError('invalid judge frame length')
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError('judge frame must be an object')
        return value

    def receive(self):
        first = self._line()
        if first.get('type') != 'snapshot_chunk':
            if first.get('type') not in ('init', 'obs', 'end'):
                raise ValueError('unknown judge message')
            return first
        total, count, sid = first.get('total_bytes'), first.get('count'), first.get('snapshot_id')
        if (type(total) is not int or not 0 <= total <= MAX_SNAPSHOT or type(count) is not int
                or not 1 <= count <= MAX_SNAPSHOT or not isinstance(sid, str) or len(sid) != 64):
            raise ValueError('invalid paged message bounds')
        chunks, size = [], 0
        for index in range(count):
            row = first if index == 0 else self._line()
            if (row.get('type'), row.get('encoding'), row.get('snapshot_id'), row.get('total_bytes'),
                    row.get('count'), row.get('index')) != (
                    'snapshot_chunk', 'base64_json_v1', sid, total, count, index):
                raise ValueError('missing, reordered or mixed snapshot page')
            chunk = base64.b64decode(row['payload'], validate=True)
            size += len(chunk)
            if size > total:
                raise ValueError('snapshot exceeds its declared length')
            chunks.append(chunk)
        raw = b''.join(chunks)
        if size != total or hashlib.sha256(raw).hexdigest() != sid:
            raise ValueError('snapshot length or digest mismatch')
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get('type') not in ('init', 'obs', 'end'):
            raise ValueError('unknown logical judge message')
        return value

    def _send(self, message):
        raw = json.dumps(message, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(raw) > MAX_LINE:
            raise ValueError('strategy message exceeds 256KiB')
        self.target.write(raw + b'\n')
        self.target.flush()

    def ready(self):
        self._send({'type': 'ready'})

    def command(self, kind, op, **parameters):
        if any(k in parameters for k in ('id', 'kind', 'op')):
            raise ValueError('reserved command field')
        result = {'id': str(self.next_id), 'kind': kind, 'op': op} | parameters
        self.next_id += 1
        return result

    def orders(self, window, commands):
        if type(window) is not int or window < 0 or not isinstance(commands, (list, tuple)) or len(commands) > 256:
            raise ValueError('invalid strategy window or command count')
        self._send({'type': 'orders', 'window': window, 'orders': list(commands)})
