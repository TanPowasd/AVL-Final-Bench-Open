"""batch_host_v1: the host side of a 64-seed batch.

`match_runner(...)` returns the `run_match(seed, seat, role)` callable that
`BatchRunner` drives. Each call builds a fresh full_match for the seed, opens a
strategy session per seat (the submission in `seat` for role 'player', the
reference everywhere else; eight references for role 'shadow'), runs the
ordinary `MatchDriver` and returns its finalizer report.

Fault attribution (07 section 4):
* a submission fault (crash, timeout, bad frame) stays inside the game: the
  session is custodial for the rest of its windows and the match is scored;
* a reference fault is never custodial. The first decision or handshake in
  which a reference seat reports errors or custody raises `ReferenceFault`;
* an engine invariant, a driver/observer fault or a host error propagates.
Both of the last two escape `run_match`, so `BatchRunner` voids the batch
with INVALID_EVALUATION instead of scoring a degraded game.

Seat factories are context managers `factory(owner, seed, role)` yielding a
`StrategySession`. The formal Super pool is the author's private reference;
this module only needs something with the session interface.
"""
from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path

from .errors import InfrastructureFault


class ReferenceFault(InfrastructureFault):
    """A reference strategy failed: the batch is invalid, not the submission."""


class _GuardedReference:
    """Session proxy that turns a reference seat's fault into ReferenceFault."""

    def __init__(self, owner: str, session):
        self.owner, self.session = owner, session

    def __getattr__(self, name):
        return getattr(self.session, name)

    def _check(self, result, stage):
        if result.errors or result.custodial:
            raise ReferenceFault(f'reference seat {self.owner} failed at {stage}: '
                                 f'{",".join(result.errors) or result.status}')
        return result

    def initialize(self, message):
        return self._check(self.session.initialize(message), 'initialization')

    def decide(self, observation):
        return self._check(self.session.decide(observation), f"window {observation.get('window')}")


def match_runner(*, submission: Callable, reference: Callable, replay_dir: Path,
                 ticks: int = 28_800, map_kind: str = 'atlas', catalog=None,
                 world: Callable[[int], object] | None = None) -> Callable[[int, int, str], dict]:
    from .catalog import Catalog
    from .driver import MatchDriver, economic_development_init, economic_development_messages
    from .economy import Economy, full_match
    from .initialization import initialize
    from .match import finalize_match

    catalog = catalog or Catalog()
    replay_dir = Path(replay_dir)
    replay_dir.mkdir(parents=True, exist_ok=True)
    if world is None:
        from ..__main__ import match_world
        world = lambda seed: match_world(seed, map_kind)

    def run_match(seed: int, seat: int, role: str) -> dict:
        if role not in ('player', 'shadow') or not 0 <= seat < 8:
            raise InfrastructureFault('invalid batch game')
        map_world = world(seed)
        game = full_match(catalog, initialize(catalog, map_world), total_ticks=ticks, commerce=True)
        economy = Economy(game.state)
        owners = sorted(game.state.initial.known)
        if len(owners) != 8:
            raise InfrastructureFault('a batch game needs eight seats')
        with ExitStack() as stack:
            sessions = {}
            for index, owner in enumerate(owners):
                if role == 'player' and index == seat:
                    sessions[owner] = stack.enter_context(submission(owner, seed, role))
                else:
                    sessions[owner] = _GuardedReference(owner, stack.enter_context(reference(owner, seed, role)))
            driver = MatchDriver(game, sessions,
                init_messages={p: economic_development_init(economy, p, game.clock) for p in sessions},
                observe=lambda p, t: economic_development_messages(economy, p, t, game.clock),
                finalize=lambda: finalize_match(game),
                replay=replay_dir / f'{seed}-{role}-{seat}.jsonl',
                manifest={'profile': 'batch_full_match', 'rules_hash': catalog.fingerprint,
                          'map_version': map_world.generator_version, 'map_attempt': map_world.attempt,
                          'role': role, 'seat': seat})
            report = driver.run()
        submission_owner = owners[seat] if role == 'player' else None
        return report | {'seed': seed, 'seat': seat, 'role': role, 'submission_owner': submission_owner}

    return run_match
