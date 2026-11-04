"""Local prospective rule measurement, never a calibrated forecast receipt."""

from dataclasses import dataclass


@dataclass(frozen=True)
class RulePaperExperiment:
    frozen_ns: int
    start_ns: int
    end_ns: int

    def __post_init__(self) -> None:
        if any(type(t) is not int for t in (self.frozen_ns, self.start_ns, self.end_ns)) or not (
            0 < self.frozen_ns < self.start_ns < self.end_ns
        ):
            raise ValueError("paper experiment must be frozen before its ordered window")
