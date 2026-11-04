"""Run a frozen DEVELOPMENT policy experiment on captured real historical bars."""

import argparse
import hashlib
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from nautilus_trader.model import Currency, Venue

from v8_next.adapters.captured_market import load_candles
from v8_next.adapters.engine_state import economic_state
from v8_next.adapters.historical_trial import HistoricalTrial
from v8_next.adapters.native_tape import build_engine
from v8_next.adapters.portfolio_tape import build_portfolio_engine
from v8_next.adapters.stop_exposure import native_stop_exposure
from v8_next.app.observe import source_hash
from v8_next.domain.config import PaperConfig
from v8_next.evaluation.equity import equity_losses
from v8_next.evaluation.outcomes import observed_outcomes
from v8_next.evaluation.store import ResearchStore, canonical


def run_trial(
    manifest: Path,
    policy: PaperConfig,
    store: ResearchStore,
    family: str,
    *,
    additional_manifests: tuple[Path, ...] = (),
    accounting_as_of_ns: int | None = None,
    component_plan: tuple[int, int, int] | None = None,
    selection_end_ns: int | None = None,
    selection_start_ns: int | None = None,
) -> dict[str, Any]:
    return _run_trial(
        manifest,
        policy,
        store,
        family,
        "DEVELOPMENT",
        additional_manifests=additional_manifests,
        accounting_as_of_ns=accounting_as_of_ns,
        component_plan=component_plan,
        selection_end_ns=selection_end_ns,
        selection_start_ns=selection_start_ns,
    )


def _run_trial(
    manifest: Path,
    policy: PaperConfig,
    store: ResearchStore,
    family: str,
    role: Literal["DEVELOPMENT", "HOLDOUT"],
    *,
    additional_manifests: tuple[Path, ...] = (),
    accounting_as_of_ns: int | None = None,
    component_plan: tuple[int, int, int] | None = None,
    selection_end_ns: int | None = None,
    selection_start_ns: int | None = None,
) -> dict[str, Any]:
    if not family.strip():
        raise ValueError("explicit research family required")
    if component_plan is not None:
        block_size, reps, seed = component_plan
        if role != "DEVELOPMENT":
            raise ValueError("component fitting cannot consume protected holdout")
        if (
            any(type(v) is not int for v in component_plan)
            or block_size < 1
            or reps < 2
            or seed < 0
        ):
            raise ValueError("invalid component plan")
    if selection_end_ns is not None and (
        type(selection_end_ns) is not int or selection_end_ns <= 0
    ):
        raise ValueError("invalid campaign selection cutoff")
    if selection_start_ns is not None and (
        type(selection_start_ns) is not int
        or selection_start_ns < 0
        or (selection_end_ns is not None and selection_start_ns >= selection_end_ns)
    ):
        raise ValueError("invalid campaign selection start")
    manifests = (manifest, *additional_manifests)
    portfolio = bool(additional_manifests)
    if portfolio and (accounting_as_of_ns is None or not 0 < accounting_as_of_ns <= time.time_ns()):
        raise ValueError("portfolio requires an explicit known accounting cutoff")
    if not portfolio and accounting_as_of_ns is not None:
        raise ValueError("accounting cutoff currently requires a portfolio trial")
    sources = sorted(hashlib.sha256(path.read_bytes()).hexdigest() for path in manifests)
    if len(set(sources)) != len(sources):
        raise ValueError("duplicate capture manifest")
    dataset_hash = (
        hashlib.sha256(canonical(sources).encode()).hexdigest() if portfolio else sources[0]
    )
    if (
        role == "DEVELOPMENT"
        and store.db.execute(
            "SELECT 1 FROM trials WHERE dataset_hash=? AND role='HOLDOUT'", (dataset_hash,)
        ).fetchone()
    ):
        raise ValueError("protected holdout cannot be used as a development trial")
    frozen: dict[str, Any] = {
        "config": policy.model_dump(mode="json"),
        "code_and_lock_hash": source_hash(),
        "execution_model": "NATIVE_OHLC_NEXT_BAR_CLOSE_TRIAL_V1",
        "role": role,
    }
    if selection_end_ns is not None:
        frozen["selection_end_ns"] = selection_end_ns
    if selection_start_ns is not None:
        frozen["selection_start_ns"] = selection_start_ns
    if component_plan is not None:
        frozen["component_plan"] = dict(
            zip(("block_size", "reps", "seed"), component_plan, strict=True)
        )
    if portfolio:
        frozen.update(
            execution_model="NATIVE_OHLC_ALIGNED_PORTFOLIO_V1",
            accounting_as_of_ns=accounting_as_of_ns,
            allocation_priority="INSTRUMENT_ID_ASCENDING",
            capture_manifest_hashes=sources,
        )
    policy_hash = hashlib.sha256(canonical(frozen).encode()).hexdigest()
    trial_id = hashlib.sha256(canonical([family, dataset_hash, policy_hash]).encode()).hexdigest()
    existing = store.db.execute(
        "SELECT registered_ns FROM trials WHERE trial_id=?", (trial_id,)
    ).fetchone()
    registered = existing[0] if existing else time.time_ns()
    store.register_trial(trial_id, family, policy_hash, dataset_hash, role, registered)
    captured = [load_candles(path) for path in manifests]
    for candles in captured:
        if not candles:
            raise ValueError("empty trial dataset")
        instruments = {c.instrument_id for c in candles}
        if len(instruments) != 1:
            raise ValueError("capture must contain one instrument")
        instrument = next(iter(instruments))
        if not portfolio and instrument != "BTCUSDT-PERP.BINANCE":
            raise ValueError("initial historical trial scope is BTCUSDT")
        # Full captured coverage conservatively includes discarded outer bars.
        store.register_dataset_window(
            dataset_hash,
            instrument,
            min(c.start_ns for c in candles),
            max(c.end_ns for c in candles),
        )
    if portfolio:
        assert accounting_as_of_ns is not None
        engine, candles = build_portfolio_engine(
            manifests,
            maker_fee=policy.maker_fee,
            taker_fee=policy.taker_fee,
            initial_balance=policy.initial_balance,
            accounting_as_of_ns=accounting_as_of_ns,
        )
        metadata = {
            "claim_status": "NO_ECONOMIC_CLAIM",
            "timing_model": "historical_close_assumed_available_not_certified",
            "metadata_scope": "current_metadata_not_historical_universe",
            "margin_scope": "generic_1x_netting_not_venue_liquidation_qualification",
            "bar_count": len(candles),
            "capture_manifest_hashes": sources,
            "instruments": sorted({c.instrument_id for c in candles}),
            "common_start_ns": candles[0].start_ns,
            "common_end_ns": candles[-1].end_ns,
        }
    else:
        candles = captured[0]
        engine, metadata = build_engine(
            manifest, policy.maker_fee, policy.taker_fee, policy.initial_balance
        )
    try:
        trial = HistoricalTrial(
            candles,
            policy,
            selection_end_ns=selection_end_ns,
            selection_start_ns=selection_start_ns,
        )
        engine.add_strategy(trial)
        engine.run()
        if trial.failure is not None or trial.callback_failure is not None:
            raise ValueError(
                f"historical trial callback failed: {trial.failure or trial.callback_failure}"
            )
        state = economic_state(engine, Venue("BINANCE"), Currency.from_str("USDT"))
        pending_ids = frozenset(
            c.campaign_id
            for c in trial.campaigns
            if c.campaign_id not in trial.submitted | trial.expired | trial.invalidated
        )
        stop_exposure = native_stop_exposure(
            engine.cache,
            trial.campaigns,
            pending_campaigns=any(
                c.campaign_id not in trial.submitted | trial.expired | trial.invalidated
                for c in trial.campaigns
            ),
            observed_ns=candles[-1].end_ns,
            pending_ids=pending_ids,
        )
        computed_ns = time.time_ns()
        losses = equity_losses(
            trial.equity_marks, capital=policy.initial_balance, computed_ns=computed_ns
        )
        result: dict[str, Any] = {
            **metadata,
            "trial_id": trial_id,
            "family": family,
            "family_trial_count": store.family_size(family),
            "registered_ns": registered,
            "dataset_hash": dataset_hash,
            "policy_hash": policy_hash,
            "frozen_policy": frozen,
            "scope": "OFFLINE_COUNTERFACTUAL_POLICY_EXPERIMENT",
            "authority": "DIAGNOSTIC_ONLY",
            "calibration_eligible": False,
            "promotion_eligible": False,
            "account": state,
            "stop_exposure": {
                **asdict(stop_exposure),
                "open_and_reserved_risk": str(stop_exposure.open_and_reserved_risk),
            }
            if stop_exposure is not None
            else None,
            "equity_marks": trial.equity_marks,
            "computed_ns": computed_ns,
            "period_loss_definition": "NEGATIVE_EQUITY_CHANGE_OVER_FIXED_INITIAL_CAPITAL",
            "period_losses": [
                {
                    "start_ns": row.start_ns,
                    "end_ns": row.end_ns,
                    "available_ns": row.available_ns,
                    "loss": str(row.loss),
                }
                for row in losses
            ],
            "decisions": trial.decisions,
            "campaigns": [c.to_record() for c in trial.campaigns],
            "campaign_observations": trial.campaign_observations(state),
            "position_closures": trial.closed_position_records(),
            "outcomes": observed_outcomes(
                [c.to_record() for c in trial.campaigns],
                trial.closed_position_records(),
                state,
                policy.initial_balance,
                economic_policy=policy.model_dump(mode="json"),
                campaign_observations=trial.campaign_observations(state),
            ),
            "funding_coverage": "OBSERVED_RECORDS_NOT_COMPLETENESS_CERTIFIED",
            "limitations": [
                "modeled_close_availability",
                "current_instrument_metadata",
                "native_OHLC_intrabar_path_assumption",
                "historical_spread_and_slippage_unmeasured",
                "generic_margin_no_venue_liquidation",
                "not_a_calibration_receipt",
            ],
        }
        if component_plan is not None:
            from v8_next.evaluation.component_estimates import estimate_components
            from v8_next.evaluation.selection_estimates import estimate_selection_cash

            block_size, reps, seed = component_plan
            result["selection_cash_estimate"] = estimate_selection_cash(
                result["outcomes"], block_size=block_size, reps=reps, seed=seed
            )
            result["component_estimates"] = estimate_components(
                result["outcomes"],
                decision_ns=computed_ns,
                block_size=block_size,
                reps=reps,
                seed=seed,
            )
        return result
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("policy", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--family", required=True)
    parser.add_argument("--additional-manifest", type=Path, action="append", default=[])
    parser.add_argument("--accounting-as-of-ns", type=int)
    parser.add_argument("--component-plan", type=int, nargs=3, metavar=("BLOCK", "REPS", "SEED"))
    parser.add_argument("--selection-end-ns", type=int)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("trial output already exists")
    policy = PaperConfig.model_validate_json(args.policy.read_text())
    store = ResearchStore(args.store)
    try:
        result = run_trial(
            args.manifest,
            policy,
            store,
            args.family,
            additional_manifests=tuple(args.additional_manifest),
            accounting_as_of_ns=args.accounting_as_of_ns,
            selection_end_ns=args.selection_end_ns,
            component_plan=tuple(args.component_plan) if args.component_plan is not None else None,
        )
        with args.output.open("x") as stream:
            stream.write(canonical(result) + "\n")
    finally:
        store.close()


if __name__ == "__main__":
    main()
