"""Organizer development CLI. Never emits a provisional formal game score."""

import argparse
from contextlib import ExitStack
import json
from pathlib import Path

from .engine import ENGINE_VERSION
from .engine.catalog import Catalog
from .engine.determinism import canonical, primitive, runtime_fingerprint, engine_source_fingerprint
from .engine.errors import InfrastructureFault
from .engine.release import PENDING_RELEASE_GATES, require_official_readiness
from .engine.replay import ReplayWriter, verify_replay
from .engine.scenarios import supply_chain, replay_supply_chain
from .engine.world import generate_candidate, MAP_VERSION
from .engine.initialization import initialize
from .engine.determinism import fingerprint
from .engine.bootstrap import run_opening, accepted_atlas_world
from .engine import world_atlas

MAPS = ('atlas', 'legacy')


def candidate_world(seed: int, kind: str, attempt: int = 0):
    """An unaccepted candidate: world_atlas_v1 (main line) or the legacy map."""
    return world_atlas.generate_candidate(seed, attempt) if kind == 'atlas' else generate_candidate(seed, attempt)


def match_world(seed: int, kind: str):
    """The world a match runs on: atlas candidates pass the real 150-tick gate."""
    return accepted_atlas_world(seed) if kind == 'atlas' else generate_candidate(seed)


def replay_world(manifest: dict):
    version = manifest.get('map_version', MAP_VERSION)
    if version == world_atlas.VERSION:
        return world_atlas.generate_candidate(manifest['seed'], manifest.get('map_attempt', 0))
    if version != MAP_VERSION:
        raise InfrastructureFault('replay map version mismatch')
    return generate_candidate(manifest['seed'])


def main() -> int:
    parser = argparse.ArgumentParser(description='裂陆纪元正式引擎开发工具')
    parser.add_argument('--official', action='store_true')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('check')
    world = commands.add_parser('world', help='construct an unaccepted spatial candidate and the major-nation starting template')
    world.add_argument('--seed', type=int, default=1000)
    world.add_argument('--map', choices=MAPS, default='atlas')
    world.add_argument('--attempt', type=int, default=0)
    bootstrap = commands.add_parser('bootstrap', help='execute the 150-tick opening economy and report its actual invariants')
    bootstrap.add_argument('--seed', type=int, default=1000)
    bootstrap.add_argument('--audit-every-phase', action='store_true')
    bootstrap.add_argument('--map', choices=MAPS, default='atlas')
    bootstrap.add_argument('--attempt', type=int, default=0)
    sandbox = commands.add_parser('sandbox-check', help='inspect the target cgroup delegation without running a submission')
    sandbox.add_argument('--cgroup', type=Path, required=True)
    strategy = commands.add_parser('strategy-opening', help='drive eight sandboxed submissions in the bounded development economy; no formal score')
    strategy.add_argument('--submissions', nargs=8, type=Path, required=True)
    strategy.add_argument('--cgroup', type=Path, required=True)
    strategy.add_argument('--replay', type=Path, required=True)
    strategy.add_argument('--seed', type=int, default=1000)
    strategy.add_argument('--ticks', type=int, default=150)
    strategy.add_argument('--audit-every-phase', action='store_true')
    strategy.add_argument('--map', choices=MAPS, default='atlas')
    full = commands.add_parser('match', help='run a full_match with empty commands and print the finalizer report (development; no formal score)')
    full.add_argument('--seed', type=int, default=1000)
    full.add_argument('--ticks', type=int, default=28_800)
    full.add_argument('--map', choices=MAPS, default='atlas')
    full.add_argument('--audit-every-phase', action='store_true')
    full.add_argument('--progress', type=int, default=0, help='print elapsed time every N ticks to stderr')
    full.add_argument('--checkpoint', type=Path, help='write a resumable checkpoint here (development, not a replay)')
    full.add_argument('--checkpoint-every', type=int, default=500, help='ticks between checkpoints')
    full.add_argument('--resume', action='store_true', help='continue from --checkpoint if it exists')
    full.add_argument('--budget-s', type=float, default=0, help='stop after this many wall seconds, checkpoint, exit 3')
    strategy_match = commands.add_parser('strategy-match', help='drive eight sandboxed submissions through a full_match; no formal score')
    strategy_match.add_argument('--submissions', nargs=8, type=Path, required=True)
    strategy_match.add_argument('--cgroup', type=Path, required=True)
    strategy_match.add_argument('--replay', type=Path, required=True)
    strategy_match.add_argument('--seed', type=int, default=1000)
    strategy_match.add_argument('--ticks', type=int, default=28_800)
    strategy_match.add_argument('--map', choices=MAPS, default='atlas')
    match_replay = commands.add_parser('replay-strategy-opening', help='re-execute a development strategy replay and compare all tick hashes')
    match_replay.add_argument('path', type=Path)
    demo = commands.add_parser('scenario', help='run the integrated supply-chain development fixture')
    demo.add_argument('--seed', type=int, default=1)
    demo.add_argument('--ticks', type=int, default=100)
    demo.add_argument('--decision-ticks', type=int, default=5)
    demo.add_argument('--replay', type=Path)
    replay = commands.add_parser('verify-replay')
    replay.add_argument('path', type=Path)
    rerun = commands.add_parser('replay', help='re-execute the supply-chain fixture and verify every phase hash')
    rerun.add_argument('path', type=Path)
    args = parser.parse_args()
    try:
        if args.official:
            require_official_readiness()
        if args.command == 'sandbox-check':
            from .engine.sandbox import Sandbox
            Sandbox(args.cgroup).preflight()
            print(json.dumps({'delegation_preflight': True, 'full_sandbox_acceptance': False}))
            return 0
        if args.command == 'bootstrap':
            report = run_opening(args.seed, debug=args.audit_every_phase, world=candidate_world(args.seed, args.map, args.attempt))
            print(canonical(report).decode('utf-8'))
            return 0 if report.passed_core_checks else 2
        if args.command == 'strategy-opening':
            from .engine.archive import extracted_submission
            from .engine.driver import MatchDriver, economic_development_init, economic_development_messages
            from .engine.economy import Economy, opening_economy
            from .engine.ipc import StrategySession
            from .engine.sandbox import Sandbox, runtime_mounts
            launcher = Sandbox(args.cgroup)
            launcher.preflight()
            launcher.mounts = runtime_mounts()
            catalog = Catalog()
            map_world = match_world(args.seed, args.map)
            game = opening_economy(catalog, initialize(catalog, map_world),
                total_ticks=args.ticks, debug=args.audit_every_phase, commerce=True)
            economy = Economy(game.state)
            with ExitStack() as stack:
                sessions = {}
                for owner, archive in zip(sorted(game.state.initial.known), args.submissions):
                    submission = stack.enter_context(extracted_submission(archive))
                    sessions[owner] = stack.enter_context(StrategySession.launch(launcher, submission))
                driver = MatchDriver(game, sessions,
                    init_messages={p: economic_development_init(economy, p, game.clock) for p in sessions},
                    observe=lambda p, t: economic_development_messages(economy, p, t, game.clock),
                    finalize=lambda: {'scope': 'opening_economy_development', 'full_rules_accepted': False},
                    replay=args.replay, manifest={'profile': 'opening_economy_development', 'rules_hash': catalog.fingerprint,
                        'map_version': map_world.generator_version, 'map_attempt': map_world.attempt})
                print(canonical(driver.run()).decode('utf-8'))
            return 0
        if args.command == 'match':
            import sys, time
            from .engine.economy import full_match
            from .engine.match import finalize_match
            from .engine import checkpoint
            catalog = Catalog()
            map_world = match_world(args.seed, args.map)
            if args.resume and args.checkpoint and args.checkpoint.exists():
                head = checkpoint.read_header(args.checkpoint)
                if head['world_seed'] != args.seed or head['map_version'] != map_world.generator_version:
                    raise InfrastructureFault('checkpoint belongs to another seed or map')
                game, _ = checkpoint.resume(args.checkpoint, catalog, total_ticks=args.ticks,
                                            debug=args.audit_every_phase)
            else:
                game = full_match(catalog, initialize(catalog, map_world), total_ticks=args.ticks,
                                  debug=args.audit_every_phase, commerce=True)
            def save():
                checkpoint.save(game, args.checkpoint, rules_hash=catalog.fingerprint, world_seed=args.seed,
                                map_version=map_world.generator_version)
            start = time.monotonic()
            while game.tick < game.clock.total_ticks:
                game.step()
                if args.progress and game.tick % args.progress == 0:
                    print(json.dumps({'tick': game.tick, 'elapsed_s': round(time.monotonic() - start, 1)}),
                          file=sys.stderr, flush=True)
                if args.checkpoint and game.tick % args.checkpoint_every == 0:
                    save()
                if args.budget_s and time.monotonic() - start >= args.budget_s and game.tick < game.clock.total_ticks:
                    if not args.checkpoint:
                        raise InfrastructureFault('--budget-s needs --checkpoint')
                    save()
                    print(json.dumps({'paused_at_tick': game.tick, 'checkpoint': str(args.checkpoint)}))
                    return 3
            report = finalize_match(game) | {'seed': args.seed, 'map_version': map_world.generator_version,
                'map_attempt': map_world.attempt, 'ticks': game.tick, 'official': False,
                'state_hash': fingerprint(game.state.snapshot()), 'wall_seconds': round(time.monotonic() - start, 1)}
            print(canonical(report).decode('utf-8'))
            return 0
        if args.command == 'strategy-match':
            from .engine.archive import extracted_submission
            from .engine.driver import MatchDriver, economic_development_init, economic_development_messages
            from .engine.economy import Economy, full_match
            from .engine.ipc import StrategySession
            from .engine.match import finalize_match
            from .engine.sandbox import Sandbox, runtime_mounts
            launcher = Sandbox(args.cgroup)
            launcher.preflight()
            launcher.mounts = runtime_mounts()
            catalog = Catalog()
            map_world = match_world(args.seed, args.map)
            game = full_match(catalog, initialize(catalog, map_world), total_ticks=args.ticks, commerce=True)
            economy = Economy(game.state)
            with ExitStack() as stack:
                sessions = {}
                for owner, archive in zip(sorted(game.state.initial.known), args.submissions):
                    submission = stack.enter_context(extracted_submission(archive))
                    sessions[owner] = stack.enter_context(StrategySession.launch(launcher, submission))
                driver = MatchDriver(game, sessions,
                    init_messages={p: economic_development_init(economy, p, game.clock) for p in sessions},
                    observe=lambda p, t: economic_development_messages(economy, p, t, game.clock),
                    finalize=lambda: finalize_match(game),
                    replay=args.replay, manifest={'profile': 'full_match_development', 'rules_hash': catalog.fingerprint,
                        'map_version': map_world.generator_version, 'map_attempt': map_world.attempt})
                print(canonical(driver.run()).decode('utf-8'))
            return 0
        if args.command == 'replay-strategy-opening':
            from .engine.driver import replay_decisions
            from .engine.economy import opening_economy
            records = verify_replay(args.path)
            manifest = next(records)['payload']
            records.close()
            catalog = Catalog()
            if manifest.get('profile') != 'opening_economy_development' or manifest.get('rules_hash') != catalog.fingerprint:
                raise InfrastructureFault('replay profile or rules hash mismatch')
            game = opening_economy(catalog, initialize(catalog, replay_world(manifest)),
                total_ticks=manifest['clock']['total_ticks'], debug=True, commerce=True)
            print(canonical({'official': False, 'state_hash': replay_decisions(args.path, game),
                             'verification': 'reexecuted_strategy_windows_and_ticks'}).decode('utf-8'))
            return 0
        if args.command == 'verify-replay':
            count = sum(1 for _ in verify_replay(args.path))
            print(json.dumps({'verified_records': count, 'verification': 'hash_chain_only'}))
            return 0
        if args.command == 'replay':
            result = replay_supply_chain(args.path)
            print(json.dumps({'state_hash': result, 'verification': 'reexecuted_every_phase', 'official': False}))
            return 0
        catalog = Catalog()
        if args.command == 'check':
            print(json.dumps({'engine_version': ENGINE_VERSION, 'rules_version': catalog.version,
                'rules_hash': catalog.fingerprint, 'research_nodes': len(catalog.technologies),
                'engine_hash': engine_source_fingerprint(),
                'capability_milestones': len(catalog.milestones), 'goods': len(catalog.goods),
                'recipes': len(catalog.recipes), 'official_ready': False,
                'pending_release_gates': PENDING_RELEASE_GATES}, ensure_ascii=False, indent=2))
            return 0
        if args.command == 'world':
            world = candidate_world(args.seed, args.map, args.attempt)
            state = initialize(catalog, world)
            print(json.dumps({'map_version': world.generator_version, 'map_attempt': world.attempt,
                  'resource_sites': len(world.hidden_resources), 'official': False, 'bootstrap_accepted': world.accepted,
                  'regions': len(world.regions), 'hubs': len(world.hubs), 'routes': len(world.routes),
                  'city_states': len(world.city_states), 'initial_lines': len(state.lines),
                  'initial_plants': len(state.plants), 'initial_mines': len(state.mines),
                  'initial_major_civilians_mU': sum(p.total_mU for p in state.populations.values()),
                  'initial_major_military_mU': sum(u.crew_mU for u in state.units.values()),
                  'state_hash': fingerprint(state.snapshot())}, ensure_ascii=False, indent=2))
            return 0
        engine = supply_chain(args.seed, total_ticks=args.ticks, decision_ticks=args.decision_ticks)
        manifest = {'scenario': 'supply_chain_v1', 'scope': 'component_integration_fixture',
                    'official': False, 'seed': args.seed, 'clock': engine.clock,
                    'engine_version': ENGINE_VERSION, 'rules_hash': catalog.fingerprint,
                    'engine_hash': engine_source_fingerprint(),
                    'runtime': runtime_fingerprint()}
        recorder = ReplayWriter(args.replay, manifest) if args.replay else None
        try:
            for _ in range(args.ticks):
                result = engine.step()
                if recorder:
                    recorder.write('tick', primitive(result))
            if recorder:
                recorder.finish({'ticks': engine.tick, 'state_hash': result.state_hash})
        finally:
            if recorder:
                recorder.close()
        print(canonical({'scope': 'component_integration_fixture', 'official': False,
                         'ticks': engine.tick, 'state_hash': result.state_hash,
                         'observation': engine.state.observed}).decode('utf-8'))
        return 0
    except (InfrastructureFault, ValueError, OSError) as exc:
        print(json.dumps({'error': type(exc).__name__, 'detail': str(exc)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
