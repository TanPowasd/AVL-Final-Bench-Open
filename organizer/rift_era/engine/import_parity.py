"""10 C06/C07 import parity (import_parity_v1).

For a good g that nation p imported on net (by cleared value, RU) over the 5
last completed cycles, every Hub of p quotes g at no less than the import-parity price
p_g^RU / e_p x (1 + tariff) x (1 + .002): p_g^RU is the RU export-venue price
at that Hub when one exists, else the 10 x w_cat reference; the tariff is the
local venue's. The floor enters every caller of `OperatingWorld.price` (05
dispatch marginal cost, C01 VA and C02 CPI read it) and, by C07, covers chips
and accelerators like any other imported good. Nothing is recorded for
domestic trades or for cities that are not major nations.
"""
from .numeric import Q


class ImportParity:
    WINDOW = 5

    def __init__(self, state):
        self.s = state
        self.cycles = {}   # cycle -> {'p|g': net import value in RU}

    def record(self, good, buyer, seller, value_ru):
        known = self.s.initial.known
        if buyer == seller or not value_ru:
            return
        cycle = self.s.completed_cycles
        row = self.cycles.setdefault(cycle, {})
        if buyer in known:
            row[f'{buyer}|{good}'] = row.get(f'{buyer}|{good}', Q(0)) + value_ru
        if seller in known:
            row[f'{seller}|{good}'] = row.get(f'{seller}|{good}', Q(0)) - value_ru
        for old in [c for c in self.cycles if c < cycle - self.WINDOW]:
            del self.cycles[old]

    def net_importer(self, nation, good):
        if not self.cycles:
            return False
        cycle = self.s.completed_cycles
        key = f'{nation}|{good}'
        # Only completed cycles count, so a tick's quotes never move after P1 reservations.
        return sum((row.get(key, Q(0)) for c, row in self.cycles.items() if cycle - self.WINDOW <= c < cycle), Q(0)) > 0

    def floor(self, good, hub):
        state = self.s
        owner = state.initial.map.hubs[hub].owner
        if owner not in state.initial.known or not self.net_importer(owner, good):
            return None
        export = state.frame.prices.get(f'export:{hub}:{good}')
        world_ru = Q(export, 1000) if export is not None else 10 * state.catalog.weights[good]
        venue = state.spot.venues.get(f'spot:{hub}:{good}')
        tariff = venue.tariff if venue is not None else Q(0)
        return world_ru / state.rates[owner] * (1 + tariff) * Q(1002, 1000)

    def snapshot(self):
        return {str(c): row for c, row in sorted(self.cycles.items())}
