"""Stateless random streams and canonical, lossless simulation fingerprints."""

from __future__ import annotations

import hashlib
import json
import math
import platform
import sys
from pathlib import Path
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
from fractions import Fraction
from typing import Any


def primitive(value: Any) -> Any:
    """Convert engine values to unambiguous JSON. No repr(), pickle or eval()."""
    if isinstance(value, Fraction):
        return {"$rational": [value.numerator, value.denominator]}
    if isinstance(value, Enum):
        return primitive(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: primitive(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        if not all(isinstance(k, str) for k in value):
            raise TypeError("canonical objects require string keys")
        return {k: primitive(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [primitive(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((primitive(v) for v in value), key=canonical)
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise TypeError(f"not a canonical value: {type(value).__name__}")


def canonical(value: Any) -> bytes:
    return json.dumps(primitive(value), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def digest(*key: str | int) -> bytes:
    # JSON preserves int vs string, and prevents ambiguous concatenated keys.
    if any(type(k) not in (str, int) for k in key):
        raise TypeError("random keys must contain only typed strings and integers")
    return hashlib.sha256(canonical(key)).digest()


def uniform(seed: int, stream: str, tick: int, location: str, sub: int = 0) -> float:
    bits = int.from_bytes(digest(seed, stream, tick, location, sub)[:8], "big")
    return (bits >> 11) * 2.0 ** -53


def normal(seed: int, stream: str, tick: int, location: str) -> float:
    u = max(uniform(seed, stream, tick, location, 0), 2.0 ** -53)
    v = uniform(seed, stream, tick, location, 1)
    return math.sqrt(-2 * math.log(u)) * math.cos(2 * math.pi * v)


def runtime_fingerprint() -> dict[str, str]:
    return {"python": sys.version, "implementation": platform.python_implementation(),
            "architecture": platform.machine(), "platform": platform.platform(),
            "libc": ":".join(platform.libc_ver())}


def engine_source_fingerprint() -> str:
    root = Path(__file__).resolve().parents[1]
    files = sorted((root / 'engine').glob('*.py')) + sorted((root / 'data').glob('*.json')) + [root / '__init__.py', root / '__main__.py', root / 'sdk.py']
    return fingerprint({str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in files})
