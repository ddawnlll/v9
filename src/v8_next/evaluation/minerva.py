"""Minerva robustness engine — thin port of v8-core/src/benchmark/minerva.rs (arXiv:2608.23808).

Signed margins from academic thresholds (DSR/PBO/SPA/MinTRL/regime), a
non-compensable 5-gate vector, and the binary Robustness Seal: seal needs all
5 gates AND score >= 80; any gate failure caps the effective score below 80
and denies the seal. Post-selection certification only — never an
optimization target. Statistical kernels (DSR/PBO/SPA) are consumed as inputs;
they live in #322/#348 and are not reimplemented here.
"""

from __future__ import annotations

from dataclasses import dataclass

from v8_next.evaluation.benchmark_receipt import GateState

__all__ = [
    "MinervaEvaluator",
    "MinervaGateVector",
    "MinervaMargins",
    "MinervaRobustness",
    "PrudexCompass",
]


@dataclass(frozen=True)
class MinervaMargins:
    dsr_margin: float
    pbo_margin: float
    spa_margin: float
    min_trl_margin: float
    regime_stability_margin: float

    def as_dict(self) -> dict[str, object]:
        return {
            "dsr_margin": self.dsr_margin,
            "pbo_margin": self.pbo_margin,
            "spa_margin": self.spa_margin,
            "min_trl_margin": self.min_trl_margin,
            "regime_stability_margin": self.regime_stability_margin,
        }


@dataclass(frozen=True)
class MinervaGateVector:
    dsr_gate: GateState
    pbo_gate: GateState
    spa_gate: GateState
    min_trl_gate: GateState
    regime_stability_gate: GateState

    def all_passed(self) -> bool:
        return all(
            g.is_pass()
            for g in (
                self.dsr_gate,
                self.pbo_gate,
                self.spa_gate,
                self.min_trl_gate,
                self.regime_stability_gate,
            )
        )

    def failed_gate_count(self) -> int:
        return sum(
            0 if g.is_pass() else 1
            for g in (
                self.dsr_gate,
                self.pbo_gate,
                self.spa_gate,
                self.min_trl_gate,
                self.regime_stability_gate,
            )
        )


@dataclass(frozen=True)
class PrudexCompass:
    profitability: float = 0.0
    risk: float = 0.0
    universality: float = 0.0
    diversity: float = 0.0
    reliability: float = 0.0
    explainability: float = 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "profitability": self.profitability,
            "risk": self.risk,
            "universality": self.universality,
            "diversity": self.diversity,
            "reliability": self.reliability,
            "explainability": self.explainability,
        }


@dataclass(frozen=True)
class MinervaRobustness:
    raw_score: float
    effective_score: float
    seal_granted: bool
    seal_status: str
    gate_vector: MinervaGateVector
    margins: MinervaMargins
    prudex_compass: PrudexCompass
    claim: str = "NO_ECONOMIC_CLAIM"

    def as_dict(self) -> dict[str, object]:
        return {
            "raw_score": self.raw_score,
            "effective_score": self.effective_score,
            "seal_granted": self.seal_granted,
            "seal_status": self.seal_status,
            "gate_vector": {
                "dsr_gate": self.gate_vector.dsr_gate.value,
                "pbo_gate": self.gate_vector.pbo_gate.value,
                "spa_gate": self.gate_vector.spa_gate.value,
                "min_trl_gate": self.gate_vector.min_trl_gate.value,
                "regime_stability_gate": self.gate_vector.regime_stability_gate.value,
            },
            "margins": self.margins.as_dict(),
            "prudex_compass": self.prudex_compass.as_dict(),
            "claim": self.claim,
        }


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


class MinervaEvaluator:
    """Post-selection robustness certification (never an optimization target)."""

    @staticmethod
    def evaluate(
        dsr: float,
        pbo: float,
        spa_p_value: float,
        actual_track_days: float,
        min_trl_days: float,
        worst_regime_return_bps: float,
        regime_floor_bps: float,
        prudex: PrudexCompass | None = None,
    ) -> MinervaRobustness:
        for name, value in (
            ("dsr", dsr),
            ("pbo", pbo),
            ("spa_p_value", spa_p_value),
            ("actual_track_days", actual_track_days),
            ("min_trl_days", min_trl_days),
            ("worst_regime_return_bps", worst_regime_return_bps),
            ("regime_floor_bps", regime_floor_bps),
        ):
            if value != value or value in (float("inf"), float("-inf")):
                raise ValueError(f"MINERVA_INPUT: {name} is not finite")
        margins = MinervaMargins(
            dsr_margin=dsr - 0.95,
            pbo_margin=0.50 - pbo,
            spa_margin=0.05 - spa_p_value,
            min_trl_margin=actual_track_days - min_trl_days,
            regime_stability_margin=worst_regime_return_bps - regime_floor_bps,
        )
        gates = MinervaGateVector(
            dsr_gate=GateState.PASS if dsr >= 0.95 else GateState.BLOCKED,
            pbo_gate=GateState.PASS if pbo < 0.50 else GateState.BLOCKED,
            spa_gate=GateState.PASS if spa_p_value <= 0.05 else GateState.BLOCKED,
            min_trl_gate=GateState.PASS if actual_track_days >= min_trl_days else GateState.BLOCKED,
            regime_stability_gate=GateState.PASS
            if worst_regime_return_bps >= regime_floor_bps
            else GateState.BLOCKED,
        )
        dsr_norm = _clamp01((dsr - 0.50) / 0.50)
        pbo_norm = _clamp01((1.0 - pbo) / 1.0)
        if spa_p_value <= 0.05:
            spa_norm = 0.80 + 0.20 * (1.0 - (spa_p_value / 0.05))
        else:
            spa_norm = 0.80 * max(0.0, 1.0 - ((spa_p_value - 0.05) / 0.95))
        min_trl_norm = (actual_track_days / min_trl_days / 1.5) if min_trl_days > 0.0 else 0.5
        regime_norm = _clamp01((worst_regime_return_bps - regime_floor_bps + 2000.0) / 4000.0)
        weights = (0.25, 0.25, 0.20, 0.15, 0.15)
        norms = (dsr_norm, pbo_norm, spa_norm, min_trl_norm, regime_norm)
        inv_sum = sum(w / max(0.01, v) for w, v in zip(weights, norms, strict=True))
        raw_score = round(_clamp01(sum(weights) / inv_sum) * 100.0)
        if gates.all_passed():
            if raw_score >= 80.0:
                effective, granted = raw_score, True
                status = "SEAL_GRANTED: All 5 validation gates passed with score >= 80"
            else:
                effective, granted = raw_score, False
                status = f"SEAL_DENIED_SCORE_TOO_LOW: All gates passed but raw score ({raw_score:.0f}) < 80"
        else:
            failed = max(1, gates.failed_gate_count())
            capped = round(min(raw_score, 79.0) * (5.0 - failed) / 5.0)
            effective, granted = capped, False
            status = f"SEAL_DENIED_GATE_FAILURE: {failed} of 5 hard gates failed (score capped to {capped:.0f})"
        return MinervaRobustness(
            raw_score=raw_score,
            effective_score=effective,
            seal_granted=granted,
            seal_status=status,
            gate_vector=gates,
            margins=margins,
            prudex_compass=prudex if prudex is not None else PrudexCompass(),
        )
