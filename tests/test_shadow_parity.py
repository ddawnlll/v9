"""M2-06: V8 policy-off parity and V9 shadow invariance (differential harness).

The fixture below is the whole economic event set: one campaign, one opportunity,
two native orders, two protective orders, two fills, two cashflows, one funding
row, thirty bars and four decision cuts. Nothing is downloaded and no engine is
started -- :func:`v8_next.evaluation.shadow_parity.capture_replay` hashes the
fixture and (in shadow mode only) advances belief at the cuts.

The pinned reference is a *golden*: it is the policy-off digest set of this exact
fixture, committed as canonical JSON. A later wave that changes a digest payload,
a canonicalisation rule or the fixture fails here before it can move a baseline.
"""

from __future__ import annotations

import ast
import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from v8_next.belief import BeliefStatus, MarketBeliefV1, UncertaintyState
from v8_next.domain.decision import (
    DecisionAction,
    OpportunityLifecycle,
    ReasonCode,
    VenueOrderStatus,
)
from v8_next.domain.observation import QualityState
from v8_next.economics.legality import LegalActionTable, PositionPresence
from v8_next.economics.policy import CapacityState, TraderDecisionPolicy
from v8_next.evaluation import shadow_parity as sp
from v8_next.evaluation.resource_baseline import GateStatus
from v8_next.telemetry.decisions import DecisionLedger, DecisionReceipt, ReceiptInputs
from v8_next.telemetry.identity import canonical_digest

NS = 1_000_000_000
M15 = 15 * 60 * NS
IID = "BTCUSDT-PERP.BINANCE"
CAMPAIGN = "camp-0001"
OPPORTUNITY = "opp-0001"
BASE = 1_700_000_000 * NS

#: The golden policy-off reference. Regenerate only with a documented reason.
PINNED_REFERENCE_JSON = (
    '{"decision_ids":[],"decisions":null,'
    '"fills":"b35da0cb6530ddaf77b54abd2dd00200e79886be78b68bc77befce5e0a4dc028",'
    '"funding":"078f7463d5409a80528e13d28ce0a7e20f7f8baaf7206a35dd2761a652d8768d",'
    '"lineage":"711a2afd544c1357735c1150c87655b7fa458dba05ed28f36ae1b91b9f87c630",'
    '"opportunity":"6bffc2f39f2de75dfefdc185383e670273c0aed1ac30bf6f7e226978da8a934a",'
    '"orders":"5de2cbde77f69690543b3327f5f68cc82f476c3f725bf575744bd457b9c59abe",'
    '"policy_state":"OFF",'
    '"protection":"fa08357cd296a05a5958755d8d48663fa111a5625324eb2f850dedcda7f02ef4"}'
)


# --------------------------------------------------------------------------- #
# fixture
# --------------------------------------------------------------------------- #
def _bar(index: int) -> sp.BarEvent:
    close = Decimal(100 + index)
    return sp.BarEvent(
        instrument_id=IID,
        start_ns=BASE + index * M15,
        end_ns=BASE + (index + 1) * M15,
        open=close - Decimal("0.5"),
        high=close + Decimal("0.25"),
        low=close - Decimal("0.75"),
        close=close,
        volume=Decimal("10"),
        sequence=index,
    )


def _cut(
    bars_closed: int,
    *,
    venue: VenueOrderStatus,
    presence: PositionPresence = PositionPresence.NONE,
    quantity: str = "0",
    exposure: str = "0",
) -> sp.DecisionCutEvent:
    return sp.DecisionCutEvent(
        decision_time_ns=BASE + bars_closed * M15,
        instrument_id=IID,
        venue_status=venue,
        position_presence=presence,
        equity=Decimal("100000"),
        capacity=CapacityState.AVAILABLE,
        position_quantity=Decimal(quantity),
        position_exposure=Decimal(exposure),
        opportunity_id=OPPORTUNITY,
    )


def _script() -> sp.ReplayScript:
    return sp.ReplayScript(
        instrument_id=IID,
        as_of_ns=BASE + 31 * M15,
        bars=tuple(_bar(index) for index in range(30)),
        opportunities=(
            sp.OpportunityEvent(
                opportunity_id=OPPORTUNITY,
                campaign_id=CAMPAIGN,
                instrument_id=IID,
                direction="LONG",
                lifecycle=OpportunityLifecycle.ARMED,
                selected_ns=BASE + 5 * M15,
                available_at_ns=BASE + 10 * M15,
            ),
        ),
        native_decisions=(
            sp.NativeDecisionEvent(
                decision_id="dec-0001",
                opportunity_id=OPPORTUNITY,
                campaign_id=CAMPAIGN,
                instrument_id=IID,
                decision_time_ns=BASE + 20 * M15,
                available_at_ns=BASE + 20 * M15,
            ),
        ),
        orders=(
            sp.NativeOrderEvent(
                client_order_id="O-1",
                decision_id="dec-0001",
                campaign_id=CAMPAIGN,
                instrument_id=IID,
                side="BUY",
                order_type="LIMIT",
                quantity=Decimal("1"),
                price=Decimal("119"),
                submitted_ns=BASE + 20 * M15,
                available_at_ns=BASE + 20 * M15,
            ),
            sp.NativeOrderEvent(
                client_order_id="O-2",
                decision_id="dec-0001",
                campaign_id=CAMPAIGN,
                instrument_id=IID,
                side="SELL",
                order_type="LIMIT",
                quantity=Decimal("1"),
                price=Decimal("124"),
                submitted_ns=BASE + 26 * M15,
                available_at_ns=BASE + 26 * M15,
            ),
        ),
        protection=(
            sp.ProtectionOrderEvent(
                client_order_id="P-1",
                parent_client_order_id="O-1",
                kind="STOP_LOSS",
                side="SELL",
                trigger_price=Decimal("118"),
                quantity=Decimal("1"),
                submitted_ns=BASE + 20 * M15,
                available_at_ns=BASE + 20 * M15,
            ),
            sp.ProtectionOrderEvent(
                client_order_id="P-2",
                parent_client_order_id="O-1",
                kind="TAKE_PROFIT",
                side="SELL",
                trigger_price=Decimal("124"),
                quantity=Decimal("1"),
                submitted_ns=BASE + 20 * M15,
                available_at_ns=BASE + 20 * M15,
            ),
        ),
        fills=(
            sp.FillEvent(
                fill_id="F-1",
                client_order_id="O-1",
                decision_id="dec-0001",
                campaign_id=CAMPAIGN,
                instrument_id=IID,
                quantity=Decimal("1"),
                price=Decimal("119"),
                fee_amount=Decimal("0.0595"),
                fee_currency="USDT",
                event_time_ns=BASE + 20 * M15,
                available_at_ns=BASE + 20 * M15,
            ),
            sp.FillEvent(
                fill_id="F-2",
                client_order_id="O-2",
                decision_id="dec-0001",
                campaign_id=CAMPAIGN,
                instrument_id=IID,
                quantity=Decimal("1"),
                price=Decimal("124"),
                fee_amount=Decimal("0.062"),
                fee_currency="USDT",
                event_time_ns=BASE + 26 * M15,
                available_at_ns=BASE + 26 * M15,
            ),
        ),
        cashflows=(
            sp.CashflowEvent(
                cashflow_id="C-1",
                fill_id="F-1",
                campaign_id=CAMPAIGN,
                amount=Decimal("-0.0595"),
                event_time_ns=BASE + 20 * M15,
                available_at_ns=BASE + 20 * M15,
            ),
            sp.CashflowEvent(
                cashflow_id="C-2",
                fill_id="F-2",
                campaign_id=CAMPAIGN,
                amount=Decimal("4.938"),
                event_time_ns=BASE + 26 * M15,
                available_at_ns=BASE + 26 * M15,
            ),
        ),
        funding=(
            sp.FundingRowEvent(
                funding_id="FU-1",
                instrument_id=IID,
                amount=Decimal("-0.12"),
                currency="USDT",
                rate=Decimal("-0.0001"),
                event_time_ns=BASE + 24 * M15,
                available_at_ns=BASE + 24 * M15,
            ),
        ),
        cuts=(
            # Before the opportunity is available (released at bar 10): PIT refusal.
            _cut(6, venue=VenueOrderStatus.CANCELED),
            _cut(20, venue=VenueOrderStatus.CANCELED),
            _cut(
                25,
                venue=VenueOrderStatus.FILLED,
                presence=PositionPresence.OPEN,
                quantity="1",
                exposure="10000",
            ),
            _cut(30, venue=VenueOrderStatus.UNKNOWN),
        ),
    )


SCRIPT = _script()


class RecordingNativeSession:
    """A native sink the shadow pass can actually reach.

    Its digest is a content digest of every ``submit``/``cancel`` it has observed,
    so an injected adaptive step that touches the sink changes the digest and the
    zero-mutation gate can observe it. This is the double that makes
    :class:`sp.ShadowMutationError` reachable; a fixture digest alone never could.
    """

    def __init__(self, label: str = "paper-session-1") -> None:
        self._label = label
        self._calls: list[tuple[str, str]] = []
        self.digest_calls = 0

    @property
    def calls(self) -> tuple[tuple[str, str], ...]:
        return tuple(self._calls)

    def digest(self) -> str:
        self.digest_calls += 1
        return canonical_digest(
            "test.native-session.v1",
            {"label": self._label, "calls": [list(call) for call in self._calls]},
        )

    def submit(self, intent: object) -> None:
        self._calls.append(("submit", str(intent)))

    def cancel(self, client_order_id: str) -> None:
        self._calls.append(("cancel", client_order_id))


def _capture(
    state: sp.PolicyState,
    *,
    session: RecordingNativeSession | None = None,
) -> sp.ReplaySignature:
    if state == sp.POLICY_OFF:
        return sp.capture_replay(SCRIPT, policy_state=state)
    return sp.capture_replay(
        SCRIPT,
        policy_state=state,
        native_session=session if session is not None else RecordingNativeSession(),
    )


# --------------------------------------------------------------------------- #
# policy-off baseline
# --------------------------------------------------------------------------- #
def test_policy_off_signature_is_complete_and_carries_no_adaptive_stream() -> None:
    signature = _capture(sp.POLICY_OFF)

    assert signature.policy_state == "OFF"
    assert signature.native_missing() == ()
    assert signature.missing() == ("decisions",)
    assert signature.decisions is None
    assert signature.decision_ids == ()
    assert all(sp._is_digest(signature.digest(stream) or "") for stream in sp.NATIVE_STREAMS)


def test_policy_off_replay_reproduces_the_pinned_reference_byte_for_byte() -> None:
    signature = _capture(sp.POLICY_OFF)
    pinned = sp.pin(signature)

    # The reference is canonical JSON: sorted keys, no whitespace, no NaN.
    assert pinned == sp.pin(sp.ReplaySignature.model_validate(json.loads(pinned)))
    assert " " not in pinned and "\n" not in pinned
    assert json.loads(pinned)["policy_state"] == "OFF"

    sp.assert_pinned(signature, pinned)
    sp.assert_pinned(signature, json.loads(pinned))
    sp.assert_pinned(signature, signature)
    assert pinned == PINNED_REFERENCE_JSON


# --------------------------------------------------------------------------- #
# policy-on shadow
# --------------------------------------------------------------------------- #
def test_policy_on_shadow_adds_no_native_mutation_and_populates_decisions() -> None:
    policy_off = _capture(sp.POLICY_OFF)
    shadow = _capture(sp.POLICY_SHADOW)

    sp.assert_shadow_unmutated(policy_off, shadow)

    for stream in sp.NATIVE_STREAMS:
        assert shadow.digest(stream) == policy_off.digest(stream), stream

    receipts = sp.shadow_decisions(SCRIPT)
    enumerated = tuple(receipt.decision_id for receipt in receipts)
    assert len(receipts) == len(SCRIPT.cuts) == 4
    assert len(set(enumerated)) == 4
    assert shadow.decision_ids == enumerated
    assert shadow.decisions is not None and sp._is_digest(shadow.decisions)


def test_shadow_decisions_are_verified_advisory_and_legal() -> None:
    receipts = sp.shadow_decisions(SCRIPT)
    assert len(receipts) == 4
    actions = {receipt.action for receipt in receipts}
    # The adaptive chain actually responded to the evidence, cut by cut.
    assert actions == {DecisionAction.ABSTAIN, DecisionAction.ENTER, DecisionAction.HOLD}

    cuts = sorted(SCRIPT.cuts, key=lambda cut: cut.decision_time_ns)
    for cut, receipt in zip(cuts, receipts, strict=True):
        assert isinstance(receipt, DecisionReceipt)
        assert receipt.advisory_only is True
        assert receipt.verify()
        assert receipt.policy_version == "trader-policy-v1"
        assert receipt.decision_time_ns == cut.decision_time_ns

        armed = receipt.opportunity_id is not None
        stale = ReasonCode.BELIEF_STALE in receipt.reason_codes
        legal = LegalActionTable.legal(
            cut.position_presence,
            venue=cut.venue_status,
            stale=stale,
            armed_opportunity=armed,
        )
        assert receipt.action in legal, (cut.decision_time_ns, receipt.action, legal)

    by_time = {receipt.decision_time_ns: receipt for receipt in receipts}
    first = by_time[SCRIPT.cuts[0].decision_time_ns]
    assert ReasonCode.OPPORTUNITY_NOT_ARMED in first.reason_codes
    last = by_time[SCRIPT.cuts[-1].decision_time_ns]
    assert ReasonCode.VENUE_UNKNOWN in last.reason_codes
    entry = by_time[SCRIPT.cuts[1].decision_time_ns]
    assert entry.action is DecisionAction.ENTER
    assert entry.proposed_intent.value == "LIMIT_ENTRY"
    hold = by_time[SCRIPT.cuts[2].decision_time_ns]
    assert hold.action is DecisionAction.HOLD


# --------------------------------------------------------------------------- #
# unreadable belief is never presented as fresh
# --------------------------------------------------------------------------- #
def _requests() -> list[sp.DecisionRequest]:
    """The policy request for every cut, assembled exactly as the shadow pass does."""
    previous: MarketBeliefV1 | None = None
    requests: list[sp.DecisionRequest] = []
    for cut in sorted(SCRIPT.cuts, key=lambda row: row.decision_time_ns):
        belief, _view = sp._belief_at(SCRIPT, cut, previous)
        previous = belief
        requests.append(sp._decision_request(SCRIPT, cut, belief))
    return requests


def _unreadable(belief: MarketBeliefV1) -> MarketBeliefV1:
    """The same belief, but its aggregate quality is unreadable while a regime is named.

    ``uncertainty`` is left at its original (permissive) band on purpose: the
    ``BELIEF_*`` gate alone must block new risk, not the uncertainty ladder.
    """
    payload = belief.model_dump()
    payload.update(
        {
            "status": BeliefStatus.UNKNOWN,
            "quality": QualityState.CONTRADICTORY,
            "origin": "UPDATED",
            "transition": None,
            "version_hash": "",
        }
    )
    return MarketBeliefV1.model_validate(payload)


def test_unknown_belief_can_never_enter_or_bare_hold() -> None:
    policy = TraderDecisionPolicy()
    previous: MarketBeliefV1 | None = None
    entry_cut = SCRIPT.cuts[1]
    for cut in sorted(SCRIPT.cuts, key=lambda row: row.decision_time_ns):
        belief, _view = sp._belief_at(SCRIPT, cut, previous)
        previous = belief
        if cut is not entry_cut:
            continue
        fresh_request = sp._decision_request(SCRIPT, cut, belief)
        assert policy.evaluate(fresh_request).action is DecisionAction.ENTER

        unknown = _unreadable(belief)
        assert sp._belief_view(unknown).stale is True
        assert sp._belief_view(unknown).abstain is False
        decision = policy.evaluate(sp._decision_request(SCRIPT, cut, unknown))
        assert decision.action not in {DecisionAction.ENTER, DecisionAction.HOLD}
        belief_reasons = [
            code for code in decision.reason_codes if code.value.startswith("BELIEF_")
        ]
        assert belief_reasons, decision.reason_codes
        assert ReasonCode.BELIEF_STALE in decision.reason_codes


def test_unknown_belief_on_an_open_position_cannot_hold() -> None:
    policy = TraderDecisionPolicy()
    previous: MarketBeliefV1 | None = None
    open_cut = SCRIPT.cuts[2]
    for cut in sorted(SCRIPT.cuts, key=lambda row: row.decision_time_ns):
        belief, _view = sp._belief_at(SCRIPT, cut, previous)
        previous = belief
        if cut is not open_cut:
            continue
        assert policy.evaluate(sp._decision_request(SCRIPT, cut, belief)).action is (
            DecisionAction.HOLD
        )

        unknown = _unreadable(belief)
        decision = policy.evaluate(sp._decision_request(SCRIPT, cut, unknown))
        assert decision.action is not DecisionAction.HOLD
        assert ReasonCode.BELIEF_STALE in decision.reason_codes


def _belief_with(belief: MarketBeliefV1, **updates: object) -> MarketBeliefV1:
    """The same belief content under a different status/quality (revalidated)."""
    payload = belief.model_dump()
    payload.update({"version_hash": "", **updates})
    return MarketBeliefV1.model_validate(payload)


def test_every_non_fresh_status_is_unusable_and_abstain_keeps_its_branch() -> None:
    previous: MarketBeliefV1 | None = None
    belief, _view = sp._belief_at(SCRIPT, SCRIPT.cuts[2], previous)
    assert belief.status is BeliefStatus.FRESH, belief.status

    fresh = sp._belief_view(belief)
    assert (fresh.stale, fresh.abstain) == (False, False)

    stale = sp._belief_view(
        _belief_with(
            belief,
            status=BeliefStatus.STALE,
            quality=QualityState.STALE,
            uncertainty=UncertaintyState.UNKNOWN,
        )
    )
    assert (stale.stale, stale.abstain) == (True, False)

    unknown = sp._belief_view(_unreadable(belief))
    assert (unknown.stale, unknown.abstain) == (True, False)

    abstained = sp._belief_view(
        _belief_with(
            belief,
            status=BeliefStatus.ABSTAIN,
            quality=QualityState.MISSING,
            uncertainty=UncertaintyState.UNKNOWN,
            regime_label="UNKNOWN",
            regime_candidate="UNKNOWN",
            candidate_streak=0,
            missing_inputs=("bars",),
            transition=None,
        )
    )
    assert (abstained.stale, abstained.abstain) == (False, True)


# --------------------------------------------------------------------------- #
# receipts carry the constraints that shaped the decision
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("code", "field"),
    (
        ("VENUE_UNKNOWN", "constraints"),
        ("POSITION_OPEN", "reason_codes"),
        ("SCALE_IN_DISABLED", "reason_codes"),
    ),
)
def test_receipt_carries_the_provenance_that_shaped_the_decision(code: str, field: str) -> None:
    receipts = sp.shadow_decisions(SCRIPT)
    requests = _requests()
    policy = TraderDecisionPolicy()
    assert len(receipts) == len(requests)

    matched: list[DecisionReceipt] = []
    for receipt, request in zip(receipts, requests, strict=True):
        explanation = policy.explain(request)
        # The receipt's active-constraint tuple is the policy's, not a placeholder.
        assert receipt.constraints == explanation.constraints
        assert receipt.reason_codes == explanation.reason_codes
        if code in getattr(receipt, field):
            matched.append(receipt)

    assert len(matched) == 1, (code, [tuple(getattr(r, field)) for r in receipts])


def test_receipt_digest_covers_the_constraint_set() -> None:
    decision = TraderDecisionPolicy().evaluate(_requests()[3])

    plain = DecisionLedger()
    plain.append(decision, ReceiptInputs())
    constrained = DecisionLedger()
    constrained.append(decision, ReceiptInputs(constraints=("VENUE_UNKNOWN",)))

    unconstrained_receipt = plain.receipts()[0]
    constrained_receipt = constrained.receipts()[0]
    assert unconstrained_receipt.constraints == ()
    assert constrained_receipt.constraints == ("VENUE_UNKNOWN",)
    assert constrained_receipt.digest != unconstrained_receipt.digest
    assert constrained_receipt.verify() and unconstrained_receipt.verify()


def test_shadow_replay_is_deterministic_across_repeats() -> None:
    first = _capture(sp.POLICY_SHADOW)
    second = _capture(sp.POLICY_SHADOW)
    assert sp.pin(first) == sp.pin(second)
    assert first.decision_ids == second.decision_ids


# --------------------------------------------------------------------------- #
# the zero-native-mutation gate observes a real sink
# --------------------------------------------------------------------------- #
def test_shadow_capture_requires_an_observable_native_sink() -> None:
    with pytest.raises(sp.ShadowParityError) as missing:
        sp.capture_replay(SCRIPT, policy_state=sp.POLICY_SHADOW)
    assert missing.value.code == "SHADOW_SINK_MISSING"

    class MuteSession:
        def digest(self) -> str:
            return "not-a-digest"

        def submit(self, intent: object) -> None:  # pragma: no cover - never reached
            raise AssertionError("a shadow pass must not submit")

        def cancel(self, client_order_id: str) -> None:  # pragma: no cover - never reached
            raise AssertionError("a shadow pass must not cancel")

    with pytest.raises(sp.ShadowParityError) as malformed:
        sp.capture_replay(SCRIPT, policy_state=sp.POLICY_SHADOW, native_session=MuteSession())
    assert malformed.value.code == "SHADOW_SINK_MALFORMED"
    assert malformed.value.reasons == ("SHADOW_SINK_MALFORMED:before",)


def test_clean_shadow_pass_reads_the_sink_twice_and_leaves_it_alone() -> None:
    session = RecordingNativeSession()
    shadow = sp.capture_replay(SCRIPT, policy_state=sp.POLICY_SHADOW, native_session=session)

    # The gate provably observed the sink, before and after the pass.
    assert session.digest_calls == 2
    assert session.calls == ()
    assert shadow.decisions is not None
    assert len(shadow.decision_ids) == len(SCRIPT.cuts)


def test_mutating_adaptive_step_is_caught_by_the_native_sink_gate() -> None:
    """The gate fails: an adaptive step that submits is detected, not argued away."""

    def mutating_step(
        events: sp.ReplayScript, session: sp.NativeSession
    ) -> tuple[DecisionReceipt, ...]:
        session.submit({"action": "ENTER"})
        return sp.shadow_decisions(events)

    session = RecordingNativeSession()
    with pytest.raises(sp.ShadowMutationError) as caught:
        sp.capture_replay(
            SCRIPT,
            policy_state=sp.POLICY_SHADOW,
            native_session=session,
            shadow_step=mutating_step,
        )
    assert caught.value.reasons == ("DIGEST_DRIFT:native_session",)
    assert session.calls == (("submit", "{'action': 'ENTER'}"),)


def test_cancelling_adaptive_step_is_caught_by_the_native_sink_gate() -> None:
    def cancelling_step(
        events: sp.ReplayScript, session: sp.NativeSession
    ) -> tuple[DecisionReceipt, ...]:
        session.cancel("O-1")
        return sp.shadow_decisions(events)

    with pytest.raises(sp.ShadowMutationError):
        sp.capture_replay(
            SCRIPT,
            policy_state=sp.POLICY_SHADOW,
            native_session=RecordingNativeSession(),
            shadow_step=cancelling_step,
        )


# --------------------------------------------------------------------------- #
# equivalence and stated order sensitivity
# --------------------------------------------------------------------------- #
def _reversed(script: sp.ReplayScript) -> sp.ReplayScript:
    """Same events, every stream reversed, cuts out of time order."""
    return script.model_copy(
        update={
            "bars": tuple(reversed(script.bars)),
            "opportunities": tuple(reversed(script.opportunities)),
            "native_decisions": tuple(reversed(script.native_decisions)),
            "orders": tuple(reversed(script.orders)),
            "protection": tuple(reversed(script.protection)),
            "fills": tuple(reversed(script.fills)),
            "cashflows": tuple(reversed(script.cashflows)),
            "funding": tuple(reversed(script.funding)),
            "cuts": tuple(reversed(script.cuts)),
        }
    )


def test_reordered_fixture_keeps_native_digests_and_decision_ids_stable() -> None:
    reordered = _reversed(SCRIPT)
    assert reordered == sp.ReplayScript.model_validate(reordered.model_dump())

    baseline = _capture(sp.POLICY_OFF)
    shuffled_off = sp.capture_replay(reordered, policy_state=sp.POLICY_OFF)
    assert sp.pin(shuffled_off) == sp.pin(baseline)
    sp.assert_pinned(shuffled_off, sp.pin(baseline))

    shadow = _capture(sp.POLICY_SHADOW)
    shuffled_shadow = sp.capture_replay(
        reordered, policy_state=sp.POLICY_SHADOW, native_session=RecordingNativeSession()
    )
    assert shuffled_shadow.decision_ids == shadow.decision_ids
    assert sp.pin(shuffled_shadow) == sp.pin(shadow)
    sp.assert_shadow_unmutated(shuffled_off, shuffled_shadow)


def test_decision_stream_is_order_sensitive_so_duplicate_cut_times_are_refused() -> None:
    duplicate = SCRIPT.cuts[1].model_copy(
        update={"decision_time_ns": SCRIPT.cuts[0].decision_time_ns}
    )
    with pytest.raises(ValidationError, match="distinct decision_time_ns"):
        sp.ReplayScript.model_validate(
            {**SCRIPT.model_dump(), "cuts": (*SCRIPT.cuts[:-1], duplicate)}
        )


def test_equivalent_decimal_representations_do_not_drift() -> None:
    baseline = _capture(sp.POLICY_OFF)
    trailing_zero_fill = sp.FillEvent.model_validate(
        {**SCRIPT.fills[0].model_dump(), "fee_amount": Decimal("0.05950")}
    )
    respelled = SCRIPT.model_copy(update={"fills": (trailing_zero_fill, *SCRIPT.fills[1:])})
    assert sp.pin(sp.capture_replay(respelled, policy_state=sp.POLICY_OFF)) == sp.pin(baseline)


# --------------------------------------------------------------------------- #
# numerical drift
# --------------------------------------------------------------------------- #
def _drifted(stream: str, step: str) -> sp.ReplayScript:
    """Mutate exactly one amount of ``stream`` by ``step`` (one tick / one cent)."""
    delta = Decimal(step)
    if stream == "opportunity":
        return SCRIPT.model_copy(
            update={
                "opportunities": (
                    SCRIPT.opportunities[0].model_copy(update={"direction": "SHORT"}),
                )
            }
        )
    if stream == "orders":
        return SCRIPT.model_copy(
            update={
                "orders": (
                    SCRIPT.orders[0].model_copy(update={"price": SCRIPT.orders[0].price + delta}),
                    SCRIPT.orders[1],
                )
            }
        )
    if stream == "protection":
        return SCRIPT.model_copy(
            update={
                "protection": (
                    SCRIPT.protection[0].model_copy(
                        update={"trigger_price": SCRIPT.protection[0].trigger_price + delta}
                    ),
                    SCRIPT.protection[1],
                )
            }
        )
    if stream == "fills":
        return SCRIPT.model_copy(
            update={
                "fills": (
                    SCRIPT.fills[0].model_copy(
                        update={"fee_amount": SCRIPT.fills[0].fee_amount + delta}
                    ),
                    SCRIPT.fills[1],
                )
            }
        )
    if stream == "funding":
        return SCRIPT.model_copy(
            update={
                "funding": (
                    SCRIPT.funding[0].model_copy(
                        update={"amount": SCRIPT.funding[0].amount + delta}
                    ),
                )
            }
        )
    if stream == "lineage":
        return SCRIPT.model_copy(
            update={
                "cashflows": (
                    SCRIPT.cashflows[0].model_copy(
                        update={"amount": SCRIPT.cashflows[0].amount + delta}
                    ),
                    SCRIPT.cashflows[1],
                )
            }
        )
    raise AssertionError(f"unknown stream {stream!r}")


@pytest.mark.parametrize(
    ("stream", "step"),
    [
        ("opportunity", "0"),
        ("orders", "0.01"),
        ("protection", "0.01"),
        ("fills", "0.01"),
        ("funding", "0.01"),
        ("lineage", "0.01"),
    ],
)
def test_one_tick_or_one_cent_drift_fails_and_names_the_stream(stream: str, step: str) -> None:
    baseline = _capture(sp.POLICY_OFF)
    drifted = sp.capture_replay(_drifted(stream, step), policy_state=sp.POLICY_OFF)

    verdict = sp.compare(baseline, drifted)

    assert verdict.status is GateStatus.FAIL
    assert verdict.passed() is False
    failing = {row.stream for row in verdict.streams if row.status is GateStatus.FAIL}
    assert failing == {stream}
    assert f"DIGEST_DRIFT:{stream}" in verdict.blocking_reasons
    assert all(row.status is GateStatus.PASS for row in verdict.streams if row.stream != stream)


def test_cashflow_drift_is_caught_by_the_lineage_digest() -> None:
    baseline = _capture(sp.POLICY_OFF)
    drifted = sp.capture_replay(_drifted("lineage", "0.01"), policy_state=sp.POLICY_OFF)
    assert baseline.lineage != drifted.lineage
    # Cashflows carry no stream of their own; the lineage digest is where they land.
    verdict = sp.compare(baseline, drifted)
    assert verdict.status is GateStatus.FAIL
    assert tuple(row.stream for row in verdict.streams if row.status is GateStatus.FAIL) == (
        "lineage",
    )


# --------------------------------------------------------------------------- #
# fail-closed comparisons
# --------------------------------------------------------------------------- #
def test_missing_digest_is_blocked_never_pass() -> None:
    baseline = _capture(sp.POLICY_OFF)

    for stream in sp.NATIVE_STREAMS:
        truncated = baseline.model_copy(update={stream: None})
        verdict = sp.compare(baseline, truncated)
        assert verdict.status is GateStatus.BLOCKED
        assert verdict.passed() is False
        assert f"DIGEST_MISSING:{stream}:candidate" in verdict.blocking_reasons

    truncated_reference = baseline.model_copy(update={"orders": None})
    verdict = sp.compare(truncated_reference, baseline)
    assert verdict.status is GateStatus.BLOCKED
    assert "DIGEST_MISSING:orders:baseline" in verdict.blocking_reasons


def test_shadow_candidate_is_not_certified_as_a_policy_off_baseline() -> None:
    baseline = _capture(sp.POLICY_OFF)
    shadow = _capture(sp.POLICY_SHADOW)

    verdict = sp.compare(baseline, shadow)
    assert verdict.status is GateStatus.BLOCKED
    assert verdict.passed() is False
    assert "CANDIDATE_NOT_POLICY_OFF:SHADOW" in verdict.blocking_reasons
    assert all(row.status is GateStatus.PASS for row in verdict.streams)

    self_compared = sp.compare(shadow, shadow)
    assert self_compared.status is GateStatus.BLOCKED
    assert "REFERENCE_NOT_POLICY_OFF:SHADOW" in self_compared.blocking_reasons


def test_shadow_helper_refuses_non_shadow_or_foreign_streams() -> None:
    baseline = _capture(sp.POLICY_OFF)
    shadow = _capture(sp.POLICY_SHADOW)

    with pytest.raises(sp.ShadowMutationError) as both_off:
        sp.assert_shadow_unmutated(baseline, baseline)
    assert "CANDIDATE_NOT_SHADOW:OFF" in both_off.value.reasons

    with pytest.raises(sp.ShadowMutationError) as shadow_reference:
        sp.assert_shadow_unmutated(shadow, shadow)
    assert "REFERENCE_NOT_POLICY_OFF:SHADOW" in shadow_reference.value.reasons

    no_decisions = shadow.model_copy(update={"decisions": None, "decision_ids": ()})
    with pytest.raises(sp.ShadowMutationError) as empty:
        sp.assert_shadow_unmutated(baseline, no_decisions)
    assert "SHADOW_DECISION_STREAM_MISSING" in empty.value.reasons
    assert "SHADOW_DECISION_IDS_EMPTY" in empty.value.reasons

    mutated = shadow.model_copy(update={"orders": "0" * 64})
    with pytest.raises(sp.ShadowMutationError) as drifted:
        sp.assert_shadow_unmutated(baseline, mutated)
    assert "DIGEST_DRIFT:orders" in drifted.value.reasons


def test_shadow_without_cuts_is_not_evidence() -> None:
    no_cuts = SCRIPT.model_copy(update={"cuts": ()})
    with pytest.raises(sp.ShadowNotExercised):
        sp.capture_replay(
            no_cuts, policy_state=sp.POLICY_SHADOW, native_session=RecordingNativeSession()
        )
    # A policy-off capture needs no cuts: it never evaluates the adaptive chain.
    assert sp.capture_replay(no_cuts, policy_state=sp.POLICY_OFF).native_missing() == ()


def test_incomplete_lineage_cannot_be_a_baseline() -> None:
    no_cashflows = SCRIPT.model_copy(update={"cashflows": ()})
    with pytest.raises(sp.LineageNotCertifiable) as blocked:
        sp.capture_replay(no_cashflows, policy_state=sp.POLICY_OFF)
    assert "MISSING_JOIN" in blocked.value.reasons

    # A record after the declared certification cut is BLOCKED, never COMPLETE.
    late = SCRIPT.model_copy(update={"as_of_ns": SCRIPT.fills[1].event_time_ns - 1})
    with pytest.raises(sp.LineageNotCertifiable) as late_blocked:
        sp.capture_replay(late, policy_state=sp.POLICY_OFF)
    assert "LATE_RECORD" in late_blocked.value.reasons


def test_unknown_policy_state_is_refused() -> None:
    with pytest.raises(sp.ShadowParityError) as refused:
        sp.capture_replay(SCRIPT, policy_state="SHADOWY")  # type: ignore[arg-type]
    assert refused.value.code == "UNKNOWN_POLICY_STATE"


def test_capture_refuses_foreign_inputs_and_foreign_cut_instruments() -> None:
    with pytest.raises(TypeError):
        sp.capture_replay(object(), policy_state=sp.POLICY_OFF)  # type: ignore[arg-type]

    foreign_cut = SCRIPT.cuts[0].model_copy(update={"instrument_id": "ETHUSDT-PERP.BINANCE"})
    foreign = SCRIPT.model_copy(update={"cuts": (foreign_cut, *SCRIPT.cuts[1:])})
    with pytest.raises(sp.ShadowParityError) as mismatch:
        sp.capture_replay(
            foreign, policy_state=sp.POLICY_SHADOW, native_session=RecordingNativeSession()
        )
    assert mismatch.value.code == "CUT_INSTRUMENT_MISMATCH"
    # A policy-off capture has no adaptive chain, so it never reads the cut's instrument.
    assert sp.capture_replay(foreign, policy_state=sp.POLICY_OFF).native_missing() == ()


# --------------------------------------------------------------------------- #
# pinning
# --------------------------------------------------------------------------- #
def test_assert_pinned_names_every_drifted_stream() -> None:
    baseline = _capture(sp.POLICY_OFF)
    reference = sp.pin(baseline)
    drifted = sp.capture_replay(_drifted("orders", "0.01"), policy_state=sp.POLICY_OFF)

    with pytest.raises(sp.PinnedReferenceDrift) as error:
        sp.assert_pinned(drifted, reference)
    assert error.value.reasons == ("PINNED_DRIFT:orders",)
    assert "orders" in str(error.value)


def test_assert_pinned_refuses_malformed_and_shadow_references() -> None:
    baseline = _capture(sp.POLICY_OFF)
    shadow = _capture(sp.POLICY_SHADOW)

    with pytest.raises(sp.PinnedReferenceMalformed):
        sp.assert_pinned(baseline, "{not json")
    with pytest.raises(sp.PinnedReferenceMalformed):
        sp.assert_pinned(baseline, "[]")

    with pytest.raises(sp.PinnedReferenceDrift) as shadow_reference:
        sp.assert_pinned(shadow, sp.pin(baseline))
    assert shadow_reference.value.reasons == ("CANDIDATE_NOT_POLICY_OFF:SHADOW",)

    with pytest.raises(sp.PinnedReferenceDrift) as wrong_pin:
        sp.assert_pinned(baseline, sp.pin(shadow))
    assert wrong_pin.value.reasons == ("REFERENCE_NOT_POLICY_OFF:SHADOW",)


# --------------------------------------------------------------------------- #
# signature and money discipline
# --------------------------------------------------------------------------- #
def test_signature_refuses_mixed_policy_states() -> None:
    baseline = _capture(sp.POLICY_OFF)
    shadow = _capture(sp.POLICY_SHADOW)

    with pytest.raises(ValidationError, match="policy-off replay"):
        sp.ReplaySignature(**{**baseline.model_dump(), "decisions": shadow.decisions})
    with pytest.raises(ValidationError, match="shadow replay"):
        sp.ReplaySignature(**{**shadow.model_dump(), "decisions": None, "decision_ids": ()})
    with pytest.raises(ValidationError, match="malformed digest"):
        sp.ReplaySignature(**{**baseline.model_dump(), "orders": "not-a-digest"})
    with pytest.raises(ValidationError, match="duplicate decision ids"):
        sp.ReplaySignature(**{**shadow.model_dump(), "decision_ids": (shadow.decision_ids[0],) * 2})


def test_money_values_must_be_decimal() -> None:
    for price in (119.0, "119"):
        with pytest.raises(ValidationError, match="Decimal"):
            sp.NativeOrderEvent(
                client_order_id="O-x",
                decision_id="dec-0001",
                campaign_id=CAMPAIGN,
                instrument_id=IID,
                side="BUY",
                order_type="LIMIT",
                quantity=Decimal("1"),
                price=price,  # type: ignore[arg-type]
                submitted_ns=BASE,
                available_at_ns=BASE,
            )


def test_script_refuses_duplicate_layer_ids() -> None:
    with pytest.raises(ValidationError, match="duplicate order id"):
        sp.ReplayScript.model_validate(
            {**SCRIPT.model_dump(), "orders": (SCRIPT.orders[0], SCRIPT.orders[0])}
        )


# --------------------------------------------------------------------------- #
# structural impossibility of order side effects
# --------------------------------------------------------------------------- #
_FORBIDDEN_IMPORT_PREFIXES = (
    "nautilus_trader",
    "v8_next.adapters",
    "v8_next.exchange",
    "v8_next.execution",
    "v8_next.venues",
)
_FORBIDDEN_NAMES = (
    "ClientOrderId",
    "OrderFactory",
    "OrderType",
    "OrderSide",
    "OrderList",
    "submit_order",
    "place_order",
    "cancel_order",
    "modify_order",
)


def _module_tree() -> ast.Module:
    path = Path(sp.__file__ or "")
    assert path.is_file(), path
    return ast.parse(path.read_text())


def test_module_imports_no_execution_adapter_or_order_type() -> None:
    tree = _module_tree()
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
            imported.extend(f"{node.module}.{alias.name}" for alias in node.names)
    assert imported, "the scan must actually see imports"
    for name in imported:
        for forbidden in _FORBIDDEN_IMPORT_PREFIXES:
            assert not name.startswith(forbidden), (name, forbidden)


def test_module_never_names_an_order_primitive() -> None:
    tree = _module_tree()
    named = (
        {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        | {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        | {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
    )
    assert not named & set(_FORBIDDEN_NAMES)
    # The harness's own outputs are advisory, and no signature field is order-shaped.
    assert "order_id" not in sp.ReplaySignature.model_fields
    assert not set(sp.ReplaySignature.model_fields) & {
        "order",
        "order_type",
        "side",
        "quantity",
        "price",
        "client_order_id",
    }
