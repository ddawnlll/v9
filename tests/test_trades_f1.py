"""F1 (#400): TradeTick capture + aggressor-aware, trade-driven fill.

Evidence classes in this file:

* **mechanics** — the archive reader's column mapping, window restriction and
  drop accounting on a synthetic archive written into ``tmp_path``. No tape, no
  evaluation, no economic weight.
* **evaluative** — the shared Nautilus account run twice over the SAME real
  1h bar tape, once bar-only and once with real venue aggTrades streamed from
  the public daily archive whose checksum the venue signed. It asserts the fill
  the engine produces is the fill the tape printed. It skips when either the bar
  tape or the archive capture is absent; nothing here is synthesised.

The small tracked fixture (``fixtures/f1-btc-12h``) holds 2000 real REST
aggTrades used for the JSON-page path; it covers 12 bars, which is less than the
ensemble's warmup, so it can carry mechanics and feed-visibility only.
"""

from __future__ import annotations

import json
import zipfile
from decimal import Decimal
from pathlib import Path

import pytest
from nautilus_trader.model import AggressorSide

from v8_next.adapters import binance_capture
from v8_next.adapters.tape_paths import tape_path
from v8_next.adapters.trade_tape import (
    aggressor_side,
    trades_digest,
    trades_from_dump,
    trades_from_pages,
    trades_in_window,
)

FIX = Path(__file__).parent / "fixtures" / "f1-btc-12h"

#: Absolute paths on purpose: a relative default resolves against whatever cwd
#: the suite runs from, which silently turns an evaluative test into a skip.
QUAD_TAPE = tape_path("quad-1h-12m")
BTC_ARCHIVE = tape_path("btcusdt-agg-trades-2025-07")
ETH_ARCHIVE = tape_path("ethusdt-agg-trades-2025-07")
ARCHIVE_INSTRUMENT = "BTCUSDT-PERP.BINANCE"
#: Real clock window of the measured run: covers the BTC entry (07-03 11:00
#: bar-only) and the ETH entry (07-03 15:00) that the tape produces.
TRADE_WINDOW_START_NS = 1_751_536_800_000_000_000  # 2025-07-03T10:00:00Z
TRADE_WINDOW_END_NS = 1_751_565_600_000_000_000  # 2025-07-03T18:00:00Z


# --------------------------------------------------------------------------- #
# mechanics
# --------------------------------------------------------------------------- #
def test_aggressor_mapping_follows_the_venue_flag() -> None:
    assert aggressor_side(True) is AggressorSide.SELL
    assert aggressor_side(False) is AggressorSide.BUY
    assert aggressor_side(None) is None
    assert aggressor_side("m") is None
    assert aggressor_side(1) is None


def test_loader_counts_and_drops_without_guessing() -> None:
    ticks, stats = trades_from_pages(FIX, "BTCUSDT-PERP.BINANCE")
    assert stats["records"] == 2000
    assert stats["loaded"] == 2000
    assert stats["dropped"] == 0
    assert len(ticks) == 2000
    assert {str(t.aggressor_side) for t in ticks} <= {"BUY", "SELL"}


def test_loader_is_deterministic_and_sorted() -> None:
    a, _ = trades_from_pages(FIX, "BTCUSDT-PERP.BINANCE")
    b, _ = trades_from_pages(FIX, "BTCUSDT-PERP.BINANCE")
    assert trades_digest(a) == trades_digest(b)
    times = [t.ts_event for t in a]
    assert times == sorted(times)


def test_window_restriction_extrapolates_nothing() -> None:
    ticks, _ = trades_from_pages(FIX, "BTCUSDT-PERP.BINANCE")
    sub = trades_in_window(ticks, ticks[0].ts_event, ticks[0].ts_event)
    assert all(t.ts_event == ticks[0].ts_event for t in sub)
    assert trades_in_window(ticks, 1, 2) == ()


HEADER = "agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker"
IN_MS, OUT_MS = 1_751_536_800_000, 1_751_590_000_000


def _archive(tmp_path: Path, body: str, *, header: bool, name: str) -> Path:
    """MECHANICS ONLY: a synthetic archive with the venue's column layout."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / name
    text = (HEADER + "\n" if header else "") + body
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(name.replace(".zip", ".csv"), text)
    return path


def test_archive_reader_takes_the_window_out_of_real_venue_rows(tmp_path: Path) -> None:
    body = (
        f"1,100.50,0.500,10,10,{IN_MS},true\n"
        f"2,101.00,0.500,11,11,{OUT_MS},false\n"
        f"3,102.00,0.500,12,12,{IN_MS},maybe\n"
        f"4,103.00,0.500,13,13,not-a-time,false\n"
    )
    _archive(tmp_path, body, header=True, name="SYN-aggTrades-2025-07-03.zip")
    ticks, stats = trades_from_dump(
        tmp_path,
        "SYN-PERP.BINANCE",
        start_ns=IN_MS * 1_000_000,
        end_ns=IN_MS * 1_000_000 + 1,
    )
    assert stats["archives"] == 1 and stats["files"] == 1
    assert stats["rows"] == 4
    assert stats["loaded"] == 1
    # Two rows fall inside the window; one of them carries an unreadable
    # aggressor flag and is dropped rather than recorded with a guessed side.
    assert stats["rows_in_window"] == 2
    assert stats["out_of_window"] == 1
    assert stats["dropped_unknown_aggressor"] == 1
    assert stats["dropped_unparsable"] == 1
    assert str(ticks[0].aggressor_side) == "SELL"
    assert str(ticks[0].price) == "100.50"


def test_archive_reader_accepts_headerless_archives(tmp_path: Path) -> None:
    body = f"7,200.00,0.250,70,70,{IN_MS},false\n"
    _archive(tmp_path, body, header=False, name="SYN-aggTrades-2019-01-01.zip")
    ticks, stats = trades_from_dump(tmp_path, "SYN-PERP.BINANCE", start_ns=0, end_ns=10**19)
    assert stats["rows"] == 1 and stats["loaded"] == 1
    assert str(ticks[0].aggressor_side) == "BUY"


def test_dump_capture_refuses_an_archive_the_venue_did_not_sign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _archive(
        tmp_path, f"1,1.0,1.0,1,1,{IN_MS},true\n", header=True, name="SYN-aggTrades-2025-07-03.zip"
    )
    payload = archive.read_bytes()

    def fake_fetch(url: str, timeout: int = 120) -> bytes:
        if url.endswith(".CHECKSUM"):
            return b"0" * 64 + b"  SYN-aggTrades-2025-07-03.zip\n"
        return payload

    monkeypatch.setattr(binance_capture, "_fetch", fake_fetch)
    with pytest.raises(ValueError, match="checksum mismatch"):
        binance_capture.capture_agg_trades_dump(tmp_path / "capture", "SYN", days=("2025-07-03",))
    assert not (tmp_path / "capture").exists() or not list((tmp_path / "capture").glob("*.zip"))


def test_dump_capture_rejects_a_malformed_day(tmp_path: Path) -> None:
    for bad in ("2025-7-3", "20250703", "2025-07-32"):
        with pytest.raises(ValueError):
            binance_capture.capture_agg_trades_dump(
                tmp_path / f"capture-{bad}", "BTCUSDT", days=(bad,)
            )


# --------------------------------------------------------------------------- #
# feed visibility on the tracked real fixture
# --------------------------------------------------------------------------- #
def _candles():
    from v8_next.domain.market import Candle

    rows = json.loads((FIX / "klines.json").read_text())
    out = []
    for k in rows:
        # [openTime, o, h, l, c, volume, closeTime, ...]
        out.append(
            Candle(
                "BTCUSDT-PERP.BINANCE",
                int(k[0]) * 1_000_000,
                (int(k[6]) + 1) * 1_000_000,
                Decimal(str(k[1])),
                Decimal(str(k[2])),
                Decimal(str(k[3])),
                Decimal(str(k[4])),
                Decimal(str(k[5])),
                (int(k[6]) + 1) * 1_000_000,
                (int(k[6]) + 1) * 1_000_000,
                "f1-real-klines",
            )
        )
    return tuple(out)


def _run_fixture(trades):
    from v8_next.adapters.portfolio_backtest import SleeveSpec, run_portfolio_backtest

    return run_portfolio_backtest(
        {"BTCUSDT": _candles()},
        (SleeveSpec("incumbent", 1, 28, 1.0),),
        (),
        per_leg_notional=Decimal("1000"),
        taker_fee=Decimal("0.0005"),
        initial_balance=Decimal("10000"),
        execution_profile="realistic",
        trades=trades,
    )


def test_fixture_trade_feed_reaches_the_engine() -> None:
    ticks, _ = trades_from_pages(FIX, "BTCUSDT-PERP.BINANCE")
    with_trades = _run_fixture(ticks)
    without = _run_fixture(())
    assert with_trades["trades_fed"] == len(ticks) == 2000
    assert without["trades_fed"] == 0
    # 12 bars is shorter than the ensemble's warmup: this window carries feed
    # plumbing only, which is exactly what is asserted here.
    assert with_trades["execution"]["fill_signature"] is not None


# --------------------------------------------------------------------------- #
# evaluative: real bars + real venue trades
# --------------------------------------------------------------------------- #
def _archive_ticks() -> tuple[tuple, dict]:
    if not (QUAD_TAPE / "tape.jsonl").exists():
        pytest.skip(f"real bar tape absent at {QUAD_TAPE}")
    if not list(BTC_ARCHIVE.glob("*-aggTrades-*.zip")):
        pytest.skip(f"aggTrades archive capture absent at {BTC_ARCHIVE}")
    btc, stats = trades_from_dump(
        BTC_ARCHIVE,
        ARCHIVE_INSTRUMENT,
        start_ns=TRADE_WINDOW_START_NS,
        end_ns=TRADE_WINDOW_END_NS,
    )
    ticks = list(btc)
    if list(ETH_ARCHIVE.glob("*-aggTrades-*.zip")):
        eth, eth_stats = trades_from_dump(
            ETH_ARCHIVE,
            "ETHUSDT-PERP.BINANCE",
            start_ns=TRADE_WINDOW_START_NS,
            end_ns=TRADE_WINDOW_END_NS,
        )
        ticks.extend(eth)
        stats = {f"btc_{k}": v for k, v in stats.items()}
        stats.update({f"eth_{k}": v for k, v in eth_stats.items()})
    if not ticks:
        pytest.skip("archive capture holds no trades in the measured window")
    return tuple(ticks), stats


def _run_tape(profile: str, trades):
    from v8_next.adapters.portfolio_backtest import SleeveSpec, run_portfolio_backtest
    from v8_next.evaluation.multitape import load_multitape

    tape = load_multitape(QUAD_TAPE, limit=120)
    return run_portfolio_backtest(
        tape.candles,
        (SleeveSpec("incumbent", 1, 28, 1.0),),
        tape.funding,
        per_leg_notional=Decimal("1000"),
        taker_fee=Decimal("0.0005"),
        initial_balance=Decimal("10000"),
        funding_dropped=tape.funding_dropped,
        execution_profile=profile,
        trades=trades,
    )


def test_real_trades_make_the_fill_aggressor_driven_and_deterministic() -> None:
    ticks, stats = _archive_ticks()
    bar_only = _run_tape("realistic", ())
    first = _run_tape("realistic", ticks)
    second = _run_tape("realistic", ticks)

    # 1. the feed reached the engine and the run repeated bit-identically.
    assert first["trades_fed"] == len(ticks) == second["trades_fed"] > 0
    assert first["execution"]["fill_signature"] == second["execution"]["fill_signature"]
    assert first["opened_positions"] == second["opened_positions"]

    # 2. both runs filled; bar-only fills exist on this window (5 fills, 3 opens).
    assert bar_only["execution"]["fills_count"] > 0
    assert first["execution"]["fills_count"] > 0

    # 3. the trade feed changes WHERE and AT WHAT PRICE the entry fills: the
    #    bar-only entry is stamped on the bar boundary, the trade-driven entry on
    #    a real printed trade inside the bar.
    hour_ns = 3_600 * 10**9
    bar_opens = [p for p in bar_only["opened_positions"] if p["event_ns"] % hour_ns == 0]
    trade_opens = [p for p in first["opened_positions"] if p["event_ns"] % hour_ns != 0]
    assert bar_opens and trade_opens
    assert bar_only["execution"]["fill_signature"] != first["execution"]["fill_signature"]

    by_time = {(str(t.instrument_id), t.ts_event): str(t.price) for t in ticks}
    for position in trade_opens:
        printed = by_time[(str(position["instrument_id"]), position["event_ns"])]
        fill_px = Decimal(str(position["avg_px_open"]))
        # `realistic` carries OneTickSlippageFillModel, so the fill is the printed
        # price plus at most one price increment (0.01) plus float rendering.
        assert abs(fill_px - Decimal(printed)) <= Decimal("0.03"), (position, printed)

    # 4. the measured implementation shortfall of the same window moves, and is
    #    published with its sample counts rather than asserted in prose.
    report = {
        "trades_fed": len(ticks),
        "archive_stats": stats,
        "bar_only": {
            "fills": bar_only["execution"]["fills_count"],
            "signature": bar_only["execution"]["fill_signature"],
            "slippage_bps_mean": bar_only["execution"]["slippage_bps_mean"],
            "slippage_samples": bar_only["execution"]["slippage_samples"],
            "opened": bar_only["opened_positions"],
        },
        "bar_plus_trades": {
            "fills": first["execution"]["fills_count"],
            "signature": first["execution"]["fill_signature"],
            "slippage_bps_mean": first["execution"]["slippage_bps_mean"],
            "slippage_samples": first["execution"]["slippage_samples"],
            "opened": first["opened_positions"],
        },
    }
    print("\n[F1] " + json.dumps(report, default=str))
    assert first["execution"]["slippage_samples"] > 0
    assert bar_only["execution"]["slippage_samples"] > 0
    assert first["execution"]["slippage_bps_mean"] != bar_only["execution"]["slippage_bps_mean"]

pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
