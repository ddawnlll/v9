"""Costed native replay and single-intervention ablations (M4-02, issue #30).

What this module measures
-------------------------
The V8 baseline is replayed through the *existing* NautilusTrader path
(:func:`v8_next.adapters.portfolio_backtest.run_portfolio_backtest`) on one
authorised tape. Fees, funding, mark basis, intrabar ordering, slippage and
partial-fill semantics are each published with the arm they produced, never
assumed:

* ``fee_total`` is summed from the engine's own order-fill commission column;
* ``funding_total`` is *isolated* by replaying the identical strategy twice, once
  with the tape's funding rows and once without, and taking the balance
  difference (the same method ``app/portfolio.py`` uses). The two replays must
  execute an identical trade signature; when they do not, ``funding_total`` is
  ``None`` and the arm carries a named ``unsupported`` reason instead of a zero;
* ``net_pnl`` is the native account balance delta, which the engine defines as
  ``initial + sum(position.realized_pnl)`` (commissions and funding are already
  inside ``realized_pnl``);
* every other cost/sequence fact is published as a basis string in
  :class:`Provenance`.

Intervention arms
-----------------
The five V9 interventions (belief, expert routing, entry timing, position
thesis, allocation) are adaptive order-side effects and are **off by design**
in V9. This module therefore never simulates them: every intervention arm is
``NOT_RUN`` with the reason ``ADAPTIVE_ORDERS_DISABLED_SHADOW_ONLY`` unless the
caller supplies an already-produced costed :class:`ArmResult` from a permitted
run. A delta is computed only when both its arms are costed; a report is
``COMPLETE`` only when every intervention has a costed arm, a delta and a delta
confidence interval.

Honesty rules
-------------
* Nothing here is a venue claim. The evidence class is a bar-tape simulation.
* Missing microstructure is tagged ``UNSUPPORTED`` and never filled with zero.
* The baseline replay command is recorded by the CLI so the numbers can be
  reproduced.
* :func:`publish_costed_result` is the only producer of
  ``reports/evaluation/costed_result.json``. It refuses unless the arm is the
  preregistered costed ``V9_INTERVENTION`` (never the V8 baseline or the
  random-entry control), digest-bound to the preregistration on disk, and its
  costs equal the registered fee/funding/slippage vector.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import random
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Sequence

from v8_next.evaluation.accounting_contract import EconomicContract, compare_economic_contracts

SCHEMA_VERSION = "v9.ablation.1"
BASELINE_ARM_ID = "baseline_v8_no_intervention"
#: The preregistered intervention kind a costed result may be bound to.
V9_INTERVENTION_KIND = "V9_INTERVENTION"
#: The shared-contract location the economic verdict reads.
PREREGISTRATION_PATH = Path("reports/evaluation/preregistration.json")
COSTED_RESULT_PATH = Path("reports/evaluation/costed_result.json")
COSTED_RESULT_VERDICTS = frozenset({"PASS", "NO_EDGE", "INCONCLUSIVE", "STOP", "INVALID"})
BOOTSTRAP_SEED = 20251105
BOOTSTRAP_RESAMPLES = 2000
CI_LEVEL = 0.95

#: Every adaptive (V9) intervention arm is unrun by design: their order side
#: effects are disabled, and a shadow-only intent count is not an economic fact.
NOT_RUN_ADAPTIVE_ORDERS = "ADAPTIVE_ORDERS_DISABLED_SHADOW_ONLY"
MICROSTRUCTURE_UNSUPPORTED = (
    "UNSUPPORTED:NO_CAPTURED_L2_L3_ORDER_BOOK_DEPTH: microstructure (queue position, "
    "depth-limited partial fills, adverse selection) is not measurable on the kline tape; "
    "execution is a published model assumption, not venue truth"
)
MICROSTRUCTURE_UNSUPPORTED_TOKEN = "MICROSTRUCTURE_UNSUPPORTED_NO_CAPTURED_BOOK_DEPTH"
INTRABAR_TIE_BASIS = (
    "NAUTILUS_2.0.0rc4_BAR_EXECUTION_DEFAULT (bar_adaptive_high_low_ordering=False): "
    "the intrabar stop-vs-target sequence is an engine model assumption, not an observed "
    "sequence (no STOP_FIRST claim is made)"
)
COST_ATTRIBUTION_BASIS = (
    "ENGINE_NATIVE: net_pnl is the native account balance delta; commissions and funding "
    "are inside position.realized_pnl; fee_total is summed from the order-fill commission "
    "column; funding_total is isolated by a funding/no-funding dual replay over an identical "
    "trade signature"
)


class Intervention(StrEnum):
    """The five single interventions whose incremental impact is measured."""

    BELIEF = "BELIEF"
    EXPERT_ROUTING = "EXPERT_ROUTING"
    ENTRY_TIMING = "ENTRY_TIMING"
    POSITION_THESIS = "POSITION_THESIS"
    ALLOCATION = "ALLOCATION"


def _round(value: float | None, digits: int = 10) -> float | None:
    """Round a measured value for deterministic JSON; absence stays absent."""
    if value is None:
        return None
    return round(float(value), digits)


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_json(payload: Mapping[str, Any]) -> str:
    """Stable sha256 over a JSON payload (sorted keys, no whitespace)."""
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def _quantile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolation quantile over an already sorted sequence."""
    if not sorted_values:
        raise ValueError("cannot take a quantile of an empty sequence")
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    position = q * (len(sorted_values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return float(sorted_values[lower]) * (1.0 - weight) + float(sorted_values[upper]) * weight


def bootstrap_mean_ci(
    samples: Sequence[float],
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
    level: float = CI_LEVEL,
) -> tuple[float | None, float | None]:
    """Percentile bootstrap CI for the mean of per-campaign net-R samples.

    Deterministic: the resampling RNG is seeded from the module constant, so two
    runs over the same samples publish byte-identical bounds. Fewer than two
    samples cannot evidence a distribution, so the bounds are ``None``.
    """
    values = [float(value) for value in samples]
    if len(values) < 2:
        return None, None
    rng = random.Random(seed)
    count = len(values)
    means: list[float] = []
    for _ in range(resamples):
        total = 0.0
        for _ in range(count):
            total += values[rng.randrange(count)]
        means.append(total / count)
    means.sort()
    tail = (1.0 - level) / 2.0
    return _quantile(means, tail), _quantile(means, 1.0 - tail)


def bootstrap_mean_difference_ci(
    baseline_samples: Sequence[float],
    treatment_samples: Sequence[float],
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
    level: float = CI_LEVEL,
) -> tuple[float | None, float | None]:
    """Percentile bootstrap CI for ``mean(treatment) - mean(baseline)``."""
    baseline = [float(value) for value in baseline_samples]
    treatment = [float(value) for value in treatment_samples]
    if len(baseline) < 2 or len(treatment) < 2:
        return None, None
    rng = random.Random(seed)
    differences: list[float] = []
    for _ in range(resamples):
        b_total = 0.0
        for _ in range(len(baseline)):
            b_total += baseline[rng.randrange(len(baseline))]
        t_total = 0.0
        for _ in range(len(treatment)):
            t_total += treatment[rng.randrange(len(treatment))]
        differences.append(t_total / len(treatment) - b_total / len(baseline))
    differences.sort()
    tail = (1.0 - level) / 2.0
    return _quantile(differences, tail), _quantile(differences, 1.0 - tail)


@dataclass(frozen=True)
class Provenance:
    """Where an arm's numbers came from and which cost model produced them."""

    tape_sha256: str
    tape_manifest_sha256: str | None
    config_sha256: str
    engine: str
    engine_version: str
    execution_profile: str
    execution_profile_digest: str
    fee_model: str
    maker_fee: str
    taker_fee: str
    funding_basis: str
    mark_basis: str
    intrabar_tie_basis: str
    slippage_basis: str
    partial_fills_basis: str
    cost_attribution_basis: str = COST_ATTRIBUTION_BASIS

    def to_dict(self) -> dict[str, Any]:
        return {
            "tape_sha256": self.tape_sha256,
            "tape_manifest_sha256": self.tape_manifest_sha256,
            "config_sha256": self.config_sha256,
            "engine": self.engine,
            "engine_version": self.engine_version,
            "execution_profile": self.execution_profile,
            "execution_profile_digest": self.execution_profile_digest,
            "fee_model": self.fee_model,
            "maker_fee": self.maker_fee,
            "taker_fee": self.taker_fee,
            "funding_basis": self.funding_basis,
            "mark_basis": self.mark_basis,
            "intrabar_tie_basis": self.intrabar_tie_basis,
            "slippage_basis": self.slippage_basis,
            "partial_fills_basis": self.partial_fills_basis,
            "cost_attribution_basis": self.cost_attribution_basis,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Provenance:
        return cls(
            tape_sha256=str(payload["tape_sha256"]),
            tape_manifest_sha256=(
                None
                if payload.get("tape_manifest_sha256") is None
                else str(payload["tape_manifest_sha256"])
            ),
            config_sha256=str(payload["config_sha256"]),
            engine=str(payload["engine"]),
            engine_version=str(payload["engine_version"]),
            execution_profile=str(payload["execution_profile"]),
            execution_profile_digest=str(payload["execution_profile_digest"]),
            fee_model=str(payload["fee_model"]),
            maker_fee=str(payload["maker_fee"]),
            taker_fee=str(payload["taker_fee"]),
            funding_basis=str(payload["funding_basis"]),
            mark_basis=str(payload["mark_basis"]),
            intrabar_tie_basis=str(payload["intrabar_tie_basis"]),
            slippage_basis=str(payload["slippage_basis"]),
            partial_fills_basis=str(payload["partial_fills_basis"]),
            cost_attribution_basis=str(
                payload.get("cost_attribution_basis", COST_ATTRIBUTION_BASIS)
            ),
        )


@dataclass(frozen=True)
class ArmResult:
    """One measured (or explicitly unrun) arm of the ablation.

    ``costed`` is the gate: only a costed arm may enter a delta. Every numeric
    field is ``None`` when it was not measured; a ``0`` is only ever published
    when the engine measured a zero (e.g. no funding settlements were fed).
    """

    arm_id: str
    intervention: Intervention | None
    costed: bool
    trades: int | None
    net_r: float | None
    net_pnl: float | None
    max_drawdown: float | None
    hit_rate: float | None
    fee_total: float | None
    funding_total: float | None
    ci_low: float | None
    ci_high: float | None
    ci_method: str | None
    provenance: Provenance | None
    net_r_samples: tuple[float, ...] = ()
    risk_basis: str | None = None
    open_campaigns: int | None = None
    unsupported: tuple[str, ...] = ()
    #: ``V9_INTERVENTION`` for the preregistered adaptive arm; ``None`` when unstated.
    arm_kind: str | None = None
    #: sha256 of the preregistration this arm was measured under, when it declares one.
    preregistration_digest: str | None = None
    economic_contract: EconomicContract | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "arm_id": self.arm_id,
            "intervention": None if self.intervention is None else self.intervention.value,
            "costed": self.costed,
            "trades": self.trades,
            "net_r": _round(self.net_r),
            "net_pnl": _round(self.net_pnl),
            "max_drawdown": _round(self.max_drawdown),
            "hit_rate": _round(self.hit_rate),
            "fee_total": _round(self.fee_total),
            "funding_total": _round(self.funding_total),
            "ci_low": _round(self.ci_low),
            "ci_high": _round(self.ci_high),
            "ci_method": self.ci_method,
            "provenance": None if self.provenance is None else self.provenance.to_dict(),
            "net_r_samples": [_round(value) for value in self.net_r_samples],
            "risk_basis": self.risk_basis,
            "open_campaigns": self.open_campaigns,
            "unsupported": list(self.unsupported),
            "arm_kind": self.arm_kind,
            "preregistration_digest": self.preregistration_digest,
            "economic_contract": (None if self.economic_contract is None else self.economic_contract.to_dict()),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ArmResult:
        intervention = payload.get("intervention")
        provenance = payload.get("provenance")
        return cls(
            arm_id=str(payload["arm_id"]),
            intervention=None if intervention is None else Intervention(str(intervention)),
            costed=bool(payload.get("costed", False)),
            trades=None if payload.get("trades") is None else int(payload["trades"]),
            net_r=None if payload.get("net_r") is None else float(payload["net_r"]),
            net_pnl=None if payload.get("net_pnl") is None else float(payload["net_pnl"]),
            max_drawdown=(
                None if payload.get("max_drawdown") is None else float(payload["max_drawdown"])
            ),
            hit_rate=None if payload.get("hit_rate") is None else float(payload["hit_rate"]),
            fee_total=(
                None if payload.get("fee_total") is None else float(payload["fee_total"])
            ),
            funding_total=(
                None if payload.get("funding_total") is None else float(payload["funding_total"])
            ),
            ci_low=None if payload.get("ci_low") is None else float(payload["ci_low"]),
            ci_high=None if payload.get("ci_high") is None else float(payload["ci_high"]),
            ci_method=None if payload.get("ci_method") is None else str(payload["ci_method"]),
            provenance=(
                None
                if provenance is None
                else Provenance.from_dict(provenance)
            ),
            net_r_samples=tuple(float(value) for value in payload.get("net_r_samples", ())),
            risk_basis=None if payload.get("risk_basis") is None else str(payload["risk_basis"]),
            open_campaigns=(
                None if payload.get("open_campaigns") is None else int(payload["open_campaigns"])
            ),
            unsupported=tuple(str(value) for value in payload.get("unsupported", ())),
            arm_kind=None if payload.get("arm_kind") is None else str(payload["arm_kind"]),
            economic_contract=(
                None if payload.get("economic_contract") is None
                else EconomicContract.from_dict(payload["economic_contract"])
            ),
            preregistration_digest=(
                None
                if payload.get("preregistration_digest") is None
                else str(payload["preregistration_digest"])
            ),
        )


@dataclass(frozen=True)
class ArmDelta:
    """Increment ``treatment - baseline``; only constructible from two costed arms."""

    intervention: Intervention
    baseline_arm_id: str
    treatment_arm_id: str
    delta_net_r: float | None
    delta_net_pnl: float | None
    delta_max_drawdown: float | None
    delta_hit_rate: float | None
    delta_ci_low: float | None
    delta_ci_high: float | None
    ci_method: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "intervention": self.intervention.value,
            "baseline_arm_id": self.baseline_arm_id,
            "treatment_arm_id": self.treatment_arm_id,
            "delta_net_r": _round(self.delta_net_r),
            "delta_net_pnl": _round(self.delta_net_pnl),
            "delta_max_drawdown": _round(self.delta_max_drawdown),
            "delta_hit_rate": _round(self.delta_hit_rate),
            "delta_ci_low": _round(self.delta_ci_low),
            "delta_ci_high": _round(self.delta_ci_high),
            "ci_method": self.ci_method,
        }


def _difference(treatment: float | None, baseline: float | None) -> float | None:
    if treatment is None or baseline is None:
        return None
    return treatment - baseline


def _require_comparable_provenance(baseline: ArmResult, treatment: ArmResult) -> None:
    """Refuse a delta whose two arms were not measured on one measurement basis.

    A difference between numbers produced on different tapes, engines or execution
    profiles is not an intervention effect; it conflates the treatment with the
    measurement. Each mismatch is named so the caller can see which fact differs.
    """
    left = baseline.provenance
    right = treatment.provenance
    if left is None or right is None:
        raise ValueError(
            "a delta requires provenance on both arms; an arm without a declared "
            "measurement basis cannot be compared"
        )
    checks = (
        ("tape", left.tape_sha256, right.tape_sha256),
        ("engine", left.engine, right.engine),
        ("engine_version", left.engine_version, right.engine_version),
        ("execution_profile", left.execution_profile, right.execution_profile),
        ("execution_profile_digest", left.execution_profile_digest, right.execution_profile_digest),
    )
    mismatched = [name for name, base, cand in checks if base != cand]
    if mismatched:
        raise ValueError(
            "a delta requires both arms measured on the same tape, engine and execution "
            f"profile; mismatched: {', '.join(mismatched)}"
        )


def compute_delta(
    baseline: ArmResult,
    treatment: ArmResult,
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> ArmDelta:
    """Increment of a costed treatment over a costed baseline.

    Refuses uncosted arms: a delta computed from an unrun arm would be a
    fabricated economic claim. Also refuses arms whose provenance names a different
    tape, engine or execution profile: such a difference is not an intervention
    effect, no matter what the numbers say.
    """
    if not baseline.costed or not treatment.costed:
        raise ValueError("a delta requires both arms costed")
    if treatment.intervention is None:
        raise ValueError("a treatment arm must declare its intervention")
    _require_comparable_provenance(baseline, treatment)
    left_contract = baseline.economic_contract
    right_contract = treatment.economic_contract
    if left_contract is None or right_contract is None:
        raise ValueError("economic comparison BLOCKED: missing economic contract")
    comparison = compare_economic_contracts(left_contract, right_contract)
    if comparison.status == "BLOCKED":
        raise ValueError(
            "economic comparison BLOCKED: "
            f"missing={comparison.missing}, mismatched={comparison.mismatched}"
        )
    for arm in (baseline, treatment):
        contract = arm.economic_contract
        provenance = arm.provenance
        assert contract is not None and provenance is not None
        try:
            receipt_fees = (Decimal(provenance.maker_fee), Decimal(provenance.taker_fee))
        except InvalidOperation as exc:
            raise ValueError("economic comparison BLOCKED: invalid provenance fees") from exc
        if (
            contract.tape_sha256 != provenance.tape_sha256
            or contract.execution_profile != provenance.execution_profile
            or (contract.maker_fee, contract.taker_fee) != receipt_fees
        ):
            raise ValueError("economic comparison BLOCKED: contract conflicts with provenance")
    low, high = bootstrap_mean_difference_ci(
        baseline.net_r_samples,
        treatment.net_r_samples,
        resamples=resamples,
        seed=seed,
    )
    method = (
        f"BOOTSTRAP_PERCENTILE_{int(CI_LEVEL * 100)}_DELTA_OF_MEAN "
        f"(resamples={resamples}, seed={seed}, unit=per-campaign net R)"
    )
    if low is None or high is None:
        if (
            baseline.net_r is not None
            and treatment.net_r is not None
            and baseline.ci_low is not None
            and baseline.ci_high is not None
            and treatment.ci_low is not None
            and treatment.ci_high is not None
        ):
                z = 1.959963984540054
                baseline_se = (baseline.ci_high - baseline.ci_low) / (2.0 * z)
                treatment_se = (treatment.ci_high - treatment.ci_low) / (2.0 * z)
                combined = (baseline_se**2 + treatment_se**2) ** 0.5
                if combined > 0.0:
                    point = treatment.net_r - baseline.net_r
                    low = point - z * combined
                    high = point + z * combined
                    method = (
                        f"NORMAL_APPROXIMATION_{int(CI_LEVEL * 100)} "
                        "from each arm's published CI half-width"
                    )
    return ArmDelta(
        intervention=treatment.intervention,
        baseline_arm_id=baseline.arm_id,
        treatment_arm_id=treatment.arm_id,
        delta_net_r=_difference(treatment.net_r, baseline.net_r),
        delta_net_pnl=_difference(treatment.net_pnl, baseline.net_pnl),
        delta_max_drawdown=_difference(treatment.max_drawdown, baseline.max_drawdown),
        delta_hit_rate=_difference(treatment.hit_rate, baseline.hit_rate),
        delta_ci_low=low,
        delta_ci_high=high,
        ci_method=method if low is not None and high is not None else None,
    )


class CostedResultError(ValueError):
    """A costed result was refused; nothing was written."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


def _decimal_term(value: object) -> Decimal | None:
    """A finite Decimal parsed from a string/number, or ``None`` when unusable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _declared_costs(costs: object, *, source: str) -> dict[str, Any]:
    """The four shared-contract cost terms, validated; absence is never zero."""
    if not isinstance(costs, Mapping):
        raise CostedResultError(
            f"{source}_COSTS_MISSING", "the cost vector is not a mapping"
        )
    if costs.get("funding_included") is not True:
        raise CostedResultError(
            f"{source}_COSTS_INVALID",
            "costs must declare funding_included=true",
        )
    resolved: dict[str, Any] = {"funding_included": True}
    for field in ("fee_bps_per_side", "funding_bps_per_day", "slippage_bps_per_side"):
        parsed = _decimal_term(costs.get(field))
        if parsed is None or parsed < 0:
            raise CostedResultError(
                f"{source}_COSTS_INVALID",
                f"{field} is not a non-negative number: {costs.get(field)!r}",
            )
        resolved[field] = parsed
    return resolved


def publish_costed_result(
    arm: ArmResult | None,
    *,
    costs: Mapping[str, Any],
    net_return: str | Decimal | int | float | None,
    verdict: str,
    preregistration_path: Path | str = PREREGISTRATION_PATH,
    output_path: Path | str = COSTED_RESULT_PATH,
    uncertainty: Mapping[str, Any] | None = None,
) -> Path:
    """Publish the shared-contract costed result for the preregistered V9 arm.

    The file is written only after every binding fact has been checked: a
    preregistration must exist on disk, the arm must be the preregistered
    ``V9_INTERVENTION`` (not the V8 baseline and not the random-entry control), it
    must record the sha256 of the preregistration it was measured under, and the
    declared costs must equal the registered costs exactly. Every refusal raises
    :class:`CostedResultError` before any write, so a failed publish cannot leave a
    partially-bound artifact behind.
    """
    prereg_path = Path(preregistration_path)
    if not prereg_path.is_file():
        raise CostedResultError(
            "PREREGISTRATION_MISSING", f"no preregistration at {prereg_path}"
        )
    try:
        preregistration: object = json.loads(prereg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CostedResultError("PREREGISTRATION_UNREADABLE", str(exc)) from exc
    if not isinstance(preregistration, Mapping):
        raise CostedResultError(
            "PREREGISTRATION_UNREADABLE", "preregistration is not a JSON object"
        )
    if arm is None:
        raise CostedResultError(
            "COSTED_RESULT_ARM_MISSING",
            "no arm was supplied; a costed result cannot be published",
        )
    if not arm.costed:
        raise CostedResultError(
            "COSTED_RESULT_ARM_NOT_COSTED",
            f"arm {arm.arm_id!r} is not costed; nothing measured nothing to publish",
        )
    intervention = preregistration.get("intervention")
    intervention = intervention if isinstance(intervention, Mapping) else {}
    if intervention.get("kind") != V9_INTERVENTION_KIND:
        raise CostedResultError(
            "PREREGISTRATION_NOT_V9_INTERVENTION",
            f"preregistration intervention kind is {intervention.get('kind')!r}",
        )
    if arm.arm_kind != V9_INTERVENTION_KIND:
        raise CostedResultError(
            "COSTED_RESULT_ARM_NOT_V9_INTERVENTION",
            f"arm {arm.arm_id!r} declares kind {arm.arm_kind!r}; only the "
            "preregistered V9 intervention may be published",
        )
    registered_arm_id = intervention.get("arm_id")
    if not isinstance(registered_arm_id, str) or arm.arm_id != registered_arm_id:
        raise CostedResultError(
            "COSTED_RESULT_ARM_NOT_PREREGISTERED",
            f"arm {arm.arm_id!r} is not the preregistered arm {registered_arm_id!r}",
        )
    digest = hashlib.sha256(prereg_path.read_bytes()).hexdigest()
    if arm.preregistration_digest != digest:
        raise CostedResultError(
            "PREREGISTRATION_DIGEST_MISMATCH",
            f"arm records {arm.preregistration_digest!r}; preregistration on disk is {digest!r}",
        )
    registered = _declared_costs(preregistration.get("costs"), source="PREREGISTRATION")
    declared = _declared_costs(costs, source="COSTED_RESULT")
    mismatched = [
        field
        for field in ("fee_bps_per_side", "funding_bps_per_day", "slippage_bps_per_side")
        if declared[field] != registered[field]
    ]
    if mismatched:
        raise CostedResultError(
            "COSTS_DIFFER_FROM_REGISTRATION",
            f"declared {', '.join(mismatched)} differ from the registered costs",
        )
    dataset_role = preregistration.get("dataset_role")
    if not isinstance(dataset_role, str) or not dataset_role:
        raise CostedResultError(
            "PREREGISTRATION_DATASET_ROLE_MISSING",
            "the preregistration does not declare a dataset_role",
        )
    net = _decimal_term(net_return)
    if net is None:
        raise CostedResultError(
            "NET_RETURN_INVALID", f"net_return is not a finite decimal: {net_return!r}"
        )
    declared_verdict = str(verdict).strip().upper()
    if declared_verdict not in COSTED_RESULT_VERDICTS:
        raise CostedResultError(
            "VERDICT_INVALID",
            f"verdict {verdict!r} is not one of {sorted(COSTED_RESULT_VERDICTS)}",
        )
    payload: dict[str, Any] = {
        "preregistration_digest": digest,
        "dataset_role": dataset_role,
        "costs": {
            "fee_bps_per_side": str(registered["fee_bps_per_side"]),
            "funding_bps_per_day": str(registered["funding_bps_per_day"]),
            "slippage_bps_per_side": str(registered["slippage_bps_per_side"]),
            "funding_included": True,
        },
        "net_return": str(net),
        "verdict": declared_verdict,
    }
    # The trade count is part of the measurement, not an optional note: the
    # verdict gate refuses a PASS below the registered minimum, so the producer
    # carries what the arm actually observed (and refuses to invent it).
    if arm.trades is not None:
        payload["trades"] = int(arm.trades)
    if uncertainty is not None:
        if not isinstance(uncertainty, Mapping):
            raise CostedResultError(
                "UNCERTAINTY_NOT_JSON", "uncertainty must be a mapping"
            )
        payload["uncertainty"] = dict(uncertainty)
    try:
        encoded = json.dumps(payload, sort_keys=True, indent=2, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise CostedResultError("COSTED_RESULT_NOT_JSON", str(exc)) from exc
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(encoded + "\n", encoding="utf-8")
    return target


def _not_run_arm(intervention: Intervention, reason: str) -> ArmResult:
    return ArmResult(
        arm_id=f"{intervention.value.lower()}_arm",
        intervention=intervention,
        costed=False,
        trades=None,
        net_r=None,
        net_pnl=None,
        max_drawdown=None,
        hit_rate=None,
        fee_total=None,
        funding_total=None,
        ci_low=None,
        ci_high=None,
        ci_method=None,
        provenance=None,
        unsupported=(reason,),
    )


@dataclass(frozen=True)
class AblationReport:
    """Per-intervention report with its baseline, deltas and honest status."""

    baseline: ArmResult | None
    arms: tuple[ArmResult, ...]
    deltas: tuple[ArmDelta, ...]
    status: str
    unsupported: tuple[tuple[str, tuple[str, ...]], ...]
    microstructure: str
    notes: tuple[str, ...] = ()
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.status not in ("COMPLETE", "PARTIAL", "NOT_RUN"):
            raise ValueError(f"unknown ablation status {self.status!r}")
        arm_by_intervention = {
            arm.intervention: arm for arm in self.arms if arm.intervention is not None
        }
        delta_by_intervention = {delta.intervention: delta for delta in self.deltas}
        if self.status == "COMPLETE":
            if self.baseline is None or not self.baseline.costed:
                raise ValueError("COMPLETE requires a costed baseline arm")
            for intervention in Intervention:
                arm = arm_by_intervention.get(intervention)
                if arm is None or not arm.costed:
                    raise ValueError(
                        f"COMPLETE cannot be published while {intervention.value} is NOT_RUN"
                    )
                delta = delta_by_intervention.get(intervention)
                if delta is None:
                    raise ValueError(
                        f"COMPLETE requires a delta for {intervention.value}"
                    )
                if delta.delta_net_r is None:
                    raise ValueError(
                        f"COMPLETE requires a net-R delta for {intervention.value}"
                    )
                if delta.delta_ci_low is None or delta.delta_ci_high is None:
                    raise ValueError(
                        f"COMPLETE requires a delta confidence interval for {intervention.value}"
                    )
        if self.status == "NOT_RUN" and self.baseline is not None and self.baseline.costed:
            raise ValueError("NOT_RUN cannot carry a costed baseline")

    @property
    def not_run(self) -> tuple[str, ...]:
        return tuple(arm.arm_id for arm in self.arms if not arm.costed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "baseline": None if self.baseline is None else self.baseline.to_dict(),
            "arms": [arm.to_dict() for arm in self.arms],
            "deltas": [delta.to_dict() for delta in self.deltas],
            "not_run": list(self.not_run),
            "unsupported": {key: list(reasons) for key, reasons in self.unsupported},
            "microstructure": self.microstructure,
            "notes": list(self.notes),
        }

    def to_json(self) -> str:
        """Deterministic JSON (sorted keys, fixed float rounding)."""
        return json.dumps(self.to_dict(), sort_keys=True, indent=2, ensure_ascii=True) + "\n"

    def write_json(self, path: Path | str) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.to_json(), encoding="utf-8")
        return target


def build_report(
    *,
    baseline: ArmResult | None,
    supplied_arms: Mapping[Intervention, ArmResult] | None = None,
    notes: Sequence[str] = (),
) -> AblationReport:
    """Assemble a report from a baseline and any permitted costed interventions.

    An intervention arm is used only when it is costed *and* declares the same
    intervention. Everything else is synthesised as ``NOT_RUN`` with a named
    reason; no delta is computed for an unrun arm.
    """
    supplied = supplied_arms or {}
    arms: list[ArmResult] = []
    unsupported: dict[str, tuple[str, ...]] = {}
    deltas: list[ArmDelta] = []
    baseline_costed = baseline is not None and baseline.costed
    for intervention in Intervention:
        candidate = supplied.get(intervention)
        if candidate is not None and candidate.costed:
            if candidate.intervention != intervention:
                raise ValueError(
                    f"supplied arm {candidate.arm_id!r} declares "
                    f"{candidate.intervention} but was supplied for {intervention.value}"
                )
            arm = candidate
        else:
            reason = (
                NOT_RUN_ADAPTIVE_ORDERS
                if candidate is None
                else "SUPPLIED_ARM_NOT_COSTED"
            )
            arm = _not_run_arm(intervention, reason)
            if candidate is not None:
                arm = ArmResult(
                    arm_id=arm.arm_id,
                    intervention=intervention,
                    costed=False,
                    trades=None,
                    net_r=None,
                    net_pnl=None,
                    max_drawdown=None,
                    hit_rate=None,
                    fee_total=None,
                    funding_total=None,
                    ci_low=None,
                    ci_high=None,
                    ci_method=None,
                    provenance=None,
                    unsupported=tuple(sorted({reason, *candidate.unsupported})),
                )
        arms.append(arm)
        if arm.unsupported:
            unsupported[arm.arm_id] = arm.unsupported
        if arm.costed and baseline_costed:
            assert baseline is not None
            delta = compute_delta(baseline, arm)
            deltas.append(delta)
            if delta.delta_net_r is None:
                unsupported.setdefault(arm.arm_id, ())
                unsupported[arm.arm_id] = (
                    *unsupported[arm.arm_id],
                    "DELTA_NET_R_UNSUPPORTED: arm has no net-R samples",
                )

    if baseline is not None and baseline.unsupported:
        unsupported[baseline.arm_id] = baseline.unsupported

    all_costed = all(arm.costed for arm in arms)
    deltas_complete = all(
        delta.delta_net_r is not None
        and delta.delta_ci_low is not None
        and delta.delta_ci_high is not None
        for delta in deltas
    )
    if not baseline_costed:
        status = "NOT_RUN"
    elif all_costed and len(deltas) == len(Intervention) and deltas_complete:
        status = "COMPLETE"
    else:
        status = "PARTIAL"

    default_notes = [
        "No economic claim is made from shadow-only intent counts; only a costed native "
        "replay can enter a delta.",
        "Adaptive (V9) intervention arms are NOT_RUN because adaptive order side effects "
        "are disabled by design.",
        "max_drawdown is peak-to-trough of the realized-equity curve (initial balance plus "
        "closed-campaign realized net PnL); open campaigns are counted, never marked into "
        "the realized curve.",
    ]
    return AblationReport(
        baseline=baseline,
        arms=tuple(arms),
        deltas=tuple(deltas),
        status=status,
        unsupported=tuple(sorted(unsupported.items())),
        microstructure=MICROSTRUCTURE_UNSUPPORTED,
        notes=tuple(default_notes) + tuple(notes),
    )


# --------------------------------------------------------------------------- #
# Native replay driver
# --------------------------------------------------------------------------- #


def _money(value: Any) -> Decimal | None:
    from v8_next.evaluation.economic_benchmark import parse_money

    return parse_money(value)


def _engine_version() -> str:
    try:
        return importlib.metadata.version("nautilus_trader")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover - env defect
        return "UNKNOWN"


def _manifest_sha(tape_argument: Path) -> str | None:
    if tape_argument.is_dir():
        manifest = tape_argument / "tape.manifest.json"
    else:
        manifest = tape_argument.parent / "tape.manifest.json"
    if not manifest.is_file():
        return None
    return hashlib.sha256(manifest.read_bytes()).hexdigest()


def _commission_total(fill_records: Sequence[Mapping[str, Any]]) -> tuple[float, int]:
    """Sum the native fill commissions; report entries it could not parse."""
    total = Decimal(0)
    unparsable = 0
    for record in fill_records:
        commissions = record.get("commissions") or []
        if not commissions:
            unparsable += 1
            continue
        for entry in commissions:
            parsed = _money(entry)
            if parsed is None:
                unparsable += 1
            else:
                total += parsed
    return float(total), unparsable


def _partial_fill_basis(fill_records: Sequence[Mapping[str, Any]]) -> str:
    """Report partial fills from the order-fill report, never assume zero."""
    partial = 0
    for record in fill_records:
        quantity = _money(record.get("quantity"))
        filled = _money(record.get("filled_qty"))
        if quantity is not None and filled is not None and filled < quantity:
            partial += 1
    return (
        f"ORDER_FILL_REPORT_ROWS={len(fill_records)} PARTIAL_ORDERS={partial}; "
        "bar execution fills the submitted quantity at the bar price and no captured "
        "L2/L3 depth limits size"
    )


def _metrics_from_records(
    *,
    opened: Sequence[Mapping[str, Any]],
    closed: Sequence[Mapping[str, Any]],
    initial_balance: float,
    bracket_stop_pct: Decimal | None,
    resamples: int,
    seed: int,
) -> dict[str, Any]:
    """Per-campaign net-R, hit rate and drawdown from native position records."""
    from v8_next.evaluation.economic_benchmark import pair_positions

    pairs = pair_positions(list(opened), list(closed))
    samples: list[float] = []
    pnls: list[tuple[int, float]] = []
    unsupported: list[str] = []
    open_campaigns = 0
    risk_basis = (
        "BRACKET_RISK: per-campaign risk = entry_notional * bracket_stop_pct"
        if bracket_stop_pct is not None
        else "ENTRY_NOTIONAL: no bracket stop declared"
    )

    for open_record, close_record in pairs:
        quantity = _money(open_record.get("quantity"))
        entry = _money(open_record.get("avg_px_open"))
        if quantity is None or entry is None:
            unsupported.append("CAMPAIGN_ENTRY_UNPARSABLE")
            continue
        entry_notional = abs(quantity * entry)
        risk = (
            entry_notional * bracket_stop_pct
            if bracket_stop_pct is not None
            else entry_notional
        )
        if close_record is None:
            # Open campaigns are counted, never marked into the realized curve:
            # net_pnl is the native account's realized delta, so mixing an
            # unrealised mark into the drawdown would compare two valuations.
            open_campaigns += 1
            continue
        pnl = _money(close_record.get("realized_pnl"))
        if pnl is None or risk <= 0:
            unsupported.append("CAMPAIGN_NET_PNL_UNPARSABLE")
            continue
        samples.append(float(pnl / risk))
        event_ns = int(close_record.get("event_ns", 0) or 0)
        pnls.append((event_ns, float(pnl)))

    pnls.sort(key=lambda item: item[0])
    equity = float(initial_balance)
    peak = equity
    max_drawdown = 0.0
    for _, campaign_pnl in pnls:
        equity += campaign_pnl
        peak = max(peak, equity)
        if peak > 0.0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
    ci_low, ci_high = bootstrap_mean_ci(samples, resamples=resamples, seed=seed)
    hit_rate = (
        sum(1 for value in samples if value > 0.0) / len(samples) if samples else None
    )
    return {
        "trades": len(samples),
        "net_r": (sum(samples) / len(samples)) if samples else None,
        "net_r_samples": tuple(samples),
        "max_drawdown": max_drawdown if samples else None,
        "hit_rate": hit_rate,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "open_campaigns": open_campaigns,
        "risk_basis": risk_basis,
        "unsupported": tuple(sorted(set(unsupported))),
    }


def run_native_baseline(
    *,
    tape: Path | str,
    instrument: str | None = None,
    initial_balance: Decimal = Decimal("10000"),
    per_leg_notional: Decimal = Decimal("1000"),
    maker_fee: Decimal = Decimal("0.0002"),
    taker_fee: Decimal = Decimal("0.0005"),
    execution_profile: str = "baseline",
    bracket_stop_pct: Decimal | None = Decimal("0.02"),
    bracket_target_pct: Decimal | None = Decimal("0.04"),
    bootstrap_resamples: int = BOOTSTRAP_RESAMPLES,
    bootstrap_seed: int = BOOTSTRAP_SEED,
) -> ArmResult:
    """Replay the V8 baseline once (twice for funding isolation) on a real tape.

    The two passes are the *same* strategy over the same bars: one receives the
    tape's funding rows and one receives none. ``funding_total`` is their balance
    difference, and the trade signatures must match or the isolation is refused.
    """
    from v8_next.adapters.execution_models import profile_digest, profile_summary
    from v8_next.adapters.portfolio_backtest import (
        SleeveSpec,
        run_portfolio_backtest,
        trade_signature,
    )
    from v8_next.evaluation.multitape import load_multitape

    tape_argument = Path(tape)
    loaded = load_multitape(tape_argument)
    if not loaded.candles:
        raise ValueError(f"tape {tape_argument} carries no kline legs")
    selected_instrument = instrument or sorted(loaded.candles)[0]
    if selected_instrument not in loaded.candles:
        raise ValueError(
            f"instrument {selected_instrument!r} absent from tape; "
            f"available: {sorted(loaded.candles)}"
        )
    legs = {selected_instrument: loaded.candles[selected_instrument]}
    funding = tuple(row for row in loaded.funding if row.instrument == selected_instrument)
    sleeves = (SleeveSpec("incumbent", 1, 28, 1.0),)
    run_kwargs: dict[str, Any] = {
        "per_leg_notional": per_leg_notional,
        "maker_fee": maker_fee,
        "taker_fee": taker_fee,
        "initial_balance": initial_balance,
        "bracket_stop_pct": bracket_stop_pct,
        "bracket_target_pct": bracket_target_pct,
        "execution_profile": execution_profile,
        "funding_dropped": loaded.funding_dropped,
    }

    funded = run_portfolio_backtest(legs, sleeves, funding, **run_kwargs)
    unfunded = run_portfolio_backtest(legs, sleeves, (), **run_kwargs)

    funded_execution: Mapping[str, Any] = funded.get("execution") or {}
    config_payload: dict[str, Any] = {
        "arm_id": BASELINE_ARM_ID,
        "instrument": selected_instrument,
        "sleeves": [["incumbent", 1, 28, 1.0]],
        "initial_balance": str(initial_balance),
        "per_leg_notional": str(per_leg_notional),
        "maker_fee": str(maker_fee),
        "taker_fee": str(taker_fee),
        "bracket_stop_pct": None if bracket_stop_pct is None else str(bracket_stop_pct),
        "bracket_target_pct": (
            None if bracket_target_pct is None else str(bracket_target_pct)
        ),
        "execution_profile": profile_summary(execution_profile),
        "engine": "nautilus_trader",
        "engine_version": _engine_version(),
    }
    config_sha = sha256_json(config_payload)

    initial = float(initial_balance)
    balance = _money((funded.get("account") or {}).get("balance_total"))
    nofund_balance = _money((unfunded.get("account") or {}).get("balance_total"))
    net_pnl = None if balance is None else float(balance) - initial

    signature_ok = trade_signature(funded) == trade_signature(unfunded)
    funding_total: float | None = None
    unsupported: list[str] = [MICROSTRUCTURE_UNSUPPORTED_TOKEN]
    coverage = str(funded.get("funding_coverage") or "")
    if not funding:
        unsupported.append(
            "FUNDING_UNSUPPORTED_NO_ROWS: tape carries no funding rows for this instrument; "
            "a zero here would be absence, not a measured cost"
        )
    elif coverage in ("DROPPED_RECORDS",):
        unsupported.append(
            f"FUNDING_COVERAGE_INCOMPLETE:{coverage}: funding cost is understated by the "
            "records the tape loader dropped"
        )
    if not funding:
        funding_total = None
    elif signature_ok and balance is not None and nofund_balance is not None:
        funding_total = float(balance) - float(nofund_balance)
    else:
        unsupported.append(
            "FUNDING_ISOLATION_UNSUPPORTED: funding and no-funding replays diverged on "
            "trade signature or balance; funding cost cannot be separated"
        )

    fill_records = list(funded.get("fill_records") or [])
    fee_total, unparsable_fees = _commission_total(fill_records)
    if unparsable_fees:
        unsupported.append(
            f"FEE_TOTAL_PARTIAL:{unparsable_fees} fill commission entry/entries unparsable"
        )

    metrics = _metrics_from_records(
        opened=list(funded.get("opened_positions") or []),
        closed=list(funded.get("closed_positions") or []),
        initial_balance=initial,
        bracket_stop_pct=bracket_stop_pct,
        resamples=bootstrap_resamples,
        seed=bootstrap_seed,
    )
    unsupported.extend(metrics["unsupported"])
    if metrics["ci_low"] is None:
        unsupported.append("INSUFFICIENT_CLOSED_CAMPAIGNS_FOR_CI")

    slippage_basis = (
        f"PROFILE={funded_execution.get('profile')} "
        f"FILL_MODEL={funded_execution.get('fill_model')} "
        f"SLIPPED={funded_execution.get('fill_model_slipped')} "
        f"PROB_SLIPPAGE={funded_execution.get('prob_slippage')} "
        f"MEASURED_SLIPPAGE_BPS_MEAN={funded_execution.get('slippage_bps_mean')}; "
        "a model assumption, not venue slippage"
    )
    funding_basis = (
        f"TAPE_FUNDING_ROWS={len(funding)} "
        f"COVERAGE={funded.get('funding_coverage')} "
        f"SETTLEMENTS_FED={funded.get('funding_settlements_fed')} "
        f"DROPPED_AT_LOAD={funded.get('funding_dropped_at_load')} "
        f"MARK={funded.get('funding_mark_basis')} "
        f"DUAL_RUN_ISOLATION={'VALID' if signature_ok else 'DIVERGED'}"
    )
    provenance = Provenance(
        tape_sha256=loaded.tape_sha256,
        tape_manifest_sha256=_manifest_sha(tape_argument),
        config_sha256=config_sha,
        engine="nautilus_trader.BacktestEngine",
        engine_version=_engine_version(),
        execution_profile=str(funded_execution.get("profile") or execution_profile),
        execution_profile_digest=str(
            funded_execution.get("digest") or profile_digest(execution_profile)
        ),
        fee_model=str(funded_execution.get("fee_model") or "maker_taker"),
        maker_fee=str(maker_fee),
        taker_fee=str(taker_fee),
        funding_basis=funding_basis,
        mark_basis=str(
            funded.get("funding_mark_basis")
            or "OHLC_CLOSE_ASOF_BOUNDED_HISTORICAL_MODEL_NOT_VENUE_MARK"
        ),
        intrabar_tie_basis=INTRABAR_TIE_BASIS,
        slippage_basis=slippage_basis,
        partial_fills_basis=_partial_fill_basis(fill_records),
    )
    return ArmResult(
        arm_id=BASELINE_ARM_ID,
        intervention=None,
        costed=True,
        trades=metrics["trades"],
        net_r=metrics["net_r"],
        net_pnl=net_pnl,
        max_drawdown=metrics["max_drawdown"],
        hit_rate=metrics["hit_rate"],
        fee_total=fee_total,
        funding_total=funding_total,
        ci_low=metrics["ci_low"],
        ci_high=metrics["ci_high"],
        ci_method=(
            f"BOOTSTRAP_PERCENTILE_{int(CI_LEVEL * 100)}_OF_MEAN "
            f"(resamples={bootstrap_resamples}, seed={bootstrap_seed}, "
            "unit=per-campaign net R)"
        ),
        provenance=provenance,
        net_r_samples=metrics["net_r_samples"],
        risk_basis=metrics["risk_basis"],
        open_campaigns=metrics["open_campaigns"],
        unsupported=tuple(sorted(set(unsupported))),
    )


def run_ablation(
    *,
    tape: Path | str,
    supplied_arms: Mapping[Intervention, ArmResult] | None = None,
    run_baseline: bool = True,
    instrument: str | None = None,
    initial_balance: Decimal = Decimal("10000"),
    per_leg_notional: Decimal = Decimal("1000"),
    maker_fee: Decimal = Decimal("0.0002"),
    taker_fee: Decimal = Decimal("0.0005"),
    execution_profile: str = "baseline",
    bracket_stop_pct: Decimal | None = Decimal("0.02"),
    bracket_target_pct: Decimal | None = Decimal("0.04"),
    bootstrap_resamples: int = BOOTSTRAP_RESAMPLES,
    bootstrap_seed: int = BOOTSTRAP_SEED,
    notes: Sequence[str] = (),
) -> AblationReport:
    """Drive the costed baseline replay and assemble every intervention arm.

    No V9 intervention is ever simulated. An intervention arm appears as a
    treatment only when the caller supplies an already-produced costed result.
    """
    baseline: ArmResult | None = None
    if run_baseline:
        baseline = run_native_baseline(
            tape=tape,
            instrument=instrument,
            initial_balance=initial_balance,
            per_leg_notional=per_leg_notional,
            maker_fee=maker_fee,
            taker_fee=taker_fee,
            execution_profile=execution_profile,
            bracket_stop_pct=bracket_stop_pct,
            bracket_target_pct=bracket_target_pct,
            bootstrap_resamples=bootstrap_resamples,
            bootstrap_seed=bootstrap_seed,
        )
    return build_report(baseline=baseline, supplied_arms=supplied_arms, notes=notes)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _load_supplied_arm(path: Path, intervention: Intervention) -> ArmResult:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and "costed" in payload and "arm_id" in payload:
        arm = ArmResult.from_dict(payload)
    elif isinstance(payload, dict) and isinstance(payload.get("arms"), list):
        matches = [
            ArmResult.from_dict(entry)
            for entry in payload["arms"]
            if isinstance(entry, dict)
            and str(entry.get("intervention")) == intervention.value
        ]
        costed = [arm for arm in matches if arm.costed]
        if costed:
            matches = costed
        if not matches:
            raise ValueError(
                f"{path} carries no arm for intervention {intervention.value}"
            )
        arm = matches[0]
    else:
        raise ValueError(f"{path} is neither an ArmResult nor an ablation report")
    if arm.intervention != intervention:
        raise ValueError(
            f"{path} declares intervention {arm.intervention}; expected {intervention.value}"
        )
    return arm


def _parse_supplied(values: Sequence[str]) -> dict[Intervention, ArmResult]:
    supplied: dict[Intervention, ArmResult] = {}
    for value in values:
        name, separator, raw_path = value.partition("=")
        if not separator or not raw_path:
            raise SystemExit(
                f"--supplied-arm expects INTERVENTION=PATH, got {value!r}"
            )
        try:
            intervention = Intervention(name.upper())
        except ValueError as exc:
            raise SystemExit(f"unknown intervention {name!r}") from exc
        supplied[intervention] = _load_supplied_arm(Path(raw_path), intervention)
    return supplied


def _print_summary(report: AblationReport) -> None:
    print(f"status: {report.status}")
    baseline = report.baseline
    if baseline is None:
        print("baseline: NONE (run_baseline disabled or failed)")
    else:
        print(
            "baseline: "
            f"costed={baseline.costed} trades={baseline.trades} "
            f"net_r={baseline.net_r} net_pnl={baseline.net_pnl} "
            f"max_dd={baseline.max_drawdown} hit_rate={baseline.hit_rate} "
            f"fees={baseline.fee_total} funding={baseline.funding_total}"
        )
    for arm in report.arms:
        if arm.costed:
            print(f"arm {arm.arm_id}: COSTED trades={arm.trades} net_r={arm.net_r}")
        else:
            print(f"arm {arm.arm_id}: NOT_RUN {', '.join(arm.unsupported)}")
    for delta in report.deltas:
        print(
            f"delta {delta.intervention.value}: net_r={delta.delta_net_r} "
            f"net_pnl={delta.delta_net_pnl} "
            f"ci=[{delta.delta_ci_low}, {delta.delta_ci_high}]"
        )
    print(f"microstructure: {report.microstructure}")


def _peak_rss_bytes() -> tuple[int, str]:
    """Process peak RSS and its platform unit basis (``ru_maxrss`` is bytes on macOS)."""
    import resource

    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return int(raw), "ru_maxrss_bytes_macos"
    return int(raw) * 1024, "ru_maxrss_kilobytes_converted"


def main(argv: Sequence[str] | None = None) -> int:
    import time

    started = time.perf_counter()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tape", required=True, help="authorised tape directory or jsonl")
    parser.add_argument("--out", required=True, help="path for the ablation JSON")
    parser.add_argument("--instrument", default=None)
    parser.add_argument("--initial-balance", default="10000")
    parser.add_argument("--per-leg-notional", default="1000")
    parser.add_argument("--maker-fee", default="0.0002")
    parser.add_argument("--taker-fee", default="0.0005")
    parser.add_argument("--execution-profile", default="baseline")
    parser.add_argument("--bracket-stop-pct", default="0.02")
    parser.add_argument("--bracket-target-pct", default="0.04")
    parser.add_argument("--bootstrap-resamples", type=int, default=BOOTSTRAP_RESAMPLES)
    parser.add_argument("--bootstrap-seed", type=int, default=BOOTSTRAP_SEED)
    parser.add_argument(
        "--supplied-arm",
        action="append",
        default=[],
        help="INTERVENTION=PATH to an already-produced costed ArmResult",
    )
    parser.add_argument(
        "--no-baseline",
        action="store_true",
        help="do not replay; publish NOT_RUN instead of inventing a baseline",
    )
    parser.add_argument(
        "--record-measurement",
        action="store_true",
        help=(
            "append this process's wall time and peak RSS to the artifact; these two "
            "fields are the only non-deterministic part of the JSON"
        ),
    )
    args = parser.parse_args(argv)

    def _optional_decimal(value: str | None) -> Decimal | None:
        if value is None or value.strip().lower() in ("", "none"):
            return None
        return Decimal(value)

    stop_pct = _optional_decimal(args.bracket_stop_pct)
    target_pct = _optional_decimal(args.bracket_target_pct)
    report = run_ablation(
        tape=args.tape,
        supplied_arms=_parse_supplied(args.supplied_arm),
        run_baseline=not args.no_baseline,
        instrument=args.instrument,
        initial_balance=Decimal(args.initial_balance),
        per_leg_notional=Decimal(args.per_leg_notional),
        maker_fee=Decimal(args.maker_fee),
        taker_fee=Decimal(args.taker_fee),
        execution_profile=args.execution_profile,
        bracket_stop_pct=stop_pct,
        bracket_target_pct=target_pct,
        bootstrap_resamples=args.bootstrap_resamples,
        bootstrap_seed=args.bootstrap_seed,
    )
    payload = report.to_dict()
    payload["invocation"] = {
        "module": "v8_next.evaluation.ablations",
        "argv": list(sys.argv[1:] if argv is None else argv),
    }
    if args.record_measurement:
        peak_rss, peak_basis = _peak_rss_bytes()
        payload["measurement"] = {
            "wall_seconds": round(time.perf_counter() - started, 3),
            "peak_rss_bytes": peak_rss,
            "peak_rss_basis": peak_basis,
            "scope": "whole CLI process, measured at JSON write time",
        }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    _print_summary(report)
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
