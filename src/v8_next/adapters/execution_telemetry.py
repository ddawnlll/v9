"""Execution telemetry: turn a Nautilus fills report into measured evidence.

Kept separate from the engine adapters so both the single-instrument D-153
runner and the multi-asset portfolio backtest measure execution the same way
and cannot drift apart.

Honesty rules encoded here:
  * a value that cannot be parsed stays ``None`` -- never a placeholder;
  * non-finite floats (the engine emits ``nan`` slippage on some rows) become
    ``None`` so a receipt stays strictly JSON-serialisable;
  * amounts in different currencies are never summed together;
  * shortfall compares a fill only against the decision of the SAME instrument,
    because comparing legs compares different price scales;
  * the signed mean shortfall (net cost) and the mean magnitude of shortfall
    (deviation from the decision price) are both published, because averaging a
    signed quantity into a fidelity score lets opposite-signed fills cancel.

Decision-trail binding (additive surface, EEO-001H / EEO-002 / D-136):

  * :func:`attach_decision_trail` attaches a structured, hash-bound decision trail to a decision
    record, so ``v8_next.app.explain`` can answer a single-trade counterfactual with the PIT
    decision lineage beside it instead of prose. Spans must be PIT ``DecisionSpan``s on the
    context's own trace; an evidence-plane or foreign-trace span is refused by name;
  * :func:`decision_trail_digest` / :func:`verify_decision_trail` are the fail-closed read side:
    a trail whose digest does not re-derive from its content is not a trail;
  * nothing above changes the behaviour of the measurement functions in this module, and the
    input record is never mutated: the trail is returned in a copy.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from v8_next.adapters.execution_models import ExecutionProfile, profile_summary
from v8_next.telemetry import (
    DecisionSpan,
    EconomicTraceContext,
    TraceLineageError,
    canonical_digest,
)

#: Fill-report columns that carry execution semantics. The set is intersected
#: with the columns the engine actually emitted, so a Nautilus version that
#: renames a column cannot silently produce an all-identical signature.
SIGNATURE_COLUMNS = (
    "instrument_id",
    "side",
    "quantity",
    "filled_qty",
    "last_px",
    "avg_px",
    "slippage",
    "commissions",
    "liquidity_side",
    "position_id",
    "order_list_id",
    "venue_order_id",
    "trade_id",
    "ts_event",
    "ts_init",
    "ts_last",
)


def as_float(value: Any) -> float | None:
    """Convert a ledger/report value to a finite float, or None. Never guesses."""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def json_safe(value: Any) -> Any:
    """JSON-safe scalar: pandas timestamps become epoch ns, non-finite floats None."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return value
    if type(value).__name__ == "Timestamp" and hasattr(value, "value"):
        return int(value.value)
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return str(value)


def money_amounts(value: Any) -> dict[str, float]:
    """Parse Nautilus money values into ``{currency: summed amount}``.

    The fills report carries commissions as ``["0.49407624 USDT"]``; custom fee
    models can surface ``Money(0.5, USDT)`` instead. Both are accepted, and
    amounts in different currencies are never summed together.
    """
    out: dict[str, float] = {}
    if value is None or isinstance(value, bool):
        return out
    if isinstance(value, (list, tuple)):
        for item in value:
            for currency, amount in money_amounts(item).items():
                out[currency] = out.get(currency, 0.0) + amount
        return out
    if isinstance(value, (int, float)):
        if value != value:
            return out
        out[""] = float(value)
        return out
    text = str(value)
    for amount, currency in re.findall(r"([-+]?[0-9]*\.?[0-9]+)\s+([A-Za-z]{2,10})\b", text):
        try:
            parsed = float(amount)
        except ValueError:
            continue
        out[currency.upper()] = out.get(currency.upper(), 0.0) + parsed
    for amount, currency in re.findall(
        r"Money\(\s*([-+]?[0-9]*\.?[0-9]+)\s*,\s*([A-Za-z]{2,10})", text
    ):
        try:
            parsed = float(amount)
        except ValueError:
            continue
        out[currency.upper()] = out.get(currency.upper(), 0.0) + parsed
    return out


def native_fill_records(engine: Any) -> tuple[list[dict[str, Any]], str]:
    """Normalise the engine's order-fills report into JSON-safe records.

    NautilusTrader 2.0.0rc4 returns a pandas DataFrame rather than a declared
    stub type, so the shape is probed at runtime and reported verbatim instead
    of being assumed.
    """
    try:
        report = engine.generate_order_fills_report()
    except Exception as exc:  # pragma: no cover - engine-level failure
        return [], f"UNAVAILABLE:{type(exc).__name__}"
    if report is None:
        return [], "NONE"
    records: list[dict[str, Any]] = []
    try:
        if hasattr(report, "to_dicts"):
            records = [dict(r) for r in report.to_dicts()]
        elif hasattr(report, "to_dict"):
            try:
                raw = report.to_dict("records")
                records = [dict(r) for r in raw]
            except TypeError:
                records = [dict(report.to_dict())]
        elif isinstance(report, (list, tuple)):
            records = [dict(r) for r in report if isinstance(r, dict)]
    except Exception:  # pragma: no cover - defensive
        return [], f"UNPARSABLE:{type(report).__name__}"
    safe = [{str(key): json_safe(value) for key, value in rec.items()} for rec in records]
    return safe, type(report).__name__


def fill_signature(records: list[dict[str, Any]]) -> str:
    """Identity of executed fills: instrument/side/qty/price/cost/time, sorted.

    Price, slippage and commission columns are part of the identity on purpose:
    a signature that ignores them cannot detect that two profiles filled at
    different prices, which is exactly what an execution claim depends on.
    """
    import hashlib as _hl
    import json as _js

    if not records:
        return _hl.sha256(b'{"columns":[],"rows":[]}').hexdigest()
    available = set(records[0])
    pick = [c for c in SIGNATURE_COLUMNS if c in available]
    if not pick:  # unknown schema: fall back to every column, still deterministic
        pick = sorted(available)
    rows = sorted(tuple(str(r.get(k, "")) for k in pick) for r in records)
    payload = _js.dumps({"columns": pick, "rows": rows}, sort_keys=True)
    return _hl.sha256(payload.encode()).hexdigest()


#: A fill and the decision bar that authorised it must describe the same price
#: series. A larger ratio means they do not, so the pair is rejected instead of
#: being averaged into a friction metric.
MAX_REFERENCE_RATIO = 1.5


def configuration_cannot_slip(block: dict[str, Any]) -> bool:
    """Declared semantics that make a fill deviation impossible by construction.

    ``prob_slippage`` of zero with a fill model that never slips places every fill
    at the price the decision asked for, so the magnitudes such a run produces are
    a property of the configuration instead of a measurement of the venue. The
    consumer reads this signal (see
    ``scoring.EXECUTION_FIDELITY_CANNOT_SLIP_SIGNAL``) so a degenerate statistic
    is named where it is consumed rather than re-derived from the profile.
    """
    prob_slippage = block.get("prob_slippage")
    if isinstance(prob_slippage, bool) or not isinstance(prob_slippage, (int, float)):
        return False
    return float(prob_slippage) <= 0.0 and not block.get("fill_model_slipped")


def persist_execution_telemetry(path: Any, block: dict[str, Any]) -> Any:
    """Write an execution-telemetry artifact that carries its own digest.

    The digest covers every field except ``sha256`` itself, so a reader can
    detect later edits. Friction values taken from this file are therefore
    traceable to the run that measured them, rather than assumed.
    """
    import hashlib as _hl
    import json as _js
    from pathlib import Path as _Path

    out = _Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    body = {k: v for k, v in block.items() if k != "sha256"}
    digest = _hl.sha256(_js.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
    out.write_text(
        _js.dumps({**body, "sha256": digest}, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    return out


def load_execution_telemetry(path: Any) -> dict[str, Any]:
    """Load a telemetry artifact written by :func:`persist_execution_telemetry`.

    Returns ``{}`` when the file is absent, unreadable, malformed, or fails its
    own digest. An empty result keeps a consumer failing closed on missing
    evidence instead of receiving a cost that was never measured.
    """
    import hashlib as _hl
    import json as _js
    from pathlib import Path as _Path

    src = _Path(path)
    if not src.exists():
        return {}
    try:
        payload = _js.loads(src.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    recorded = payload.get("sha256")
    body = {k: v for k, v in payload.items() if k != "sha256"}
    computed = _hl.sha256(_js.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
    if recorded != computed:
        return {}
    return body


def execution_friction_inputs(block: dict[str, Any]) -> dict[str, Any]:
    """Map a telemetry block onto the friction fields a utility decision needs.

    Only measured values are returned. A field with no measurement is omitted so
    the caller keeps its ``None`` (fail-closed) rather than receiving a zero that
    would be read as a free cost.
    """
    if not block:
        return {}
    samples = block.get("slippage_samples") or 0
    out: dict[str, Any] = {}
    if samples > 0 and isinstance(block.get("slippage_bps_mean"), (int, float)):
        mean_bps = float(block["slippage_bps_mean"])
        out["slippage"] = abs(mean_bps) / 1e4
    total = block.get("commission_total")
    if isinstance(total, (int, float)):
        out["fees"] = abs(float(total))
    digest = block.get("digest")
    if digest:
        out["calibration_receipt"] = f"execution_profile:{block.get('profile')}:{digest}"
    return out


def execution_telemetry(
    profile: ExecutionProfile,
    fill_records: list[dict[str, Any]],
    fill_report_type: str,
    opened: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Measure execution quality from fills versus the decision price.

    Implementation shortfall is the signed cost of the fill against the close of
    the decision bar that authorised it: positive means the fill was adverse
    (paid above for a buy, sold below for a sell). Order lifetime comes from the
    engine's own fill rows, because bar execution stamps fills at the bar
    boundary and therefore cannot show modelled order latency.

    Two shortfall statistics are published: the signed mean (``slippage_bps_mean``,
    the net cost of trading) and the mean magnitude (``slippage_bps_abs_mean``,
    how far fills landed from the price the decision asked for). The magnitude is
    the one the ExecutionFidelity convention is declared over -- averaging signed
    deviations lets adverse and favourable fills cancel, which is a fidelity
    measurement reporting "no deviation" for two fills that both deviated.
    """
    # Decisions are matched per instrument: comparing a fill against the last
    # decision of a *different* leg compares BTC against AVAX and yields a
    # nonsense shortfall (observed as -2.4e6 bps before this was fixed).
    per_instrument: dict[str, list[tuple[int, Any]]] = {}
    for d in decisions:
        inst = str(d.get("instrument_id", ""))
        per_instrument.setdefault(inst, []).append(
            (int(d.get("decision_ns", 0) or 0), d.get("close"))
        )
    for rows in per_instrument.values():
        rows.sort(key=lambda r: r[0])

    slippage_bps: list[float] = []
    slippage_magnitudes: list[float] = []
    latencies: list[int] = []
    unmatched = 0
    cross_series = 0
    for pos in opened:
        side = str(pos.get("side", "")).upper()
        fill_px = as_float(pos.get("avg_px_open"))
        if fill_px is None or fill_px <= 0:
            unmatched += 1
            continue
        event_ns = int(pos.get("event_ns", 0) or 0)
        rows = per_instrument.get(str(pos.get("instrument_id", "")), [])
        prior = [r for r in rows if r[0] <= event_ns]
        if not prior:
            unmatched += 1
            continue
        ref_ns, ref_close = prior[-1]
        ref_px = as_float(ref_close)
        if ref_px is None or ref_px <= 0:
            unmatched += 1
            continue
        # A fill and its authorising decision bar must describe the same price
        # series. A ratio outside MAX_REFERENCE_RATIO means they do not -- this
        # happens when a single-instrument configuration is fed a multi-asset
        # tape, which produced a -5.8e7 bps "shortfall" before this guard.
        # Such pairs are rejected and counted, never averaged into the metric.
        if max(fill_px, ref_px) / min(fill_px, ref_px) > MAX_REFERENCE_RATIO:
            cross_series += 1
            continue
        if side in {"BUY", "LONG"}:
            sign = 1.0
        elif side in {"SELL", "SHORT"}:
            sign = -1.0
        else:
            # Unknown side names must not be silently treated as short.
            unmatched += 1
            continue
        signed_bps = sign * (fill_px - ref_px) / ref_px * 1e4
        slippage_bps.append(signed_bps)
        slippage_magnitudes.append(abs(signed_bps))
        latencies.append(event_ns - ref_ns)

    commission_totals: dict[str, float] = {}
    for rec in fill_records:
        for key in rec:
            if "commission" in key.lower():
                for currency, amount in money_amounts(rec[key]).items():
                    commission_totals[currency] = commission_totals.get(currency, 0.0) + amount

    order_lifetime: list[int] = []
    for rec in fill_records:
        start, end = rec.get("ts_init"), rec.get("ts_last")
        if isinstance(start, int) and isinstance(end, int) and end >= start:
            order_lifetime.append(end - start)

    configured_latency = sum(
        (
            profile.base_latency_nanos,
            profile.insert_latency_nanos,
            profile.update_latency_nanos,
            profile.cancel_latency_nanos,
        )
    )
    block = profile_summary(profile)
    block.update(
        {
            "fill_signature": fill_signature(fill_records),
            "fills_count": len(fill_records),
            "fills_report_type": fill_report_type,
            "slippage_samples": len(slippage_bps),
            "slippage_unmatched_positions": unmatched,
            "slippage_rejected_cross_series": cross_series,
            "slippage_bps_mean": (
                round(sum(slippage_bps) / len(slippage_bps), 6) if slippage_bps else None
            ),
            #: The statistic the ExecutionFidelity convention is declared over: the
            #: mean *magnitude* of the per-fill shortfall. Published next to the
            #: signed mean on purpose, because they answer different questions: the
            #: signed mean is the net cost of trading (friction), while the mean
            #: magnitude is how far fills landed from the price the decision asked
            #: for (fidelity). Opposite-signed fills cancel in the signed mean --
            #: measured on the 120-bar quad window, per-fill deviations of ~0.05 bps
            #: in both directions collapsed to a signed mean of 0.0004 bps, which is
            #: why the published fidelity was a constant before this field existed.
            "slippage_bps_abs_mean": (
                round(sum(slippage_magnitudes) / len(slippage_magnitudes), 6)
                if slippage_magnitudes
                else None
            ),
            "slippage_bps_max": round(max(slippage_bps), 6) if slippage_bps else None,
            "slippage_bps_min": round(min(slippage_bps), 6) if slippage_bps else None,
            #: Degeneracy signals for the magnitude statistic above (#439). A zero
            #: magnitude is either every sample being identical or a declared
            #: configuration that cannot slip, and neither discriminates execution
            #: quality. Published here so a consumer abstains on evidence the
            #: producer named, instead of scoring the top of its range on a
            #: statistic that cannot vary.
            "slippage_bps_abs_max": (
                round(max(slippage_magnitudes), 6) if slippage_magnitudes else None
            ),
            "slippage_magnitudes_all_zero": (
                all(magnitude == 0.0 for magnitude in slippage_magnitudes)
                if slippage_magnitudes
                else None
            ),
            "slippage_configuration_cannot_slip": configuration_cannot_slip(block),
            "decision_to_position_event_ns_mean": (
                int(sum(latencies) / len(latencies)) if latencies else None
            ),
            "decision_to_position_event_ns_max": max(latencies) if latencies else None,
            "order_to_fill_ns_mean": (
                int(sum(order_lifetime) / len(order_lifetime)) if order_lifetime else None
            ),
            "order_to_fill_ns_max": max(order_lifetime) if order_lifetime else None,
            "configured_latency_nanos": configured_latency,
            "latency_observability": (
                "BAR_EXECUTION_STAMPS_FILLS_AT_BAR_TIME"
                if configured_latency > 0 and not any(latencies)
                else "MEASURED_ORDER_LIFETIME"
                if order_lifetime
                else "NOT_OBSERVABLE"
            ),
            "commission_totals_by_currency": {
                cur: round(amt, 10) for cur, amt in sorted(commission_totals.items())
            },
            "commission_total": (
                round(next(iter(commission_totals.values())), 10)
                if len(commission_totals) == 1
                else None
            ),
        }
    )
    return block


#: Field the decision-trail digest is carried in. Excluded from its own preimage, so a reader can
#: recompute what a recorded trail claims without stripping anything by hand.
TRAIL_DIGEST_FIELD = "trail_digest"

#: Refusal codes for the trail binding, so a consumer branches on the code and never on prose.
TRAIL_SPAN_NOT_PIT_DECISION = "TRAIL_SPAN_NOT_PIT_DECISION"
TRAIL_TRACE_LINEAGE_MISMATCH = "TRAIL_TRACE_LINEAGE_MISMATCH"
TRAIL_DIGEST_MISSING = "TRAIL_DIGEST_MISSING"
TRAIL_DIGEST_MISMATCH = "TRAIL_DIGEST_MISMATCH"


def decision_trail_digest(trail: Mapping[str, Any]) -> str:
    """Content digest of a decision trail, excluding the digest field itself.

    Domain-separated as ``DecisionTrail`` and computed over the repository's canonical JSON, so
    the same trail content yields the same digest and no wall clock is involved.
    """
    body = {key: value for key, value in trail.items() if key != TRAIL_DIGEST_FIELD}
    return canonical_digest("DecisionTrail", body)


def verify_decision_trail(trail: Mapping[str, Any]) -> str:
    """Re-derive a trail digest from its content and fail closed on mismatch.

    Returns the digest when the trail is intact; raises :class:`TraceLineageError` when the digest
    is missing or does not re-derive, so a trail that was edited after recording is refused rather
    than read.
    """
    recorded = trail.get(TRAIL_DIGEST_FIELD)
    if not isinstance(recorded, str) or not recorded:
        raise TraceLineageError(
            "decision trail carries no digest", code=TRAIL_DIGEST_MISSING
        )
    expected = decision_trail_digest(trail)
    if recorded != expected:
        raise TraceLineageError(
            f"decision trail digest mismatch (recorded {recorded}, content yields {expected})",
            code=TRAIL_DIGEST_MISMATCH,
        )
    return expected


def _pit_decision_spans(
    trace_context: EconomicTraceContext, spans: Sequence[DecisionSpan]
) -> list[DecisionSpan]:
    """Validate the spans a trail may carry: PIT decision spans on this context's own trace."""
    ordered = list(spans)
    for span in ordered:
        if not isinstance(span, DecisionSpan):
            raise TraceLineageError(
                f"decision trail accepts PIT DecisionSpan only, got {type(span).__name__}",
                code=TRAIL_SPAN_NOT_PIT_DECISION,
            )
        if span.trace_id != trace_context.trace_id:
            raise TraceLineageError(
                f"span {span.span_id} belongs to trace {span.trace_id}, not to trace "
                f"{trace_context.trace_id}",
                code=TRAIL_TRACE_LINEAGE_MISMATCH,
            )
    return ordered


def attach_decision_trail(
    payload: dict[str, Any],
    *,
    trace_context: EconomicTraceContext,
    spans: Sequence[DecisionSpan],
) -> dict[str, Any]:
    """Attach a structured, hash-bound decision trail to a decision record.

    Returns a **new** mapping -- ``{**payload, "decision_trail": trail}`` -- so a sibling reader of
    the input record is unaffected. The trail is JSON-serialisable and carries:

    * ``trace_context``: opportunity id, trajectory modality/tag, PIT timestamp and provenance;
    * ``spans``: the ordered PIT decision spans, with their stages, links and receipt ids;
    * ``counterfactual_branches``: every ``SpanLinkType.CounterfactualBranch`` link, so
      ``v8_next.app.explain`` reads the counterfactual lineage off the trail rather than inferring
      it from the outcome;
    * ``trail_digest``: :func:`decision_trail_digest` over everything above.

    Raises :class:`TraceLineageError` when a span is not a PIT decision span or belongs to another
    trace: a trail is evidence of a decision path, so a span that was not on that path cannot be
    packaged into one.
    """
    if not isinstance(payload, dict):
        raise TypeError(f"decision record payload must be a dict, got {type(payload).__name__}")
    ordered = _pit_decision_spans(trace_context, spans)
    trail: dict[str, Any] = {
        "trajectory": trace_context.trajectory_type.as_str(),
        "is_observed": trace_context.trajectory_type.is_observed(),
        "is_counterfactual": trace_context.trajectory_type.is_counterfactual(),
        "trace_context": trace_context.as_dict(),
        "spans": [span.as_dict() for span in ordered],
        "stages": [span.stage.as_str() for span in ordered],
        "open_spans": sum(1 for span in ordered if not span.is_closed),
        "receipt_ids": [span.receipt_id for span in ordered if span.receipt_id],
        "counterfactual_branches": [
            link.as_dict() for span in ordered for link in span.counterfactual_branches
        ],
    }
    trail[TRAIL_DIGEST_FIELD] = decision_trail_digest(trail)
    return {**payload, "decision_trail": trail}
