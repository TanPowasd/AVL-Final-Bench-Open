"""Three independent clocks; an altered decision cadence is a practice ruleset."""

from dataclasses import dataclass
from enum import IntEnum


class Phase(IntEnum):
    COMMANDS = 1
    ENVIRONMENT_LOGISTICS = 2
    POWER = 3
    COMBAT = 4
    MARKETS = 5
    PRODUCTION = 6
    SETTLEMENT = 7
    OBSERVATION = 8


@dataclass(frozen=True)
class ClockConfig:
    total_ticks: int = 28_800
    decision_ticks: int = 5
    cycle_ticks: int = 10

    def __post_init__(self):
        if any(type(v) is not int or v <= 0 for v in
               (self.total_ticks, self.decision_ticks, self.cycle_ticks)):
            raise ValueError("clock intervals must be positive integers")
        if self.total_ticks % self.cycle_ticks or self.cycle_ticks % self.decision_ticks:
            raise ValueError("cycles must contain complete windows; match must contain complete cycles")

    @property
    def official(self) -> bool:
        return (self.total_ticks, self.decision_ticks, self.cycle_ticks) == (28_800, 5, 10)

    def is_decision(self, tick: int) -> bool:
        return tick % self.decision_ticks == 0

    def is_window_end(self, tick: int) -> bool:
        return (tick + 1) % self.decision_ticks == 0

    def is_cycle_end(self, tick: int) -> bool:
        return (tick + 1) % self.cycle_ticks == 0

    def cycle(self, tick: int) -> int:
        return tick // self.cycle_ticks

    def window(self, tick: int) -> int:
        return tick // self.decision_ticks
