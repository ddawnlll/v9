"""MECHANICS ONLY: venue evidence refuses false reconciliation and zero-filling."""

from v8_next.adapters.shadow_ingest import (
    decompose_fill_shortfall,
    reconcile_shadow_account,
    summarize_fill_latency,
)


def test_shadow_account_count_divergence_remains_unproven() -> None:
    result = reconcile_shadow_account(
        [{"fill_id": "venue-1", "instrument": "BTCUSDT", "price": 100, "qty": 1}],
        {"balance_total": "100", "positions": [], "orders": [], "currency": "USDT"},
    )
    assert result["reconciled"] is False
    assert result["status"] == "EXECUTION_UNPROVEN"
    assert result["claim_status"] == "NO_ECONOMIC_CLAIM"


def test_shortfall_missing_components_are_not_zero_filled() -> None:
    result = decompose_fill_shortfall(
        [{"fill_id": "venue-1", "instrument": "BTCUSDT", "price": 100, "qty": 1}]
    )
    assert result["status"] == "NO_ECONOMIC_CLAIM"
    assert result["model_vs_fill"] is None
    assert result["adverse_selection"] is None
    assert result["fees"] is None


def test_latency_requires_submit_and_venue_timestamps() -> None:
    blocked = summarize_fill_latency(
        [{"fill_id": "venue-1", "instrument": "BTCUSDT", "price": 100, "qty": 1}]
    )
    assert blocked["status"] == "DATA_BLOCKED"
    assert blocked["latency_ms"] is None

    observed = summarize_fill_latency(
        [
            {
                "fill_id": "venue-1",
                "instrument": "BTCUSDT",
                "price": 100,
                "qty": 1,
                "submit_time_ns": 1_000_000_000,
                "venue_time_ns": 1_250_000_000,
            }
        ]
    )
    assert observed["status"] == "OBSERVED_DIAGNOSTIC"
    assert observed["latency_ms"]["median"] == 250.0
