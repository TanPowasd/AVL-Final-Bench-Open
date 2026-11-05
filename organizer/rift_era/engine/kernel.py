"""Phase-ordered simulation kernel with explicit systems and audit boundaries.

A registered system is trusted organizer code, never an imported contestant.
No phase silently disappears: the assembly must supply all eight phases.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable, Mapping
from typing import Protocol

from .clock import ClockConfig, Phase
from .determinism import fingerprint
from .errors import InfrastructureFault


class WorldState(Protocol):
    def snapshot(self) -> dict: ...
    def audit(self) -> None: ...


@dataclass(frozen=True)
class TickContext:
    seed: int
    tick: int
    clock: ClockConfig
    orders: Mapping[str, tuple[dict, ...]]

    @property
    def cycle(self) -> int:
        return self.clock.cycle(self.tick)

    @property
    def window(self) -> int:
        return self.clock.window(self.tick)


@dataclass(frozen=True)
class System:
    id: str
    phase: Phase
    advance: Callable[[WorldState, TickContext], None]
    after: tuple[str, ...] = ()


@dataclass(frozen=True)
class TickResult:
    tick: int
    stage_hashes: tuple[tuple[str, str], ...]
    state_hash: str


def order_systems(systems: tuple[System, ...]) -> tuple[System, ...]:
    by_id = {s.id: s for s in systems}
    if len(by_id) != len(systems):
        raise InfrastructureFault('duplicate simulation system')
    if {s.phase for s in systems} != set(Phase):
        raise InfrastructureFault('all eight phases must be explicitly assembled')
    for system in systems:
        for dependency in system.after:
            if dependency not in by_id or by_id[dependency].phase > system.phase:
                raise InfrastructureFault('missing dependency or backwards phase dependency')
    result, done = [], set()
    for phase in Phase:
        pending = {s.id for s in systems if s.phase == phase}
        while pending:
            ready = sorted(s for s in pending if set(by_id[s].after) <= done)
            if not ready:
                raise InfrastructureFault('cyclic system dependency')
            for sid in ready:
                result.append(by_id[sid])
                done.add(sid)
                pending.remove(sid)
    return tuple(result)


class Kernel:
    def __init__(self, seed: int, state: WorldState, systems: tuple[System, ...], *,
                 clock: ClockConfig = ClockConfig(), debug: bool = True,
                 fingerprint_each_tick: bool = True):
        if type(seed) is not int or not 0 <= seed < 2**64:
            raise ValueError('seed must be uint64')
        self.seed, self.state, self.clock = seed, state, clock
        self.systems = order_systems(systems)
        self.tick = 0
        self.debug = debug
        self.fingerprint_each_tick = fingerprint_each_tick
        self.failed = False
        state.audit()

    def step(self, orders: Mapping[str, tuple[dict, ...]] | None = None) -> TickResult:
        if self.failed:
            raise InfrastructureFault('failed simulation cannot resume')
        if self.tick >= self.clock.total_ticks:
            raise ValueError('match has ended')
        if orders and not self.clock.is_decision(self.tick):
            raise ValueError('new commands outside decision boundary')
        context = TickContext(self.seed, self.tick, self.clock, orders or {})
        hashes = []
        try:
            for phase in Phase:
                for system in self.systems:
                    if system.phase == phase:
                        system.advance(self.state, context)
                if self.debug:
                    self.state.audit()
                    hashes.append((phase.name, fingerprint(self.state.snapshot())))
            state_hash = (hashes[-1][1] if hashes else fingerprint(self.state.snapshot())) if (self.debug or self.fingerprint_each_tick) else ''
        except Exception as exc:
            self.failed = True
            # A leaked player rejection is an engine bug: P1 must catch it per order.
            if isinstance(exc, InfrastructureFault):
                raise
            raise InfrastructureFault(f'simulation failed at tick {self.tick}, phase {phase.name}') from exc
        result = TickResult(self.tick, tuple(hashes), state_hash)
        self.tick += 1
        return result
