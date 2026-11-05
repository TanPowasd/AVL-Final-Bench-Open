"""Observer-local aliases and frozen contacts; hidden mutations never touch views."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

from .errors import InfrastructureFault
from .numeric import Q, floor, ceil


@dataclass(frozen=True)
class Sighting:
    internal_id: str  # trusted sensor input, NEVER serialized
    observed_tick: int
    source: str
    region: str
    quality_mU: int
    estimated_low_mU: int
    estimated_high_mU: int
    domain: str
    confirmed_destroyed: bool = False
    owner: str | None = None
    estimated_center_mU: int | None = None


class Observer:
    def __init__(self, nation: str, max_tracks: int = 8192):
        self.nation = nation
        self.max_tracks = max_tracks
        self.sequence = 0
        self._aliases: dict[str, str] = {}
        self._tracks: dict[str, dict] = {}
        self._visible: set[str] = set()

    def alias(self, internal_id: str) -> str:
        """Called only by the sensor update, never by observation queries."""
        if internal_id not in self._aliases:
            self.sequence += 1
            self._aliases[internal_id] = f'contact-{self.sequence}'
        return self._aliases[internal_id]

    def update(self, tick: int, sightings: tuple[Sighting, ...]) -> dict:
        """Only legal sensor inputs enter here, never the unfiltered entity map.

        Reidentification is explicit: sensors supply a fresh internal trajectory
        key when continuous identification has been lost. The observer cannot
        infer hidden identities from a global creation counter.
        """
        visible, changed, deleted, expired = set(), [], [], []
        for sight in sorted(sightings, key=lambda s: (s.observed_tick, s.region, s.source, s.internal_id)):
            if not 0 < sight.quality_mU <= 950 or sight.observed_tick > tick:
                raise InfrastructureFault('invalid sensor output')
            alias = self.alias(sight.internal_id)
            visible.add(alias)
            if sight.confirmed_destroyed:
                deleted.append(alias)
                self._tracks.pop(alias, None)
                continue
            row = {'id': alias, 'source': sight.source, 'observed_tick': sight.observed_tick,
                   'last_region': sight.region, 'quality_mU': sight.quality_mU,
                   'estimated_low_mU': sight.estimated_low_mU, 'estimated_high_mU': sight.estimated_high_mU,
                   'domain': sight.domain, 'owner': sight.owner,
                   'estimated_center_mU': sight.estimated_center_mU if sight.estimated_center_mU is not None else
                                          (sight.estimated_low_mU+sight.estimated_high_mU)//2}
            previous = self._tracks.get(alias)
            if previous is not None and previous['domain'] != sight.domain:
                raise InfrastructureFault('trajectory domain changed without a new identification')
            compare = {k: v for k, v in previous.items() if k != 'version'} if previous else None
            if compare != row:
                row['version'] = previous['version'] + 1 if previous else 1
                self._tracks[alias] = row
                changed.append(dict(row))
        lost = self._visible - visible
        over = max(0, len(self._tracks) - self.max_tracks)
        old = sorted((row for alias, row in self._tracks.items() if alias not in visible),
                     key=lambda row: (row['observed_tick'], row['id']))
        if over > len(old):
            raise InfrastructureFault('current visible tracks exceed internal capacity')
        for row in old[:over]:
            expired.append(row['id'])
            del self._tracks[row['id']]
        retired = set(deleted) | set(expired)
        self._aliases = {internal: alias for internal, alias in self._aliases.items() if alias not in retired}
        self._visible = visible - retired
        return {'tracks': changed, 'deleted_ids': sorted(deleted),
                'lost_contact_ids': sorted(lost - retired), 'expired_track_ids': sorted(expired)}

    def retire(self, alias: str) -> None:
        """Remove a positively identified false signal, not a physical death."""
        self._tracks.pop(alias, None)
        self._visible.discard(alias)
        self._aliases = {key: value for key, value in self._aliases.items() if value != alias}

    def snapshot(self) -> dict:
        return {'nation': self.nation, 'tracks': [deepcopy(self._tracks[k]) for k in sorted(self._tracks)]}

    def view(self, tick: int, domain_speeds, *, cycle_ticks: int = 10) -> dict:
        """Age frozen observations without reading current hidden entities."""
        rows=[]
        for alias, frozen in sorted(self._tracks.items()):
            row=deepcopy(frozen)
            age=max(0,tick-frozen['observed_tick']) if alias not in self._visible else 0
            distance=domain_speeds[frozen['domain']]*Q(age,cycle_ticks)
            quality=Q(frozen['quality_mU'],1000)/(1+distance)
            width=ceil(frozen['estimated_center_mU']*(Q(15,100)*(1-Q(frozen['quality_mU'],1000))+Q(age,50)))
            row.update(quality_mU=floor(1000*quality),position_radius_hops=ceil(distance),
                       estimated_low_mU=max(0,frozen['estimated_center_mU']-width),
                       estimated_high_mU=frozen['estimated_center_mU']+width,
                       currently_visible=alias in self._visible)
            rows.append(row)
        return {'nation':self.nation,'tracks':rows}

    def expire(self, tick: int, domain_speeds, *, cycle_ticks: int = 10) -> tuple[str,...]:
        expired=[]
        for alias,row in tuple(self._tracks.items()):
            if alias in self._visible:continue
            distance=domain_speeds[row['domain']]*Q(max(0,tick-row['observed_tick']),cycle_ticks)
            if Q(row['quality_mU'],1000)/(1+distance)<Q(1,20):
                expired.append(alias);del self._tracks[alias]
        retired=set(expired)
        self._aliases={key:alias for key,alias in self._aliases.items() if alias not in retired}
        return tuple(sorted(expired))

    def state_snapshot(self) -> dict:
        return {'nation':self.nation,'sequence':self.sequence,'aliases':self._aliases,
                'tracks':self._tracks,'visible':self._visible}
