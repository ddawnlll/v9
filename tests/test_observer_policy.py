import pytest

from v8_next.app.observe import initialize
from v8_next.domain.market import CausalFrame
from v8_next.economics.observer_policy import FAMILIES, policy_stances, validate_observer_policy
from v8_next.experts.catalog import observe_all


def test_family_registry_covers_actual_catalog_and_selection_preserves_groups():
    frame = CausalFrame("BTCUSDT-PERP.BINANCE", 0, ())
    all_stances = observe_all(frame, None)
    assert {s.observer_id for s in all_stances} == FAMILIES
    selected = policy_stances(frame, None, "families:divergence-12-setups,pandf-breakout")
    assert len(selected) == 6
    assert len({s.dependency_group for s in selected}) == 2
    assert selected == tuple(
        s for s in all_stances if s.observer_id in {"divergence-12-setups", "pandf-breakout"}
    )


@pytest.mark.parametrize(
    "policy",
    [
        "families:",
        "families:unknown",
        "catalog",
        "families:pandf-breakout,pandf-breakout",
        "families:pandf-breakout,divergence-12-setups",
    ],
)
def test_invalid_or_ambiguous_policy_rejects(policy):
    with pytest.raises(ValueError):
        validate_observer_policy(policy)


def test_execution_selection_is_frozen_before_capture(tmp_path):
    first = initialize(tmp_path, {"observer_policy": "families:donchian-breakout"})
    assert first["policy"]["execution_observer_policy"] == "families:donchian-breakout"
    with pytest.raises(ValueError, match="frozen policy"):
        initialize(tmp_path, {"observer_policy": "squeeze"})
