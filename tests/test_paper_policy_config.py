import json

import pytest

from v8_next.app.observe import initialize
from v8_next.app.paper import load_policy_config
from v8_next.domain.config import PaperConfig


def test_paper_symbol_universe_is_validated_and_frozen(tmp_path):
    from v8_next.app.observe import initialize
    from v8_next.domain.config import PaperConfig

    config = dict(
        maker_fee="0",
        taker_fee="0",
        initial_balance="10000",
        max_notional="100",
        max_exposure_fraction=".1",
        symbols=["BTCUSDT", "ETHUSDT"],
    )
    assert PaperConfig.model_validate(config).symbols == ("BTCUSDT", "ETHUSDT")
    frozen = initialize(tmp_path / "session", config)
    assert frozen["policy"]["instruments"] == ["BTCUSDT-PERP.BINANCE", "ETHUSDT-PERP.BINANCE"]
    with pytest.raises(ValueError, match="frozen"):
        initialize(tmp_path / "session", {**config, "symbols": ["BTCUSDT"]})
    for symbols in ([], ["BTCUSDT", "BTCUSDT"], ["UNKNOWN"]):
        with pytest.raises(ValueError):
            PaperConfig.model_validate({**config, "symbols": symbols})




def test_policy_file_freshness_is_validated_and_frozen(tmp_path):
    path = tmp_path / "selection.json"
    policy = {
        "observer_policy": "families:funding-crowding-reversal",
        "campaign_policy": "funding:b:v2",
        "funding_max_age_ns": 3600000000000,
        "open_interest_max_age_ns": 300000000000,
        "calibration_source_run": "/path/to/source",
    }
    path.write_text(json.dumps(policy))
    config = dict(
        maker_fee=".0002",
        taker_fee=".0005",
        initial_balance="10000",
        max_notional="100",
        max_exposure_fraction=".1",
    )
    config.update(load_policy_config(path))
    parsed = PaperConfig.model_validate(config)
    assert parsed.funding_max_age_ns == policy["funding_max_age_ns"]
    assert parsed.calibration_source_run == "/path/to/source"
    run = tmp_path / "run"
    frozen = initialize(run, config)
    assert frozen["policy"]["paper_config"] == config
    assert initialize(run, config) == frozen
    with pytest.raises(ValueError, match="frozen"):
        initialize(run, {**config, "funding_max_age_ns": 1})
    with pytest.raises(ValueError, match="frozen"):
        initialize(run, {**config, "open_interest_max_age_ns": 1})
    for invalid in (True, "100", 0, -1):
        with pytest.raises(ValueError):
            PaperConfig.model_validate({**config, "funding_max_age_ns": invalid})
        with pytest.raises(ValueError):
            PaperConfig.model_validate({**config, "open_interest_max_age_ns": invalid})


@pytest.mark.parametrize("payload", [{"maker_fee": "0"}, {"unknown": 1}, []])
def test_policy_file_cannot_override_execution_assumptions(tmp_path, payload):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="economic policy"):
        load_policy_config(path)


@pytest.mark.parametrize(
    "fields",
    [
        {"account_ratio_period": "5m"},
        {"account_ratio_max_age_ns": 100},
        {"account_ratio_period": "2m", "account_ratio_max_age_ns": 100},
        {"account_ratio_period": "5m", "account_ratio_max_age_ns": True},
    ],
)
def test_ratio_requires_complete_explicit_policy(fields):
    base = dict(
        maker_fee=".0002",
        taker_fee=".0005",
        initial_balance="10000",
        max_notional="100",
        max_exposure_fraction=".1",
    )
    with pytest.raises(ValueError):
        PaperConfig.model_validate({**base, **fields})
