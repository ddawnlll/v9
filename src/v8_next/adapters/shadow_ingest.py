"""Shadow / live ingest and reconciliation.

Public funding rows are in the quad tape (see funding_history.quad_funding_summary).
Private venue fills require an authenticated account; absent credentials => UNRUN.

Fixture files are never counted as live fills.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import time
from pathlib import Path
from typing import Any

FIXTURE_MARKERS = ("tests/", "fixtures", "fixture")

# Canonical live path (relative to repo root). Callers may pass an override.
DEFAULT_SHADOW_FILLS = Path("artifacts/shadow_fills.jsonl")


def is_fixture_path(p: Path | str) -> bool:
    s = str(p).replace("\\", "/").lower()
    return any(m.lower() in s for m in FIXTURE_MARKERS)


#: Provenance the caller must claim, and what it is allowed to mean. Only an
#: authenticated venue statement may read as settled; public paper is technical
#: evidence and can never become LIVE_VENUE_SETTLED (NX10.R4).
PROVENANCE_PUBLIC_PAPER = "public_paper"
PROVENANCE_AUTHENTICATED = "authenticated_venue_statement"

MODE_PUBLIC_PAPER = "PUBLIC_PAPER_NOT_SETTLED"
MODE_SETTLED = "LIVE_VENUE_SETTLED"
MODE_UNVERIFIED = "UNVERIFIED_PROVENANCE"

#: A caller-controlled provenance string is not venue authenticity. Settlement
#: evidence additionally needs a receipt produced by the trusted ingestion
#: process, bound to the fill file hash, venue, account and issue time, and
#: signed with an out-of-band key (``V8_VENUE_ATTESTATION_KEY``). The key is a
#: local trust anchor, not a claim that the venue signs statements.
ATTESTATION_VERSION = 1
ATTESTATION_VENUE = "BINANCE"
ATTESTATION_KEY_ENV = "V8_VENUE_ATTESTATION_KEY"
ATTESTATION_MAX_AGE_NS = 30 * 24 * 3_600 * 10**9


def _attestation_message(payload: dict[str, Any]) -> bytes:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return canonical


def verify_venue_attestation(
    attestation: dict[str, Any] | None,
    *,
    fills_sha256: str,
    account_id: str,
    key: bytes | str | None = None,
    now_ns: int | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Verify a trusted-ingestion receipt binds this file to this account.

    Returns ``(ok, reason, normalized)``. Missing/invalid receipts fail closed;
    no caller string, checksum, or filename can substitute for the signature.
    """
    if not isinstance(attestation, dict):
        return False, "ATTESTATION_MISSING", {}
    secret = key
    if secret is None:
        secret = os.environ.get(ATTESTATION_KEY_ENV, "").encode("utf-8") or None
    if not secret:
        return False, "ATTESTATION_KEY_UNAVAILABLE", {}
    key_bytes = secret.encode("utf-8") if isinstance(secret, str) else secret
    required = (
        "version",
        "venue",
        "account_id",
        "fills_sha256",
        "issued_ns",
        "key_id",
        "signature",
    )
    missing = [name for name in required if name not in attestation]
    if missing:
        return False, f"ATTESTATION_FIELDS_MISSING:{','.join(missing)}", {}
    if int(attestation.get("version", 0)) != ATTESTATION_VERSION:
        return False, "ATTESTATION_VERSION_UNSUPPORTED", {}
    if str(attestation.get("venue", "")).upper() != ATTESTATION_VENUE:
        return False, "ATTESTATION_VENUE_MISMATCH", {}
    if str(attestation.get("account_id", "")) != account_id:
        return False, "ATTESTATION_ACCOUNT_MISMATCH", {}
    if str(attestation.get("fills_sha256", "")) != fills_sha256:
        return False, "ATTESTATION_FILLS_HASH_MISMATCH", {}
    try:
        issued_ns = int(attestation["issued_ns"])
    except (TypeError, ValueError):
        return False, "ATTESTATION_ISSUED_NS_INVALID", {}
    current_ns = time.time_ns() if now_ns is None else int(now_ns)
    if issued_ns > current_ns:
        return False, "ATTESTATION_ISSUED_IN_FUTURE", {}
    if current_ns - issued_ns > ATTESTATION_MAX_AGE_NS:
        return False, "ATTESTATION_EXPIRED", {}
    payload = {
        "version": int(attestation["version"]),
        "venue": str(attestation["venue"]).upper(),
        "account_id": str(attestation["account_id"]),
        "fills_sha256": str(attestation["fills_sha256"]),
        "issued_ns": issued_ns,
        "key_id": str(attestation["key_id"]),
    }
    expected = hmac.new(
        key_bytes, _attestation_message(payload), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, str(attestation.get("signature", ""))):
        return False, "ATTESTATION_SIGNATURE_INVALID", {}
    return True, "ATTESTATION_VERIFIED", {**payload, "signature_verified": True}


def make_venue_attestation(
    *,
    fills_sha256: str,
    account_id: str,
    key: bytes | str,
    key_id: str = "test-authority",
    issued_ns: int | None = None,
) -> dict[str, Any]:
    """Create a receipt for tests / a trusted ingestion process (not the venue)."""
    payload = {
        "version": ATTESTATION_VERSION,
        "venue": ATTESTATION_VENUE,
        "account_id": account_id,
        "fills_sha256": fills_sha256,
        "issued_ns": time.time_ns() if issued_ns is None else int(issued_ns),
        "key_id": key_id,
    }
    key_bytes = key.encode("utf-8") if isinstance(key, str) else key
    payload["signature"] = hmac.new(
        key_bytes, _attestation_message(payload), hashlib.sha256
    ).hexdigest()
    return payload


def load_shadow_fills(
    path: Path | str,
    *,
    source: str = "live",
    provenance: str = PROVENANCE_PUBLIC_PAPER,
    account_id: str = "",
    attestation: dict[str, Any] | None = None,
    attestation_key: bytes | str | None = None,
    now_ns: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load and validate shadow fills file.

    Returns (records, meta). Meta contains mode, reason, sha256, validation details.
    Fixture source is never LIVE_VENUE_SETTLED.

    NX10.R4: a well-formed file is not a settlement. Mode depends on the claimed
    provenance: public paper (the default, and all this machine has) reads
    ``PUBLIC_PAPER_NOT_SETTLED`` with ``authority: NONE``; only an explicitly
    authenticated venue statement -- with an account identity -- may read
    ``LIVE_VENUE_SETTLED``. Column presence is never treated as proof.
    """
    if provenance not in (PROVENANCE_PUBLIC_PAPER, PROVENANCE_AUTHENTICATED):
        raise ValueError(f"unknown provenance {provenance!r}")
    if provenance == PROVENANCE_AUTHENTICATED and not account_id.strip():
        raise ValueError(
            "an authenticated venue statement requires an account identity; "
            "a file path alone is not provenance"
        )
    p = Path(path)
    if source == "fixture" or is_fixture_path(p):
        return [], {
            "mode": "FIXTURE_NOT_LIVE",
            "reason": f"fixture path not counted as live: {p}",
            "path": str(p),
            "fixture_guard": True,
        }
    if not p.exists() or p.stat().st_size == 0:
        return [], {
            "mode": "UNRUN_NO_VENUE_ACCOUNT",
            "reason": f"no venue account / no file at {p}",
            "expected_command": (
                "uv run --project v8-next python -m v8_next.adapters.shadow_ingest fetch "
                "--symbol BTCUSDT --out artifacts/shadow_fills.jsonl  # requires BINANCE_API_KEY"
            ),
            "path": str(p),
        }
    content = p.read_bytes()
    sha = hashlib.sha256(content).hexdigest()
    lines = [ln for ln in content.decode("utf-8").splitlines() if ln.strip()]
    if not lines:
        return [], {"mode": "UNRUN_NO_VENUE_ACCOUNT", "reason": "empty fills file", "path": str(p), "sha256": sha}
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    for i, ln in enumerate(lines):
        rec_errors: list[str] = []
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError as e:
            errors.append(f"line {i}: json error {e}")
            continue
        # Minimal schema check; presence of fill_id + instrument + price + qty
        if not isinstance(rec, dict):
            errors.append(f"line {i}: not an object")
            continue
        for k in ("fill_id", "instrument", "price", "qty"):
            if k not in rec:
                rec_errors.append(f"line {i}: missing {k}")
        # Type sanity (price/qty finite)
        try:
            price = float(rec.get("price", 0))
            qty = float(str(rec.get("qty", 0)))
            if not (price > 0 and qty > 0):
                rec_errors.append(f"line {i}: nonpositive price/qty")
        except (ValueError, TypeError):
            rec_errors.append(f"line {i}: bad price/qty type")
            continue
        if rec_errors:
            errors.extend(rec_errors)
        elif rec.get("fill_id") and rec.get("instrument"):
            records.append(rec)
    if errors and not records:
        return [], {"mode": "MALFORMED_SHADOW_FILE", "reason": "; ".join(errors[:3]), "path": str(p), "sha256": sha}
    # At least one well-formed record required for live
    valid = [r for r in records if all(k in r for k in ("fill_id", "instrument", "price", "qty"))]
    ids = [str(r.get("fill_id")) for r in valid]
    if len(ids) != len(set(ids)):
        return [], {
            "mode": "MALFORMED_SHADOW_FILE",
            "reason": "DUPLICATE_FILL_IDS",
            "path": str(p),
            "sha256": sha,
        }
    if not valid:
        return [], {"mode": "UNRUN_NO_VENUE_ACCOUNT", "reason": "no valid fill records", "path": str(p), "sha256": sha, "errors": errors}
    if provenance == PROVENANCE_AUTHENTICATED:
        # Full settlement schema is required before an authenticated receipt is
        # even considered. A partial diagnostic row is not a venue fill.
        settlement_fields = (
            "side",
            "venue_time_ns",
            "order_id",
        )
        missing = sorted(
            {
                field
                for record in valid
                for field in settlement_fields
                if field not in record
            }
        )
        if missing:
            return valid, {
                "mode": MODE_UNVERIFIED,
                "status": MODE_UNVERIFIED,
                "reason": f"SETTLEMENT_FIELDS_MISSING:{','.join(missing)}",
                "path": str(p),
                "sha256": sha,
                "provenance": provenance,
                "account_id": account_id,
                "authority": "NONE",
                "passed": False,
            }
        for record in valid:
            if str(record.get("side", "")).upper() not in ("BUY", "SELL"):
                return valid, {
                    "mode": MODE_UNVERIFIED,
                    "status": MODE_UNVERIFIED,
                    "reason": "SETTLEMENT_SIDE_INVALID",
                    "path": str(p),
                    "sha256": sha,
                    "passed": False,
                }
            try:
                venue_time_ns = int(record["venue_time_ns"])
            except (TypeError, ValueError):
                return valid, {
                    "mode": MODE_UNVERIFIED,
                    "status": MODE_UNVERIFIED,
                    "reason": "SETTLEMENT_VENUE_TIME_INVALID",
                    "path": str(p),
                    "sha256": sha,
                    "passed": False,
                }
            if venue_time_ns <= 0 or not str(record.get("order_id", "")).strip():
                return valid, {
                    "mode": MODE_UNVERIFIED,
                    "status": MODE_UNVERIFIED,
                    "reason": "SETTLEMENT_ORDER_ID_INVALID",
                    "path": str(p),
                    "sha256": sha,
                    "passed": False,
                }
        ok, reason, normalized = verify_venue_attestation(
            attestation,
            fills_sha256=sha,
            account_id=account_id,
            key=attestation_key,
            now_ns=now_ns,
        )
        if not ok:
            return valid, {
                "mode": MODE_UNVERIFIED,
                "status": MODE_UNVERIFIED,
                "reason": reason,
                "path": str(p),
                "sha256": sha,
                "provenance": provenance,
                "account_id": account_id,
                "authority": "NONE",
                "attestation": normalized,
                "passed": False,
            }
        mode = MODE_SETTLED
        authority = "VENUE_ATTESTATION_RECEIPT"
        return valid, {
            "mode": mode,
            "status": mode,
            "fills_count": len(valid),
            "path": str(p),
            "sha256": sha,
            "errors": errors[:5],
            "provenance": provenance,
            "account_id": account_id,
            "authority": authority,
            "attestation": normalized,
            "label": "AUTHENTICATED_VENUE_STATEMENT",
            "note": (
                "a trusted-ingestion receipt binds this file, venue and account; it is "
                "not a claim that the venue itself digitally signed the statement"
            ),
        }
    mode = MODE_PUBLIC_PAPER
    authority = "NONE"
    return valid, {
        "mode": mode,
        "status": mode,
        "fills_count": len(valid),
        "path": str(p),
        "sha256": sha,
        "errors": errors[:5],
        "provenance": provenance,
        "account_id": account_id or None,
        "authority": authority,
        "label": (
            "PUBLIC_PAPER: technical evidence of a capture pipeline, not a settlement"
            if provenance == PROVENANCE_PUBLIC_PAPER
            else "AUTHENTICATED_VENUE_STATEMENT"
        ),
        "note": (
            "a valid file shows the pipeline ran; it does not show that a venue "
            "settled anything (NX10.R4)"
        ),
    }


def ingest_status(
    path: Path | str | None = None,
    *,
    source: str = "live",
    provenance: str = PROVENANCE_PUBLIC_PAPER,
    account_id: str = "",
) -> dict[str, Any]:
    """Return ingest status dict without raising. Used by G8 and CLI."""
    p = Path(path) if path is not None else DEFAULT_SHADOW_FILLS
    if source == "fixture" or is_fixture_path(p):
        return {
            "mode": "FIXTURE_NOT_LIVE",
            "status": "FIXTURE_NOT_LIVE",
            "reason": "fixture not counted as live",
            "path": str(p),
            "fixture_guard": True,
            "format": "jsonl per docs/contracts/SHADOW_LIVE_DATA_SPEC.md",
            "source": "venue private REST GET /fapi/v1/userTrades (requires BINANCE_API_KEY)",
            "command": "uv run --project v8-next python -m v8_next.adapters.shadow_ingest fetch --symbol BTCUSDT --out artifacts/shadow_fills.jsonl",
        }
    if not p.exists() or p.stat().st_size == 0:
        return {
            "mode": "UNRUN_NO_VENUE_ACCOUNT",
            "status": "UNRUN_NO_VENUE_ACCOUNT",
            "reason": "no venue account / no file",
            "path": str(p),
            "format": "jsonl per docs/contracts/SHADOW_LIVE_DATA_SPEC.md",
            "source": "venue private REST GET /fapi/v1/userTrades (requires BINANCE_API_KEY)",
            "command": "uv run --project v8-next python -m v8_next.adapters.shadow_ingest fetch --symbol BTCUSDT --out artifacts/shadow_fills.jsonl",
            "funding_note": "public funding is in research/tape/quad-1h-12m/tape.jsonl (channel=funding); see funding_history.quad_funding_summary",
        }
    _, meta = load_shadow_fills(
        p, source=source, provenance=provenance, account_id=account_id
    )
    return {**meta, "status": meta.get("mode"), "format": "jsonl per docs/contracts/SHADOW_LIVE_DATA_SPEC.md",
            "authority_note": (
                "public paper is technical evidence; source=live or column presence is "
                "not an authority receipt (NX10.R4)"
            ),
            "source": "venue private REST GET /fapi/v1/userTrades", "command": "uv run --project v8-next python -m v8_next.adapters.shadow_ingest verify --fills artifacts/shadow_fills.jsonl"}


def reconcile_shadow_account(
    fills: list[dict[str, Any]],
    account: dict[str, Any] | None,
) -> dict[str, Any]:
    """Compare shadow fills against engine AccountState (economic_state).

    Returns comparison dict; raises only on programmatic misuse, otherwise returns
    a status field. No fixture is ever reconciled as PASS.
    """
    if account is None:
        return {
            "reconciled": False,
            "reason": "NO_ACCOUNT_SNAPSHOT",
            "mode": "UNRUN_NO_VENUE_ACCOUNT",
            "status": "UNRUN_NO_VENUE_ACCOUNT",
            "claim_status": "NO_ECONOMIC_CLAIM",
        }
    # Basic reconciliation: count and notional sanity vs orders/positions
    # AccountState has keys: balance_total, positions, orders
    required = {"balance_total", "positions", "orders"}
    if not required <= set(account.keys()):
        return {
            "reconciled": False,
            "reason": "ACCOUNT_INPUTS_UNAVAILABLE",
            "account_keys": sorted(account.keys()),
            "status": "ACCOUNT_INPUTS_UNAVAILABLE",
            "claim_status": "NO_ECONOMIC_CLAIM",
        }
    # Derive engine fill count: orders with FILLED
    engine_fills = [o for o in account.get("orders", []) if o.get("status") == "FILLED"]
    shadow_count = len(fills)
    engine_count = len(engine_fills)
    # NX10.R4: exactly-once means the counts must agree. "There is at least one
    # fill" is not a reconciliation, so a count mismatch is reported, not waved
    # through.
    problems: list[str] = []
    if shadow_count != engine_count:
        problems.append("FILL_COUNT_MISMATCH")
    # Signed quantity reconciliation against native positions, when the account
    # snapshot exposes them. Missing quantities are a refusal, never a zero.
    shadow_net: float | None = None
    engine_net: float | None = None
    if any("qty" in fill for fill in fills):
        try:
            shadow_net = 0.0
            for fill in fills:
                qty = float(str(fill["qty"]))
                side = str(fill.get("side", "")).upper()
                if side not in ("BUY", "SELL") or not math.isfinite(qty):
                    problems.append("FILL_SIDE_OR_QTY_INVALID")
                    shadow_net = None
                    break
                shadow_net += qty if side == "BUY" else -qty
        except (KeyError, TypeError, ValueError):
            problems.append("FILL_QTY_UNPARSABLE")
            shadow_net = None
    if account.get("venue") is not None and str(account.get("venue")).upper() != "BINANCE":
        problems.append("ACCOUNT_VENUE_MISMATCH")
    fill_instruments = {
        str(fill.get("instrument", "")).upper().replace("-PERP.BINANCE", "")
        for fill in fills
        if fill.get("instrument")
    }
    account_instruments = {
        str(position.get("instrument_id", "")).upper().replace("-PERP.BINANCE", "")
        for position in account.get("positions", [])
        if position.get("instrument_id")
    }
    if fill_instruments and account_instruments and fill_instruments != account_instruments:
        problems.append("INSTRUMENT_MISMATCH")
    positions = account.get("positions", [])
    if positions:
        engine_net = 0.0
        for position in positions:
            try:
                qty = abs(float(str(position.get("quantity", "nan")).split()[0]))
                side = str(position.get("side", "")).upper()
            except (TypeError, ValueError):
                problems.append("POSITION_QTY_UNPARSABLE")
                engine_net = None
                break
            if not math.isfinite(qty):
                problems.append("POSITION_QTY_UNPARSABLE")
                engine_net = None
                break
            if "LONG" in side or side == "BUY":
                engine_net += qty
            elif "SHORT" in side or side == "SELL":
                engine_net -= qty
            else:
                problems.append("POSITION_SIDE_INVALID")
                engine_net = None
                break
    if shadow_net is not None and engine_net is not None:
        if abs(shadow_net - engine_net) > 1e-9:
            problems.append("SIGNED_QUANTITY_MISMATCH")
    matches = not problems
    return {
        "reconciled": matches,
        "mode": "COUNT_MATCH" if matches else "RECONCILIATION_MISMATCH",
        "status": "RECONCILED_DIAGNOSTIC" if matches else "EXECUTION_UNPROVEN",
        "claim_status": "NO_ECONOMIC_CLAIM",
        "shadow_fills": shadow_count,
        "engine_filled_orders": engine_count,
        "delta": shadow_count - engine_count,
        "shadow_net_quantity": shadow_net,
        "engine_net_quantity": engine_net,
        "problems": problems,
        "account_currency": account.get("currency"),
        "note": "shadow vs simulated; venue settlement finality not certified here",
    }


def decompose_fill_shortfall(fills: list[dict[str, Any]]) -> dict[str, Any]:
    """Decompose only physically supplied fill marks; missing pieces stay absent.

    Records may carry ``model_price``, ``reference_mid``, ``side_sign`` and
    ``fee``.  The loader intentionally accepts older minimal capture rows for
    compatibility, but those rows cannot produce a fabricated zero shortfall:
    a component with no supplied mark on any row is published as ``None``, and
    the module stays MECHANICS ONLY (no gate, score or P&L takes this value).
    """
    if not fills:
        return {
            "status": "DATA_BLOCKED",
            "model_vs_fill": None,
            "adverse_selection": None,
            "fees": None,
            "claim_status": "NO_ECONOMIC_CLAIM",
        }
    model_drag = 0.0
    adverse = 0.0
    fees = 0.0
    model_available = True
    mid_available = True
    fee_available = True
    for row in fills:
        qty = float(row["qty"])
        price = float(row["price"])
        side = float(row.get("side_sign", 1.0))
        if side not in {-1.0, 1.0} or qty <= 0 or not all(
            math.isfinite(value) for value in (qty, price, side)
        ):
            raise ValueError("invalid fill shortfall inputs")
        if row.get("model_price") is None:
            model_available = False
        else:
            model_drag += (price - float(row["model_price"])) * qty * side
        if row.get("reference_mid") is None:
            mid_available = False
        else:
            adverse += (price - float(row["reference_mid"])) * qty * side
        if row.get("fee") is None:
            fee_available = False
        else:
            fees += float(row["fee"])
    return {
        "status": "NO_ECONOMIC_CLAIM",
        "model_vs_fill": model_drag if model_available else None,
        "adverse_selection": adverse if mid_available else None,
        "fees": fees if fee_available else None,
        "claim_status": "NO_ECONOMIC_CLAIM",
    }


def summarize_fill_latency(fills: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize latency only when submit and venue timestamps are captured.

    ``venue_time_ns`` is the settlement-side timestamp from the live-fill
    contract.  A separate ``submit_time_ns`` is required; elapsed wall time
    cannot be reconstructed from venue fills alone.
    """
    latencies_ms: list[float] = []
    for row in fills:
        submit_ns = row.get("submit_time_ns")
        venue_ns = row.get("venue_time_ns")
        if submit_ns is None or venue_ns is None:
            return {
                "status": "DATA_BLOCKED",
                "samples": len(latencies_ms),
                "latency_ms": None,
                "claim_status": "NO_ECONOMIC_CLAIM",
                "reason": "submit_time_ns and venue_time_ns are required",
            }
        try:
            submit = float(submit_ns)
            venue = float(venue_ns)
        except (TypeError, ValueError):
            return {
                "status": "MALFORMED_LATENCY_EVIDENCE",
                "samples": len(latencies_ms),
                "latency_ms": None,
                "claim_status": "NO_ECONOMIC_CLAIM",
                "reason": "latency timestamps must be numeric",
            }
        latency_ms = (venue - submit) / 1_000_000.0
        if not math.isfinite(latency_ms) or latency_ms < 0:
            return {
                "status": "MALFORMED_LATENCY_EVIDENCE",
                "samples": len(latencies_ms),
                "latency_ms": None,
                "claim_status": "NO_ECONOMIC_CLAIM",
                "reason": "venue timestamp precedes submit timestamp",
            }
        latencies_ms.append(latency_ms)
    if not latencies_ms:
        return {
            "status": "DATA_BLOCKED",
            "samples": 0,
            "latency_ms": None,
            "claim_status": "NO_ECONOMIC_CLAIM",
            "reason": "no venue fills",
        }
    ordered = sorted(latencies_ms)
    midpoint = len(ordered) // 2
    median = (
        ordered[midpoint]
        if len(ordered) % 2
        else (ordered[midpoint - 1] + ordered[midpoint]) / 2
    )
    return {
        "status": "OBSERVED_DIAGNOSTIC",
        "samples": len(ordered),
        "latency_ms": {
            "mean": sum(ordered) / len(ordered),
            "median": median,
            "min": ordered[0],
            "max": ordered[-1],
        },
        "claim_status": "NO_ECONOMIC_CLAIM",
    }


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Shadow ingest helper")
    sub = ap.add_subparsers(dest="cmd")
    p_fetch = sub.add_parser("fetch", help="fetch venue fills (requires BINANCE_API_KEY)")
    p_fetch.add_argument("--symbol", required=True)
    p_fetch.add_argument("--out", type=Path, default=DEFAULT_SHADOW_FILLS)
    p_fetch.add_argument("--start-ms", type=int, default=None)
    p_fetch.add_argument("--end-ms", type=int, default=None)

    p_verify = sub.add_parser("verify", help="verify local shadow file")
    p_verify.add_argument("--fills", type=Path, default=DEFAULT_SHADOW_FILLS)
    p_verify.add_argument("--check-account", type=Path, default=None)

    p_check = sub.add_parser("check", help="ingest status")
    p_check.add_argument("path", nargs="?", type=Path, default=DEFAULT_SHADOW_FILLS)

    args = ap.parse_args()
    if args.cmd == "fetch":
        import os

        if not os.environ.get("BINANCE_API_KEY"):
            print(json.dumps({"mode": "UNRUN_NO_VENUE_ACCOUNT", "reason": "BINANCE_API_KEY not set", "path": str(args.out)}, indent=2))
            raise SystemExit(2)
        # Real fetch would call private REST here; stub documents the requirement
        print(json.dumps({"error": "private fetch requires venue credentials; documented in docs/contracts/SHADOW_LIVE_DATA_SPEC.md"}, indent=2))
        raise SystemExit(2)
    elif args.cmd == "verify":
        fills, meta = load_shadow_fills(args.fills)
        print(json.dumps(meta, indent=2))
        if args.check_account and args.check_account.exists():
            acct = json.loads(args.check_account.read_text())
            print(json.dumps(reconcile_shadow_account(fills, acct), indent=2))
        raise SystemExit(0 if meta.get("mode") == "LIVE_VENUE_SETTLED" else 1)
    else:
        status = ingest_status(getattr(args, "path", DEFAULT_SHADOW_FILLS))
        print(json.dumps(status, indent=2))
        raise SystemExit(0)


if __name__ == "__main__":
    main()
