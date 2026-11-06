"""07 section 3-4: the 64-seed batch, its shadow baselines and INVALID_EVALUATION.

The runner owns only bookkeeping. `run_match(seed, seat, role)` is supplied by
the host and must return a `finalize_match` report: role 'player' is the formal
game (the submission sits in `seat`, seven Super references elsewhere), role
'shadow' is the same seed with eight Super references. Participant faults are
handled inside that game (custodial empty commands) and never reach here. Any
exception that escapes `run_match` is a judge, reference or host fault and
voids the whole batch: no seed is dropped and no mean is recomputed.

The journal is append-only JSON lines with fsync, so a batch can resume after
the host stops it. A journal whose header names another batch is refused.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
import json
import os
from pathlib import Path
from typing import Callable

from .determinism import canonical
from .errors import InfrastructureFault
from .evaluation import Batch
from .match import seat_score
from .numeric import Q

INVALID_EVALUATION = 'INVALID_EVALUATION'
ROLES = ('player', 'shadow')


@dataclass
class BatchResult:
    status: str                   # 'complete' | 'incomplete' | INVALID_EVALUATION
    done: int                     # finished (seed, role) games
    score_lower: str | None = None
    score_upper: str | None = None
    certified: bool = False       # every report full_rules_accepted
    reason: str | None = None


class BatchRunner:
    def __init__(self, batch: Batch, journal: Path, run_match: Callable[[int, int, str], dict]):
        batch.validate()
        self.batch, self.journal, self.run_match = batch, Path(journal), run_match
        self.reports: dict[tuple[int, str], dict] = {}
        self.invalid: str | None = None
        self._load()

    # -- journal ----------------------------------------------------------------
    def _append(self, row: dict) -> None:
        with self.journal.open('ab') as file:
            file.write(canonical(row) + b'\n')
            file.flush()
            os.fsync(file.fileno())

    def _load(self) -> None:
        if not self.journal.exists():
            self._append({'kind': 'header', 'batch_id': self.batch.batch_id})
            return
        lines = [json.loads(line) for line in self.journal.read_bytes().splitlines() if line.strip()]
        if not lines or lines[0].get('kind') != 'header' or lines[0].get('batch_id') != self.batch.batch_id:
            raise InfrastructureFault('batch journal belongs to another batch')
        for row in lines[1:]:
            if row['kind'] == 'report':
                self.reports[row['seed'], row['role']] = row['report']
            elif row['kind'] == INVALID_EVALUATION:
                self.invalid = row['reason']

    # -- running ----------------------------------------------------------------
    def pending(self):
        for seed, seat in zip(self.batch.seeds, self.batch.seats):
            for role in ROLES:
                if (seed, role) not in self.reports:
                    yield seed, seat, role

    def run(self, limit: int | None = None) -> BatchResult:
        """Run up to `limit` pending games (all when None), then summarize."""
        count = 0
        for seed, seat, role in list(self.pending()):
            if self.invalid or (limit is not None and count >= limit):
                break
            try:
                report = self.run_match(seed, seat, role)
                if not isinstance(report, dict) or 'scope' not in report:
                    raise InfrastructureFault('run_match returned no finalizer report')
            except Exception as exc:  # judge, reference strategy or host fault
                self.invalid = f'{role} seed={seed} seat={seat}: {type(exc).__name__}: {exc}'
                self._append({'kind': INVALID_EVALUATION, 'reason': self.invalid})
                break
            self.reports[seed, role] = report
            self._append({'kind': 'report', 'seed': seed, 'seat': seat, 'role': role,
                          'shadow_key': self.batch.shadow_key(seed, seat) if role == 'shadow' else None,
                          'report': report})
            count += 1
        return self.summary()

    # -- scoring ----------------------------------------------------------------
    @staticmethod
    def _v(report: dict, seat: int):
        nations = sorted(report['V'])
        return Q(*report['V'][nations[seat]])

    def summary(self) -> BatchResult:
        done = len(self.reports)
        if self.invalid:
            return BatchResult(INVALID_EVALUATION, done, reason=self.invalid)
        if done < 2 * len(self.batch.seeds):
            return BatchResult('incomplete', done)
        lower = upper = Decimal(0)  # seat scores carry ~50 digits; sums are exact at prec 120
        certified = True
        for seed, seat in zip(self.batch.seeds, self.batch.seats):
            player, shadow = self.reports[seed, 'player'], self.reports[seed, 'shadow']
            if not (player.get('complete_trajectory') and shadow.get('complete_trajectory')):
                return BatchResult('incomplete', done, reason=f'seed {seed}: trajectory shorter than 2880 cycles')
            certified &= bool(player.get('full_rules_accepted') and shadow.get('full_rules_accepted'))
            row = seat_score(self._v(player, seat), self._v(shadow, seat))
            with localcontext() as ctx:
                ctx.prec = 120
                lower += Decimal(row['score_lower'])
                upper += Decimal(row['score_upper'])
        n = len(self.batch.seeds)
        # Keep the mean a true interval: round the lower end down, the upper up.
        with localcontext() as ctx:
            ctx.prec, ctx.rounding = 60, ROUND_FLOOR
            low = lower / n
        with localcontext() as ctx:
            ctx.prec, ctx.rounding = 60, ROUND_CEILING
            high = upper / n
        return BatchResult('complete', done, str(low), str(high), certified)
