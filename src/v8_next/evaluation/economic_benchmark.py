"""Economic benchmark fabric: real-data comparison with separate evidence verdicts.

Implements the professional-benchmark decision structure (research validity,
economic evidence, statistical evidence, portfolio contribution, execution
evidence, capital eligibility) as six independent verdicts. No single PASS
exists; a statistical confidence never mints economic success or capital
permission.

Conventions:
- All inputs are real venue data or engine replays of it. Synthetic series
  are allowed only inside control experiments explicitly labeled SYNTHETIC.
- Missing data/estimators stay missing (None / UNRUN / UNDERPOWERED) and
  never convert to success.
- Funding settlement is not fed to the engine in this scope, so funding cost
  is reported MISSING on every leg (strategy and benchmarks share the basis).
- Commissions come from the engine exactly once (verified estimate per fill
  reconciled against the engine-reported commission total). They are never
  subtracted a second time.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import re
import subprocess
from bisect import bisect_left
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Mapping, Sequence, get_args

import numpy as np
import polars as pl
from pydantic import BaseModel, ConfigDict, Field, field_validator

from v8_next.evaluation.statistics_plan import (
    StatisticsPlan,
    sample_sufficiency,
)

if TYPE_CHECKING:  # pragma: no cover - annotation-only, keeps the import graph acyclic
    from v8_next.domain.basket import BasketSpec
    from v8_next.domain.market import Candle
    from v8_next.evaluation.multitape import FundingRow

HOURS_PER_YEAR = 365.0 * 24.0
#: Per-bar -> annualized Sharpe scale. Every *published* Sharpe number (the
#: point estimate and its bootstrap CI alike) lives on this one scale (#388):
#: a CI stored per-bar next to an annualized point cannot contain its own
#: estimate, and the report then contradicts itself.
SHARPE_ANNUALIZATION = math.sqrt(HOURS_PER_YEAR)
TAKER_FEE_DEFAULT = 0.0005
CAPITAL_DEFAULT = 10000.0
VOL_LOOKBACK = 48
VOL_TARGET_ANNUAL = 0.20
MAX_LEVERAGE = 2.0
INTERVAL_BARS = 24  # one calendar day of 1h bars per return interval
BOOTSTRAP_BLOCK = 24
BOOTSTRAP_REPS = 999
BOOTSTRAP_SEED = 7
OOS_FIT_BARS = 350  # chronological split of the 500-bar window; fixed, never relabeled
#: #438 declared degeneracy condition, arm 1: the minimum number of INFORMATIVE
#: (moving) bars a per-bar variance estimate may rest on. Below it there is no
#: denominator worth publishing, however long the window is.
MIN_INFORMATIVE_BARS = 10
#: #438 declared degeneracy condition, arm 2: the ceiling on |CI| / |point
#: estimate| for a published Sharpe interval. Above it the percentiles are not
#: a range the point estimate lives in -- they are the surviving replicates of a
#: bootstrap whose variance estimate is dominated by near-zero-variance draws.
#: The threshold is bracketed by the frozen #388 receipt: the widest ratio a
#: supported variance produced there is 131x (incumbent, in-sample; every other
#: unmarked row with an interval is <= 33x), while the observed defect is 1.02e4x
#: (challenger, in-sample). 1000x separates the two with ~7x of slack on each
#: side and does not move any stored marker (see the pinned rows in
#: `tests/test_economic_benchmark.py`).
DEGENERATE_CI_RATIO = 1000.0
#: #438 declared degeneracy condition, arm 3: the ceiling on the interpretable
#: point estimate, in per-bar Sharpe. A per-bar Sharpe past 1.0 (annualized past
#: `SHARPE_ANNUALIZATION`) cannot be arithmetic on an estimated variance that is
#: really there.
DEGENERATE_PER_BAR_SHARPE = 1.0

VerdictState = Literal[
    "VALID",
    "INVALID",
    "POSITIVE_DESCRIPTIVE",
    "NEGATIVE",
    "INCONCLUSIVE",
    "SUPPORTED",
    "SUPPORTS_EDGE",
    "SUPPORTS_UNDERPERFORMANCE",
    "UNDERPOWERED",
    "UNSUPPORTED",
    "ESTIMATOR_UNAVAILABLE",
    "HELPFUL_DESCRIPTIVE",
    "NOT_HELPFUL",
    "SIM_ONLY",
    "EXECUTION_UNPROVEN",
    "UNRUN",
    "NOT_AUTHORIZED",
]

#: The same vocabulary as a runtime set, so a published verdict string can be
#: checked against the declared states instead of being trusted.
VERDICT_STATES: frozenset[str] = frozenset(get_args(VerdictState))

#: The one statistic state that means "the estimator never ran": a declared
#: dependency is not importable by this interpreter (#440).
ESTIMATOR_UNAVAILABLE: VerdictState = "ESTIMATOR_UNAVAILABLE"

#: What one entry of the `statistics` block may say (#440). A closed set, and
#: `ESTIMATOR_UNAVAILABLE` is deliberately not `UNDERPOWERED`/`UNSUPPORTED`:
#: those two mean the estimator ran and its sample could not support an estimate,
#: while `ESTIMATOR_UNAVAILABLE` means it never ran because a declared
#: dependency is not importable by this interpreter. Collapsing the second into
#: the first publishes an environment state as a measured statistical outcome.
STATISTIC_STATES: frozenset[str] = frozenset(
    {"COMPUTED", "UNDERPOWERED", "UNSUPPORTED", ESTIMATOR_UNAVAILABLE}
)

#: Optional statistics dependencies each estimator imports at call time (#440),
#: in the order the estimator imports them. `scipy`/`arch` are declared only in
#: `[project.optional-dependencies].research`, so a sync without that extra lands
#: exactly here; `polars` is a core dependency and is probed with the same
#: instrument so every estimator records its dependencies in one shape.
ESTIMATOR_MODULES: dict[str, tuple[str, ...]] = {
    "dsr": ("scipy",),
    "pbo": ("scipy", "polars"),
    "spa": ("scipy", "arch"),
}

#: How availability was measured, recorded in the receipt so the claim
#: "this interpreter could import it" is attributable to one instrument.
AVAILABILITY_PROBE = "importlib.util.find_spec"


def module_available(name: str) -> bool:
    """Can this interpreter import `name` at all? (#440)

    Asked with `importlib.util.find_spec` BEFORE an estimator is called, so the
    never-ran state is a measured environment fact. It is not inferred from a
    caught `ImportError`: a missing module never has to be *called* to be known
    missing, and an estimator that fails for its own reasons must not be
    mistaken for one that was never provisioned. A blocked import finder raises
    here, which is the same answer.
    """
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


class EstimatorAvailability(BaseModel):
    """Which statistics dependencies this interpreter can import (#440).

    Published on the receipt beside `run.code.estimator_versions` (which names
    what *is* installed) so a consumer can separate "not measured" from
    "measured, underpowered" from machine-readable fields rather than from a
    reason string.
    """

    model_config = ConfigDict(frozen=True)

    probe: str
    checked: dict[str, bool]
    unavailable: tuple[str, ...]
    required_by: dict[str, tuple[str, ...]]

    def for_estimator(self, estimator: str) -> tuple[str, ...]:
        """The modules `estimator` needs that this interpreter cannot import."""
        return tuple(
            name
            for name in self.required_by.get(estimator, ())
            if not self.checked.get(name, False)
        )


def estimator_availability() -> EstimatorAvailability:
    """Probe every statistics dependency the estimators import (#440).

    Deterministic in a given interpreter: no wall clock, no ordering beyond the
    declared `ESTIMATOR_MODULES`, nothing observed about a run's numbers.
    """
    required = sorted({name for names in ESTIMATOR_MODULES.values() for name in names})
    checked = {name: module_available(name) for name in required}
    return EstimatorAvailability(
        probe=AVAILABILITY_PROBE,
        checked=checked,
        unavailable=tuple(name for name in required if not checked[name]),
        required_by={key: tuple(names) for key, names in ESTIMATOR_MODULES.items()},
    )


def statistical_verdict_state(published: str) -> str:
    """Closed-vocabulary state token of a published statistical verdict (#440).

    The statistical verdict is the one published cell that may carry a payload
    (`ESTIMATOR_UNAVAILABLE: scipy,arch`), because the state alone cannot say
    *which* estimator was not measured. The token before the colon is always a
    declared :data:`VerdictState`, so a consumer switches on it without parsing
    prose.
    """
    return published.split(":", 1)[0].strip()


def estimator_unavailable_verdict(names: Sequence[str]) -> str:
    """`ESTIMATOR_UNAVAILABLE: scipy,arch` — the state, then what was missing."""
    return f"{ESTIMATOR_UNAVAILABLE}: {','.join(names)}"

# Preregistered inferential rules. Declared before any run and applied
# mechanically: computation success alone never mints inferential support.
# - SPA_EDGE_RULE: joint SPA consistent p < alpha AND the best variant's
#   excess vs baseline is positive  => SUPPORTS_EDGE (family-vs-baseline).
# - DSR_EDGE_RULE: DSR confidence >= level AND selected Sharpe > 0
#   => SUPPORTS_EDGE (selected-variant).
# - EXCESS_CI_RULE (#396): 95% block-bootstrap CI of the WINDOW EXCESS RETURN
#   (strategy net return minus primary net return, both compounded over the same
#   paired bars) entirely below zero => SUPPORTS_UNDERPERFORMANCE
#   (strategy-vs-primary). The interval shares the estimand and the scale of the
#   point estimate `excess_vs_primary`; a per-bar Sharpe is a different quantity
#   and may never be published as a return interval. Entirely above zero does
#   NOT mint edge (direction must survive the SPA/DSR family rules).
SPA_EDGE_ALPHA = 0.05
DSR_EDGE_CONFIDENCE = 0.95
# Single source of truth for the rule text the code applies; kept in the module
# so the estimator and its preregistered text cannot drift apart (#396).
EXCESS_CI_RULE = (
    "95% block-bootstrap CI of the window EXCESS RETURN (strategy net return "
    "minus primary net return, compounded over the same paired bars) entirely "
    "below zero => SUPPORTS_UNDERPERFORMANCE (strategy-vs-primary); the "
    "interval is on the RETURN scale of excess_vs_primary, never a Sharpe ratio"
)

BENCHMARK_IDS = (
    "cash",
    "btc_buy_hold",
    "equal_weight",
    "vol_target",
    "simple_trend",
)
class DatasetIdentity(BaseModel):
    """What real data entered the run. Hash-bound, no substitution allowed."""

    model_config = ConfigDict(frozen=True)

    tape_path: str
    tape_sha256: str
    universe: tuple[str, ...]
    period_start_ns: int
    period_end_ns: int
    n_bars: int
    source_hashes: tuple[str, ...]  # per-candle source_hash chain head/tail sample


class CodeIdentity(BaseModel):
    """Which code produced the numbers, bound by content and not just by revision.

    `git_dirty` is a single bit, so it cannot separate two materially different
    uncommitted trees at the same HEAD; `source_sha256` is what does (see
    :func:`source_sha256`). It is required: a receipt that does not say which
    source content ran is the defect this field exists to close.
    """

    model_config = ConfigDict(frozen=True)

    git_rev: str
    git_dirty: str
    config_sha256: str
    estimator_versions: dict[str, str]
    source_sha256: str


class RunIdentity(BaseModel):
    """Binds data, universe, period, code/config, seed, estimator, benchmark, family."""

    model_config = ConfigDict(frozen=True)

    dataset: DatasetIdentity
    code: CodeIdentity
    seed: int
    primary_benchmark: str
    diagnostic_benchmarks: tuple[str, ...]
    strategy_family: tuple[str, ...]
    capital: float
    taker_fee: float
    opex_monthly_usd: float

    def digest(self) -> str:
        raw = self.model_dump_json()
        return hashlib.sha256(raw.encode()).hexdigest()


class MetricSet(BaseModel):
    model_config = ConfigDict(frozen=True)

    net_return: float
    excess_vs_primary: float | None
    # #396: the interval of the quantity above — the WINDOW excess return, on the
    # same scale as `excess_vs_primary` (fraction of capital over the window).
    # A per-bar Sharpe ratio is a different estimand and must never be stored or
    # published here.
    excess_ci_low: float | None = None
    excess_ci_high: float | None = None
    sharpe_per_bar: float
    sharpe_annualized: float
    sharpe_ci_low: float | None
    sharpe_ci_high: float | None
    sharpe_degenerate: bool = False
    max_drawdown: float
    tail_mean_5pct: float
    avg_exposure: float
    #: #472: the curve's own leg-exposure concentration, measured as
    #: ``max_i |w_i| / sum_j |w_j|`` over the per-leg exposure weights the
    #: producer handed over (`concentration_legs`). `None` means no measurement
    #: stands behind the cell -- never a constant, and never a zero-filled or
    #: one-filled placeholder: the pre-fix producer published the literal `1.0`
    #: for every curve, so a structural claim (cash vs a four-leg basket) was
    #: published as a shared number nobody measured.
    concentration: float | None
    #: The per-leg exposure weights behind the cell above: each leg's
    #: window-MEAN absolute weight, keyed by leg. Published so the number is
    #: recomputable from the receipt alone instead of taken on trust; empty when
    #: no leg weights reached the measurement.
    concentration_legs: dict[str, float] = Field(default_factory=dict)
    #: Why the cell reads what it reads (`MEASURED_MAX_LEG_SHARE`, or
    #: `CONCENTRATION_NOT_MEASURED_NO_LEG_WEIGHTS[:<ground>]`), following the
    #: `funding_basis` precedent.
    concentration_basis: str | None = None
    turnover_notional_over_capital: float
    commission_cost: float
    funding_cost: float | None  # None == MISSING, never zero-filled
    #: Why the cell above reads what it reads. Set when the leg was measured (or
    #: could not be) so a reader never has to infer a named reason from an
    #: absence: `MEASURED`, `NO_ENGINE_RULE`, `TRADES_DIVERGED`,
    #: `BASKET_CONVENTION_DIFFERS[:<basket_id>]` -- the one state whose payload
    #: names the published curve that carries the number -- or None where the
    #: zero-exposure rule already speaks for the curve.
    funding_basis: str | None = None
    cost_basis: str  # VERIFIED_ENGINE | ANALYTIC_MODEL | MISSING
    n_bars: int
    n_trades: int
    #: #445: this curve's measured engine<->series closed-loop reconciliation --
    #: the signed residual, the `closed_loop_terms` it is a function of, the
    #: tolerance it was compared against, the closed-loop identity it was
    #: computed under and the named reason it carries. `None` when the curve
    #: measured no reconciliation of its own (the analytic legs), and a curve
    #: whose balance could not be read publishes `state=NOT_MEASURED` with a null
    #: residual rather than a number nobody measured (#445).
    cost_reconciliation: dict[str, Any] | None = None


class EvidenceVerdicts(BaseModel):
    model_config = ConfigDict(frozen=True)

    research_validity: VerdictState
    research_note: str
    economic: VerdictState
    economic_note: str
    #: A declared `VerdictState`, plus the dependencies it could not measure when
    #: the state is `ESTIMATOR_UNAVAILABLE` (`ESTIMATOR_UNAVAILABLE: scipy,arch`);
    #: see :func:`statistical_verdict_state` (#440). The state alone cannot say
    #: *which* estimator never ran, and a consumer must not have to read the note.
    statistical: str
    statistical_note: str
    portfolio: VerdictState
    portfolio_note: str
    execution: VerdictState
    execution_note: str
    capital: VerdictState
    capital_note: str

    @field_validator("statistical")
    @classmethod
    def _statistical_state_is_declared(cls, value: str) -> str:
        """The published statistical verdict leads with a declared state (#440).

        The one cell that may carry a payload keeps its vocabulary closed: the
        token before the colon is checked against `VERDICT_STATES`, so a
        consumer can switch on it without parsing free text.
        """
        token = statistical_verdict_state(value)
        if token not in VERDICT_STATES:
            raise ValueError(
                f"undeclared statistical verdict state {token!r}; "
                f"declared: {sorted(VERDICT_STATES)}"
            )
        return value


class EconomicReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    receipt_id: str
    run: RunIdentity
    metrics: dict[str, MetricSet]
    oos_metrics: dict[str, MetricSet]
    verdicts: EvidenceVerdicts
    statistics: dict[str, Any]
    controls: dict[str, Any]
    portfolio_mix: dict[str, Any]
    capacity_scenarios: list[dict[str, Any]]
    parity: dict[str, Any]
    shadow_live: dict[str, Any]
    limitations: list[str]
    #: Measured execution evidence: the fill/latency/fee semantics actually in
    #: force plus the frictions they produced (shortfall, order lifetime,
    #: commissions, fill signature). Empty when no execution telemetry was
    #: produced -- never populated with placeholder or assumed values.
    execution: dict[str, Any] = Field(default_factory=dict)
    #: Which statistics dependencies this interpreter could import, probed before
    #: any estimator was called (#440). Default is the probe itself, so every
    #: receipt carries it beside `run.code.estimator_versions` -- the field that
    #: names what is installed -- without a builder having to remember it.
    estimator_availability: EstimatorAvailability = Field(
        default_factory=estimator_availability
    )
    claim_status: Literal["NO_ECONOMIC_CLAIM"] = "NO_ECONOMIC_CLAIM"

    def digest(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


def git_info() -> dict[str, str]:
    def run(*args: str) -> str:
        try:
            out = subprocess.run(
                ["git", *args], capture_output=True, text=True, timeout=10
            )
            return out.stdout.strip() if out.returncode == 0 else "unknown"
        except Exception:
            return "unknown"

    dirty = run("status", "--porcelain")
    return {
        "rev": run("rev-parse", "--short", "HEAD"),
        "dirty": "yes"
        if dirty not in ("", "unknown")
        else ("unknown" if dirty == "unknown" else "no"),
    }


def estimator_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for name in ("numpy", "scipy", "arch", "nautilus_trader", "polars"):
        try:
            import importlib.metadata as md

            versions[name] = md.version(name)
        except Exception:
            versions[name] = "absent"
    return versions


#: Project members outside `src/` that decide what the code can do. Recorded by
#: name so a missing member changes the digest instead of disappearing from it.
SOURCE_MEMBERS = ("pyproject.toml", "uv.lock")

#: Build residue, not code: it must never enter the source digest.
SOURCE_SKIP_DIRS = frozenset({"__pycache__", ".venv"})

#: What `CodeIdentity.source_sha256` covers, in the receipt's own words: a
#: receipt must say which source content it binds, not only which revision was
#: declared (a one-bit `git_dirty` flag cannot name two dirty trees apart).
SOURCE_IDENTITY_NOTE = (
    "Code identity: run.code.source_sha256 is sha256 over sorted "
    "(relative path, file sha256) for v8-next/src/**, v8-next/pyproject.toml and "
    "v8-next/uv.lock; run.code.git_dirty is one bit and cannot separate two dirty "
    "trees at the same revision, so the source hash is what identifies the code."
)


def project_root() -> Path:
    """The v8-next project directory this module runs from (`.../v8-next`)."""
    return Path(__file__).resolve().parents[3]


def _source_rows(base: Path) -> list[tuple[str, str]]:
    """Sorted `(relative posix path, file sha256)` for every file that can run."""
    rows: list[tuple[str, str]] = []
    src = base / "src"
    if src.is_dir():
        for path in src.rglob("*"):
            if not path.is_file() or path.suffix == ".pyc":
                continue
            rel = path.relative_to(base)
            if SOURCE_SKIP_DIRS.intersection(rel.parts):
                continue
            rows.append(
                (rel.as_posix(), hashlib.sha256(path.read_bytes()).hexdigest())
            )
    else:
        rows.append(("src", "ABSENT"))
    for name in SOURCE_MEMBERS:
        member = base / name
        rows.append(
            (
                name,
                hashlib.sha256(member.read_bytes()).hexdigest()
                if member.is_file()
                else "ABSENT",
            )
        )
    return sorted(rows)


def source_sha256(root: Path | str | None = None) -> str:
    """Content digest of the code that can actually run under `root`.

    Covers `src/**`, `pyproject.toml` and `uv.lock` as sorted
    `(relative path, per-file sha256)` pairs, so a materially changed tree is a
    different identity even at the same `git_rev` with the same one-bit
    `git_dirty`. Deterministic: no wall clock, no absolute path, no directory
    listing order — only file bytes and their project-relative names.
    """
    base = Path(root) if root is not None else project_root()
    digest = hashlib.sha256()
    for relpath, file_sha256 in _source_rows(base):
        digest.update(relpath.encode())
        digest.update(b"\0")
        digest.update(file_sha256.encode())
        digest.update(b"\0")
    return digest.hexdigest()


def code_and_lock_hash(root: Path | str | None = None) -> str:
    """`rev:dirty:source_sha256` — the code-change component of a run key.

    The one-bit `git_dirty` value alone cannot tell two dirty trees apart, so a
    changed tree must be distinguished by its content digest; otherwise a re-run
    of changed code in the same output directory is mistaken for a completed
    window. Lock/config bytes are covered by :func:`source_sha256`.
    """
    info = git_info()
    return f"{info['rev']}:{info['dirty']}:{source_sha256(root)}"


@dataclass(frozen=True)
class BarView:
    end_ns: int
    open: float
    high: float
    low: float
    close: float


def bars_from_candles(candles: Sequence[Any]) -> list[BarView]:
    return [
        BarView(
            end_ns=int(c.end_ns),
            open=float(c.open),
            high=float(c.high),
            low=float(c.low),
            close=float(c.close),
        )
        for c in candles
    ]


def validate_chronology(bars: Sequence[BarView]) -> tuple[bool, str]:
    """Point-in-time guard: strictly increasing, positive prices, no gaps in order."""
    if len(bars) < 2:
        return False, "EMPTY_OR_SINGLE_BAR_WINDOW"
    for i, b in enumerate(bars):
        if not all(math.isfinite(v) and v > 0 for v in (b.open, b.high, b.low, b.close)):
            return False, f"NON_POSITIVE_OR_NONFINITE_BAR_{i}"
        if not (b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high):
            return False, f"OHLC_ORDER_VIOLATION_{i}"
        if i and bars[i - 1].end_ns >= b.end_ns:
            return False, f"NON_MONOTONIC_TIME_{i}"
    return True, "OK"


#: Identity of the future-leak probe (#437). Published beside its verdict so a
#: reader can tell *which* measurement produced the state, and so a receipt that
#: merely claims `leak_probe=OK` without this identity is visibly incomplete.
FUTURE_LEAK_PROBE = "series_own_bar_close_containment"

#: What the known-defect control measured (#493): the *displaced* copy of the
#: series under test, built by :func:`displaced_closes_against_timestamps`, plus
#: the mutation rule it applies. A `CAUGHT`/`MISSED` status is a verdict on that
#: copy, so a block that publishes the copy's violation counts without naming it
#: reads as a live leak on the canonical tape -- which it is not.
FUTURE_LEAK_DISPLACED_INPUT = "displaced_closes_against_timestamps(bars)"
FUTURE_LEAK_DISPLACED_RULE = (
    "bar i keeps timestamp/open/high/low and reports the close of bar i+1"
)


def series_sha256(bars: Sequence[BarView]) -> str:
    """sha256 over the exact per-bar tuples of ``bars``.

    Digests the series a probe measured, not a label for it: two runs that load
    the same bars publish the same digest, and a series with any bar rewritten
    (the displaced copy above, most of all) cannot share the canonical digest.
    """
    digest = hashlib.sha256()
    for b in bars:
        row = f"{int(b.end_ns)},{b.open!r},{b.high!r},{b.low!r},{b.close!r}\n"
        digest.update(row.encode())
    return digest.hexdigest()


def series_set_sha256(per_series: Mapping[str, str]) -> str:
    """One digest over ``{name: series_sha256}``, rows sorted by name.

    The portfolio path measures one control per instrument; its block publishes
    this single digest over the per-instrument ones, re-derivable from the block's
    own ``instruments`` cells (#493).
    """
    digest = hashlib.sha256()
    for name in sorted(per_series):
        digest.update(f"{name}:{per_series[name]}\n".encode())
    return digest.hexdigest()


def displaced_closes_against_timestamps(bars: Sequence[BarView]) -> list[BarView]:
    """Return ``bars`` with its close vector displaced against its own timestamps.

    Bar ``i`` keeps its own timestamp, its own open, and its own high/low range
    but reports the close of bar ``i+1``: the series still looks like candles
    (positive prices, strictly increasing time, ordered ranges) while every bar
    now quotes a price that did not exist until the *next* bar had closed. It is
    the known defect the probe below must be able to catch, built from the series
    under test rather than from a caller-supplied flag, and it is what the
    positive control runs the probe against.
    """
    if len(bars) < 2:
        return []
    return [
        BarView(b.end_ns, b.open, b.high, b.low, bars[i + 1].close)
        for i, b in enumerate(bars[:-1])
    ]


def detect_future_leak(bars: Sequence[BarView]) -> tuple[bool, str]:
    """Point-in-time probe: does every bar quote only its own timestamp's prices?

    A future leak is a price vector carried by the wrong timestamps. It is
    decidable from ``bars`` alone -- no caller-supplied flag can set the verdict,
    because the verdict is a measurement of the series handed in -- since the
    bar's own ``[low, high]`` range is the price path its timestamp can
    legitimately contain:

    * a close outside its own bar's range is a quote taken from another
      timestamp's path, and the note says how many of those closes land inside
      the *next* bar instead (the look-ahead direction);
    * a close vector whose consecutive closes are all equal carries no per-bar
      price at all (a duplicated/stale copy of one value);
    * a close vector with no boundary carrying a close into the next bar's open
      is not ordered by this series' own time.

    Returns ``(True, "OK; ...measured counts...")`` only when the series shows
    none of these, and a named ``FUTURE_LEAK_*`` reason otherwise. The same call
    therefore returns ``False`` for a series displaced and ``True`` for the same
    series undisplaced: the verdict discriminates instead of being constant.

    Stated limitation: a displacement that moves timestamps *and* prices together
    (a whole-record block shift) is not observable in a single series and is not
    claimed here. What this probe bounds is the close-against-own-bar class,
    measured on the very series the numbers were computed from: on the canonical
    tapes it measures 0 close-in-own-bar violations, 42-53% carried-forward
    boundaries, and 177-234 violations on the same series displaced.
    """
    if len(bars) < 2:
        return False, f"FUTURE_LEAK_PROBE_UNDECIDABLE_SERIES_LEN_{len(bars)}"
    outside = [i for i, b in enumerate(bars) if not (b.low <= b.close <= b.high)]
    if outside:
        into_next = sum(
            1
            for i in outside
            if i + 1 < len(bars) and bars[i + 1].low <= bars[i].close <= bars[i + 1].high
        )
        return False, (
            f"FUTURE_LEAK_CLOSE_OUTSIDE_OWN_BAR: {len(outside)}/{len(bars)} closes are not in "
            f"their own bar (first at index {outside[0]}); {into_next} of them are inside the "
            "NEXT bar, i.e. the close series is carried by the wrong timestamps"
        )
    repeated = sum(1 for i in range(len(bars) - 1) if bars[i].close == bars[i + 1].close)
    if repeated == len(bars) - 1:
        return False, (
            f"FUTURE_LEAK_CLOSE_VECTOR_DUPLICATED: all {len(bars) - 1} consecutive closes are "
            "equal, so the close vector measures no per-bar price"
        )
    # A candle series carries its close into the next bar: `open[i+1]` is the price
    # the market reopened at after bar `i` closed. Measured on the canonical tapes
    # this holds on 42-53% of boundaries (the rest are session gaps/rollovers) and
    # on none of them is it 0. This is also the load-bearing rule inside the app
    # paths: the ingest guard (`domain/market.py`) already rejects a close outside
    # its own bar, so a leak that still loads is a close vector ordered by
    # something other than this series' own time -- which is exactly the state
    # below. Fail closed: it is not a pass just because the candles look plausible.
    carried = sum(1 for i in range(len(bars) - 1) if bars[i].close == bars[i + 1].open)
    if carried == 0:
        return False, (
            f"FUTURE_LEAK_CLOSE_NOT_CARRIED_FORWARD: 0/{len(bars) - 1} bar boundaries continue "
            "a close into the next bar's open, so the close vector is not bound to the "
            "timestamps that carry it"
        )
    return True, (
        f"OK; probe={FUTURE_LEAK_PROBE}; bars={len(bars)}; "
        f"close_in_own_bar={len(bars)}/{len(bars)}; repeated_closes={repeated}; "
        f"close_carried_forward={carried}/{len(bars) - 1}"
    )


def _real_series_status(ok: bool, note: str) -> str:
    """The real-series side of the control block, in one word (#493).

    ``OK`` only when the probe passed the series as it is. A named
    ``FUTURE_LEAK_*`` reason is a ``LEAK`` on that series, except the probe's own
    undecidability (too short a series to decide), which stays ``UNDECIDABLE``
    instead of being published as a leak it never measured.
    """
    if ok:
        return "OK"
    if note.startswith("FUTURE_LEAK_PROBE_UNDECIDABLE"):
        return "UNDECIDABLE"
    return "LEAK"


def future_leak_positive_control(bars: Sequence[BarView]) -> dict[str, Any]:
    """Run the probe against a genuinely displaced copy of ``bars`` (known defect).

    The returned ``status`` is the *measurement*: ``CAUGHT`` means the probe
    returned a named future-leak reason on the displaced series, ``MISSED`` means
    it passed a series whose closes are one bar ahead of their timestamps. A
    probe that cannot fail cannot produce ``CAUGHT`` here, so publishing this
    control is publishing whether the probe still discriminates.

    #493: the block names the series each number belongs to. The verdict is
    measured on the *displaced* copy, so the copy is published with that verdict
    -- ``input``/``input_rule`` say which series and which mutation,
    ``bars_in``/``bars_out`` its extent, ``input_sha256`` its bytes -- and the
    same call's measurement of the series as it stands is published beside it as
    ``real_series``. Without that, a per-instrument ``"152/335 closes are not in
    their own bar"`` read as a live leak on the canonical tape while
    ``research_validity`` reported the identical probe ``VALID`` on that same
    tape. The two subjects are one block, named, or the block is incomplete.
    """
    displaced = displaced_closes_against_timestamps(bars)
    ok, note = detect_future_leak(displaced)
    real_ok, real_note = detect_future_leak(bars)
    return {
        "status": "CAUGHT" if not ok else "MISSED",
        "probe": FUTURE_LEAK_PROBE,
        "subject": "displaced_series",
        "input": FUTURE_LEAK_DISPLACED_INPUT,
        "input_rule": FUTURE_LEAK_DISPLACED_RULE,
        # #437 pins `bars` to the displaced length: it is `bars_out`, never a
        # canonical-tape count, which is why `bars_in` is published beside it.
        "bars": len(displaced),
        "bars_in": len(bars),
        "bars_out": len(displaced),
        "input_sha256": series_sha256(displaced),
        "reason": note,
        "real_series": {
            "status": _real_series_status(real_ok, real_note),
            "subject": "canonical_series",
            "bars": len(bars),
            # The canonical series' own digest: beside `input_sha256` it shows the
            # two subjects are two different series, not one series described twice.
            "series_sha256": series_sha256(bars),
            "reason": real_note,
        },
    }


def aggregate_future_leak_control(
    per_instrument: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    """Roll per-instrument known-defect controls into one named block (#493).

    The portfolio path measures one control per instrument and publishes a single
    ``known_defect_future_leak_caught`` for the run, so the aggregate carries the
    same provenance as the per-instrument blocks it summarizes: the extent
    (``bars_in``/``bars_out``) summed, one ``input_sha256`` over the per-instrument
    digests, the real-series measurement per instrument, and the rule. ``status``
    keeps its #437 semantics exactly -- ``CAUGHT`` iff every instrument's displaced
    copy was caught.
    """
    rows = list(per_instrument.values())
    caught = all(str(r.get("status")) == "CAUGHT" for r in rows)
    real = {name: r["real_series"] for name, r in per_instrument.items()}
    real_statuses = [str(r["status"]) for r in real.values()]
    if not real_statuses:
        real_status = "NO_SERIES"
    elif all(s == "OK" for s in real_statuses):
        real_status = "OK"
    elif any(s == "LEAK" for s in real_statuses):
        real_status = "LEAK"
    else:
        real_status = "UNDECIDABLE"
    return {
        "status": "CAUGHT" if caught else "MISSED",
        "probe": FUTURE_LEAK_PROBE,
        "subject": "displaced_series_per_instrument",
        "input": FUTURE_LEAK_DISPLACED_INPUT,
        "input_rule": FUTURE_LEAK_DISPLACED_RULE,
        "bars_in": sum(int(r.get("bars_in") or 0) for r in rows),
        "bars_out": sum(int(r.get("bars_out") or 0) for r in rows),
        "input_sha256": series_set_sha256(
            {name: str(r["input_sha256"]) for name, r in per_instrument.items()}
        ),
        "real_series": {
            "status": real_status,
            "bars": sum(int(r["bars"]) for r in real.values()),
            "series_sha256": series_set_sha256(
                {name: str(r["series_sha256"]) for name, r in real.items()}
            ),
            # A roll-up summarizes; it does not re-measure. The named readings are
            # the per-instrument cells below, one subject each.
            "reason": (
                f"{real_status}; probe={FUTURE_LEAK_PROBE}; instruments={len(real)}; "
                "per-instrument readings in real_series.instruments"
            ),
            "instruments": real,
        },
        "instruments": dict(per_instrument),
    }


def per_bar_returns(equity: Sequence[float]) -> list[float]:
    out: list[float] = []
    for prev, cur in zip(equity, equity[1:], strict=False):
        out.append((cur - prev) / prev if prev > 0 else 0.0)
    return out


def sharpe_stats(returns: Sequence[float]) -> tuple[float, float]:
    arr = np.asarray(list(returns), dtype=np.float64)
    if arr.size < 2:
        return 0.0, 0.0
    sd = float(arr.std(ddof=1))
    if sd <= 1e-12:
        return 0.0, 0.0
    s = float(arr.mean() / sd)
    return s, s * SHARPE_ANNUALIZATION


def block_bootstrap_ci(
    returns: Sequence[float],
    block: int = BOOTSTRAP_BLOCK,
    reps: int = BOOTSTRAP_REPS,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float | None, float | None]:
    """Circular block bootstrap CI for the PER-BAR Sharpe; preserves dependence.

    Deliberately left on the per-bar scale: it is the primitive used for
    scale-free sign tests (positive control). Anything PUBLISHED as a Sharpe
    confidence interval must go through `sharpe_ci_annualized` so it shares the
    scale of `sharpe_annualized` (#388). It is NOT the excess-return interval:
    a Sharpe ratio is not a return, so `excess_ci_*` is produced by
    `period_excess_ci` (#396).
    """
    arr = np.asarray(list(returns), dtype=np.float64)
    n = arr.size
    if n < 2 * block or not np.isfinite(arr).all():
        return None, None
    rng = np.random.default_rng(seed)
    stats: list[float] = []
    for _ in range(reps):
        idx = (rng.integers(0, n, size=(n // block + 1))[:, None] + np.arange(block)).ravel()[:n] % n
        sample = arr[idx]
        sd = float(sample.std(ddof=1))
        if sd <= 1e-12 or not math.isfinite(sd):
            continue
        stats.append(float(sample.mean() / sd))
    if len(stats) < 10:
        return None, None
    lo, hi = np.percentile(np.asarray(stats), [2.5, 97.5])
    return float(lo), float(hi)


def sharpe_ci_annualized(
    returns: Sequence[float],
    block: int = BOOTSTRAP_BLOCK,
    reps: int = BOOTSTRAP_REPS,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float | None, float | None]:
    """Block-bootstrap Sharpe CI on the scale of `sharpe_annualized` (#388).

    The bootstrap distribution is resampled on per-bar returns, so its
    percentiles are per-bar Sharpe values; they are multiplied by the same
    sqrt(HOURS_PER_YEAR) factor the point estimate uses. Publishing both on one
    scale is what lets a reader check `ci_low <= sharpe_annualized <= ci_high`.
    """
    lo, hi = block_bootstrap_ci(returns, block=block, reps=reps, seed=seed)
    if lo is None or hi is None:
        return None, None
    return lo * SHARPE_ANNUALIZATION, hi * SHARPE_ANNUALIZATION


def unreproducible_variance(
    sharpe_annualized: float,
    ci_low: float | None,
    ci_high: float | None,
    informative_bars: int | None = None,
) -> bool:
    """Is this Sharpe arithmetic unsupported by the variance behind it? (#438)

    The single criterion behind `DEGENERATE_VARIANCE_MARKER`, derived from what
    the row PUBLISHES rather than from the length of the window it was measured
    in. A row cannot be interpreted when, on the published annualized scale:

    - `informative_bars` is known and below `MIN_INFORMATIVE_BARS`: the variance
      the Sharpe divides by rests on too few bars to be an estimate at all;
    - the point estimate is past `DEGENERATE_PER_BAR_SHARPE`: a per-bar Sharpe of
      1.0 only arises when that variance is ~0;
    - the interval is past `DEGENERATE_CI_RATIO` times its own point estimate:
      the bootstrap's surviving replicates split into near-zero-variance draws,
      so its percentiles stop being a range the estimate lives in.

    Passing `informative_bars=None` means "not known at this call site" (a stored
    receipt carries no bar count when the marker is re-derived at render time)
    and drops only the first test; the two tests that read published cells stay
    identical, which is what keeps a stored flag and a re-derived one in
    agreement instead of drifting apart.

    Absence of an interval is NOT degeneracy on its own: a row whose CI could not
    be computed publishes no interval and is judged on the other two tests.
    """
    if informative_bars is not None and informative_bars < MIN_INFORMATIVE_BARS:
        return True
    if not math.isfinite(sharpe_annualized):
        return True
    if abs(sharpe_annualized) / SHARPE_ANNUALIZATION > DEGENERATE_PER_BAR_SHARPE:
        return True
    if ci_low is None or ci_high is None or sharpe_annualized == 0.0:
        return False
    return max(abs(ci_low), abs(ci_high)) / abs(sharpe_annualized) > DEGENERATE_CI_RATIO


def period_excess_ci(
    equity: Sequence[float],
    primary_equity: Sequence[float],
    block: int = BOOTSTRAP_BLOCK,
    reps: int = BOOTSTRAP_REPS,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float | None, float | None]:
    """Block-bootstrap CI for the WINDOW EXCESS RETURN the point estimate uses (#396).

    The estimand is exactly `excess_vs_primary`: the strategy's net return over
    the window minus the primary benchmark's net return over the same window,
    each compounded from the same bar sequence. Every replicate draws ONE set of
    circular block indices and applies it to both curves, so the paired bars stay
    paired; it then compounds both legs over the resampled sequence and records
    their difference. The observed window is itself one such replicate (identity
    ordering), so the percentiles are on the return scale and bracket their own
    point estimate, which is what makes
    `excess_ci_low <= excess_vs_primary <= excess_ci_high` readable.

    A per-bar Sharpe ratio is NOT a return, so `block_bootstrap_ci` (which
    bootstraps `mean/sd`) can never supply this interval.
    """
    strat = np.asarray(list(equity), dtype=np.float64)
    prim = np.asarray(list(primary_equity), dtype=np.float64)
    if strat.size != prim.size or strat.size < 2 * block + 1:
        return None, None
    s_rets = strat[1:] / strat[:-1] - 1.0
    p_rets = prim[1:] / prim[:-1] - 1.0
    n = s_rets.size
    if not (np.isfinite(s_rets).all() and np.isfinite(p_rets).all()):
        return None, None
    rng = np.random.default_rng(seed)
    stats: list[float] = []
    for _ in range(reps):
        idx = (rng.integers(0, n, size=(n // block + 1))[:, None] + np.arange(block)).ravel()[:n] % n
        stats.append(float(np.prod(1.0 + s_rets[idx]) - np.prod(1.0 + p_rets[idx])))
    if len(stats) < 10 or not all(math.isfinite(v) for v in stats):
        return None, None
    lo, hi = np.percentile(np.asarray(stats), [2.5, 97.5])
    return float(lo), float(hi)


def max_drawdown(equity: Sequence[float]) -> float:
    peak = -math.inf
    worst = 0.0
    for v in equity:
        peak = max(peak, v)
        if peak > 0:
            worst = min(worst, (v - peak) / peak)
    return worst


def compute_multileg_family(
    legs_closes: dict[str, list[float]],
    capital: float = CAPITAL_DEFAULT,
    taker_fee: float = TAKER_FEE_DEFAULT,
) -> dict[str, dict[str, Any]]:
    """Costed passive benchmarks over a real multi-asset universe.

    equal_weight buys 1/N per leg once (no rebalance fiction). vol_target
    scales each leg by trailing-only volatility with equal risk contribution
    and pays turnover on every weight change. bh_<RAW> holds each leg alone.
    The rule is causal: the position for bar i is decided on the closed bar
    i-1 only (prior 48-bar high vs prior close); the current bar's own move
    is never both the signal and the earned return.
    """
    names = sorted(legs_closes)
    n = len(next(iter(legs_closes.values())))
    fams: dict[str, dict[str, Any]] = {}
    fams["cash"] = {"equity": [capital] * n, "turnover": 0.0, "commission": 0.0,
                    "n_trades": 0, "exposure": [0.0] * n, "leg_weights": {}}
    for raw in names:
        closes = legs_closes[raw]
        shares = (capital * (1.0 - taker_fee)) / closes[0]
        fams[f"bh_{raw.split('.')[0]}"] = {
            "equity": [shares * c for c in closes], "turnover": 1.0,
            "commission": capital * taker_fee, "n_trades": 1, "exposure": [1.0] * n,
            # #472: one leg, measured -- the only way this curve may read 1.0.
            "leg_weights": {raw: [1.0] * n},
        }
    k = len(names)
    eq_ew = [capital * (1.0 - taker_fee)]
    for i in range(1, n):
        r = sum(legs_closes[w][i] / legs_closes[w][i - 1] - 1.0 for w in names) / k
        eq_ew.append(eq_ew[-1] * (1.0 + r))
    fams["equal_weight"] = {"equity": eq_ew, "turnover": 1.0,
                            "commission": capital * taker_fee, "n_trades": k,
                            "exposure": [1.0] * n,
                            # #472: the basket's own 1/k per leg, measured.
                            "leg_weights": {w: [1.0 / k] * n for w in names}}
    target_pb = VOL_TARGET_ANNUAL / math.sqrt(HOURS_PER_YEAR)
    eq_vt = [capital]
    exp_vt = [0.0]
    turn_vt = 0.0
    comm_vt = 0.0
    prev_w = [0.0] * k
    #: #472: the same per-bar leg weights the curve is built from, published with
    #: it so `concentration` is a function of this curve's own run.
    wts_vt: dict[str, list[float]] = {leg: [0.0] for leg in names}
    for i in range(1, n):
        inv = []
        for leg in names:
            rets = [legs_closes[leg][j] / legs_closes[leg][j - 1] - 1.0
                    for j in range(max(1, i - VOL_LOOKBACK + 1), i + 1)]
            sd = float(np.std(np.asarray(rets), ddof=1)) if len(rets) >= 2 else 0.0
            inv.append(1.0 / sd if sd > 1e-12 else 0.0)
        tot = sum(inv)
        raw_w = [v / tot if tot > 0 else 0.0 for v in inv]
        port_vol = math.sqrt(sum(
            (raw_w[a] * (1.0 / inv[a] if inv[a] > 0 else 0.0)) ** 2 for a in range(k)
        )) if tot > 0 else 0.0
        scale = min(target_pb / port_vol, MAX_LEVERAGE) if port_vol > 1e-12 else 0.0
        wts = [x * scale for x in raw_w]
        cost = sum(abs(wts[a] - prev_w[a]) for a in range(k)) * eq_vt[-1] * taker_fee
        comm_vt += cost
        turn_vt += sum(abs(wts[a] - prev_w[a]) for a in range(k))
        r = sum(wts[a] * (legs_closes[names[a]][i] / legs_closes[names[a]][i - 1] - 1.0)
                for a in range(k))
        eq_vt.append(eq_vt[-1] * (1.0 + r) - cost)
        exp_vt.append(sum(wts))
        for a, leg in enumerate(names):
            wts_vt[leg].append(wts[a])
        prev_w = wts
    fams["vol_target"] = {"equity": eq_vt, "turnover": turn_vt, "commission": comm_vt,
                          "n_trades": 0, "exposure": exp_vt, "leg_weights": wts_vt}
    sleeve = capital / k
    eq_tr = [capital]
    exp_tr = [0.0]
    turn_tr = 0.0
    comm_tr = 0.0
    in_pos = [False] * k
    wts_tr: dict[str, list[float]] = {leg: [0.0] for leg in names}
    for i in range(1, n):
        day_r = 0.0
        ex = 0.0
        for a, raw in enumerate(names):
            closes = legs_closes[raw]
            # Causal: decision uses the closed bar i-1 only (no lookahead).
            window = closes[max(0, i - 1 - VOL_LOOKBACK):i - 1]
            signal = bool(window) and closes[i - 1] > max(window)
            if signal != in_pos[a]:
                cost = sleeve * taker_fee
                comm_tr += cost
                turn_tr += 1.0 / k
                eq_tr[-1] -= cost
                in_pos[a] = signal
            wts_tr[raw].append(1.0 / k if in_pos[a] else 0.0)
            if in_pos[a]:
                day_r += (closes[i] / closes[i - 1] - 1.0) / k
                ex += 1.0 / k
        eq_tr.append(eq_tr[-1] * (1.0 + day_r))
        exp_tr.append(ex)
    fams["simple_trend"] = {"equity": eq_tr, "turnover": turn_tr, "commission": comm_tr,
                            "n_trades": 0, "exposure": exp_tr, "leg_weights": wts_tr}
    return fams


def compute_benchmark_family(
    bars: Sequence[BarView],
    capital: float = CAPITAL_DEFAULT,
    taker_fee: float = TAKER_FEE_DEFAULT,
) -> dict[str, dict[str, Any]]:
    """Five analytic benchmarks on the same bars, capital, and fee. All costs explicit.

    vol_target uses trailing-48-bar volatility only (never future). simple_trend
    is a fixed 48-bar Donchian long/flat rule. equal_weight over the single-BTC
    universe equals buy-and-hold by construction; computed independently anyway.
    """
    closes = [b.close for b in bars]
    n = len(closes)
    fams: dict[str, dict[str, Any]] = {}

    fams["cash"] = {
        "equity": [capital] * n,
        "turnover": 0.0,
        "commission": 0.0,
        "n_trades": 0,
        "exposure": [0.0] * n,
        # #472: no leg ever carried exposure, so there is nothing to concentrate.
        "leg_weights": {},
    }

    shares = (capital * (1.0 - taker_fee)) / closes[0]
    eq_bh = [shares * c for c in closes]
    fams["btc_buy_hold"] = {
        "equity": eq_bh,
        "turnover": (capital / capital),
        "commission": capital * taker_fee,
        "n_trades": 1,
        "exposure": [1.0] * n,
        # #472: one leg, measured -- the only way this curve may read 1.0.
        "leg_weights": {SINGLE_INSTRUMENT_LEG: [1.0] * n},
    }
    fams["equal_weight"] = {
        "equity": list(eq_bh),
        "turnover": 1.0,
        "commission": capital * taker_fee,
        "n_trades": 1,
        "exposure": [1.0] * n,
        "note": "single-asset universe: identical to btc_buy_hold by construction",
        "leg_weights": {SINGLE_INSTRUMENT_LEG: [1.0] * n},
    }

    logrets = [0.0] + [math.log(closes[i] / closes[i - 1]) for i in range(1, n)]
    target_pb = VOL_TARGET_ANNUAL / math.sqrt(HOURS_PER_YEAR)
    # M5: rolling stats in one Polars kernel (no per-bar numpy call overhead).
    # lr[k] == logrets[k+1], so trailing-48 over lr ending at i-1 is exactly
    # logrets[max(1,i-47):i+1]. min_samples=2 preserves the len<2 -> flat rule
    # (null maps to scale 0). Stateful carryover below stays an explicit
    # Python scan in the same order (no numba: not in tree).
    _lr = logrets[1:]
    sd_pre: list[float | None] = (
        pl.Series(_lr).rolling_std(window_size=VOL_LOOKBACK, ddof=1, min_samples=2).to_list()
        if _lr
        else []
    )
    eq_vt = [capital]
    exposure_vt = [0.0]
    turnover_vt = 0.0
    commission_vt = 0.0
    prev_pos = 0.0
    for i in range(1, n):
        sd_val = sd_pre[i - 1]
        if sd_val is None:
            scale = 0.0
        else:
            sd = float(sd_val)
            scale = min(target_pb / sd, MAX_LEVERAGE) if sd > 1e-12 else 0.0
        pos = scale * eq_vt[-1]
        d_notional = abs(pos - prev_pos)
        cost = d_notional * taker_fee
        commission_vt += cost
        turnover_vt += d_notional / capital
        pnl = pos * (closes[i] / closes[i - 1] - 1.0) - cost
        eq_vt.append(eq_vt[-1] + pnl)
        exposure_vt.append(abs(pos) / eq_vt[-2] if eq_vt[-2] > 0 else 0.0)
        prev_pos = pos
    fams["vol_target"] = {
        "equity": eq_vt,
        "turnover": turnover_vt,
        "commission": commission_vt,
        "n_trades": sum(1 for i in range(1, n) if exposure_vt[i] > 0 and exposure_vt[i - 1] == 0),
        "exposure": exposure_vt,
        # #472: a single-instrument curve holds one leg; its own measured exposure
        # IS that leg's weight, so the published concentration is a measurement.
        "leg_weights": {SINGLE_INSTRUMENT_LEG: list(exposure_vt)},
    }

    eq_tr = [capital]
    exposure_tr = [0.0]
    turnover_tr = 0.0
    commission_tr = 0.0
    in_pos = False
    for i in range(1, n):
        # Causal: decide on closed bar i-1 (prior 48-bar high vs prior close).
        lookback = [b.high for b in bars[max(0, i - 1 - VOL_LOOKBACK) : i - 1]]
        signal = bool(lookback) and closes[i - 1] > max(lookback)
        want = 1.0 if signal else 0.0
        have = 1.0 if in_pos else 0.0
        if want != have:
            cost = abs(want - have) * eq_tr[-1] * taker_fee
            commission_tr += cost
            turnover_tr += abs(want - have)
            eq_tr[-1] -= cost
            in_pos = signal
        r = (closes[i] / closes[i - 1] - 1.0) if in_pos else 0.0
        eq_tr.append(eq_tr[-1] * (1.0 + r))
        exposure_tr.append(1.0 if in_pos else 0.0)
    fams["simple_trend"] = {
        "equity": eq_tr,
        "turnover": turnover_tr,
        "commission": commission_tr,
        "n_trades": 0,
        "exposure": exposure_tr,
        # #472: one instrument, in or out; the measured exposure is the leg weight.
        "leg_weights": {SINGLE_INSTRUMENT_LEG: list(exposure_tr)},
    }
    flips = sum(1 for i in range(1, n) if exposure_tr[i] != exposure_tr[i - 1]) // 2
    fams["simple_trend"]["n_trades"] = flips
    return fams


# --------------------------------------------------------------------------- #
# Quad multi-asset baskets (real quad tape, executed in the native engine)
# --------------------------------------------------------------------------- #

#: Published `funding $` states. Every row is exactly one of them, so a reader
#: never has to guess whether a blank-looking cell means "zero", "not
#: applicable" or "nobody measured it" -- the three states that a single
#: `MISSING` cell conflated.
FUNDING_MEASURED = "MEASURED"  # a real USDT cost
FUNDING_ZERO_NO_EXPOSURE = "ZERO_NO_EXPOSURE"  # a measured zero: never invested
FUNDING_NOT_MEASURED = "NOT_MEASURED"  # no funding feed reached this curve
#: Two named reasons a leg has no number even though the run *did* reach it. Both
#: are states of the measurement, never a restatement of NOT_MEASURED: the first
#: says the engine executes no basket for this leg's rule, the second says the
#: engine's funded and unfunded runs traded differently so the balance difference
#: is not attributable to funding.
FUNDING_NO_ENGINE_RULE = "NO_ENGINE_RULE"
FUNDING_TRADES_DIVERGED = "TRADES_DIVERGED"
#: D-165: the leg's measuring basket executes a different sizing convention from
#: the analytic curve published beside it, so the measured number is published on
#: that basket's own curve in the same table and this row names where it went.
#: The one state that carries a payload -- its cell names the basket, the way the
#: published statistical verdict names the estimators it could not run.
FUNDING_BASKET_CONVENTION_DIFFERS = "BASKET_CONVENTION_DIFFERS"

#: The states that withhold the number from the row that names them. Every other
#: row publishes either a measured cost or the measured zero of no exposure.
FUNDING_WITHHELD_STATES: tuple[str, ...] = (
    FUNDING_NO_ENGINE_RULE,
    FUNDING_TRADES_DIVERGED,
    FUNDING_BASKET_CONVENTION_DIFFERS,
)

#: Separator between a `funding_basis` state and its payload (`STATE:detail`).
FUNDING_BASIS_DETAIL_SEPARATOR = ":"


def funding_basis_state(basis: str | None) -> str | None:
    """The declared state token of a `funding_basis` value (`STATE[:detail]`).

    The token before the separator is what a consumer switches on; the detail
    after it is the named reason's own subject (which basket carries the number).
    """
    if basis is None:
        return None
    token = basis.split(FUNDING_BASIS_DETAIL_SEPARATOR, 1)[0].strip()
    return token or None


def funding_basis_detail(basis: str | None) -> str | None:
    """The payload of a `funding_basis` value, or ``None`` when it has none."""
    if basis is None:
        return None
    _, separator, detail = basis.partition(FUNDING_BASIS_DETAIL_SEPARATOR)
    detail = detail.strip()
    return detail if separator and detail else None


def funding_basis_for_convention_differs(basket_id: str) -> str:
    """The `funding_basis` of an index row whose number moved to *basket_id*."""
    return f"{FUNDING_BASKET_CONVENTION_DIFFERS}{FUNDING_BASIS_DETAIL_SEPARATOR}{basket_id}"


def funding_state(m: MetricSet) -> str:
    """Which of the declared funding states this published row is in."""
    if m.funding_cost is not None:
        return FUNDING_MEASURED
    if m.avg_exposure == 0.0:
        # Zero exposure at every settlement is a measurement, not an absence: a
        # curve that never holds a position cannot have paid funding. It is read
        # before the measured-but-withheld bases, so a never-invested curve never
        # reports someone else's reason.
        return FUNDING_ZERO_NO_EXPOSURE
    state = funding_basis_state(m.funding_basis)
    if state in FUNDING_WITHHELD_STATES:
        return state
    return FUNDING_NOT_MEASURED


def funding_cell(m: MetricSet) -> str:
    """The `funding $` cell: a measured cost, or the reason there is not one."""
    state = funding_state(m)
    if state == FUNDING_MEASURED and m.funding_cost is not None:
        return f"{m.funding_cost:.2f}"
    if state == FUNDING_ZERO_NO_EXPOSURE:
        return "0.00 (NO_EXPOSURE)"
    if state in FUNDING_WITHHELD_STATES:
        # A withheld state never publishes the token alone where the state's own
        # subject can be named with it: the reader is told which curve carries
        # the number, not left to search the receipt for it.
        detail = funding_basis_detail(m.funding_basis)
        return f"n/a ({state}, {detail})" if detail else f"n/a ({state})"
    return "n/a (NOT_MEASURED)"


# --------------------------------------------------------------------------- #
# Published `concentration` states (#472)
# --------------------------------------------------------------------------- #

#: The measured state: a real `max_i |w_i| / sum_j |w_j|` on this curve's own
#: per-leg exposure weights. `1.0` is reachable ONLY here, and only by a curve
#: whose measured weights really are one leg (a single-instrument buy-and-hold);
#: it is never a rule default.
CONCENTRATION_MEASURED = "MEASURED_MAX_LEG_SHARE"
#: The unmeasured state: no number is published, and this names why. The pre-fix
#: producer published the literal `1.0` for every curve instead, so this state is
#: what replaces an unmeasured constant.
CONCENTRATION_NOT_MEASURED_NO_LEG_WEIGHTS = "CONCENTRATION_NOT_MEASURED_NO_LEG_WEIGHTS"
#: Grounds of the unmeasured state (the payload after the separator, the way
#: `BASKET_CONVENTION_DIFFERS:<basket_id>` carries its own subject): the producer
#: handed over no leg weights at all, versus a curve whose legs exist but never
#: carried exposure (a zero-exposure `cash` curve concentrates nothing).
CONCENTRATION_NOT_MEASURED_MISSING_LEG_WEIGHTS = (
    f"{CONCENTRATION_NOT_MEASURED_NO_LEG_WEIGHTS}:NO_LEG_WEIGHT_SERIES_HANDED_OVER"
)
CONCENTRATION_NOT_MEASURED_ZERO_LEG_EXPOSURE = (
    f"{CONCENTRATION_NOT_MEASURED_NO_LEG_WEIGHTS}:ZERO_LEG_EXPOSURE"
)
#: Separator between a `concentration_basis` state and its ground.
CONCENTRATION_BASIS_DETAIL_SEPARATOR = ":"

#: The measurement rule, declared where the number is produced so the rule and
#: the arithmetic cannot drift apart.
CONCENTRATION_RULE = (
    "max_i |w_i| / sum_j |w_j| over the curve's own per-leg exposure weights, "
    "each leg weight averaged over the window's bars (MEAN_LEG_EXPOSURE_WEIGHT); "
    "every bar's leg weights must sum to the exposure this same curve publishes "
    "for that bar"
)

#: Float-arithmetic tolerance on the leg-weights-vs-exposure identity. It absorbs
#: summation rounding only: it is never used to rescale a weight series, because a
#: series that does not add up is a different run's series and must fail loudly
#: instead of being normalized into this curve's.
CONCENTRATION_EXPOSURE_TOLERANCE = 1e-9

#: Raised-message token for the fail-loud identity above.
LEG_WEIGHTS_DO_NOT_SUM_TO_EXPOSURE = "LEG_WEIGHTS_DO_NOT_SUM_TO_EXPOSURE"

#: The leg id a single-instrument analytic curve publishes (#472). The bars a
#: :func:`compute_benchmark_family` call is given carry no instrument id, so the
#: leg is named for exactly what that call measured: the one instrument it holds.
SINGLE_INSTRUMENT_LEG = "single_instrument"


def concentration_basis_state(basis: str | None) -> str | None:
    """The declared state token of a `concentration_basis` (`STATE[:ground]`)."""
    if basis is None:
        return None
    token = basis.split(CONCENTRATION_BASIS_DETAIL_SEPARATOR, 1)[0].strip()
    return token or None


def concentration_from_leg_weights(
    leg_weights: Mapping[str, Sequence[float]] | None,
    exposure: Sequence[float],
) -> tuple[float | None, dict[str, float], str]:
    """Measure `max_i |w_i| / sum_j |w_j|` from a curve's own leg weights (#472).

    ``leg_weights`` maps each leg to its per-bar exposure weight, aligned to the
    bars of the *same* curve's ``exposure`` series. Every bar's absolute leg
    weights must add up to the exposure published for that bar; a series that does
    not is another run's series and raises (:data:`LEG_WEIGHTS_DO_NOT_SUM_TO_EXPOSURE`)
    rather than being silently normalized.

    Returns ``(concentration, mean_leg_weights, basis)``. ``None`` with the named
    unmeasured state when no leg weights were handed over, and when the legs never
    carried exposure (nothing to concentrate), so no curve can publish a number
    the producer did not measure.
    """
    if leg_weights is None:
        return None, {}, CONCENTRATION_NOT_MEASURED_MISSING_LEG_WEIGHTS
    n = len(exposure)
    if n == 0:
        return None, {}, CONCENTRATION_NOT_MEASURED_MISSING_LEG_WEIGHTS
    bar_sum = [0.0] * n
    mean_abs: dict[str, float] = {}
    for name, series in leg_weights.items():
        if len(series) != n:
            raise ValueError(
                f"{LEG_WEIGHTS_DO_NOT_SUM_TO_EXPOSURE}: leg {name!r} carries "
                f"{len(series)} bars, the published exposure carries {n}"
            )
        vals = [abs(float(v)) for v in series]
        for i, v in enumerate(vals):
            bar_sum[i] += v
        mean_abs[str(name)] = sum(vals) / n
    for i in range(n):
        published = abs(float(exposure[i]))
        if abs(bar_sum[i] - published) > CONCENTRATION_EXPOSURE_TOLERANCE * max(1.0, published):
            raise ValueError(
                f"{LEG_WEIGHTS_DO_NOT_SUM_TO_EXPOSURE}: bar {i} leg weights sum to "
                f"{bar_sum[i]} but the exposure published for that bar is {published}"
            )
    total = sum(mean_abs.values())
    if total <= 0.0:
        return None, {}, CONCENTRATION_NOT_MEASURED_ZERO_LEG_EXPOSURE
    return max(mean_abs.values()) / total, mean_abs, CONCENTRATION_MEASURED


def slice_leg_weights(
    leg_weights: Mapping[str, Sequence[float]] | None,
    start: int,
) -> dict[str, list[float]] | None:
    """The leg weights of a curve's OOS slice, offset exactly like its exposure.

    ``None`` in, ``None`` out: a curve that published no leg weights for its full
    window publishes none for its slice either, so the slice row can never read a
    measured state the full-window row did not.
    """
    if leg_weights is None:
        return None
    return {str(k): [float(v) for v in vals[start:]] for k, vals in leg_weights.items()}


def quad_family_legs(
    tape_path: str = "research/tape/quad-1h-12m",
    limit: int | None = 500,
) -> tuple[dict[str, tuple[Any, ...]], dict[str, list[BarView]], tuple[Any, ...]]:
    """Load the quad tape once: (candles per leg, BarViews per leg, funding rows).

    One read feeds both the rule arithmetic and the engine execution, so the two
    cannot describe different windows.
    """
    from v8_next.domain.basket import QUAD_INSTRUMENTS, quad_tape_legs

    tape = quad_tape_legs(tape_path, limit=limit)
    legs = {sym: tape.candles[sym] for sym in QUAD_INSTRUMENTS}
    views = {sym: bars_from_candles(legs[sym]) for sym in QUAD_INSTRUMENTS}
    return legs, views, tuple(tape.funding)


def load_quad_bars(
    limit: int | None = 500,
    tape_path: str = "research/tape/quad-1h-12m",
) -> dict[str, list[BarView]]:
    """Quad tape as per-instrument BarViews (real venue klines)."""
    _, views, _ = quad_family_legs(tape_path, limit)
    return views


def compute_basket_equity_real(
    quad_bars: dict[str, list[BarView]],
    kind: str,
    capital: float = CAPITAL_DEFAULT,
    taker_fee: float = TAKER_FEE_DEFAULT,
    *,
    rebalance_bars: int | None = INTERVAL_BARS,
    vol_lookback: int = VOL_LOOKBACK,
    vol_target_annual: float = VOL_TARGET_ANNUAL,
    max_leverage: float = MAX_LEVERAGE,
) -> dict[str, Any]:
    """Per-bar equity of one basket **rule** on real closes.

    The rule mirrors :class:`v8_next.adapters.basket_backtest.BasketStrategy` in
    schedule and sizing -- same rebalance bars, same trailing volatility window,
    same per-leg notional cap -- so both describe one strategy. Engine execution
    adds what only the venue has: lot rounding to ``size_increment``, a minimum
    order notional, and the configured fill model. Those differ from pure rule
    arithmetic, so the engine's realized terminal balance is published beside this
    curve as ``engine_vs_rule_terminal_delta_usdt``; a divergence is disclosed,
    never assumed away. The curve itself is labeled ``ANALYTIC_MODEL`` wherever it
    is published.
    """
    names = tuple(sorted(quad_bars))
    if not names:
        raise ValueError("no instruments in quad bars")
    n = len(quad_bars[names[0]])
    misaligned = [sym for sym in names if len(quad_bars[sym]) != n]
    if misaligned:
        raise ValueError(f"quad bars misaligned: {', '.join(misaligned)}")
    if n < 2:
        raise ValueError("quad basket needs at least two bars")
    closes = {sym: [b.close for b in quad_bars[sym]] for sym in names}
    k = len(names)
    if kind not in ("buy_hold", "equal_weight", "vol_target"):
        raise ValueError(f"unknown basket kind {kind!r}")
    if kind == "buy_hold" and k != 1:
        raise ValueError("buy_hold is a single-instrument rule")

    quantities = dict.fromkeys(names, 0.0)
    cash = capital
    equity: list[float] = []
    exposure: list[float] = []
    #: #472: the same per-bar per-leg exposure weights `exposure` is the sum of,
    #: published with the curve so concentration is measured on this run's own
    #: positions (`leg notional / equity`) instead of assumed.
    leg_weights: dict[str, list[float]] = {sym: [] for sym in names}
    turnover = 0.0
    commission = 0.0
    n_trades = 0

    for i in range(n):
        bar = i + 1  # 1-based, matching the strategy's aligned-bar counter
        targets: dict[str, float] | None = None
        if kind == "buy_hold":
            targets = {names[0]: capital} if bar == 1 else None
        elif kind == "equal_weight":
            if bar == 1 or (rebalance_bars and bar % rebalance_bars == 0):
                targets = {sym: capital / k for sym in names}
        else:  # vol_target
            if bar >= vol_lookback + 1:
                inv_vol: dict[str, float] = {}
                for sym in names:
                    rets = [
                        closes[sym][j] / closes[sym][j - 1] - 1.0
                        for j in range(i + 1 - vol_lookback, i + 1)
                    ]
                    sd = float(np.std(np.asarray(rets), ddof=1)) if len(rets) >= 2 else 0.0
                    inv_vol[sym] = (1.0 / sd) if sd > 1e-12 else 0.0
                total = sum(inv_vol.values())
                if total > 1e-12:
                    cap_per_leg = capital * max_leverage / k
                    targets = {
                        sym: min(capital * weight / total, cap_per_leg)
                        for sym, weight in inv_vol.items()
                    }
        if targets is not None:
            for sym, notional in targets.items():
                px = closes[sym][i]
                if px <= 0:
                    continue
                delta_qty = notional / px - quantities[sym]
                # The engine skips a delta below half a size increment; the rule
                # must skip the same trades or the two curves are different rules.
                if abs(delta_qty) < 0.0005:
                    continue
                traded = abs(delta_qty) * px
                cost = traded * taker_fee
                cash -= delta_qty * px + cost
                commission += cost
                turnover += traded / capital
                n_trades += 1
                quantities[sym] = notional / px
        invested = sum(quantities[sym] * closes[sym][i] for sym in names)
        level = cash + invested
        equity.append(level)
        exposure.append(invested / level if level > 0 else 0.0)
        for sym in names:
            leg_weights[sym].append(
                quantities[sym] * closes[sym][i] / level if level > 0 else 0.0
            )

    return {
        "equity": equity,
        "exposure": exposure,
        # #472: the per-leg weights the exposure above is the sum of. A four-leg
        # basket therefore measures 1/4, not the constant the pre-fix producer
        # published for it and for a zero-exposure curve alike.
        "leg_weights": leg_weights,
        "turnover": turnover,
        "commission": commission,
        "n_trades": n_trades,
        "rule_terminal_equity_usdt": equity[-1],
    }


#: basket id in the published family -> (basket spec id, rule kind)
QUAD_FAMILY_BASKETS: tuple[tuple[str, str, str], ...] = (
    ("btc_buy_hold", "btc_buy_hold", "buy_hold"),
    ("equal_weight", "equal_weight_quad", "equal_weight"),
    ("vol_target", "vol_target_quad", "vol_target"),
)


def compute_benchmark_family_engine(
    *,
    tape_path: str = "research/tape/quad-1h-12m",
    limit: int | None = 500,
    capital: float = CAPITAL_DEFAULT,
    taker_fee: float = TAKER_FEE_DEFAULT,
    verify_engine: bool = True,
) -> dict[str, dict[str, Any]]:
    """Benchmark family on the real quad universe, engine-executed and funded.

    Single truth: the baskets are the same four real instruments the strategy is
    scored against, executed in one shared MARGIN/NETTING account, with funding
    settled from the tape's real funding rows. Each basket therefore publishes a
    **measured** funding cost -- the engine's own balance difference between the
    funded and unfunded run of the identical basket -- instead of a blank cell.
    ``simple_trend`` stays an analytic single-asset reference and says so.
    """
    from v8_next.adapters.basket_backtest import run_basket_funding_measurement
    from v8_next.domain.basket import resolve_basket

    legs, views, funding_rows = quad_family_legs(tape_path, limit)
    n = len(views[sorted(views)[0]])
    fams: dict[str, dict[str, Any]] = {
        "cash": {
            "equity": [capital] * n,
            "exposure": [0.0] * n,
            # #472: never invested -- no leg carried exposure to concentrate.
            "leg_weights": {},
            "turnover": 0.0,
            "commission": 0.0,
            "n_trades": 0,
            "funding": None,
            "cost_basis": "NOT_APPLICABLE",
        }
    }
    for family_id, spec_id, kind in QUAD_FAMILY_BASKETS:
        spec = resolve_basket(spec_id)
        # The rule parameters come from the basket spec, so the published curve
        # and the executed basket cannot be two different rules.
        base = compute_basket_equity_real(
            {sym: views[sym] for sym in spec.instruments},
            kind,
            capital,
            taker_fee,
            rebalance_bars=spec.rebalance_bars,
            vol_lookback=spec.vol_lookback,
            vol_target_annual=spec.vol_target_annual,
            max_leverage=spec.max_leverage,
        )
        base["cost_basis"] = "ANALYTIC_MODEL"
        base["funding"] = None
        base["funding_basis"] = FUNDING_NOT_MEASURED
        if verify_engine:
            measurement = run_basket_funding_measurement(
                {sym: legs[sym] for sym in legs},
                spec_id,
                funding_rows,
                capital=Decimal(str(capital)),
                taker_fee=Decimal(str(taker_fee)),
            )
            base["funding"] = measurement["funding_cost"]
            base["funding_basis"] = measurement["funding_basis"]
            base["engine_terminal_balance_usdt"] = measurement["funded_balance_usdt"]
            base["engine_unfunded_balance_usdt"] = measurement["unfunded_balance_usdt"]
            base["funding_settlements_fed"] = measurement["funding_settlements_fed"]
            base["funding_rows_available"] = measurement["funding_rows_available"]
            base["funding_out_of_window"] = measurement["funding_out_of_window"]
            base["engine_vs_rule_terminal_delta_usdt"] = (
                measurement["funded_balance_usdt"] - base["rule_terminal_equity_usdt"]
            )
        fams[family_id] = base
        if family_id == "equal_weight":
            fams["equal_weight_quad"] = dict(base)
        if family_id == "vol_target":
            fams["vol_target_quad"] = dict(base)

    btc_view = views["BTCUSDT"]
    analytic_single = compute_benchmark_family(btc_view, capital, taker_fee)
    fams["simple_trend"] = dict(analytic_single["simple_trend"])
    fams["simple_trend"]["cost_basis"] = "ANALYTIC_MODEL"
    fams["simple_trend"]["funding"] = None
    return fams


# --------------------------------------------------------------------------- #
# Funding feed for the analytic multi-leg family: measured per leg, engine-side
# --------------------------------------------------------------------------- #

#: The engine basket that executes each published multi-leg family leg whose rule
#: the engine implements, keyed by the family id the reports publish. These are
#: the canonical basket ids `compute_benchmark_family_engine` puts behind
#: `equal_weight` / `vol_target`, so both reports measure one basket under one
#: name instead of this module defining a second equal-weight rule.
FAMILY_ENGINE_BASKETS: dict[str, str] = {
    "equal_weight": "equal_weight_quad",
    "vol_target": "vol_target_quad",
}

#: Prefix of a single-instrument buy-and-hold leg (`bh_BTCUSDT-PERP`).
FAMILY_BUY_HOLD_PREFIX = "bh_"

#: The family leg that is not a strategy at all: capital held, never invested. Its
#: funding is zero by construction at every settlement (the published
#: ZERO_NO_EXPOSURE state), so it is never handed a rule-based reason it has not.
FAMILY_NEVER_INVESTED = "cash"


def family_funding_basket(family_id: str) -> BasketSpec | None:
    """The engine basket whose rule the *family_id* curve models, or ``None``.

    Membership is read off the leg's own declared name, never inferred from what
    the tape happens to hold: a single-instrument leg is measured with a
    single-instrument basket. ``None`` means the engine implements no basket for
    that rule; the caller then publishes that name instead of another basket's
    number.
    """
    from v8_next.domain.basket import resolve_basket, single_leg_buy_hold

    if family_id.startswith(FAMILY_BUY_HOLD_PREFIX):
        symbol = family_id[len(FAMILY_BUY_HOLD_PREFIX):].split("-PERP")[0]
        return single_leg_buy_hold(symbol)
    basket_id = FAMILY_ENGINE_BASKETS.get(family_id)
    if basket_id is None:
        return None
    return resolve_basket(basket_id)


def basket_sizing_differs_from_the_analytic_family(spec: BasketSpec) -> bool:
    """Whether *spec* sizes its legs under a different convention than the curve.

    The analytic family mirrors a compounding rule arithmetic
    (:func:`compute_multileg_family`): its `equal_weight` buys 1/N once and holds
    it, and its `vol_target` scales a compounding curve by trailing portfolio
    volatility. The engine baskets that carry those same rules hold a **fixed
    notional** per leg instead -- `capital/k` rebalanced every `rebalance_bars`,
    or inverse-vol at full capital with a per-leg cap. A basket that rebalances a
    fixed notional is therefore not the curve published beside it, while a
    single-instrument buy-and-hold basket is (both buy once and hold), so the two
    cases must not be conflated.
    """
    if spec.kind == "equal_weight":
        return spec.rebalance_bars is not None
    return spec.kind == "vol_target"


def funding_basket_convention_differs(family_id: str) -> str | None:
    """The engine basket whose own curve carries *family_id*'s measured number.

    ``None`` means the measuring basket executes the curve's own sizing
    convention, so the number stays on the family leg's own row. Membership is
    read off the basket the leg's rule resolves to (its kind and its
    ``rebalance_bars``), never inferred from the leg's name.
    """
    basket_id = FAMILY_ENGINE_BASKETS.get(family_id)
    if basket_id is None:
        return None
    from v8_next.domain.basket import resolve_basket

    spec = resolve_basket(basket_id)
    return basket_id if basket_sizing_differs_from_the_analytic_family(spec) else None


def funding_curve_for_family(family_id: str) -> str:
    """The published curve id that carries *family_id*'s measured funding number.

    The leg's own row, unless its measuring basket executes a different sizing
    convention -- then the number is published on that basket's curve beside it.
    """
    return funding_basket_convention_differs(family_id) or family_id


def measure_benchmark_family_funding(
    legs: dict[str, tuple[Candle, ...]],
    funding: tuple[FundingRow, ...],
    family_ids: Sequence[str],
    *,
    capital: float = CAPITAL_DEFAULT,
    taker_fee: float = TAKER_FEE_DEFAULT,
) -> dict[str, dict[str, Any]]:
    """Engine-measured funding for each published benchmark family leg.

    An analytic family curve that no funding feed ever reached can only print "no
    feed reached this curve" -- a statement about the wiring, not about the
    strategy. This measures every leg whose rule the engine executes on the tape
    the run actually loaded: the engine runs the basket that carries that leg's
    rule over the same legs and funding rows, twice, and the published number is
    its own balance difference in the published sign (negative when paid). No
    settlement model is re-derived here; the engine owns it.

    Per leg the result carries the number (``funding``), the published state
    (``funding_basis``: ``MEASURED``, or the name of the reason it was withheld),
    the basket the number belongs to, the engine's row counters and both run
    balances, so the cell is auditable rather than asserted. A leg whose rule the
    engine does not execute at all is returned with ``NO_ENGINE_RULE``, and the
    never-invested reference is omitted entirely (zero exposure is its own
    measurement).
    """
    from v8_next.adapters.basket_backtest import (
        BASIS_TRADES_DIVERGED,
        run_basket_funding_measurement,
    )

    out: dict[str, dict[str, Any]] = {}
    for family_id in family_ids:
        if family_id == FAMILY_NEVER_INVESTED:
            continue
        spec = family_funding_basket(family_id)
        if spec is None:
            out[family_id] = {
                "funding": None,
                "funding_basis": FUNDING_NO_ENGINE_RULE,
                "funding_basket": None,
                "funding_engine_basis": None,
                "funding_settlements_fed": 0,
                "funding_rows_available": len(funding),
                "funding_out_of_window": 0,
                "funding_unknown_leg": 0,
                "engine_terminal_balance_usdt": None,
                "engine_unfunded_balance_usdt": None,
            }
            continue
        measurement = run_basket_funding_measurement(
            legs,
            spec,
            funding,
            capital=Decimal(str(capital)),
            taker_fee=Decimal(str(taker_fee)),
        )
        if measurement["funding_cost"] is not None:
            state = FUNDING_MEASURED
        elif measurement["funding_basis"] == BASIS_TRADES_DIVERGED:
            # The feed did reach this leg and the engine withheld the number by
            # name; that reason is published, not flattened into NOT_MEASURED.
            state = FUNDING_TRADES_DIVERGED
        else:
            state = FUNDING_NOT_MEASURED
        out[family_id] = {
            "funding": measurement["funding_cost"],
            "funding_basis": state,
            "funding_basket": spec.basket_id,
            "funding_basket_kind": spec.kind,
            "funding_basket_rule": spec.description,
            "funding_engine_basis": measurement["funding_basis"],
            "funding_settlements_fed": measurement["funding_settlements_fed"],
            "funding_rows_available": measurement["funding_rows_available"],
            "funding_out_of_window": measurement["funding_out_of_window"],
            "funding_unknown_leg": measurement["funding_unknown_leg"],
            "engine_terminal_balance_usdt": measurement["funded_balance_usdt"],
            "engine_unfunded_balance_usdt": measurement["unfunded_balance_usdt"],
        }
    return out


def family_basket_mirror_curves(
    views: dict[str, list[BarView]],
    family_funding: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    capital: float = CAPITAL_DEFAULT,
    taker_fee: float = TAKER_FEE_DEFAULT,
) -> dict[str, dict[str, Any]]:
    """The measuring basket's own analytic curve, per convention-differing leg.

    D-165: where a published family leg's funding is measured on an engine basket
    that sizes its legs under a different convention from the curve published
    beside it, that basket's own curve is published in the same table under its
    basket id, so the `funding $` cell and the curve it is read beside describe
    one strategy:

    * the curve is the analytic **mirror of the engine basket** -- the identical
      call shape :func:`compute_benchmark_family_engine` uses, at the spec's
      declared parameters over this run's own legs, labeled ``ANALYTIC_MODEL``;
    * when *family_funding* (this run's measured feed) is passed, the curve
      carries the engine's measured number for that basket, both run balances and
      ``engine_vs_rule_terminal_delta_usdt`` (the funded balance minus this
      curve's own terminal equity), so the engine/rule gap is disclosed rather
      than assumed away.

    Returns ``{basket_id: curve}``. The same builder called on the pre-slice bars
    returns the flow the frozen OOS row must subtract (see ``#443``).
    """
    from v8_next.domain.basket import resolve_basket

    feed = family_funding or {}
    out: dict[str, dict[str, Any]] = {}
    for family_id, basket_id in FAMILY_ENGINE_BASKETS.items():
        spec = resolve_basket(basket_id)
        if not basket_sizing_differs_from_the_analytic_family(spec):
            continue
        missing = [sym for sym in spec.instruments if sym not in views]
        if missing:
            raise ValueError(
                f"quad basket {basket_id} needs leg(s) this run does not carry: "
                f"{', '.join(missing)}"
            )
        curve: dict[str, Any] = compute_basket_equity_real(
            {sym: views[sym] for sym in spec.instruments},
            spec.kind,
            capital,
            taker_fee,
            rebalance_bars=spec.rebalance_bars,
            vol_lookback=spec.vol_lookback,
            vol_target_annual=spec.vol_target_annual,
            max_leverage=spec.max_leverage,
        )
        curve["cost_basis"] = "ANALYTIC_MODEL"
        curve["family_id"] = family_id
        curve["basket_id"] = basket_id
        curve["basket_kind"] = spec.kind
        curve["basket_rule"] = spec.description
        curve["funding"] = None
        curve["funding_basis"] = FUNDING_NOT_MEASURED
        measured = feed.get(family_id)
        if measured:
            basis = measured.get("funding_basis")
            curve["funding"] = measured.get("funding")
            curve["funding_basis"] = str(basis) if basis else FUNDING_NOT_MEASURED
            for key in (
                "funding_settlements_fed",
                "funding_rows_available",
                "funding_out_of_window",
                "funding_unknown_leg",
                "engine_terminal_balance_usdt",
                "engine_unfunded_balance_usdt",
            ):
                if measured.get(key) is not None:
                    curve[key] = measured[key]
            funded_balance = measured.get("engine_terminal_balance_usdt")
            if funded_balance is not None:
                curve["engine_vs_rule_terminal_delta_usdt"] = (
                    float(funded_balance) - float(curve["rule_terminal_equity_usdt"])
                )
        out[basket_id] = curve
    return out


def published_funding_records(
    metrics: Mapping[str, MetricSet],
    family_funding: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    basket_curves: Mapping[str, Mapping[str, Any]] | None = None,
    engine_measurements: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Per **published curve**, the record its `funding $` cell is read from.

    D-165: the audit set is keyed by the published curve id, so a reader can look
    up the row in front of them instead of guessing which family leg it belongs
    to. Each record names the measurement behind that curve's cell -- the
    measuring basket with its kind and rule, the engine's own row counters and
    both run balances -- plus three fields that say where the number went:

    * ``funding_cell`` is exactly what this row prints (a cost, a measured zero,
      or the named state, never another curve's number);
    * ``funding`` is the measurement's number, ``None`` where none was produced;
    * ``funding_curve`` is the published curve id that carries that number --
      this curve itself, or the measuring basket's own curve where the two
      conventions differ, or ``None`` when no published curve has one.

    ``engine_measurements`` carries the fields of a curve the engine measured on
    its own portfolio run rather than on a benchmark basket (``portfolio_P`` /
    ``portfolio_PE``); a field is ``None`` where the measurement has no such
    quantity, never a zero standing in for an absence.
    """
    feed = family_funding or {}
    baskets = basket_curves or {}
    engine = engine_measurements or {}
    # The family leg each published basket curve belongs to, declared by the same
    # map that decides whose number moved, never inferred from a curve's name.
    family_of_basket = {
        basket_id: family_id for family_id, basket_id in FAMILY_ENGINE_BASKETS.items()
    }
    out: dict[str, dict[str, Any]] = {}
    for name, m in metrics.items():
        state = funding_state(m)
        family_id = name if name in feed else family_of_basket.get(name)
        record: dict[str, Any] = {
            "funding_curve": None,
            "funding_cell": funding_cell(m),
            "funding": None,
            "funding_basis": state,
            "funding_basket": None,
            "funding_basket_kind": None,
            "funding_basket_rule": None,
            "funding_engine_basis": None,
            "funding_settlements_fed": None,
            "funding_rows_available": None,
            "funding_out_of_window": None,
            "funding_unknown_leg": None,
            "engine_terminal_balance_usdt": None,
            "engine_unfunded_balance_usdt": None,
            "engine_vs_rule_terminal_delta_usdt": None,
        }
        if family_id is not None:
            measured = feed.get(family_id, {})
            number = measured.get("funding")
            record.update(
                {
                    # The carrier exists only when the measurement produced a
                    # number: a withheld state points nowhere, exactly like the
                    # cell that publishes it.
                    "funding_curve": (
                        funding_curve_for_family(family_id) if number is not None else None
                    ),
                    "funding": number,
                    "funding_basket": measured.get("funding_basket"),
                    "funding_basket_kind": measured.get("funding_basket_kind"),
                    "funding_basket_rule": measured.get("funding_basket_rule"),
                    "funding_engine_basis": measured.get("funding_engine_basis"),
                    "funding_settlements_fed": measured.get("funding_settlements_fed"),
                    "funding_rows_available": measured.get("funding_rows_available"),
                    "funding_out_of_window": measured.get("funding_out_of_window"),
                    "funding_unknown_leg": measured.get("funding_unknown_leg"),
                    "engine_terminal_balance_usdt": measured.get(
                        "engine_terminal_balance_usdt"
                    ),
                    "engine_unfunded_balance_usdt": measured.get(
                        "engine_unfunded_balance_usdt"
                    ),
                }
            )
        else:
            measured_engine = engine.get(name, {})
            # A curve the engine measured on its own portfolio run carries its
            # number on its own row: there is no other published curve holding it.
            if m.funding_cost is not None:
                record["funding_curve"] = name
                record["funding"] = m.funding_cost
            record.update(
                {
                    key: value
                    for key, value in measured_engine.items()
                    if key in record and value is not None
                }
            )
        # The engine/rule gap is published on the basket's own row, the curve the
        # funded balance is compared against.
        mirror = baskets.get(name)
        if mirror is not None:
            record["engine_vs_rule_terminal_delta_usdt"] = mirror.get(
                "engine_vs_rule_terminal_delta_usdt"
            )
        out[name] = record
    return out


def pair_positions(
    opened: list[Any], closed: list[Any]
) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
    """Pair opens to closes in time order per (instrument, position slot).

    Under NETTING, sequential positions on one instrument+strategy slot reuse
    the same position_id, so id-keyed matching collides. With max one
    concurrent position per slot, chronological pairing is exact; any break
    (close before open, leftover opens) leaves the close unpaired rather than
    misattributed.
    """
    def key(o: Any) -> tuple[str, str]:
        return (str(o.get("instrument_id", "")), str(o.get("position_id", "")))

    opens: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for o in opened:
        if isinstance(o, dict):
            opens.setdefault(key(o), []).append(o)
    closes: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for c in closed:
        if isinstance(c, dict):
            closes.setdefault(key(c), []).append(c)
    pairs: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    for k, olist in opens.items():
        olist.sort(key=lambda o: int(o.get("event_ns", 0) or 0))
        clist = sorted(closes.get(k, []), key=lambda c: int(c.get("event_ns", 0) or 0))
        ci = 0
        for o in olist:
            match: dict[str, Any] | None = None
            o_ns = int(o.get("event_ns", 0) or 0)
            while ci < len(clist):
                c_ns = int(clist[ci].get("event_ns", 0) or 0)
                if c_ns > o_ns:
                    match = clist[ci]
                    ci += 1
                    break
                ci += 1
            pairs.append((o, match))
    return pairs


# --------------------------------------------------------------------------- #
# NX02 — one lifecycle/accounting contract shared by both reporting paths
# --------------------------------------------------------------------------- #
#: Units contract (NX02.R3). Absolute PnL is USDT; returns are dimensionless.
#: The two series are never mixed into one list.
ACCOUNTING_UNITS: dict[str, str] = {
    "pnl": "USDT",
    "return": "dimensionless",
    "price": "USDT",
    "quantity": "base_units",
}

#: Measured on the real engine (`balance_total == initial + sum(realized_pnl) +
#: sum(funding adjustments)`, exactly, with commissions already inside
#: realized_pnl). Pinned as an absolute tolerance in USDT; the replay is
#: Decimal, so the comparison is exact to this bound.
NATIVE_BALANCE_ATOL = Decimal("0.00000001")

# --------------------------------------------------------------------------- #
# #436 — closed-loop residual bounds and the cost-basis vocabulary
# --------------------------------------------------------------------------- #
#: Absolute tolerance (USDT) for the strategy path's closed-loop identity
#: `balance_delta == sum_realized_pnl + open_position_realized_pnl`. Retained
#: from the NX02 measurement, where the residual on the real window was float
#: dust (7.06e-13); anything above this bound is a fail-closed state.
STRATEGY_LOOP_ATOL = 0.01

#: Absolute tolerance (USDT) for the portfolio path's closed-loop identity
#: `balance_delta == sum_realized_pnl - open_entry_commissions`. Retained from
#: the NX02 measurement (`|base_loop| <= 0.02`). A residual outside it is
#: published as `MISMATCH`; it is never absorbed into the equity curve.
PORTFOLIO_LOOP_ATOL = 0.02

#: Cost-basis vocabulary these two series may publish (#436). `MISMATCH` and
#: `UNKNOWN` are fail-closed: the engine did not close, or the closed-loop
#: identity could not be measured at all. `VERIFIED_*` is only ever published
#: when the residual was measured AND is inside tolerance -- the defect this
#: vocabulary closes was a `VERIFIED_ENGINE_FUNDING` asserted on a quantity the
#: artifact never measured.
COST_BASIS_STATES = frozenset(
    {
        "VERIFIED_ENGINE",
        "VERIFIED_ENGINE_FUNDING",
        "FUNDING_UNVERIFIED",
        "ESTIMATED",
        "MISMATCH",
        "UNKNOWN",
    }
)

#: How a returned `equity` list is built, declared so no consumer reads the
#: reconciled curve as the strategy's un-grafted mark path (#436). `equity` is
#: `equity_mtm` plus a uniform per-bar reconciliation adjustment that forces the
#: terminal level onto the engine's `balance_total`.
EQUITY_CONSTRUCTION = (
    "equity = equity_mtm + reconciliation_adjustment_per_bar: the terminal level is "
    "forced onto the engine balance_total by a uniform per-bar adjustment. "
    "equity_mtm is the un-grafted per-bar mark path; the adjustment is declared, "
    "never silent."
)

#: #491 — once the exit leg is marked at the engine's own close price, the graft
#: residual can only be the costs the mark path deliberately excludes
#: (commissions, funding) plus the mark of still-open positions that the engine
#: balance does not carry. The bound is the issue's declared real-tape tolerance:
#: a residual outside it is a named non-reconciling state, never silently smeared.
EQUITY_MTM_IDENTITY_ATOL = 0.05


def _graft_residual_identity(
    *,
    gap: float,
    expected_gap: float,
    commission: float | None,
    funding: float | None,
    open_unrealized: float,
    measured: bool,
) -> dict[str, Any]:
    """Assert that the declared graft absorbs only the excluded costs (#491).

    ``gap`` is the builder's own published terminal gap (its sign convention is
    preserved); ``expected_gap`` is the same gap reconstructed from the costs the
    per-bar mark path omits and the unrealized mark of still-open positions. When
    those differ by more than the declared bound the state says so instead of
    presenting an unexplained residual as cost. Absence is never zero-filled.
    """
    residual = gap - expected_gap
    if not measured:
        state = "NOT_MEASURED"
    elif abs(residual) <= EQUITY_MTM_IDENTITY_ATOL:
        state = "EXPLAINED_BY_EXCLUDED_COSTS"
    else:
        state = "UNEXPLAINED_RESIDUAL"
    return {
        "gap_usdt": gap,
        "expected_gap_usdt": expected_gap,
        "unexplained_usdt": residual,
        "tolerance_usdt": EQUITY_MTM_IDENTITY_ATOL,
        "state": state,
        "terms_usdt": {
            "commission": commission,
            "funding": funding,
            "open_unrealized": open_unrealized,
        },
    }

# --------------------------------------------------------------------------- #
# #445 — the measured closed-loop residual, its terms and its tolerance travel
# into the published receipt
# --------------------------------------------------------------------------- #
#: Published states of one curve's closed-loop reconciliation. `MEASURED` means
#: the engine balance was read and the residual is that measurement; the receipt
#: then carries the number, the terms it came from and the bound it was compared
#: against. `NOT_MEASURED` means the balance could not be read at all, so the
#: receipt carries NO residual (null) and names why: a number under
#: `NOT_MEASURED` would be a fabricated measurement, not an absent one.
RECONCILIATION_STATES = frozenset({"MEASURED", "NOT_MEASURED"})

#: The two closed-loop identities in this module, as published tokens. They
#: differ only in the term attributed to still-open positions: the strategy path
#: subtracts the account's own `realized_pnl` for open positions, the portfolio
#: path adds its taker-fee estimate of their entry commissions. A published
#: residual names the identity it was computed under, so a consumer recomputes
#: it under the same one instead of guessing (#445). Which identity is the right
#: one for a given engine state is NOT decided here -- both are measured.
IDENTITY_STRATEGY_OPEN_REALIZED = "STRATEGY_OPEN_REALIZED_PNL"
IDENTITY_PORTFOLIO_OPEN_ENTRY_COMMISSIONS = "PORTFOLIO_OPEN_ENTRY_COMMISSIONS"

#: The `closed_loop_terms` key that names each identity above.
IDENTITY_OPEN_TERM_KEYS = {
    IDENTITY_STRATEGY_OPEN_REALIZED: "open_position_realized_pnl",
    IDENTITY_PORTFOLIO_OPEN_ENTRY_COMMISSIONS: "open_entry_commissions_of_open_positions",
}

#: Named reasons a published reconciliation carries (#445). A consumer switches
#: on the token; a reason is never inferred from an absence.
REASON_RESIDUAL_WITHIN_TOLERANCE = "ENGINE_SERIES_RESIDUAL_WITHIN_TOLERANCE"
REASON_RESIDUAL_OUTSIDE_TOLERANCE = "ENGINE_SERIES_RESIDUAL_OUTSIDE_TOLERANCE"
REASON_RESIDUAL_NOT_FINITE = "RECONCILIATION_RESIDUAL_NOT_FINITE"
REASON_BALANCE_UNMEASURABLE = "ENGINE_BALANCE_UNMEASURABLE"
REASON_OPEN_TERMS_UNMEASURABLE = "OPEN_POSITION_TERMS_UNMEASURABLE"
REASON_TERMS_NOT_PUBLISHED = "RECONCILIATION_TERMS_NOT_PUBLISHED"
REASON_IDENTITIES_DIVERGED = "RECONCILIATION_IDENTITIES_DIVERGED"

RECONCILIATION_REASONS = frozenset(
    {
        REASON_RESIDUAL_WITHIN_TOLERANCE,
        REASON_RESIDUAL_OUTSIDE_TOLERANCE,
        REASON_RESIDUAL_NOT_FINITE,
        REASON_BALANCE_UNMEASURABLE,
        REASON_OPEN_TERMS_UNMEASURABLE,
        REASON_TERMS_NOT_PUBLISHED,
        REASON_IDENTITIES_DIVERGED,
    }
)

#: Where the open-position terms of the second identity are read from.
OPEN_TERMS_SOURCE = "ENGINE_ACCOUNT_OPEN_POSITIONS"


def parse_money(value: Any) -> Decimal | None:
    """Parse an engine money/quantity rendering; ``None`` on anything unparsable.

    A malformed, missing or non-finite value is *never* converted to zero
    (NX02.R3): absence stays absent and the caller reports it.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = Decimal(str(value))
        return parsed if parsed.is_finite() else None
    text = str(value).strip().split()
    if not text:
        return None
    try:
        parsed = Decimal(text[0])
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _magnitude(value: Any) -> float | None:
    """A published magnitude, or None. A non-finite one stays ABSENT (#445).

    `None` in, `None` out; `inf`/`nan` are not measurements of anything and may
    not be published as one.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        as_float = float(value)
        return as_float if math.isfinite(as_float) else None
    return None


def closed_loop_identity(terms: Mapping[str, Any]) -> str | None:
    """Which closed-loop identity a `closed_loop_terms` block was written under.

    Read off the block's own attributed term, never supplied by a caller: the
    residual can then only be recomputed under the identity that produced it
    (#445). `None` when the block attributes no open-position term at all.
    """
    present = [ident for ident, key in IDENTITY_OPEN_TERM_KEYS.items() if key in terms]
    if len(present) > 1:
        raise ValueError(
            f"ambiguous closed-loop terms: {sorted(present)} both attribute an "
            "open-position term; one block names one identity"
        )
    return present[0] if present else None


def recompute_closed_loop_residual(terms: Mapping[str, Any]) -> float | None:
    """Rebuild a published residual from the receipt's own `closed_loop_terms`.

    The residual is a function of the magnitudes bound beside it
    (`balance_delta - sum_realized +/- the identity's open-position term`), so a
    reader recomputes it instead of taking it on trust (#445). `None` when the
    block does not carry what its identity needs.
    """
    identity = closed_loop_identity(terms)
    if identity is None:
        return None
    balance_delta = _magnitude(terms.get("balance_delta"))
    sum_realized = _magnitude(terms.get("sum_realized_pnl"))
    open_term = _magnitude(terms.get(IDENTITY_OPEN_TERM_KEYS[identity]))
    if balance_delta is None or sum_realized is None or open_term is None:
        return None
    if identity == IDENTITY_PORTFOLIO_OPEN_ENTRY_COMMISSIONS:
        return balance_delta - sum_realized + open_term
    return balance_delta - sum_realized - open_term


def open_position_identity_terms(engine_result: dict[str, Any], *, taker_fee: float) -> dict[str, Any]:
    """The two terms the closed-loop identities attribute to open positions (#445).

    Measured off the engine account's own open positions: the `realized_pnl` the
    account reports for them (what it has booked so far) and the taker-fee
    estimate of their entry commissions. Both are published so either identity
    can be evaluated on one engine balance. An account that does not expose its
    positions, or one open position whose quantity/price is unparsable, leaves
    the pair ABSENT (`measurable=False`): never zero-filled.
    """
    account = engine_result.get("account", {}) if isinstance(engine_result, dict) else {}
    if not isinstance(account, dict) or "positions" not in account:
        return {"measurable": False, "named_reason": REASON_OPEN_TERMS_UNMEASURABLE}
    open_realized = 0.0
    entry_comm = 0.0
    for pos in account.get("positions", []):
        if not isinstance(pos, dict) or pos.get("is_closed"):
            continue
        realized = parse_money(pos.get("realized_pnl"))
        qty = parse_money(pos.get("quantity"))
        px = parse_money(pos.get("average_open_price"))
        if realized is None or qty is None or px is None:
            return {"measurable": False, "named_reason": REASON_OPEN_TERMS_UNMEASURABLE}
        open_realized += float(realized)
        entry_comm += abs(float(qty)) * float(px) * float(taker_fee)
    return {
        "measurable": True,
        "named_reason": None,
        "open_position_realized_pnl": open_realized,
        "open_entry_commissions_of_open_positions": entry_comm,
    }


def cross_identity_reconciliation(
    engine_result: dict[str, Any],
    *,
    taker_fee: float,
    capital: float,
    balance_total: float,
    balance_measured: bool,
    sum_realized: float,
    governing_identity: str,
) -> dict[str, Any]:
    """The OTHER closed-loop identity, measured on this same engine balance (#445).

    The two identities differ only in the open-position term, so on one engine
    balance their residuals differ by exactly those two terms. Measuring both
    publishes which one the fail-closed state rests on and how far the other is
    from it -- the measurement a reader needs to see whether a `MISMATCH` is
    engine-side or identity-side. It is a published cross-check only:
    `cost_basis`, `loop_err` and every gate still derive from the governing
    identity, and this block is never an input to them.
    """
    identity = (
        IDENTITY_STRATEGY_OPEN_REALIZED
        if governing_identity == IDENTITY_PORTFOLIO_OPEN_ENTRY_COMMISSIONS
        else IDENTITY_PORTFOLIO_OPEN_ENTRY_COMMISSIONS
    )
    out: dict[str, Any] = {
        "identity": identity,
        "governing": False,
        "terms_source": OPEN_TERMS_SOURCE,
        "balance_delta": (balance_total - capital) if balance_measured else None,
        "sum_realized_pnl": sum_realized if balance_measured else None,
        "residual_usdt": None,
    }
    terms = open_position_identity_terms(engine_result, taker_fee=taker_fee)
    out.update(terms)
    if not terms["measurable"]:
        # The other identity's own term is absent: nothing is computed for it.
        out["named_reason"] = REASON_OPEN_TERMS_UNMEASURABLE
        return out
    if not balance_measured:
        out["named_reason"] = REASON_BALANCE_UNMEASURABLE
        return out
    out["named_reason"] = None
    if identity == IDENTITY_STRATEGY_OPEN_REALIZED:
        residual = (balance_total - capital) - sum_realized - terms["open_position_realized_pnl"]
    else:
        residual = (balance_total - capital) - sum_realized + terms[
            "open_entry_commissions_of_open_positions"
        ]
    out["residual_usdt"] = residual
    return out


def reconcile_closed_loop_identities(
    reconciliation: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Both closed-loop identities on ONE engine balance, from a published receipt.

    This is a measurement that can come out either way, never a constant: `PASS`
    when both identities are evaluable from what the receipt publishes on one
    balance and their residuals agree within the bound that curve declares,
    otherwise `FAIL` with a named reason -- terms the receipt does not carry, a
    non-finite measurement, or a real divergence, which is reported with the
    measured difference rather than absorbed. Callers must not read `FAIL` as
    "the residual is wrong": it says the two identity conventions do not agree on
    this balance, which is exactly the open question #445 publishes and does not
    decide.
    """
    report: dict[str, Any] = {
        "identities": {},
        "difference_usdt": None,
        "comparison_tolerance_usdt": None,
        "comparison_bound_source": "the curve's declared engine_series_tolerance_usdt",
        "reason": None,
    }
    recon = reconciliation or {}
    terms = recon.get("closed_loop_terms") or {}
    cross = recon.get("cross_identity") or {}
    governing_identity = closed_loop_identity(terms) if isinstance(terms, Mapping) else None
    tolerance = _magnitude(recon.get("engine_series_tolerance_usdt"))
    report["comparison_tolerance_usdt"] = tolerance
    stored = _magnitude(recon.get("engine_series_residual_usdt"))
    recomputed = recompute_closed_loop_residual(terms) if isinstance(terms, Mapping) else None
    report["identities"][governing_identity or "UNNAMED_IDENTITY"] = {
        "residual_usdt": stored,
        "recomputed_from_terms_usdt": recomputed,
        "governing": True,
    }
    cross_residual = _magnitude(cross.get("residual_usdt")) if isinstance(cross, Mapping) else None
    # The other identity's own terms, rebuilt under ITS identity so its residual
    # is recomputed the same way the governing one is.
    cross_identity = str(cross.get("identity") or "")
    cross_terms: dict[str, Any] = {
        "balance_delta": cross.get("balance_delta"),
        "sum_realized_pnl": cross.get("sum_realized_pnl"),
    }
    cross_term_key = IDENTITY_OPEN_TERM_KEYS.get(cross_identity)
    if cross_term_key:
        cross_terms[cross_term_key] = cross.get(cross_term_key)
    report["identities"][cross_identity or "UNNAMED_IDENTITY"] = {
        "residual_usdt": cross_residual,
        "recomputed_from_terms_usdt": recompute_closed_loop_residual(cross_terms)
        if isinstance(cross, Mapping) and cross.get("measurable")
        else None,
        "governing": False,
    }
    first = recomputed if recomputed is not None else stored
    if first is None or cross_residual is None:
        # Not evaluable: the receipt does not carry both identities' terms on
        # this balance. Named, never reported as agreement.
        report["status"] = "FAIL"
        report["reason"] = REASON_TERMS_NOT_PUBLISHED
        return report
    difference = first - cross_residual
    report["difference_usdt"] = difference
    agrees = tolerance is not None and abs(difference) <= tolerance
    report["status"] = "PASS" if agrees else "FAIL"
    report["reason"] = None if agrees else REASON_IDENTITIES_DIVERGED
    return report


def published_cost_reconciliation(
    reconciliation: Mapping[str, Any] | None,
    *,
    cost_basis: str,
) -> dict[str, Any] | None:
    """The receipt-side view of one curve's closed-loop reconciliation (#445).

    `None` in, `None` out: a curve that measured no reconciliation (the analytic
    legs) publishes none at all, never an empty block that reads as a zero. A
    measured reconciliation publishes the signed residual, the terms it was
    computed from, the tolerance it was compared against, the identity it was
    computed under and the check of the other identity on the same balance. A
    curve whose engine balance could not be read publishes NO residual (null)
    and names why -- the `MISMATCH`/`UNKNOWN` decision alone never claimed a
    number it did not measure.
    """
    if reconciliation is None:
        return None
    recon = reconciliation
    measured = bool(recon.get("engine_series_residual_measured"))
    terms = recon.get("closed_loop_terms") or {}
    identity = closed_loop_identity(terms) if isinstance(terms, Mapping) else None
    if identity is None:
        identity = recon.get("identity")
    tolerance = _magnitude(recon.get("engine_series_tolerance_usdt"))
    residual = _magnitude(recon.get("engine_series_residual_usdt")) if measured else None
    # An unmeasured balance publishes neither the residual NOR its magnitude: the
    # series computes both against the substituted capital, and those numbers are
    # arithmetic on a quantity that was never read (#445).
    abs_residual = _magnitude(recon.get("engine_series_residual_abs_usdt")) if measured else None
    if residual is not None and (abs_residual is None or abs_residual != abs(residual)):
        abs_residual = abs(residual)
    ok = recon.get("engine_series_ok", recon.get("closed_loop_ok"))
    if measured and residual is not None and ok is None and tolerance is not None:
        # The state is derivable from the two magnitudes the receipt publishes
        # beside it; deriving it here keeps the field present on every path.
        ok = abs(residual) <= tolerance
    ok = bool(ok) if measured and residual is not None and ok is not None else None
    # Only a measured reconciliation is recomputable: an unmeasured one carries
    # no terms, and its arithmetic ran on a balance that was never read.
    recomputed = (
        recompute_closed_loop_residual(terms)
        if measured and isinstance(terms, Mapping)
        else None
    )
    if not measured:
        named_reason = REASON_BALANCE_UNMEASURABLE
    elif residual is None:
        named_reason = REASON_RESIDUAL_NOT_FINITE
    elif ok is None:
        named_reason = REASON_TERMS_NOT_PUBLISHED
    elif ok:
        named_reason = REASON_RESIDUAL_WITHIN_TOLERANCE
    else:
        named_reason = REASON_RESIDUAL_OUTSIDE_TOLERANCE
    published: dict[str, Any] = {
        "cost_basis": cost_basis,
        "state": "MEASURED" if measured else "NOT_MEASURED",
        # Which identity the residual above was computed under.
        "identity": identity,
        "engine_series_residual_usdt": residual,
        "engine_series_residual_abs_usdt": abs_residual,
        "engine_series_residual_measured": measured,
        "engine_series_ok": ok,
        "engine_series_tolerance_usdt": tolerance,
        # The magnitudes the residual is a function of, or None where they were
        # not measured -- never a zero standing in for one.
        "closed_loop_terms": dict(terms) if measured and isinstance(terms, Mapping) and terms else None,
        # Recomputable from the terms above: published so a reader can check it.
        "recomputed_residual_usdt": recomputed,
        "recompute_matches_stored": bool(
            recomputed is not None and residual is not None and recomputed == residual
        )
        if measured
        else None,
        "named_reason": named_reason,
        # #491: the graft's declared residual identity travels with the curve. Before this
        # the builder computed it and the receipt silently dropped it, so a reader never saw
        # whether the residual was the costs the mark path excludes or something else.
        # §14 of the issue: a residual not explainable by fees + funding publishes a NAMED
        # non-reconciling state (`UNEXPLAINED_RESIDUAL` + `unexplained_usdt`) instead of a
        # graft that presents it as cost.
        "exit_leg_identity": recon.get("exit_leg_identity"),
        # The other identity on the same engine balance (see
        # `cross_identity_reconciliation`); non-governing.
        "cross_identity": recon.get("cross_identity"),
    }
    published["identities_check"] = reconcile_closed_loop_identities(published)
    return published


@dataclass(frozen=True)
class CampaignAccounting:
    """One accounting view for a run: absolute USDT and dimensionless returns.

    ``realized_pnl_usdt`` holds completed campaigns only; open campaigns are
    carried at the cutoff bar-end mark and reported separately as
    ``open_risk_usdt`` (NX02.R3/R5). Anything the current engine semantics do not
    support is named in ``unsupported`` instead of being quietly folded into the
    totals (NX02.R2).
    """

    cutoff_ns: int
    closed_campaigns: int
    open_campaigns: int
    realized_pnl_usdt: tuple[float, ...]
    campaign_returns: tuple[float, ...]
    open_risk_usdt: float | None
    open_risk_returns: tuple[float, ...]
    orphan_closes: tuple[str, ...]
    unparsable: tuple[str, ...]
    unsupported: tuple[str, ...]
    units: dict[str, str]

    @property
    def trade_count(self) -> int:
        """Completed campaigns. Open risk is reported, never counted as a trade."""
        return self.closed_campaigns

    @property
    def supported(self) -> bool:
        return not self.unsupported

    def as_dict(self) -> dict[str, Any]:
        return {
            "cutoff_ns": self.cutoff_ns,
            "closed_campaigns": self.closed_campaigns,
            "open_campaigns": self.open_campaigns,
            "trade_count": self.trade_count,
            "realized_pnl_usdt": list(self.realized_pnl_usdt),
            "campaign_returns": list(self.campaign_returns),
            "open_risk_usdt": self.open_risk_usdt,
            "open_risk_returns": list(self.open_risk_returns),
            "orphan_closes": list(self.orphan_closes),
            "unparsable": list(self.unparsable),
            "unsupported": list(self.unsupported),
            "units": dict(self.units),
        }


def _pair_key(o: Any) -> tuple[str, str]:
    return (str(o.get("instrument_id", "")), str(o.get("position_id", "")))


def campaign_lifecycle_checks(
    opened: list[dict[str, Any]],
    closed: list[dict[str, Any]],
    pairs: list[tuple[dict[str, Any], dict[str, Any] | None]],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Derive partial/scale-in/reversal/orphan handling from engine semantics.

    Returns ``(unsupported, orphan_closes)``. Every case the current engine
    semantics cannot express is *named* rather than silently mis-accounted:

    * ``ORPHAN_CLOSE`` — a close record that no open could consume;
    * ``DUPLICATE_CLOSE_REUSE`` — one close would close more than one campaign;
    * ``SCALE_IN_OR_PARTIAL_REDUCTION`` — several opens on one slot before the
      close (netting reuses the slot, so per-campaign attribution is unsupported);
    * ``CLOSE_WITHOUT_OPEN_IN_WINDOW`` — the close precedes every open on its slot.
    """
    unsupported: list[str] = []
    consumed: dict[int, int] = {}
    for _open_rec, close_rec in pairs:
        if close_rec is None:
            continue
        marker = id(close_rec)
        consumed[marker] = consumed.get(marker, 0) + 1
    orphans: list[str] = []
    for close_rec in closed:
        if id(close_rec) not in consumed:
            orphans.append(str(close_rec.get("position_id", "")))
    for times in consumed.values():
        if times > 1:
            unsupported.append(
                f"DUPLICATE_CLOSE_REUSE: one close consumed by {times} campaigns"
            )
            break

    opens_per_slot: dict[tuple[str, str], int] = {}
    for open_rec in opened:
        key = _pair_key(open_rec)
        opens_per_slot[key] = opens_per_slot.get(key, 0) + 1
    for key, count in sorted(opens_per_slot.items()):
        if count > 1:
            closes_in_slot = [c for c in closed if _pair_key(c) == key]
            unsupported.append(
                f"SCALE_IN_OR_PARTIAL_REDUCTION: {key[1]} reused by {count} opens "
                f"with {len(closes_in_slot)} close(s)"
            )
    return tuple(sorted(unsupported)), tuple(sorted(orphans))


def campaign_accounting(
    opened: Sequence[Any],
    closed: Sequence[Any],
    *,
    bars: Sequence[BarView],
    cutoff_ns: int,
    capital: float = CAPITAL_DEFAULT,
) -> CampaignAccounting:
    """Consolidated lifecycle/accounting view over opens and closes.

    Uses the existing chronological :func:`pair_positions` contract (instrument +
    position slot + event order), so a reused ``position_id`` under NETTING can
    never mis-attribute a close. Realized PnL is absolute USDT; returns are
    per-campaign and dimensionless. Open positions are marked at the last bar end
    at or before ``cutoff_ns`` — no artificial close and no future price.
    """
    open_dicts = [o for o in opened if isinstance(o, dict)]
    close_dicts = [c for c in closed if isinstance(c, dict)]
    pairs = pair_positions(list(open_dicts), list(close_dicts))
    unsupported, orphans = campaign_lifecycle_checks(open_dicts, close_dicts, pairs)

    cutoff_bars = [b for b in bars if b.end_ns <= cutoff_ns]
    mark_price = cutoff_bars[-1].close if cutoff_bars else None
    if mark_price is None and any(close is None for _, close in pairs):
        unsupported = tuple(
            sorted({*unsupported, "NO_BAR_AT_OR_BEFORE_CUTOFF_FOR_OPEN_MTM"})
        )

    realized: list[float] = []
    returns: list[float] = []
    open_risk_usdt = 0.0
    open_returns: list[float] = []
    unparsable: list[str] = []
    open_campaigns = 0

    for open_rec, close_rec in pairs:
        entry_px = parse_money(open_rec.get("avg_px_open"))
        qty = parse_money(open_rec.get("quantity"))
        if close_rec is None:
            open_campaigns += 1
            if mark_price is None or entry_px is None or qty is None:
                unparsable.append(f"OPEN_MTM_UNMEASURED:{open_rec.get('position_id', '')}")
                continue
            direction = 1.0 if str(open_rec.get("side", "")).upper() in ("BUY", "LONG") else -1.0
            pnl = (float(mark_price) - float(entry_px)) * float(qty) * direction
            open_risk_usdt += pnl
            notional = abs(float(qty)) * float(entry_px)
            if notional > 0.0:
                open_returns.append(pnl / notional)
            continue

        pnl_value = parse_money(close_rec.get("realized_pnl"))
        if pnl_value is None:
            # A parse failure is absence, not a zero return.
            unparsable.append(f"CLOSE_PNL_UNPARSABLE:{close_rec.get('position_id', '')}")
            continue
        realized.append(float(pnl_value))
        if entry_px is None or qty is None:
            unparsable.append(f"ENTRY_UNPARSABLE:{open_rec.get('position_id', '')}")
            continue
        notional = abs(float(qty)) * float(entry_px)
        if notional <= 0.0:
            unparsable.append(f"ENTRY_NOTIONAL_ZERO:{open_rec.get('position_id', '')}")
            continue
        returns.append(float(pnl_value) / notional)

    return CampaignAccounting(
        cutoff_ns=cutoff_ns,
        closed_campaigns=len(realized),
        open_campaigns=open_campaigns,
        realized_pnl_usdt=tuple(realized),
        campaign_returns=tuple(returns),
        open_risk_usdt=(open_risk_usdt if mark_price is not None else None),
        open_risk_returns=tuple(open_returns),
        orphan_closes=orphans,
        unparsable=tuple(sorted(unparsable)),
        unsupported=tuple(unsupported),
        units=dict(ACCOUNTING_UNITS),
    )


def reconcile_native_account(
    account: dict[str, Any],
    *,
    accounting: CampaignAccounting,
    initial_balance: float = CAPITAL_DEFAULT,
    atol: Decimal = NATIVE_BALANCE_ATOL,
) -> dict[str, Any]:
    """Reconcile native equity against an independent replay of its own parts.

    Two independent comparisons, each reported separately:

    * ``balance`` — native ``balance_total`` versus
      ``initial + sum(realized_pnl) + sum(funding adjustments)`` over every
      native position (the measured engine identity);
    * ``report_path`` — the reporting path's completed-campaign PnL versus the
      native realized PnL of the closed native positions, pair-wise.

    ``status`` is ``DIVERGED`` when either comparison fails, and every failing
    component is named. Nothing is coerced to MATCHED.
    """
    native_balance = parse_money(account.get("balance_total"))
    positions = account.get("positions") or []
    total_realized = Decimal(0)
    total_adjustments = Decimal(0)
    components_unparsable: list[str] = []
    closed_native: list[Decimal] = []
    for index, position in enumerate(positions):
        realized = parse_money(position.get("realized_pnl"))
        if realized is None:
            components_unparsable.append(f"position[{index}].realized_pnl")
        else:
            total_realized += realized
        for adj_index, adjustment in enumerate(position.get("adjustments") or []):
            raw = adjustment.get("value", adjustment.get("amount")) if isinstance(
                adjustment, dict
            ) else adjustment
            parsed = parse_money(raw)
            if parsed is None:
                components_unparsable.append(f"position[{index}].adjustments[{adj_index}]")
            else:
                total_adjustments += parsed
        if position.get("is_closed"):
            if realized is None:
                closed_native.append(Decimal("NaN"))
            else:
                closed_native.append(realized)

    reasons: list[str] = []
    replay_balance: Decimal | None = None
    balance_delta: Decimal | None = None
    if native_balance is None:
        reasons.append("NATIVE_BALANCE_UNPARSABLE")
    elif components_unparsable:
        reasons.append("COMPONENT_UNPARSABLE:" + ",".join(sorted(components_unparsable)))
    else:
        replay_balance = Decimal(str(initial_balance)) + total_realized + total_adjustments
        balance_delta = native_balance - replay_balance
        if abs(balance_delta) > atol:
            reasons.append(
                f"BALANCE_REPLAY_MISMATCH: native {native_balance} vs replay {replay_balance}"
            )

    report_pnl = sum(Decimal(str(v)) for v in accounting.realized_pnl_usdt)
    report_delta: Decimal | None = None
    if any(v.is_nan() for v in closed_native):
        reasons.append("CLOSED_POSITION_PNL_UNPARSABLE")
    elif len(closed_native) != accounting.closed_campaigns:
        reasons.append(
            "CLOSED_CAMPAIGN_COUNT_MISMATCH: "
            f"native {len(closed_native)} vs report {accounting.closed_campaigns}"
        )
    else:
        native_closed_total = sum(closed_native, Decimal(0))
        report_delta = report_pnl - native_closed_total
        if abs(report_delta) > atol:
            reasons.append(
                f"REPORT_PATH_MISMATCH: report {report_pnl} vs native closed {native_closed_total}"
            )

    return {
        "status": "MATCHED" if not reasons else "DIVERGED",
        "reasons": sorted(reasons),
        "native_balance_usdt": None if native_balance is None else str(native_balance),
        "replay_balance_usdt": None if replay_balance is None else str(replay_balance),
        "balance_delta_usdt": None if balance_delta is None else str(balance_delta),
        "atol_usdt": str(atol),
        "components": {
            "initial_balance_usdt": str(initial_balance),
            "sum_realized_pnl_usdt": str(total_realized),
            "sum_funding_adjustments_usdt": str(total_adjustments),
            "native_positions": len(positions),
        },
        "report_path": {
            "closed_campaigns": accounting.closed_campaigns,
            "report_pnl_usdt": str(report_pnl),
            "delta_usdt": None if report_delta is None else str(report_delta),
        },
        "units": dict(ACCOUNTING_UNITS),
    }


def strategy_series_from_engine(
    engine_result: dict[str, Any],
    bars: Sequence[BarView],
    capital: float = CAPITAL_DEFAULT,
    taker_fee: float = TAKER_FEE_DEFAULT,
) -> dict[str, Any]:
    """Per-bar strategy equity from engine fills + closes. No double-counted costs.

    Commission per fill is estimated as notional * taker_fee and reconciled
    against the engine-reported commission total. Turnover counts the entry AND
    the exit leg (#397), so the published turnover, the commission estimate and
    the capacity breakeven derived from them all read in the one declared fee
    convention. Funding is MISSING (engine is not fed funding events in this
    scope).
    """
    closes = [b.close for b in bars]
    n = len(closes)
    end_ns = [b.end_ns for b in bars]
    idx_of = {ns: i for i, ns in enumerate(end_ns)}

    opened = engine_result.get("opened_positions", [])
    closed = engine_result.get("closed_positions", [])
    pairs = pair_positions(
        [p for p in opened if isinstance(p, dict)],
        [c for c in closed if isinstance(c, dict)],
    )
    # NX02.R3: a completed campaign and an open position are different things.
    # `n_trades` historically counted every open event; the split is now explicit
    # so a reader cannot mistake open risk for a completed trade.
    n_completed_campaigns = sum(1 for _, c in pairs if c is not None)
    n_open_positions = sum(1 for _, c in pairs if c is None)

    reported_comm_total = 0.0
    for pos in engine_result.get("account", {}).get("positions", []):
        for c in pos.get("commissions", []) or []:
            try:
                reported_comm_total += float(str(c).split()[0])
            except (ValueError, TypeError):
                pass

    fills = [
        o
        for o in engine_result.get("account", {}).get("orders", [])
        if o.get("status") == "FILLED" and o.get("average_price") is not None
    ]
    est_comm_total = sum(
        float(o.get("filled_qty") or 0) * float(o.get("average_price") or 0) * taker_fee
        for o in fills
    )
    # #397: same fill set as the commission estimate, so turnover and commission
    # cannot drift into two different fee conventions.
    fill_notional_total = sum(
        abs(float(o.get("filled_qty") or 0)) * float(o.get("average_price") or 0)
        for o in fills
    )
    # Closed-loop verification: engine realized_pnl sums exactly to the balance
    # delta, so the engine charge is complete and counted once. The per-fill
    # estimate is then full attribution under the same fee model (the account's
    # commission list retains only the last position and is partial by itself).
    sum_realized = 0.0
    for c in closed:
        if isinstance(c, dict) and c.get("realized_pnl"):
            try:
                sum_realized += float(str(c["realized_pnl"]).split()[0])
            except (ValueError, TypeError):
                pass
    # The account reports realized_pnl for OPEN positions as well (the commission
    # paid so far). Leaving it out made the closed-loop error look like an
    # unexplained residual on a real run; it is attributed here instead.
    open_realized = 0.0
    for pos in engine_result.get("account", {}).get("positions", []):
        if pos.get("is_closed"):
            continue
        parsed = parse_money(pos.get("realized_pnl"))
        if parsed is not None:
            open_realized += float(parsed)
    balance_measured = True
    try:
        balance_total = float(str(engine_result["account"]["balance_total"]).split()[0])
        balance_measured = math.isfinite(balance_total)
    except (KeyError, ValueError, TypeError, AttributeError):
        balance_total = capital
        balance_measured = False
    if not balance_measured:
        balance_total = capital
    # #436: the closed-loop residual is MEASURED (signed) and published, never
    # assumed away. The level is later forced onto `balance_total` by a declared
    # per-bar adjustment, so without this number the curve can never contradict
    # the engine's own accounting.
    engine_series_residual = (balance_total - capital) - sum_realized - open_realized
    loop_err = abs(engine_series_residual)
    loop_ok = balance_measured and loop_err <= STRATEGY_LOOP_ATOL
    # #445: the OTHER closed-loop identity, measured on this same engine balance,
    # so a reader sees both conventions side by side instead of only the one the
    # fail-closed state rests on. Non-governing: `cost_basis` above and every
    # gate derive from `engine_series_residual` only.
    cross_identity = cross_identity_reconciliation(
        engine_result,
        taker_fee=taker_fee,
        capital=capital,
        balance_total=balance_total,
        balance_measured=balance_measured,
        sum_realized=sum_realized,
        governing_identity=IDENTITY_STRATEGY_OPEN_REALIZED,
    )

    if not balance_measured:
        # Unmeasurable is not verified: fail closed instead of guessing.
        cost_basis = "UNKNOWN"
        rel_err = math.inf
    elif not loop_ok:
        # The engine did not close. Fail closed on the measured residual; the
        # commission cross-check may not upgrade this state.
        cost_basis = "MISMATCH"
        rel_err = (
            abs(est_comm_total - reported_comm_total) / est_comm_total
            if est_comm_total
            else math.inf
        )
    elif est_comm_total > 0:
        cost_basis = "VERIFIED_ENGINE"
        rel_err = abs(est_comm_total - reported_comm_total) / est_comm_total if est_comm_total else math.inf
    elif reported_comm_total > 0:
        rel_err = abs(est_comm_total - reported_comm_total) / reported_comm_total
        cost_basis = "VERIFIED_ENGINE" if rel_err <= 0.02 else "ESTIMATED"
    else:
        rel_err = math.inf
        cost_basis = "ESTIMATED" if est_comm_total == 0 else "MISMATCH"

    # Position intervals: open bar -> close bar with signed exposure.
    events: dict[int, list[tuple[str, float, float]]] = {}
    engine_priced_exits = 0
    bar_close_proxy_exits = 0
    for pos, clo in pairs:
        try:
            o_idx = idx_of.get(int(pos.get("event_ns", -1)), None)
        except (ValueError, TypeError):
            o_idx = None
        if o_idx is None:
            continue
        side = str(pos.get("side", "")).upper()
        try:
            q = float(pos.get("quantity") or 0)
            px = float(pos.get("avg_px_open") or 0)
        except (ValueError, TypeError):
            continue
        d = 1.0 if side in ("BUY", "LONG") else -1.0
        events.setdefault(o_idx, []).append(("open", d * q, px))
        if isinstance(clo, dict):
            try:
                c_idx = idx_of.get(int(clo.get("event_ns", -1)), None)
            except (ValueError, TypeError):
                c_idx = None
            if c_idx is not None and c_idx > o_idx:
                # #397: the exit leg carries turnover on its own notional. The
                # engine's recorded close price is used when present; a missing
                # close price falls back to the bar-close fill proxy and is
                # counted in the reconciliation (never a free exit).
                exit_px = parse_money(clo.get("avg_px_close"))
                if exit_px is None:
                    exit_price = float(closes[c_idx])
                    bar_close_proxy_exits += 1
                else:
                    exit_price = float(exit_px)
                    engine_priced_exits += 1
                events.setdefault(c_idx, []).append(("close", d * q, exit_price))
    # Authoritative realized PnL per close bar (engine net of its own fees).
    realized_by_bar: dict[int, float] = {}
    for _pos, clo in pairs:
        if not isinstance(clo, dict) or not clo.get("realized_pnl"):
            continue
        try:
            c_idx = idx_of.get(int(clo.get("event_ns", -1)), None)
            val = float(str(clo["realized_pnl"]).split()[0])
        except (ValueError, TypeError):
            continue
        if c_idx is not None:
            realized_by_bar[c_idx] = realized_by_bar.get(c_idx, 0.0) + val
    equity = [capital]
    exposure = [0.0]
    qty = 0.0
    avg = 0.0
    #: #472: this series carries ONE position at a time, so the measured exposure
    #: is that single leg's weight. The leg is named from the engine's own position
    #: record: a series that cannot name the leg it held publishes no weights, and
    #: the metric names the absence instead of inventing a leg label.
    strategy_leg_id = next(
        (
            str(p.get("instrument_id"))
            for p in opened
            if isinstance(p, dict) and p.get("instrument_id")
        ),
        None,
    )
    turnover = 0.0
    entry_turnover = 0.0
    exit_turnover = 0.0
    exit_fills = 0
    n_trades = 0
    for i in range(1, n):
        exit_step = 0.0
        for kind, qd, px in events.get(i, []):
            if kind == "open":
                qty = qd
                avg = px
                turnover += abs(qd) * px / capital
                entry_turnover += abs(qd) * px / capital
                n_trades += 1
            else:
                # #491: book the exit segment (last bar close -> measured exit
                # price) BEFORE the position becomes flat. `px` is the engine's
                # `avg_px_close`, or the documented bar-close proxy already
                # counted in `bar_close_proxy_exits`; it is measured, never
                # imputed. Before this the close set `qty = 0` first and the
                # whole exit leg was dropped from `equity_mtm`.
                exit_step += qd * (px - closes[i - 1])
                qty = 0.0
                # #397: the exit leg pays the same fee on its own notional and
                # is therefore turnover, not a free close.
                turnover += abs(qd) * px / capital
                exit_turnover += abs(qd) * px / capital
                exit_fills += 1
        mtm = qty * (closes[i] - avg) if qty != 0.0 else 0.0
        equity.append(equity[-1] + mtm + exit_step)
        exposure.append(abs(qty * closes[i]) / equity[-2] if equity[-2] > 0 else 0.0)
    # Replace level with authoritative accounting: engine balance delta is the
    # net truth; distribute the residual (commissions + rounding) uniformly so
    # the terminal equity reconciles exactly and costs are counted once. That
    # uniform distribution is a GRAFT -- `adj[-1] == balance_total` holds by
    # construction for any residual -- so it is declared (#436) and the measured
    # residual it absorbs is published beside it.
    equity_mtm = list(equity)
    # `balance_total` / `balance_measured` are the same measured values the
    # closed-loop block above computed; they are not re-parsed (a second parse
    # could not disagree, and a differing one would have been a second truth).
    drift = (balance_total - equity[-1]) / max(1, n - 1)
    adj = [equity[0]]
    for i in range(1, n):
        adj.append(adj[-1] + (equity[i] - equity[i - 1]) + drift)
    commission = capital + sum(
        realized_by_bar.get(i, 0.0) for i in range(n)
    ) - balance_total
    # commission here is the engine-implied total cost drag; prefer the full
    # fill-level attribution (reported list is last-position partial).
    commission_used = est_comm_total if cost_basis == "VERIFIED_ENGINE" else (
        reported_comm_total if reported_comm_total > 0 else est_comm_total
    )
    # #491: the still-open position's own unrealized mark is carried by
    # `equity_mtm` but not by the engine balance, so it belongs in the expected
    # gap. It is measured from the same qty/avg the last bar was marked with.
    open_unrealized = qty * (closes[-1] - avg) if qty != 0.0 else 0.0
    exit_leg_identity = _graft_residual_identity(
        gap=balance_total - equity_mtm[-1],
        expected_gap=-(commission_used + open_unrealized),
        commission=commission_used,
        funding=None,
        open_unrealized=open_unrealized,
        measured=balance_measured,
    )
    return {
        "equity": adj,
        # #436: the un-grafted per-bar mark path, named. `raw_equity` is kept as
        # the legacy alias of the same list.
        "equity_mtm": equity_mtm,
        "raw_equity": list(equity_mtm),
        "reconciliation_adjustment_per_bar": drift,
        "equity_construction": EQUITY_CONSTRUCTION,
        "exposure": exposure,
        # #472: the single leg's own measured weights (`None` when the engine's
        # position record does not name the instrument it held).
        "leg_weights": {strategy_leg_id: list(exposure)} if strategy_leg_id else None,
        "turnover": turnover,
        "commission": commission_used,
        "funding": None,
        "cost_basis": cost_basis,
        "closed_loop_residual_usdt": engine_series_residual,
        "closed_loop_residual_measured": balance_measured,
        "closed_loop_ok": loop_ok,
        "cost_reconciliation": {
            "reported_commission_partial": reported_comm_total,
            "estimated_commission_total": est_comm_total,
            "sum_realized_pnl": sum_realized,
            # The attributed term, measured. It is NOT the residual (#436): the
            # field that claimed to be "the residual explained by open positions"
            # was identically equal to this input and could never disagree with it.
            "open_position_realized_pnl": open_realized,
            "engine_series_residual_usdt": engine_series_residual,
            "engine_series_residual_abs_usdt": loop_err,
            "engine_series_residual_measured": balance_measured,
            "engine_series_tolerance_usdt": STRATEGY_LOOP_ATOL,
            # #445: the identity this residual was computed under (the terms
            # below attribute the open-position term the same way), the state
            # under the same name the portfolio path publishes, and the OTHER
            # identity measured on this same engine balance. Non-governing.
            "identity": IDENTITY_STRATEGY_OPEN_REALIZED,
            "engine_series_ok": loop_ok,
            "cross_identity": cross_identity,
            "closed_loop_terms": {
                "balance_delta": balance_total - capital,
                "sum_realized_pnl": sum_realized,
                "open_position_realized_pnl": open_realized,
                "residual_usdt": engine_series_residual,
            },
            "balance_delta": balance_total - capital,
            "closed_loop_error": loop_err,
            "closed_loop_ok": loop_ok,
            "relative_error": rel_err if math.isfinite(rel_err) else None,
            "implied_drag": commission,
            "balance_total": balance_total,
            # The graft, declared: the terminal level of `equity` is the engine
            # balance and the un-grafted mark path ends `equity_mtm_gap_usdt`
            # away from it.
            "equity_construction": EQUITY_CONSTRUCTION,
            "reconciliation_adjustment_per_bar_usdt": drift,
            "reconciliation_adjustment_total_usdt": balance_total - equity_mtm[-1],
            "equity_mtm_terminal": equity_mtm[-1],
            "equity_mtm_gap_usdt": balance_total - equity_mtm[-1],
            # #491: the residual identity, asserted from this path's published
            # terms. `EXPLAINED_BY_EXCLUDED_COSTS` means the graft absorbs only
            # the commission this path excludes; anything else is named.
            "exit_leg_identity": exit_leg_identity,
            # #397: turnover notional, entry/exit separated, against the fill
            # notional the commission estimate charges (one fee convention).
            "turnover_notional": {
                "entry_notional_usdt": entry_turnover * capital,
                "exit_notional_usdt": exit_turnover * capital,
                "total_notional_usdt": turnover * capital,
                "entry_fills": n_trades,
                "exit_fills": exit_fills,
                "exit_price_source": {
                    "engine_avg_px_close": engine_priced_exits,
                    "bar_close_proxy": bar_close_proxy_exits,
                },
                "fills_notional_usdt": fill_notional_total,
                "fills_notional_unattributed_usdt": fill_notional_total
                - turnover * capital,
            },
        },
        "n_trades": n_trades,
        "n_trades_definition": "campaign open events (legacy field)",
        "n_completed_campaigns": n_completed_campaigns,
        "n_open_positions": n_open_positions,
    }


def funding_boundary_index(end_ns: list[int], boundary: int) -> int | None:
    """First index with ``end_ns[i] >= boundary`` via binary search.

    Identical to ``next((i for i, ns in enumerate(end_ns) if ns >= boundary), None)``
    on a monotonically non-decreasing timeline. Unsorted input fails closed
    instead of returning a wrong index; past-end maps to ``None``.
    """
    for i in range(1, len(end_ns)):
        if end_ns[i] < end_ns[i - 1]:
            raise ValueError("funding boundary lookup requires sorted end_ns")
    j = bisect_left(end_ns, boundary)
    return j if j < len(end_ns) else None


def portfolio_series_from_engine(
    engine_result: dict[str, Any],
    legs_closes: dict[str, list[float]],
    end_ns: list[int],
    capital: float = CAPITAL_DEFAULT,
    taker_fee: float = TAKER_FEE_DEFAULT,
    legs_quotevols: dict[str, list[float]] | None = None,
    funding_rows: Sequence[Any] | None = None,
    funding_measured_drag: float | None = None,
    funding_trades_identical: bool = False,
) -> dict[str, Any]:
    """Per-bar portfolio equity across legs from one shared-account engine run.

    legs_closes maps full instrument ids to per-bar closes on the shared
    end_ns timeline. Funding is measured by dual-run balance difference under
    verified identical trade signatures (the engine embeds settled funding in
    realized PnL, so single-run gap arithmetic cannot isolate it); the
    analytic row x open-position expectation is retained only as a
    cross-check. Unreconciled funding stays UNVERIFIED, never zero-filled.
    Participation per position leg is fill notional over real quote volume.

    Turnover counts the entry AND the exit leg of every campaign (#397): the
    published ``turnover_notional_over_capital`` and ``commission_cost`` are
    built from the same fills, so both read in the one fee convention the
    receipt declares (``taker_fee`` on traded notional).
    """
    n = len(end_ns)
    idx_of = {ns: i for i, ns in enumerate(end_ns)}
    opened = engine_result.get("opened_positions", [])
    closed = engine_result.get("closed_positions", [])
    pairs = pair_positions(
        [p for p in opened if isinstance(p, dict)],
        [c for c in closed if isinstance(c, dict)],
    )

    fills = [
        o
        for o in engine_result.get("account", {}).get("orders", [])
        if o.get("status") == "FILLED" and o.get("average_price") is not None
    ]
    est_comm_total = sum(
        float(o.get("filled_qty") or 0) * float(o.get("average_price") or 0) * taker_fee
        for o in fills
    )
    # #397: the fee base the commission estimate charges is the same fill set the
    # turnover is built from, so the two published columns stay readable in the
    # one fee convention the receipt declares.
    fill_notional_total = sum(
        abs(float(o.get("filled_qty") or 0)) * float(o.get("average_price") or 0)
        for o in fills
    )

    # Per-position intervals on their own leg (pair-indexed: NETTING slots
    # reuse position ids, so id-keyed matching would collide).
    events: dict[int, list[tuple[str, str, float, float]]] = {}
    infos: dict[int, dict[str, Any]] = {}
    engine_priced_exits = 0
    bar_close_proxy_exits = 0
    for pi, (pos, clo) in enumerate(pairs):
        inst = str(pos.get("instrument_id", ""))
        closes = legs_closes.get(inst)
        if closes is None or len(closes) != n:
            continue
        try:
            o_idx = idx_of.get(int(pos.get("event_ns", -1)), None)
            q = float(pos.get("quantity") or 0)
            px = float(pos.get("avg_px_open") or 0)
        except (ValueError, TypeError):
            continue
        if o_idx is None:
            continue
        side = str(pos.get("side", "")).upper()
        d = 1.0 if side in ("BUY", "LONG") else -1.0
        events.setdefault(o_idx, []).append(("open", inst, d * q, px))
        infos[pi] = {"instrument": inst, "qty": d * q, "open_px": px, "open_idx": o_idx,
                     "close_idx": None}
        if isinstance(clo, dict):
            try:
                c_idx = idx_of.get(int(clo.get("event_ns", -1)), None)
            except (ValueError, TypeError):
                c_idx = None
            if c_idx is not None and c_idx > o_idx:
                # #397: the exit leg of a campaign carries turnover on its own
                # notional, exactly like the entry leg. The engine's recorded
                # close price is used when present; a missing close price falls
                # back to the bar-close fill proxy AND is counted in
                # cost_reconciliation, never silently treated as a free exit.
                exit_px = parse_money(clo.get("avg_px_close"))
                if exit_px is None:
                    exit_price = float(closes[c_idx])
                    bar_close_proxy_exits += 1
                else:
                    exit_price = float(exit_px)
                    engine_priced_exits += 1
                events.setdefault(c_idx, []).append(("close", inst, d * q, exit_price))
                infos[pi]["close_idx"] = c_idx

    sum_realized = 0.0
    for c in closed:
        if isinstance(c, dict) and c.get("realized_pnl"):
            try:
                sum_realized += float(str(c["realized_pnl"]).split()[0])
            except (ValueError, TypeError):
                pass
    balance_measured = True
    try:
        balance_total = float(str(engine_result["account"]["balance_total"]).split()[0])
        balance_measured = math.isfinite(balance_total)
    except (KeyError, ValueError, TypeError, AttributeError):
        balance_total = capital
        balance_measured = False
    if not balance_measured:
        balance_total = capital

    equity = [capital]
    exposure = [0.0]
    live: dict[str, list[float]] = {}  # inst -> [qty, avg]
    #: #472: per-bar per-leg gross weight (`|qty * close| / equity`), the sum of
    #: which IS the `exposure` published for the same bar. Index 0 is the flat
    #: starting bar, exactly like `exposure[0]`.
    leg_gross: dict[str, float] = {inst: 0.0 for inst in legs_closes}
    leg_weights: dict[str, list[float]] = {inst: [0.0] for inst in legs_closes}
    turnover = 0.0
    entry_turnover = 0.0
    exit_turnover = 0.0
    n_trades = 0
    exit_fills = 0
    prev_mtm: dict[str, float] = {}
    for i in range(1, n):
        exit_step = 0.0
        for kind, inst, qd, px in events.get(i, []):
            if kind == "open":
                live[inst] = [qd, px]
                turnover += abs(qd) * px / capital
                entry_turnover += abs(qd) * px / capital
                n_trades += 1
            else:
                # #491: book the exit segment (last bar close -> measured exit
                # price) BEFORE the position leaves the mark book. `px` is the
                # engine's `avg_px_close`, or the documented bar-close proxy
                # already counted in `bar_close_proxy_exits`; it is measured,
                # never imputed. Before this the pops below came first and the
                # per-bar loop could not mark the leg on its exit bar.
                exit_step += qd * (px - legs_closes[inst][i - 1])
                live.pop(inst, None)
                prev_mtm.pop(inst, None)
                # #397: an exit pays the same linear fee on its own notional, so
                # it is turnover too. Before this the published turnover was
                # entry-only while the commission estimate covered both legs
                # (implied fee ~2x the declared taker fee).
                turnover += abs(qd) * px / capital
                exit_turnover += abs(qd) * px / capital
                exit_fills += 1
        for inst in leg_weights:
            # A leg flat at this bar carries no weight for it: the map is reset
            # every bar, so a leg that left the book cannot leave its last
            # weight behind and double count against the published exposure.
            leg_gross[inst] = 0.0
        step_pnl = exit_step
        gross = 0.0
        for inst, (qd, avg) in live.items():
            closes = legs_closes[inst]
            mtm = qd * (closes[i] - avg)
            step_pnl += mtm - prev_mtm.get(inst, 0.0)
            prev_mtm[inst] = mtm
            leg_gross[inst] = abs(qd * closes[i])
            gross += abs(qd * closes[i])
        equity.append(equity[-1] + step_pnl)
        exposure.append(gross / equity[-2] if equity[-2] > 0 else 0.0)
        for inst in leg_weights:
            leg_weights[inst].append(
                leg_gross[inst] / equity[-2] if equity[-2] > 0 else 0.0
            )

    # Open-position mtm at the last bar + entry commissions of open positions.
    # balance_total carries realized (net) of closed positions minus entry
    # commissions of still-open ones; it does not carry their unrealized.
    open_mtm = 0.0
    open_entry_comm = 0.0
    for _pid, info in infos.items():
        if info["close_idx"] is None:
            closes = legs_closes[info["instrument"]]
            open_mtm += info["qty"] * (closes[-1] - info["open_px"])
            open_entry_comm += abs(info["qty"]) * info["open_px"] * taker_fee

    # Funding reconciliation: expected from real rows x open positions.
    # Engine-matched holding convention (pinned empirically: exact 4-decimal
    # match over 587 holdings on the OOS window, plus isolated LONG/SHORT
    # settlement tests): a position opened at the boundary bar IS held
    # (open_idx <= b_idx); a position closed at the boundary bar is NOT
    # (close_idx > b_idx required). Same-ts opens fill after the funding
    # event, same-ts closes execute before it.
    funding_expected = 0.0
    funding_boundaries = 0
    per_boundary: list[float] = []
    # F1: end_ns is monotonically non-decreasing (bars are chronology-validated);
    # verified once so the per-row binary search stays O(log N). Unsorted fails
    # closed instead of returning a wrong index.
    for i in range(1, len(end_ns)):
        if end_ns[i] < end_ns[i - 1]:
            raise ValueError("funding boundary lookup requires sorted end_ns")
    for row in funding_rows or []:
        inst_full = f"{getattr(row, 'instrument', '')}-PERP.BINANCE"
        closes = legs_closes.get(inst_full)
        if closes is None:
            continue
        try:
            boundary = int(getattr(row, "funding_time_ms", -1)) * 1_000_000
            rate = float(getattr(row, "funding_rate", 0))
        except (ValueError, TypeError):
            continue
        # Identical to the linear first-ns->=boundary scan; past-end maps to None.
        _b = bisect_left(end_ns, boundary)
        b_idx: int | None = _b if _b < len(end_ns) else None
        if b_idx is None:
            continue
        for _pid, info in infos.items():
            if info["instrument"] != inst_full:
                continue
            notion = abs(info["qty"]) * closes[b_idx]
            leg = -math.copysign(1.0, info["qty"]) * notion * rate
            if info["open_idx"] <= b_idx and (info["close_idx"] is None or info["close_idx"] > b_idx):
                funding_expected += leg
                funding_boundaries += 1
                per_boundary.append(leg)
    funding_paid = -funding_measured_drag if funding_measured_drag is not None else 0.0
    # Tolerance is data-derived: the largest single-boundary settlement held
    # (one event-ordering difference) plus dust. No invented coefficient.
    dust = (max((abs(v) for v in per_boundary), default=0.0)) + 0.02
    funding_ok = (
        funding_measured_drag is not None
        and funding_trades_identical
        and abs(funding_measured_drag - (-funding_expected)) <= dust
    )
    fed = bool(funding_rows)
    # #436: the engine↔series closed-loop residual is MEASURED (signed) and
    # published. `balance_total` carries closed-position realized PnL net of the
    # entry commissions of still-open positions; it does not carry their
    # unrealized mark. Before this the only guard the portfolio path published
    # was replaced by the funding difference whenever funding rows were present
    # (which they always are on the real tape), so the engine↔series residual
    # was never surfaced and `VERIFIED_ENGINE_FUNDING` was asserted on a
    # quantity the receipt did not measure.
    engine_series_residual = (balance_total - capital) - sum_realized + open_entry_comm
    engine_series_residual_abs = abs(engine_series_residual)
    engine_series_ok = balance_measured and engine_series_residual_abs <= PORTFOLIO_LOOP_ATOL
    # #445: the OTHER closed-loop identity (the strategy path's), measured on this
    # same engine balance. Published so the reader can see whether a `MISMATCH` is
    # engine-side or identity-side. Non-governing: `cost_basis` and every gate
    # below derive from this path's own identity only.
    cross_identity = cross_identity_reconciliation(
        engine_result,
        taker_fee=taker_fee,
        capital=capital,
        balance_total=balance_total,
        balance_measured=balance_measured,
        sum_realized=sum_realized,
        governing_identity=IDENTITY_PORTFOLIO_OPEN_ENTRY_COMMISSIONS,
    )

    funding_residual = (
        funding_measured_drag - (-funding_expected)
        if (fed and funding_measured_drag is not None)
        else None
    )
    funding_residual_abs = abs(funding_residual) if funding_residual is not None else None
    # `loop_err` is the CONJUNCTION of the two independent reconciliations: the
    # funding settlement difference AND the engine↔series residual. A pass on one
    # may not stand in for the other, and the components are published beside it.
    loop_err = max(engine_series_residual_abs, funding_residual_abs or 0.0)
    loop_ok = engine_series_ok and (funding_ok if fed else True)
    if not balance_measured:
        # Unmeasurable is not verified: fail closed instead of guessing.
        cost_basis = "UNKNOWN"
    elif not engine_series_ok:
        # The engine's own ledger did not close. Fail closed; neither funding nor
        # the commission cross-check may upgrade this state.
        cost_basis = "MISMATCH"
    elif fed and funding_ok:
        cost_basis = "VERIFIED_ENGINE_FUNDING"
    elif fed:
        cost_basis = "FUNDING_UNVERIFIED"
    else:
        cost_basis = "VERIFIED_ENGINE"
        funding_paid = 0.0

    # The graft, declared (#436): `adj[-1] == balance_total` holds by
    # construction, so the returned object names the un-grafted mark path and the
    # per-bar adjustment that absorbs the difference.
    equity_mtm = list(equity)
    drift = (balance_total - equity[-1]) / max(1, n - 1)
    adj = [equity[0]]
    for i in range(1, n):
        adj.append(adj[-1] + (equity[i] - equity[i - 1]) + drift)
    # #491: assert the graft's declared meaning on this path's own published
    # terms: the residual it absorbs is the costs the mark path excludes
    # (commissions + funding) plus the unrealized mark of still-open positions.
    exit_leg_identity = _graft_residual_identity(
        gap=equity_mtm[-1] - balance_total,
        expected_gap=est_comm_total + (funding_paid if fed else 0.0) + open_mtm,
        commission=est_comm_total,
        funding=funding_paid if fed else None,
        open_unrealized=open_mtm,
        measured=balance_measured,
    )

    # Participation: entry/exit notional over real bar quote volume.
    participations: list[float] = []
    if legs_quotevols:
        for _pid, info in infos.items():
            qv = legs_quotevols.get(info["instrument"])
            if not qv:
                continue
            o_not = abs(info["qty"]) * info["open_px"]
            if qv[info["open_idx"]] > 0:
                participations.append(o_not / qv[info["open_idx"]])
            if info["close_idx"] is not None:
                closes = legs_closes[info["instrument"]]
                x_not = abs(info["qty"]) * closes[info["close_idx"]]
                if qv[info["close_idx"]] > 0:
                    participations.append(x_not / qv[info["close_idx"]])
    return {
        "equity": adj,
        # #436: the un-grafted per-bar mark path, named. `raw_equity` is kept as
        # the legacy alias of the same list.
        "equity_mtm": equity_mtm,
        "raw_equity": list(equity_mtm),
        "reconciliation_adjustment_per_bar": drift,
        "equity_construction": EQUITY_CONSTRUCTION,
        "exposure": exposure,
        # #472: the per-leg weights the exposure above is the sum of, keyed by the
        # instrument each position was held in.
        "leg_weights": leg_weights,
        "turnover": turnover,
        "commission": est_comm_total,
        "funding": funding_paid if fed else None,
        "funding_expected": funding_expected if fed else None,
        "funding_reconciled": funding_ok if fed else None,
        "funding_boundaries_held": funding_boundaries,
        "cost_basis": cost_basis,
        "closed_loop_residual_usdt": engine_series_residual,
        "closed_loop_residual_measured": balance_measured,
        "closed_loop_ok": loop_ok,
        "cost_reconciliation": {
            "estimated_commission_total": est_comm_total,
            "sum_realized_pnl": sum_realized,
            "open_mtm_last": open_mtm,
            # The term the closed-loop identity attributes to still-open
            # positions: their entry commissions, carried by the balance as a
            # reduction and measured here rather than assumed.
            "open_entry_commissions_of_open_positions": open_entry_comm,
            # #436: the engine↔series closed-loop residual, signed and measured.
            # It was previously computed as `base_loop` and thrown away in favour
            # of the funding difference.
            "engine_series_residual_usdt": engine_series_residual,
            "engine_series_residual_abs_usdt": engine_series_residual_abs,
            "engine_series_residual_measured": balance_measured,
            "engine_series_tolerance_usdt": PORTFOLIO_LOOP_ATOL,
            "engine_series_ok": engine_series_ok,
            # #445: the identity this residual was computed under, and the OTHER
            # identity measured on this same engine balance. Non-governing.
            "identity": IDENTITY_PORTFOLIO_OPEN_ENTRY_COMMISSIONS,
            "cross_identity": cross_identity,
            "closed_loop_terms": {
                "balance_delta": balance_total - capital,
                "sum_realized_pnl": sum_realized,
                "open_entry_commissions_of_open_positions": open_entry_comm,
                "residual_usdt": engine_series_residual,
            },
            "balance_delta": balance_total - capital,
            "funding_paid_implied": funding_paid,
            "funding_expected": funding_expected,
            # #436: the funding reconciliation, side by side with the residual
            # above. `loop_err` is the conjunction of the two, so neither may be
            # read as the whole verification.
            "funding_residual_usdt": funding_residual,
            "funding_tolerance_usdt": dust if fed else None,
            "funding_ok": funding_ok if fed else None,
            "loop_err_is_conjunction": True,
            "loop_err_components_usdt": {
                "engine_series_residual_abs": engine_series_residual_abs,
                "funding_residual_abs": funding_residual_abs,
            },
            "loop_ok": loop_ok,
            "closed_loop_error": loop_err,
            "balance_total": balance_total,
            # The graft, declared: the terminal level of `equity` is the engine
            # balance and the un-grafted mark path ends `equity_mtm_gap_usdt`
            # away from it.
            "equity_construction": EQUITY_CONSTRUCTION,
            "reconciliation_adjustment_per_bar_usdt": drift,
            "reconciliation_adjustment_total_usdt": balance_total - equity_mtm[-1],
            "equity_mtm_terminal": equity_mtm[-1],
            "equity_mtm_gap_usdt": equity_mtm[-1] - balance_total,
            # #491: the residual identity, asserted from this path's published
            # terms. `EXPLAINED_BY_EXCLUDED_COSTS` means the graft absorbs only
            # commissions + funding + still-open marks; anything else is named.
            "exit_leg_identity": exit_leg_identity,
            # #397: the notional the published turnover is built from, entry and
            # exit separated, beside the fill notional the commission estimate
            # charges. One fee convention: the two must agree.
            "turnover_notional": {
                "entry_notional_usdt": entry_turnover * capital,
                "exit_notional_usdt": exit_turnover * capital,
                "total_notional_usdt": turnover * capital,
                "entry_fills": n_trades,
                "exit_fills": exit_fills,
                "exit_price_source": {
                    "engine_avg_px_close": engine_priced_exits,
                    "bar_close_proxy": bar_close_proxy_exits,
                },
                "fills_notional_usdt": fill_notional_total,
                "fills_notional_unattributed_usdt": fill_notional_total
                - turnover * capital,
            },
        },
        "participation": participations,
        "n_trades": n_trades,
    }


def metrics_for_curve(
    equity: Sequence[float],
    exposure: Sequence[float],
    turnover: float,
    commission: float,
    funding: float | None,
    cost_basis: str,
    primary_equity: Sequence[float] | None,
    n_trades: int,
    raw_nonzero_bars: int | None = None,
    *,
    cost_reconciliation: Mapping[str, Any] | None = None,
    leg_weights: Mapping[str, Sequence[float]] | None = None,
) -> MetricSet:
    rets = per_bar_returns(list(equity))
    s_pb, s_ann = sharpe_stats(rets)
    # #388: the stored/published Sharpe CI shares the annualized scale of
    # sharpe_annualized, so the interval contains its own point estimate.
    ci_lo, ci_hi = sharpe_ci_annualized(rets)
    # #438 degeneracy: a leg whose variance estimate is not really there (one
    # trade in the window, or a bootstrap whose surviving replicates explode)
    # yields explosive Sharpe arithmetic. The flag is derived from the DECLARED
    # condition on the published cells (see `unreproducible_variance`), not from
    # a bar count alone: the count arm is sufficient, never necessary, so a
    # 500-bar window and a 150-bar window of the same curve agree. The count is
    # read on raw pre-reconciliation steps, never on drift-smeared levels.
    if raw_nonzero_bars is None:
        raw_nonzero_bars = sum(1 for v in rets if abs(v) > 1e-12)
    degenerate = unreproducible_variance(s_ann, ci_lo, ci_hi, raw_nonzero_bars)
    net = (equity[-1] / equity[0] - 1.0) if equity[0] > 0 else 0.0
    excess = None
    excess_ci: tuple[float | None, float | None] = (None, None)
    if primary_equity is not None and len(primary_equity) == len(equity):
        p_net = (primary_equity[-1] / primary_equity[0] - 1.0) if primary_equity[0] > 0 else 0.0
        excess = net - p_net
        # #396: `excess` is a window-scale return, so its interval must be the
        # block-bootstrap CI of that same compounded excess on that same scale.
        # The pre-fix interval was the per-bar Sharpe of the return difference
        # (a dimensionless ratio), which is a different estimand and does not
        # even contain the point estimate it is published beside.
        excess_ci = period_excess_ci(list(equity), list(primary_equity))
    tail = float(np.mean(np.sort(np.asarray(rets))[: max(1, int(0.05 * len(rets)))])) if rets else 0.0
    # #472: the published concentration is MEASURED from this curve's own per-leg
    # exposure weights, or named as unmeasured. The pre-fix producer published the
    # literal `1.0` here for every curve, so `cash` and a four-leg basket shared a
    # number neither of them had: the diagnostic was declared, never taken.
    concentration, concentration_legs, concentration_basis = concentration_from_leg_weights(
        leg_weights, list(exposure)
    )
    return MetricSet(
        net_return=net,
        excess_vs_primary=excess,
        excess_ci_low=excess_ci[0],
        excess_ci_high=excess_ci[1],
        sharpe_per_bar=s_pb,
        sharpe_annualized=s_ann,
        sharpe_ci_low=ci_lo,
        sharpe_ci_high=ci_hi,
        sharpe_degenerate=degenerate,
        max_drawdown=max_drawdown(list(equity)),
        tail_mean_5pct=tail,
        avg_exposure=float(np.mean(np.asarray(list(exposure)))) if exposure else 0.0,
        concentration=concentration,
        concentration_legs=concentration_legs,
        concentration_basis=concentration_basis,
        turnover_notional_over_capital=turnover,
        commission_cost=commission,
        funding_cost=funding,
        cost_basis=cost_basis,
        n_bars=len(equity),
        n_trades=n_trades,
        cost_reconciliation=dict(cost_reconciliation) if cost_reconciliation is not None else None,
    )


def interval_losses(
    equity: Sequence[float],
    end_ns: Sequence[int],
) -> tuple[tuple[Any, ...], int, int, int]:
    """Aggregate per-bar equity into equal INTERVAL_BARS loss intervals.

    Returns (losses, frozen_ns, evaluation_end_ns, decision_ns) satisfying the
    paired-loss chronology contract: frozen < eval_end < decision, intervals
    tiling [frozen, eval_end] exactly, each outcome available before decision.
    """
    from v8_next.evaluation.alignment import IntervalLoss

    n = len(equity)
    if n < 2 or len(end_ns) != n:
        raise ValueError("equity and timestamps must align with at least 2 bars")
    step = int(end_ns[1]) - int(end_ns[0])
    usable = (n - 1) // INTERVAL_BARS * INTERVAL_BARS
    frozen = int(end_ns[0])
    eval_end = int(end_ns[usable])
    decision = eval_end + step
    out: list[IntervalLoss] = []
    for k in range(usable // INTERVAL_BARS):
        a = 1 + k * INTERVAL_BARS
        b = 1 + (k + 1) * INTERVAL_BARS
        r = equity[b - 1] / equity[a - 1] - 1.0 if equity[a - 1] > 0 else 0.0
        out.append(
            IntervalLoss(
                int(end_ns[a - 1]),
                int(end_ns[b - 1]),
                int(end_ns[b - 1]),
                Decimal(str(-r)),
            )
        )
    return tuple(out), frozen, eval_end, decision


def run_statistics(
    family_equity: dict[str, Sequence[float]],
    end_ns: Sequence[int],
    baseline_id: str,
    *,
    plan: StatisticsPlan,
) -> dict[str, Any]:
    """DSR + PBO + SPA on real family curves. Fail closed; never proxy.

    NX07.R3/R4: every resampling parameter (block length, reps, seed), the trial
    multiplicity, the effective-independent-trials declaration and the CSCV
    partition count come from an immutable pre-registered ``plan``. This function
    has no statistical defaults left for a caller to tune after seeing a result,
    and it reports which statistics are authority conditions and which are
    diagnostics instead of leaving that to the reader.
    """
    from v8_next.evaluation.deflated_sharpe import DSRPlan, deflated_sharpe_diagnostic
    from v8_next.evaluation.overfitting import pbo_diagnostic

    if not isinstance(plan, StatisticsPlan):
        raise TypeError("run_statistics requires a pinned StatisticsPlan")
    stats: dict[str, Any] = {
        "baseline": baseline_id,
        "plan_id": plan.identity(),
        "plan": plan.as_dict(),
        "authority_conditions": list(plan.authority_conditions),
        "diagnostics": list(plan.diagnostics),
    }
    losses: dict[str, tuple[Any, ...]] = {}
    bounds: dict[str, tuple[int, int, int]] = {}
    for name, eq in family_equity.items():
        losses[name], frozen, eval_end, decision = interval_losses(list(eq), list(end_ns))
        bounds[name] = (frozen, eval_end, decision)
    first = next(iter(bounds.values()))
    if any(b != first for b in bounds.values()):
        raise ValueError("family curves do not share one evaluation chronology")
    frozen, eval_end, decision = first
    stats["intervals_per_variant"] = len(next(iter(losses.values()))) if losses else 0
    stats["interval_bars"] = INTERVAL_BARS
    base_eq = list(family_equity[baseline_id])
    base_net = base_eq[-1] / base_eq[0] - 1.0 if base_eq[0] > 0 else 0.0
    excess_by_name: dict[str, float] = {}
    for name, equity in family_equity.items():
        if name == baseline_id:
            continue
        values = list(equity)
        net = values[-1] / values[0] - 1.0 if values[0] > 0 else 0.0
        excess_by_name[name] = net - base_net
    best = max(excess_by_name.values(), default=None)
    stats["variant_excess_vs_baseline"] = best

    dsr_names = sorted(n for n in losses if n != baseline_id)
    # DSR must describe the selected (best-excess) variant, never whichever name
    # happens to sort first. Ties keep a deterministic lexicographic choice.
    selected_variant = max(dsr_names, key=lambda name: excess_by_name[name], default=baseline_id)
    sufficiency = sample_sufficiency(int(stats["intervals_per_variant"]), plan.block_size)
    stats["sample_sufficiency"] = sufficiency
    # #440: availability is probed BEFORE any estimator is called -- once, with
    # the same instrument the receipt records in `estimator_availability`. An
    # estimator whose dependency this interpreter cannot import is recorded as
    # never run: it is not called, and its state is not inferred from a caught
    # ImportError (which would conflate "could not be provisioned" with "ran and
    # failed on its own inputs").
    availability = estimator_availability()
    unmeasured = {name: availability.for_estimator(name) for name in ESTIMATOR_MODULES}

    if unmeasured["dsr"]:
        stats["dsr"] = unmeasured_statistic("dsr", unmeasured["dsr"])
    else:
        try:
            dsr_plan = DSRPlan(
                selected_variant=selected_variant,
                registered_variants=tuple(dsr_names),
                effective_independent_trials=plan.effective_independent_trials,
                independence_basis=plan.independence_basis,
            )
            dsr_losses = {n: losses[n] for n in dsr_names}
            stats["dsr"] = deflated_sharpe_diagnostic(
                dsr_losses,
                plan=dsr_plan,
                frozen_ns=frozen,
                evaluation_end_ns=eval_end,
                decision_ns=decision,
            )
            stats["dsr"]["verdict"] = "COMPUTED"
        except Exception as e:  # fail closed, keep reason
            stats["dsr"] = {"verdict": "UNDERPOWERED", "reason": f"{type(e).__name__}: {e}"}

    n_intervals = stats["intervals_per_variant"]
    partitions = (
        plan.pbo_partitions
        if n_intervals % plan.pbo_partitions == 0 and n_intervals >= 2 * plan.pbo_partitions
        else 0
    )
    if unmeasured["pbo"]:
        # Availability first: an estimator that cannot run at all is not "too few
        # intervals", and the interval count must not be published as its reason.
        stats["pbo"] = unmeasured_statistic("pbo", unmeasured["pbo"])
    elif partitions and len(dsr_names) >= 2:
        try:
            stats["pbo"] = pbo_diagnostic(
                {n: losses[n] for n in dsr_names},
                registered_variants=tuple(dsr_names),
                frozen_ns=frozen,
                evaluation_end_ns=eval_end,
                decision_ns=decision,
                partitions=partitions,
                metric=plan.pbo_metric,
                max_splits=plan.pbo_max_splits,
            )
            stats["pbo"]["verdict"] = "COMPUTED"
        except Exception as e:
            stats["pbo"] = {"verdict": "UNDERPOWERED", "reason": f"{type(e).__name__}: {e}"}
    else:
        stats["pbo"] = {
            "verdict": "UNDERPOWERED",
            "reason": f"interval count {n_intervals} does not admit even CSCV partitions>=8",
        }

    if unmeasured["spa"]:
        stats["spa"] = unmeasured_statistic("spa", unmeasured["spa"])
    else:
        try:
            from v8_next.evaluation.inference import spa_diagnostic

            variants = {n: losses[n] for n in dsr_names}
            stats["spa"] = spa_diagnostic(
                losses[baseline_id],
                variants,
                frozen_ns=frozen,
                evaluation_end_ns=eval_end,
                decision_ns=decision,
                block_size=plan.block_size,
                reps=plan.reps,
                seed=plan.seed,
            )
            stats["spa"]["verdict"] = "COMPUTED"
        except Exception as e:
            stats["spa"] = {
                "verdict": "UNSUPPORTED",
                "reason": f"{type(e).__name__}: {e}",
            }

    # R1 (#440): every estimator entry carries a state from the declared set. The
    # check is here so an unregistered state cannot reach a receipt by accident.
    for key in ESTIMATOR_MODULES:
        state = str((stats.get(key) or {}).get("verdict"))
        if state not in STATISTIC_STATES:
            raise ValueError(
                f"statistics entry {key!r} published undeclared state {state!r}; "
                f"declared: {sorted(STATISTIC_STATES)}"
            )
    return stats


def allocator_mix(
    inc_rets: Sequence[float],
    ch_rets: Sequence[float],
    budget: float = 1.0,
) -> dict[str, Any]:
    """Same allocator procedure, same risk budget, re-run on P and P+E.

    Vol-target sleeve sizing with trailing-only volatility; P uses the full
    budget, P+E splits it equally across sleeves. Engine-level joint execution
    is NOT performed here (disclosed); this is allocator-level contribution.
    """
    inc = np.asarray(list(inc_rets), dtype=np.float64)
    ch = np.asarray(list(ch_rets), dtype=np.float64)
    n = min(inc.size, ch.size)
    inc, ch = inc[:n], ch[:n]
    target_pb = VOL_TARGET_ANNUAL / math.sqrt(HOURS_PER_YEAR)

    def sleeve(rets: np.ndarray, share: float) -> np.ndarray:
        out = np.zeros_like(rets)
        for i in range(1, n):
            window = rets[max(0, i - VOL_LOOKBACK) : i]
            sd = float(window.std(ddof=1)) if window.size >= 2 else 0.0
            scale = min(target_pb / sd, MAX_LEVERAGE) if sd > 1e-12 else 0.0
            out[i] = rets[i] * scale * share * budget
        return out

    p_only = sleeve(inc, 1.0)
    p_plus_e = sleeve(inc, 0.5) + sleeve(ch, 0.5)
    cum_p = float(np.prod(1.0 + p_only) - 1.0)
    cum_pe = float(np.prod(1.0 + p_plus_e) - 1.0)
    return {
        "method": "VOL_TARGET_SLEEVE_RERUN",
        "scope": "ALLOCATOR_LEVEL_NOT_ENGINE_JOINT",
        "p_net": cum_p,
        "pe_net": cum_pe,
        "incremental_net": cum_pe - cum_p,
        "budget": budget,
    }


def capacity_table(
    net_excess: float,
    turnover: float,
    capital: float,
    taker_fee: float,
) -> list[dict[str, Any]]:
    """Capacity scenarios for the hourly swing contract.

    The benchmark uses OHLCV-derived turnover/ADV and a declared real-fill
    model. L2, queue position and intraday spread are deliberately outside
    this contract. Rows remain accounting diagnostics until real fills exist;
    notional scaling is never promoted to a measured capacity claim.
    """
    notionals = turnover * capital
    breakeven_bp = (net_excess * capital / notionals * 1e4) if notionals > 0 else None
    data_resolution = "1h OHLCV bars + derived ADV; no L2/tick dependency"
    modeled = [
        "taker_fee from the declared venue schedule",
        "turnover notional/capital from hourly bar accounting",
        "participation against OHLCV-derived ADV when real fills are present",
        "implementation shortfall from the declared real-fill model",
    ]
    unmodeled = [
        "capacity outside the observed participation range",
        "fill ratio without real execution records",
        "intraday queue position and spread microstructure (out of contract)",
    ]
    rows: list[dict[str, Any]] = []
    for mult in (1.0, 10.0, 100.0):
        if mult == 1.0:
            validation_note = (
                "1x = observed hourly accounting only; real-fill validation is required "
                "before capacity is treated as measured"
            )
            claim_validated = "NO: capacity requires real fills and OHLCV-derived ADV"
        else:
            validation_note = (
                f"CANNOT BE VALIDATED at {mult:.0f}x from the observed fill range: "
                "OHLCV/ADV cannot establish counterfactual fills; accounting only"
            )
            claim_validated = (
                "NO: capacity with preserved edge cannot be validated outside observed fills"
            )
        rows.append(
            {
                "capital_mult": mult,
                "capital": capital * mult,
                "assumed_fee_bp": taker_fee * 1e4,
                "linear_net_excess": net_excess * mult,
                "breakeven_extra_cost_bp": breakeven_bp,
                "data_resolution": data_resolution,
                "modeled": list(modeled),
                "unmodeled": list(unmodeled),
                "validation_note": validation_note,
                "claim_validated": claim_validated,
                # legacy key kept for backward compatibility
                "note": "MODELED: hourly turnover + declared fee schedule + OHLCV/ADV basis; "
                "UNMODELED: unobserved fill behaviour and capacity outside observed fills "
                "(no counterfactual fills or coefficients invented)",
            }
        )
    return rows


def negative_control_shuffled(
    returns: Sequence[float], seed: int = 11
) -> dict[str, Any]:
    """Shuffled signal-outcome link, cost totals preserved: must NOT read as success."""
    rng = np.random.default_rng(seed)
    arr = np.asarray(list(returns), dtype=np.float64)
    sh = rng.permutation(arr)
    s, _ = sharpe_stats([float(v) for v in sh])
    return {
        "kind": "NEGATIVE_CONTROL_SHUFFLED",
        "scope": "SYNTHETIC_CONTROL",
        "sharpe_per_bar": s,
        "declares_winner": bool(s > 0.1),
        "expected": False,
    }


def positive_control_known_effect(
    n: int = 480, drift: float = 0.002, vol: float = 0.01, seed: int = 5
) -> dict[str, Any]:
    """Known-effect synthetic process: detector must separate it (power check)."""
    rng = np.random.default_rng(seed)
    rets = drift + vol * rng.standard_normal(n)
    s, _ = sharpe_stats([float(v) for v in rets])
    ci_lo, _ = block_bootstrap_ci(list(rets))
    detected = ci_lo is not None and ci_lo > 0
    return {
        "kind": "POSITIVE_CONTROL_KNOWN_EFFECT",
        "scope": "SYNTHETIC_CONTROL",
        "n": n,
        "drift": drift,
        "vol": vol,
        "sharpe_per_bar": s,
        "ci_low": ci_lo,
        "detected": bool(detected),
        "expected": True,
    }


#: An estimator whose recorded reason is a failed import was never *provisioned*:
#: that is an environment state, not a statistical result, and it must not be
#: presented as "the estimator ran and produced nothing". `scipy`/`arch` are
#: declared only in `[project.optional-dependencies].research`, so a fresh sync
#: without the extra lands exactly here. Since #440 this regex is only the
#: *legacy* reading of that state, for receipts written before
#: `ESTIMATOR_UNAVAILABLE` and its explicit `unavailable` list existed.
_UNPROVISIONED_IMPORT_RE = re.compile(
    r"^(?:ModuleNotFoundError|ImportError): No module named '([^']+)'"
)

#: How to fix the state the note above names. Kept in one place so the receipt
#: note and the stdout line cannot drift apart.
UNPROVISIONED_REMEDY = (
    "provision the optional `research` extra "
    "(`uv run --project v8-next --extra research ...`)"
)


def unmeasured_statistic(estimator: str, missing: Sequence[str]) -> dict[str, Any]:
    """The record for an estimator that was never called (#440).

    Named by the state token and by the dependencies that are missing, in
    machine-readable fields, so nothing about it reads as a measured statistical
    outcome: `ESTIMATOR_UNAVAILABLE` is an environment fact about this
    interpreter, and the entry carries no p-value, confidence or score that a
    consumer could mistake for one.
    """
    return {
        "verdict": ESTIMATOR_UNAVAILABLE,
        "estimator": estimator,
        "unavailable": list(missing),
        "reason": (
            f"estimator not provisioned: {', '.join(missing)} not importable by "
            f"this interpreter ({UNPROVISIONED_REMEDY})"
        ),
    }


def unprovisioned_estimators(stats: dict[str, Any]) -> tuple[str, ...]:
    """Dependencies this interpreter could not import, in estimator order.

    Reads the explicit `unavailable` list `run_statistics` records for an
    unmeasured estimator (#440), falling back to the fail-closed import `reason`
    for receipts written before that field existed. An empty tuple means every
    estimator that failed, failed for a reason of its own and the verdict string
    must not claim a provisioning problem.
    """
    missing: list[str] = []
    for key in ESTIMATOR_MODULES:
        entry = stats.get(key)
        if not isinstance(entry, dict):
            continue
        declared = entry.get("unavailable")
        if isinstance(declared, (list, tuple)) and declared:
            for name in declared:
                if str(name) not in missing:
                    missing.append(str(name))
            continue
        match = _UNPROVISIONED_IMPORT_RE.match(str(entry.get("reason", "")).strip())
        if match and match.group(1) not in missing:
            missing.append(match.group(1))
    return tuple(missing)


def unprovisioned_estimator_hint(stats: dict[str, Any]) -> str | None:
    """Name the missing estimator dependencies, or None when there are none.

    The returned sentence is the operator-visible difference between "the
    estimators could not be provisioned" and "the estimators ran and produced no
    supporting output" — two states that otherwise share one verdict string.
    """
    missing = unprovisioned_estimators(stats)
    if not missing:
        return None
    return (
        f"estimator dependencies not provisioned: {', '.join(missing)} "
        f"({UNPROVISIONED_REMEDY})"
    )


def build_verdicts(
    *,
    chrono_ok: bool,
    chrono_note: str,
    leak_probe: tuple[bool, str] | None,
    excess: float | None,
    excess_ci: tuple[float | None, float | None] | None,
    stats: dict[str, Any],
    mix: dict[str, Any],
    cost_basis_ok: bool,
    funding_missing: bool,
    live_fills_present: bool,
    parity_ok: bool,
) -> EvidenceVerdicts:
    """Derive every published verdict from measured inputs.

    ``leak_probe`` is the result of :func:`detect_future_leak` run on the very
    series the numbers were computed from, or ``None`` when no probe ran (#437).
    It is keyword-required and fail-closed: a caller cannot omit it and still
    publish ``research_validity = VALID``, because the absence of a probe is
    itself the state that makes the series unvalidated. The probe's note travels
    into ``research_note`` beside the probe identity that measured it, so a
    receipt says *what* inspected the series and not only that something did.
    (A future caller can still hand-write a passing tuple; what this closes is
    the omission that published a validity nobody measured -- #437.)
    """
    if leak_probe is None:
        leak_ok = False
        leak_note = "FUTURE_LEAK_PROBE_NOT_RUN: no probe inspected the series these numbers came from"
    else:
        leak_ok, leak_note = bool(leak_probe[0]), str(leak_probe[1])
    data_ok = bool(chrono_ok) and leak_ok
    if data_ok:
        research: VerdictState = "VALID"
    else:
        research = "INVALID"
    research_note = f"{chrono_note}; leak_probe={leak_note}"
    if not data_ok:
        economic: VerdictState = "INCONCLUSIVE"
        eco_note = "no economic inference from invalid data/accounting"
    elif excess is None:
        economic = "INCONCLUSIVE"
        eco_note = "primary comparison unavailable"
    elif excess > 0:
        economic = "POSITIVE_DESCRIPTIVE"
        eco_note = "net excess vs primary is positive on this window; not an edge claim"
    else:
        economic = "NEGATIVE"
        eco_note = "net excess vs primary is not positive on this window"
    dsr = stats.get("dsr", {})
    spa = stats.get("spa", {})
    pbo = stats.get("pbo", {})
    dsr_v = str(dsr.get("verdict", "UNDERPOWERED"))
    spa_v = str(spa.get("verdict", "UNSUPPORTED"))
    pbo_v = str(pbo.get("verdict", "UNDERPOWERED"))
    computed = f"DSR={dsr_v} PBO={pbo_v} SPA={spa_v}"
    unmeasured = unprovisioned_estimators(stats)
    if unmeasured:
        # #440: a dependency this interpreter cannot import is not a statistical
        # result, and it is not a weak one either. The published verdict is the
        # state that says so and names the dependencies; `UNDERPOWERED` and
        # `UNSUPPORTED` stay reserved for estimators that actually ran, so the
        # power vocabulary never reads as a measurement that did not happen.
        statistical_state: VerdictState = ESTIMATOR_UNAVAILABLE
        published_statistical = estimator_unavailable_verdict(unmeasured)
        coverage = (
            "no estimator output"
            if dsr_v != "COMPUTED" and pbo_v != "COMPUTED" and spa_v != "COMPUTED"
            else "partial estimator coverage"
        )
        # The per-estimator states are not echoed here: for a receipt written
        # before #440 they still carry the power vocabulary, and the note must
        # never publish a measurement word for an estimator that never ran. The
        # states themselves stay in the statistics block, where they are fields.
        stat_note = (
            f"{coverage}: estimator dependencies not provisioned: "
            f"{', '.join(unmeasured)} ({UNPROVISIONED_REMEDY}) — never measured, "
            "so no statistical outcome is published"
        )
    elif dsr_v != "COMPUTED" and pbo_v != "COMPUTED" and spa_v != "COMPUTED":
        # Every estimator ran and failed on its own inputs: this is the path
        # that already published a power verdict, and it keeps doing so.
        statistical_state = (
            "UNSUPPORTED"
            if spa_v == "UNSUPPORTED" or dsr_v == "UNSUPPORTED"
            else "UNDERPOWERED"
        )
        published_statistical = statistical_state
        stat_note = f"no estimator output: {computed}"
    else:
        # Preregistered rules, applied mechanically. Computation alone mints nothing.
        spa_p = None
        try:
            spa_p = float(spa.get("pvalues", {}).get("consistent", float("nan")))
        except (ValueError, TypeError):
            spa_p = None
        dsr_conf = dsr.get("dsr_confidence")
        dsr_sr = dsr.get("selected_sharpe_nonannualized")
        dsr_sel = dsr.get("selected_variant")
        best_excess = None
        for key in ("variant_excess_vs_baseline",):
            if key in stats and isinstance(stats[key], (int, float)):
                best_excess = float(stats[key])
        edge_hits: list[str] = []
        if spa_p is not None and spa_p < SPA_EDGE_ALPHA and (best_excess or 0.0) > 0:
            edge_hits.append(f"SPA_EDGE_RULE(p={spa_p:.4f}<{SPA_EDGE_ALPHA},excess>0)")
        if (
            isinstance(dsr_conf, (int, float))
            and isinstance(dsr_sr, (int, float))
            and dsr_conf >= DSR_EDGE_CONFIDENCE
            and dsr_sr > 0
        ):
            edge_hits.append(
                f"DSR_EDGE_RULE(conf={dsr_conf:.4f}>={DSR_EDGE_CONFIDENCE},{dsr_sel},sharpe>0)"
            )
        ci_lo, ci_hi = excess_ci if excess_ci else (None, None)
        under_hit = (
            ci_lo is not None and ci_hi is not None and ci_hi < 0
        )
        if edge_hits:
            statistical_state = "SUPPORTS_EDGE"
            stat_note = "fired: " + "; ".join(edge_hits)
        elif under_hit:
            statistical_state = "SUPPORTS_UNDERPERFORMANCE"
            assert ci_lo is not None and ci_hi is not None
            stat_note = (
                f"fired: EXCESS_CI_RULE(excess 95% CI [{ci_lo:.5f},{ci_hi:.5f}]<0)"
            )
        else:
            statistical_state = "INCONCLUSIVE"
            stat_note = f"computed but no preregistered rule fired: {computed}"
        published_statistical = statistical_state
    inc = mix.get("incremental_net")
    if inc is None:
        portfolio: VerdictState = "INCONCLUSIVE"
        pf_note = "mix unavailable"
    elif inc > 0:
        portfolio = "HELPFUL_DESCRIPTIVE"
        pf_note = "P+E shared-account engine rerun improves net on this window"
    else:
        portfolio = "NOT_HELPFUL"
        pf_note = "P+E sleeve mix does not improve allocator-level net on this window"
    if not cost_basis_ok:
        execution: VerdictState = "EXECUTION_UNPROVEN"
        ex_note = "cost basis unverified; simulated fills only"
    elif funding_missing:
        execution = "SIM_ONLY"
        ex_note = "simulated fills; funding unavailable; no venue-settled fills"
    elif not live_fills_present:
        execution = "SIM_ONLY"
        ex_note = "simulated fills; public funding measured; no venue-settled fills"
    elif not parity_ok:
        execution = "EXECUTION_UNPROVEN"
        ex_note = "ledger parity diverged"
    else:
        execution = "SIM_ONLY"
        ex_note = "simulated fills with reconciled costs; venue settlement absent"
    return EvidenceVerdicts(
        research_validity=research,
        research_note=research_note,
        economic=economic,
        economic_note=eco_note,
        statistical=published_statistical,
        statistical_note=stat_note,
        portfolio=portfolio,
        portfolio_note=pf_note,
        execution=execution,
        execution_note=ex_note,
        capital="NOT_AUTHORIZED",
        capital_note="no score mints capital permission; separate risk decision required",
    )


#: Render vocabulary for a curve whose published Sharpe arithmetic is not
#: supported by the variance behind it (too few informative bars, or a bootstrap
#: whose surviving replicates explode). Shared by the in-sample table and the
#: chronological OOS block so the two cannot drift: an explosive Sharpe printed
#: without this marker is a reporting defect.
DEGENERATE_VARIANCE_MARKER = "DEGENERATE-VARIANCE"


def degenerate_row(m: MetricSet) -> bool:
    """Does this published row carry the marker? (#438)

    Re-derived from the row's own published cells with the same criterion the
    metric was built with, in addition to its stored flag. A receipt whose flag
    predates the criterion -- or whose bootstrap interval blows up while the
    flag stays false -- cannot then be rendered unmarked, and the renderer's
    verdict for a curve in the in-sample table and in the OOS block comes from
    one criterion instead of from two window lengths.
    """
    return bool(m.sharpe_degenerate) or unreproducible_variance(
        m.sharpe_annualized, m.sharpe_ci_low, m.sharpe_ci_high
    )


def degeneracy_marker(m: MetricSet) -> str:
    """Return the degeneracy marker for a metric row (empty when sound)."""
    return f" {DEGENERATE_VARIANCE_MARKER}" if degenerate_row(m) else ""


def sharpe_ci_cell(m: MetricSet) -> str:
    """Render the ANNUALIZED Sharpe CI as `[lo,hi]`; empty when not computed.

    Shared by the in-sample table and the OOS block so both publish the interval
    on the same scale as the `sharpe_annualized` cell beside it (#388).
    """
    if m.sharpe_ci_low is None or m.sharpe_ci_high is None:
        return ""
    return f"[{m.sharpe_ci_low:.3f},{m.sharpe_ci_high:.3f}]"


def excess_ci_cell(m: MetricSet) -> str:
    """Render the WINDOW EXCESS RETURN CI as `[lo,hi]`; empty when not computed.

    The interval is the block-bootstrap interval of `excess_vs_primary` (#396),
    so it is a return-scale interval (fraction of capital over the window) and
    never a Sharpe ratio. It is printed beside its own point estimate at that
    estimate's precision, which keeps `excess_ci_low <= excess_vs_primary <=
    excess_ci_high` readable off the page. Honest absence mirrors the Sharpe
    path: no interval computed, no bracket printed.
    """
    if m.excess_ci_low is None or m.excess_ci_high is None:
        return ""
    return f"[{m.excess_ci_low:.4f},{m.excess_ci_high:.4f}]"


def render_report(receipt: EconomicReceipt) -> str:
    r = receipt
    # #440: only when something is actually missing, so a fully provisioned run's
    # report is unchanged. When it is, the report says what the `statistical`
    # verdict above is resting on instead of leaving the reader to infer it.
    availability_lines: list[str] = []
    if r.estimator_availability.unavailable:
        availability_lines = [
            "",
            "Estimator availability (probed with "
            f"`{r.estimator_availability.probe}` before any estimator was "
            "called): not importable by this interpreter: "
            f"{', '.join(r.estimator_availability.unavailable)}. The `statistical` "
            "verdict above names those dependencies and is a fail-closed "
            "environment state, not a measured statistical outcome.",
        ]
    lines = [
        "# Economic Benchmark Report (NO_ECONOMIC_CLAIM)",
        "",
        f"receipt: `{r.receipt_id}`",
        f"run digest: `{r.run.digest()}`",
        f"primary benchmark (pre-declared): **{r.run.primary_benchmark}**",
        f"diagnostics: {', '.join(r.run.diagnostic_benchmarks)}",
        f"family: {', '.join(r.run.strategy_family)}",
        f"window: {r.run.dataset.n_bars} bars "
        f"[{r.run.dataset.period_start_ns} .. {r.run.dataset.period_end_ns}]",
        f"capital: {r.run.capital} fee: {r.run.taker_fee} opex/mo: {r.run.opex_monthly_usd}",
        "",
        "## Verdicts (independent; no aggregate PASS)",
        "",
        "| domain | state | note |",
        "|---|---|---|",
        f"| research validity | {r.verdicts.research_validity} | {r.verdicts.research_note} |",
        f"| economic | {r.verdicts.economic} | {r.verdicts.economic_note} |",
        f"| statistical | {r.verdicts.statistical} | {r.verdicts.statistical_note} |",
        f"| portfolio | {r.verdicts.portfolio} | {r.verdicts.portfolio_note} |",
        f"| execution | {r.verdicts.execution} | {r.verdicts.execution_note} |",
        f"| capital | {r.verdicts.capital} | {r.verdicts.capital_note} |",
        *availability_lines,
        "",
        "## Metrics (cost-adjusted, shared basis)",
        "",
        "All money columns in USDT on the run capital "
        f"({r.run.capital:.2f} USDT); returns are fractions of that capital.",
        "",
        "Sharpe_ann and its [CI] are both annualized (per-bar Sharpe x "
        f"{SHARPE_ANNUALIZATION:.2f} = sqrt({HOURS_PER_YEAR:.0f})); the CI is a "
        "circular block-bootstrap percentile interval on that same scale, so it "
        "contains its own point estimate unless the row is marked degenerate. "
        f"A row is marked `{DEGENERATE_VARIANCE_MARKER}` when the variance "
        "behind its Sharpe is not there to be estimated: fewer than "
        f"{MIN_INFORMATIVE_BARS} informative bars, a point estimate past "
        f"{DEGENERATE_PER_BAR_SHARPE:.1f} per bar (Sharpe_ann past "
        f"{SHARPE_ANNUALIZATION * DEGENERATE_PER_BAR_SHARPE:.1f}), or a CI past "
        f"{DEGENERATE_CI_RATIO:.0f}x its own point estimate. Such a row "
        "publishes no interval (`n/a`): those percentiles are bootstrap "
        "artefacts, not a range its estimate lives in. "
        "`excess vs primary [CI]` is the block-bootstrap interval of the window "
        "excess return beside it — a return-scale interval (fraction of capital "
        "over the window), never a Sharpe ratio — and is omitted when it could "
        "not be computed.",
        "",
        "| curve | net | $P&L | excess vs primary [CI] | Sharpe_ann [CI] | maxDD | turn | commission $ | funding $ |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for name, m in r.metrics.items():
        # #438: when the criterion holds the interval is not published at all.
        ci = ("" if degenerate_row(m) else sharpe_ci_cell(m)) or "n/a"
        ci += degeneracy_marker(m)
        ex = f"{m.excess_vs_primary:.4f}" if m.excess_vs_primary is not None else "—"
        ex_ci = excess_ci_cell(m)
        if ex_ci:
            ex = f"{ex} {ex_ci}"
        fd = funding_cell(m)
        pnl = m.net_return * r.run.capital
        lines.append(
            f"| {name} | {m.net_return:.4f} | {pnl:.2f} | {ex} | {m.sharpe_annualized:.3f} {ci} "
            f"| {m.max_drawdown:.4f} | {m.turnover_notional_over_capital:.3f} "
            f"| {m.commission_cost:.2f} | {fd} |"
        )
    lines += [
        "",
        "`funding $` is the funding measured for that curve -- the basket that "
        "carries its rule, run twice by the engine with the tape's real funding "
        "rows and with none, publishing its own balance difference (negative when "
        "funding was paid, positive when received). A number is published only on "
        "the curve the engine measured it for: where the rule's measuring basket "
        "sizes its legs under a different convention from the curve here (the "
        "fixed-notional rebalancing quad baskets against the compounding analytic "
        "index they are paired with), the number is published on that basket's own "
        "row in this same table and this row reads "
        "`n/a (BASKET_CONVENTION_DIFFERS, <basket_id>)`, naming it. The column "
        "otherwise says why there is no number: `0.00 (NO_EXPOSURE)` means the "
        "curve held no position at any settlement, so its funding is zero by "
        "construction; `n/a (NO_ENGINE_RULE)` means the leg names a rule no engine "
        "basket executes, so no basket could settle it; `n/a (TRADES_DIVERGED)` "
        "means the funded and unfunded runs of that basket traded differently, so "
        "the difference is not attributable to funding; `n/a (NOT_MEASURED)` means "
        "no funding feed reached that curve on this run. Every row names its state "
        "and, when the number lives elsewhere, the curve that carries it: never a "
        "bare blank, never another curve's number, and never a zero standing in "
        "for an absence.",
    ]
    # #445: the fail-closed cost state is published beside the measurement it
    # rests on. Only curves that measured a reconciliation of their own appear.
    recon_rows = [
        (name, m.cost_reconciliation)
        for name, m in r.metrics.items()
        if m.cost_reconciliation
    ]
    if recon_rows:
        def cell(value: Any, spec: str = ".10g") -> str:
            return "—" if value is None else format(float(value), spec)

        lines += [
            "",
            "## Cost reconciliation (the measured engine↔series closed-loop residual)",
            "",
            "`cost_basis` is a decision; the number it rests on is here. "
            "`residual` is the signed engine↔series closed-loop residual (USDT), "
            "measured on the engine balance and compared against the curve's "
            "declared `tolerance`. `terms` are the magnitudes that residual is a "
            "function of — `balance_delta - sum_realized_pnl +/- the "
            "open-position term` — published so it can be RECOMPUTED from this "
            "report instead of taken on trust; `identity` names which closed-loop "
            "identity it was computed under. A curve whose engine balance could "
            "not be read publishes `NOT_MEASURED`, a null residual and the named "
            "reason: never a number nobody measured, and never a zero standing in "
            "for one. A curve that reconciled nothing of its own (the analytic "
            "legs) publishes no block at all rather than an empty one. These rows "
            "are the window's; the chronological-OOS rows below carry the window's "
            "cost-basis label and no separate reconciliation of their own.",
            "",
            "| curve | cost basis | state | identity | residual USDT | abs USDT | tolerance | ok | reason |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for name, recon in recon_rows:
            lines.append(
                f"| {name} | {recon['cost_basis']} | {recon['state']} "
                f"| {recon['identity'] or '—'} "
                f"| {cell(recon['engine_series_residual_usdt'])} "
                f"| {cell(recon['engine_series_residual_abs_usdt'])} "
                f"| {cell(recon['engine_series_tolerance_usdt'])} "
                f"| {recon['engine_series_ok'] if recon['engine_series_ok'] is not None else '—'} "
                f"| {recon['named_reason']} |"
            )
        lines += [
            "",
            "| curve | balance_delta | sum_realized_pnl | open-position term | recomputed residual | recompute == stored |",
            "|---|---|---|---|---|---|",
        ]
        for name, recon in recon_rows:
            terms = recon["closed_loop_terms"] or {}
            open_key = None
            for candidate in IDENTITY_OPEN_TERM_KEYS.values():
                if candidate in terms:
                    open_key = candidate
            lines.append(
                f"| {name} | {cell(terms.get('balance_delta'))} "
                f"| {cell(terms.get('sum_realized_pnl'))} "
                f"| {open_key or '—'}: {cell(terms.get(open_key) if open_key else None)} "
                f"| {cell(recon['recomputed_residual_usdt'])} "
                f"| {recon['recompute_matches_stored'] if recon['recompute_matches_stored'] is not None else '—'} |"
            )
        lines += [
            "",
            "The other closed-loop identity this code has — the two differ only in "
            "the term they attribute to still-open positions — is measured on the "
            "same engine balance, so a `MISMATCH` can be read as engine-side or "
            "identity-side instead of assumed. That check is a measurement and can "
            "come out either way: `FAIL` with reason "
            f"`{REASON_IDENTITIES_DIVERGED}` means the two conventions do not agree "
            "within the bound the curve declares, and it carries the difference it "
            "measured; `FAIL` with reason "
            f"`{REASON_TERMS_NOT_PUBLISHED}` means the receipt does not carry both "
            "identities' terms on one balance, so no agreement is claimed. Neither "
            "is an input to `cost_basis`, to the gates or to any verdict.",
            "",
            "| curve | identity check | difference USDT | comparison bound | reason |",
            "|---|---|---|---|---|",
        ]
        for name, recon in recon_rows:
            check = recon["identities_check"]
            lines.append(
                f"| {name} | {check['status']} | {cell(check['difference_usdt'])} "
                f"| {cell(check['comparison_tolerance_usdt'])} "
                f"| {check['reason'] or '—'} |"
            )
    mix = r.portfolio_mix
    if isinstance(mix, dict) and mix.get("scope") == "ENGINE_LEVEL_SAME_BUDGET":
        lines += [
            "",
            "Budget note: P and P+E run at engine level with the same total "
            "per-leg notional budget and risk rules. P+E splits each leg 50/50 "
            "across incumbent and challenger sleeves, so the measured "
            "incremental isolates exactly one change: the contradiction-"
            "tolerance gate (28 -> 0) at half risk each. It is not a sum of "
            "standalone P&Ls.",
            "",
        ]
    lines += [
        "",
        "## Chronological OOS (frozen split; never relabeled)",
        "",
        f"`{DEGENERATE_VARIANCE_MARKER}` marks a row whose Sharpe_ann is "
        "arithmetic on a variance the block cannot support -- fewer than "
        f"{MIN_INFORMATIVE_BARS} informative bars, a point estimate past "
        f"{DEGENERATE_PER_BAR_SHARPE:.1f} per bar, or a CI past "
        f"{DEGENERATE_CI_RATIO:.0f}x its own point estimate; the same criterion, "
        "applied to the same published cells, decides the in-sample table. The "
        "value is not interpretable and is never evidence of performance. CI is "
        "the annualized block-bootstrap interval, on the same scale as "
        "Sharpe_ann, and is omitted when it could not be computed or when the "
        "row is marked.",
        "",
    ]
    for name, m in r.oos_metrics.items():
        # #438: the same criterion suppresses the interval here as in the table.
        ci = "" if degenerate_row(m) else sharpe_ci_cell(m)
        lines.append(
            f"- {name}: net {m.net_return:.4f} Sharpe_ann "
            f"{m.sharpe_annualized:.3f}"
            f"{f' CI {ci}' if ci else ''}{degeneracy_marker(m)}"
        )
    lines += [
        "",
        "## Statistics (family-scoped, descriptive)",
        "",
        "```json",
        json.dumps(r.statistics, indent=2, default=str)[:6000],
        "```",
        "",
        "## Controls",
        "",
        "```json",
        json.dumps(r.controls, indent=2, default=str)[:4000],
        "```",
        "",
        "## Portfolio P vs P+E (allocator rerun, same budget)",
        "",
        "```json",
        json.dumps(r.portfolio_mix, indent=2, default=str),
        "```",
        "",
        "## Capacity scenarios — bounded to 1h bar resolution (MODELED vs UNMODELED)",
        "",
        "```json",
        json.dumps(r.capacity_scenarios, indent=2, default=str),
        "```",
        "",
        "## Parity / shadow / live",
        "",
        "```json",
        json.dumps({**r.parity, **r.shadow_live}, indent=2, default=str),
        "```",
        "",
        "## Limitations",
        "",
    ]
    lines += [f"- {lim}" for lim in r.limitations]
    lines += ["", "claim_status: NO_ECONOMIC_CLAIM", ""]
    return "\n".join(lines)
