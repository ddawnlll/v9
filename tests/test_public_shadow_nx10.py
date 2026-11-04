"""NX10 (#431) — bounded public capture, exactly-once restart, honest provenance.

Evidence classes:

* **mechanics** (synthetic events, MECHANICS ONLY) — duplicate identities are dropped
  and counted, a gap beyond the declared maximum fails closed until it is
  acknowledged, out-of-order events are rejected rather than reordered, the batch
  identity is a deterministic chain, and resume rebuilds the state from disk.
* **artifact read-back** — the delivered capture: the forward window is in the future
  relative to its freeze clock, the rolling chain verifies, the maturity report keeps
  prospectivity PENDING, and public paper never reads as a settlement.

The tests never require a settlement, a maturity verdict, or a G7 pass.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from v8_next.adapters.shadow_ingest import (
    MODE_PUBLIC_PAPER,
    MODE_SETTLED,
    MODE_UNVERIFIED,
    PROVENANCE_AUTHENTICATED,
    load_shadow_fills,
    make_venue_attestation,
    reconcile_shadow_account,
)
from v8_next.evaluation.prospective_capture import (
    PUBLIC_PAPER_SOURCE,
    CaptureState,
    TradeEvent,
    batch_identity,
    load_capture,
    merge_batch,
    write_batch,
    write_manifest,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
NX10_DIR = REPO_ROOT / "docs" / "evidence" / "v87" / "NX10"
HOUR_NS = 3_600 * 10**9


def _event(trade_id: str, event_ns: int, arrival_ns: int = 0) -> TradeEvent:
    return TradeEvent(
        source=PUBLIC_PAPER_SOURCE,
        symbol="BTCUSDT",
        trade_id=trade_id,
        event_ns=event_ns,
        arrival_ns=arrival_ns or event_ns,
        price="100",
        qty="1",
    )


def _load(name: str) -> dict:
    path = NX10_DIR / name
    if not path.is_file():
        pytest.skip(f"NX10 evidence absent at {path}; run tools/nx10_public_shadow.py first")
    return json.loads(path.read_text())


# --------------------------------------------------------------------------- #
# mechanics — synthetic events
# --------------------------------------------------------------------------- #


def test_duplicate_identities_are_dropped_and_counted() -> None:
    state = CaptureState()
    first = [_event("1", 1_000), _event("2", 2_000)]
    state, report = merge_batch(state, first, max_gap_ns=HOUR_NS)
    assert report["accepted"] == 2 and report["duplicates_dropped"] == 0
    # the same poll again: every identity is re-seen, so nothing is double-counted
    state, report = merge_batch(state, first, max_gap_ns=HOUR_NS)
    assert report["accepted"] == 0 and report["duplicates_dropped"] == 2
    assert state.accepted == 2 and state.duplicates == 2
    # a partial overlap counts only the new identities
    state, report = merge_batch(state, [_event("2", 2_000), _event("3", 3_000)], max_gap_ns=HOUR_NS)
    assert report["accepted"] == 1 and report["duplicates_dropped"] == 1
    assert state.accepted == 3


def test_a_gap_fails_closed_until_it_is_acknowledged() -> None:
    state = CaptureState()
    state, _ = merge_batch(state, [_event("1", 1_000)], max_gap_ns=HOUR_NS)
    hole = [_event("9", 1_000 + 10 * HOUR_NS)]
    state, report = merge_batch(state, hole, max_gap_ns=HOUR_NS)
    assert report["recovery_required"] is True
    assert report["gap"]["kind"] == "GAP_DETECTED"
    assert "MISSING_INTERVAL_NOT_BRIDGED" in report["reason"]
    # nothing was merged while the hole was unacknowledged
    assert state.accepted == 1 and state.last_event_ns == 1_000
    assert state.gaps == 1
    # the identity is not burned: an acknowledged retry merges it once
    state, report = merge_batch(state, hole, max_gap_ns=HOUR_NS, acknowledge_gap=True)
    assert report["recovery_required"] is False
    assert state.accepted == 2
    assert state.gap_acknowledgements and state.gap_acknowledgements[0]["acknowledged"] is True


def test_out_of_order_events_are_rejected_not_reordered() -> None:
    state = CaptureState()
    state, _ = merge_batch(state, [_event("5", 5_000)], max_gap_ns=HOUR_NS)
    state, report = merge_batch(state, [_event("4", 4_000)], max_gap_ns=HOUR_NS)
    assert report["out_of_order_rejected"] == 1
    assert report["accepted"] == 0
    assert state.last_event_ns == 5_000


def test_batch_identity_is_a_deterministic_chain() -> None:
    events = [_event("1", 1_000), _event("2", 2_000)]
    first = batch_identity(events, "GENESIS")
    assert first == batch_identity(events, "GENESIS")
    second = batch_identity(events, first["chain_hash"])
    assert second["chain_hash"] != first["chain_hash"]
    assert second["prev_hash"] == first["chain_hash"]
    assert first["first_arrival_ns"] == 1_000 and first["last_arrival_ns"] == 2_000
    with pytest.raises(ValueError):
        merge_batch(CaptureState(), events, max_gap_ns=0)


def test_resume_rebuilds_the_state_from_disk(tmp_path: Path) -> None:
    out = tmp_path / "capture"
    state = CaptureState()
    events = [_event("1", 1_000), _event("2", 2_000)]
    state, report = merge_batch(state, events, max_gap_ns=HOUR_NS)
    write_batch(out, 1, events, report)
    write_manifest(out, {"batches": {"batch_0001": report["batch_identity"]}, "chain_hash": state.prev_hash})

    manifest, resumed, resume_report = load_capture(out)
    assert resume_report["chain_verified"] is True
    assert resumed.accepted == 2
    assert resumed.last_event_ns == 2_000
    # the resumed run refuses to double-count what it just replayed
    resumed, second = merge_batch(resumed, events, max_gap_ns=HOUR_NS)
    assert second["duplicates_dropped"] == 2

    # a tampered batch is caught by the chain check rather than trusted
    tampered = out / "batch_0001.jsonl"
    body = tampered.read_text().replace('"qty":"1"', '"qty":"2"')
    tampered.write_text(body)
    _, _, broken = load_capture(out)
    assert broken["chain_verified"] is False
    assert any("payload hash mismatch" in problem for problem in broken["problems"])


# --------------------------------------------------------------------------- #
# R4 — provenance: public paper is not a settlement
# --------------------------------------------------------------------------- #


def test_public_paper_never_reads_as_a_settlement(tmp_path: Path, monkeypatch) -> None:
    fills = tmp_path / "public.jsonl"
    fills.write_text(json.dumps({"fill_id": "P-1", "instrument": "BTCUSDT", "price": 100, "qty": 1}) + "\n")
    records, meta = load_shadow_fills(fills)
    assert records and meta["mode"] == MODE_PUBLIC_PAPER
    assert meta["authority"] == "NONE"
    assert meta["label"].startswith("PUBLIC_PAPER")
    with pytest.raises(ValueError, match="account identity"):
        load_shadow_fills(fills, provenance=PROVENANCE_AUTHENTICATED)
    # A caller label plus an account id is not authenticity: no attestation is
    # supplied, so settlement is refused by name.
    _, unverified = load_shadow_fills(
        fills, provenance=PROVENANCE_AUTHENTICATED, account_id="acct-1"
    )
    assert unverified["mode"] == MODE_UNVERIFIED
    assert unverified["reason"] == "SETTLEMENT_FIELDS_MISSING:order_id,side,venue_time_ns"

    monkeypatch.setenv("V8_VENUE_ATTESTATION_KEY", "test-key")
    settled = tmp_path / "settled.jsonl"
    settled.write_text(
        json.dumps(
            {
                "fill_id": "P-1",
                "instrument": "BTCUSDT",
                "price": 100,
                "qty": 1,
                "side": "BUY",
                "venue_time_ns": 1,
                "order_id": "O-1",
            }
        )
        + "\n"
    )
    sha = hashlib.sha256(settled.read_bytes()).hexdigest()
    attestation = make_venue_attestation(fills_sha256=sha, account_id="acct-1", key="test-key")
    _, authenticated = load_shadow_fills(
        settled,
        provenance=PROVENANCE_AUTHENTICATED,
        account_id="acct-1",
        attestation=attestation,
    )
    assert authenticated["mode"] == MODE_SETTLED
    assert authenticated["authority"] == "VENUE_ATTESTATION_RECEIPT"


def test_reconciliation_is_exactly_once_not_has_any_fills() -> None:
    fills = [{"fill_id": "P-1"}, {"fill_id": "P-2"}]
    account = {"balance_total": "100", "positions": [], "orders": [{"status": "FILLED"}]}
    result = reconcile_shadow_account(fills, account)
    assert result["reconciled"] is False
    assert result["mode"] == "RECONCILIATION_MISMATCH"
    assert result["delta"] == 1
    matched = reconcile_shadow_account(
        fills[:1], {"balance_total": "100", "positions": [], "orders": [{"status": "FILLED"}]}
    )
    assert matched["reconciled"] is True and matched["mode"] == "COUNT_MATCH"


# --------------------------------------------------------------------------- #
# artifact read-back — the delivered capture
# --------------------------------------------------------------------------- #


def test_the_frozen_window_is_prospective_and_was_not_backdated() -> None:
    plan = _load("forward_plan.json")
    assert plan["window_is_in_the_future"] is True
    assert plan["start_ns"] > plan["freeze_clock_ns"]
    assert plan["start_ns"] % HOUR_NS == 0 and plan["end_ns"] % HOUR_NS == 0
    assert plan["historical_replay_labelled_prospective"] is False
    assert plan["prospective_label"] == "PROSPECTIVE_WINDOW_NOT_YET_OBSERVED"
    assert plan["code_and_lock_hash"]
    assert len(plan["policies"]) >= 2 and plan["baseline"] in plan["policies"]


def test_capture_manifest_chain_and_maturity_report() -> None:
    maturity = _load("maturity.json")
    capture_dir = NX10_DIR / "capture"
    manifest = json.loads((capture_dir / "manifest.json").read_text())
    assert manifest["label"].startswith("PUBLIC_PAPER_CAPTURE")
    assert manifest["authority"] == "NONE"
    assert manifest["batches"], "a capture with no batch is not evidence"

    # every recorded batch is on disk and its payload hash matches the manifest
    for name, identity in manifest["batches"].items():
        batch = capture_dir / f"{name}.jsonl"
        assert batch.is_file(), name
        import hashlib

        payload = [
            json.loads(line) for line in batch.read_text().splitlines() if line.strip()
        ]
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        assert digest == identity["payload_sha256"], name

    assert maturity["prospective_maturity"] == "PENDING"
    assert maturity["settlement"]["mode"] == "PUBLIC_PAPER_NOT_SETTLED"
    assert maturity["settlement"]["authority"] == "NONE"
    assert maturity["economic_claim"] == "NONE"
    assert maturity["g7_state"] == "UNKNOWN"
    assert maturity["technical_acceptance"] == "BOUNDED_CAPTURE_AND_RESTART_EVIDENCE"
    assert maturity["arrival_and_event_clocks_recorded"] is True
    assert maturity["identity_binding"]["per_batch_chain"] is True
    assert maturity["restart"]["resume"]["chain_verified"] is True
    assert maturity["restart"]["resume"]["events_replayed"] == maturity["exactly_once"]["accepted"]
