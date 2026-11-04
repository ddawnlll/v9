from decimal import Decimal as D

from v8_next.domain.market import Candle, CausalFrame
from v8_next.economics.decisions import Opportunity
from v8_next.economics.protection import protection_at


def test_squeeze_uses_source_range_geometry_and_macro_expiry():
    bars = []
    for i in range(69):
        close = D(100 + (10 if i % 2 else -10)) if i < 20 else D(100) + D(i) / 100
        bars.append(
            Candle(
                "BTC",
                i,
                i + 1,
                close,
                close + D(".001"),
                close - D(".001"),
                close,
                D(3 if i == 68 else 1),
                i + 1,
                i + 1,
                "test-only",
            )
        )
    frame = CausalFrame("BTC", 69, tuple(bars))
    opportunity = Opportunity("s", "btc", "BTC", "LONG", 69, 1000)
    result = protection_at(frame, opportunity, "squeeze:baseline:v2", D(".001"))
    assert result is not None
    assert result.stop_price == bars[-1].close - D(".004")
    assert result.target_price == bars[-1].close + D(".008")
    assert result.expires_ns == 69 + 336

    from v8_next.economics.observer_policy import policy_stances

    m1 = policy_stances(frame, opportunity, "squeeze:m1")[0]
    assert m1.variant_id == "m1"
    assert m1.kind == "SUPPORT"
    assert protection_at(frame, opportunity, "squeeze:m1:v2", D(".001")) is not None
    for variant in ("m2", "m3"):
        stance = policy_stances(frame, opportunity, f"squeeze:{variant}")[0]
        assert stance.reason == "WARMUP"
        assert protection_at(frame, opportunity, f"squeeze:{variant}:v2", D(".001")) is None

    from dataclasses import replace

    from v8_next.economics.decisions import observe_squeeze

    for changed in ({"direction": "SHORT"}, {"instrument_id": "ETH"}):
        assert observe_squeeze(frame, replace(opportunity, **changed)).kind == "ABSTAIN"
    mirrored = tuple(
        replace(c, open=200 - c.open, close=200 - c.close, high=200 - c.low, low=200 - c.high)
        for c in bars
    )
    short_frame = replace(frame, candles=mirrored)
    short_opportunity = replace(opportunity, direction="SHORT")
    short = protection_at(short_frame, short_opportunity, "squeeze:baseline:v2", D(".001"))
    assert short is not None
    assert short.stop_price == mirrored[-1].close + D(".004")
    assert short.target_price == mirrored[-1].close - D(".008")


def test_macro_variants_include_oldest_prior_bar_and_support_both_directions():
    from dataclasses import replace

    from v8_next.economics.observer_policy import policy_stances

    bars = tuple(
        Candle(
            "BTC",
            i,
            i + 1,
            close,
            close + D(".001"),
            close - D(".001"),
            close,
            D(3 if i == 72 else 1),
            i + 1,
            i + 1,
            "test-only",
        )
        for i in range(73)
        for close in [D(100 + (2 if i % 2 else -2)) if i < 20 else D(100) + D(i) / 10]
    )
    for direction in ("LONG", "SHORT"):
        candles = (
            bars
            if direction == "LONG"
            else tuple(
                replace(
                    c, open=200 - c.open, close=200 - c.close, high=200 - c.low, low=200 - c.high
                )
                for c in bars
            )
        )
        frame = CausalFrame("BTC", 73, candles)
        opportunity = Opportunity("macro", "btc", "BTC", direction, 73, 1000)
        for variant in ("m2", "m3"):
            assert policy_stances(frame, opportunity, f"squeeze:{variant}")[0].kind == "SUPPORT"
            assert protection_at(frame, opportunity, f"squeeze:{variant}:v2", D(".001")) is not None
            oldest = (
                replace(candles[0], high=D(199))
                if direction == "LONG"
                else replace(candles[0], low=D(1))
            )
            blocked = replace(frame, candles=(oldest,) + candles[1:])
            assert (
                policy_stances(blocked, opportunity, f"squeeze:{variant}")[0].reason
                == "NO_VARIANT_BREAKOUT"
            )
            assert protection_at(blocked, opportunity, f"squeeze:{variant}:v2", D(".001")) is None
