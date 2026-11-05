"""Focused tests for the cross-sectional screen's construction.

The screen's whole claim rests on two invariants: every event selects the weakest
longs and strongest shorts from information strictly before the entry bar, and
every event carries equal long and short legs. Those are asserted here on a
synthetic panel where the ranking is known by construction.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from v8_next.research import screen

TOOL = Path(__file__).resolve().parents[1] / "tools" / "cross_sectional_screen.py"
_spec = importlib.util.spec_from_file_location("cross_sectional_screen", TOOL)
assert _spec is not None and _spec.loader is not None
xs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(xs)


def _panel(paths: dict[str, list[float]], bars: int = 80) -> dict[str, list[screen.Bar]]:
    hour_ns = 3_600_000_000_000
    panel: dict[str, list[screen.Bar]] = {}
    for symbol, returns in paths.items():
        price = 100.0
        series: list[screen.Bar] = []
        for index in range(bars):
            step = returns[index % len(returns)]
            open_ = price
            price = price * (1.0 + step)
            series.append(
                screen.Bar(
                    end_ns=(index + 1) * hour_ns,
                    open=open_,
                    high=max(open_, price) * 1.001,
                    low=min(open_, price) * 0.999,
                    close=price,
                    volume=1.0,
                )
            )
        panel[symbol] = series
    return panel


def test_events_buy_the_weakest_and_sell_the_strongest_symbols() -> None:
    # AAA falls every bar, BBB flat, CCC rises every bar: ranks are unambiguous.
    panel = _panel({"AAA": [-0.05], "BBB": [0.0], "CCC": [0.05]})
    events = xs.selection_events(panel, symbols=("AAA", "BBB", "CCC"), lookback=4, legs=1)
    assert events, "a known trend must produce at least one rank entry"
    first = events[0]
    assert first["symbol"] == "AAA" and first["direction"] == 1
    assert all(
        (e["symbol"] == "AAA") == (e["direction"] == 1) for e in events
    ), "the down symbol is always the long and the up symbol always the short"


def test_each_event_carries_equal_long_and_short_legs() -> None:
    panel = _panel({"A": [0.03], "B": [0.01], "C": [-0.01], "D": [-0.03]})
    events = xs.selection_events(panel, symbols=("A", "B", "C", "D"), lookback=4, legs=1)
    per_bar: dict[int, dict[int, int]] = {}
    for event in events:
        counts = per_bar.setdefault(event["bar_index"], {1: 0, -1: 0})
        counts[event["direction"]] += 1
    assert per_bar
    for counts in per_bar.values():
        assert counts[1] == counts[-1] == 1


def test_selection_uses_only_bars_before_the_entry_bar() -> None:
    panel = _panel({"A": [0.03], "B": [0.01], "C": [-0.01], "D": [-0.03]})
    baseline = xs.selection_events(panel, symbols=("A", "B", "C", "D"), lookback=4, legs=1)
    last = panel["A"][-1]
    panel["A"][-1] = screen.Bar(
        end_ns=last.end_ns,
        open=last.open,
        high=last.high * 10,
        low=last.low / 10,
        close=last.close * 10,
        volume=last.volume,
    )
    mutated = xs.selection_events(panel, symbols=("A", "B", "C", "D"), lookback=4, legs=1)
    cutoff = panel["A"][-2].end_ns
    assert [e for e in baseline if panel["A"][e["bar_index"]].end_ns <= cutoff] == [
        e for e in mutated if panel["A"][e["bar_index"]].end_ns <= cutoff
    ]


def test_misaligned_grid_is_refused(tmp_path, monkeypatch) -> None:
    # Two tapes with different bar counts must fail loudly, not rank misaligned rows.
    tape_a = tmp_path / "aaausdt-1h-2025H1"
    tape_b = tmp_path / "bbbusdt-1h-2025H1"
    tape_a.mkdir()
    tape_b.mkdir()

    def row(index: int, symbol: str) -> str:
        return (
            '{"channel":"kline","instrument":"%s","event_time":%d,'
            '"payload":{"open":100,"high":101,"low":99,"close":100,"volume":1,'
            '"open_time_ms":%d,"closed":true}}'
            % (symbol, (index + 1) * 3_600_000, index * 3_600_000)
        )

    (tape_a / "tape.jsonl").write_text(
        "\n".join(row(i, "AAAUSDT") for i in range(5)), encoding="utf-8"
    )
    (tape_b / "tape.jsonl").write_text(
        "\n".join(row(i, "BBBUSDT") for i in range(4)), encoding="utf-8"
    )
    monkeypatch.setattr(xs, "TAPE_DIR", tmp_path)

    with pytest.raises(RuntimeError, match="share one bar grid"):
        xs.load_panel(("AAAUSDT", "BBBUSDT"), start_ms=0, end_ms=10**13)


def test_momentum_mode_is_the_exact_opposite_ranking() -> None:
    panel = _panel({"AAA": [-0.05], "BBB": [0.0], "CCC": [0.05]})
    reversal = xs.selection_events(
        panel, symbols=("AAA", "BBB", "CCC"), lookback=4, legs=1, mode="reversal"
    )
    momentum = xs.selection_events(
        panel, symbols=("AAA", "BBB", "CCC"), lookback=4, legs=1, mode="momentum"
    )
    assert reversal and momentum
    assert {(e["bar_index"], e["symbol"]): e["direction"] for e in reversal} == {
        (e["bar_index"], e["symbol"]): -e["direction"] for e in momentum
    }


def test_unknown_rank_mode_is_refused() -> None:
    panel = _panel({"AAA": [-0.05], "BBB": [0.05]})
    with pytest.raises(ValueError, match="unknown rank mode"):
        xs.selection_events(panel, symbols=("AAA", "BBB"), lookback=4, legs=1, mode="carry")


def test_flow_ranking_buys_the_strongest_aggressive_flow() -> None:
    panel = _panel({"AAA": [0.0], "BBB": [0.0], "CCC": [0.0]}, bars=20)
    shares = {"AAA": 0.70, "BBB": 0.50, "CCC": 0.30}
    flow = {
        symbol: {bar.end_ns: {"buy_share": shares[symbol], "trades": 1.0, "quote_volume": 1.0}
                 for bar in series}
        for symbol, series in panel.items()
    }
    events = xs.selection_events(
        panel, symbols=("AAA", "BBB", "CCC"), lookback=1, legs=1, mode="flow", flow=flow
    )
    assert events
    assert {(e["symbol"], e["direction"]) for e in events} == {("AAA", 1), ("CCC", -1)}


def test_load_flow_skips_bars_without_a_quote_denominator(tmp_path) -> None:
    tape = tmp_path / "tape.jsonl"

    def row(open_ms: int, quote: float, buy: float) -> str:
        return (
            '{"channel":"kline","instrument":"BTCUSDT","event_time":%d,'
            '"payload":{"open":100,"high":101,"low":99,"close":100,"volume":1,'
            '"quote_asset_volume":%f,"taker_buy_quote_asset_volume":%f,'
            '"open_time_ms":%d,"closed":true}}' % (open_ms + 3_600_000, quote, buy, open_ms)
        )

    tape.write_text(
        "\n".join([row(0, 1000.0, 600.0), row(3_600_000, 0.0, 0.0)]), encoding="utf-8"
    )
    flow = screen.load_flow(tape, instrument="BTCUSDT")
    assert list(flow) == [3_600_000 * 1_000_000]
    assert flow[3_600_000 * 1_000_000]["buy_share"] == pytest.approx(0.6)
