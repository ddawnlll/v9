"""Causal bar-path counterfactuals for research arms.

What this module is
-------------------
A deterministic evaluator that walks *already closed* bars forward from a
declared entry and reports what a declared policy would have done. It exists so
an experiment can screen policy variants cheaply before (or instead of) a native
NautilusTrader replay.

What this module is **not**
---------------------------
It is not an execution engine, does not model a venue, queue position, partial
fills, funding or exchange limits, and never claims to be one. Every row it
produces is ``MODEL_DERIVED`` and carries the assumptions used.

Declared causality rules (enforced, not advisory)
------------------------------------------------
1. The entry bar's own high/low are NOT post-entry excursion. Measurement starts
   at the first bar whose close time is strictly after the entry timestamp.
2. A stop/target is evaluated on a closed bar only. If both are inside one bar's
   range, the declared intrabar ordering (default ``STOP_FIRST``) decides.
3. A stop modification triggered by bar *n* is effective from bar *n+1*: the
   decision maker only learns the trigger at bar *n*'s close.
4. A resting limit order (partial realisation at +1R) fills when the bar range
   touches it. Touch is treated as fill, which is an optimistic assumption and
   is labelled ``TOUCH_FILL_MODEL``.
5. Nothing uses a future bar index to choose an entry.
"""

from __future__ import annotations

import json
import math
import random
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

NS_PER_MS = 1_000_000


@dataclass(frozen=True)
class Bar:
    end_ns: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class FeeModel:
    """Fee rates as fractions of notional, charged per side."""

    maker: float = 0.0002
    taker: float = 0.0005

    def fee_usdt(self, price: float, quantity: float, *, maker: bool) -> float:
        return abs(price * quantity) * (self.maker if maker else self.taker)


DEFAULT_STOP_PCT = 0.02
DEFAULT_TARGET_PCT = 0.04
INTRABAR = "STOP_FIRST"


def load_bars(
    tape_path: Path | str,
    *,
    instrument: str = "BTCUSDT",
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> list[Bar]:
    """Load closed kline bars for one instrument from a multitape JSONL file.

    The tape records ``event_time`` in milliseconds; ``Bar.end_ns`` is the true
    nanosecond close instant, so a bar can be compared directly with a native
    timestamp (``Position.ts_opened``, ``FundingRateUpdate.ts_event``). Mixing
    the two units silently skipped every bar, which is why the conversion is
    explicit here and not left to callers.
    """
    bars: list[Bar] = []
    with open(tape_path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("channel") != "kline":
                continue
            if str(row.get("instrument")) != instrument:
                continue
            payload = row.get("payload") or {}
            if not payload.get("closed", True):
                continue
            open_ms = int(payload.get("open_time_ms") or 0)
            if start_ms is not None and open_ms < start_ms:
                continue
            if end_ms is not None and open_ms >= end_ms:
                continue
            bars.append(
                Bar(
                    end_ns=int(row["event_time"]) * NS_PER_MS,
                    open=float(payload["open"]),
                    high=float(payload["high"]),
                    low=float(payload["low"]),
                    close=float(payload["close"]),
                    volume=float(payload.get("volume") or 0.0),
                )
            )
    bars.sort(key=lambda bar: bar.end_ns)
    return bars


def load_funding(
    tape_path: Path | str,
    *,
    instrument: str = "BTCUSDT",
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> tuple[tuple[int, float], ...]:
    """Load ``(settlement_ns, rate)`` funding rows for one instrument.

    The settlement instant is the venue's ``funding_time_ms``: that is when the
    charge is actually applied. A row without it is not invented from the event
    time, it is skipped and remains absent.
    """
    rows: list[tuple[int, float]] = []
    with open(tape_path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("channel") != "funding":
                continue
            if str(row.get("instrument")) != instrument:
                continue
            payload = row.get("payload") or {}
            settle_ms = payload.get("funding_time_ms")
            rate = payload.get("funding_rate")
            if settle_ms is None or rate is None:
                continue
            settle_ms = int(settle_ms)
            if start_ms is not None and settle_ms < start_ms:
                continue
            if end_ms is not None and settle_ms >= end_ms:
                continue
            rows.append((settle_ms * NS_PER_MS, float(rate)))
    rows.sort(key=lambda item: item[0])
    return tuple(rows)


def bar_index_at(bars: Sequence[Bar], ts_ns: int) -> int:
    """Index of the first bar whose close is strictly after ``ts_ns``."""
    ends = [bar.end_ns for bar in bars]
    return bisect_right(ends, ts_ns)


def read_jsonl(path: Path | str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    except FileNotFoundError:
        return []
    return rows


def load_run(run_dir: Path | str) -> dict[str, list[dict[str, Any]]]:
    """Load the persisted evidence of one native audit run."""
    root = Path(run_dir)
    return {
        "lifecycles": read_jsonl(root / "trade_lifecycles.jsonl"),
        "fills": read_jsonl(root / "fills.jsonl"),
        "opportunities": read_jsonl(root / "opportunities.jsonl"),
        "events": read_jsonl(root / "events.jsonl"),
        "orders": read_jsonl(root / "orders.jsonl"),
    }


def load_stances(events: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, str]]:
    """Map opportunity_id -> {expert_id: stance} from the event ledger."""
    out: dict[str, dict[str, str]] = {}
    for event in events:
        if event.get("event_type") != "EXPERT_EVALUATED":
            continue
        opp = event.get("opportunity_id")
        expert = event.get("expert_id")
        if not opp or not expert:
            continue
        out.setdefault(str(opp), {})[str(expert)] = str(event.get("stance") or "")
    return out


def _round(value: float | None, digits: int = 10) -> float | None:
    return None if value is None else round(value, digits)


def evaluate_path(
    bars: Sequence[Bar],
    *,
    entry_ns: int,
    entry: float,
    direction: int,
    stop: float | None,
    target: float | None,
    policy: str = "STATIC",
    risk_budget_usdt: float | None = 100.0,
    fee_model: FeeModel | None = None,
    partial_fraction: float = 0.5,
    partial_trigger_r: float = 1.0,
    max_bars: int | None = None,
    slippage_bps: float = 0.0,
    funding: Sequence[tuple[int, float]] = (),
    target_maker: bool = False,
) -> dict[str, Any]:
    """Evaluate one declared exit policy from a declared entry.

    ``policy``:
      ``STATIC``          initial stop/target only
      ``BREAKEVEN_1R``    stop moves to breakeven (+round-trip cost) after +1R
      ``PARTIAL_50_1R``   half realised at +1R, rest under the initial bracket
      ``TIME_STOP``       flat exit at the close of ``max_bars`` bars

    ``slippage_bps`` is charged per side on market fills only (entry, stop,
    time stop, cutoff liquidation). ``target_maker`` charges the maker fee and
    no slippage on a target fill, because the target is a resting limit order.
    ``funding`` carries ``(settlement_ns, rate)`` pairs; a positive rate is paid
    by longs and received by shorts. Both terms are reported separately so the
    accounting identity stays auditable.
    """
    if stop is None:
        return {"outcome": "NO_STOP", "net_r": None, "metric_unavailable_reason": "NO_INITIAL_STOP"}
    fees = fee_model or FeeModel()
    risk_price = abs(entry - stop)
    if risk_price <= 0:
        return {"outcome": "ZERO_RISK", "net_r": None, "metric_unavailable_reason": "ZERO_STOP_DISTANCE"}

    # Size from a fixed risk budget (1% of the 10,000 USDT account) so USDT
    # figures are comparable with the native run. R economics are scale
    # invariant; only the nominal size depends on this choice.
    qty = (risk_budget_usdt / risk_price) if risk_budget_usdt else 1.0
    slip = slippage_bps / 10_000.0
    entry_fill = entry + direction * entry * slip
    remaining = qty
    realised_usdt = 0.0
    slippage_usdt = qty * abs(entry_fill - entry)
    fees_usdt = fees.fee_usdt(entry_fill, qty, maker=False)  # entry crosses
    active_stop = stop
    stop_updated = False
    bars_seen = 0
    outcome = "OPEN_AT_CUTOFF"
    exit_ns: int | None = None
    exit_price: float | None = None
    trigger_bar: int | None = None
    partial_done = False

    for bar in bars:
        if bar.end_ns <= entry_ns:
            continue
        bars_seen += 1
        last_bar = bar
        favourable = (bar.high - entry_fill) if direction > 0 else (entry_fill - bar.low)
        trigger_price = entry_fill + direction * risk_price * partial_trigger_r
        triggers = favourable >= risk_price * partial_trigger_r

        stop_hit = (bar.low <= active_stop) if direction > 0 else (bar.high >= active_stop)
        target_hit = target is not None and (
            (bar.high >= target) if direction > 0 else (bar.low <= target)
        )

        # Intrabar ordering decides when both levels live inside the same bar.
        if stop_hit and (target_hit is False or INTRABAR == "STOP_FIRST"):
            exit_price = active_stop
            exit_ns = bar.end_ns
            outcome = "BREAKEVEN_STOP" if stop_updated and stop_updated and active_stop != stop else "STOP"
            break
        if target_hit:
            exit_price = target
            exit_ns = bar.end_ns
            outcome = "TARGET"
            break

        if policy == "PARTIAL_50_1R" and not partial_done and triggers:
            close_qty = qty * partial_fraction
            realised_usdt += (trigger_price - entry_fill) * direction * close_qty
            fees_usdt += fees.fee_usdt(trigger_price, close_qty, maker=True)
            remaining -= close_qty
            partial_done = True
            trigger_bar = bars_seen

        if policy == "BREAKEVEN_1R" and not stop_updated and triggers:
            # Effective from the NEXT bar: the trigger is only known at close.
            cost_buffer = abs(entry_fill) * 2 * fees.taker
            active_stop = entry_fill + direction * cost_buffer
            stop_updated = True
            trigger_bar = bars_seen

        if policy == "TIME_STOP" and max_bars is not None and bars_seen == max_bars:
            exit_price = bar.close
            exit_ns = bar.end_ns
            outcome = "TIME_STOP"
            break

    exit_fill: float | None = None
    exit_maker = outcome == "TARGET" and target_maker
    if exit_price is not None and remaining > 0:
        exit_fill = exit_price if exit_maker else exit_price - direction * exit_price * slip
        realised_usdt += (exit_fill - entry_fill) * direction * remaining
        slippage_usdt += remaining * abs(exit_fill - exit_price)
        fees_usdt += fees.fee_usdt(exit_fill, remaining, maker=exit_maker)
    elif remaining > 0 and bars_seen > 0:
        # Still open at the end of the measured window: mark to market at the
        # last closed bar so an open campaign is not silently valued at zero.
        last = locals().get("last_bar")
        if last is not None:
            exit_price = last.close
            exit_ns = last.end_ns
            outcome = "OPEN_AT_CUTOFF_MTM"
            exit_fill = exit_price - direction * exit_price * slip
            realised_usdt += (exit_fill - entry_fill) * direction * remaining
            slippage_usdt += remaining * abs(exit_fill - exit_price)

    # A positive funding rate is paid by longs and received by shorts. The
    # charge uses the position notional at entry; settlements outside the
    # holding window are not attributed to this campaign.
    funding_usdt = 0.0
    if funding and exit_ns is not None:
        notional = qty * entry_fill
        for settle_ns, rate in funding:
            if entry_ns < settle_ns <= exit_ns:
                funding_usdt += direction * notional * rate

    gross_usdt = realised_usdt
    net_usdt = gross_usdt - fees_usdt - funding_usdt
    risk_usdt = risk_price * qty
    return {
        "outcome": outcome,
        "exit_ns": exit_ns,
        "exit_price": _round(exit_price),
        "exit_fill_price": _round(exit_fill),
        "entry_fill_price": _round(entry_fill),
        "gross_usdt": _round(gross_usdt),
        "net_usdt": _round(net_usdt),
        "fees_usdt": _round(fees_usdt),
        "slippage_usdt": _round(slippage_usdt),
        "funding_usdt": _round(funding_usdt),
        "gross_r": _round(gross_usdt / risk_usdt),
        "net_r": _round(net_usdt / risk_usdt),
        "risk_usdt": _round(risk_usdt),
        # Alias so the shared metric summarizer reads one risk column name
        # across native and counterfactual rows.
        "initial_risk_usdt": _round(risk_usdt),
        "bars_measured": bars_seen,
        "stop_moved": stop_updated,
        "trigger_bar": trigger_bar,
        "partial_done": partial_done,
        "intrabar_contract": INTRABAR,
        "limit_fill_model": "TOUCH_FILL_MODEL" if partial_done else None,
        "slippage_bps_per_side": slippage_bps,
        "funding_included": bool(funding),
        "target_fill_model": ("MAKER_LIMIT" if target_maker else "TAKER_FALLBACK"),
        "evidence_class": "MODEL_DERIVED",
    }


def static_bracket(
    entry: float,
    direction: int,
    *,
    stop_pct: float = DEFAULT_STOP_PCT,
    target_pct: float = DEFAULT_TARGET_PCT,
) -> tuple[float, float]:
    stop = entry - direction * entry * stop_pct
    target = entry + direction * entry * target_pct
    return stop, target


def exit_policy_rows(
    lifecycles: Sequence[Mapping[str, Any]],
    bars: Sequence[Bar],
    policy: str,
    *,
    fee_model: FeeModel | None = None,
) -> list[dict[str, Any]]:
    """Paired exit-policy comparison on the SAME native entries.

    The native entry price/timestamp/stop/target come from the audit ledger; the
    only thing that changes is the exit rule.
    """
    rows: list[dict[str, Any]] = []
    for lifecycle in lifecycles:
        entry = lifecycle.get("entry_price")
        opened_ns = lifecycle.get("opened_ns")
        stop = lifecycle.get("initial_stop_price")
        target = lifecycle.get("initial_target_price")
        if entry is None or opened_ns is None:
            continue
        direction = 1 if str(lifecycle.get("direction")).upper() == "LONG" else -1
        result = evaluate_path(
            bars,
            entry_ns=int(opened_ns),
            entry=float(entry),
            direction=direction,
            stop=None if stop is None else float(stop),
            target=None if target is None else float(target),
            policy=policy,
            fee_model=fee_model,
            max_bars=18 if policy == "TIME_STOP" else None,
        )
        rows.append(
            {
                "campaign_id": lifecycle.get("campaign_id"),
                "opportunity_id": lifecycle.get("opportunity_id"),
                "direction": lifecycle.get("direction"),
                "policy": policy,
                "entry_price": entry,
                "entry_ns": opened_ns,
                "native_exit_kind": lifecycle.get("exit_kind"),
                "native_net_r": lifecycle.get("realized_net_r"),
                "native_net_pnl_usdt": lifecycle.get("realized_net_pnl_usdt"),
                **result,
            }
        )
    return rows


def opportunity_path_rows(
    opportunities: Sequence[Mapping[str, Any]],
    bars: Sequence[Bar],
    *,
    policy: str = "STATIC",
    entry_offset_bars: int = 0,
    fee_model: FeeModel | None = None,
    stop_pct: float = DEFAULT_STOP_PCT,
    target_pct: float = DEFAULT_TARGET_PCT,
) -> list[dict[str, Any]]:
    """Evaluate every DETECTED opportunity under a declared entry/exit policy.

    ``entry_offset_bars=0`` enters at the detection bar's close (baseline
    same-bar admission). ``1`` enters at the next closed bar's close. The entry
    bar is never measured for excursion (see module docstring).
    """
    by_end = [bar.end_ns for bar in bars]
    rows: list[dict[str, Any]] = []
    for opportunity in opportunities:
        if opportunity.get("event_type") not in (None, "OPPORTUNITY_DETECTED"):
            continue
        opp_ns = int(opportunity.get("event_time_ns") or opportunity.get("as_of_time_ns") or 0)
        direction = 1 if str(opportunity.get("direction")).upper().endswith("LONG") else -1
        if not opp_ns:
            continue
        index = bisect_right(by_end, opp_ns) + entry_offset_bars - 1
        if index < 0 or index >= len(bars):
            continue
        bar = bars[index]
        entry = bar.close
        stop, target = static_bracket(entry, direction, stop_pct=stop_pct, target_pct=target_pct)
        result = evaluate_path(
            bars,
            entry_ns=bar.end_ns,
            entry=entry,
            direction=direction,
            stop=stop,
            target=target,
            policy="STATIC",
            fee_model=fee_model,
        )
        rows.append(
            {
                "opportunity_id": opportunity.get("opportunity_id"),
                "direction": opportunity.get("direction"),
                "detected_ns": opp_ns,
                "entry_ns": bar.end_ns,
                "entry_delay_bars": entry_offset_bars,
                "entry_price": entry,
                "policy": policy,
                **result,
            }
        )
    return rows


def retest_rows(
    opportunities: Sequence[Mapping[str, Any]],
    bars: Sequence[Bar],
    *,
    ttl_bars: int = 6,
    fee_model: FeeModel | None = None,
    stop_pct: float = DEFAULT_STOP_PCT,
    target_pct: float = DEFAULT_TARGET_PCT,
) -> list[dict[str, Any]]:
    """Bounded retest lifecycle: wait for the level to be touched, else expire.

    A resting limit at the detection price fills when the bar range touches it
    (``TOUCH_FILL_MODEL``). Invalidation is a breach of the detection stop;
    expiry is ``ttl_bars`` closed bars without a touch.
    """
    by_end = [bar.end_ns for bar in bars]
    rows: list[dict[str, Any]] = []
    for opportunity in opportunities:
        if opportunity.get("event_type") not in (None, "OPPORTUNITY_DETECTED"):
            continue
        opp_ns = int(opportunity.get("event_time_ns") or opportunity.get("as_of_time_ns") or 0)
        direction = 1 if str(opportunity.get("direction")).upper().endswith("LONG") else -1
        level = opportunity.get("entry_price")
        if not opp_ns or level is None:
            continue
        level_f = float(level)
        stop, target = static_bracket(
            level_f, direction, stop_pct=stop_pct, target_pct=target_pct
        )
        start = bisect_right(by_end, opp_ns)
        state = "WATCHING"
        outcome = "EXPIRED"
        fill_bar_ns = None
        rows_measured = 0
        for bar in bars[start : start + ttl_bars]:
            rows_measured += 1
            invalidated = (bar.low <= stop) if direction > 0 else (bar.high >= stop)
            if invalidated:
                state, outcome = "INVALIDATED", "INVALIDATED"
                break
            touched = (bar.low <= level_f) if direction > 0 else (bar.high >= level_f)
            if touched:
                state, outcome = "ENTERED", "RETEST_FILLED"
                fill_bar_ns = bar.end_ns
                break
        if fill_bar_ns is None:
            rows.append(
                {
                    "opportunity_id": opportunity.get("opportunity_id"),
                    "direction": opportunity.get("direction"),
                    "detected_ns": opp_ns,
                    "policy": "RETEST_TTL",
                    "state": state,
                    "outcome": outcome,
                    "entry_price": None,
                    "net_r": None,
                    "net_pnl_usdt": None,
                    "fill_bar_ns": None,
                    "bars_watched": rows_measured,
                    "evidence_class": "MODEL_DERIVED",
                }
            )
            continue
        result = evaluate_path(
            bars,
            entry_ns=fill_bar_ns,
            entry=level_f,
            direction=direction,
            stop=stop,
            target=target,
            policy="STATIC",
            fee_model=fee_model,
        )
        rows.append(
            {
                "opportunity_id": opportunity.get("opportunity_id"),
                "direction": opportunity.get("direction"),
                "detected_ns": opp_ns,
                "policy": "RETEST_TTL",
                "state": "ENTERED",
                "outcome": result["outcome"],
                "entry_price": level_f,
                "fill_bar_ns": fill_bar_ns,
                "bars_watched": rows_measured,
                **result,
                # Explicit after the spread so the model label is authoritative.
                "limit_fill_model": "TOUCH_FILL_MODEL",
                "evidence_class": "MODEL_DERIVED",
            }
        )
    return rows


def matched_random_sample(
    rows: Sequence[Mapping[str, Any]],
    *,
    size: int,
    seed: int,
    key: str = "direction",
) -> list[Mapping[str, Any]]:
    """Seeded random control matched on ``key`` (direction/volatility bucket)."""
    rng = random.Random(seed)
    buckets: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(str(row.get(key)), []).append(row)
    quotas = {name: 0 for name in buckets}
    total = sum(len(v) for v in buckets.values())
    for name, values in buckets.items():
        quotas[name] = max(1, round(size * len(values) / total)) if values else 0
    sample: list[Mapping[str, Any]] = []
    for name, values in buckets.items():
        count = min(quotas[name], len(values))
        sample.extend(rng.sample(list(values), count))
    return sample


def volatility_bucket(row: Mapping[str, Any], bars: Sequence[Bar]) -> str:
    """Coarse ex-ante volatility bucket from the bars BEFORE the decision."""
    entry_ns = int(row.get("entry_ns") or row.get("detected_ns") or 0)
    index = bisect_right([bar.end_ns for bar in bars], entry_ns) - 1
    window = bars[max(0, index - 24) : max(1, index)]
    if not window:
        return "NA"
    ranges = [(bar.high - bar.low) / bar.close for bar in window if bar.close]
    if not ranges:
        return "NA"
    spread = sum(ranges) / len(ranges)
    if spread < 0.01:
        return "LOW"
    if spread < 0.02:
        return "MID"
    return "HIGH"


def wilson_interval(successes: int, trials: int, z: float = 1.96) -> tuple[float, float] | None:
    if trials <= 0:
        return None
    p = successes / trials
    denominator = 1 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denominator
    margin = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denominator
    return (max(0.0, centre - margin), min(1.0, centre + margin))
