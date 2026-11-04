import pytest

from v8_next.domain.config import PaperConfig
from v8_next.evaluation.forward_plan import ForwardPlan, freeze_forward_plan
from v8_next.evaluation.store import ResearchStore

HOUR = 3600 * 10**9


def plan():
    policy = PaperConfig(
        maker_fee=".001",
        taker_fee=".001",
        initial_balance="1000",
        max_notional="100",
        max_exposure_fraction=".1",
    )
    return ForwardPlan(
        instrument_id="BTCUSDT-PERP.BINANCE",
        start_ns=10 * HOUR,
        end_ns=20 * HOUR,
        policies={
            "a": policy,
            "b": policy.model_copy(update={"observer_policy": "breakout_baseline"}),
        },
        baseline="a",
        block_size=2,
        reps=99,
        seed=1,
    )


def test_freeze_uses_current_clock_and_is_immutable_after_restart(tmp_path, monkeypatch):
    path = tmp_path / "store.sqlite"
    monkeypatch.setattr("v8_next.evaluation.forward_plan.time.time_ns", lambda: HOUR)
    store = ResearchStore(path)
    digest = freeze_forward_plan(store, "planned", plan(), "code")
    store.close()
    store = ResearchStore(path)
    try:
        monkeypatch.setattr("v8_next.evaluation.forward_plan.time.time_ns", lambda: 11 * HOUR)
        assert freeze_forward_plan(store, "planned", plan(), "code") == digest
        assert store.db.execute("SELECT registered_ns FROM forward_plans").fetchone()[0] == HOUR
        with pytest.raises(ValueError, match="before"):
            freeze_forward_plan(store, "backdated", plan(), "code")
        with pytest.raises(ValueError, match="rewritten"):
            freeze_forward_plan(store, "planned", plan(), "changed-code")
    finally:
        store.close()


def test_invalid_family_and_time_plan_reject():
    payload = plan().model_dump()
    payload["baseline"] = "missing"
    with pytest.raises(ValueError):
        ForwardPlan.model_validate(payload)


def test_forward_warmup_is_frozen_and_requires_complete_source(tmp_path, monkeypatch):
    from decimal import Decimal

    from v8_next.domain.market import Candle
    from v8_next.evaluation.forward_plan import bind_forward_data

    payload = plan().model_dump()
    payload["warmup_bars"] = 5
    planned = ForwardPlan.model_validate(payload)
    monkeypatch.setattr("v8_next.evaluation.forward_plan.time.time_ns", lambda: HOUR)
    store = ResearchStore(tmp_path / "warmup.sqlite")
    try:
        freeze_forward_plan(store, "warm", planned, "code")
        candles = tuple(
            Candle(
                planned.instrument_id,
                i * HOUR,
                (i + 1) * HOUR,
                Decimal(10),
                Decimal(11),
                Decimal(9),
                Decimal(10),
                Decimal(1),
                21 * HOUR,
                None,
                "test-only",
            )
            for i in range(5, 20)
        )
        monkeypatch.setattr("v8_next.evaluation.forward_plan.time.time_ns", lambda: 21 * HOUR)
        with pytest.raises(ValueError, match="incomplete"):
            bind_forward_data(store, "warm", "missing", candles[5:], "code")
        assert bind_forward_data(store, "warm", "complete", candles, "code") == planned
    finally:
        store.close()
    payload = plan().model_dump()
    payload["end_ns"] = payload["start_ns"]
    with pytest.raises(ValueError):
        ForwardPlan.model_validate(payload)


def test_forward_binding_requires_complete_frozen_window_and_cannot_switch_data(
    tmp_path, monkeypatch
):
    from decimal import Decimal

    from v8_next.domain.market import Candle
    from v8_next.evaluation.forward_plan import bind_forward_data

    store = ResearchStore(tmp_path / "store.sqlite")
    monkeypatch.setattr("v8_next.evaluation.forward_plan.time.time_ns", lambda: HOUR)
    freeze_forward_plan(store, "planned", plan(), "code")
    candles = tuple(
        Candle(
            "BTCUSDT-PERP.BINANCE",
            i * HOUR,
            (i + 1) * HOUR,
            Decimal(10),
            Decimal(11),
            Decimal(9),
            Decimal(10),
            Decimal(1),
            21 * HOUR,
            None,
            "fixture",
        )
        for i in range(10, 20)
    )
    try:
        with pytest.raises(ValueError, match="not completed"):
            bind_forward_data(store, "planned", "data", candles, "code")
        monkeypatch.setattr("v8_next.evaluation.forward_plan.time.time_ns", lambda: 21 * HOUR)
        with pytest.raises(ValueError, match="runtime"):
            bind_forward_data(store, "planned", "data", candles, "changed")
        with pytest.raises(ValueError, match="incomplete"):
            bind_forward_data(store, "planned", "data", candles[:-1], "code")
        assert bind_forward_data(store, "planned", "data", candles, "code") == plan()
        assert bind_forward_data(store, "planned", "data", candles, "code") == plan()
        with pytest.raises(ValueError, match="another dataset"):
            bind_forward_data(store, "planned", "different", candles, "code")
    finally:
        store.close()


def test_forward_runner_executes_every_frozen_policy_with_holdout_role(tmp_path, monkeypatch):
    pytest.importorskip("arch", reason="arch not installed (research extra)")
    from decimal import Decimal

    from v8_next.app.forward import run_forward
    from v8_next.domain.market import Candle

    store = ResearchStore(tmp_path / "store.sqlite")
    manifest = tmp_path / "manifest.json"
    manifest.write_text("test-only input")
    monkeypatch.setattr("v8_next.evaluation.forward_plan.time.time_ns", lambda: HOUR)
    freeze_forward_plan(store, "p", plan(), "code")
    candles = tuple(
        Candle(
            "BTCUSDT-PERP.BINANCE",
            i * HOUR,
            (i + 1) * HOUR,
            Decimal(10),
            Decimal(11),
            Decimal(9),
            Decimal(10),
            Decimal(1),
            21 * HOUR,
            None,
            "fixture",
        )
        for i in range(10, 20)
    )
    monkeypatch.setattr("v8_next.app.forward.load_candles", lambda _: candles)
    monkeypatch.setattr("v8_next.app.forward.source_hash", lambda: "code")
    monkeypatch.setattr("v8_next.evaluation.forward_plan.time.time_ns", lambda: 21 * HOUR)
    calls = []

    def trial(manifest, policy, store, family, role, *, selection_start_ns, selection_end_ns):
        assert selection_start_ns == 10 * HOUR
        assert selection_end_ns == 20 * HOUR
        calls.append((policy.observer_policy, family, role))
        scale = int(policy.observer_policy == "breakout_baseline")
        marks = [
            dict(
                end_ns=c.end_ns,
                observed_ns=c.end_ns,
                source_hash="fixture",
                phase="PRE_STRATEGY_BAR_CALLBACK",
                currency="USDT",
                cash="1000",
                unrealized_pnl=str(scale * i * i),
                equity=str(1000 + scale * i * i),
            )
            for i, c in enumerate(candles)
        ]
        return {
            "frozen_policy": {"code_and_lock_hash": "code"},
            "equity_marks": marks,
            "computed_ns": 20 * HOUR + 1,
        }

    monkeypatch.setattr("v8_next.app.forward._run_trial", trial)
    try:
        result = run_forward(manifest, store, "p")
        assert len(calls) == 2
        assert all(c[1:] == ("forward:p", "HOLDOUT") for c in calls)
        assert result["diagnostic"]["sample_intervals"] == 9
        assert result["promotion_eligible"] is False
        assert set(result["trials"]) == {"a", "b"}
    finally:
        store.close()
