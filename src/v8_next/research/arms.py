"""Declarative research arms.

An arm is a *policy variant*, never a new engine. ``kind`` decides the evidence
class:

``native``
    Runs the existing NautilusTrader replay with declared config overrides and
    produces ``OBSERVED_NATIVE`` economics.

``screen``
    Runs :mod:`v8_next.research.screen` counterfactuals over the shared baseline
    and produces ``MODEL_DERIVED`` economics.

Every arm declares which variables it changes and which it holds fixed, so
confounding is visible in the artifact rather than in prose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Arm:
    arm_id: str
    experiment_id: str
    description: str
    kind: str  # "native" | "screen"
    is_baseline: bool = False
    changes: tuple[str, ...] = ()
    holds_fixed: tuple[str, ...] = ()
    config_overrides: dict[str, Any] = field(default_factory=dict)
    execution_profile: str | None = None
    screen_policy: str | None = None
    entry_offset_bars: int = 0
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "arm_id": self.arm_id,
            "experiment_id": self.experiment_id,
            "description": self.description,
            "kind": self.kind,
            "is_baseline": self.is_baseline,
            "changes": list(self.changes),
            "holds_fixed": list(self.holds_fixed),
            "config_overrides": {
                key: str(value) for key, value in self.config_overrides.items()
            },
            "execution_profile": self.execution_profile,
            "screen_policy": self.screen_policy,
            "entry_offset_bars": self.entry_offset_bars,
            "notes": list(self.notes),
            "evidence_class": "OBSERVED_NATIVE" if self.kind == "native" else "MODEL_DERIVED",
        }


ARMS: tuple[Arm, ...] = (
    # ---------------- Experiment A: signal / expert value ----------------
    Arm(
        arm_id="A0",
        experiment_id="A",
        description="Canonical ensemble baseline (support quorum 1, tolerance 28).",
        kind="native",
        is_baseline=True,
        changes=(),
        holds_fixed=("entry_policy", "exit_policy", "risk_budget", "dataset"),
    ),
    Arm(
        arm_id="A1",
        experiment_id="A",
        description="Grammar-only eligibility: the expert filter cannot reject an opportunity.",
        kind="native",
        config_overrides={"min_support_quorum": 0, "max_contradiction_tolerance": 28},
        changes=("expert_gate",),
        holds_fixed=("grammar", "entry_policy", "exit_policy", "risk_budget", "dataset"),
        notes=(
            "quorum 0 / tolerance 28 makes any detected opportunity supported; the only "
            "remaining gate is position occupancy.",
        ),
    ),
    Arm(
        arm_id="A2",
        experiment_id="A",
        description="Opportunity outcomes conditioned on the selected expert's stance.",
        kind="screen",
        screen_policy="EXPERT_STANCE_CONDITIONING",
        changes=("eligibility (analysis-only)",),
        holds_fixed=("grammar", "entry_policy", "exit_policy", "risk_budget", "dataset"),
        notes=(
            "Rows with a SUPPORT stance are compared with rows whose stance is "
            "ABSTAIN/CONTRADICT; the gate is not re-run natively.",
        ),
    ),
    Arm(
        arm_id="A3",
        experiment_id="A",
        description="Seeded random-entry control matched on direction and volatility bucket.",
        kind="screen",
        screen_policy="MATCHED_RANDOM",
        changes=("opportunity selection",),
        holds_fixed=("entry_policy", "exit_policy", "risk_budget", "dataset"),
        notes=("Reported as a distribution over seeds, never as the best seed.",),
    ),
    # ---------------- Experiment B: entry / execution ----------------
    Arm(
        arm_id="B0",
        experiment_id="B",
        description="Baseline same-close MARKET fill (declared simulation assumption).",
        kind="native",
        is_baseline=True,
    ),
    Arm(
        arm_id="B1",
        experiment_id="B",
        description="Enter at the close of the first bar that legally follows the signal.",
        kind="screen",
        screen_policy="NEXT_BAR_ENTRY",
        entry_offset_bars=1,
        changes=("entry_timing",),
        holds_fixed=("grammar", "expert_gate", "exit_policy", "risk_budget", "dataset"),
        notes=("Screened baseline static bracket on the same 123 detected opportunities.",),
    ),
    Arm(
        arm_id="B2",
        experiment_id="B",
        description="Time-limited retest LIMIT at the detection level (6-bar TTL).",
        kind="screen",
        screen_policy="RETEST_LIMIT_TTL",
        changes=("entry_timing", "order_type"),
        holds_fixed=("grammar", "exit_policy", "risk_budget", "dataset"),
        notes=("Touch equals fill (TOUCH_FILL_MODEL); unfilled orders stay in the denominator.",),
    ),
    Arm(
        arm_id="B3",
        experiment_id="B",
        description="Baseline policy under the existing conservative execution profile.",
        kind="native",
        execution_profile="realistic",
        changes=("fill/latency model",),
        holds_fixed=("grammar", "expert_gate", "entry_timing", "exit_policy", "dataset"),
    ),
    Arm(
        arm_id="B4",
        experiment_id="B",
        description="NATIVE next-executable entry: the previous bar's opportunity must still hold.",
        kind="native",
        config_overrides={"research_require_confirmation": True},
        changes=("entry_timing",),
        holds_fixed=("grammar", "expert_gate", "exit_policy", "risk_budget", "dataset"),
        notes=(
            "Amendment AM-001. Same engine, same exit contract; only the admission "
            "lifecycle is delayed by one closed bar and must be confirmed.",
        ),
    ),
    # ---------------- Experiment C: position management ----------------
    Arm(
        arm_id="C0",
        experiment_id="C",
        description="Static bracket baseline, re-evaluated on the model path.",
        kind="screen",
        is_baseline=True,
        screen_policy="STATIC",
        changes=(),
    ),
    Arm(
        arm_id="C1",
        experiment_id="C",
        description="Move the stop to breakeven plus round-trip cost after +1R.",
        kind="screen",
        screen_policy="BREAKEVEN_1R",
        changes=("stop_management",),
        holds_fixed=("entries", "initial_stop", "target", "risk_budget", "dataset"),
    ),
    Arm(
        arm_id="C2",
        experiment_id="C",
        description="Realise 50% at +1R, keep the remainder under the initial bracket.",
        kind="screen",
        screen_policy="PARTIAL_50_1R",
        changes=("position_size_management",),
        holds_fixed=("entries", "initial_stop", "target", "dataset"),
        notes=("Maker fee applied to the partial; quantity precision not modelled.",),
    ),
    Arm(
        arm_id="C3",
        experiment_id="C",
        description="Time stop: flat exit after 18 closed bars in the position.",
        kind="screen",
        screen_policy="TIME_STOP",
        changes=("time_in_market",),
        holds_fixed=("entries", "initial_stop", "target", "risk_budget", "dataset"),
    ),
    Arm(
        arm_id="C4",
        experiment_id="C",
        description="NATIVE breakeven after +1R (research-only exit policy, default off).",
        kind="native",
        config_overrides={"research_exit_policy": "BREAKEVEN_1R"},
        changes=("stop_management",),
        holds_fixed=("entries", "initial_stop", "target", "risk_budget", "dataset"),
        notes=(
            "Amendment AM-001. Exercises the same causal rule as the C1 screen, but "
            "through NautilusTrader's native order modification.",
        ),
    ),
    # ---------------- Experiment D: stateful opportunity lifecycle ----------------
    Arm(
        arm_id="D0",
        experiment_id="D",
        description="Same-bar admission of every detected opportunity (screened baseline).",
        kind="screen",
        is_baseline=True,
        screen_policy="SAME_BAR",
        entry_offset_bars=0,
    ),
    Arm(
        arm_id="D1",
        experiment_id="D",
        description="Require the next closed bar to confirm before entering.",
        kind="screen",
        screen_policy="CONFIRM_NEXT_BAR",
        entry_offset_bars=1,
        changes=("opportunity_lifecycle",),
        holds_fixed=("grammar", "exit_policy", "risk_budget", "dataset"),
        notes=("Post-breakout confirmation only; this does not predict the breakout.",),
    ),
    Arm(
        arm_id="D3",
        experiment_id="D",
        description="NATIVE persistent confirmation: previous-bar opportunity must hold.",
        kind="native",
        config_overrides={"research_require_confirmation": True},
        changes=("opportunity_lifecycle",),
        holds_fixed=("grammar", "exit_policy", "risk_budget", "dataset"),
        notes=("Amendment AM-001. Same gate as B4 but reported as a lifecycle arm.",),
    ),
    Arm(
        arm_id="D2",
        experiment_id="D",
        description="Bounded retest lifecycle with TTL=6, invalidation on stop breach.",
        kind="screen",
        screen_policy="RETEST_TTL",
        changes=("opportunity_lifecycle",),
        holds_fixed=("grammar", "exit_policy", "risk_budget", "dataset"),
    ),
    Arm(
        arm_id="D4",
        experiment_id="D",
        description=(
            "NATIVE persistent retest/reclaim lifecycle "
            "(D4_RETEST_RECLAIM_LIFECYCLE, TTL=6, reclaim 0.1R, invalidation 0.5R)."
        ),
        kind="native",
        config_overrides={"research_opportunity_lifecycle": "D4_RETEST_RECLAIM_LIFECYCLE"},
        changes=("opportunity_lifecycle",),
        holds_fixed=(
            "grammar",
            "expert_gate",
            "exit_policy",
            "risk_budget",
            "dataset",
        ),
        notes=(
            "D4 continuation. Not a rename of B4/D3: admission requires WATCHING -> "
            "RETEST_SEEN -> ARMED on three distinct completed bars, with INVALIDATED / "
            "EXPIRED / CANCELLED terminal outcomes. Default off.",
        ),
    ),
)

ARM_BY_ID: dict[str, Arm] = {arm.arm_id: arm for arm in ARMS}


def arms_for(experiment_id: str) -> list[Arm]:
    return [arm for arm in ARMS if arm.experiment_id == experiment_id]


def get_arm(arm_id: str) -> Arm:
    try:
        return ARM_BY_ID[arm_id]
    except KeyError:
        raise KeyError(f"unknown arm {arm_id!r}; known: {sorted(ARM_BY_ID)}") from None
