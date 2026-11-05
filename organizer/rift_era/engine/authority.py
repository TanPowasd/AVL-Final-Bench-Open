"""Independent territorial control, sovereignty and asset economic rights.

Occupation changes control only. Share transfers and operator appointments are
separate transactions, so military movement cannot confiscate neutral property.
Callers supply the actual garrison/contract facts; this book does not invent them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from .errors import InvariantViolation, RuleViolation
from .numeric import Q


@dataclass
class RegionAuthority:
    sovereign: str | None
    controller: str | None
    support_mU: int = 720
    occupied_since_tick: int | None = None
    claimant: str | None = None
    garrison_ticks: int = 0
    version: int = 0


@dataclass
class AssetRights:
    operator: str
    owner_shares: dict[str, int]
    pledged: dict[str, dict[str, int]] = field(default_factory=dict)
    version: int = 0

    def free_shares(self, owner):
        return self.owner_shares.get(owner, 0) - sum(p.get(owner, 0) for p in self.pledged.values())


class Authority:
    def __init__(self, world, assets, actors):
        self.world, self.actors = world, frozenset(actors)
        self.regions = {rid: RegionAuthority(r.sovereign, r.sovereign) for rid, r in world.regions.items()}
        self.assets = {aid: AssetRights(a.owner, {a.owner: 1000}) for aid, a in assets.items()}
        self.asset_hubs = {aid: a.hub for aid, a in assets.items()}
        self.last_control_tick = -1

    def controls_hub(self, owner, hub):
        site = self.world.hubs.get(hub)
        return site is not None and self.regions[site.region].controller == owner

    def sync_assets(self, physical):
        """Lifecycle registration only; existing shares never follow owner hints."""
        for aid, asset in physical.items():
            if aid not in self.assets:
                self.assets[aid] = AssetRights(asset.owner, {asset.owner: 1000})
                self.asset_hubs[aid] = asset.hub
        for aid in tuple(self.assets):
            if aid not in physical:
                if self.assets[aid].pledged:
                    raise InvariantViolation('pledged asset removed without collateral settlement')
                del self.assets[aid]
                del self.asset_hubs[aid]

    def advance_control(self, tick, *, sole_garrisons, hostile_defenders):
        """One tick of actual uncontested garrison, never a declaration of intent."""
        if tick <= self.last_control_tick:
            raise InvariantViolation('control advanced twice or backdated')
        if set(sole_garrisons) - self.regions.keys() or set(hostile_defenders) - self.regions.keys():
            raise InvariantViolation('control evidence references an unknown region')
        if any(p not in self.actors for p in sole_garrisons.values()):
            raise InvariantViolation('control evidence references an inactive actor')
        if self.last_control_tick >= 0 and tick != self.last_control_tick + 1:
            raise InvariantViolation('garrison evidence skipped simulation ticks')
        changed = []
        for rid, row in sorted(self.regions.items()):
            claimant = sole_garrisons.get(rid)
            if claimant is None or hostile_defenders.get(rid, False) or claimant == row.controller:
                row.claimant, row.garrison_ticks = None, 0
                continue
            row.garrison_ticks = row.garrison_ticks + 1 if row.claimant == claimant else 1
            row.claimant = claimant
            if row.garrison_ticks == 10:
                previous = row.controller
                row.controller = claimant
                row.support_mU = 550 if claimant == row.sovereign else 280
                row.occupied_since_tick = tick
                row.claimant, row.garrison_ticks = None, 0
                row.version += 1
                changed.append((rid, previous, claimant))
        self.last_control_tick = tick
        return tuple(changed)

    def cede(self, giver, recipient, region_ids):
        """Apply an already signed cession; asset settlement remains separate."""
        if giver == recipient or giver not in self.actors or recipient not in self.actors:
            raise RuleViolation('ERR_SCHEMA')
        if not region_ids or len(set(region_ids)) != len(region_ids):
            raise RuleViolation('ERR_SCHEMA')
        if any(r not in self.regions or self.regions[r].sovereign != giver for r in region_ids):
            raise RuleViolation('ERR_NO_PERMISSION')
        for rid in region_ids:
            row = self.regions[rid]
            row.sovereign = recipient
            row.version += 1

    def pledge(self, asset_id, contract_id, owner, shares):
        row = self.assets.get(asset_id)
        if row is None:
            raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        if type(shares) is not int or shares <= 0 or contract_id in row.pledged:
            raise RuleViolation('ERR_SCHEMA')
        if shares > row.free_shares(owner):
            raise RuleViolation('ERR_INSUFFICIENT_STOCK', 'shares already pledged or not owned')
        row.pledged[contract_id] = {owner: shares}
        row.version += 1

    def transfer_shares(self, asset_id, source, destination, shares, *, collateral_id=None):
        row = self.assets.get(asset_id)
        if row is None:
            raise RuleViolation('ERR_NO_VISIBLE_TARGET')
        if source == destination or destination not in self.actors or type(shares) is not int or shares <= 0:
            raise RuleViolation('ERR_SCHEMA')
        pledged = row.pledged.get(collateral_id, {}).get(source, 0) if collateral_id is not None else 0
        available = pledged if collateral_id is not None else row.free_shares(source)
        if shares > available:
            raise RuleViolation('ERR_INSUFFICIENT_STOCK')
        row.owner_shares[source] -= shares
        if not row.owner_shares[source]:
            del row.owner_shares[source]
        row.owner_shares[destination] = row.owner_shares.get(destination, 0) + shares
        if collateral_id is not None:
            row.pledged[collateral_id][source] -= shares
            if not row.pledged[collateral_id][source]:
                del row.pledged[collateral_id][source]
            if not row.pledged[collateral_id]:
                del row.pledged[collateral_id]
        row.version += 1

    def military_operating_factor(self, asset_id, at_war):
        row = self.assets[asset_id]
        region = self.regions[self.world.hubs[self.asset_hubs[asset_id]].region]
        controller = region.controller
        if controller is None or controller == row.operator or row.operator not in self.actors:
            return Q(1)
        # Third-party ownership is preserved. The military administration cap
        # applies only to enemy-operated, wholly enemy-owned national assets.
        if at_war(controller, row.operator) and all(p in self.actors and at_war(controller, p) for p in row.owner_shares):
            return Q(3, 5)
        return Q(1)

    def audit(self):
        for row in self.regions.values():
            if not 0 <= row.support_mU <= 1000 or not 0 <= row.garrison_ticks < 10:
                raise InvariantViolation('invalid control/support state')
            if row.controller is not None and row.controller not in self.actors:
                raise InvariantViolation('unknown military controller')
        for row in self.assets.values():
            if sum(row.owner_shares.values()) != 1000 or any(type(q) is not int or q <= 0 for q in row.owner_shares.values()):
                raise InvariantViolation('asset shares do not conserve 1000 mU')
            if row.operator not in self.actors | {'world'} or set(row.owner_shares) - (self.actors | {'world'}):
                raise InvariantViolation('unknown economic rights holder')
            if any(row.free_shares(owner) < 0 for owner in row.owner_shares):
                raise InvariantViolation('asset shares pledged more than once')
            if any(set(p) - row.owner_shares.keys() or any(q <= 0 for q in p.values()) for p in row.pledged.values()):
                raise InvariantViolation('invalid collateral custody')

    def snapshot(self):
        return {'regions': self.regions, 'assets': self.assets, 'asset_hubs': self.asset_hubs,
                'last_control_tick': self.last_control_tick}
