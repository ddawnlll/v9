"""NX01 (#422) — four-year tape identity, burn map and Python data binding.

Evidence classes:

* **mechanics** — burn-table fail-closed behaviour, the tape-role ->
  ``store.DataRole`` mapping, the 24/12/12 calendar's eligibility rule, duplicate
  and intersection accounting in ``load_multitape``, instrument-metadata refusal
  and the manifest round trip. Synthetic JSONL written in ``tmp_path``; no
  evaluation, no tape.
* **evaluative** — the real four-year tape is measured read-only: sha256, rows,
  channels, per-leg coverage, three-way archive verification, funding interval
  distribution (incl. the heterogeneous SOL leg), mark-price absence, the
  measured parity-slice boundary, and the resulting final-window ineligibility.
  Skips when the tape (or the archives) are absent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import get_args

import pytest

from v8_next.adapters.portfolio_backtest import BASE_CURRENCIES, base_currency
from v8_next.evaluation.multitape import load_multitape
from v8_next.evaluation.store import DataRole, ResearchStore
from v8_next.evaluation.tape_identity import (
    HOUR_MS,
    NON_PROTECTED_TAPE_ROLES,
    ROLE_TO_DATA_ROLE,
    BurnSegment,
    RecordedAccess,
    TapeInventory,
    _add_months,
    bind_manifest_to_store,
    build_manifest,
    burn_segments,
    inventory_tape,
    measure_covering_tape,
    policy_lineage_burn_table,
    readback_calendar,
    swing_calendar,
    verify_manifest_against_file,
)

REPO_ROOT = Path("/Users/hootie/src/v8")
TAPE_DIR = REPO_ROOT / "research" / "tape" / "multi-1h-4y"
TAPE = TAPE_DIR / "tape.jsonl"
#: Physical tapes whose window sits inside the last 12 months of the four-year
#: window. Their existence is the measured evidence that the tail was consumed.
TAIL_TAPES = (
    REPO_ROOT / "research" / "tape" / "quad-1h-12m",
    REPO_ROOT / "research" / "tape" / "btcusdt-1h-12m",
    REPO_ROOT / "research" / "tape" / "sol-dev-solusdt-2025-07-2026-07",
)


def _tape_present() -> bool:
    return TAPE.is_file()


# --------------------------------------------------------------------------- #
# mechanics
# --------------------------------------------------------------------------- #
def test_tape_roles_are_not_data_role_values_and_map_one_way() -> None:
    """BURNED_DIAGNOSTIC / USAGE_UNKNOWN are tape roles, not store roles."""
    data_roles = set(get_args(DataRole))
    assert data_roles == {"DEVELOPMENT", "HOLDOUT", "PROSPECTIVE"}
    for role in NON_PROTECTED_TAPE_ROLES:
        assert role not in data_roles
        assert ROLE_TO_DATA_ROLE[role] == "DEVELOPMENT"
    assert ROLE_TO_DATA_ROLE["PROTECTED_OOS"] == "HOLDOUT"


def test_burn_table_refuses_an_unverifiable_citation(tmp_path: Path) -> None:
    """A cited access that is not in the artifact is a failure, not a skip."""
    evidence = tmp_path / "tests" / "parity" / "test_parity_s0.py"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("\n".join(f"line {i}" for i in range(1, 200)) + "\n")
    wrong = RecordedAccess(
        label="fabricated",
        evidence_file="tests/parity/test_parity_s0.py",
        evidence_line=139,
        evidence_contains='load_real_tape("multi-1h-4y", limit=25_000)',
        slice_rows=25_000,
    )
    missing = RecordedAccess(
        label="absent",
        evidence_file="tests/parity/does_not_exist.py",
        evidence_line=1,
        evidence_contains="anything",
        slice_rows=10,
    )
    for access in (wrong, missing):
        with pytest.raises(ValueError, match="unverifiable recorded access"):
            policy_lineage_burn_table(
                repo_root=tmp_path,
                tape_path=tmp_path / "missing.jsonl",
                recorded_accesses=(access,),
            )


def _segment(segment_id: str, role: str, start_ms: int, end_ms: int) -> BurnSegment:
    return BurnSegment(
        segment_id=segment_id,
        role=role,  # type: ignore[arg-type]
        start_ms=start_ms,
        end_ms=end_ms,
        instruments=("BTCUSDT",),
        evidence=("mechanics fixture",),
    )


def _window_around_final(final_start_ms: int) -> tuple[int, int]:
    """A 24/12/12 window whose computed final window starts at ``final_start_ms``."""
    start = _add_months(final_start_ms, -36)
    end = _add_months(final_start_ms, 12)
    return start, end


def test_calendar_is_ineligible_when_the_final_window_is_not_protected() -> None:
    """A burned/unknown final window yields NO_PROTECTED_FINAL, never silence."""
    final_start = 2_000_000_000_000
    start, end = _window_around_final(final_start)
    segments = (
        _segment("HEAD", "BURNED_DIAGNOSTIC", start, final_start - 1),
        _segment("TAIL", "USAGE_UNKNOWN", final_start, end - 1),
    )
    calendar = swing_calendar(inventory=_inventory_stub(start, end), segments=segments)
    assert calendar.final.start_ms == final_start

    assert calendar.final_eligible is False
    assert calendar.no_protected_final is True
    assert calendar.final_ineligibility_reason is not None
    assert calendar.final_ineligibility_reason.startswith("NO_PROTECTED_FINAL")
    assert "TAIL" in calendar.final_ineligibility_reason
    assert len(calendar.folds) == 4
    # folds are chronological, contiguous and disjoint from the final window
    for earlier, later in zip(calendar.folds, calendar.folds[1:], strict=False):
        assert earlier.end_ms + 1 == later.start_ms
    assert calendar.folds[-1].end_ms + 1 == calendar.final.start_ms
    assert calendar.development.end_ms + 1 == calendar.folds[0].start_ms


def test_calendar_is_eligible_only_with_a_protected_final_segment() -> None:
    final_start = 2_000_000_000_000
    start, end = _window_around_final(final_start)
    segments = (
        _segment("DEV", "USAGE_UNKNOWN", start, final_start - 1),
        _segment("FINAL_PROTECTED", "PROTECTED_OOS", final_start, end - 1),
    )
    calendar = swing_calendar(inventory=_inventory_stub(start, end), segments=segments)
    assert calendar.final.start_ms == final_start
    assert calendar.final_eligible is True
    assert calendar.no_protected_final is False
    assert calendar.final_ineligibility_reason is None


def _inventory_stub(start_ms: int, end_ms: int) -> TapeInventory:
    """Minimal inventory for calendar mechanics (no file, no measurement)."""
    return TapeInventory(
        tape_path="mechanics-only",
        tape_sha256="0" * 64,
        tape_bytes=0,
        total_rows=0,
        channel_counts={},
        symbols=("BTCUSDT",),
        interval="1h",
        schema_versions=("mechanics-only",),
        legs=(),
        mark_price_channels=(),
        absence_notes=(),
        unparsable_rows=0,
        unclosed_kline_rows=0,
        window_start_ms=start_ms,
        window_end_ms=end_ms - 1,
    )


def _row(instrument: str, open_ms: int, *, volume: float = 1.0) -> dict[str, object]:
    close_ms = open_ms + HOUR_MS - 1
    return {
        "channel": "kline",
        "instrument": instrument,
        "event_time": close_ms * 1_000_000,
        "payload": {
            "open_time_ms": open_ms,
            "close_time_ms": close_ms,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": volume,
            "quote_asset_volume": volume * 100.0,
            "closed": True,
            "payload_hash": f"{instrument}-{open_ms}",
            "schema_version": "mechanics-only",
        },
    }


def _write_tape(path: Path, rows: list[dict[str, object]]) -> Path:
    target = path / "tape.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return target


def test_loader_refuses_duplicate_bar_slots(tmp_path: Path) -> None:
    """MECHANICS ONLY: a duplicated slot is a hard error, not a silent overwrite."""
    base = 1_700_000_000_000
    rows = [_row("BTCUSDT", base), _row("ETHUSDT", base), _row("BTCUSDT", base)]
    tape = _write_tape(tmp_path, rows)
    with pytest.raises(ValueError, match="duplicated bar slot"):
        load_multitape(tape)


def test_loader_reports_intersection_drops_instead_of_trimming_silently(
    tmp_path: Path,
) -> None:
    """MECHANICS ONLY: a leg that would lose bars to the intersection is reported."""
    base = 1_700_000_000_000
    rows = [
        _row("BTCUSDT", base),
        _row("BTCUSDT", base + HOUR_MS),
        _row("BTCUSDT", base + 2 * HOUR_MS),
        _row("ETHUSDT", base),
        _row("ETHUSDT", base + 2 * HOUR_MS),
    ]
    tape = _write_tape(tmp_path, rows)
    with pytest.raises(ValueError, match="would drop bars"):
        load_multitape(tape)

    lenient = load_multitape(tape, strict_intersection=False)
    assert lenient.intersection_dropped == {"BTCUSDT": 1, "ETHUSDT": 0}
    assert lenient.coverage["ETHUSDT"].missing_vs_union == 1
    assert lenient.n_bars == 2
    assert all(leg.duplicate_slots == 0 for leg in lenient.coverage.values())


def test_loader_records_mark_price_absence_rather_than_zero(tmp_path: Path) -> None:
    """MECHANICS ONLY: a tape without a mark field reports absence."""
    base = 1_700_000_000_000
    tape = _write_tape(tmp_path, [_row("BTCUSDT", base), _row("BTCUSDT", base + HOUR_MS)])
    tape_obj = load_multitape(tape)
    assert tape_obj.mark_price_absent is True
    assert any(note.startswith("MARK_PRICE_ABSENT") for note in tape_obj.absence_notes)
    assert tape_obj.funding_interval_hours_present is True


def test_unknown_symbol_is_refused_instead_of_defaulted_to_btc() -> None:
    """An unmapped instrument is unsupported, not a BTC perpetual."""
    assert base_currency("SOLUSDT") == "SOL"
    assert len(BASE_CURRENCIES) == 10
    with pytest.raises(ValueError, match="refusing to fabricate a base currency"):
        base_currency("FOOUSDT")


def test_calendar_readback_reproduces_the_same_windows(tmp_path: Path) -> None:
    """The serialized calendar re-reads into identical windows (NX03/NX05 input)."""
    final_months_ms = 14 * 30 * 24 * HOUR_MS
    final_start = 2_000_000_000_000
    start = final_start - final_months_ms * 3
    end = final_start + final_months_ms
    segments = (_segment("MID", "USAGE_UNKNOWN", start, end),)
    calendar = swing_calendar(inventory=_inventory_stub(start, end), segments=segments)
    re_read = readback_calendar(calendar, tmp_path / "calendar.json")
    assert re_read.as_dict() == calendar.as_dict()


def test_manifest_identity_is_wall_clock_free(tmp_path: Path) -> None:
    """The same inputs re-derive the same identity digest."""
    final_months_ms = 14 * 30 * 24 * HOUR_MS
    final_start = 2_000_000_000_000
    start = final_start - final_months_ms * 3
    end = final_start + final_months_ms
    inventory = _inventory_stub(start, end)
    calendar = swing_calendar(
        inventory=inventory, segments=(_segment("MID", "USAGE_UNKNOWN", start, end),)
    )
    first = build_manifest(inventory=inventory, calendar=calendar, config_identity="cfg-a")
    second = build_manifest(inventory=inventory, calendar=calendar, config_identity="cfg-a")
    other = build_manifest(inventory=inventory, calendar=calendar, config_identity="cfg-b")
    assert first.identity_digest() == second.identity_digest()
    assert first.identity_digest() != other.identity_digest()


# --------------------------------------------------------------------------- #
# evaluative — real four-year tape (read-only)
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def real_inventory():
    if not _tape_present():
        pytest.skip(f"four-year tape absent at {TAPE}")
    return inventory_tape(TAPE, verify_archive_set=True, archive_dir=TAPE_DIR)


def test_real_tape_identity_and_coverage(real_inventory) -> None:
    inv = real_inventory
    assert inv.tape_sha256 == "b27891e917c38b59aaede1c07fe8316cd0600332389bbefcb667253448a18fa0"
    assert inv.tape_bytes == 226_706_603
    assert inv.total_rows == 394_545
    assert inv.channel_counts == {"kline": 350_640, "funding": 43_905}
    assert len(inv.symbols) == 10
    assert inv.interval == "1h"
    assert inv.unparsable_rows == 0
    assert inv.unclosed_kline_rows == 0
    assert inv.duplicated_slots == 0
    assert inv.internal_gap_slots == 0
    # real UTC window of the four-year dataset
    assert inv.as_dict()["window_start_utc"] == "2022-07-01T00:00:00Z"
    assert inv.as_dict()["window_end_utc"] == "2026-06-30T23:59:59.999000Z"
    for leg in inv.legs:
        assert leg.kline_rows == leg.unique_slots == 35_064
        assert leg.coverage_vs_own_grid == 1.0


def test_real_archive_set_verifies_three_ways(real_inventory) -> None:
    inv = real_inventory
    assert inv.archive_total == 960
    assert inv.archive_matches == 960
    assert inv.registry_sha256 is not None
    bad = [c.zip_name for c in inv.archive_checks if not c.three_way_ok]
    assert bad == []


def test_real_tape_funding_intervals_are_measured_not_assumed(real_inventory) -> None:
    """The heterogeneous SOL funding interval survives; mark price is absent."""
    inv = real_inventory
    sol = next(leg for leg in inv.legs if leg.instrument == "SOLUSDT")
    assert sol.funding_interval_counts == {"2h": 99, "4h": 2, "8h": 4357}
    assert sol.funding_interval_defaulted == 0
    others = [leg for leg in inv.legs if leg.instrument != "SOLUSDT"]
    assert all(leg.funding_interval_counts == {"8h": 4383} for leg in others)
    assert inv.funding_interval_counts()["2h"] == 99
    assert inv.mark_price_channels == ()
    assert any(note.startswith("MARK_PRICE_ABSENT") for note in inv.absence_notes)


def test_measured_head_access_and_tail_burn_invalidate_the_final(real_inventory) -> None:
    """The parity slices and the consumed tail are measured, then bind the calendar."""
    inv = real_inventory
    tail = [measure_covering_tape(p) for p in TAIL_TAPES if (p / "tape.jsonl").is_file()]
    assert tail, "no physical tail tape found; the tail burn cannot be claimed"
    tail_start = min(t.window_start_ms for t in tail)
    assert all(t.window_start_ms >= 1_748_736_000_000 for t in tail)  # 2025-06-01
    verified = policy_lineage_burn_table(repo_root=REPO_ROOT, tape_path=TAPE)
    segments = burn_segments(
        inventory=inv,
        verified_accesses=verified,
        tail_burned_from_ms=tail_start,
        tail_evidence=tuple(f"{t.path} sha256:{t.sha256}" for t in tail),
    )
    calendar = swing_calendar(inventory=inv, segments=segments)
    assert calendar.final_eligible is False
    assert calendar.final_ineligibility_reason is not None
    assert "NO_PROTECTED_FINAL" in calendar.final_ineligibility_reason
    assert calendar.as_dict()["no_protected_final"] is True
    head = next(s for s in segments if s.segment_id.startswith("ACCESS_"))
    assert head.start_ms == 1_656_633_600_000  # 2022-07-01T00:00:00Z
    assert head.end_ms == 1_664_632_799_999  # 2022-10-01T13:59:59.999Z


def test_catalog_consumer_binds_to_the_manifest_and_fails_closed(
    real_inventory, tmp_path: Path
) -> None:
    """R5: a catalog build re-hashes the tape and cannot claim a protected final."""
    from dataclasses import replace as dc_replace

    from v8_next.adapters.catalog_tape import build_catalog

    inv = real_inventory
    tail = [measure_covering_tape(p) for p in TAIL_TAPES if (p / "tape.jsonl").is_file()]
    verified = policy_lineage_burn_table(repo_root=REPO_ROOT, tape_path=TAPE)
    segments = burn_segments(
        inventory=inv,
        verified_accesses=verified,
        tail_burned_from_ms=min(t.window_start_ms for t in tail),
        tail_evidence=tuple(t.path for t in tail),
    )
    calendar = swing_calendar(inventory=inv, segments=segments)
    manifest = build_manifest(inventory=inv, calendar=calendar, config_identity="nx01-catalog")

    tampered = dc_replace(manifest, tape_sha256="0" * 64)
    with pytest.raises(ValueError, match="does not match the physical tape"):
        build_catalog(tmp_path, tape_path=TAPE, limit=5, manifest=tampered)

    with pytest.raises(ValueError, match="refusing to open it"):
        build_catalog(
            tmp_path, tape_path=TAPE, limit=5, manifest=manifest, require_protected_final=True
        )

    build = build_catalog(tmp_path, tape_path=TAPE, limit=5, manifest=manifest)
    contract = build.role_contract
    assert contract is not None
    assert contract["manifest_identity"] == manifest.identity_digest()
    assert contract["final_eligible"] is False
    assert contract["protected_final_claim_refused"] is False
    assert set(contract["roles"]) == {s.segment_id for s in calendar.burn_segments}
    assert "PROTECTED_OOS" not in set(contract["roles"].values())


def test_manifest_binds_to_store_and_fails_closed(real_inventory, tmp_path: Path) -> None:
    inv = real_inventory
    tail = [measure_covering_tape(p) for p in TAIL_TAPES if (p / "tape.jsonl").is_file()]
    verified = policy_lineage_burn_table(repo_root=REPO_ROOT, tape_path=TAPE)
    segments = burn_segments(
        inventory=inv,
        verified_accesses=verified,
        tail_burned_from_ms=min(t.window_start_ms for t in tail),
        tail_evidence=tuple(t.path for t in tail),
    )
    calendar = swing_calendar(inventory=inv, segments=segments)
    manifest = build_manifest(
        inventory=inv, calendar=calendar, config_identity="nx01-test-config"
    )
    ok, reason = verify_manifest_against_file(manifest, TAPE)
    assert ok, reason

    store = ResearchStore(tmp_path / "research.sqlite")
    try:
        report = bind_manifest_to_store(manifest, store, calendar=calendar, lineage="NX01_TEST")
        assert report["dataset_hash"] == inv.data_id
        assert report["final_eligible"] is False
        burns = store.list_burns()
        assert {b.lineage for b in burns} == set(report["burns_registered"])
        # a tampered manifest is refused by the physical read-back
        from dataclasses import replace

        tampered = replace(manifest, tape_sha256="0" * 64)
        ok, reason = verify_manifest_against_file(tampered, TAPE)
        assert not ok and "TAPE_HASH_MISMATCH" in reason
    finally:
        store.close()

pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
