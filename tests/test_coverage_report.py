"""M4-04 (#32): point-in-time universe coverage and regime qualification tests.

Synthetic tapes exercise the reporting arithmetic and the negative controls. The
real in-repo tape test asserts the *documented* development slice (one BTCUSDT
symbol, NOT_RUN altcoin universe) and never claims an economic result.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from v8_next.adapters.universe import InstrumentMetadata
from v8_next.evaluation.coverage import (
    CoverageError,
    DatasetRole,
    DataStatus,
    Regime,
    RegimeConclusion,
    TapeIntegrityError,
    UniverseStatus,
    build_coverage_report,
    main,
)

HOUR_MS = 3_600_000
HOUR_NS = 3_600 * 1_000_000_000
START_MS = 1_735_689_600_000  # 2025-01-01T00:00:00Z
REAL_TAPE = Path(__file__).resolve().parents[1] / "research/tape/btcusdt-1h-2025Q1"


def _kline(symbol: str, index: int, close: str = "100") -> dict[str, object]:
    open_ms = START_MS + index * HOUR_MS
    return {
        "channel": "kline",
        "event_time": open_ms + HOUR_MS - 1,
        "instrument": symbol,
        "payload": {
            "open_time_ms": open_ms,
            "close": close,
            "closed": True,
            "schema_version": "v9.tape.1",
        },
    }


def _funding(symbol: str, index: int, *, interval_hours: float | None = 8.0) -> dict[str, object]:
    stamp = START_MS + index * HOUR_MS + 15
    payload: dict[str, object] = {
        "funding_time_ms": stamp,
        "funding_rate": "0.00010000",
        "schema_version": "v9.tape.1",
    }
    if interval_hours is not None:
        payload["funding_interval_hours"] = interval_hours
    return {
        "channel": "funding",
        "event_time": stamp,
        "instrument": symbol,
        "payload": payload,
    }


def _mark(symbol: str, index: int) -> dict[str, object]:
    stamp = START_MS + index * HOUR_MS
    return {
        "channel": "mark",
        "event_time": stamp,
        "instrument": symbol,
        "payload": {"mark_price": "100", "schema_version": "v9.tape.1"},
    }


def _write_tape(tmp_path: Path, records: list[dict[str, object]], *, name: str = "tape") -> Path:
    directory = tmp_path / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "tape.jsonl").write_text(
        "\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n",
        encoding="utf-8",
    )
    return directory


def _report(
    tmp_path: Path,
    records: list[dict[str, object]],
    *,
    roles: dict[str, object] | None = None,
    expected_interval_ns: int = HOUR_NS,
    **kwargs: object,
):
    directory = _write_tape(tmp_path, records)
    return build_coverage_report(
        {"tape": directory},
        expected_interval_ns=expected_interval_ns,
        roles=roles or {"*": DatasetRole.DEVELOPMENT},
        **kwargs,
    )


def _metadata(symbol: str, *, delisted_at: int | None, available_at: int = 0) -> InstrumentMetadata:
    return InstrumentMetadata.model_validate(
        {
            "symbol": symbol,
            "venue": "BINANCE",
            "contract_type": "PERPETUAL",
            "listed_at": 0,
            "delisted_at": delisted_at,
            "tick_size": Decimal("0.01"),
            "step_size": Decimal("0.001"),
            "min_notional": Decimal("5"),
            "min_qty": Decimal("0.001"),
            "leverage_max": Decimal("20"),
            "maintenance_margin_rate": Decimal("0.01"),
            "metadata_version": "meta-v1",
            "available_at": available_at,
        }
    )


# --------------------------------------------------------------------------- #
# gap / integrity
# --------------------------------------------------------------------------- #
def test_gap_detection_counts_missing_bars_and_names_the_gap(tmp_path: Path) -> None:
    records = [_kline("ETHUSDT", index) for index in (0, 1, 2, 5, 6)]
    report = _report(tmp_path, records, roles={"ETHUSDT": DatasetRole.VALIDATION})
    row = report.symbols[0]

    assert row.symbol == "ETHUSDT"
    assert row.role is DatasetRole.VALIDATION
    assert row.bar_count == 5
    assert row.expected_bars == 7
    assert row.missing_bar_ratio == Decimal("0.285714")
    assert any(gap.startswith("BARS:gap:") and "missing=2" in gap for gap in row.gaps)
    assert row.status is DataStatus.PARTIAL
    assert report.data_status is DataStatus.PARTIAL


def test_duplicate_timestamp_is_rejected(tmp_path: Path) -> None:
    records = [_kline("ETHUSDT", index) for index in (0, 1, 1)]

    with pytest.raises(TapeIntegrityError) as raised:
        _report(tmp_path, records)

    assert raised.value.code == "DUPLICATE_TIMESTAMP"


def test_out_of_order_rows_are_rejected(tmp_path: Path) -> None:
    records = [_kline("ETHUSDT", index) for index in (0, 2, 1)]

    with pytest.raises(TapeIntegrityError) as raised:
        _report(tmp_path, records)

    assert raised.value.code == "OUT_OF_ORDER_ROWS"


def test_off_grid_timestamp_is_rejected(tmp_path: Path) -> None:
    records = [_kline("ETHUSDT", 0), _kline("ETHUSDT", 1), _kline("ETHUSDT", 3)]
    records.append(
        {
            "channel": "kline",
            "event_time": START_MS + 4 * HOUR_MS,
            "instrument": "ETHUSDT",
            "payload": {
                "open_time_ms": START_MS + 3 * HOUR_MS + 60_000,
                "close": "100",
                "closed": True,
            },
        }
    )

    with pytest.raises(TapeIntegrityError) as raised:
        _report(tmp_path, records)

    assert raised.value.code == "NON_ALIGNED_TIMESTAMP"


# --------------------------------------------------------------------------- #
# delisting boundary / survivorship
# --------------------------------------------------------------------------- #
def test_delisting_boundary_is_reported_and_survivorship_excluded(tmp_path: Path) -> None:
    records = [_kline("ALTUSDT", index) for index in range(5)]
    delisted_at = START_MS * 1_000_000 + 5 * HOUR_NS
    report = _report(
        tmp_path,
        records,
        roles={"ALTUSDT": DatasetRole.VALIDATION},
        as_of_ns=delisted_at,
        metadata={"ALTUSDT": [_metadata("ALTUSDT", delisted_at=delisted_at)]},
    )
    row = report.symbols[0]

    assert row.delisted_at == delisted_at
    assert row.last_ts is not None and row.last_ts < delisted_at
    assert row.survivorship_risk is False
    assert row.survivorship_reason is None
    assert "meta-v1" == row.metadata_versions["instrument"]
    assert report.aggregate.delisted_symbols == ("ALTUSDT",)


def test_bars_after_delisting_are_blocked(tmp_path: Path) -> None:
    records = [_kline("ALTUSDT", index) for index in range(6)]
    delisted_at = START_MS * 1_000_000 + 5 * HOUR_NS
    report = _report(
        tmp_path,
        records,
        roles={"ALTUSDT": DatasetRole.VALIDATION},
        as_of_ns=delisted_at + HOUR_NS,
        metadata={"ALTUSDT": [_metadata("ALTUSDT", delisted_at=delisted_at)]},
    )
    row = report.symbols[0]

    assert row.status is DataStatus.BLOCKED
    assert row.last_ts is not None and row.last_ts >= delisted_at
    assert report.data_status is DataStatus.BLOCKED
    assert any("BARS_AFTER_DELISTING" in blocker for blocker in report.blockers)
    assert any(gap.startswith("BARS:after_delisting") for gap in row.gaps)


def test_metadata_absent_is_a_named_survivorship_risk(tmp_path: Path) -> None:
    report = _report(tmp_path, [_kline("ALTUSDT", index) for index in range(4)])
    row = report.symbols[0]

    assert row.survivorship_risk is True
    assert row.survivorship_reason == "NO_DELISTING_METADATA"
    assert report.aggregate.survivorship_risk is True


# --------------------------------------------------------------------------- #
# funding / mark absence
# --------------------------------------------------------------------------- #
def test_missing_funding_and_mark_are_partial_with_named_gaps(tmp_path: Path) -> None:
    records = [_kline("ETHUSDT", index) for index in range(5)]
    report = _report(tmp_path, records, roles={"ETHUSDT": DatasetRole.DEVELOPMENT})
    row = report.symbols[0]

    assert row.status is DataStatus.PARTIAL
    assert row.funding_rows == 0
    assert row.mark_rows == 0
    assert row.expected_mark_rows == 5
    assert row.expected_funding_rows == 1
    assert any(gap.startswith("FUNDING:absent") for gap in row.gaps)
    assert any(gap.startswith("MARK:absent") for gap in row.gaps)


def test_complete_fixture_is_available(tmp_path: Path) -> None:
    records: list[dict[str, object]] = []
    for index in range(5):
        records.extend([_kline("ETHUSDT", index), _mark("ETHUSDT", index)])
    records.append(_funding("ETHUSDT", 0))
    report = _report(tmp_path, records, roles={"ETHUSDT": DatasetRole.DEVELOPMENT})
    row = report.symbols[0]

    assert row.status is DataStatus.AVAILABLE
    assert row.gaps == ()
    assert row.funding_rows == 1
    assert row.mark_rows == 5
    assert report.data_status is DataStatus.AVAILABLE


# --------------------------------------------------------------------------- #
# point-in-time as-of
# --------------------------------------------------------------------------- #
def test_as_of_excludes_later_rows_and_reports_them(tmp_path: Path) -> None:
    records = [_kline("ETHUSDT", index) for index in range(6)]
    as_of_ns = START_MS * 1_000_000 + 3 * HOUR_NS
    report = _report(tmp_path, records, as_of_ns=as_of_ns)
    row = report.symbols[0]

    assert row.bar_count == 4
    assert row.last_ts == as_of_ns
    assert row.rows_after_as_of == 2
    assert report.aggregate.rows_after_as_of == 2
    assert "ROWS:after_as_of=2" in row.gaps


# --------------------------------------------------------------------------- #
# universe status / regimes
# --------------------------------------------------------------------------- #
def test_real_tape_reports_one_development_symbol_and_not_run_universe() -> None:
    manifest = json.loads((REAL_TAPE / "tape.manifest.json").read_text(encoding="utf-8"))
    report = build_coverage_report(
        {"btcusdt-1h-2025Q1": REAL_TAPE},
        expected_interval_ns=HOUR_NS,
        roles={"*": DatasetRole.DEVELOPMENT},
    )

    assert len(report.symbols) == 1
    row = report.symbols[0]
    assert row.symbol == "BTCUSDT"
    assert row.role is DatasetRole.DEVELOPMENT
    assert row.bar_count == 2160
    assert row.expected_bars == 2160
    assert row.missing_bar_ratio == Decimal(0)
    assert row.funding_rows == 270
    assert row.source_sha256 == manifest["tape_sha256"]
    assert row.source_sha256 == hashlib.sha256((REAL_TAPE / "tape.jsonl").read_bytes()).hexdigest()
    assert row.status is DataStatus.PARTIAL  # mark history is absent from this tape
    assert any(gap.startswith("MARK:absent") for gap in row.gaps)

    assert report.universe_status is UniverseStatus.NOT_RUN
    assert report.altcoin_symbols == ()
    assert len(report.blockers) == 1
    assert "M4-04:NO_ALTCOIN_TAPE_IN_REPO" in report.blockers[0]
    assert "universe_status=NOT_RUN" in report.blockers[0]
    assert "BTCUSDT" in report.blockers[0]
    assert report.aggregate.by_role[0].role is DatasetRole.DEVELOPMENT
    assert report.aggregate.by_role[0].symbols == 1


def test_regime_block_reports_counts_without_an_economic_result() -> None:
    report = build_coverage_report(
        {"btcusdt-1h-2025Q1": REAL_TAPE},
        expected_interval_ns=HOUR_NS,
        roles={"*": DatasetRole.DEVELOPMENT},
    )
    regimes = report.regimes

    assert regimes.conclusion is RegimeConclusion.NOT_RUN
    assert regimes.economic_result == "NOT_RUN"
    assert regimes.classified_bars + regimes.warmup_bars == 2160
    assert regimes.classified_bars == 2136
    assert sum(count.bars for count in regimes.regimes) == 2136
    assert [count.regime for count in regimes.regimes] == [
        Regime.TREND_UP,
        Regime.TREND_DOWN,
        Regime.RANGE,
        Regime.HIGH_VOL,
    ]
    for count in regimes.regimes:
        expected_share = (Decimal(count.bars) / Decimal(2136)).quantize(Decimal("0.000001"))
        assert count.share == expected_share
    assert "no confidence interval" in regimes.uncertainty
    assert "no economic result" in regimes.conclusion_reason
    assert not hasattr(RegimeConclusion, "PASS")


def test_regime_outcomes_require_an_economic_result(tmp_path: Path) -> None:
    closes = [str(100 + index) for index in range(30)]
    records = [_kline("ETHUSDT", index, close) for index, close in enumerate(closes)]
    outcomes = {regime: RegimeConclusion.NO_EDGE for regime in Regime}
    report = _report(tmp_path, records, regime_outcomes=outcomes)

    assert report.regimes.classified_bars == 6
    assert report.regimes.conclusion is RegimeConclusion.NO_EDGE
    assert report.regimes.economic_result == "PROVIDED"

    # Fewer than one window of bars cannot yield a decisive regime conclusion,
    # even when a caller supplies outcomes.
    short = _write_tape(tmp_path, [_kline("ETHUSDT", index) for index in range(8)], name="short")
    short_report = build_coverage_report(
        {"short": short},
        expected_interval_ns=HOUR_NS,
        roles={"*": DatasetRole.DEVELOPMENT},
        regime_outcomes=outcomes,
    )
    assert short_report.regimes.classified_bars == 0
    assert short_report.regimes.conclusion is RegimeConclusion.NOT_RUN


def test_altcoin_tape_marks_universe_provisioned_and_flags_survivorship(tmp_path: Path) -> None:
    records = [_kline("ETHUSDT", index) for index in range(4)]
    report = _report(tmp_path, records, roles={"ETHUSDT": DatasetRole.VALIDATION})

    assert report.universe_status is UniverseStatus.PROVISIONED
    assert report.altcoin_symbols == ("ETHUSDT",)
    assert any(blocker.startswith("SURVIVORSHIP:NO_DELISTING_METADATA") for blocker in report.blockers)


# --------------------------------------------------------------------------- #
# determinism / misc contracts
# --------------------------------------------------------------------------- #
def test_report_json_is_deterministic_and_digest_matches() -> None:
    first = build_coverage_report(
        {"btcusdt-1h-2025Q1": REAL_TAPE},
        expected_interval_ns=HOUR_NS,
        roles={"*": DatasetRole.DEVELOPMENT},
    )
    second = build_coverage_report(
        {"btcusdt-1h-2025Q1": REAL_TAPE},
        expected_interval_ns=HOUR_NS,
        roles={"*": DatasetRole.DEVELOPMENT},
    )

    assert first.to_json() == second.to_json()
    assert first.digest() == second.digest()
    document = json.loads(first.to_json())
    assert document["report_digest"] == first.digest()
    assert document["symbols"][0]["source_sha256"] == first.symbols[0].source_sha256
    assert document["regimes"]["conclusion"] == "NOT_RUN"


def test_no_tape_is_blocked_with_a_named_reason() -> None:
    report = build_coverage_report(
        {}, expected_interval_ns=HOUR_NS, roles={"*": DatasetRole.DEVELOPMENT}
    )

    assert report.data_status is DataStatus.BLOCKED
    assert report.symbols == ()
    assert "NO_TAPE_SUPPLIED" in report.blockers
    assert report.universe_status is UniverseStatus.NOT_RUN
    assert report.regimes.classified_bars == 0
    assert report.regimes.conclusion is RegimeConclusion.NOT_RUN


def test_symbol_without_a_role_is_refused(tmp_path: Path) -> None:
    directory = _write_tape(tmp_path, [_kline("ETHUSDT", 0)])
    with pytest.raises(CoverageError) as raised:
        build_coverage_report(
            {"tape": directory},
            expected_interval_ns=HOUR_NS,
            roles={"BTCUSDT": DatasetRole.DEVELOPMENT},
        )
    assert raised.value.code == "ROLE_NOT_ASSIGNED"


def test_cli_writes_deterministic_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "coverage.json"
    exit_code = main(
        [
            "--tape",
            str(REAL_TAPE),
            "--interval-seconds",
            "3600",
            "--json",
            "--out",
            str(out),
        ]
    )

    assert exit_code == 0
    printed = capsys.readouterr().out.strip()
    assert printed == out.read_text(encoding="utf-8").strip()
    document = json.loads(printed)
    assert document["universe_status"] == "NOT_RUN"
    assert document["symbols"][0]["symbol"] == "BTCUSDT"
    assert document["report_digest"]
