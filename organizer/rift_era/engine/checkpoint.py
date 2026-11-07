"""Development checkpoints so one 28,800-tick match can run in segments.

A checkpoint is the pickled match state plus the kernel tick, written only at a
tick boundary, with a header naming the rules hash, map seed/version and the
fingerprint of the state. Resuming assembles `full_match` around the saved
state (fresh Economy/command handlers bound to it, no re-initialization), so the
next tick is the one the uninterrupted run would have taken. Loading verifies
the rules hash, the body digest and the state fingerprint.

Checkpoints are trusted local files for development and batch hosts. They are
never a replay or a scoring artefact: the official record stays the replay.
"""
from __future__ import annotations

import copyreg
import hashlib
import os
import pickle
import sys
from pathlib import Path
from types import MappingProxyType

from .determinism import fingerprint
from .errors import InfrastructureFault

MAGIC = b'RIFTCKPT1\n'


def _mappingproxy(data):
    return MappingProxyType(data)


copyreg.pickle(MappingProxyType, lambda m: (_mappingproxy, (dict(m),)))


def save(kernel, path: Path, *, rules_hash: str, world_seed: int, map_version: str) -> str:
    """Write atomically (tmp + fsync + rename); returns the state fingerprint."""
    path = Path(path)
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(limit, 100_000))
    try:
        body = pickle.dumps(kernel.state, protocol=5)
    finally:
        sys.setrecursionlimit(limit)
    state_hash = fingerprint(kernel.state.snapshot())
    header = pickle.dumps({'tick': kernel.tick, 'total_ticks': kernel.clock.total_ticks, 'rules_hash': rules_hash,
                           'world_seed': world_seed, 'map_version': map_version, 'state_hash': state_hash,
                           'body_sha256': hashlib.sha256(body).hexdigest()}, protocol=5)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with tmp.open('wb') as file:
        file.write(MAGIC + len(header).to_bytes(8, 'big') + header + body)
        file.flush()
        os.fsync(file.fileno())
    os.replace(tmp, path)
    return state_hash


def read_header(path: Path) -> dict:
    with Path(path).open('rb') as file:
        if file.read(len(MAGIC)) != MAGIC:
            raise InfrastructureFault('not a rift_era checkpoint')
        size = int.from_bytes(file.read(8), 'big')
        return pickle.loads(file.read(size))


def resume(path: Path, catalog, *, total_ticks: int | None = None, debug: bool = False):
    """Rebuild a full_match kernel around a saved state; returns (kernel, header)."""
    from .economy import full_match
    header, state = load_state(path, rules_hash=catalog.fingerprint)
    kernel = full_match(catalog, state.initial, total_ticks=total_ticks or header['total_ticks'],
                        debug=debug, resume_state=state)
    if kernel.clock.total_ticks != header['total_ticks']:
        raise InfrastructureFault('checkpoint clock mismatch')
    kernel.tick = header['tick']
    return kernel, header


def load_state(path: Path, *, rules_hash: str):
    """Read and verify a checkpoint; returns (header, state)."""
    data = Path(path).read_bytes()
    if not data.startswith(MAGIC):
        raise InfrastructureFault('not a rift_era checkpoint')
    size = int.from_bytes(data[len(MAGIC):len(MAGIC) + 8], 'big')
    start = len(MAGIC) + 8
    header = pickle.loads(data[start:start + size])
    body = data[start + size:]
    if header['rules_hash'] != rules_hash:
        raise InfrastructureFault('checkpoint rules hash mismatch')
    if hashlib.sha256(body).hexdigest() != header['body_sha256']:
        raise InfrastructureFault('checkpoint body corrupted')
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(limit, 100_000))
    try:
        state = pickle.loads(body)
    finally:
        sys.setrecursionlimit(limit)
    if fingerprint(state.snapshot()) != header['state_hash']:
        raise InfrastructureFault('checkpoint state fingerprint mismatch')
    return header, state
