"""Read-only rule catalogs. JSON formula strings are documentation, never code."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from types import MappingProxyType
from collections.abc import Mapping

from .determinism import fingerprint
from .errors import InfrastructureFault

RULE_FILES = ("technology", "industry", "coupling", "scoring", "runtime_rules", "military")
DEFAULT_DATA = Path(__file__).resolve().parents[3] / "designs" / "rift_era" / "data"


def freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({k: freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(map(freeze, value))
    if isinstance(value, Decimal):
        return Fraction(value)
    return value


def unique(rows, label: str) -> Mapping:
    result = {r["id"]: r for r in rows}
    if len(result) != len(rows):
        raise InfrastructureFault(f"duplicate {label} ID")
    return MappingProxyType(result)


class Catalog:
    def __init__(self, directory: Path = DEFAULT_DATA) -> None:
        tables, hashes = {}, {}
        for name in RULE_FILES:
            raw = (directory / f"{name}.json").read_bytes()
            hashes[name] = hashlib.sha256(raw).hexdigest()
            tables[name] = freeze(json.loads(raw, parse_float=Decimal))
        self.tables = MappingProxyType(tables)
        self.hashes = MappingProxyType(hashes)
        self.fingerprint = fingerprint(hashes)
        self.version = tables["runtime_rules"]["version"]
        self.goods = unique(tables["industry"]["goods"], "good")
        self.recipes = unique(tables["industry"]["recipes"], "recipe")
        self.profiles = unique(tables["industry"]["facility_profiles"], "profile")
        self.modes = unique(tables["industry"]["line_modes"], "mode")
        self.technologies = unique(tables["technology"]["technologies"], "tech")
        self.milestones = unique(tables["technology"]["capability_milestones"], "milestone")
        self.units = tables["military"]["units"]
        self.weights = tables["scoring"]["shadow_weights"]["w_cat"]
        self.runtime = tables["runtime_rules"]
        self._validate()

    def _validate(self) -> None:
        def require(condition, message):
            if not condition:
                raise InfrastructureFault(f"invalid catalog: {message}")

        all_tech = dict(self.technologies) | dict(self.milestones)
        require(len(all_tech) == len(self.technologies) + len(self.milestones), "tech collision")
        require(set(self.weights) == set(self.goods), "shadow weight coverage")
        require(all(w >= 0 for w in self.weights.values()), "negative shadow weight")
        whole = {g for g, row in self.goods.items() if row.get("indivisible", False)}
        require(whole == set(self.runtime["numeric"]["whole_goods"]), "whole-good cross-table mismatch")
        for name in ("scoring", "coupling", "military"):
            require(self.tables[name]["version"] == self.version, f"{name} version mismatch")
        pending = set(all_tech)
        while pending:
            ready = {t for t in pending
                     if not (set(all_tech[t].get("requires_all", ())) |
                             set(all_tech[t].get("requires_any", ()))) & pending}
            require(bool(ready), "technology dependency cycle")
            for t in ready:
                deps = set(all_tech[t].get("requires_all", ())) | set(all_tech[t].get("requires_any", ()))
                require(deps <= all_tech.keys(), f"unknown prerequisite of {t}")
            pending -= ready
        for rid, r in self.recipes.items():
            require(set(r["requires_all"]) <= all_tech.keys(), f"{rid} prerequisite")
            for field in ("inputs", "outputs", "installed"):
                require(set(r[field]) <= self.goods.keys(), f"{rid}.{field} unknown good")
                require(all(q >= 0 for q in r[field].values()), f"{rid}.{field} negative quantity")
        for uid, u in self.units.items():
            require(u["domain"] in self.tables["military"]["domain_channel"], f"{uid} domain")
            require(set(u["installed"]) <= self.goods.keys(), f"{uid} installed")
            if u["recipe_id"] is not None:
                require(u["recipe_id"] in self.recipes, f"{uid} recipe")
                require(set(u["installed"]) <= self.recipes[u["recipe_id"]]["outputs"].keys(),
                        f"{uid} manufacturing output")
        require(set(self.runtime["extraction"]) <= self.goods.keys(), "extraction goods")
        require(set(self.runtime["initial_world"]["productive_lines"]) <= self.recipes.keys(),
                "initial recipes")
        require(set(self.tables["scoring"]["pillars"]["tech"]["active_scale_rules"]) == set(all_tech),
                "active-scale coverage")

    def researchable(self, tech_id: str, known: set[str] | frozenset[str]) -> bool:
        row = self.technologies.get(tech_id)
        return row is not None and set(row["requires_all"]) <= known and (
            not row["requires_any"] or bool(set(row["requires_any"]) & known))

    def value(self, quantities_mU: Mapping[str, int]) -> Fraction:
        return sum((Fraction(q, 1000) * self.weights[g] for g, q in quantities_mU.items()), Fraction())
