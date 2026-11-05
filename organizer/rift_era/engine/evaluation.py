"""Batch manifests and conservative resource admission. No best-of-run scoring."""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
import json
import os
from pathlib import Path
import secrets

from .determinism import canonical, fingerprint
from .errors import InfrastructureFault
from .numeric import ceil, floor, Q

GIB = 1024**3


@dataclass(frozen=True)
class ResourceBudget:
    effective_cpu: Fraction
    memory_available: int
    memory_total: int
    engine_cpu_peak: Fraction
    engine_memory_peak: int
    active_strategies: int
    resident_strategies: int

    def concurrency(self) -> int:
        if self.effective_cpu <= 0 or self.engine_cpu_peak <= 0 or self.engine_memory_peak <= 0:
            raise InfrastructureFault('missing engine resource measurements/budget')
        if not 1 <= self.active_strategies <= self.resident_strategies <= 8:
            raise InfrastructureFault('invalid strategy scheduler plan')
        if not 0 <= self.memory_available <= self.memory_total:
            raise InfrastructureFault('invalid host memory measurement')
        reserved = min(max(Q(0), self.effective_cpu - 1), max(1, ceil(self.effective_cpu / 4)))
        per_match_cpu = ceil(self.engine_cpu_peak) + self.active_strategies
        cpu_jobs = floor((self.effective_cpu - reserved) / per_match_cpu)
        memory_margin = max(4 * GIB, ceil(Q(self.memory_total, 5)))
        memory_jobs = (self.memory_available - memory_margin) // (self.engine_memory_peak + self.resident_strategies * GIB)
        return max(0, min(cpu_jobs, memory_jobs))


def effective_cpu(cgroup_root: Path = Path('/sys/fs/cgroup')) -> Fraction:
    count = len(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else (os.cpu_count() or 1)
    result = Q(count)
    # Walk the process's actual hierarchy and all ancestors: a parent can bind it.
    try:
        relative = next(line.split(':', 2)[2] for line in Path('/proc/self/cgroup').read_text().splitlines()
                        if line.startswith('0::'))
        current = cgroup_root / relative.lstrip('/')
        while current == cgroup_root or cgroup_root in current.parents:
            file = current / 'cpu.max'
            if file.is_file():
                quota, period = file.read_text().split()
                if quota != 'max':
                    result = min(result, Q(int(quota), int(period)))
            if current == cgroup_root:
                break
            current = current.parent
    except (OSError, StopIteration, ValueError) as exc:
        raise InfrastructureFault('cannot determine cgroup CPU budget') from exc
    return result


@dataclass(frozen=True)
class Batch:
    batch_id: str
    seeds: tuple[int, ...]
    seats: tuple[int, ...]
    rules_hash: str
    engine_hash: str
    reference_hash: str
    runtime_hash: str
    resource_plan_hash: str
    source: str = 'os_csprng'

    @classmethod
    def create(cls, *, rules_hash: str, engine_hash: str, reference_hash: str,
               runtime_hash: str, resource_plan_hash: str) -> Batch:
        seeds, seen = [], set()
        while len(seeds) < 64:
            seed = secrets.randbits(64)
            if seed not in seen:
                seen.add(seed)
                seeds.append(seed)
        return cls(secrets.token_hex(16), tuple(seeds), tuple(i % 8 for i in range(64)),
                   rules_hash, engine_hash, reference_hash, runtime_hash, resource_plan_hash)

    def validate(self) -> None:
        if (self.source != 'os_csprng' or len(self.seeds) != 64 or len(set(self.seeds)) != 64 or
                any(type(seed) is not int or not 0 <= seed < 2**64 for seed in self.seeds) or
                self.seats != tuple(i % 8 for i in range(64))):
            raise InfrastructureFault('invalid official batch sampling')
        if any(not h for h in (self.rules_hash, self.engine_hash, self.reference_hash, self.runtime_hash, self.resource_plan_hash)):
            raise InfrastructureFault('missing batch fingerprint')

    def write_new(self, path: Path) -> None:
        self.validate()
        with path.open('xb') as file:
            file.write(canonical(self) + b'\n')
            file.flush()
            os.fsync(file.fileno())

    @classmethod
    def read(cls, path: Path) -> Batch:
        data = json.loads(path.read_bytes())
        data['seeds'], data['seats'] = tuple(data['seeds']), tuple(data['seats'])
        result = cls(**data)
        result.validate()
        return result

    def shadow_key(self, seed: int, seat: int) -> str:
        if seed not in self.seeds or seat not in range(8):
            raise ValueError('seed/seat outside batch')
        return fingerprint({'batch': self, 'seed': seed, 'seat': seat})


@dataclass(frozen=True)
class TimeBudget:
    base_window_seconds: Fraction = Q(1, 2)
    maximum_window_seconds: Fraction = Q(30)
    initial_pool_seconds: Fraction = Q(450)
    total_seconds: Fraction = Q(3600)

    def __post_init__(self):
        if (self.base_window_seconds <= 0 or self.maximum_window_seconds < self.base_window_seconds
                or self.initial_pool_seconds < 0 or self.total_seconds <= 0):
            raise ValueError('invalid strategy thinking budget')


@dataclass
class StrategyClock:
    budget: TimeBudget = TimeBudget()
    pool_seconds: Fraction = field(init=False)
    spent_seconds: Fraction = Q(0)
    timeouts: int = 0
    custodial: bool = False

    def __post_init__(self) -> None:
        self.pool_seconds = self.budget.initial_pool_seconds

    @property
    def allowance(self) -> Fraction:
        if self.custodial:
            return Q(0)
        return max(Q(0), min(self.budget.maximum_window_seconds,
                   self.budget.base_window_seconds + self.pool_seconds,
                   self.budget.total_seconds - self.spent_seconds))

    def charge(self, elapsed_ns: int, *, timed_out: bool = False, crashed: bool = False) -> None:
        if elapsed_ns < 0:
            raise ValueError('negative execution time')
        elapsed = Q(elapsed_ns, 1_000_000_000)
        self.spent_seconds += elapsed
        self.pool_seconds = max(Q(0), self.pool_seconds - max(Q(0), elapsed - self.budget.base_window_seconds))
        self.timeouts += int(timed_out)
        self.custodial |= crashed or self.timeouts >= 50 or self.pool_seconds == 0 or self.spent_seconds >= self.budget.total_seconds

    def charge_initialization(self, elapsed_ns: int, *, failed: bool = False) -> None:
        """The separate 15s initialization allowance does not spend time_pool."""
        if elapsed_ns < 0:
            raise ValueError('negative initialization time')
        self.spent_seconds += Q(elapsed_ns, 1_000_000_000)
        self.custodial |= failed or self.spent_seconds >= self.budget.total_seconds
