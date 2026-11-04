"""Prospective capture and bounded native paper replay. No private execution."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from nautilus_trader.model import (
    Currency,
    FundingRateUpdate,
    InstrumentId,
    MarkPriceUpdate,
    Price,
    Quantity,
    QuoteTick,
    Venue,
)

from v8_next.adapters.accounting_replay import replay_frozen_campaigns
from v8_next.adapters.binance_capture import capture, verify
from v8_next.adapters.captured_market import (
    load_account_ratio,
    load_candles,
    load_open_interest,
    load_settled_funding,
)
from v8_next.adapters.economic_paper import EconomicPaperAdapter
from v8_next.adapters.engine_state import economic_state, reconcile_replay
from v8_next.adapters.execution_models import resolve_profile
from v8_next.adapters.execution_telemetry import (
    execution_friction_inputs,
    execution_telemetry,
    fill_signature,
    load_execution_telemetry,
    native_fill_records,
    persist_execution_telemetry,
)
from v8_next.adapters.native_tape import build_engine, capture_native_inputs
from v8_next.adapters.settlements import (
    FinalFunding,
    final_funding,
    funding_query_windows,
    missing_announced_settlements,
    position_funding_query_coverage,
)
from v8_next.app.observe import initialize, observe_capture
from v8_next.domain.campaign import PaperCampaign
from v8_next.domain.config import PaperConfig
from v8_next.domain.experiment import RulePaperExperiment
from v8_next.domain.market import CausalFrame, frame_at
from v8_next.domain.positioning import PositioningReading
from v8_next.economics.controller import InstrumentConstraints
from v8_next.economics.decisions import UtilityInputs
from v8_next.evaluation.store import ResearchStore, canonical
from v8_next.risk.admission import RiskLimits


def build_funding_reconciliation(
    manifest_receipts: tuple[tuple[int, Path], ...],
    settlements: tuple[FinalFunding, ...],
    session_start_ns: int,
) -> Callable[[list[dict[str, Any]], int], dict[str, Any]]:
    """Readmit closed lifetimes only under verified bounded funding coverage.

    Manifests become known by their quote receipt; coverage is evaluated with
    records known by each decision, never future receipts. Every lifetime must
    be fully covered by contiguous bounded responses and no announced
    settlement may be missing. Anything else fails closed. Query coverage is
    not venue cash-settlement finality and never mints economic authority.
    """

    def reconciled(
        lifetimes: list[dict[str, Any]], decision_ns: int
    ) -> dict[str, Any]:
        known = [path for received, path in manifest_receipts if received <= decision_ns]
        if not lifetimes:
            return {"reconciled": False, "reason": "NO_LIFETIME_HISTORY"}
        if not known:
            return {"reconciled": False, "reason": "NO_KNOWN_SOURCE"}
        windows = funding_query_windows(known, decision_ns)
        coverage = position_funding_query_coverage(lifetimes, windows, decision_ns)
        records = tuple(s for s in settlements if s.received_ns <= decision_ns)
        missing = missing_announced_settlements(known, records, session_start_ns, decision_ns)
        verdict = all(
            item["query_status"] == "BOUNDED_RESPONSE_COVERS_EXPOSURE" for item in coverage
        ) and missing == ()
        return {
            "reconciled": verdict,
            "coverage": coverage,
            "missing_announced_settlements": missing,
            "known_manifests": len(known),
            "known_settlements": len(records),
        }

    return reconciled


def _refuse_unattributed_delayed_funding(
    settlements: tuple[FinalFunding, ...], positions: list[dict[str, Any]]
) -> None:
    """Refuse a paper result whose delayed funding lands on the wrong position.

    A settlement that arrives after its boundary is re-timed to receipt by the
    accounting path. That is only sound when the position open at the boundary is
    the same position open at receipt: then the charge is still associated with
    the exposure that incurred it. If the exposure changed in between, the charge
    would be attributed to a different trade and no exposure snapshot is
    available to split it, so the run is refused instead of published.
    """

    def exposed_at(position: dict[str, Any], ns: int) -> bool:
        opened = int(position["opened_ns"])
        closed = int(position.get("closed_ns") or 0)
        # Equal-time close/funding ordering is not established by this snapshot;
        # treating the boundary instant as exposed is the conservative choice.
        return opened <= ns and (closed == 0 or ns <= closed)

    for settlement in settlements:
        if settlement.received_ns == settlement.boundary_ns:
            continue
        relevant = [
            (index, position)
            for index, position in enumerate(positions)
            if position["instrument_id"] == settlement.instrument_id
        ]
        at_boundary = {i for i, p in relevant if exposed_at(p, settlement.boundary_ns)}
        at_receipt = {i for i, p in relevant if exposed_at(p, settlement.received_ns)}
        if at_boundary != at_receipt:
            raise ValueError(
                "delayed funding boundary exposure accounting unavailable; "
                "refusing an unreconciled paper result"
            )


def replay_account(
    manifests: list[Path],
    config: dict[str, Any],
    *,
    observer: str | None = None,
    experiment_frozen_ns: int | None = None,
    execution_profile: str | None = None,
) -> dict[str, Any]:
    parsed = PaperConfig.model_validate(config)
    experiment = None
    if parsed.experiment_window is not None:
        if experiment_frozen_ns is None:
            raise ValueError("experiment replay requires verified policy freeze time")
        experiment = RulePaperExperiment(experiment_frozen_ns, *parsed.experiment_window)
    selected_observer = observer if observer is not None else parsed.observer_policy
    if not manifests:
        raise ValueError("no prospective captures")
    quotes = []
    frames: dict[int | tuple[str, int], CausalFrame] = {}
    positioning_readings: list[PositioningReading] = []
    instrument_manifests: dict[str, Path] = {}
    manifest_receipts: list[tuple[int, Path]] = []
    for manifest in manifests:
        verify(manifest)
        metadata = json.loads(manifest.read_text())
        artifact = next(a for a in metadata["artifacts"] if a["path"] == "quote.json")
        row = json.loads((manifest.parent / "quote.json").read_text())
        if row["symbol"] not in parsed.symbols:
            raise ValueError("capture outside configured paper symbols")
        instrument_id = row["symbol"] + "-PERP.BINANCE"
        instrument_manifests.setdefault(instrument_id, manifest)
        received = int(artifact["received_time_ns"])
        manifest_receipts.append((received, manifest))
        event = int(row["time"]) * 1_000_000
        if event > received:
            raise ValueError("venue clock ahead of local receipt; clock qualification required")
        if Decimal(row["bidPrice"]) > Decimal(row["askPrice"]):
            raise ValueError("crossed quote")
        if parsed.funding_max_age_ns is not None:
            positioning_readings.extend(
                load_settled_funding(manifest, max_age_ns=parsed.funding_max_age_ns)
            )
        if parsed.open_interest_max_age_ns is not None:
            positioning_readings.extend(
                load_open_interest(manifest, max_age_ns=parsed.open_interest_max_age_ns)
            )
        if parsed.account_ratio_period is not None and parsed.account_ratio_max_age_ns is not None:
            positioning_readings.extend(
                load_account_ratio(
                    manifest,
                    period=parsed.account_ratio_period,
                    max_age_ns=parsed.account_ratio_max_age_ns,
                )
            )
        candles = load_candles(manifest)
        known = tuple(replace(c, available_ns=c.received_ns) for c in candles)
        key = (instrument_id, received)
        if key in frames:
            raise ValueError("duplicate paper instrument receipt")
        frames[key] = frame_at(instrument_id, received, known)
        quotes.append(
            QuoteTick(
                InstrumentId.from_str(instrument_id),
                Price.from_str(row["bidPrice"]),
                Price.from_str(row["askPrice"]),
                Quantity.from_str(row["bidQty"]),
                Quantity.from_str(row["askQty"]),
                event,
                received,
            )
        )
    engine, engine_meta = build_engine(
        manifests[0],
        Decimal(config["maker_fee"]),
        Decimal(config["taker_fee"]),
        Decimal(config["initial_balance"]),
        historical_data=False,
        execution_profile=execution_profile,
    )
    try:
        constraints_by_instrument = {}
        for index, (instrument_id, manifest) in enumerate(instrument_manifests.items()):
            if index:
                _, native_instrument, _, _ = capture_native_inputs(
                    manifest, parsed.maker_fee, parsed.taker_fee
                )
                engine.add_instrument(native_instrument)
            metadata = json.loads((manifest.parent / "instruments.json").read_text())
            symbol = instrument_id.split("-")[0]
            instrument = next(i for i in metadata["symbols"] if i["symbol"] == symbol)
            filters = {f["filterType"]: f for f in instrument["filters"]}
            lot = filters["MARKET_LOT_SIZE"]
            constraints_by_instrument[instrument_id] = InstrumentConstraints(
                Decimal(lot["stepSize"]),
                Decimal(lot["minQty"]),
                Decimal(lot["maxQty"]),
                Decimal(filters["MIN_NOTIONAL"]["notional"]),
            )
        calibration_provider = None
        if parsed.calibration_source_run is not None:
            source_run_path = Path(parsed.calibration_source_run)

            def calibration_provider(
                opportunity: Any, decision_ns: int
            ) -> tuple[UtilityInputs, bool]:
                from v8_next.evaluation.calibration import inspect_calibration_source

                inspection = inspect_calibration_source(source_run_path, decision_ns)
                if not inspection.get("eligible_for_utility", False):
                    return (
                        UtilityInputs(None, None, None, None, None, None, None),
                        False,
                    )
                # Friction comes from the source run's own measured execution
                # telemetry. If that artifact is absent, malformed, or fails its
                # digest, every field stays None so the decision keeps failing
                # closed on missing calibration instead of assuming a free cost.
                friction = execution_friction_inputs(
                    load_execution_telemetry(source_run_path / "execution_telemetry.json")
                )
                return (
                    UtilityInputs(
                        gross_edge=inspection.get("gross_edge"),
                        fees=friction.get("fees"),
                        spread=friction.get("spread"),
                        slippage=friction.get("slippage"),
                        funding_cost=friction.get("funding_cost"),
                        uncertainty=inspection.get("uncertainty"),
                        calibration_receipt=friction.get("calibration_receipt"),
                    ),
                    True,
                )

        quotes.sort(key=lambda q: (q.ts_init, str(q.instrument_id), q.ts_event))
        # Never apply a delayed settlement to a receipt-time native position.
        # No backdated accounting updates enter the decision engine. Until an
        # independently reconciled boundary-exposure ledger exists, position
        # bearing delayed settlements cause the paper result to be refused.
        online_inputs: list[tuple[int, int, object]] = [(q.ts_init, 0, q) for q in quotes]
        funding_settlements = final_funding(
            manifests,
            min(q.ts_event for q in quotes),
            max(q.ts_init for q in quotes),
            max(q.ts_init for q in quotes),
        )
        for settlement in funding_settlements:
            if settlement.received_ns != settlement.boundary_ns:
                continue
            instrument = InstrumentId.from_str(settlement.instrument_id)
            online_inputs.append(
                (
                    settlement.received_ns,
                    1,
                    MarkPriceUpdate(
                        instrument,
                        Price.from_str(str(settlement.mark_price)),
                        settlement.received_ns,
                        settlement.received_ns,
                    ),
                )
            )
            online_inputs.append(
                (
                    settlement.received_ns,
                    2,
                    FundingRateUpdate(
                        instrument,
                        settlement.rate,
                        settlement.received_ns,
                        settlement.received_ns,
                        next_funding_ns=settlement.received_ns,
                    ),
                )
            )
        strategy = EconomicPaperAdapter(
            frames,
            RiskLimits(
                Decimal(1),
                Decimal(config["max_exposure_fraction"]),
                Decimal(config["max_notional"]),
                0,
            ),
            constraints_by_instrument,
            Decimal(config["max_notional"]),
            calibration=calibration_provider,
            observer=selected_observer,
            grammar=parsed.grammar_policy,
            campaign_policy=parsed.campaign_policy,
            stop_budget=parsed.stop_budget,
            positioning_readings=tuple(positioning_readings),
            experiment=experiment,
            funding_reconciliation=build_funding_reconciliation(
                tuple(manifest_receipts),
                funding_settlements,
                min(q.ts_event for q in quotes),
            ),
        )
        engine.add_strategy(strategy)
        online_inputs.sort(key=lambda item: (item[0], item[1]))
        engine.add_data([item[2] for item in online_inputs])
        engine.run()
        if strategy.callback_failure is not None:
            raise ValueError(f"paper callback failed: {strategy.callback_failure}")
        state = economic_state(engine, Venue("BINANCE"), Currency.from_str("USDT"))
        _refuse_unattributed_delayed_funding(funding_settlements, state["positions"])
        state["quote_count"] = len(quotes)
        state["instruments"] = sorted(instrument_manifests)
        state["campaign_status"] = (
            "RULE_EXPERIMENT_NOT_CALIBRATED" if experiment else "NO_VERIFIED_CALIBRATION"
        )
        state["economic_decisions"] = strategy.decisions
        state["campaigns"] = [c.to_record() for c in strategy.campaigns]
        state["campaign_observations"] = strategy.campaign_observations(state)
        state["funding_status"] = (
            "UNVERIFIED_ONLINE_FUNDING"
            if state["positions"]
            else "NO_EXPOSURE_IN_SESSION_NO_FUNDING_LIABILITY"
        )

        def _open_at(position: dict[str, Any], ns: int) -> bool:
            closed = int(position.get("closed_ns") or 0)
            return int(position["opened_ns"]) <= ns and (closed == 0 or ns < closed)

        # A settlement enters the engine as an event; with no position open at
        # that instant it is a native no-op. Count only the settlements that
        # actually had exposure, never every record selected by the window.
        state["online_funding_settlements_applied"] = sum(
            1
            for settlement in funding_settlements
            if settlement.boundary_ns == settlement.received_ns and any(
                position["instrument_id"] == settlement.instrument_id
                and _open_at(position, int(settlement.received_ns))
                for position in state["positions"]
            )
        )
        state["online_funding_settlements"] = [
            {
                "instrument_id": s.instrument_id,
                "boundary_ns": s.boundary_ns,
                "received_ns": s.received_ns,
                "rate": str(s.rate),
                "mark_price": str(s.mark_price),
                "source_sha256": s.source_sha256,
            }
            for s in funding_settlements
        ]
        state["limitations"] = [
            "sampled_REST_quotes_not_continuous_execution_feed",
            "positive_campaign_and_online_funding_integration_pending",
        ]
        # Measured execution evidence, so a later calibration read can cite the
        # friction this run actually produced instead of assuming one. The
        # engine metadata records the semantics that were in force.
        fill_records, fill_report_type = native_fill_records(engine)
        if execution_profile is not None:
            state["execution"] = execution_telemetry(
                resolve_profile(execution_profile),
                fill_records,
                fill_report_type,
                getattr(strategy, "opened_positions", []),
                [
                    {**d, "instrument_id": str(d.get("instrument_id", ""))}
                    for d in strategy.decisions
                ],
            )
        else:
            state["execution"] = {
                "profile": None,
                "evidence_class": "UNSPECIFIED_EXECUTION_SEMANTICS",
                "fills_count": len(fill_records),
                "fill_report_type": fill_report_type,
                "fill_signature": fill_signature(fill_records),
            }
        state["execution_semantics"] = engine_meta.get("execution_semantics")
        return state
    finally:
        engine.dispose()


def _acquire_writer_lock(lock: Any) -> None:
    """Acquire an exclusive, non-blocking writer lock on ``lock``.

    ``fcntl`` is Unix-only and ``msvcrt`` is Windows-only, so the platform
    branch lives here rather than at module import time. The lock file itself
    is only a marker; the OS releases the lock if the process exits.
    """
    try:
        if os.name == "nt":
            import importlib

            msvcrt = importlib.import_module("msvcrt")
            lock.seek(0, os.SEEK_END)
            if lock.tell() == 0:
                lock.write("\0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import importlib

            fcntl = importlib.import_module("fcntl")
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        raise RuntimeError("paper session already has an active writer") from error


def _release_writer_lock(lock: Any) -> None:
    if os.name == "nt":
        import importlib

        msvcrt = importlib.import_module("msvcrt")
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import importlib

        fcntl = importlib.import_module("fcntl")
        fcntl.flock(lock, fcntl.LOCK_UN)


def step(
    run: Path,
    config: dict[str, Any],
    *,
    replay_only: bool = False,
    execution_profile: str | None = None,
) -> dict[str, Any]:
    """One local writer per session; the OS releases the lock on process exit."""
    PaperConfig.model_validate(config)
    run.mkdir(parents=True, exist_ok=True)
    with (run / ".paper.lock").open("a+") as lock:
        _acquire_writer_lock(lock)
        try:
            return _step_locked(
                run, config, replay_only=replay_only, execution_profile=execution_profile
            )
        finally:
            _release_writer_lock(lock)


def _step_locked(
    run: Path,
    config: dict[str, Any],
    *,
    replay_only: bool,
    execution_profile: str | None = None,
) -> dict[str, Any]:
    parsed = PaperConfig.model_validate(config)
    frozen = initialize(run, config)
    replay_options: dict[str, Any] = (
        {"experiment_frozen_ns": int(str(frozen["frozen_ns"]))} if parsed.experiment_window else {}
    )
    if execution_profile is not None:
        replay_options["execution_profile"] = execution_profile
    manifests = sorted(run.glob("capture-*/manifest.json"))
    checkpoint = run / "paper-state.json"
    if checkpoint.exists():
        saved = json.loads(checkpoint.read_text())
        if saved["policy_hash"] != frozen["policy_hash"]:
            raise ValueError("paper checkpoint belongs to another policy")
        prior_paths = [(run / name).resolve() for name in saved["manifests"]]
        if any(not path.is_relative_to(run.resolve()) for path in prior_paths):
            raise ValueError("paper checkpoint source escapes run")
        if len(set(prior_paths)) != len(prior_paths):
            raise ValueError("duplicate paper checkpoint source")
        for path, expected in zip(prior_paths, saved["manifest_hashes"], strict=True):
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError("paper capture manifest changed")
        recovered = replay_account(prior_paths, config, **replay_options)
        reconcile_replay(saved["native_state"], recovered)
        prior_campaigns = tuple(PaperCampaign.from_record(c) for c in recovered["campaigns"])
        reconcile_replay(
            saved["revised_accounting"],
            replay_frozen_campaigns(
                prior_paths,
                prior_campaigns,
                config,
                int(saved["revised_accounting"]["accounting_as_of_ns"]),
            ),
        )
    if not replay_only:
        for symbol in parsed.symbols:
            manifests.append(
                capture(
                    run / f"capture-{time.time_ns()}-{symbol}",
                    symbol=symbol,
                    # Retain all session liabilities and the pre-session settled
                    # observation window. Receipt-time gating still applies.
                    funding_start_ms=max(
                        0, int(str(frozen["frozen_ns"])) - (parsed.funding_max_age_ns or 0)
                    )
                    // 1_000_000,
                    include_open_interest=parsed.open_interest_max_age_ns is not None,
                    account_ratio_period=parsed.account_ratio_period,
                )
            )
    if not manifests:
        raise ValueError("no captured session to replay")
    for manifest in manifests:
        observe_capture(run, manifest, frozen)
    state = replay_account(manifests, config, **replay_options)
    campaigns = tuple(PaperCampaign.from_record(c) for c in state["campaigns"])
    cutoff = max(
        int(a["received_time_ns"])
        for p in manifests
        for a in json.loads(p.read_text())["artifacts"]
    )
    revised_accounting = replay_frozen_campaigns(manifests, campaigns, config, cutoff)
    store = ResearchStore(run / "research.sqlite")
    try:
        for observation in state["campaign_observations"]:
            store.record_campaign_observation(observation["campaign_id"], cutoff, observation)
    finally:
        store.close()
    result = {
        "policy_hash": frozen["policy_hash"],
        "manifests": [str(p.relative_to(run)) for p in manifests],
        "manifest_hashes": [hashlib.sha256(p.read_bytes()).hexdigest() for p in manifests],
        "native_state": state,
        "revised_accounting": revised_accounting,
    }
    temporary = run / "paper-state.tmp"
    with temporary.open("w") as output:
        output.write(canonical(result) + "\n")
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(checkpoint)
    directory = os.open(run, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return result


def load_policy_config(path: Path) -> dict[str, Any]:
    """Economic choices only; CLI account/fee assumptions cannot be overridden."""
    policy = json.loads(path.read_text())
    allowed = {
        "observer_policy",
        "grammar_policy",
        "campaign_policy",
        "stop_budget",
        "funding_max_age_ns",
        "open_interest_max_age_ns",
        "account_ratio_period",
        "account_ratio_max_age_ns",
        "calibration_source_run",
        "symbols",
        "experiment_window",
    }
    if not isinstance(policy, dict) or set(policy) - allowed:
        raise ValueError("policy config must contain only economic policy fields")
    return dict(policy)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_directory", type=Path)
    parser.add_argument("--maker-fee", type=Decimal, required=True)
    parser.add_argument("--taker-fee", type=Decimal, required=True)
    parser.add_argument("--initial-balance", type=Decimal, required=True)
    parser.add_argument("--max-notional", type=Decimal, required=True)
    parser.add_argument("--max-exposure-fraction", type=Decimal, required=True)
    parser.add_argument(
        "--policy-config", type=Path, help="JSON economic policy selection and freshness"
    )
    parser.add_argument("--replay-only", action="store_true")
    parser.add_argument(
        "--execution-profile",
        default=None,
        help=(
            "Nautilus simulated-execution profile (baseline | realistic | "
            "volume_aware) for the paper engine. Omit to keep engine defaults; "
            "the run then records no declared execution semantics."
        ),
    )
    args = parser.parse_args()
    config: dict[str, Any] = {
        "maker_fee": str(args.maker_fee),
        "taker_fee": str(args.taker_fee),
        "initial_balance": str(args.initial_balance),
        "max_notional": str(args.max_notional),
        "max_exposure_fraction": str(args.max_exposure_fraction),
    }
    if args.policy_config is not None:
        config.update(load_policy_config(args.policy_config))
    PaperConfig.model_validate(config)
    started = time.perf_counter()
    try:
        result = step(
            args.run_directory,
            config,
            replay_only=args.replay_only,
            execution_profile=args.execution_profile,
        )
    except Exception as error:
        logging.error(
            json.dumps(
                {
                    "event": "PAPER_STEP_FAILED",
                    "time_ns": time.time_ns(),
                    "error_type": type(error).__name__,
                    "wall_seconds": time.perf_counter() - started,
                }
            )
        )
        raise
    # Persist the measured execution evidence next to the run state, so a later
    # calibration read can cite the friction this run actually produced.
    execution = (result.get("native_state") or {}).get("execution")
    if execution:
        persist_execution_telemetry(
            args.run_directory / "execution_telemetry.json", execution
        )
    logging.info(
        json.dumps(
            {
                "event": "PAPER_STEP_COMPLETED",
                "time_ns": time.time_ns(),
                "wall_seconds": time.perf_counter() - started,
                "capture_count": len(result["manifests"]),
                "order_count": len(result["native_state"]["orders"]),
                "position_count": len(result["native_state"]["positions"]),
                "funding_status": result["native_state"]["funding_status"],
            }
        )
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
