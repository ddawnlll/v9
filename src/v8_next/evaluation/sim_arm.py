"""Costed measurement of the V9 adaptive arm against the V8 baseline.

This module is the *only* runner of
:class:`v8_next.adapters.adaptive_strategy.AdaptiveSimStrategy`, and it only ever
drives a NautilusTrader ``BacktestEngine``. There is no venue adapter, no
credential read and no dry-run path here: the arm's orders exist as engine
events and nothing in this module can send one anywhere.

Two arms, one measurement basis:

* :func:`run_sim_arm` measures the V9 adaptive arm.
* :func:`run_baseline_arm` measures the V8 expert ensemble with the *identical*
  tape window, funding feed, fees, execution profile and sizing/stop/target
  parameters (``SimArmConfig.baseline_config()``), so a delta between them is a
  decision-source effect and not a measurement artefact.

Every number published here comes from the engine: ``net_pnl`` is the native
account balance delta, ``fee_total`` is summed from the order-fill commission
column, ``funding_total`` is summed from the engine's own funding adjustments,
and ``net_r``/``hit_rate``/``max_drawdown`` are computed from the native
position open/close records. Nothing is defaulted to zero: an unmeasured value
stays ``None`` and is named in ``unsupported``.

Refusals are typed and fail closed:

* ``SIM_ARM_DISABLED`` — ``SimArmConfig.enabled`` is not explicitly ``True``.
* ``TAPE_NOT_AUTHORIZED`` — the tape is not the authorized development slice.
* ``TAPE_INTEGRITY_MISMATCH`` — the tape's bytes do not match its manifest.
* ``TAPE_EMPTY`` — the loaded window carries no bars.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.common import LogLevel
from nautilus_trader.config import BacktestEngineConfig, LoggerConfig
from nautilus_trader.model import (
    AccountType,
    BarType,
    Currency,
    FundingRateUpdate,
    MarkPriceUpdate,
    Money,
    OmsType,
    Price,
    Venue,
)

from v8_next.adapters.adaptive_strategy import (
    AdaptiveSimStrategy,
    SimArmConfig,
    SimArmDisabled,
    SimArmRefused,
)
from v8_next.adapters.engine_state import economic_state
from v8_next.adapters.execution_models import profile_digest, resolve_profile, venue_kwargs
from v8_next.adapters.execution_telemetry import execution_telemetry, native_fill_records
from v8_next.adapters.expert_strategy import ExpertEnsembleStrategy, ExpertStrategyConfig
from v8_next.adapters.portfolio_backtest import (
    _bars,
    _instrument,
    _plan_funding_events,
    base_currency,
)
from v8_next.adapters.protection_invariants import (
    BracketInvariantChecker,
    ProtectionEvent,
)
from v8_next.domain.market import Candle
from v8_next.evaluation.ablations import (
    BASELINE_ARM_ID,
    BOOTSTRAP_RESAMPLES,
    BOOTSTRAP_SEED,
    CI_LEVEL,
    INTRABAR_TIE_BASIS,
    V9_INTERVENTION_KIND,
    ArmResult,
    Intervention,
    Provenance,
    bootstrap_mean_ci,
)
from v8_next.evaluation.economic_benchmark import pair_positions, parse_money
from v8_next.evaluation.multitape import FundingRow, MultiTape, load_multitape
from v8_next.evaluation.tape_identity import sha256_file

__all__ = [
    "ADAPTIVE_ARM_ID",
    "AUTHORIZED_DEVELOPMENT_SLICE",
    "SimArmMeasurement",
    "run_baseline_arm",
    "run_sim_arm",
]

#: The one tape the owner authorized for this arm's development measurement.
AUTHORIZED_DEVELOPMENT_SLICE: Final[str] = "btcusdt-1h-2025Q1"

#: Directory shape a tape must have to be recognized as a repository slice.
_SLICE_ROOT_NAMES: Final[tuple[str, str]] = ("research", "tape")

#: Arm identity of the V9 adaptive measurement.
ADAPTIVE_ARM_ID: Final[str] = "v9_adaptive_sim_arm"

#: Money fields the engine renders as "<amount> <CURRENCY>".
_ZERO: Final[Decimal] = Decimal("0")


# --------------------------------------------------------------------------- #
# authorization
# --------------------------------------------------------------------------- #
def _authorize_tape(tape: Path | str) -> tuple[Path, str]:
    """Resolve the authorized slice and verify its bytes; refuse anything else.

    Authorization is by identity (the slice name inside a ``research/tape``
    layout) *and* by integrity (the file must hash to the digest its own manifest
    records). A renamed copy of some other data, or a mutated slice, is refused
    rather than measured.
    """
    path = Path(tape)
    directory = path if path.is_dir() else path.parent
    if directory.name != AUTHORIZED_DEVELOPMENT_SLICE:
        raise SimArmRefused(
            "TAPE_NOT_AUTHORIZED",
            f"{directory.name!r} is not the authorized development slice "
            f"{AUTHORIZED_DEVELOPMENT_SLICE!r}",
        )
    if (
        directory.parent.name != _SLICE_ROOT_NAMES[1]
        or directory.parent.parent.name != _SLICE_ROOT_NAMES[0]
    ):
        raise SimArmRefused(
            "TAPE_NOT_AUTHORIZED",
            f"{directory} is not laid out as research/tape/{AUTHORIZED_DEVELOPMENT_SLICE}; "
            "a relabelled copy is not the authorized slice",
        )
    jsonl = directory / "tape.jsonl"
    if not jsonl.is_file():
        raise SimArmRefused("TAPE_NOT_AUTHORIZED", f"{jsonl} does not exist")
    manifest_path = directory / "tape.manifest.json"
    if not manifest_path.is_file():
        raise SimArmRefused(
            "TAPE_INTEGRITY_MISMATCH", f"{manifest_path} is missing; the slice is unverified"
        )
    manifest = json.loads(manifest_path.read_text())
    declared = manifest.get("tape_sha256")
    actual = sha256_file(jsonl)
    if not isinstance(declared, str) or declared != actual:
        raise SimArmRefused(
            "TAPE_INTEGRITY_MISMATCH",
            f"tape.jsonl hashes to {actual} but the manifest declares {declared!r}",
        )
    return jsonl, actual


def _manifest_sha(directory: Path) -> str | None:
    manifest = directory / "tape.manifest.json"
    return sha256_file(manifest) if manifest.is_file() else None


# --------------------------------------------------------------------------- #
# measurement
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SimArmMeasurement:
    """One costed arm of the V9-vs-V8 comparison.

    ``costed`` is the gate: every numeric field is ``None`` when it was not
    measured, and a measured zero stays a zero. ``to_arm_result`` converts the
    measurement into the repository's ablation ``ArmResult`` contract.
    """

    arm_id: str
    arm_kind: str | None
    costed: bool
    trades: int
    net_pnl: float
    net_r: float | None
    net_r_samples: tuple[float, ...]
    max_drawdown: float | None
    hit_rate: float | None
    fee_total: float
    funding_total: float | None
    open_campaigns: int
    risk_basis: str
    bars: int
    decision_count: int
    orders: tuple[dict[str, Any], ...]
    decisions: tuple[dict[str, Any], ...]
    trade_digest: str
    orders_digest: str
    decisions_digest: str
    protection: dict[str, Any]
    execution: dict[str, Any]
    funding_coverage: str
    provenance: Provenance
    unsupported: tuple[str, ...] = ()

    def to_arm_result(self, *, intervention: Intervention | None = None) -> ArmResult:
        """The ablation-contract projection of this measurement."""
        low, high = bootstrap_mean_ci(
            self.net_r_samples, resamples=BOOTSTRAP_RESAMPLES, seed=BOOTSTRAP_SEED
        )
        return ArmResult(
            arm_id=self.arm_id,
            intervention=intervention,
            costed=self.costed,
            trades=self.trades,
            net_r=self.net_r,
            net_pnl=self.net_pnl,
            max_drawdown=self.max_drawdown,
            hit_rate=self.hit_rate,
            fee_total=self.fee_total,
            funding_total=self.funding_total,
            ci_low=low,
            ci_high=high,
            ci_method=(
                f"BOOTSTRAP_PERCENTILE_{int(CI_LEVEL * 100)}_OF_MEAN "
                f"(resamples={BOOTSTRAP_RESAMPLES}, seed={BOOTSTRAP_SEED}, "
                "unit=per-campaign net R)"
            ),
            provenance=self.provenance,
            net_r_samples=self.net_r_samples,
            risk_basis=self.risk_basis,
            open_campaigns=self.open_campaigns,
            unsupported=self.unsupported,
            arm_kind=self.arm_kind,
        )


def _engine_version() -> str:
    try:
        return importlib.metadata.version("nautilus_trader")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover - env defect
        return "UNKNOWN"


def _canonical(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _sha(payload: object) -> str:
    return hashlib.sha256(_canonical(payload).encode()).hexdigest()


def _commission_total(fill_records: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> tuple[float, int]:
    total = _ZERO
    unparsable = 0
    for record in fill_records:
        commissions = record.get("commissions") or []
        if not commissions:
            unparsable += 1
            continue
        for entry in commissions:
            parsed = parse_money(entry)
            if parsed is None:
                unparsable += 1
            else:
                total += parsed
    return float(total), unparsable


def _funding_total(positions: list[dict[str, Any]]) -> tuple[float, int]:
    """Funding cost carried by the engine's own position adjustments."""
    total = _ZERO
    counted = 0
    unparsable = 0
    for position in positions:
        for adjustment in position.get("adjustments") or []:
            if str(adjustment.get("adjustment_type", "")).upper() != "FUNDING":
                continue
            parsed = parse_money(adjustment.get("pnl_change"))
            if parsed is None:
                unparsable += 1
                continue
            total += parsed
            counted += 1
    return float(total), unparsable


def _campaign_metrics(
    *,
    opened: list[dict[str, Any]],
    closed: list[dict[str, Any]],
    initial_balance: Decimal,
    bracket_stop_pct: Decimal,
) -> dict[str, Any]:
    """Per-campaign net R, hit rate and drawdown from native position records."""
    pairs = pair_positions(list(opened), list(closed))
    samples: list[float] = []
    pnls: list[tuple[int, float]] = []
    unsupported: list[str] = []
    open_campaigns = 0
    for open_record, close_record in pairs:
        quantity = parse_money(open_record.get("quantity"))
        entry = parse_money(open_record.get("avg_px_open"))
        if quantity is None or entry is None:
            unsupported.append("CAMPAIGN_ENTRY_UNPARSABLE")
            continue
        risk = abs(quantity * entry) * bracket_stop_pct
        if close_record is None:
            open_campaigns += 1
            continue
        pnl = parse_money(close_record.get("realized_pnl"))
        if pnl is None or risk <= 0:
            unsupported.append("CAMPAIGN_NET_PNL_UNPARSABLE")
            continue
        samples.append(float(pnl / risk))
        pnls.append((int(close_record.get("event_ns", 0) or 0), float(pnl)))
    pnls.sort(key=lambda item: item[0])
    equity = float(initial_balance)
    peak = equity
    max_drawdown = 0.0
    for _, campaign_pnl in pnls:
        equity += campaign_pnl
        peak = max(peak, equity)
        if peak > 0.0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
    hit_rate = sum(1 for value in samples if value > 0.0) / len(samples) if samples else None
    return {
        "trades": len(samples),
        "net_r": (sum(samples) / len(samples)) if samples else None,
        "net_r_samples": tuple(samples),
        "max_drawdown": max_drawdown if samples else None,
        "hit_rate": hit_rate,
        "open_campaigns": open_campaigns,
        "unsupported": tuple(unsupported),
    }


def _trade_digest(opened: list[dict[str, Any]]) -> str:
    """Identity of the executed trades, in time order: equal digests == equal trades."""
    rows = sorted(
        (
            str(record.get("instrument_id", "")),
            str(record.get("side", "")),
            str(record.get("quantity", "")),
            str(record.get("avg_px_open", "")),
            int(record.get("event_ns", 0) or 0),
        )
        for record in opened
    )
    return _sha([list(row) for row in rows])


# --------------------------------------------------------------------------- #
# engine driver
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _Window:
    instrument_id: str
    raw_symbol: str
    bar_type: BarType
    candles: tuple[Candle, ...]
    funding: tuple[FundingRow, ...]


def _window(loaded: MultiTape, config: SimArmConfig, limit: int | None) -> _Window:
    raw_symbol = config.instrument_id.split(".")[0].replace("-PERP", "")
    if raw_symbol not in loaded.candles:
        raise SimArmRefused(
            "TAPE_INSTRUMENT_MISSING",
            f"{raw_symbol!r} is absent from the tape; available: {sorted(loaded.candles)}",
        )
    candles = loaded.candles[raw_symbol]
    if limit is not None:
        if limit <= 0:
            raise SimArmRefused("TAPE_EMPTY", f"limit must be positive, got {limit}")
        candles = candles[:limit]
    if len(candles) < 2:
        raise SimArmRefused("TAPE_EMPTY", f"window carries {len(candles)} bar(s)")
    funding = tuple(row for row in loaded.funding if row.instrument == raw_symbol)
    return _Window(
        instrument_id=config.instrument_id,
        raw_symbol=raw_symbol,
        bar_type=BarType.from_str(config.bar_type_str),
        candles=candles,
        funding=funding,
    )


def _drive(
    *,
    window: _Window,
    strategy: AdaptiveSimStrategy | ExpertEnsembleStrategy,
    funding_dropped_at_load: int,
    initial_balance: Decimal,
    maker_fee: Decimal,
    taker_fee: Decimal,
    execution_profile: str,
    currency: str = "USDT",
    venue: str = "BINANCE",
) -> dict[str, Any]:
    """Run one strategy through the BacktestEngine over the prepared window."""
    curr = Currency.from_str(currency)
    ven = Venue(venue)
    profile = resolve_profile(execution_profile)
    engine = BacktestEngine(
        BacktestEngineConfig(
            bypass_logging=True,
            logging=LoggerConfig(stdout_level=LogLevel.WARNING),
        )
    )
    try:
        engine.add_venue(
            ven,
            OmsType.NETTING,
            AccountType.MARGIN,
            [Money(float(initial_balance), curr)],
            default_leverage=Decimal(1),
            liquidation_enabled=False,
            **dict(venue_kwargs(profile)),
        )
        engine.add_instrument(
            _instrument(
                window.instrument_id,
                window.raw_symbol,
                base_currency(window.raw_symbol),
                curr,
                maker_fee,
                taker_fee,
            )
        )
        engine.add_data(_bars(window.candles, window.bar_type))

        window_start = window.candles[0].end_ns
        window_end = window.candles[-1].end_ns
        prepared, funding_counts = _plan_funding_events(
            window.funding,
            {window.raw_symbol: window.candles},
            window_start,
            window_end,
        )
        for effective, boundary, row, fund_iid, mark in prepared:
            engine.add_data(
                [MarkPriceUpdate(fund_iid, Price(float(mark), 2), boundary, boundary)]
            )
            engine.add_data(
                [
                    FundingRateUpdate(
                        fund_iid,
                        row.funding_rate,
                        effective,
                        effective,
                        next_funding_ns=effective,
                    )
                ]
            )

        engine.add_strategy(strategy)
        engine.run()
        if isinstance(strategy, AdaptiveSimStrategy):
            strategy.finish()

        account = economic_state(engine, ven, curr)
        fill_records, fill_report_type = native_fill_records(engine)
        return {
            "account": account,
            "fill_records": fill_records,
            "fill_report_type": fill_report_type,
            "profile": profile,
            "funding_settlements_fed": len(prepared),
            "funding_rows_available": len(window.funding),
            "funding_out_of_window": funding_counts["out_of_window"],
            "funding_missing_mark": funding_counts["missing_mark"],
            "funding_receipt_shifted": funding_counts["receipt_shifted"],
            "funding_receipt_unknown": funding_counts["receipt_unknown"],
            "funding_dropped_at_load": funding_dropped_at_load,
        }
    finally:
        engine.dispose()


def _audit_protection(
    events: list[ProtectionEvent],
) -> tuple[BracketInvariantChecker, list[dict[str, Any]]]:
    """Audit every observed callback; no assumed atomicity across callbacks.

    Transitions describe observed states, not duration or proof that the event
    producer captured every venue callback. An unacknowledged order stays a gap
    until an acceptance or fill is observed.
    """
    checker = BracketInvariantChecker()
    transitions: list[dict[str, Any]] = []
    previous: dict[str, tuple[str, str]] = {}
    for event in events:
        checker.apply(event)
        positions = checker.snapshot()["positions"]
        assert isinstance(positions, dict)
        for position_id in positions:
            quantity = checker.position_quantity(position_id)
            state = checker.state_of(position_id).value
            observed = (state, str(quantity))
            if previous.get(position_id) != observed:
                transitions.append(
                    {
                        "ts_ns": event.ts_ns,
                        "event_kind": event.kind.value,
                        "position_id": position_id,
                        "quantity": str(quantity),
                        "state": state,
                    }
                )
                previous[position_id] = observed
    return checker, transitions


def _measure(
    *,
    arm_id: str,
    arm_kind: str | None,
    strategy: AdaptiveSimStrategy | ExpertEnsembleStrategy,
    result: dict[str, Any],
    window: _Window,
    config: SimArmConfig,
    tape_sha256: str | None,
    tape_dir: Path,
    initial_balance: Decimal,
    maker_fee: Decimal,
    taker_fee: Decimal,
    execution_profile: str,
) -> SimArmMeasurement:
    account = result["account"]
    fill_records = result["fill_records"]
    positions = account.get("positions") or []
    metrics = _campaign_metrics(
        opened=list(strategy.opened_positions),
        closed=list(strategy.closed_positions),
        initial_balance=initial_balance,
        bracket_stop_pct=config.bracket_stop_pct,
    )
    unsupported: list[str] = list(metrics["unsupported"])

    balance = parse_money(account.get("balance_total"))
    if balance is None:
        raise SimArmRefused("MEASUREMENT_INCOMPLETE", "the native account balance is unreadable")
    net_pnl = float(balance - initial_balance)

    fee_total, unparsable_fees = _commission_total(fill_records)
    if unparsable_fees:
        unsupported.append(f"FEE_TOTAL_PARTIAL:{unparsable_fees} fill(s) without a parsable commission")
    funding_total: float | None
    funding_total, unparsable_funding = _funding_total(positions)
    if unparsable_funding:
        unsupported.append(f"FUNDING_TOTAL_PARTIAL:{unparsable_funding} adjustment(s) unparsable")
    if not window.funding:
        funding_total = None
        unsupported.append(
            "FUNDING_UNSUPPORTED_NO_ROWS: tape carries no funding rows for this instrument; "
            "a zero here would be absence, not a measured cost"
        )
    elif result["funding_settlements_fed"] == 0:
        unsupported.append("FUNDING_UNSUPPORTED_NO_SETTLEMENTS_IN_WINDOW")

    decisions: list[dict[str, Any]]
    orders: list[dict[str, Any]]
    if isinstance(strategy, AdaptiveSimStrategy):
        ledger = strategy.order_ledger()
        decisions = ledger["decisions"]
        orders = ledger["orders"]
        decisions_digest = str(ledger["decisions_digest"])
        orders_digest = str(ledger["digest"])
        protection_events = list(strategy.protection_events)
        checker, protection_transitions = _audit_protection(protection_events)
        # The producer still buffers some native callbacks before publishing
        # them, so even a clean observed ledger cannot establish continuous
        # coverage. A cutoff snapshot is not a lifecycle qualification.
        unsupported.append("PROTECTION_NOT_QUALIFIED_BUFFERED_NATIVE_CALLBACKS")
        protection = {
            "state": str(checker.state),
            "qualification": "NOT_QUALIFIED",
            "violations": [
                {
                    "code": violation.code.value,
                    "ts_ns": violation.ts_ns,
                    "position_id": violation.position_id,
                    "client_order_id": violation.client_order_id,
                    "detail": violation.detail,
                }
                for violation in checker.violations
            ],
            "events_applied": checker.events_applied,
            "transitions": protection_transitions,
            "native_callback_trace": list(strategy.native_callback_trace),
            "snapshot": checker.snapshot(),
            "digest": checker.digest(),
        }
        arm_kind_value = arm_kind
    else:
        decisions = []
        orders = [
            {
                "client_order_id": str(order.get("client_order_id", "")),
                "decision_id": None,
                "role": str(order.get("side", "")),
                "sim_arm": False,
                "advisory_only": None,
            }
            for order in account.get("orders") or []
        ]
        decisions_digest = _sha([])
        orders_digest = _sha([order["client_order_id"] for order in orders])
        protection = {"state": "NOT_APPLICABLE", "violations": [], "events_applied": 0, "digest": ""}
        arm_kind_value = None

    execution = execution_telemetry(
        result["profile"],
        fill_records,
        result["fill_report_type"],
        list(strategy.opened_positions),
        [{**row, "instrument_id": window.instrument_id} for row in decisions],
    )
    slippage_basis = (
        f"PROFILE={execution.get('profile')} FILL_MODEL={execution.get('fill_model')} "
        f"SLIPPED={execution.get('fill_model_slipped')} "
        f"PROB_SLIPPAGE={execution.get('prob_slippage')} "
        f"MEASURED_SLIPPAGE_BPS_MEAN={execution.get('slippage_bps_mean')}; "
        "a model assumption, not venue slippage"
    )
    partial = 0
    for record in fill_records:
        quantity = parse_money(record.get("quantity"))
        filled = parse_money(record.get("filled_qty"))
        if quantity is not None and filled is not None and filled < quantity:
            partial += 1
    provenance = Provenance(
        tape_sha256=tape_sha256 or "",
        tape_manifest_sha256=_manifest_sha(tape_dir),
        config_sha256=_config_sha(config),
        engine="nautilus_trader.BacktestEngine",
        engine_version=_engine_version(),
        execution_profile=str(execution.get("profile") or execution_profile),
        execution_profile_digest=str(
            execution.get("digest") or profile_digest(execution_profile)
        ),
        fee_model=str(execution.get("fee_model") or "maker_taker"),
        maker_fee=str(maker_fee),
        taker_fee=str(taker_fee),
        funding_basis=(
            f"TAPE_FUNDING_ROWS={result['funding_rows_available']} "
            f"SETTLEMENTS_FED={result['funding_settlements_fed']} "
            f"RECEIPT_SHIFTED={result['funding_receipt_shifted']} "
            f"DROPPED_AT_LOAD={result['funding_dropped_at_load']} "
            "MARK=OHLC_CLOSE_ASOF_BOUNDED_HISTORICAL_MODEL_NOT_VENUE_MARK "
            "ACCOUNTING=ENGINE_NATIVE_POSITION_ADJUSTMENTS"
        ),
        mark_basis="OHLC_CLOSE_ASOF_BOUNDED_HISTORICAL_MODEL_NOT_VENUE_MARK",
        intrabar_tie_basis=INTRABAR_TIE_BASIS,
        slippage_basis=slippage_basis,
        partial_fills_basis=(
            f"ORDER_FILL_REPORT_ROWS={len(fill_records)} PARTIAL_ORDERS={partial}; "
            "bar execution fills the submitted quantity at the bar price and no captured "
            "L2/L3 depth limits size"
        ),
    )
    return SimArmMeasurement(
        arm_id=arm_id,
        arm_kind=arm_kind_value,
        costed=True,
        trades=int(metrics["trades"]),
        net_pnl=net_pnl,
        net_r=metrics["net_r"],
        net_r_samples=tuple(metrics["net_r_samples"]),
        max_drawdown=metrics["max_drawdown"],
        hit_rate=metrics["hit_rate"],
        fee_total=fee_total,
        funding_total=funding_total,
        open_campaigns=int(metrics["open_campaigns"]),
        risk_basis=(
            "BRACKET_RISK: per-campaign risk = entry_notional * "
            f"bracket_stop_pct({config.bracket_stop_pct})"
        ),
        bars=len(window.candles),
        decision_count=len(decisions),
        orders=tuple(orders),
        decisions=tuple(decisions),
        trade_digest=_trade_digest(list(strategy.opened_positions)),
        orders_digest=orders_digest,
        decisions_digest=decisions_digest,
        protection=protection,
        execution=execution,
        funding_coverage=(
            "NO_FUNDING_ROWS"
            if not window.funding
            else "FULL_BOUNDARY_RECEIPT_ATTRIBUTED"
            if result["funding_receipt_unknown"] == 0
            else "FULL_WITH_UNKNOWN_RECEIPT_TIME"
        ),
        provenance=provenance,
        unsupported=tuple(sorted(set(unsupported))),
    )


def _config_sha(config: SimArmConfig) -> str:
    return _sha(
        {
            "enabled": True,
            "instrument_id": config.instrument_id,
            "bar_type_str": config.bar_type_str,
            "grammar_policy": config.grammar_policy,
            "min_support_quorum": config.min_support_quorum,
            "max_contradiction_tolerance": config.max_contradiction_tolerance,
            "order_quantity": str(config.order_quantity),
            "target_notional": (
                None if config.target_notional is None else str(config.target_notional)
            ),
            "bracket_stop_pct": str(config.bracket_stop_pct),
            "bracket_target_pct": str(config.bracket_target_pct),
            "max_notional_per_order": (
                None
                if config.max_notional_per_order is None
                else str(config.max_notional_per_order)
            ),
            "max_concurrent_positions": config.max_concurrent_positions,
        }
    )


# --------------------------------------------------------------------------- #
# public arms
# --------------------------------------------------------------------------- #
def run_sim_arm(
    *,
    tape: Path | str,
    config: SimArmConfig,
    limit: int | None = None,
    initial_balance: Decimal = Decimal("10000"),
    maker_fee: Decimal = Decimal("0.0002"),
    taker_fee: Decimal = Decimal("0.0005"),
    execution_profile: str = "baseline",
) -> SimArmMeasurement:
    """Measure the V9 adaptive arm on the authorized development slice.

    Refuses ``SIM_ARM_DISABLED`` unless ``config.enabled`` is explicitly ``True``
    and ``TAPE_NOT_AUTHORIZED``/``TAPE_INTEGRITY_MISMATCH`` unless the tape is the
    integrity-checked authorized slice.
    """
    if not isinstance(config, SimArmConfig):
        raise TypeError(f"expected SimArmConfig, got {type(config).__name__}")
    if config.enabled is not True:
        raise SimArmDisabled()
    jsonl, tape_sha = _authorize_tape(tape)
    loaded = load_multitape(jsonl, limit=limit)
    window = _window(loaded, config, limit)
    strategy = AdaptiveSimStrategy(
        config,
        source_candles={candle.end_ns: candle for candle in window.candles},
    )
    result = _drive(
        window=window,
        strategy=strategy,
        funding_dropped_at_load=loaded.funding_dropped,
        initial_balance=initial_balance,
        maker_fee=maker_fee,
        taker_fee=taker_fee,
        execution_profile=execution_profile,
    )
    return _measure(
        arm_id=ADAPTIVE_ARM_ID,
        arm_kind=V9_INTERVENTION_KIND,
        strategy=strategy,
        result=result,
        window=window,
        config=config,
        tape_sha256=tape_sha,
        tape_dir=jsonl.parent,
        initial_balance=initial_balance,
        maker_fee=maker_fee,
        taker_fee=taker_fee,
        execution_profile=execution_profile,
    )


def run_baseline_arm(
    *,
    tape: Path | str,
    config: SimArmConfig,
    limit: int | None = None,
    initial_balance: Decimal = Decimal("10000"),
    maker_fee: Decimal = Decimal("0.0002"),
    taker_fee: Decimal = Decimal("0.0005"),
    execution_profile: str = "baseline",
    baseline_config: ExpertStrategyConfig | None = None,
) -> SimArmMeasurement:
    """Measure the V8 expert ensemble on the identical window and parameters.

    ``config`` is the same object handed to :func:`run_sim_arm`; the ensemble is
    built from ``config.baseline_config()`` so sizing, stop/target, quorum and the
    risk gate cannot drift between the arms. A explicit ``baseline_config`` is
    accepted only when it is the byte-identical mapping (used by tests).
    """
    if not isinstance(config, SimArmConfig):
        raise TypeError(f"expected SimArmConfig, got {type(config).__name__}")
    expected = config.baseline_config()
    if baseline_config is not None and baseline_config != expected:
        raise SimArmRefused(
            "BASELINE_CONFIG_DIVERGED",
            "the baseline config must be the mapping of the same SimArmConfig; "
            "a different baseline would confound the comparison",
        )
    jsonl, tape_sha = _authorize_tape(tape)
    loaded = load_multitape(jsonl, limit=limit)
    window = _window(loaded, config, limit)
    strategy = ExpertEnsembleStrategy(
        expected, source_candles={candle.end_ns: candle for candle in window.candles}
    )
    result = _drive(
        window=window,
        strategy=strategy,
        funding_dropped_at_load=loaded.funding_dropped,
        initial_balance=initial_balance,
        maker_fee=maker_fee,
        taker_fee=taker_fee,
        execution_profile=execution_profile,
    )
    return _measure(
        arm_id=BASELINE_ARM_ID,
        arm_kind=None,
        strategy=strategy,
        result=result,
        window=window,
        config=config,
        tape_sha256=tape_sha,
        tape_dir=jsonl.parent,
        initial_balance=initial_balance,
        maker_fee=maker_fee,
        taker_fee=taker_fee,
        execution_profile=execution_profile,
    )
