import pytest

from v8_next.app.observe import initialize
from v8_next.app.paper import replay_account
from v8_next.domain.config import PaperConfig
from v8_next.domain.experiment import RulePaperExperiment


def config():
    return dict(
        maker_fee="0",
        taker_fee="0",
        initial_balance="10000",
        max_notional="100",
        max_exposure_fraction=".1",
        campaign_policy="donchian:a:v2",
        experiment_window=[20, 30],
    )


def test_registration_is_before_window_and_restart_does_not_reregister(tmp_path, monkeypatch):
    monkeypatch.setattr("v8_next.app.observe.time.time_ns", lambda: 10)
    frozen = initialize(tmp_path / "registered", config())
    monkeypatch.setattr("v8_next.app.observe.time.time_ns", lambda: 25)
    assert initialize(tmp_path / "registered", config()) == frozen
    with pytest.raises(ValueError, match="frozen before"):
        initialize(tmp_path / "late", config())
    assert not (tmp_path / "late" / "policy.json").exists()


def test_experiment_requires_protection_distinct_authority_and_freeze_time():
    for override in (
        {"campaign_policy": "timeout-only-v1"},
        {"calibration_source_run": "/not-a-certificate"},
        {"experiment_window": [True, 30]},
        {"experiment_window": [30, 20]},
    ):
        with pytest.raises(ValueError):
            PaperConfig.model_validate({**config(), **override})
    with pytest.raises(ValueError, match="freeze time"):
        replay_account([], config())
    with pytest.raises(ValueError, match="frozen before"):
        RulePaperExperiment(20, 20, 30)
