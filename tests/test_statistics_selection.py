"""DSR must name the selected variant, not the alphabetically first one."""

from v8_next.evaluation import economic_benchmark as eb
from v8_next.evaluation.statistics_plan import g5_plan


class _Available:
    def for_estimator(self, _name: str) -> None:
        return None


def test_run_statistics_selects_the_best_excess_variant(monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_dsr(losses, *, plan, **kwargs):
        captured["selected"] = plan.selected_variant
        return {
            "verdict": "COMPUTED",
            "selected_variant": plan.selected_variant,
            "dsr_confidence": 0.5,
            "selected_sharpe_nonannualized": 0.1,
        }

    monkeypatch.setattr(eb, "estimator_availability", lambda: _Available())
    monkeypatch.setattr(
        "v8_next.evaluation.deflated_sharpe.deflated_sharpe_diagnostic", fake_dsr
    )
    monkeypatch.setattr(
        "v8_next.evaluation.overfitting.pbo_diagnostic",
        lambda *args, **kwargs: {"verdict": "COMPUTED"},
    )
    monkeypatch.setattr(
        "v8_next.evaluation.inference.spa_diagnostic",
        lambda *args, **kwargs: {"verdict": "COMPUTED", "pvalues": {}},
    )

    curves = {
        "baseline": [10000.0 + (i % 5) for i in range(100)],
        # a_weak sorts first but ends with the lowest excess.
        "a_weak": [10000.0 + (i % 7) for i in range(100)],
        "b_mid": [10000.0 + 0.5 * i + (i % 3) for i in range(100)],
        "z_best": [10000.0 + 2.0 * i + (i % 4) for i in range(100)],
    }
    plan = g5_plan(
        family="selection-test",
        pinned_ns=1,
        block_size=5,
        reps=10,
        seed=1,
        effective_independent_trials=4.0,
    )
    stats = eb.run_statistics(
        curves, [i * 1_000_000 for i in range(100)], "baseline", plan=plan
    )

    assert captured["selected"] == "z_best"
    assert stats["dsr"]["selected_variant"] == "z_best"
