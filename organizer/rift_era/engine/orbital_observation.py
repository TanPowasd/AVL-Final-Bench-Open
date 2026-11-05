"""Approved N06 ground-control observation of foreign orbital slots.

Queries return a cached whitelist. In particular, resolving a received contact
never probes whether its hidden physical target still exists at command time.
"""
from copy import deepcopy

from .errors import RuleViolation
from .numeric import Q

ORBITAL_OBSERVATION_VERSION = 'orbital_target_visibility_v1'


class OrbitalObservation:
    def __init__(self, state):
        self.s = state
        owners = tuple(state.initial.known)
        self.sequences = dict.fromkeys(owners, 0)
        self.current = {owner: {} for owner in owners}
        self.covered = dict.fromkeys(owners, False)
        self.events = {owner: {'lost_contact_ids': set(), 'deleted_ids': set()} for owner in owners}
        self.views = {}
        self.publish(0)

    def has_coverage(self, owner):
        if 'N06' not in self.s.research[owner].known:
            return False
        return any(system.owner == owner and system.kind in ('launch_site', 'fusion_sensor')
                   and system.online and self.s.systems.availability(system) > 0
                   for system in self.s.systems.instances.values())

    def begin_tick(self, ctx):
        if ctx.clock.is_decision(ctx.tick):
            for events in self.events.values():
                for values in events.values():
                    values.clear()

    def publish(self, tick):
        for owner, previous in self.current.items():
            covered = self.has_coverage(owner)
            current = {}
            if covered:
                # The slot is public under this observation permission. Never
                # order aliases by foreign internal IDs or issuer command seq.
                for satellite in sorted(self.s.orbit.satellites.values(), key=lambda s: s.berth):
                    if satellite.owner == owner:
                        continue
                    old = previous.get(satellite.id)
                    if old is None:
                        self.sequences[owner] += 1
                        alias = f'orbital-contact-{self.sequences[owner]}'
                    else:
                        alias = old['id']
                    current[satellite.id] = {
                        'id': alias, 'owner': satellite.owner,
                        'orbit_plane': satellite.berth[0], 'slot': satellite.berth[1], 'berth': satellite.berth[2],
                        'quality_mU': 800, 'observed_tick': tick,
                    }
            missing = set(previous) - set(current)
            for key in missing:
                disappeared = covered and self.covered[owner] and key not in self.s.orbit.satellites
                event = 'deleted_ids' if disappeared else 'lost_contact_ids'
                self.events[owner][event].add(previous[key]['id'])
            self.current[owner] = current
            self.covered[owner] = covered
            self.views[owner] = {
                'coverage': covered,
                'contacts': [dict(row) for row in current.values()],
                **{key: sorted(values) for key, values in self.events[owner].items()},
            }

    def advance(self, ctx):
        self.publish(ctx.tick + 1)

    def visible(self, owner):
        return deepcopy(self.views[owner])

    def resolve(self, owner, alias, *, minimum_quality=Q(1, 2)):
        for target, row in self.current[owner].items():
            if row['id'] == alias and Q(row['quality_mU'], 1000) >= minimum_quality:
                return target, deepcopy(row)
        raise RuleViolation('ERR_NO_VISIBLE_TARGET')

    def snapshot(self):
        return {'version': ORBITAL_OBSERVATION_VERSION, 'sequences': self.sequences,
                'current': self.current, 'covered': self.covered, 'events': self.events, 'views': self.views}
