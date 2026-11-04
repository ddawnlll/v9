import pytest
from pydantic import ValidationError

from v8_next.app.paper import step


@pytest.mark.parametrize(
    "key,value",
    [
        ("maker_fee", "NaN"),
        ("taker_fee", "-1"),
        ("initial_balance", "0"),
        ("max_notional", "Infinity"),
        ("max_exposure_fraction", "1.01"),
        ("enable_live", "true"),
    ],
)
def test_invalid_api_config_cannot_create_run(tmp_path, key, value):
    config = {
        "maker_fee": "0",
        "taker_fee": "0",
        "initial_balance": "10000",
        "max_notional": "100",
        "max_exposure_fraction": "0.1",
        key: value,
    }
    run = tmp_path / "must-not-exist"
    with pytest.raises(ValidationError):
        step(run, config)
    assert not run.exists()


def test_stop_budget_requires_protection_and_explicit_valid_limits():
    from v8_next.domain.config import PaperConfig

    values = dict(
        maker_fee="0",
        taker_fee="0",
        initial_balance="1000",
        max_notional="100",
        max_exposure_fraction=".1",
        stop_budget=dict(risk_fraction=".01", max_heat_fraction=".05", max_concurrency=3),
    )
    with pytest.raises(ValueError, match="protected"):
        PaperConfig.model_validate(values)
    values["campaign_policy"] = "donchian:a:v2"
    policy = PaperConfig.model_validate(values)
    assert policy.stop_budget.max_concurrency == 3
    assert PaperConfig.model_validate_json(policy.model_dump_json()) == policy
    values["stop_budget"]["risk_fraction"] = ".1"
    with pytest.raises(ValueError, match="invalid stop budget"):
        PaperConfig.model_validate(values)
