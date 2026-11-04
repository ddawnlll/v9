"""M0-04: restart-safe durable intent journal and outbox.

Every test exercises the journal, never a native order: the module has no submission
authority. The acceptance fixtures are crash-before-submit, crash-after-submit,
duplicate delivery, cancel/fill race, unknown venue status and overfill.
"""

from __future__ import annotations

import threading
from decimal import Decimal
from pathlib import Path

import pytest

from v8_next.adapters.durable_intent import (
    CLIENT_ORDER_ID_PREFIX,
    DuplicateSubmit,
    IntentRecord,
    IntentState,
    InvalidTransition,
    Outbox,
    OutboxError,
    OverfillError,
    UnknownIntent,
    VenueEvidenceError,
    VenueFillObservation,
    VenueOrderObservation,
    VenueView,
    derive_intent_identity,
)
from v8_next.domain.decision import VenueOrderStatus

NS = 1_700_000_000_000_000_000
INSTRUMENT = "BTCUSDT-PERP.BINANCE"
DECISION = "decision-0001"
POLICY = "policy-v1"
SIDE = "BUY"
QTY = Decimal("1.0")
PRICE = Decimal("50000")


def record_intent(outbox: Outbox, *, now_ns: int = NS, **overrides: object) -> IntentRecord:
    kwargs: dict[str, object] = {
        "decision_id": DECISION,
        "instrument_id": INSTRUMENT,
        "side": SIDE,
        "quantity": QTY,
        "price": PRICE,
        "policy_version": POLICY,
    }
    kwargs.update(overrides)
    return outbox.record_intent(now_ns=now_ns, **kwargs)  # type: ignore[arg-type]


def test_identity_is_deterministic_and_replay_stable() -> None:
    first = derive_intent_identity(
        decision_id=DECISION,
        instrument_id=INSTRUMENT,
        side=SIDE,
        quantity=QTY,
        price=PRICE,
        policy_version=POLICY,
    )
    second = derive_intent_identity(
        decision_id=DECISION,
        instrument_id=INSTRUMENT,
        side="buy",
        quantity=Decimal("1.00"),
        price=Decimal("50000.0"),
        policy_version=POLICY,
    )
    assert first == second
    assert len(first.client_order_id) == 36
    assert first.client_order_id.startswith(CLIENT_ORDER_ID_PREFIX)

    variants = [
        {"decision_id": "decision-0002"},
        {"instrument_id": "ETHUSDT-PERP.BINANCE"},
        {"side": "SELL"},
        {"quantity": Decimal("2")},
        {"price": Decimal("50001")},
        {"price": None},
        {"policy_version": "policy-v2"},
    ]
    ids = {first.intent_id}
    for variant in variants:
        params: dict[str, object] = {
            "decision_id": DECISION,
            "instrument_id": INSTRUMENT,
            "side": SIDE,
            "quantity": QTY,
            "price": PRICE,
            "policy_version": POLICY,
        }
        params.update(variant)
        ids.add(
            derive_intent_identity(**params).intent_id  # type: ignore[arg-type]
        )
    assert len(ids) == len(variants) + 1


def test_record_intent_is_idempotent(tmp_path: Path) -> None:
    with Outbox(tmp_path / "outbox.db") as outbox:
        first = record_intent(outbox)
        second = record_intent(outbox, now_ns=NS + 5)
        assert second == first
        assert first.state is IntentState.PENDING
        assert first.attempt == 0
        assert len(outbox.records()) == 1


def test_crash_before_submit_keeps_pending_and_resubmit_is_safe(tmp_path: Path) -> None:
    path = tmp_path / "outbox.db"
    with Outbox(path) as outbox:
        recorded = record_intent(outbox)

    with Outbox(path) as reopened:
        survived = reopened.get(recorded.intent_id)
        assert survived is not None
        assert survived.state is IntentState.PENDING
        assert survived.attempt == 0
        # A crash before the journal recorded a submission must not be mistaken for
        # an unobserved submission.
        assert reopened.recover(NS + 10) == ()

        started = reopened.begin_submit(recorded.intent_id, now_ns=NS + 20)
        assert started.state is IntentState.SUBMITTING
        assert started.attempt == 1
        assert started.client_order_id == recorded.client_order_id
        assert started.submitted_ns == NS + 20
        assert len(reopened.records()) == 1


def test_crash_after_submit_before_ack_is_unknown_and_resubmit_refused(tmp_path: Path) -> None:
    path = tmp_path / "outbox.db"
    with Outbox(path) as outbox:
        recorded = record_intent(outbox)
        outbox.begin_submit(recorded.intent_id, now_ns=NS + 1)
        # Crash: no ack was observed.

    with Outbox(path) as reopened:
        recovered = reopened.recover(NS + 2)
        assert len(recovered) == 1
        unknown = recovered[0]
        assert unknown.state is IntentState.UNKNOWN
        assert unknown.reconcile_note == "RECOVERED_SUBMISSION_UNOBSERVED"
        assert unknown.is_terminal is False

        with pytest.raises(DuplicateSubmit):
            reopened.begin_submit(recorded.intent_id, now_ns=NS + 3)
        unchanged = reopened.get(recorded.intent_id)
        assert unchanged is not None
        assert unchanged.state is IntentState.UNKNOWN
        assert unchanged.attempt == 1
        assert len(reopened.records()) == 1


def test_duplicate_ack_and_fill_delivery_make_single_transition(tmp_path: Path) -> None:
    with Outbox(tmp_path / "outbox.db") as outbox:
        recorded = record_intent(outbox)
        outbox.begin_submit(recorded.intent_id, now_ns=NS + 1)

        first_ack = outbox.observe_ack(recorded.intent_id, VenueOrderStatus.ACCEPTED, now_ns=NS + 2)
        duplicate_ack = outbox.observe_ack(
            recorded.intent_id, VenueOrderStatus.ACCEPTED, now_ns=NS + 3
        )
        assert first_ack.state is IntentState.ACKNOWLEDGED
        assert duplicate_ack == first_ack
        assert duplicate_ack.updated_ns == NS + 2

        first_fill = outbox.observe_fill(recorded.intent_id, Decimal("0.4"), now_ns=NS + 4)
        duplicate_fill = outbox.observe_fill(recorded.intent_id, Decimal("0.40"), now_ns=NS + 5)
        assert first_fill.state is IntentState.PARTIALLY_FILLED
        assert duplicate_fill == first_fill
        assert duplicate_fill.filled_qty == Decimal("0.4")
        assert outbox.cumulative_fills(recorded.intent_id) == (Decimal("0.4"),)
        assert duplicate_fill.attempt == 1


def test_cancel_fill_race_fill_wins_and_cancel_ack_never_regresses(tmp_path: Path) -> None:
    with Outbox(tmp_path / "outbox.db") as outbox:
        recorded = record_intent(outbox)
        outbox.begin_submit(recorded.intent_id, now_ns=NS + 1)
        outbox.observe_ack(recorded.intent_id, VenueOrderStatus.ACCEPTED, now_ns=NS + 2)

        pending = outbox.request_cancel(recorded.intent_id, now_ns=NS + 3)
        assert pending.state is IntentState.CANCEL_PENDING
        assert pending.is_terminal is False

        filled = outbox.observe_fill(recorded.intent_id, QTY, now_ns=NS + 4)
        assert filled.state is IntentState.FILLED
        assert filled.terminal_ns == NS + 4
        assert filled.filled_qty == QTY

        after_cancel_ack = outbox.observe_cancel(recorded.intent_id, now_ns=NS + 5)
        assert after_cancel_ack.state is IntentState.FILLED
        assert after_cancel_ack.terminal_ns == NS + 4
        assert outbox.request_cancel(recorded.intent_id, now_ns=NS + 6) == after_cancel_ack


def test_unknown_venue_status_is_never_terminal_and_recover_lists_it(tmp_path: Path) -> None:
    with Outbox(tmp_path / "outbox.db") as outbox:
        recorded = record_intent(outbox)
        outbox.begin_submit(recorded.intent_id, now_ns=NS + 1)
        unknown = outbox.observe_ack(recorded.intent_id, VenueOrderStatus.UNKNOWN, now_ns=NS + 2)
        assert unknown.state is IntentState.UNKNOWN
        assert unknown.venue_status is VenueOrderStatus.UNKNOWN
        assert unknown.is_terminal is False

        assert outbox.recover(NS + 3) == (unknown,)
        assert outbox.recover(NS + 4) == (unknown,)

        with pytest.raises(InvalidTransition):
            outbox.request_cancel(recorded.intent_id, now_ns=NS + 5)
        with pytest.raises(DuplicateSubmit):
            outbox.begin_submit(recorded.intent_id, now_ns=NS + 6)


def test_missing_venue_evidence_stays_unknown_forever(tmp_path: Path) -> None:
    with Outbox(tmp_path / "outbox.db") as outbox:
        recorded = record_intent(outbox)
        outbox.begin_submit(recorded.intent_id, now_ns=NS + 1)
        (unknown,) = outbox.recover(NS + 2)

        first = outbox.reconcile(unknown, VenueView(), now_ns=NS + 3)
        second = outbox.reconcile(unknown, VenueView(), now_ns=NS + 4)
        assert first == unknown
        assert second == unknown
        assert first.state is IntentState.UNKNOWN
        assert outbox.recover(NS + 5) == (unknown,)


def test_reconcile_acknowledges_from_open_order_evidence(tmp_path: Path) -> None:
    with Outbox(tmp_path / "outbox.db") as outbox:
        recorded = record_intent(outbox)
        outbox.begin_submit(recorded.intent_id, now_ns=NS + 1)
        (unknown,) = outbox.recover(NS + 2)

        view = VenueView(
            orders=(
                VenueOrderObservation(
                    client_order_id=recorded.client_order_id,
                    status=VenueOrderStatus.ACCEPTED,
                ),
            )
        )
        reconciled = outbox.reconcile(unknown, view, now_ns=NS + 3)
        assert reconciled.state is IntentState.ACKNOWLEDGED
        assert reconciled.venue_status is VenueOrderStatus.ACCEPTED
        assert reconciled.reconcile_note == "RECONCILED_ACKNOWLEDGED"
        assert reconciled.acknowledged_ns == NS + 3
        assert reconciled.is_terminal is False

        with pytest.raises(InvalidTransition):
            outbox.reconcile(unknown, view, now_ns=NS + 4)


def test_reconcile_from_fill_evidence_reaches_partial_then_filled(tmp_path: Path) -> None:
    with Outbox(tmp_path / "outbox.db") as outbox:
        partial_intent = record_intent(outbox)
        outbox.begin_submit(partial_intent.intent_id, now_ns=NS + 1)
        (unknown,) = outbox.recover(NS + 2)
        partial = outbox.reconcile(
            unknown,
            VenueView(
                orders=(
                    VenueOrderObservation(
                        client_order_id=partial_intent.client_order_id,
                        status=VenueOrderStatus.ACCEPTED,
                        cumulative_qty=Decimal("0.25"),
                    ),
                ),
                fills=(
                    VenueFillObservation(
                        client_order_id=partial_intent.client_order_id,
                        cumulative_qty=Decimal("0.25"),
                    ),
                ),
            ),
            now_ns=NS + 3,
        )
        assert partial.state is IntentState.PARTIALLY_FILLED
        assert partial.filled_qty == Decimal("0.25")
        assert outbox.cumulative_fills(partial_intent.intent_id) == (Decimal("0.25"),)

        full_intent = record_intent(outbox, decision_id="decision-0002")
        outbox.begin_submit(full_intent.intent_id, now_ns=NS + 4)
        (unknown_full,) = outbox.recover(NS + 5)
        full = outbox.reconcile(
            unknown_full,
            VenueView(
                fills=(
                    VenueFillObservation(
                        client_order_id=full_intent.client_order_id,
                        cumulative_qty=QTY,
                    ),
                )
            ),
            now_ns=NS + 6,
        )
        assert full.state is IntentState.FILLED
        assert full.filled_qty == QTY
        assert full.terminal_ns == NS + 6


def test_reconcile_overfill_is_typed_and_leaves_unknown(tmp_path: Path) -> None:
    with Outbox(tmp_path / "outbox.db") as outbox:
        recorded = record_intent(outbox)
        outbox.begin_submit(recorded.intent_id, now_ns=NS + 1)
        (unknown,) = outbox.recover(NS + 2)
        view = VenueView(
            fills=(
                VenueFillObservation(
                    client_order_id=recorded.client_order_id,
                    cumulative_qty=Decimal("1.5"),
                ),
            )
        )
        with pytest.raises(OverfillError):
            outbox.reconcile(unknown, view, now_ns=NS + 3)
        stored = outbox.get(recorded.intent_id)
        assert stored is not None
        assert stored.state is IntentState.UNKNOWN
        assert outbox.cumulative_fills(recorded.intent_id) == ()


def test_reconcile_rejects_contradictory_evidence(tmp_path: Path) -> None:
    with Outbox(tmp_path / "outbox.db") as outbox:
        recorded = record_intent(outbox)
        outbox.begin_submit(recorded.intent_id, now_ns=NS + 1)
        (unknown,) = outbox.recover(NS + 2)

        rejected_with_fill = VenueView(
            orders=(
                VenueOrderObservation(
                    client_order_id=recorded.client_order_id,
                    status=VenueOrderStatus.REJECTED,
                ),
            ),
            fills=(
                VenueFillObservation(
                    client_order_id=recorded.client_order_id,
                    cumulative_qty=Decimal("0.5"),
                ),
            ),
        )
        with pytest.raises(VenueEvidenceError):
            outbox.reconcile(unknown, rejected_with_fill, now_ns=NS + 3)

        filled_below_quantity = VenueView(
            orders=(
                VenueOrderObservation(
                    client_order_id=recorded.client_order_id,
                    status=VenueOrderStatus.FILLED,
                ),
            )
        )
        with pytest.raises(VenueEvidenceError):
            outbox.reconcile(unknown, filled_below_quantity, now_ns=NS + 4)

        ambiguous = VenueView(
            orders=(
                VenueOrderObservation(
                    client_order_id=recorded.client_order_id,
                    status=VenueOrderStatus.ACCEPTED,
                ),
                VenueOrderObservation(
                    client_order_id=recorded.client_order_id,
                    status=VenueOrderStatus.CANCELED,
                ),
            )
        )
        with pytest.raises(VenueEvidenceError):
            outbox.reconcile(unknown, ambiguous, now_ns=NS + 5)

        stored = outbox.get(recorded.intent_id)
        assert stored is not None
        assert stored.state is IntentState.UNKNOWN


def test_reconcile_canceled_without_fills_is_terminal(tmp_path: Path) -> None:
    with Outbox(tmp_path / "outbox.db") as outbox:
        recorded = record_intent(outbox)
        outbox.begin_submit(recorded.intent_id, now_ns=NS + 1)
        (unknown,) = outbox.recover(NS + 2)
        reconciled = outbox.reconcile(
            unknown,
            VenueView(
                orders=(
                    VenueOrderObservation(
                        client_order_id=recorded.client_order_id,
                        status=VenueOrderStatus.CANCELED,
                    ),
                )
            ),
            now_ns=NS + 3,
        )
        assert reconciled.state is IntentState.CANCELED
        assert reconciled.is_terminal is True
        assert reconciled.filled_qty == 0


def test_overfill_is_typed_and_never_clamped(tmp_path: Path) -> None:
    with Outbox(tmp_path / "outbox.db") as outbox:
        recorded = record_intent(outbox)
        outbox.begin_submit(recorded.intent_id, now_ns=NS + 1)
        outbox.observe_ack(recorded.intent_id, VenueOrderStatus.ACCEPTED, now_ns=NS + 2)

        with pytest.raises(OverfillError):
            outbox.observe_fill(recorded.intent_id, Decimal("1.0000001"), now_ns=NS + 3)

        stored = outbox.get(recorded.intent_id)
        assert stored is not None
        assert stored.state is IntentState.ACKNOWLEDGED
        assert stored.filled_qty == 0
        assert outbox.cumulative_fills(recorded.intent_id) == ()


def test_unlisted_venue_status_is_refused_not_inferred(tmp_path: Path) -> None:
    with Outbox(tmp_path / "outbox.db") as outbox:
        recorded = record_intent(outbox)
        outbox.begin_submit(recorded.intent_id, now_ns=NS + 1)

        with pytest.raises(VenueEvidenceError):
            outbox.observe_ack(recorded.intent_id, "PROBABLY_FILLED", now_ns=NS + 2)
        with pytest.raises(VenueEvidenceError):
            VenueOrderObservation(
                client_order_id=recorded.client_order_id,
                status="PROBABLY_FILLED",  # type: ignore[arg-type]
            )

        stored = outbox.get(recorded.intent_id)
        assert stored is not None
        assert stored.state is IntentState.SUBMITTING


def test_unknown_status_never_rewrites_terminal_state(tmp_path: Path) -> None:
    with Outbox(tmp_path / "outbox.db") as outbox:
        recorded = record_intent(outbox)
        outbox.begin_submit(recorded.intent_id, now_ns=NS + 1)
        outbox.observe_ack(recorded.intent_id, VenueOrderStatus.ACCEPTED, now_ns=NS + 2)
        filled = outbox.observe_fill(recorded.intent_id, QTY, now_ns=NS + 3)

        assert outbox.observe_ack(recorded.intent_id, VenueOrderStatus.UNKNOWN, now_ns=NS + 4) == (
            filled
        )
        assert outbox.recover(NS + 5) == ()


def test_pending_intent_refuses_late_evidence(tmp_path: Path) -> None:
    with Outbox(tmp_path / "outbox.db") as outbox:
        recorded = record_intent(outbox)
        with pytest.raises(InvalidTransition):
            outbox.observe_ack(recorded.intent_id, VenueOrderStatus.ACCEPTED, now_ns=NS + 1)
        with pytest.raises(InvalidTransition):
            outbox.observe_fill(recorded.intent_id, Decimal("0.1"), now_ns=NS + 2)
        with pytest.raises(InvalidTransition):
            outbox.request_cancel(recorded.intent_id, now_ns=NS + 3)
        with pytest.raises(InvalidTransition):
            outbox.observe_cancel(recorded.intent_id, now_ns=NS + 4)
        stored = outbox.get(recorded.intent_id)
        assert stored is not None
        assert stored.state is IntentState.PENDING


def test_stale_lower_fill_reading_never_regresses(tmp_path: Path) -> None:
    with Outbox(tmp_path / "outbox.db") as outbox:
        recorded = record_intent(outbox)
        outbox.begin_submit(recorded.intent_id, now_ns=NS + 1)
        outbox.observe_ack(recorded.intent_id, VenueOrderStatus.ACCEPTED, now_ns=NS + 2)
        first = outbox.observe_fill(recorded.intent_id, Decimal("0.6"), now_ns=NS + 3)
        stale = outbox.observe_fill(recorded.intent_id, Decimal("0.4"), now_ns=NS + 4)
        assert stale == first
        assert stale.filled_qty == Decimal("0.6")
        assert outbox.cumulative_fills(recorded.intent_id) == (Decimal("0.6"),)

        filled = outbox.observe_fill(recorded.intent_id, QTY, now_ns=NS + 5)
        assert filled.state is IntentState.FILLED
        assert filled.terminal_ns == NS + 5


def test_recover_marks_submitted_and_cancel_pending_unknown(tmp_path: Path) -> None:
    with Outbox(tmp_path / "outbox.db") as outbox:
        submitted_intent = record_intent(outbox)
        outbox.begin_submit(submitted_intent.intent_id, now_ns=NS + 1)
        outbox.observe_ack(submitted_intent.intent_id, VenueOrderStatus.SUBMITTED, now_ns=NS + 2)

        cancel_intent = record_intent(outbox, decision_id="decision-0002")
        outbox.begin_submit(cancel_intent.intent_id, now_ns=NS + 3)
        outbox.observe_ack(cancel_intent.intent_id, VenueOrderStatus.ACCEPTED, now_ns=NS + 4)
        outbox.request_cancel(cancel_intent.intent_id, now_ns=NS + 5)

        recovered = outbox.recover(NS + 6)
        by_id = {record.intent_id: record for record in recovered}
        assert by_id[submitted_intent.intent_id].state is IntentState.UNKNOWN
        assert by_id[submitted_intent.intent_id].reconcile_note == "RECOVERED_ACK_UNOBSERVED"
        assert by_id[cancel_intent.intent_id].state is IntentState.UNKNOWN
        assert by_id[cancel_intent.intent_id].reconcile_note == "RECOVERED_CANCEL_UNOBSERVED"


def test_two_handles_cannot_own_the_same_transition(tmp_path: Path) -> None:
    path = tmp_path / "outbox.db"
    with Outbox(path) as first, Outbox(path) as second:
        recorded = record_intent(first)

        started = second.begin_submit(recorded.intent_id, now_ns=NS + 1)
        assert started.state is IntentState.SUBMITTING

        with pytest.raises(DuplicateSubmit):
            first.begin_submit(recorded.intent_id, now_ns=NS + 2)
        stored = first.get(recorded.intent_id)
        assert stored is not None
        assert stored.attempt == 1

        second.observe_ack(recorded.intent_id, VenueOrderStatus.ACCEPTED, now_ns=NS + 3)
        partial = first.observe_fill(recorded.intent_id, Decimal("0.5"), now_ns=NS + 4)
        assert partial.state is IntentState.PARTIALLY_FILLED

        with pytest.raises(OverfillError):
            second.observe_fill(recorded.intent_id, Decimal("1.5"), now_ns=NS + 5)


def test_concurrent_handles_have_exactly_one_submit_winner(tmp_path: Path) -> None:
    path = tmp_path / "outbox.db"
    with Outbox(path) as setup:
        recorded = record_intent(setup)

    barrier = threading.Barrier(2)
    wins: list[IntentRecord] = []
    refusals: list[DuplicateSubmit] = []
    lock = threading.Lock()

    def attempt() -> None:
        with Outbox(path) as outbox:
            barrier.wait()
            try:
                result = outbox.begin_submit(recorded.intent_id, now_ns=NS + 1)
            except DuplicateSubmit as exc:
                with lock:
                    refusals.append(exc)
            else:
                with lock:
                    wins.append(result)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(wins) == 1
    assert len(refusals) == 1
    with Outbox(path) as reopened:
        stored = reopened.get(recorded.intent_id)
        assert stored is not None
        assert stored.state is IntentState.SUBMITTING
        assert stored.attempt == 1


def test_restart_preserves_fill_evidence_and_identity(tmp_path: Path) -> None:
    path = tmp_path / "outbox.db"
    with Outbox(path) as outbox:
        recorded = record_intent(outbox)
        outbox.begin_submit(recorded.intent_id, now_ns=NS + 1)
        outbox.observe_ack(recorded.intent_id, VenueOrderStatus.ACCEPTED, now_ns=NS + 2)
        outbox.observe_fill(recorded.intent_id, Decimal("0.5"), now_ns=NS + 3)

    with Outbox(path) as reopened:
        survived = reopened.get(recorded.intent_id)
        assert survived is not None
        assert survived.state is IntentState.PARTIALLY_FILLED
        assert survived.filled_qty == Decimal("0.5")
        assert survived.client_order_id == recorded.client_order_id
        assert reopened.cumulative_fills(recorded.intent_id) == (Decimal("0.5"),)
        # A known live order keeps its evidence on restart; it is not relabelled UNKNOWN.
        assert all(record.intent_id != recorded.intent_id for record in reopened.recover(NS + 5))


def test_unknown_intent_is_never_guessed(tmp_path: Path) -> None:
    with Outbox(tmp_path / "outbox.db") as outbox:
        assert outbox.get("missing") is None
        with pytest.raises(UnknownIntent):
            outbox.begin_submit("missing", now_ns=NS)
        with pytest.raises(OutboxError):
            outbox.observe_fill("missing", Decimal("0.1"), now_ns=NS)
