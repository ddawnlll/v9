"""Report composition checks; native settlement arithmetic is tested separately."""

import json
from decimal import Decimal

from v8_next.app.report import report


def test_baseline_revalues_its_own_campaigns_at_variant_cutoff(tmp_path, monkeypatch):
    config = {"initial_balance": "10000"}
    (tmp_path / "policy.json").write_text(
        json.dumps(
            {
                "policy": {
                    "baseline": "range-breakout-without-compression-v1",
                    "paper_config": config,
                }
            }
        )
    )
    revised = {"accounting_as_of_ns": 40, "funding_coverage": "UNQUALIFIED"}
    (tmp_path / "paper-state.json").write_text(
        json.dumps(
            {
                "manifests": ["capture/manifest.json"],
                "native_state": {"campaigns": []},
                "revised_accounting": revised,
            }
        )
    )
    calls = []

    def inspect(run, decision_ns):
        assert decision_ns == 50
        calls.append("verified")
        return {"reason": "FUNDING_COVERAGE_UNQUALIFIED"}

    def baseline(paths, assumptions, *, observer):
        assert calls == ["verified"]
        assert observer == "breakout_baseline"
        assert assumptions == config
        return {
            "campaigns": [
                {
                    "campaign_id": "baseline-only",
                    "opportunity_id": "test-only",
                    "instrument_id": "BTCUSDT-PERP.BINANCE",
                    "direction": "LONG",
                    "quantity": "0.010",
                    "decision_ns": 10,
                    "expires_ns": 30,
                }
            ]
        }

    def accounting(paths, campaigns, assumptions, cutoff):
        assert paths == [tmp_path / "capture/manifest.json"]
        assert assumptions == config
        assert cutoff == 40
        assert len(campaigns) == 1
        assert campaigns[0].campaign_id == "baseline-only"
        assert campaigns[0].quantity == Decimal("0.010")
        return {
            "accounting_as_of_ns": cutoff,
            "funding_coverage": "UNQUALIFIED",
            "realization": "SIMULATED",
            "positions": [{"is_closed": True}, {"is_closed": False}],
        }

    monkeypatch.setattr("v8_next.app.report.evaluate", lambda run: {})
    monkeypatch.setattr("v8_next.app.report.inspect_calibration_source", inspect)
    monkeypatch.setattr("v8_next.app.report.replay_account", baseline)
    monkeypatch.setattr("v8_next.app.report.replay_frozen_campaigns", accounting)
    result = report(tmp_path, 50)
    comparison = result["comparison"]
    assert comparison["variant_revised_accounting"] == revised
    assert comparison["baseline_revised_accounting"]["accounting_as_of_ns"] == 40
    assert comparison["paired_loss_sample"] is None
    assert comparison["spa"] is None
    assert result["promotion_eligible"] is False

    assert "OPEN_OUTCOME_CENSORING_POLICY_REQUIRED" in comparison["baseline_blockers"]
    assert "FUNDING_COVERAGE_UNQUALIFIED" in comparison["baseline_blockers"]
    assert comparison["variant_blockers"] == ["FUNDING_COVERAGE_UNQUALIFIED"]
