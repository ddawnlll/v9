"""Per-trade forensic metrics: lifecycle, MFE/MAE, R multiples, post-exit paths.

Every number here is derived from the replay's own observed events. Where the
OHLC tape cannot identify a fact (intrabar path, missing stop, absent depth),
the metric is ``None`` together with an explicit reason instead of 0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

POST_EXIT_HORIZONS: tuple[int, ...] = (1, 3, 6, 12, 24, 48)

STOP_FIRST_CONTRACT = (
    "STOP_FIRST: when a bar's range covers both the protective stop and the target, "
    "the stop is assumed to trigger first. This is a declared model assumption, not an "
    "observed intrabar sequence."
)

#: Extra exclusion applied to the entry timestamp before a bar may count as
#: post-entry. Zero by default: the native fills of a bar-close replay are
#: stamped at the bar's end, which is exactly the eligibility boundary.
INTRABAR_EXCLUSION_NS: int = 0

#: Documented eligibility rule for every MFE/MAE measurement.
POST_ENTRY_ELIGIBILITY = (
    "POST_ENTRY_ONLY: a bar contributes to MFE/MAE only when its start is at or after "
    "the entry timestamp plus INTRABAR_EXCLUSION_NS. The entry bar's high/low were "
    "produced before the position existed, and intrabar entry timing is not recoverable "
    "from an OHLC tape, so the containing bar is never counted. Mutating the entry bar's "
    "future high/low therefore cannot change any measured excursion."
)


def entry_eligibility_boundary(
    opened_ns: int, *, intrabar_exclusion_ns: int = INTRABAR_EXCLUSION_NS
) -> int:
    """First instant at which a bar may carry genuinely post-entry excursion."""
    return opened_ns + intrabar_exclusion_ns


def _bar_start_ns(candles: Sequence[Any], index: int) -> int | None:
    """Nominal bar start; contiguous tapes use the previous bar's end as fallback.

    An unknown start (first bar of a tape that carries no ``start_ns``) returns
    ``None`` so the caller can fail closed instead of guessing.
    """
    start = getattr(candles[index], "start_ns", None)
    if start is not None:
        return int(start)
    if index > 0:
        return int(candles[index - 1].end_ns)
    return None


def post_entry_window(
    candles: Sequence[Any],
    *,
    opened_ns: int,
    exit_ns: int | None = None,
    intrabar_exclusion_ns: int = INTRABAR_EXCLUSION_NS,
) -> list[Any]:
    """Bars strictly after the entry eligibility boundary and not past the exit.

    Pure: the input sequence is never mutated and the returned bars are the
    caller's own objects. A bar whose start cannot be established is excluded
    (it may contain the entry instant), never included on a guess.
    """
    boundary = entry_eligibility_boundary(
        opened_ns, intrabar_exclusion_ns=intrabar_exclusion_ns
    )
    window: list[Any] = []
    for index, candle in enumerate(candles):
        if exit_ns is not None and int(candle.end_ns) > exit_ns:
            break
        start_ns = _bar_start_ns(candles, index)
        if start_ns is None or start_ns < boundary:
            continue
        window.append(candle)
    return window


def _dec(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).split(" ")[0])
    except (TypeError, ValueError):
        return None


def _direction(side: str) -> int:
    text = side.upper()
    if "SHORT" in text or text == "SELL":
        return -1
    return 1


def _money(value: Any, currency: str = "USDT") -> float | None:
    """Parse '12.34 USDT' / ['1 USDT'] / numbers."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (list, tuple)):
        total = 0.0
        seen = False
        for item in value:
            parsed = _money(item, currency)
            if parsed is not None:
                total += parsed
                seen = True
        return total if seen else None
    text = str(value)
    for part in text.replace("[", " ").replace("]", " ").replace(",", " ").split():
        try:
            return float(part)
        except ValueError:
            continue
    return None


@dataclass
class TradeFacts:
    position_id: str
    instrument_id: str
    side: str
    quantity: float | None
    entry_price: float | None
    opened_ns: int | None
    closed_ns: int | None
    exit_price: float | None
    initial_stop_price: float | None = None
    initial_target_price: float | None = None
    opportunity_id: str | None = None
    decision_id: str | None = None
    entry_bar_ns: int | None = None
    exit_kind: str = "UNKNOWN"
    native_realized_pnl: float | None = None
    fees: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


def _orders_for_position(
    position_id: str,
    order_rows: Iterable[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    rows = []
    for row in order_rows:
        if str(row.get("position_id") or "") == position_id:
            rows.append(row)
    return rows


EXIT_TAGS: dict[str, str] = {"STOP_LOSS": "STOP", "TAKE_PROFIT": "TARGET_LIMIT"}


def _tag_set(row: Mapping[str, Any]) -> set[str]:
    return {str(tag).upper() for tag in (row.get("tags") or [])}


def classify_exit_kind(
    direction: int,
    orders: Sequence[Mapping[str, Any]],
    opened_ns: int | None = None,
) -> str:
    """Infer the closing order class from this cycle's native order rows.

    The native bracket tags (ENTRY/STOP_LOSS/TAKE_PROFIT) are authoritative. When
    they are absent the order type and side are used. Fills stamped exactly at
    the cycle open are entry fills; a non-entry fill at that timestamp belongs to
    the previous campaign and is ignored.
    """
    if not orders:
        return "UNIDENTIFIED_NO_NATIVE_ORDER"
    candidates: list[tuple[Mapping[str, Any], set[str]]] = []
    for row in orders:
        status = str(row.get("status") or "").upper()
        if status not in {"FILLED", "PARTIALLY_FILLED"}:
            continue
        tags = _tag_set(row)
        ts = int(row.get("ts_last") or 0)
        if opened_ns is not None and ts <= opened_ns and "ENTRY" not in tags:
            continue
        if "ENTRY" in tags:
            continue
        if not (tags & EXIT_TAGS.keys()):
            side = str(row.get("side") or "").upper()
            if not (("BUY" in side and direction < 0) or ("SELL" in side and direction > 0)):
                continue
        candidates.append((row, tags))
    if not candidates:
        return "UNIDENTIFIED_NO_CLOSING_FILL"
    tagged = {EXIT_TAGS[tag] for _, tags in candidates for tag in tags if tag in EXIT_TAGS}
    if "STOP" in tagged:
        return "STOP"
    if "TARGET_LIMIT" in tagged:
        return "TARGET_LIMIT"
    order_type = str(candidates[0][0].get("type") or "").upper()
    if "STOP" in order_type:
        return "STOP"
    if "LIMIT" in order_type:
        return "TARGET_LIMIT"
    if "MARKET" in order_type:
        return "MARKET_EXIT"
    return f"OTHER_{order_type or 'UNKNOWN'}"


def stop_first_path(
    *,
    candles: Sequence[Any],
    direction: int,
    entry: float,
    stop: float | None,
    target: float | None,
    opened_ns: int,
    exit_ns: int | None,
) -> dict[str, Any] | None:
    """Walk bars under the declared STOP-FIRST intrabar contract.

    When a bar's range covers the stop, the stop is assumed to fill before any
    favourable excursion of that same bar, so MAE is capped at the stop distance
    and the bar's high/low cannot inflate MFE beyond it. Without a stop the raw
    OHLC extremes are returned instead (no contract can be applied).

    Bars come from :func:`post_entry_window`, so the entry bar and any bar that
    may contain the entry instant are never measured. A bar covering both the
    stop and the target is a tie the tape cannot resolve: the declared contract
    still labels it ``STOP_FIRST`` but ``same_bar_stop_and_target`` records that
    the intrabar sequence is not identified.
    """
    if stop is None:
        return None
    mfe = 0.0
    mae = 0.0
    outcome = "OPEN_AT_CUTOFF"
    exit_price = None
    exit_ns_path = None
    bars_measured = 0
    same_bar_tie = False
    window = post_entry_window(
        candles, opened_ns=opened_ns, exit_ns=exit_ns
    )
    for candle in window:
        bar_end = int(candle.end_ns)
        bars_measured += 1
        high = float(candle.high)
        low = float(candle.low)
        bar_mfe = high - entry if direction > 0 else entry - low
        bar_mae = low - entry if direction > 0 else entry - high
        stop_hit = low <= stop if direction > 0 else high >= stop
        target_hit = target is not None and (high >= target if direction > 0 else low <= target)
        if stop_hit:
            if target_hit:
                same_bar_tie = True
            # Stop-first: the stop fills before any favourable excursion here.
            mae = min(mae, stop - entry if direction > 0 else entry - stop)
            mfe = max(mfe, 0.0)
            outcome = "STOP_FIRST"
            exit_price = stop
            exit_ns_path = bar_end
            break
        if target_hit:
            assert target is not None
            mfe = max(mfe, bar_mfe, target - entry if direction > 0 else entry - target)
            mae = min(mae, bar_mae)
            outcome = "TARGET_FIRST"
            exit_price = target
            exit_ns_path = bar_end
            break
        mfe = max(mfe, bar_mfe)
        mae = min(mae, bar_mae)
    eligibility = {
        "eligibility_boundary_ns": entry_eligibility_boundary(opened_ns),
        "same_bar_stop_and_target": same_bar_tie,
        "tie_resolution": "STOP_FIRST_DECLARED" if same_bar_tie else None,
    }
    if bars_measured == 0:
        return {
            "mfe_price": None,
            "mae_price": None,
            "outcome": "ENTRY_BAR_ONLY",
            "exit_price": None,
            "exit_ns": None,
            "bars_measured": 0,
            **eligibility,
        }
    return {
        "mfe_price": mfe,
        "mae_price": mae,
        "outcome": outcome,
        "exit_price": exit_price,
        "exit_ns": exit_ns_path,
        "bars_measured": bars_measured,
        **eligibility,
    }


def _bar_index(candles: Sequence[Any], ts_ns: int) -> int:
    for idx, candle in enumerate(candles):
        if candle.end_ns >= ts_ns:
            return idx
    return len(candles)


def compute_trade_metrics(
    *,
    facts: TradeFacts,
    candles: Sequence[Any],
    cutoff_ns: int | None = None,
) -> dict[str, Any]:
    """Compute MFE/MAE/R for one campaign. Direction-adjusted, no invention."""
    direction = _direction(facts.side)
    entry = facts.entry_price
    qty = facts.quantity
    row: dict[str, Any] = {
        "position_id": facts.position_id,
        "campaign_id": facts.extra.get("campaign_id"),
        "instrument_id": facts.instrument_id,
        "side": facts.side,
        "direction": "LONG" if direction > 0 else "SHORT",
        "opportunity_id": facts.opportunity_id,
        "decision_id": facts.decision_id,
        "quantity": qty,
        "entry_price": entry,
        "exit_price": facts.exit_price,
        "initial_stop_price": facts.initial_stop_price,
        "initial_target_price": facts.initial_target_price,
        "opened_ns": facts.opened_ns,
        "closed_ns": facts.closed_ns,
        "exit_kind": facts.exit_kind,
        "position_status": "CLOSED" if facts.closed_ns is not None else "OPEN_AT_CUTOFF",
        "native_realized_pnl_usdt": facts.native_realized_pnl,
        "fees_usdt": round(facts.fees, 10),
        "entry_fees_usdt": round(facts.extra.get("entry_fees") or 0.0, 10),
        "exit_fees_usdt": round(facts.extra.get("exit_fees") or 0.0, 10),
        "fee_basis": "NATIVE_FILLS_BY_ENTRY_EXIT_TAG",
        "funding_usdt": None,
        "funding_mode": "FUNDING_UNAVAILABLE",
        "intrabar_contract": STOP_FIRST_CONTRACT,
    }
    orders_for_position = facts.extra.get("position_orders") or []
    row["protection_evidence"] = (
        "NATIVE_ORDER_PRESENT"
        if any(
            "STOP" in str(o.get("type") or "").upper()
            or "LIMIT" in str(o.get("type") or "").upper()
            for o in orders_for_position
        )
        else "NO_NATIVE_PROTECTIVE_ORDER"
    )

    if entry is None or qty is None or facts.opened_ns is None:
        row.update(
            mfe_price=None,
            mae_price=None,
            mfe_r=None,
            mae_r=None,
            realized_gross_r=None,
            realized_net_r=None,
        )
        row["metric_unavailable_reason"] = "MISSING_ENTRY_OR_QUANTITY"
        return row

    risk_price = None
    risk_reason = None
    if facts.initial_stop_price is None:
        risk_reason = "MISSING_INITIAL_STOP"
    else:
        distance = abs(entry - facts.initial_stop_price)
        if distance == 0:
            risk_reason = "ZERO_STOP_DISTANCE"
        else:
            risk_price = distance
    row["initial_risk_price"] = risk_price
    row["initial_risk_usdt"] = risk_price * qty if risk_price is not None else None
    row["risk_denominator_status"] = "VALID" if risk_price is not None else "INVALID"
    if risk_reason:
        row["risk_invalid_reason"] = risk_reason

    exit_ns = facts.closed_ns if facts.closed_ns is not None else cutoff_ns
    window = [
        c
        for c in candles
        if c.end_ns >= (facts.opened_ns or 0) and (exit_ns is None or c.end_ns <= exit_ns)
    ]
    eligible = post_entry_window(
        candles, opened_ns=facts.opened_ns or 0, exit_ns=exit_ns
    )
    row["entry_eligibility_rule"] = POST_ENTRY_ELIGIBILITY
    row["entry_eligibility_boundary_ns"] = entry_eligibility_boundary(facts.opened_ns or 0)
    row["entry_bars_excluded"] = max(0, len(window) - len(eligible))
    if window:
        contract = stop_first_path(
            candles=candles,
            direction=direction,
            entry=entry,
            stop=facts.initial_stop_price,
            target=facts.initial_target_price,
            opened_ns=facts.opened_ns or 0,
            exit_ns=exit_ns,
        )
        excursion: tuple[float, float] | None = None
        if contract is not None:
            row["same_bar_stop_and_target"] = bool(contract["same_bar_stop_and_target"])
            row["tie_resolution"] = contract["tie_resolution"]
            row["path_model_applied"] = "STOP_FIRST"
            if contract["outcome"] == "ENTRY_BAR_ONLY":
                row["path_model_outcome"] = "ENTRY_BAR_ONLY"
                row["metric_unavailable_reason"] = "NO_POST_ENTRY_BAR"
                row["measured_window_bars"] = 0
            else:
                row["path_model_outcome"] = contract["outcome"]
                row["path_model_exit_price"] = contract["exit_price"]
                row["path_model_exit_ns"] = contract["exit_ns"]
                row["mae_capped_by_stop"] = contract["outcome"] == "STOP_FIRST"
                excursion = (contract["mfe_price"], contract["mae_price"])
        elif eligible:
            # No protective stop: raw extremes, still from post-entry bars only.
            highs = [float(c.high) for c in eligible]
            lows = [float(c.low) for c in eligible]
            if direction > 0:
                excursion = (max(highs) - entry, min(lows) - entry)
            else:
                excursion = (entry - min(lows), entry - max(highs))
            row["path_model_applied"] = "NONE_NO_STOP"
            row["same_bar_stop_and_target"] = False
            row["tie_resolution"] = None
        else:
            # No stop AND no post-entry bar: the entry bar's extrema are not
            # evidence of anything that happened after the position existed.
            row["path_model_applied"] = "NONE_NO_STOP"
            row["path_model_outcome"] = "ENTRY_BAR_ONLY"
            row["metric_unavailable_reason"] = "NO_POST_ENTRY_BAR"
            row["measured_window_bars"] = 0
            row["same_bar_stop_and_target"] = False
            row["tie_resolution"] = None
        if excursion is not None:
            mfe_price, mae_price = excursion
            row["mfe_price"] = round(mfe_price, 10)
            row["mae_price"] = round(mae_price, 10)
            row["mfe_r"] = round(mfe_price / risk_price, 10) if risk_price else None
            row["mae_r"] = round(mae_price / risk_price, 10) if risk_price else None
            row["holding_bars"] = len(window)
            row["holding_duration_ns"] = (exit_ns or window[-1].end_ns) - facts.opened_ns
            row["measured_window_bars"] = max(0, len(window) - 1)
        else:
            row["mfe_price"] = row["mae_price"] = None
            row["mfe_r"] = row["mae_r"] = None
    else:
        row["mfe_price"] = row["mae_price"] = None
        row["mfe_r"] = row["mae_r"] = None
        row["metric_unavailable_reason"] = "NO_BARS_IN_POSITION_WINDOW"

    exit_price = facts.exit_price
    if exit_price is None:
        row["gross_pnl_usdt"] = None
        row["realized_net_pnl_usdt"] = None
        row["realized_gross_r"] = None
        row["realized_net_r"] = None
        row["metric_unavailable_reason"] = "NO_EXIT_PRICE_AT_CUTOFF"
        return row

    gross = (exit_price - entry) * direction * qty
    # Native realized PnL for a closed position is already net of that cycle's
    # commissions, so the delta (not a re-summed fill window) is the authoritative
    # fee figure whenever both numbers exist. Fill-window sums double-count when
    # the next campaign's entry fills on the same timestamp as the previous exit.
    if facts.native_realized_pnl is not None:
        fees = round(gross - facts.native_realized_pnl, 10)
        row["fees_usdt"] = fees
        row["fee_basis"] = "GROSS_MINUS_NATIVE_REALIZED"
        net = facts.native_realized_pnl
    else:
        fees = facts.fees
        net = gross - facts.fees
        row["fee_basis"] = "NATIVE_FILLS_IN_OPEN_CYCLE"
    risk_usdt = risk_price * qty if risk_price is not None else None
    row["gross_pnl_usdt"] = round(gross, 10)
    row["realized_net_pnl_usdt"] = round(net, 10)
    row["realized_gross_r"] = round(gross / risk_usdt, 10) if risk_usdt else None
    row["realized_net_r"] = round(net / risk_usdt, 10) if risk_usdt else None
    return row


POLICY_DEFINITION = (
    "Re-enter at the observed exit price in the original direction with the "
    "original bracket offsets (same stop and target distances), size = the "
    "campaign quantity; STOP_FIRST whenever a bar covers both levels. This is a "
    "counterfactual model, not an order that was ever submitted."
)


def compute_post_exit(
    *,
    facts: TradeFacts,
    candles: Sequence[Any],
    horizons: Sequence[int] = POST_EXIT_HORIZONS,
) -> list[dict[str, Any]]:
    """Post-exit follow-through split into three distinct evidence classes.

    * OBSERVED_POST_EXIT_PATH: what the tape did after the real exit.
    * LEGAL_POLICY_COUNTERFACTUAL: outcome of the declared re-entry policy.
    * HINDSIGHT_MAXIMUM: the best favourable excursion on the tape.
    All three are multiplied by the campaign quantity so units are USDT, not price.
    """
    if facts.closed_ns is None or facts.exit_price is None:
        return []
    direction = _direction(facts.side)
    qty = facts.quantity
    if qty is None or qty <= 0:
        return []
    exit_idx = _bar_index(candles, facts.closed_ns)
    if exit_idx >= len(candles):
        return []
    exit_price = facts.exit_price
    entry = facts.entry_price
    risk_frac = (
        abs(entry - facts.initial_stop_price) / entry
        if entry and facts.initial_stop_price is not None
        else None
    )
    target_frac = (
        abs(facts.initial_target_price - entry) / entry
        if entry and facts.initial_target_price is not None
        else None
    )
    policy_stop = (
        None if risk_frac is None else exit_price - direction * exit_price * risk_frac
    )
    policy_target = (
        None if target_frac is None else exit_price + direction * exit_price * target_frac
    )
    rows = []
    for horizon in horizons:
        target_idx = exit_idx + horizon - 1
        if target_idx >= len(candles):
            continue
        window = candles[exit_idx : target_idx + 1]
        close = float(window[-1].close)
        if direction > 0:
            best = max(float(c.high) for c in window) - exit_price
            adverse = min(float(c.low) for c in window) - exit_price
        else:
            best = exit_price - min(float(c.low) for c in window)
            adverse = exit_price - max(float(c.high) for c in window)

        policy_outcome = "TIME_EXIT"
        policy_usdt: float | None = None
        if policy_stop is not None and policy_target is not None:
            outcome_price = close
            for candle in window:
                high = float(candle.high)
                low = float(candle.low)
                stop_hit = low <= policy_stop if direction > 0 else high >= policy_stop
                target_hit = high >= policy_target if direction > 0 else low <= policy_target
                if stop_hit:
                    outcome_price = policy_stop
                    policy_outcome = "STOP_FIRST"
                    break
                if target_hit:
                    outcome_price = policy_target
                    policy_outcome = "TARGET_FIRST"
                    break
            policy_usdt = (outcome_price - exit_price) * direction * qty
        rows.append(
            {
                "position_id": facts.position_id,
                "campaign_id": facts.extra.get("campaign_id"),
                "exit_kind": facts.exit_kind,
                "horizon_bars": horizon,
                "observed_post_exit_path_usdt": round((close - exit_price) * direction * qty, 10),
                "legal_policy_counterfactual_usdt": (
                    round(policy_usdt, 10) if policy_usdt is not None else None
                ),
                "legal_policy_outcome": policy_outcome,
                "legal_policy_unavailable_reason": (
                    None if policy_usdt is not None else "MISSING_INITIAL_STOP_OR_TARGET"
                ),
                "hindsight_maximum_usdt": round(best * qty, 10),
                "max_adverse_after_exit_usdt": round(adverse * qty, 10),
                "evidence_class_observed": "OBSERVED_TAPE_PATH",
                "evidence_class_counterfactual": "LEGAL_POLICY_COUNTERFACTUAL_MODEL",
                "evidence_class_hindsight": "HINDSIGHT_MAXIMUM_MODEL",
                "policy_definition": POLICY_DEFINITION,
                "counterfactual_note": (
                    "Counterfactual and hindsight values are not attainable trading "
                    "profit; they describe the tape after the exit."
                ),
            }
        )
    return rows


def trade_facts_from_replay(
    result: Mapping[str, Any], *, cutoff_ns: int | None = None
) -> list[TradeFacts]:
    """Join opened/closed position cycles and fills into per-campaign facts.

    The Nautilus NETTING account reuses one position id for every campaign, so
    cycles are paired by order (open[i] closes with close[i]); pairing by
    position id would collapse every trade onto the same row.
    """
    fills = sorted(
        result.get("fill_records") or [], key=lambda row: int(row.get("ts_last") or 0)
    )
    opened = result.get("opened_positions") or []
    closed = result.get("closed_positions") or []
    decisions = result.get("decisions") or []
    book = result.get("opportunity_book")

    opportunities: list[dict[str, Any]] = []
    if book is not None:
        for record in book.all():
            opportunities.append(
                {
                    "opportunity_id": record.opportunity_id,
                    "status": str(record.status),
                    "entry_price": _dec(record.entry_price),
                    "stop_price": _dec(record.stop_price),
                    "target_price": _dec(record.target_price),
                    "as_of_time_ns": record.as_of_time_ns,
                    "valid_until_ns": record.valid_until_ns,
                    "direction": str(record.direction),
                }
            )

    facts_list: list[TradeFacts] = []
    for index, position in enumerate(opened):
        opened_ns = int(position.get("event_ns") or 0) or None
        close_row = closed[index] if index < len(closed) else None
        closed_ns = int(close_row.get("event_ns") or 0) or None if close_row else None
        entry_price = _dec(position.get("avg_px_open"))
        quantity = _dec(position.get("quantity"))
        side = str(position.get("side") or "")
        direction = _direction(side)

        decision = None
        for row in decisions:
            ts = int(row.get("decision_ns") or 0)
            if opened_ns is None or ts > opened_ns:
                continue
            # Only a decision that actually submitted an order can own this
            # campaign. Taking the last decision of any kind mis-attributes a
            # trade whenever the fill is delayed (slipped/latency profiles) and
            # the following bars carry NO_ACTION decisions.
            action = str(row.get("action") or "")
            if "SUBMITTED" not in action and decision is not None:
                continue
            decision = row
        decision_opp = (decision or {}).get("opportunity") or {}
        # The decisions ledger carries a flat opportunity_id; the nested
        # opportunity record is the fallback for older ledgers.
        opp_id = (decision or {}).get("opportunity_id") or decision_opp.get("opportunity_id")

        matched = next(
            (opp for opp in opportunities if opp_id and opp["opportunity_id"] == opp_id), None
        )
        stop_price = (matched or {}).get("stop_price")
        target_price = (matched or {}).get("target_price")
        if stop_price is None:
            stop_pct = result.get("config", {}).get("bracket_stop_pct")
            if stop_pct is not None and entry_price is not None:
                stop_pct_f = float(str(stop_pct))
                stop_price = (
                    entry_price - entry_price * stop_pct_f
                    if direction > 0
                    else entry_price + entry_price * stop_pct_f
                )
        if target_price is None:
            target_pct = result.get("config", {}).get("bracket_target_pct")
            if target_pct is not None and entry_price is not None:
                target_pct_f = float(str(target_pct))
                target_price = (
                    entry_price + entry_price * target_pct_f
                    if direction > 0
                    else entry_price - entry_price * target_pct_f
                )

        window_end = closed_ns if closed_ns is not None else cutoff_ns
        next_open_ns = (
            int(opened[index + 1].get("event_ns") or 0)
            if index + 1 < len(opened)
            else None
        )
        cycle_fills = []
        for row in fills:
            ts = int(row.get("ts_last") or 0)
            if opened_ns is None or ts < opened_ns:
                continue
            if window_end is not None and ts > window_end:
                continue
            tags = _tag_set(row)
            # A closing fill stamped exactly at this cycle's open belongs to the
            # previous campaign; only entry-tagged fills may share the open stamp.
            if ts == opened_ns and "ENTRY" not in tags:
                continue
            # The next campaign's entry fill can share this cycle's exit stamp.
            if ts == next_open_ns and "ENTRY" in tags:
                continue
            cycle_fills.append(row)
        entry_fees = sum(
            _money(row.get("commissions")) or 0.0
            for row in cycle_fills
            if "ENTRY" in _tag_set(row)
        )
        exit_fees = sum(
            _money(row.get("commissions")) or 0.0
            for row in cycle_fills
            if "ENTRY" not in _tag_set(row)
        )
        fees = entry_fees + exit_fees
        exit_price = _dec(close_row.get("avg_px_close")) if close_row else None
        native_pnl = _money(close_row.get("realized_pnl")) if close_row else None

        facts_list.append(
            TradeFacts(
                position_id=f"{position.get('position_id') or ''}#{index + 1}",
                instrument_id=str(position.get("instrument_id") or ""),
                side=side,
                quantity=quantity,
                entry_price=entry_price,
                opened_ns=opened_ns,
                closed_ns=closed_ns,
                exit_price=exit_price,
                initial_stop_price=stop_price,
                initial_target_price=target_price,
                opportunity_id=opp_id,
                decision_id=(decision or {}).get("decision_id"),
                entry_bar_ns=(decision or {}).get("decision_ns"),
                exit_kind=classify_exit_kind(direction, cycle_fills, opened_ns),
                native_realized_pnl=native_pnl,
                fees=fees,
                extra={
                    "position_orders": cycle_fills,
                    "campaign_id": f"C{index + 1:04d}",
                    "entry_fees": entry_fees,
                    "exit_fees": exit_fees,
                },
            )
        )
    return facts_list
