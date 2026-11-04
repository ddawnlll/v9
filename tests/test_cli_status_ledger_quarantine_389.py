"""#389 — a status dashboard never publishes a certificate from a broken chain.

The defect: ``cmd_status`` verified the ledger, printed ``chain: BROKEN``, and then
rendered the latest certificate anyway with exit code 0. These tests pin the two
claims apart: latest-entry identity is report metadata (not evidence), while the
evidence-chain verdict is authoritative, fail-closed and non-zero on failure.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from v8_next.app import cli as cli_module
from v8_next.app.cli import main as cli_main
from v8_next.evaluation.benchmark_receipt import (
    BenchmarkLedger,
    BenchmarkReceipt,
    GateVector,
)

#: #389: the named distinct exit code. Resolved defensively so this regression test
#: still exercises the *behaviour* on the unfixed tree instead of failing at import.
EXIT_LEDGER_QUARANTINED = getattr(cli_module, "EXIT_LEDGER_QUARANTINED", 3)


def _receipt(case_id: str) -> BenchmarkReceipt:
    """A MECHANICS ONLY receipt: no score, no window claim, no economic claim."""
    return BenchmarkReceipt.create(
        case_id=case_id,
        policy_id="pol_issue_389",
        capability_score=None,
        gates=GateVector(),
        computed_at_timestamp_ns=1_700_000_000_000_000_000,
    )


def _write_ledger(tmp_path: Path, cases: tuple[str, ...] = ("BC-389-ONE", "BC-389-TWO")) -> Path:
    ledger = BenchmarkLedger()
    for case_id in cases:
        ledger.append(_receipt(case_id))
    path = tmp_path / "benchmarks" / "benchmark_ledger.jsonl"
    ledger.save_jsonl(path)
    return path


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _write_rows(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))


def test_valid_genesis_replay_verifies(tmp_path: Path) -> None:
    path = _write_ledger(tmp_path)
    ledger = BenchmarkLedger.load_jsonl(path)

    assert len(ledger) == 2
    first, second = ledger.entries
    assert first.sequence_number == 0
    assert first.parent_entry_hash == BenchmarkLedger.GENESIS_HASH
    assert second.parent_entry_hash == first.entry_hash
    assert ledger.verify_chain() == (True, "OK")
    assert ledger.verify_report().chain_valid is True


def test_tampered_first_entry_is_caught(tmp_path: Path) -> None:
    path = _write_ledger(tmp_path)
    rows = _rows(path)
    rows[0]["receipt"]["case_id"] = "BC-389-TAMPERED"
    _write_rows(path, rows)

    ok, msg = BenchmarkLedger.load_jsonl(path).verify_chain()
    assert ok is False
    assert msg.startswith("ENTRY_RECEIPT_INVALID at 0")
    assert "DIGEST_TAMPERED" in msg


def test_broken_predecessor_digest_is_caught(tmp_path: Path) -> None:
    path = _write_ledger(tmp_path)
    rows = _rows(path)
    rows[1]["parent_entry_hash"] = "f" * 64
    _write_rows(path, rows)

    ok, msg = BenchmarkLedger.load_jsonl(path).verify_chain()
    assert ok is False
    assert msg.startswith("BROKEN_CHAIN: entry 1")


def test_stale_receipt_identity_is_rejected(tmp_path: Path) -> None:
    """A receipt swapped in after the entry hash was sealed is stale, not valid."""
    path = _write_ledger(tmp_path)
    rows = _rows(path)
    replacement = _receipt("BC-389-REPLACEMENT").model_dump(mode="json")
    rows[0]["receipt"] = replacement  # entry_hash still identifies the old receipt
    _write_rows(path, rows)

    ok, msg = BenchmarkLedger.load_jsonl(path).verify_chain()
    assert ok is False
    assert msg == "ENTRY_HASH_TAMPERED at 0"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tampered_first", "BC-389-TAMPERED"),
        ("broken_predecessor", "f" * 64),
    ],
)
def test_cli_quarantines_a_broken_chain_without_rendering_a_certificate(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    field: str,
    value: str,
) -> None:
    path = _write_ledger(tmp_path)
    rows = _rows(path)
    if field == "tampered_first":
        rows[0]["receipt"]["case_id"] = value
    else:
        rows[1]["parent_entry_hash"] = value
    _write_rows(path, rows)

    rc = cli_main(["status", "--output-dir", str(path.parent)])
    out = capsys.readouterr().out

    assert rc == EXIT_LEDGER_QUARANTINED
    assert EXIT_LEDGER_QUARANTINED == 3
    assert "chain: BROKEN" in out
    assert "EVIDENCE CHAIN: QUARANTINED" in out
    assert "the evidence chain is INVALID; no policy certificate is rendered" in out
    assert "LATEST REPORT METADATA (NOT EVIDENCE, NOT VERIFIED):" in out
    assert "V8 EVIDENCE DASHBOARD & POLICY CERTIFICATE" not in out
    assert "READINESS INDEX:" not in out


def test_cli_quarantines_an_empty_ledger(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An empty ledger is not a verified chain: genesis has nothing to replay."""
    path = tmp_path / "benchmarks" / "benchmark_ledger.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("")

    rc = cli_main(["status", "--output-dir", str(path.parent)])
    out = capsys.readouterr().out

    assert rc == EXIT_LEDGER_QUARANTINED
    assert "EMPTY_LEDGER" in out
    assert "EVIDENCE CHAIN: QUARANTINED" in out
    assert "no latest report metadata" in out
    assert "V8 EVIDENCE DASHBOARD & POLICY CERTIFICATE" not in out
