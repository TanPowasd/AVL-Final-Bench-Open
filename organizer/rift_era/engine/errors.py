"""Keep player rejections distinct from faults that invalidate an evaluation."""


class RuleViolation(ValueError):
    """A rejected operation; validation must precede all business mutations."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


class InfrastructureFault(RuntimeError):
    """An engine, ruleset, host or reference failure. Never charge the player."""


class InvariantViolation(InfrastructureFault):
    """An internal conservation or state-machine contract was violated."""
