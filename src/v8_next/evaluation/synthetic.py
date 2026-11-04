"""Synthetic authority gate — thin port of v8-core/src/benchmark/synthetic.rs (D-153 §§41–48, Rule 57.3).

Asymmetry (the whole point): a synthetic FAIL may falsify (weight 1.0); a
synthetic PASS proves nothing economic (weight 0.0). Worlds without a passed
qualification gate are refused outright, never scored.

No passport ontology is invented here: the gate reads the two primitives the
rule needs (``generator_id``, ``passport_passed``). Full GeneratorPassport
qualification lives in the world-foundry workstream; this module only enforces
the benchmark-ingestion edge. All results carry NO_ECONOMIC_CLAIM.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["SyntheticEvaluationResult"]

NO_ECONOMIC_CLAIM = "NO_ECONOMIC_CLAIM"


@dataclass(frozen=True)
class SyntheticEvaluationResult:
    generator_id: str
    passed_stress: bool
    failure_mode: str | None
    epistemic_weight: float
    claim: str = NO_ECONOMIC_CLAIM

    @classmethod
    def evaluate(
        cls,
        generator_id: str,
        passport_passed: bool,
        stress_passed: bool,
        failure_mode: str | None = None,
    ) -> SyntheticEvaluationResult:
        if not passport_passed:
            raise ValueError(
                f"WORLD_REJECTED: generator {generator_id} failed qualification gate"
            )
        return cls(
            generator_id=generator_id,
            passed_stress=stress_passed,
            failure_mode=failure_mode,
            epistemic_weight=0.0 if stress_passed else 1.0,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "generator_id": self.generator_id,
            "passed_stress": self.passed_stress,
            "failure_mode": self.failure_mode,
            "epistemic_weight": self.epistemic_weight,
            "claim": self.claim,
        }
