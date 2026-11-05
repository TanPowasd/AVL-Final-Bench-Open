"""Decision-window orchestration, private replay and submission fault reports.

This drives an explicitly supplied simulation assembly. It does not turn the
opening economy into a complete game, supply missing scoring, or bypass release
gates. Observers and finalizers are mandatory organizer-owned adapters.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict
from pathlib import Path

from .determinism import engine_source_fingerprint, fingerprint, primitive, runtime_fingerprint
from .errors import InfrastructureFault
from .evaluation import TimeBudget
from .ipc import StrategySession, downstream_pages
from .kernel import Kernel
from .release import require_official_readiness
from .replay import ReplayWriter, verify_replay


class MatchDriver:
    def __init__(self, engine: Kernel, sessions: Mapping[str, StrategySession], *,
                 init_messages: Mapping[str, dict], observe: Callable[[str, int], dict],
                 finalize: Callable[[], dict], replay: Path, manifest: dict,
                 official: bool = False):
        if official:
            require_official_readiness()
            if not engine.clock.official or len(sessions) != 8:
                raise InfrastructureFault('formal driver requires eight seats and the official clocks')
            if any(not getattr(session.managed, 'input_capacity_bytes', 0) for session in sessions.values()):
                raise InfrastructureFault('formal strategies require complete input staging while frozen')
        if engine.tick != 0 or not sessions or sessions.keys() != init_messages.keys():
            raise InfrastructureFault('driver must own a new engine and every seat handshake')
        self.engine, self.sessions = engine, dict(sessions)
        self.init_messages = dict(init_messages)
        self.observe, self.finalize = observe, finalize
        self.replay_path, self.manifest = replay, dict(manifest)
        self.official = official
        self.started = False

    def run(self) -> dict:
        if self.started:
            raise InfrastructureFault('driver cannot replay or resume a partially executed run')
        self.started = True
        engine = self.engine
        source_hash = engine_source_fingerprint()
        metadata = self.manifest | {'official': self.official, 'seed': engine.seed,
            'clock': engine.clock, 'engine_hash': source_hash, 'runtime': runtime_fingerprint(),
            'seats': sorted(self.sessions)}
        recorder = None
        faults = {p: [] for p in self.sessions}
        try:
            # Encoding/size faults are organizer faults, before a player clock
            # starts. Never truncate a large initial map or a seat's entities.
            for message in self.init_messages.values():
                if message.get('type') != 'init':
                    raise InfrastructureFault('missing initial handshake frame')
                downstream_pages(message)
            recorder = ReplayWriter(self.replay_path, metadata)
            for owner in sorted(self.sessions):
                result = self.sessions[owner].initialize(self.init_messages[owner])
                recorder.write('initialization', {'owner': owner, 'result': asdict(result)})
                if result.errors or result.custodial:
                    faults[owner].append(asdict(result))
            while engine.tick < engine.clock.total_ticks:
                tick, window = engine.tick, engine.clock.window(engine.tick)
                if not engine.clock.is_decision(tick):
                    raise InfrastructureFault('driver desynchronized from decision clock')
                # Materialize every observer's frame before invoking ANY seat.
                observations = {p: self.observe(p, tick) for p in sorted(self.sessions)}
                for observation in observations.values():
                    if (observation.get('type'), observation.get('window'), observation.get('tick'), observation.get('cycle')) != (
                            'obs', window, tick, engine.clock.cycle(tick)):
                        raise InfrastructureFault('observer supplied a mismatched decision frame')
                    downstream_pages(observation)
                orders, decisions = {}, {}
                for owner in sorted(self.sessions):
                    result = self.sessions[owner].decide(observations[owner])
                    orders[owner], decisions[owner] = result.orders, asdict(result)
                    if result.errors:
                        faults[owner].append(asdict(result))
                recorder.write('decision', {'tick': tick, 'window': window, 'orders': orders,
                    'observations': {p: fingerprint(o) for p, o in observations.items()}, 'sessions': decisions})
                for offset in range(engine.clock.decision_ticks):
                    result = engine.step(orders if offset == 0 else None)
                    # Phase hashes are optional for a fast run. The actual
                    # resulting state hash remains mandatory for replay audit.
                    recorder.write('tick', asdict(result) | {'state_hash': result.state_hash or fingerprint(engine.state.snapshot())})
            engine.state.audit()
            summary = dict(self.finalize())
            if self.official and not summary.get('full_rules_accepted'):
                raise InfrastructureFault('formal finalizer did not certify complete scoring collection')
            if engine_source_fingerprint() != source_hash:
                raise InfrastructureFault('engine source changed during the match')
            report = summary | {'valid': True, 'official': self.official, 'ticks': engine.tick,
                'state_hash': fingerprint(engine.state.snapshot()), 'faults': faults,
                'strategy_time': {p: {'spent_ns': round(s.clock.spent_seconds * 10**9),
                    'pool_remaining_ns': round(s.clock.pool_seconds * 10**9),
                    'timeouts': s.clock.timeouts, 'custodial': s.clock.custodial} for p, s in self.sessions.items()}}
            for owner, session in self.sessions.items():
                # No other seat's faults, private stderr, seed or hidden truth.
                session.finish({'type': 'end', 'tick': engine.tick, 'valid': True})
            recorder.finish(report)
            return report
        except Exception as exc:
            if recorder is not None:
                recorder.finish({'valid': False, 'official': self.official, 'tick': engine.tick,
                                 'reason': type(exc).__name__})
            raise
        finally:
            cleanup_errors = []
            try:
                for session in self.sessions.values():
                    try:
                        session.close()
                    except Exception as exc:
                        cleanup_errors.append(exc)
            finally:
                if recorder is not None:
                    recorder.close()
            if cleanup_errors:
                raise InfrastructureFault('one or more strategy sandboxes failed cleanup') from cleanup_errors[0]


def replay_decisions(path: Path, engine: Kernel) -> str:
    """Re-execute recorded accepted frames and check every resulting state."""
    records = iter(verify_replay(path))
    manifest = next(records)['payload']
    if (manifest.get('seed') != engine.seed or manifest.get('clock') != primitive(engine.clock)
            or manifest.get('engine_hash') != engine_source_fingerprint()
            or manifest.get('runtime') != runtime_fingerprint()):
        raise InfrastructureFault('replay requires the same engine, runtime, clock and seed')
    pending = None
    final = None
    for record in records:
        row = record['payload']
        if record['kind'] == 'initialization':
            if engine.tick or pending is not None:
                raise InfrastructureFault('late replay initialization')
        elif record['kind'] == 'decision':
            if (pending is not None or row['tick'] != engine.tick or not engine.clock.is_decision(engine.tick)
                    or row['window'] != engine.clock.window(engine.tick)
                    or set(row['orders']) != set(manifest['seats'])):
                raise InfrastructureFault('invalid replay decision ordering')
            pending = row['orders']
        elif record['kind'] == 'tick':
            if engine.clock.is_decision(engine.tick) and pending is None:
                raise InfrastructureFault('replay is missing a decision frame')
            result = engine.step(pending)
            pending = None
            actual = result.state_hash or fingerprint(engine.state.snapshot())
            if (result.tick != row['tick'] or actual != row['state_hash']
                    or row['stage_hashes'] and primitive(result.stage_hashes) != row['stage_hashes']):
                raise InfrastructureFault(f'replay simulation mismatch at tick {result.tick}')
        elif record['kind'] == 'end':
            final = row
        else:
            raise InfrastructureFault('unexpected replay record')
    if (pending is not None or final is None or not final.get('valid')
            or engine.tick != engine.clock.total_ticks or final['ticks'] != engine.tick
            or final['state_hash'] != fingerprint(engine.state.snapshot())):
        raise InfrastructureFault('incomplete or invalid match cannot be accepted as a replay')
    return final['state_hash']


def economic_development_messages(economy, owner: str, tick: int, clock) -> dict:
    """Explicit development observer, also usable by a complete assembly later."""
    return economy.observation(owner, tick=tick, window=clock.window(tick)) | {
        'type': 'obs', 'cycle': clock.cycle(tick)}


def economic_development_init(economy, owner: str, clock) -> dict:
    state = economy.s
    return {'type': 'init', 'protocol': 'rift_jsonl_v1', 'owner': owner,
        'rules_version': state.catalog.version, 'rules_hash': state.catalog.fingerprint,
        'scope': 'opening_economy_development', 'full_rules_accepted': False,
        'map': primitive(state.initial.map.public_map()), 'catalog': primitive(state.catalog.tables),
        'initial_observation': economic_development_messages(economy, owner, 0, clock),
        'clock': primitive(clock), 'time_budget': primitive(TimeBudget())}
