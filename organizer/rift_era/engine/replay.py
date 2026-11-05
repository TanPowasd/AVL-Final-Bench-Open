"""Append-only hash-chained organizer-side replays; no contestant destinations."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from collections.abc import Iterator

from .determinism import canonical
from .errors import InfrastructureFault


class ReplayWriter:
    def __init__(self, path: Path, manifest: dict):
        self._file = path.open('xb')
        self._previous = '0' * 64
        self._sequence = 0
        self._finished = False
        try:
            self.write('manifest', manifest)
        except Exception:
            self._file.close()
            raise

    def write(self, kind: str, payload: dict) -> None:
        if self._finished:
            raise InfrastructureFault('cannot append after replay completion')
        record = {'sequence': self._sequence, 'previous': self._previous, 'kind': kind, 'payload': payload}
        digest = hashlib.sha256(canonical(record)).hexdigest()
        self._file.write(canonical(dict(record, digest=digest)) + b'\n')
        self._previous, self._sequence = digest, self._sequence + 1

    def finish(self, summary: dict) -> None:
        self.write('end', summary)
        self._file.flush()
        os.fsync(self._file.fileno())
        self._finished = True

    def close(self) -> None:
        self._file.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def verify_replay(path: Path) -> Iterator[dict]:
    previous, sequence = '0' * 64, 0
    ended = False
    with path.open('rb') as file:
        for raw in file:
            try:
                record = json.loads(raw)
                if ended or (sequence == 0 and record.get('kind') != 'manifest'):
                    raise ValueError('invalid replay envelope')
                claimed = record.pop('digest')
                digest = hashlib.sha256(canonical(record)).hexdigest()
                if digest != claimed or record['previous'] != previous or record['sequence'] != sequence:
                    raise ValueError('chain mismatch')
            except (ValueError, KeyError, TypeError) as exc:
                raise InfrastructureFault(f'corrupt replay record {sequence}') from exc
            previous, sequence = digest, sequence + 1
            ended = record['kind'] == 'end'
            yield record
    if not ended:
        raise InfrastructureFault('incomplete replay: terminal record missing')
