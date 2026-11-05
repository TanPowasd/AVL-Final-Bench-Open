"""Actual embodied material follows assets through construction and destruction."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from fractions import Fraction

from .assets import Asset
from .capital_book import proportional_loss, CAPITAL_BOOK_SCALE
from .errors import InvariantViolation
from .ledger import Account, Inventory
from .materials import Origin
from .numeric import Q, floor
from .provenance import Use


@dataclass(frozen=True)
class AssetLoss:
    event_id: str
    tick: int
    asset_id: str
    owner: str
    value: Fraction
    removed_nmU: dict[str, Fraction]
    salvage_mU: dict[str, int]


class AssetLifecycle:
    def __init__(self, inventory: Inventory, assets: dict[str, Asset], weights, *, initial: bool = False,economic_owner=None):
        self.inventory, self.assets, self.weights = inventory, assets, weights
        self.economic_owner=economic_owner
        self.tick = -1
        self.events: list[AssetLoss] = []
        self.seen: set[tuple[str, str]] = set()
        if initial:
            draft = inventory.materials.draft()
            for asset in assets.values():
                for good, amount in asset.book_nmU.items():
                    title=economic_owner(asset.id) if economic_owner else None
                    draft.create(self.account(asset, good), {Origin(asset.owner, asset.hub,economic_owner=title): amount / 1000}, virtual=True)
            inventory.transform({}, {}, reason='declared_initial_capital', material_plan=draft.finish())

    @staticmethod
    def account(asset: Asset, good: str) -> Account:
        kind = 'asset_wip' if asset.under_construction else 'capital'
        return Account(asset.owner, asset.hub, f'{kind}:{asset.id}', good)

    def begin_tick(self, tick: int) -> None:
        if tick <= self.tick:
            raise InvariantViolation('asset lifecycle tick repeated or backdated')
        self.tick = tick
        self.events.clear()
        self.seen.clear()

    def virtual_stock(self) -> dict[Account, Fraction]:
        return {self.account(asset, good): amount / 1000 for asset in self.assets.values()
                for good, amount in asset.book_nmU.items() if amount}

    def audit(self) -> None:
        for asset in self.assets.values():
            if not 0 <= asset.health_mU <= 1000:
                raise InvariantViolation('invalid asset health')
            for good, amount in asset.book_nmU.items():
                if not 0 <= amount <= asset.original_nmU.get(good, 0) or CAPITAL_BOOK_SCALE % Q(amount).denominator:
                    raise InvariantViolation('asset book exceeds original capital')
                if self.inventory.materials.quantity(self.account(asset, good)) != amount / 1000:
                    raise InvariantViolation('asset book differs from embodied material')

    def prepare_investment(self, aid: str, inputs: dict[Account, int], *, repair: bool):
        asset = self.assets[aid]
        quantities = defaultdict(int)
        draft = self.inventory.materials.draft()
        for source, amount in inputs.items():
            if source.owner != asset.owner or source.location != asset.hub:
                raise InvariantViolation('capital input has wrong ownership or location')
            quantities[source.asset] += amount
            if asset.book_nmU.get(source.asset, Q(0)) + quantities[source.asset] * 1000 > asset.original_nmU.get(source.asset, 0):
                raise InvariantViolation('capital input exceeds original material vector')
            pieces = draft.take(source, Q(amount))
            capital = self.account(asset, source.asset)
            draft.deliver(source,capital,pieces,virtual=True,retitle=self.economic_owner is not None,
                          economic_owner=self.economic_owner(aid) if self.economic_owner else None)
            if repair:
                draft.capitalize(capital, pieces, use=Use.REPAIR, reason='asset_repair')
        return self.inventory.prepare_transform(inputs, {}, reason='asset_investment', material_plan=draft.finish())

    def commission(self, aid: str) -> None:
        asset = self.assets[aid]
        if not asset.under_construction or asset.book_nmU != {g: Q(q) for g, q in asset.original_nmU.items()}:
            raise InvariantViolation('commissioning incomplete or already operational asset')
        draft = self.inventory.materials.draft()
        for good, amount in asset.book_nmU.items():
            source = self.account(asset, good)
            destination = Account(asset.owner, asset.hub, f'capital:{aid}', good)
            pieces = draft.take(source, amount / 1000)
            draft.add(destination, pieces, virtual=True)
            draft.capitalize(destination, pieces, use=Use.CONSTRUCTION, reason='asset_commissioned')
        self.inventory.transform({}, {}, reason='asset_commissioned', material_plan=draft.finish())
        asset.under_construction = False
        asset.version += 1

    def damage(self, aid: str, amount_mU: int, *, event_id: str) -> AssetLoss:
        if (event_id, aid) in self.seen:
            raise InvariantViolation('asset damage event repeated')
        asset = self.assets[aid]
        if type(amount_mU) is not int or amount_mU < 0:
            raise InvariantViolation('invalid damage amount')
        damage = min(asset.health_mU, amount_mU)
        removed = proportional_loss(asset.book_nmU, damage, asset.health_mU)
        draft = self.inventory.materials.draft()
        for good, amount in removed.items():
            draft.consume(self.account(asset, good), amount / 1000, use=Use.DESTRUCTION, reason='asset_damage')
        self.inventory.transform({}, {}, reason='asset_damage', material_plan=draft.finish())
        asset.damage(damage)
        result = AssetLoss(event_id, self.tick, aid, asset.owner,
                           sum((q / 1000000 * self.weights[g] for g, q in removed.items()), Q(0)), removed, {})
        self.seen.add((event_id, aid))
        self.events.append(result)
        return result

    def dismantle(self, aid: str, *, event_id: str, salvage: bool = True) -> AssetLoss:
        if (event_id, aid) in self.seen:
            raise InvariantViolation('asset dismantling event repeated')
        asset = self.assets[aid]
        removed, outputs = dict(asset.book_nmU), {}
        draft = self.inventory.materials.draft()
        for good, amount in removed.items():
            pieces = draft.consume(self.account(asset, good), amount / 1000, use=Use.DESTRUCTION, reason='asset_dismantled')
            if salvage and good in ('steel', 'copper') and amount:
                # The material is relabeled as scrap; its original nation/path
                # is retained. No recursive recovery of metal inside components.
                quantity = floor(amount / 2000)
                if quantity:
                    from .materials import split_origins
                    target = Account(asset.owner, asset.hub, 'next_available', f'scrap_{good}')
                    distribution = split_origins(Q(quantity), pieces, capped=False,
                        key=(self.inventory.materials.seed, 'salvage_origin', self.tick, aid, good))
                    if self.economic_owner is not None:
                        from dataclasses import replace
                        titled=defaultdict(Fraction)
                        for origin,q in distribution.items():titled[replace(origin,economic_owner=self.economic_owner(aid))]+=q
                        distribution=dict(titled)
                    draft.create(target, distribution)
                    outputs[target] = quantity
        self.inventory.transform({}, outputs, reason='asset_dismantled', material_plan=draft.finish())
        value = sum((q / 1000000 * self.weights[g] for g, q in removed.items()), Q(0))
        value -= sum((Q(q, 1000) * self.weights[a.asset] for a, q in outputs.items()), Q(0))
        asset.book_nmU = {g: Q(0) for g in asset.book_nmU}
        asset.health_mU = 0
        asset.version += 1
        result = AssetLoss(event_id, self.tick, aid, asset.owner, value, removed, {a.asset: q for a, q in outputs.items()})
        self.seen.add((event_id, aid))
        self.events.append(result)
        return result

    def snapshot(self) -> dict:
        return {'tick': self.tick, 'events': self.events, 'seen': sorted(self.seen)}
