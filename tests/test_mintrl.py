"""MECHANICS ONLY: declared MinTRL identity, closed-form cross-check, refusal."""

import math
from statistics import NormalDist

import pytest
from pydantic import ValidationError

from v8_next.evaluation.mintrl import MinTRLPlan, estimate_min_trl


def _power_plan(**overrides: object) -> MinTRLPlan:
    fields: dict[str, object] = {
        "identity": "POWER_REQUIRED_N",
        "target_sharpe": 1.0,
        "null_sharpe": 0.0,
        "confidence": 0.95,
        "power": 0.8,
        "variance": 1.0,
        "dependence_factor": 1.0,
        "interval_unit": "daily",
        "annualization_factor": 252.0,
        "method_version": "mintrl.v1",
    }
    fields.update(overrides)
    return MinTRLPlan(**fields)  # type: ignore[arg-type]


def _closed_form_plan(**overrides: object) -> MinTRLPlan:
    fields: dict[str, object] = {
        "identity": "BAILEY_LDP_2012",
        "target_sharpe": 0.0,
        "observed_sharpe": 1.0,
        "return_skew": 0.0,
        "return_kurtosis": 3.0,
        "confidence": 0.95,
        "interval_unit": "daily",
        "annualization_factor": 252.0,
        "method_version": "mintrl.bailey-ldp-2012",
    }
    fields.update(overrides)
    return MinTRLPlan(**fields)  # type: ignore[arg-type]


def test_mintrl_requires_declared_plan() -> None:
    result = estimate_min_trl(None, 10)
    assert result.status == "UNSUPPORTED"
    assert result.required_intervals is None


def test_mintrl_requires_the_identity_to_be_declared() -> None:
    with pytest.raises(ValidationError):
        MinTRLPlan(**{**_power_plan().model_dump(), "identity": None})  # type: ignore[arg-type]


def test_mintrl_rejects_inputs_the_declared_identity_does_not_consume() -> None:
    with pytest.raises(ValidationError, match="does not consume"):
        _power_plan(observed_sharpe=1.0)
    with pytest.raises(ValidationError, match="does not consume"):
        _closed_form_plan(power=0.8, variance=1.0, dependence_factor=1.0)
    with pytest.raises(ValidationError, match="requires declared observed_sharpe"):
        _closed_form_plan(observed_sharpe=None)
    with pytest.raises(ValidationError, match="requires declared variance"):
        _power_plan(variance=None)


def test_mintrl_power_identity_is_deterministic_and_dependence_sensitive() -> None:
    base = _power_plan()
    dependent = base.model_copy(update={"dependence_factor": 2.0})
    first = estimate_min_trl(base, 1)
    second = estimate_min_trl(base, 1)
    assert first == second
    assert first.status == "UNDERPOWERED"
    assert first.effective_intervals == 1.0
    assert estimate_min_trl(dependent, 1).required_intervals > first.required_intervals


def test_mintrl_closed_form_matches_the_published_expression() -> None:
    """Cross-check against the Bailey & Lopez de Prado (2012) closed form.

    ``MinTRL = 1 + (1 - g1*SR + ((g2 - 1)/4)*SR^2) * (z / (SR - SR*))^2`` with
    ``SR`` the *observed* Sharpe, ``SR*`` the benchmark and ``g2`` Pearson
    (non-excess, 3 for a Gaussian) kurtosis.  The expected value is recomputed
    here from the expression, not read from a stored constant.
    """

    plan = _closed_form_plan()
    gap = plan.observed_sharpe - plan.target_sharpe
    assert plan.observed_sharpe is not None
    assert plan.return_skew is not None
    assert plan.return_kurtosis is not None
    correction = (
        1.0
        - plan.return_skew * plan.observed_sharpe
        + ((plan.return_kurtosis - 1.0) / 4.0) * plan.observed_sharpe**2
    )
    z = NormalDist().inv_cdf(plan.confidence)
    expected = math.ceil(1.0 + correction * (z / gap) ** 2)
    result = estimate_min_trl(plan, 1)
    assert result.required_intervals == expected
    assert result.status == "UNDERPOWERED"


def test_mintrl_closed_form_uses_the_observed_sharpe_in_the_variance_term() -> None:
    """Discriminates the SR conventions: the correction term is evaluated at SR."""

    plan = _closed_form_plan(
        target_sharpe=0.5, observed_sharpe=2.0, return_skew=0.3, return_kurtosis=5.0
    )
    observed = plan.observed_sharpe
    target = plan.target_sharpe
    assert observed is not None
    z = NormalDist().inv_cdf(plan.confidence)
    gap = observed - target
    at_observed = 1.0 - 0.3 * 2.0 + ((5.0 - 1.0) / 4.0) * 2.0**2
    at_target = 1.0 - 0.3 * 0.5 + ((5.0 - 1.0) / 4.0) * 0.5**2
    required_observed = math.ceil(1.0 + at_observed * (z / gap) ** 2)
    required_target = math.ceil(1.0 + at_target * (z / gap) ** 2)
    assert required_observed != required_target
    assert estimate_min_trl(plan, 1).required_intervals == required_observed


def test_mintrl_closed_form_fails_closed_without_a_positive_gap() -> None:
    plan = _closed_form_plan(observed_sharpe=0.0, target_sharpe=0.0)
    result = estimate_min_trl(plan, 10_000)
    assert result.status == "UNSUPPORTED"
    assert result.required_intervals is None
    assert result.reason == "MINTRL_CLOSED_FORM_UNDEFINED"
    assert result.effective_intervals is None


def test_mintrl_closed_form_has_no_assumed_dependence_model() -> None:
    """The closed form is single-trial; no dependence factor is invented for it."""

    result = estimate_min_trl(_closed_form_plan(), 100)
    assert result.status == "SUPPORTED_HORIZON"
    assert result.effective_intervals is None


def test_mintrl_plan_hash_covers_the_declared_identity() -> None:
    assert _power_plan().plan_hash != _closed_form_plan().plan_hash
