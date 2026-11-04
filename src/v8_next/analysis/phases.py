"""Regret phases 1-3 — port of ``v8-core/src/analysis/phase1.rs``, ``phase2.rs`` and
``phase3.rs`` (S6, issues #118/#119/#120; FCR-V8RR-007/-009).

One module, three phases, mirroring the Rust layout so a reader of either tree finds the same
names. Nothing here is a re-derivation of an estimator: every statistic is the **frozen oracle**
of ``src/v8/statistics.py`` (``select_block_size``, block bootstrap, ``effective_independent_
episodes``, ``practical_significance``, ``expected_false_positives``, ``effective_search_size``),
and every float is computed from the rows passed in — there is not one hardcoded p-value, effect
size, interval or verdict in this file.

* **Phase 1** (``join_dataset``) is DESCRIPTIVE ONLY: it joins one regret gap record per
  Candidate with its ACTUAL action's cube row and tags the row with the symbol its store was
  built for. It does not slice, does not test significance, and does not sum Candidate-local
  gaps into a portfolio claim. Every population-style number carries :data:`LABEL`
  (``MODEL_DERIVED_DESCRIPTIVE_NOT_YET_GATED``); each row's ``epistemic_class`` is the row-level
  tag ``MODEL_DERIVED`` (``v8_next.oracle.Identifiability``), which is deliberately *not* the
  dataset tag — conflating them broke S6 parity (939 divergences in the #117 harness).
* **Phase 2** (``declare_slices`` / ``score_slice`` / ``discovery_summary`` /
  ``ConfirmationLedger``) declares the frozen 72-slice family (3 Experts x 6 symbols x 2
  directions x 2 estimands) BEFORE any data is touched, scores it on the chronological discovery
  half, and queries any ``CANDIDATE_SYSTEMATIC`` slice against the untouched confirmation half
  **exactly once** (a second query is a hard error, FCR-V8RR-007 AP002 / protocol §8.4).
* **Phase 3** (``declare_policies`` / ``apply_policy`` / ``evaluate_slice_recoverability`` /
  ``run_phase3``) enumerates the declared decision-time policy class (ALWAYS_TRADE + 24
  threshold gates), selects the best policy on the discovery half, and estimates ``V_A``,
  ``V_R`` and ``G_R = V_R - V_A`` on the untouched confirmation half with the same block
  bootstrap.

**Determinism (FT004).** The per-slice seed is ``seed_for(key)`` =
``int(sha1_hex(json.dumps(key))[:8], 16)`` — the JSON-encoded key *with its surrounding quotes*
enters the digest. Same key ⇒ same seed ⇒ bit-identical result; the derivation never reads a
wall clock and never depends on run order.

**Fail-closed.** Degenerate inputs never silently produce a number that reads like a
measurement: too few computed rows yields ``INSUFFICIENT_SUPPORT`` / ``EXCLUDED_EMPTY``; a
confirmation half below the support floor yields ``FAILED_CONFIRMATION``; a recoverability
series below the interval floor, with no non-zero deltas, or with no confirmation rows yields
``NOT_RECOVERABLE_WITHIN_CLASS`` plus a named ``recoverability_reason``. There is no ``p = 0``,
no perfect confidence and no interval over three points.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from v8_next.oracle import Identifiability

#: The Phase-1 honesty label — the DATASET tag every population-style number carries
#: (``tools/regret_phase1.py`` ``LABEL``; phase1.rs:31). Not to be confused with a row's
#: ``epistemic_class`` (see :data:`EpistemicClass`).
LABEL: str = "MODEL_DERIVED_DESCRIPTIVE_NOT_YET_GATED"

#: The row-level epistemic tag Phase 1 stamps every joined row with — the ported
#: ``v8_next.oracle.Identifiability`` value, not a local string (phase1.rs:164).
EpistemicClass: str = Identifiability.MODEL_DERIVED.value

#: Named divergences from the Rust module. Each entry is a thing deliberately NOT ported
#: verbatim, with the reason — never a silent drop.
DIVERGENCES: tuple[str, ...] = (
    "opportunity_set / regret::Manifest: ``v8-core/src/regret.rs`` (the A(C) manifest, "
    "``generate_legal_actions``, the target_r x expiry_bars grid and ``action_id`` encoding) "
    "is a separate module with no Python counterpart yet; this port takes any object exposing "
    "``.actions`` (sequence of ``LegalAction``) and projects it, exactly as phase1.rs projects "
    "``crate::regret::Manifest``. The manifest is never re-derived here.",
    "Cube / feature-store plumbing: the Rust ``analysis`` subcommand and the CubeReducer that "
    "persist ``cube.jsonl`` keyed by (candidate, action) have no Python counterpart in the "
    "phases plane; Phase 1's ``Phase0Output`` is the in-memory shape the oracle reads from a "
    "store, and the store reader itself is out of scope.",
    "Failure-domain classification: phases 1-3 emit statistical verdicts, not pipeline failure "
    "domains. Any mapping of a verdict onto one of the seven "
    "``v8_next.system_proving.attribution.FailureDomain`` values would be an invented ontology, "
    "so this module emits none.",
    "``block_bootstrap_indices`` (phase2.rs:178, ``#[allow(dead_code)]``) is a private helper in "
    "Rust; here it is the single internal ``_block_bootstrap_indices`` shared by both phases, "
    "which is the same sampler ``src/v8/statistics.py`` drives.",
    "``run_phase3`` file output: the Rust function appends ``recoverability_attempts.jsonl`` and "
    "writes ``phase3_summary.json`` under ``out_dir``. This port keeps that behaviour (``out_dir`` "
    "is a required argument) so the artifact contract is unchanged.",
    "Phase 3 recoverability now carries a named ``recoverability_reason`` and refuses to form an "
    "interval over fewer than ``MIN_INTERVAL_POINTS`` points (see ``SEMANTIC_AMBIGUITIES``); the "
    "Rust path silently returns ``(0.0, 0.0)``. The verdict vocabulary is unchanged.",
)

#: Places where the Rust semantics were ambiguous or contradicted the fail-closed /
#: no-fabrication rules, recorded so the divergence is auditable rather than silent.
SEMANTIC_AMBIGUITIES: tuple[str, ...] = (
    "phase2.rs phase-2 comment claims ``crate::state::fsum`` is bit-identical to CPython "
    "``sum()`` *and* to ``math.fsum``. In CPython 3.12 ``sum()`` uses a compensated accumulator "
    "while ``math.fsum`` uses the ``_PyFloat_Fsum`` partials algorithm; the two are not the same "
    "function. The Rust ``fsum`` implements the partials algorithm, so this port uses "
    "``math.fsum`` throughout — the conservative choice that matches the Rust authority.",
    "phase3.rs returns ``(0.0, 0.0)`` for an empty/non-varying delta series and would still "
    "bootstrap a NON-varying-but-nonzero series of n<4. The repository's fail-closed rule "
    "forbids an interval over three points, so this port reports no interval and a named "
    "``INSUFFICIENT_CONFIRMATION_SUPPORT`` reason instead of a fabricated interval.",
    "phase3.rs ``evaluate_slice_recoverability`` reports ``NOT_RECOVERABLE_WITHIN_CLASS`` with no "
    "reason when there are zero confirmation rows, zero effective episodes or no non-zero "
    "deltas. The fail-closed rule requires a named reason, so this port adds "
    "``recoverability_reason`` without changing the verdict.",
    "phase2.rs ``effective_search_size`` asserts ``search_universe_size >= variants_evaluated``; "
    "in both call sites the two arguments are the same value (72), so the D-046 guard is "
    "vacuous. This port keeps the guard as a raised ``ValueError`` rather than a silent "
    "``max`` so an understated family size cannot pass.",
)

# ---------------------------------------------------------------------------
# Phase 1 — candidate-local opportunity accounting (phase1.rs)
# ---------------------------------------------------------------------------

#: The oracle's frozen Phase-1 field count (join_dataset output; phase1.rs ``FIELD_COUNT``,
#: pinned by ``JoinedCandidateRow.__dataclass_fields__`` in ``tools/regret_phase1.py``).
JOINED_ROW_FIELD_COUNT: int = 19


@dataclass(frozen=True)
class CandidateIdentity:
    """The candidate identity a Phase-1 join needs (phase1.rs:74).

    ``expert_id``, ``direction`` and ``birth_time``, where ``birth_time`` is the DETECTED
    transition's own knowledge_time — the only decision-time-defined clock a Candidate carries
    before any action is taken (FT002).
    """

    expert_id: str = ""
    direction: str = ""
    birth_time: int = 0


@dataclass(frozen=True)
class GapRecord:
    """The per-Candidate regret gap — the fields of the oracle's ``RegretRecord`` the join
    carries (phase1.rs:83; gap-status vocabulary is ``regret.rs``'s)."""

    candidate_id: str
    actual_action_id: str | None = None
    actual_utility: float | None = None
    best_utility: float | None = None
    tie_cardinality: int = 0
    legal_hindsight_gap: float | None = None
    gap_status: str = ""


@dataclass(frozen=True)
class CubeAccumulators:
    """The ACTUAL action's cube-reduced accumulators — the eight value fields the join carries
    (phase1.rs:98): endpoint, label_status, horizon_bars, cost_r, funding_r, mae_r, mfe_r,
    ambiguous_bars, as the CubeReducer persists them per (candidate, action)."""

    endpoint: str | None = None
    label_status: str | None = None
    horizon_bars: int | None = None
    cost_r: float | None = None
    funding_r: float | None = None
    mae_r: float | None = None
    mfe_r: float | None = None
    ambiguous_bars: int | None = None


@dataclass
class Phase0Output:
    """One symbol's Phase-0 output, in memory (phase1.rs:113): the candidate identities, the
    gap records (``regret.jsonl``) and the cube accumulators (``cube.jsonl``) keyed by
    ``(candidate_id, action_id)``, as the oracle reads them from a store."""

    identities: dict[str, CandidateIdentity] = field(default_factory=dict)
    gaps: list[GapRecord] = field(default_factory=list)
    cubes: dict[tuple[str, str], CubeAccumulators] = field(default_factory=dict)


@dataclass(frozen=True)
class JoinedCandidateRow:
    """One row of the frozen Phase-1 dataset (phase1.rs:38): a regret gap record joined with
    its ACTUAL action's cube row and tagged with the symbol its store was built for. 19 fields,
    exactly as ``tools/regret_phase1.py`` ``JoinedCandidateRow``."""

    symbol: str
    candidate_id: str
    expert_id: str
    direction: str
    birth_time: int
    gap_status: str
    legal_hindsight_gap: float | None
    actual_utility: float | None
    best_utility: float | None
    tie_cardinality: int
    endpoint: str | None
    label_status: str | None
    horizon_bars: int | None
    cost_r: float | None
    funding_r: float | None
    mae_r: float | None
    mfe_r: float | None
    ambiguous_bars: int | None
    epistemic_class: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "candidate_id": self.candidate_id,
            "expert_id": self.expert_id,
            "direction": self.direction,
            "birth_time": self.birth_time,
            "gap_status": self.gap_status,
            "legal_hindsight_gap": self.legal_hindsight_gap,
            "actual_utility": self.actual_utility,
            "best_utility": self.best_utility,
            "tie_cardinality": self.tie_cardinality,
            "endpoint": self.endpoint,
            "label_status": self.label_status,
            "horizon_bars": self.horizon_bars,
            "cost_r": self.cost_r,
            "funding_r": self.funding_r,
            "mae_r": self.mae_r,
            "mfe_r": self.mfe_r,
            "ambiguous_bars": self.ambiguous_bars,
            "epistemic_class": self.epistemic_class,
        }


#: The oracle's field count, exposed as the Rust associated constant ``JoinedCandidateRow::
#: FIELD_COUNT``. Set after the class body so it stays a plain class attribute and never
#: becomes a 20th dataclass field.
JoinedCandidateRow.FIELD_COUNT = JOINED_ROW_FIELD_COUNT  # type: ignore[attr-defined]


def join_dataset(per_symbol: Sequence[tuple[str, Phase0Output]]) -> list[JoinedCandidateRow]:
    """The frozen Phase-1 dataset (phase1.rs:122 / ``tools/regret_phase1.py:join_dataset``).

    One :class:`JoinedCandidateRow` per gap record across every symbol; symbols iterated sorted,
    rows in gap-record order per symbol. A gap record with no ``actual_action_id`` — or whose
    action is absent from the reduced cube table — yields ``None`` cube fields (the oracle's
    ``cube_by_key.get`` miss), never a zero placeholder.
    """
    rows: list[JoinedCandidateRow] = []
    for symbol, out in sorted(per_symbol, key=lambda item: item[0]):
        for g in out.gaps:
            ident = out.identities.get(g.candidate_id, CandidateIdentity())
            actual: CubeAccumulators | None = None
            if g.actual_action_id is not None:
                actual = out.cubes.get((g.candidate_id, g.actual_action_id))
            rows.append(
                JoinedCandidateRow(
                    symbol=symbol,
                    candidate_id=g.candidate_id,
                    expert_id=ident.expert_id,
                    direction=ident.direction,
                    birth_time=ident.birth_time,
                    gap_status=g.gap_status,
                    legal_hindsight_gap=g.legal_hindsight_gap,
                    actual_utility=g.actual_utility,
                    best_utility=g.best_utility,
                    tie_cardinality=g.tie_cardinality,
                    endpoint=actual.endpoint if actual is not None else None,
                    label_status=actual.label_status if actual is not None else None,
                    horizon_bars=actual.horizon_bars if actual is not None else None,
                    cost_r=actual.cost_r if actual is not None else None,
                    funding_r=actual.funding_r if actual is not None else None,
                    mae_r=actual.mae_r if actual is not None else None,
                    mfe_r=actual.mfe_r if actual is not None else None,
                    ambiguous_bars=actual.ambiguous_bars if actual is not None else None,
                    epistemic_class=EpistemicClass,
                )
            )
    return rows


@dataclass(frozen=True)
class LegalAction:
    """One legal action of the A(C) manifest — the projection of ``regret::Action``
    (regret.rs:76) that Phase 1 needs: ``kind`` is ``NO_TRADE`` | ``GEOMETRY_VARIANT``,
    ``provenance`` is ``ACTUAL`` | ``DECLARED_VARIANT``."""

    action_id: str
    kind: str
    provenance: str


class ManifestLike(Protocol):
    """Anything exposing the manifest's legal actions (``regret::Manifest``)."""

    actions: Sequence[LegalAction]


@dataclass(frozen=True)
class OpportunityCell:
    """One legal-opportunity cell with its cube-reduced utility (phase1.rs:174)."""

    action_id: str
    kind: str
    provenance: str
    utility: float | None


def opportunity_set(
    manifest: ManifestLike, utility_by_action: Mapping[str, float]
) -> list[OpportunityCell]:
    """The per-Candidate legal-opportunity set with per-cell utilities (phase1.rs:188).

    The manifest's legal actions in manifest order (NO_TRADE, ACTUAL, then the declared
    target_r x expiry_bars grid, de-duplicated by action id), each paired with its cube-reduced
    net utility (``None`` when the cell is absent from the reduced table). A thin projection of
    ``regret.rs`` — the manifest IS the oracle's A(C) (FCR FT003), so the set is never
    re-derived here.
    """
    return [
        OpportunityCell(
            action_id=a.action_id,
            kind=a.kind,
            provenance=a.provenance,
            utility=utility_by_action.get(a.action_id),
        )
        for a in manifest.actions
    ]


# ---------------------------------------------------------------------------
# Frozen constants (FCR-V8RR-007 / -009; verbatim mirror of the oracle)
# ---------------------------------------------------------------------------

EXPERTS: tuple[str, ...] = (
    "trend_pullback",
    "failed_breakout",
    "liquidity_sweep_reclaim",
)  # FT003
SYMBOLS: tuple[str, ...] = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "DOGEUSDT",
)  # FT003
DIRECTIONS: tuple[str, ...] = ("LONG", "SHORT")  # FT003
ESTIMANDS: tuple[str, ...] = ("mean_legal_hindsight_gap", "mean_actual_vs_no_trade")  # FT001

MIN_N_COMPUTED: int = 30  # FT003 minimum support
MIN_EFFECTIVE_EPISODES: float = 8.0  # FT003 minimum support
N_RESAMPLES: int = 2000  # FT004
CI: float = 0.90  # FT004
ALPHA_FAMILY: float = 0.05  # FT005 (pre-correction)
MIN_NET_R: float = 0.05  # FT007 materiality floor
MIN_TRADES_MATERIALITY: int = 30  # FT007 — same bar as MIN_N_COMPUTED

#: The smallest series an interval may be formed over. A percentile interval over three points
#: is not an estimate, so the callers refuse it (fail-closed; see ``SEMANTIC_AMBIGUITIES``).
MIN_INTERVAL_POINTS: int = 4

DISCOVERY_VERDICTS: tuple[str, ...] = (
    "CANDIDATE_SYSTEMATIC",
    "INSUFFICIENT_SUPPORT",
    "EXCLUDED_EMPTY",
    "NOT_MATERIAL",
    "NOT_SIGNIFICANT",
)
CONFIRMATION_VERDICTS: tuple[str, ...] = ("SYSTEMATIC_FINDING", "FAILED_CONFIRMATION")

#: The lag-1 autocorrelation threshold above which ``select_block_size`` doubles the base block.
BLOCK_AUTOCORR_THRESHOLD: float = 0.10

#: Phase-3 frozen feature set, gate directions and quantiles (FCR-V8RR-009 FT002).
FEATURES: tuple[str, ...] = ("rsi14", "bb_pct_b", "adx14")
GATE_DIRECTIONS: tuple[str, ...] = ("NO_TRADE_BELOW", "NO_TRADE_ABOVE")
QUANTILES: tuple[float, ...] = (0.2, 0.4, 0.6, 0.8)

#: The two recoverability classes (phase3.rs:34).
RECOVERABLE_WITHIN_CLASS: str = "RECOVERABLE_WITHIN_CLASS"
NOT_RECOVERABLE_WITHIN_CLASS: str = "NOT_RECOVERABLE_WITHIN_CLASS"

#: Named reasons carried beside a ``NOT_RECOVERABLE_WITHIN_CLASS`` verdict so a negative result
#: is auditable instead of merely silent.
REASON_RECOVERABLE = "RECOVERABLE"
REASON_NO_CONFIRMATION_ROWS = "NO_CONFIRMATION_ROWS"
REASON_ZERO_EFFECTIVE_EPISODES = "ZERO_EFFECTIVE_EPISODES"
REASON_INSUFFICIENT_CONFIRMATION_SUPPORT = "INSUFFICIENT_CONFIRMATION_SUPPORT"
REASON_NO_NONZERO_DELTAS = "NO_NONZERO_DELTAS"
REASON_BELOW_MATERIALITY_FLOOR = "BELOW_MATERIALITY_FLOOR"
REASON_INTERVAL_INCLUDES_NULL = "INTERVAL_INCLUDES_NULL"


# ---------------------------------------------------------------------------
# statistics.py ports — zero new estimator code (D-072)
# ---------------------------------------------------------------------------


def _fsum(values: Iterable[float]) -> float:
    """``crate::state::fsum`` / CPython ``math.fsum`` (the ``_PyFloat_Fsum`` partials
    algorithm). Every reduction that mirrors a Rust ``fsum`` goes through here."""
    return math.fsum(values)


def select_block_size(
    episode_net_r: Sequence[float], *, threshold: float = BLOCK_AUTOCORR_THRESHOLD
) -> int:
    """``select_block_size`` (phase2.rs:147 / statistics.py D-052).

    Two tiers selected by the lag-1 autocorrelation of episode net_R: base ``round(n**(1/3))``
    (banker's rounding — CPython ``round``), doubled when ``|lag1| > threshold``, capped at
    ``n // 2`` (the cap that keeps the circular sampler non-degenerate). ``n < 4`` returns 1;
    a zero-variance series returns ``min(base, n // 2)``.
    """
    n = len(episode_net_r)
    if n < 4:
        return 1
    mean = _fsum(episode_net_r) / n
    c0 = _fsum((x - mean) ** 2 for x in episode_net_r)
    base = max(1, int(round(n ** (1.0 / 3.0))))
    if c0 == 0.0:
        return int(max(1, min(base, n // 2)))
    c1 = _fsum(
        (episode_net_r[i] - mean) * (episode_net_r[i + 1] - mean) for i in range(n - 1)
    )
    lag1 = c1 / c0
    block = 2 * base if abs(lag1) > threshold else base
    return int(max(1, min(block, n // 2)))


def _block_bootstrap_indices(n: int, block_size: int, rng: random.Random) -> list[int]:
    """One circular fixed-block bootstrap draw of length n over [0, n) (phase2.rs:178 /
    statistics.py ``_block_bootstrap_indices``): repeatedly picks a uniform start and appends a
    contiguous run of ``block_size`` indices (wrapping past the end) until length n, then
    truncates.

    Fail-closed on the degenerate ``block_size >= n`` draw (D-052) exactly as the oracle raises:
    with one block covering the series every resample is a rotation holding each element exactly
    once, the bootstrap collapses to a point mass and a zero-width interval would reject H0 for
    any positive mean.
    """
    if n <= 0:
        return []
    if block_size <= 0:
        raise ValueError("block_size must be positive")
    if n >= 2 and block_size >= n:
        raise ValueError(
            f"block_size {block_size} >= n {n}: degenerate block bootstrap "
            "(every resample is a rotation of the whole series)"
        )
    out: list[int] = []
    while len(out) < n:
        start = rng.randrange(n)
        out.extend((start + j) % n for j in range(block_size))
    return out[:n]


def block_bootstrap_means(
    net_rs: Sequence[float], block_size: int, n_resamples: int, seed: int
) -> list[float]:
    """The section-9 circular fixed-block bootstrap resample means (phase2.rs:199).

    One rng from ``seed`` drives every resample; each mean is ``fsum(selected) / n``. Bootstrap
    theorem: resample size = original n (Aronson Ch5 p234-238).
    """
    n = len(net_rs)
    if n == 0:
        return []
    if n_resamples <= 0:
        raise ValueError("n_resamples must be positive")
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(n_resamples):
        idx = _block_bootstrap_indices(n, block_size, rng)
        means.append(_fsum(net_rs[i] for i in idx) / n)
    return means


def bootstrap_ci(
    net_r_series: Sequence[float],
    block_size: int,
    n_resamples: int,
    seed: int,
    ci: float,
) -> tuple[float, float]:
    """``bootstrap_ci`` — percentile CI on mean episode net_R (phase2.rs:240 / phase3.rs:171).

    ``tail = int(n_resamples * (1 - ci) / 2)`` computed in f64 and truncated, so with
    ``N_RESAMPLES=2000, CI=0.90`` it is 99, not 100. An empty series returns ``(0.0, 0.0)``;
    callers must treat that sentinel as "no interval", never as a zero-width result
    (fail-closed).
    """
    if not 0.0 < ci < 1.0:
        raise ValueError(f"ci must be in (0, 1) (got {ci!r})")
    means = block_bootstrap_means(net_r_series, block_size, n_resamples, seed)
    if not means:
        return (0.0, 0.0)
    means.sort()
    tail = int(n_resamples * (1.0 - ci) / 2.0)
    return (means[tail], means[-tail - 1])


def effective_independent_episodes(n_episodes: int, max_hold_bars: int) -> float:
    """``effective_independent_episodes`` — ``n / max_hold_bars`` (phase2.rs:259), the
    conservative upper bound on independent observations under overlapping holds. Zero episodes
    is 0.0; a non-positive ``max_hold_bars`` is a hard error."""
    if n_episodes == 0:
        return 0.0
    if max_hold_bars <= 0:
        raise ValueError(f"max_hold_bars must be positive (got {max_hold_bars!r})")
    return n_episodes / max_hold_bars


def practical_significance(
    net_r: Sequence[float], min_net_r: float, min_trades: int
) -> tuple[bool, str]:
    """``practical_significance`` (phase2.rs:269 / statistics.py METH-5) — economic-magnitude
    gate ``mean >= min_net_r`` AND coverage gate ``n >= min_trades``, with the auditable note
    string stating both observed values."""
    n = len(net_r)
    mean = (_fsum(net_r) / n) if n > 0 else 0.0
    meets = n >= min_trades and mean >= min_net_r
    note = (
        f'mean net_R {mean:.4f} vs economic floor {min_net_r} '
        f'({"meets" if mean >= min_net_r else "below"}); '
        f'episodes {n} vs minimum coverage {min_trades} '
        f'({"meets" if n >= min_trades else "below"})'
    )
    return meets, note


def expected_false_positives(n_rules: int, alpha: float) -> float:
    """``expected_false_positives`` (phase2.rs:283) — N rules at alpha -> N*alpha."""
    return n_rules * alpha


def effective_search_size(variants_evaluated: int, search_universe_size: int) -> int:
    """``effective_search_size`` (phase2.rs:289) — ``max(variants_evaluated,
    search_universe_size)``, so the reported family size can never understate the declared
    search (D-046). A declared search smaller than what it retained is a hard error."""
    if search_universe_size < variants_evaluated:
        raise ValueError(
            "the declared search cannot be smaller than what it retained (D-046): "
            f"search_universe_size={search_universe_size} < "
            f"variants_evaluated={variants_evaluated}"
        )
    return max(variants_evaluated, search_universe_size)


def seed_for(suffix_key: str) -> int:
    """``seed_for(key)`` — ``int(sha1_hex(key)[:8], 16)`` (phase2.rs:311 / phase3.rs:193).

    ``sha1_hex`` hashes the JSON-encoded key, so the surrounding double quotes enter the digest
    (``json.dumps(key)``). Deterministic per key (FT004): never wall-clock, never run-order
    dependent.
    """
    canonical = json.dumps(suffix_key)
    digest = hashlib.sha1(canonical.encode()).hexdigest()
    return int(digest[:8], 16)


def format_g6(x: float) -> str:
    """``format_g6`` — Python/printf ``%.6g`` (phase3.rs:205).

    The policy id embeds ``format!(\"{:.6g}\")``; CPython's ``%.6g`` is the oracle the Rust
    helper re-implements, so this port calls it natively: 6 significant digits, ``%e`` style
    when the rounded exponent is < -4 or >= 6, else ``%f`` style, trailing zeros stripped.
    """
    return "%.6g" % x


# ---------------------------------------------------------------------------
# Phase 2 — systematicity discovery (phase2.rs)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SliceRow:
    """One Phase-1 dataset row projected onto the fields Phase 2 reads (phase2.rs:69).

    The oracle consumes ``tools.regret_phase1``'s frozen join output; the field set is exactly
    what ``regret_phase2.py`` indexes. ``gap_status`` is ``"COMPUTED"`` | ``"REJECTED"`` | ...
    """

    expert_id: str
    symbol: str
    direction: str
    gap_status: str
    legal_hindsight_gap: float | None
    actual_utility: float | None
    horizon_bars: int | None


@dataclass
class SliceResult:
    """Mirror of the oracle's frozen ``SliceResult`` dataclass (phase2.rs:83): every field, in
    order, with the same None-vs-Some meaning (discovery scoring leaves the confirmation fields
    ``None``)."""

    slice_key: str
    expert_id: str
    symbol: str
    direction: str
    estimand: str
    n_total_in_slice: int
    n_computed: int
    effective_independent_episodes: float
    mean: float | None
    ci_lower: float | None
    ci_upper: float | None
    block_size: int | None
    alpha_slate: float
    practically_significant: bool | None
    materiality_note: str
    discovery_verdict: str
    confirmation_verdict: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "slice_key": self.slice_key,
            "expert_id": self.expert_id,
            "symbol": self.symbol,
            "direction": self.direction,
            "estimand": self.estimand,
            "n_total_in_slice": self.n_total_in_slice,
            "n_computed": self.n_computed,
            "effective_independent_episodes": self.effective_independent_episodes,
            "mean": self.mean,
            "ci_lower": self.ci_lower,
            "ci_upper": self.ci_upper,
            "block_size": self.block_size,
            "alpha_slate": self.alpha_slate,
            "practically_significant": self.practically_significant,
            "materiality_note": self.materiality_note,
            "discovery_verdict": self.discovery_verdict,
            "confirmation_verdict": self.confirmation_verdict,
        }


@dataclass
class ConfirmationResult:
    """The confirmation-half answer for one candidate slice (phase2.rs:105, FT006)."""

    slice_key: str
    confirmation_verdict: str
    confirmation_mean: float | None
    confirmation_ci_lower: float | None
    confirmation_ci_upper: float | None
    confirmation_n_computed: int
    confirmation_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "slice_key": self.slice_key,
            "confirmation_verdict": self.confirmation_verdict,
            "confirmation_mean": self.confirmation_mean,
            "confirmation_ci_lower": self.confirmation_ci_lower,
            "confirmation_ci_upper": self.confirmation_ci_upper,
            "confirmation_n_computed": self.confirmation_n_computed,
            "confirmation_reason": self.confirmation_reason,
        }


@dataclass
class DiscoverySummary:
    """The discovery summary the oracle writes to ``discovery_summary.json`` (phase2.rs:116)."""

    n_slices_declared: int
    discovery_verdict_distribution: list[tuple[str, int]]
    n_candidate_systematic: int
    expected_false_positives_at_family_alpha: float
    alpha_slate_bonferroni: float
    candidate_systematic_slices: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_slices_declared": self.n_slices_declared,
            "discovery_verdict_distribution": [
                [v, c] for v, c in self.discovery_verdict_distribution
            ],
            "n_candidate_systematic": self.n_candidate_systematic,
            "expected_false_positives_at_family_alpha": (
                self.expected_false_positives_at_family_alpha
            ),
            "alpha_slate_bonferroni": self.alpha_slate_bonferroni,
            "candidate_systematic_slices": list(self.candidate_systematic_slices),
        }


def estimand_series(
    rows: Sequence[SliceRow], estimand: str
) -> tuple[list[float], list[SliceRow]]:
    """``_estimand_series`` (phase2.rs:318) — restrict to ``gap_status == COMPUTED`` before
    touching any estimator (AS002); a non-COMPUTED row is never coerced into a float. An unknown
    estimand is a hard error, never a silent empty series."""
    computed = [r for r in rows if r.gap_status == "COMPUTED"]
    if estimand == "mean_legal_hindsight_gap":
        series = [r.legal_hindsight_gap for r in computed if r.legal_hindsight_gap is not None]
    elif estimand == "mean_actual_vs_no_trade":
        series = [r.actual_utility for r in computed if r.actual_utility is not None]
    else:
        raise ValueError(f"unknown estimand {estimand!r}")
    return series, computed


def max_hold_bars(rows: Sequence[SliceRow]) -> int:
    """``_max_hold_bars`` (phase2.rs:337) — max truthy ``horizon_bars`` (a 0 is falsy and
    excluded), falling back to 1 when no hold is recorded."""
    holds = [r.horizon_bars for r in rows if r.horizon_bars is not None and r.horizon_bars > 0]
    return max(holds) if holds else 1


def declare_slices() -> list[tuple[str, str, str, str, str]]:
    """FT003 — the full 72-slice discovery family, declared BEFORE any data is touched
    (phase2.rs:353).

    A verbatim mirror of ``regret_phase2.py:declare_slices``: 3 Experts x 6 symbols x 2
    directions x 2 estimands, in that nesting order. Each tuple is
    ``(slice_key, expert_id, symbol, direction, estimand)``.
    """
    out: list[tuple[str, str, str, str, str]] = []
    for expert in EXPERTS:
        for symbol in SYMBOLS:
            for direction in DIRECTIONS:
                for estimand in ESTIMANDS:
                    key = f"{expert}|{symbol}|{direction}|{estimand}"
                    out.append((key, expert, symbol, direction, estimand))
    return out


def score_slice(
    key: str,
    expert_id: str,
    symbol: str,
    direction: str,
    estimand: str,
    dataset_rows: Sequence[SliceRow],
) -> SliceResult:
    """One slice's systematicity gate (phase2.rs:383 / ``regret_phase2.py:score_slice``).

    Verdict chain per RECOVERABLE_REGRET_PROTOCOL §4 Phase 2: ``EXCLUDED_EMPTY`` (no rows) ->
    ``INSUFFICIENT_SUPPORT`` (``n_computed < MIN_N_COMPUTED`` or ``eff_n < MIN_EFFECTIVE_
    EPISODES``) -> ``NOT_MATERIAL`` (below the 0.05R economic floor or 30-trade coverage) ->
    ``NOT_SIGNIFICANT`` (``ci_lower <= 0`` — the CI must exclude the null in the claimed
    direction) -> ``CANDIDATE_SYSTEMATIC``. The candidate gate is the systematicity claim Phase 2
    makes; it is not yet a finding — only a replicated ``SYSTEMATIC_FINDING`` on the confirmation
    half is.
    """
    family_size = effective_search_size(len(declare_slices()), len(declare_slices()))
    alpha_slate = ALPHA_FAMILY / family_size

    slice_rows = [
        r
        for r in dataset_rows
        if r.expert_id == expert_id and r.symbol == symbol and r.direction == direction
    ]

    if not slice_rows:
        return SliceResult(
            slice_key=key,
            expert_id=expert_id,
            symbol=symbol,
            direction=direction,
            estimand=estimand,
            n_total_in_slice=0,
            n_computed=0,
            effective_independent_episodes=0.0,
            mean=None,
            ci_lower=None,
            ci_upper=None,
            block_size=None,
            alpha_slate=alpha_slate,
            practically_significant=None,
            materiality_note="no candidates in this slice",
            discovery_verdict="EXCLUDED_EMPTY",
            confirmation_verdict=None,
        )

    series, computed_rows = estimand_series(slice_rows, estimand)
    n_computed = len(series)
    eff_n = (
        effective_independent_episodes(n_computed, max_hold_bars(computed_rows))
        if n_computed > 0
        else 0.0
    )

    if n_computed < MIN_N_COMPUTED or eff_n < MIN_EFFECTIVE_EPISODES:
        return SliceResult(
            slice_key=key,
            expert_id=expert_id,
            symbol=symbol,
            direction=direction,
            estimand=estimand,
            n_total_in_slice=len(slice_rows),
            n_computed=n_computed,
            effective_independent_episodes=eff_n,
            mean=None,
            ci_lower=None,
            ci_upper=None,
            block_size=None,
            alpha_slate=alpha_slate,
            practically_significant=None,
            materiality_note=(
                f"n_computed={n_computed} (need >={MIN_N_COMPUTED}) or "
                f"effective_independent_episodes={eff_n:.2f} "
                f"(need >={MIN_EFFECTIVE_EPISODES})"
            ),
            discovery_verdict="INSUFFICIENT_SUPPORT",
            confirmation_verdict=None,
        )

    block = select_block_size(series)
    seed = seed_for(key)
    ci_lower, ci_upper = bootstrap_ci(series, block, N_RESAMPLES, seed, CI)
    meets, note = practical_significance(series, MIN_NET_R, MIN_TRADES_MATERIALITY)
    mean = _fsum(series) / len(series)

    if not meets:
        verdict = "NOT_MATERIAL"
    elif ci_lower <= 0.0:
        # FT005/FT007: the CI must exclude the null in the direction claimed.
        verdict = "NOT_SIGNIFICANT"
    else:
        verdict = "CANDIDATE_SYSTEMATIC"

    return SliceResult(
        slice_key=key,
        expert_id=expert_id,
        symbol=symbol,
        direction=direction,
        estimand=estimand,
        n_total_in_slice=len(slice_rows),
        n_computed=n_computed,
        effective_independent_episodes=eff_n,
        mean=mean,
        ci_lower=ci_lower,
        ci_upper=ci_upper,
        block_size=block,
        alpha_slate=alpha_slate,
        practically_significant=meets,
        materiality_note=note,
        discovery_verdict=verdict,
        confirmation_verdict=None,
    )


def discovery_summary(results: Sequence[SliceResult]) -> DiscoverySummary:
    """Discovery summary (phase2.rs:491 / ``run_discovery``'s ``discovery_summary.json``)."""
    n_candidate = sum(
        1 for r in results if r.discovery_verdict == "CANDIDATE_SYSTEMATIC"
    )
    distribution = [
        (v, sum(1 for r in results if r.discovery_verdict == v)) for v in DISCOVERY_VERDICTS
    ]
    return DiscoverySummary(
        n_slices_declared=len(declare_slices()),
        discovery_verdict_distribution=distribution,
        n_candidate_systematic=n_candidate,
        expected_false_positives_at_family_alpha=expected_false_positives(
            len(declare_slices()), ALPHA_FAMILY
        ),
        alpha_slate_bonferroni=ALPHA_FAMILY / len(declare_slices()),
        candidate_systematic_slices=[
            r.slice_key for r in results if r.discovery_verdict == "CANDIDATE_SYSTEMATIC"
        ],
    )


class ConfirmationLedger:
    """FT006 ledger (phase2.rs:525): the confirmation half is queried EXACTLY ONCE per declared
    slice. A failure is recorded permanently and never re-tested (FCR-V8RR-007 AP002); a second
    query of the same slice is a hard error, not a warning
    (RECOVERABLE_REGRET_PROTOCOL §8.4)."""

    def __init__(self) -> None:
        self._queried: set[str] = set()

    def query(
        self, candidate: SliceResult, confirmation_rows: Sequence[SliceRow]
    ) -> ConfirmationResult:
        """Query one candidate slice against the untouched confirmation half.

        Raises :class:`ValueError` if this slice's confirmation half was already queried.
        """
        if candidate.slice_key in self._queried:
            raise ValueError(
                f"confirmation half for slice '{candidate.slice_key}' already queried: a "
                "slice's confirmation half is queried exactly once (FCR-V8RR-007 AP002)"
            )
        self._queried.add(candidate.slice_key)

        slice_rows = [
            r
            for r in confirmation_rows
            if r.expert_id == candidate.expert_id
            and r.symbol == candidate.symbol
            and r.direction == candidate.direction
        ]
        if slice_rows:
            series, computed_rows = estimand_series(slice_rows, candidate.estimand)
        else:
            series, computed_rows = [], []
        n_computed = len(series)
        eff_n = (
            effective_independent_episodes(n_computed, max_hold_bars(computed_rows))
            if n_computed > 0
            else 0.0
        )

        if n_computed < MIN_N_COMPUTED or eff_n < MIN_EFFECTIVE_EPISODES:
            reason = (
                REASON_NO_CONFIRMATION_ROWS
                if not slice_rows
                else "INSUFFICIENT_CONFIRMATION_SUPPORT"
            )
            return ConfirmationResult(
                slice_key=candidate.slice_key,
                confirmation_verdict="FAILED_CONFIRMATION",
                confirmation_mean=None,
                confirmation_ci_lower=None,
                confirmation_ci_upper=None,
                confirmation_n_computed=n_computed,
                confirmation_reason=reason,
            )

        block = select_block_size(series)
        seed = seed_for(f"{candidate.slice_key}|confirmation")
        lo, hi = bootstrap_ci(series, block, N_RESAMPLES, seed, CI)
        mean = _fsum(series) / len(series)
        meets, _ = practical_significance(series, MIN_NET_R, MIN_TRADES_MATERIALITY)
        verdict = "SYSTEMATIC_FINDING" if (meets and lo > 0.0) else "FAILED_CONFIRMATION"
        reason = (
            REASON_RECOVERABLE
            if verdict == "SYSTEMATIC_FINDING"
            else (REASON_BELOW_MATERIALITY_FLOOR if not meets else REASON_INTERVAL_INCLUDES_NULL)
        )
        return ConfirmationResult(
            slice_key=candidate.slice_key,
            confirmation_verdict=verdict,
            confirmation_mean=mean,
            confirmation_ci_lower=lo,
            confirmation_ci_upper=hi,
            confirmation_n_computed=n_computed,
            confirmation_reason=reason,
        )


# ---------------------------------------------------------------------------
# Phase 3 — recoverability evaluation (phase3.rs)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicySpec:
    """One declared decision-time policy (phase3.rs:45, mirrors ``PolicySpec`` verbatim).

    ``kind`` is ``ALWAYS_TRADE`` | ``THRESHOLD_GATE``.
    """

    policy_id: str
    kind: str
    feature: str | None = None
    direction: str | None = None
    threshold: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "kind": self.kind,
            "feature": self.feature,
            "direction": self.direction,
            "threshold": self.threshold,
        }


@dataclass(frozen=True)
class _Row:
    """One cube row as consumed by Phase 3 (phase3.rs:458) — the reduced-row fields Phase 2
    certifies; identity strings are excluded from value parity."""

    expert_id: str
    symbol: str
    direction: str
    gap_status: str
    actual_utility: float
    candidate_id: str


def _row_from_mapping(v: Mapping[str, Any]) -> _Row | None:
    """``Row::from_value`` (phase3.rs:468): a row missing any required field, or with a
    non-string/non-float field, is dropped rather than coerced."""
    expert_id = v.get("expert_id")
    symbol = v.get("symbol")
    direction = v.get("direction")
    gap_status = v.get("gap_status")
    actual_utility = v.get("actual_utility")
    candidate_id = v.get("candidate_id")
    if not (
        isinstance(expert_id, str)
        and isinstance(symbol, str)
        and isinstance(direction, str)
        and isinstance(gap_status, str)
        and isinstance(candidate_id, str)
        and isinstance(actual_utility, (int, float))
        and not isinstance(actual_utility, bool)
    ):
        return None
    return _Row(
        expert_id=expert_id,
        symbol=symbol,
        direction=direction,
        gap_status=gap_status,
        actual_utility=float(actual_utility),
        candidate_id=candidate_id,
    )


def _rows_for(
    rows: Sequence[Mapping[str, Any]], expert_id: str, symbol: str, direction: str
) -> list[_Row]:
    """``rows_for`` (phase3.rs:480): the COMPUTED rows of one expert/symbol/direction slice."""
    out: list[_Row] = []
    for v in rows:
        r = _row_from_mapping(v)
        if (
            r is not None
            and r.expert_id == expert_id
            and r.symbol == symbol
            and r.direction == direction
            and r.gap_status == "COMPUTED"
        ):
            out.append(r)
    return out


def feature_value(state_rec: Mapping[str, Any], symbol: str, feature: str) -> float | None:
    """``_feature_value`` (phase3.rs:273): ``state.features["{symbol}.{feature}"]["value"]`` as a
    float; any non-numeric value (null, absent, string) is ``None`` — never a zero."""
    feats = state_rec.get("features")
    if not isinstance(feats, Mapping):
        return None
    fv = feats.get(f"{symbol}.{feature}")
    if not isinstance(fv, Mapping):
        return None
    value = fv.get("value")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a store's JSONL file into records, skipping blank lines. A missing file is a hard
    error, never an empty store."""
    if not path.is_file():
        raise FileNotFoundError(f"cannot read {path}")
    records: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if not isinstance(rec, dict):
            raise ValueError(f"{path}: line is not a JSON object")
        records.append(rec)
    return records


def _birth_features_from_records(
    candidates: Sequence[Mapping[str, Any]],
    states: Sequence[Mapping[str, Any]],
    symbol: str,
) -> dict[str, dict[str, float | None]]:
    """The shared body of ``load_birth_features`` / ``load_birth_features_from_memory``
    (phase3.rs:294/335): map each candidate's OWN ``birth_state_id`` (its DETECTED transition's
    state_id) to the three declared feature values."""
    birth_state: dict[str, str] = {}
    for rec in candidates:
        if rec.get("to_state") != "DETECTED":
            continue
        cid = rec.get("candidate_id")
        sid = rec.get("state_id")
        if isinstance(cid, str) and isinstance(sid, str):
            birth_state[cid] = sid

    states_map: dict[str, Mapping[str, Any]] = {}
    for rec in states:
        sid = rec.get("state_id")
        if isinstance(sid, str):
            states_map[sid] = rec

    out: dict[str, dict[str, float | None]] = {}
    for cid, sid in birth_state.items():
        st = states_map.get(sid)
        if st is None:
            continue
        out[cid] = {f: feature_value(st, symbol, f) for f in FEATURES}
    return out


def load_birth_features(
    store_dir: Path | str, symbol: str
) -> dict[str, dict[str, float | None]]:
    """``_load_birth_features`` (phase3.rs:294): ``candidate_id -> {feature: float | None}``,
    read from each candidate's OWN ``birth_state_id`` (``candidates.jsonl`` DETECTED edges +
    ``states.jsonl``)."""
    directory = Path(store_dir)
    candidates = _read_jsonl(directory / "candidates.jsonl")
    states = _read_jsonl(directory / "states.jsonl")
    return _birth_features_from_records(candidates, states, symbol)


def load_birth_features_from_memory(
    candidates: Sequence[Mapping[str, Any]],
    states: Sequence[Mapping[str, Any]],
    symbol: str,
) -> dict[str, dict[str, float | None]]:
    """Zero-disk in-memory birth-feature extraction (phase3.rs:335)."""
    return _birth_features_from_records(candidates, states, symbol)


def declare_policies(
    discovery_series_by_feature: Mapping[str, Sequence[float | None]],
) -> list[PolicySpec]:
    """``declare_policies`` (phase3.rs:377): ALWAYS_TRADE + 3*2*4 = 25 policies.

    Thresholds are the DISCOVERY-half quantile of each feature — computed once per slice, never
    per-candidate. A feature with no observed values contributes no gates (fail-closed: no
    fabricated threshold from an empty series).
    """
    policies: list[PolicySpec] = [
        PolicySpec(policy_id="ALWAYS_TRADE", kind="ALWAYS_TRADE")
    ]
    for feature in FEATURES:
        values: list[float] = [
            x for x in discovery_series_by_feature.get(feature, ()) if x is not None
        ]
        values.sort()
        if not values:
            continue
        for q in QUANTILES:
            idx = min(int(len(values) * q), len(values) - 1)
            threshold = values[idx]
            for direction in GATE_DIRECTIONS:
                policies.append(
                    PolicySpec(
                        policy_id=(
                            f"THRESHOLD_GATE|{feature}|{direction}|"
                            f"{format_g6(threshold)}|q{q}"
                        ),
                        kind="THRESHOLD_GATE",
                        feature=feature,
                        direction=direction,
                        threshold=threshold,
                    )
                )
    return policies


def apply_policy(
    policy: PolicySpec,
    feature_values: Mapping[str, float | None],
    actual_utility: float,
) -> float:
    """``apply_policy`` (phase3.rs:422): THRESHOLD_GATE selects NO_TRADE (utility 0.0) when it
    fires; a feature unavailable at this clock means the gate cannot fire and the Candidate's
    already-replayed utility is used."""
    if policy.kind == "ALWAYS_TRADE":
        return actual_utility
    if policy.feature is None:
        return actual_utility
    v = feature_values.get(policy.feature)
    if v is None:
        return actual_utility
    threshold = policy.threshold if policy.threshold is not None else 0.0
    if policy.direction == "NO_TRADE_BELOW":
        fires = v < threshold
    else:
        fires = v > threshold
    return 0.0 if fires else actual_utility


@dataclass
class RecoverabilityResult:
    """The per-slice recoverability result (phase3.rs:637) plus the named
    ``recoverability_reason`` the fail-closed rule requires beside a negative verdict."""

    slice_key: str
    expert_id: str
    symbol: str
    direction: str
    n_discovery: int
    n_confirmation: int
    selected_policy: PolicySpec | None
    discovery_selection_mean_utility: float | None
    confirmation_v_a: float | None
    confirmation_v_r: float | None
    confirmation_g_r: float | None
    confirmation_g_r_ci_lower: float | None
    confirmation_g_r_ci_upper: float | None
    recoverability_verdict: str
    recoverability_reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "slice_key": self.slice_key,
            "expert_id": self.expert_id,
            "symbol": self.symbol,
            "direction": self.direction,
            "n_discovery": self.n_discovery,
            "n_confirmation": self.n_confirmation,
            "selected_policy": (
                self.selected_policy.as_dict() if self.selected_policy is not None else None
            ),
            "discovery_selection_mean_utility": self.discovery_selection_mean_utility,
            "confirmation_v_a": self.confirmation_v_a,
            "confirmation_v_r": self.confirmation_v_r,
            "confirmation_g_r": self.confirmation_g_r,
            "confirmation_g_r_ci_lower": self.confirmation_g_r_ci_lower,
            "confirmation_g_r_ci_upper": self.confirmation_g_r_ci_upper,
            "recoverability_verdict": self.recoverability_verdict,
            "recoverability_reason": self.recoverability_reason,
        }


def _policy_row(
    slice_key: str, policy: PolicySpec, n: int, mean_utility: float | None
) -> dict[str, Any]:
    """``policy_row`` (phase3.rs:502): one discovery-selection attempt row."""
    return {
        "slice_key": slice_key,
        "stage": "discovery_selection",
        "policy_id": policy.policy_id,
        "kind": policy.kind,
        "feature": policy.feature,
        "direction": policy.direction,
        "threshold": policy.threshold,
        "n": n,
        "mean_utility": mean_utility,
    }


def evaluate_slice_recoverability(
    slice_key: str,
    expert_id: str,
    symbol: str,
    direction: str,
    store_dir: Path | str,
    discovery_rows: Sequence[Mapping[str, Any]],
    confirmation_rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], RecoverabilityResult]:
    """``evaluate_slice_recoverability`` (phase3.rs:521): load the slice's birth features from
    its store, then evaluate. Returns (discovery attempt rows, result)."""
    birth = load_birth_features(store_dir, symbol)
    return evaluate_slice_recoverability_with_birth(
        slice_key, expert_id, symbol, direction, birth, discovery_rows, confirmation_rows
    )


def evaluate_slice_recoverability_with_birth(
    slice_key: str,
    expert_id: str,
    symbol: str,
    direction: str,
    birth: Mapping[str, Mapping[str, float | None]],
    discovery_rows: Sequence[Mapping[str, Any]],
    confirmation_rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], RecoverabilityResult]:
    """The in-memory evaluator (phase3.rs:543) — mirrors the oracle's dict field-for-field; the
    confirmation half is touched only for ``V_A``/``V_R``/``G_R`` and the bootstrap.

    The best policy is the ALWAYS-best discovery mean; break ties by first-declared order
    (strict ``>`` keeps the earlier policy, as in the Rust loop)."""
    disc = _rows_for(discovery_rows, expert_id, symbol, direction)
    conf = _rows_for(confirmation_rows, expert_id, symbol, direction)

    def feats_of(cid: str) -> Mapping[str, float | None]:
        return birth.get(cid, {})

    disc_features = [feats_of(r.candidate_id) for r in disc]
    disc_series: dict[str, list[float | None]] = {
        f: [m.get(f) for m in disc_features] for f in FEATURES
    }
    policies = declare_policies(disc_series)

    attempt_rows: list[dict[str, Any]] = []
    best_policy: PolicySpec | None = None
    best_mean: float | None = None
    for policy in policies:
        utils = [
            apply_policy(policy, m, r.actual_utility) for r, m in zip(disc, disc_features, strict=True)
        ]
        mean_u = (_fsum(utils) / len(utils)) if utils else None
        attempt_rows.append(_policy_row(slice_key, policy, len(utils), mean_u))
        if mean_u is not None and (best_mean is None or mean_u > best_mean):
            best_mean = mean_u
            best_policy = policy

    conf_features = [feats_of(r.candidate_id) for r in conf]
    deltas: list[float]
    if best_policy is not None:
        deltas = [
            apply_policy(best_policy, m, r.actual_utility) - r.actual_utility
            for r, m in zip(conf, conf_features, strict=True)
        ]
    else:
        deltas = []
    v_a = (_fsum(r.actual_utility for r in conf) / len(conf)) if conf else None
    g_r = (_fsum(deltas) / len(deltas)) if deltas else None
    v_r = (v_a + g_r) if (v_a is not None and g_r is not None) else None

    has_nonzero = any(d != 0.0 for d in deltas)
    support_ok = len(deltas) >= MIN_INTERVAL_POINTS
    if deltas and has_nonzero and support_ok:
        block = select_block_size(deltas)
        seed = seed_for(f"{slice_key}|phase3")
        ci_lower, ci_upper = bootstrap_ci(deltas, block, N_RESAMPLES, seed, CI)
    else:
        # Fail-closed: no interval over an empty, constant or sub-floor series.
        ci_lower, ci_upper = (None, None)

    if g_r is None:
        verdict = NOT_RECOVERABLE_WITHIN_CLASS
        reason = REASON_NO_CONFIRMATION_ROWS if not conf else REASON_ZERO_EFFECTIVE_EPISODES
    elif not deltas:
        verdict = NOT_RECOVERABLE_WITHIN_CLASS
        reason = REASON_ZERO_EFFECTIVE_EPISODES
    elif not has_nonzero:
        verdict = NOT_RECOVERABLE_WITHIN_CLASS
        reason = REASON_NO_NONZERO_DELTAS
    elif not support_ok:
        verdict = NOT_RECOVERABLE_WITHIN_CLASS
        reason = REASON_INSUFFICIENT_CONFIRMATION_SUPPORT
    elif g_r < MIN_NET_R:
        verdict = NOT_RECOVERABLE_WITHIN_CLASS
        reason = REASON_BELOW_MATERIALITY_FLOOR
    elif ci_lower is None or ci_lower <= 0.0:
        verdict = NOT_RECOVERABLE_WITHIN_CLASS
        reason = REASON_INTERVAL_INCLUDES_NULL
    else:
        verdict = RECOVERABLE_WITHIN_CLASS
        reason = REASON_RECOVERABLE

    result = RecoverabilityResult(
        slice_key=slice_key,
        expert_id=expert_id,
        symbol=symbol,
        direction=direction,
        n_discovery=len(disc),
        n_confirmation=len(conf),
        selected_policy=best_policy,
        discovery_selection_mean_utility=best_mean,
        confirmation_v_a=v_a,
        confirmation_v_r=v_r,
        confirmation_g_r=g_r,
        confirmation_g_r_ci_lower=ci_lower,
        confirmation_g_r_ci_upper=ci_upper,
        recoverability_verdict=verdict,
        recoverability_reason=reason,
    )
    return attempt_rows, result


def _append_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    """``append_jsonl`` (phase3.rs:656)."""
    with path.open("a") as fh:
        for r in rows:
            fh.write(json.dumps(r))
            fh.write("\n")


def run_phase3(
    confirmed_slice_keys: Sequence[str],
    discovery_rows: Sequence[Mapping[str, Any]],
    confirmation_rows: Sequence[Mapping[str, Any]],
    store_dirs: Mapping[str, str],
    out_dir: Path | str,
) -> dict[str, Any]:
    """``run_phase3`` (phase3.rs:675): compose per-slice recoverability over the confirmed slice
    keys, append the attempt ledger, and write ``phase3_summary.json``."""
    birth_cache: dict[str, dict[str, dict[str, float | None]]] = {}
    for key in confirmed_slice_keys:
        parts = key.split("|")
        if len(parts) < 4:
            raise ValueError(
                f"bad slice_key {key!r}: expected expert|symbol|direction|estimand"
            )
        symbol = parts[1]
        store_dir = store_dirs.get(symbol)
        if store_dir is None:
            raise ValueError(f"no store_dir for symbol {symbol}")
        if symbol not in birth_cache:
            birth_cache[symbol] = load_birth_features(store_dir, symbol)
    return run_phase3_in_memory(
        confirmed_slice_keys, discovery_rows, confirmation_rows, birth_cache, out_dir
    )


def run_phase3_in_memory(
    confirmed_slice_keys: Sequence[str],
    discovery_rows: Sequence[Mapping[str, Any]],
    confirmation_rows: Sequence[Mapping[str, Any]],
    birth_cache: Mapping[str, Mapping[str, Mapping[str, float | None]]],
    out_dir: Path | str,
) -> dict[str, Any]:
    """``run_phase3_in_memory`` (phase3.rs:710): zero-disk Phase-3 execution with a pre-computed
    birth-features cache. Writes ``recoverability_attempts.jsonl`` and ``phase3_summary.json``
    under ``out_dir`` and returns the summary."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    results: list[RecoverabilityResult] = []
    all_rows: list[dict[str, Any]] = []
    for key in confirmed_slice_keys:
        parts = key.split("|")
        if len(parts) < 4:
            raise ValueError(
                f"bad slice_key {key!r}: expected expert|symbol|direction|estimand"
            )
        expert_id, symbol, direction = parts[0], parts[1], parts[2]
        birth = birth_cache.get(symbol)
        if birth is None:
            raise ValueError(f"no birth features cache for symbol {symbol}")
        attempts, result = evaluate_slice_recoverability_with_birth(
            key, expert_id, symbol, direction, birth, discovery_rows, confirmation_rows
        )
        all_rows.extend(attempts)
        conf_row = result.as_dict()
        conf_row["stage"] = "confirmation_result"
        all_rows.append(conf_row)
        results.append(result)
    _append_jsonl(out / "recoverability_attempts.jsonl", all_rows)

    recoverable = [r for r in results if r.recoverability_verdict == RECOVERABLE_WITHIN_CLASS]
    summary: dict[str, Any] = {
        "n_slices_tested": len(results),
        "n_recoverable_within_class": len(recoverable),
        "n_not_recoverable_within_class": len(results) - len(recoverable),
        "recoverable_slices": [r.slice_key for r in recoverable],
        "results": [r.as_dict() for r in results],
    }
    (out / "phase3_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary
