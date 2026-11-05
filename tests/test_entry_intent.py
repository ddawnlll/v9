"""M3-01: price-aware entry proposals, TTL and honest fill uncertainty."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from v8_next.domain.decision import VenueOrderStatus
from v8_next.economics.entry import (
    EntryEvaluation,
    EntryIntent,
    EntryMode,
    EntryReason,
    FillCertainty,
    FrictionStatus,
    MarketQuote,
    OrderPolicy,
    ProposalState,
    build_intent,
    cancel,
    evaluate_entry,
    is_duplicate,
    make_intent_id,
    revise,
)

LOW = Decimal("49900")
HIGH = Decimal("50100")


def intent(
    *,
    policy: OrderPolicy = OrderPolicy.LIMIT,
    created_ns: int = 50,
    side: str = "LONG",
    size_policy_id: str = "size-v1",
    structural_stop_ref: str = "stop-1",
    friction_status: FrictionStatus = FrictionStatus.OBSERVED,
    venue_version: str = "venue-v1",
    policy_version: str = "policy-v1",
    kill_conditions: tuple[str, ...] = ("BELIEF_INVALIDATED",),
) -> EntryIntent:
    return build_intent(
        decision_id="d" * 64,
        opportunity_id="opp-1",
        instrument_id="BTCUSDT-PERP.BINANCE",
        side=side,
        order_policy=policy,
        valid_after_ns=100,
        expires_at_ns=1_000,
        acceptable_price_low=LOW,
        acceptable_price_high=HIGH,
        structural_stop_ref=structural_stop_ref,
        size_policy_id=size_policy_id,
        friction_status=friction_status,
        venue_version=venue_version,
        policy_version=policy_version,
        created_ns=created_ns,
        kill_conditions=kill_conditions,
    )


def quote(
    *,
    bid: Decimal | None = Decimal("49990"),
    ask: Decimal | None = Decimal("50010"),
    mark: Decimal | None = Decimal("50000"),
    available_at_ns: int = 100,
) -> MarketQuote:
    return MarketQuote(
        instrument_id="BTCUSDT-PERP.BINANCE",
        bid=bid,
        ask=ask,
        mark=mark,
        available_at_ns=available_at_ns,
        event_time_ns=min(available_at_ns, 100),
        source="test-tape",
    )


def evaluate(
    target: EntryIntent,
    market_quote: MarketQuote | None,
    *,
    now_ns: int,
    venue_status: VenueOrderStatus = VenueOrderStatus.ACCEPTED,
    belief_stale: bool = False,
    belief_available: bool = True,
) -> EntryEvaluation:
    """Evaluate with explicit, known-fresh safety evidence."""
    return evaluate_entry(
        target,
        market_quote,
        now_ns=now_ns,
        venue_status=venue_status,
        belief_stale=belief_stale,
        belief_available=belief_available,
    )


def test_intent_identity_is_deterministic_and_idempotent() -> None:
    first, second = intent(), intent()
    assert first.intent_id == second.intent_id
    assert is_duplicate(first, second)
    assert make_intent_id(first) == first.intent_id


def test_identity_covers_every_field_that_defines_the_proposal() -> None:
    base = intent()
    for other in (
        intent(size_policy_id="size-v2"),
        intent(structural_stop_ref="stop-2"),
        intent(venue_version="venue-v2"),
        intent(policy_version="policy-v2"),
        intent(kill_conditions=("BELIEF_INVALIDATED", "VENUE_UNKNOWN")),
        intent(friction_status=FrictionStatus.MISSING),
    ):
        assert other.intent_id != base.intent_id
        assert not is_duplicate(base, other)


def test_cancelled_record_reproduces_its_own_identity() -> None:
    cancelled = cancel(intent(), "PRICE_MOVED")
    assert cancelled.intent_id == make_intent_id(cancelled)


def test_intent_is_advisory_and_rejects_order_shaped_fields() -> None:
    with pytest.raises(ValidationError):
        EntryIntent.model_validate({**intent().model_dump(), "quantity": "1"})
    with pytest.raises(ValidationError):
        EntryIntent.model_validate({**intent().model_dump(), "advisory_only": False})


def test_not_yet_valid_is_explicit() -> None:
    evaluation = evaluate_entry(intent(), quote(), now_ns=99)
    assert evaluation.mode is EntryMode.NOT_YET_VALID
    assert evaluation.reason is EntryReason.ENTRY_NOT_YET_VALID


def test_market_entry_ready_with_observed_touch() -> None:
    evaluation = evaluate(intent(policy=OrderPolicy.MARKET), quote(), now_ns=100)
    assert evaluation.mode is EntryMode.MARKET_NOW
    assert evaluation.reference_price == Decimal("50010")
    assert evaluation.friction_status is FrictionStatus.OBSERVED
    assert evaluation.fill_certainty is FillCertainty.NOT_APPLICABLE


def test_improved_and_worsened_price_within_band_are_ready() -> None:
    improved = evaluate(intent(), quote(ask=Decimal("49920"), bid=Decimal("49900")), now_ns=100)
    worsened = evaluate(intent(), quote(ask=Decimal("50090"), bid=Decimal("50070")), now_ns=100)
    assert improved.mode is EntryMode.LIMIT_WAIT
    assert worsened.mode is EntryMode.LIMIT_WAIT
    assert improved.limit_price == Decimal("49920")
    assert worsened.limit_price == Decimal("50090")


def test_price_outside_band_is_skipped_not_chased() -> None:
    evaluation = evaluate(intent(), quote(ask=Decimal("50200"), bid=Decimal("50190")), now_ns=100)
    assert evaluation.mode is EntryMode.SKIP_PRICE
    assert evaluation.reason is EntryReason.PRICE_OUTSIDE_BAND


def test_expired_entry_is_cancelled_stale() -> None:
    evaluation = evaluate(intent(), quote(), now_ns=1_000)
    assert evaluation.mode is EntryMode.CANCEL_STALE
    assert evaluation.reason is EntryReason.ENTRY_EXPIRED


def test_unknown_venue_blocks_every_new_entry() -> None:
    evaluation = evaluate(
        intent(policy=OrderPolicy.MARKET),
        quote(),
        now_ns=100,
        venue_status=VenueOrderStatus.UNKNOWN,
    )
    assert evaluation.mode is EntryMode.CANCEL_STALE
    assert evaluation.reason is EntryReason.VENUE_UNKNOWN


def test_omitted_safety_inputs_cannot_be_ready() -> None:
    limit = evaluate_entry(intent(), quote(), now_ns=100)
    market = evaluate_entry(intent(policy=OrderPolicy.MARKET), quote(), now_ns=100)
    assert limit.mode is not EntryMode.LIMIT_WAIT
    assert market.mode is not EntryMode.MARKET_NOW
    assert limit.reason is EntryReason.VENUE_UNKNOWN
    assert market.reason is EntryReason.VENUE_UNKNOWN


def test_accepted_venue_with_omitted_belief_freshness_fails_closed() -> None:
    evaluation = evaluate_entry(
        intent(), quote(), now_ns=100, venue_status=VenueOrderStatus.ACCEPTED
    )
    assert evaluation.mode is EntryMode.WAIT_BELIEF
    assert evaluation.reason is EntryReason.BELIEF_STALE


def test_unreadable_venue_status_is_blocked_not_coerced() -> None:
    evaluation = evaluate_entry(intent(), quote(), now_ns=100, venue_status="PENDING_NEW")
    assert evaluation.mode is EntryMode.BLOCKED
    assert evaluation.reason is EntryReason.VENUE_UNKNOWN


def test_future_quote_is_not_used() -> None:
    evaluation = evaluate(intent(), quote(available_at_ns=101), now_ns=100)
    assert evaluation.mode is EntryMode.WAIT_BELIEF
    assert evaluation.reason is EntryReason.FUTURE_QUOTE


def test_no_spread_market_cannot_be_priced_but_limit_stays_honest() -> None:
    observed = quote(bid=None, ask=None, mark=Decimal("50000"))
    market = evaluate(intent(policy=OrderPolicy.MARKET), observed, now_ns=100)
    limit = evaluate(intent(policy=OrderPolicy.LIMIT), observed, now_ns=100)
    assert market.mode is EntryMode.WAIT_BELIEF
    assert market.reason is EntryReason.DATA_MISSING
    assert limit.mode is EntryMode.LIMIT_WAIT
    assert limit.fill_certainty is FillCertainty.UNKNOWN
    assert limit.friction_status is FrictionStatus.MISSING


def test_touched_limit_is_never_reported_as_filled() -> None:
    evaluation = evaluate(intent(), quote(), now_ns=100)
    assert evaluation.fill_certainty is FillCertainty.TOUCHED_ONLY
    assert evaluation.mode is not EntryMode.MARKET_NOW


def test_post_only_rests_on_the_passive_side() -> None:
    long_evaluation = evaluate(intent(policy=OrderPolicy.POST_ONLY), quote(), now_ns=100)
    short_evaluation = evaluate(
        intent(policy=OrderPolicy.POST_ONLY, side="SHORT"), quote(), now_ns=100
    )
    assert long_evaluation.limit_price == Decimal("49990")
    assert short_evaluation.limit_price == Decimal("50010")
    for evaluation in (long_evaluation, short_evaluation):
        assert evaluation.mode is EntryMode.LIMIT_WAIT
        assert evaluation.reason is EntryReason.ENTRY_READY
        assert evaluation.fill_certainty is FillCertainty.UNKNOWN


def test_post_only_limit_is_strictly_non_marketable() -> None:
    market = quote()
    long_evaluation = evaluate(intent(policy=OrderPolicy.POST_ONLY), market, now_ns=100)
    short_evaluation = evaluate(
        intent(policy=OrderPolicy.POST_ONLY, side="SHORT"), market, now_ns=100
    )
    assert long_evaluation.limit_price is not None
    assert short_evaluation.limit_price is not None
    assert market.ask is not None and market.bid is not None
    assert long_evaluation.limit_price < market.ask
    assert short_evaluation.limit_price > market.bid


def test_locked_book_blocks_post_only_instead_of_crossing() -> None:
    locked = quote(bid=Decimal("50000"), ask=Decimal("50000"))
    for side in ("LONG", "SHORT"):
        evaluation = evaluate(intent(policy=OrderPolicy.POST_ONLY, side=side), locked, now_ns=100)
        assert evaluation.mode is EntryMode.BLOCKED
        assert evaluation.reason is EntryReason.POST_ONLY_WOULD_CROSS


def test_post_only_without_both_touches_fails_closed() -> None:
    for market_quote in (quote(ask=None), quote(bid=None)):
        for side in ("LONG", "SHORT"):
            evaluation = evaluate(
                intent(policy=OrderPolicy.POST_ONLY, side=side), market_quote, now_ns=100
            )
            assert evaluation.mode is EntryMode.WAIT_BELIEF
            assert evaluation.reason is EntryReason.DATA_MISSING


def test_missing_quote_and_stale_belief_wait() -> None:
    assert evaluate(intent(), None, now_ns=100).reason is EntryReason.DATA_MISSING
    stale = evaluate(intent(), quote(), now_ns=100, belief_stale=True)
    assert stale.mode is EntryMode.WAIT_BELIEF
    assert stale.reason is EntryReason.BELIEF_STALE


def test_cancelled_proposal_cannot_be_evaluated_as_live() -> None:
    cancelled = cancel(intent(), "PRICE_MOVED")
    assert cancelled.state is ProposalState.CANCELLED
    assert cancelled.cancel_reasons == ("PRICE_MOVED",)
    evaluation = evaluate(cancelled, quote(), now_ns=100)
    assert evaluation.mode is EntryMode.CANCEL_STALE


def test_revision_is_a_new_proposal_and_leaves_original_immutable() -> None:
    original = intent()
    revised = revise(
        original,
        created_ns=150,
        acceptable_price_low=Decimal("49800"),
        acceptable_price_high=Decimal("50050"),
        expires_at_ns=2_000,
    )
    assert revised.revision == 1
    assert revised.intent_id != original.intent_id
    assert not is_duplicate(original, revised)
    assert original.revision == 0
    assert original.acceptable_price_high == HIGH
    assert revised.expires_at_ns == 2_000


def test_revision_requires_an_actual_change() -> None:
    with pytest.raises(ValueError, match="at least one"):
        revise(intent(), created_ns=150)


def test_band_and_ttl_validation() -> None:
    base = intent().model_dump()
    with pytest.raises(ValidationError):
        EntryIntent.model_validate({**base, "acceptable_price_low": HIGH + 1})
    with pytest.raises(ValidationError):
        EntryIntent.model_validate({**base, "expires_at_ns": 100})
    with pytest.raises(ValidationError):
        EntryIntent.model_validate({**base, "side": "FLAT"})


def test_quote_rejects_inverted_or_non_positive_prices() -> None:
    with pytest.raises(ValidationError):
        MarketQuote(
            instrument_id="BTCUSDT-PERP.BINANCE",
            bid=Decimal("50010"),
            ask=Decimal("50000"),
            available_at_ns=100,
            event_time_ns=100,
            source="tape",
        )
    with pytest.raises(ValidationError):
        MarketQuote(
            instrument_id="BTCUSDT-PERP.BINANCE",
            mark=Decimal("0"),
            available_at_ns=100,
            event_time_ns=100,
            source="tape",
        )


def test_receipt_is_json_safe_and_marks_advisory() -> None:
    receipt = intent().to_receipt()
    assert receipt["advisory_only"] is True
    assert receipt["schema_version"] == "entry-intent-v1"
    assert receipt["intent_id"] == intent().intent_id
