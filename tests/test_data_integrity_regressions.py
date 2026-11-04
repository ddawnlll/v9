"""MECHANICS ONLY: input validation and artifact safety; no economic evidence."""

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from v8_next.evaluation import store as store_module
from v8_next.evaluation.multitape import _file_sha, load_multitape
from v8_next.evaluation.store import ResearchStore


def _tape(path: Path, funding_rate: str, interval: str | None) -> Path:
    rows = []
    for start in (0, 1000):
        rows.append({"channel": "kline", "instrument": "BTCUSDT", "event_time": start,
                     "payload": {"open_time_ms": start, "close_time_ms": start + 999,
                                 "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1,
                                 "closed": True, "payload_hash": str(start)}})
    payload = {"funding_time_ms": 1000, "funding_rate": funding_rate}
    if interval is not None:
        payload["funding_interval_hours"] = interval
    rows.append({"channel": "funding", "instrument": "BTCUSDT", "event_time": 1000,
                 "payload": payload})
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def test_hash_recomputed_after_metadata_preserving_rewrite(tmp_path: Path) -> None:
    path = tmp_path / "tape"
    path.write_bytes(b"aaa")
    st = path.stat()
    before = _file_sha(path)
    path.write_bytes(b"bbb")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert _file_sha(path) == hashlib.sha256(b"bbb").hexdigest() != before


@pytest.mark.parametrize("rate,interval", [("bad", None), ("0.01", "bad")])
def test_malformed_funding_is_counted_not_crashed(tmp_path: Path, rate: str, interval: str | None) -> None:
    tape = load_multitape(_tape(tmp_path / "tape.jsonl", rate, interval))
    assert tape.funding == ()
    assert tape.funding_dropped == tape.funding_raw_count == 1
    assert tape.funding_interval_defaulted == 0


@pytest.mark.parametrize("rate", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_funding_rate_is_dropped(tmp_path: Path, rate: str) -> None:
    tape = load_multitape(_tape(tmp_path / "tape.jsonl", rate, None))
    assert tape.funding == () and tape.funding_dropped == 1


@pytest.mark.parametrize("interval", ["0", "-1", "NaN", "Infinity"])
def test_invalid_funding_interval_is_dropped(tmp_path: Path, interval: str) -> None:
    tape = load_multitape(_tape(tmp_path / "tape.jsonl", "0.01", interval))
    assert tape.funding == () and tape.funding_dropped == 1
    assert tape.funding_interval_counts == {}


def test_export_never_overwrites_source_or_existing_destination_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.sqlite"
    dest = tmp_path / "existing.duckdb"
    dest.write_bytes(b"original")
    store = ResearchStore(source)
    def fail_connect(*args: object) -> None:
        raise RuntimeError("injected failure")
    monkeypatch.setattr(store_module, "duckdb", SimpleNamespace(connect=fail_connect))
    try:
        with pytest.raises(ValueError, match="source SQLite"):
            store.export_duckdb(source)
        assert store.db.execute("SELECT count(*) FROM trials").fetchone() is not None
        with pytest.raises(RuntimeError, match="injected failure"):
            store.export_duckdb(dest)
        assert dest.read_bytes() == b"original"
    finally:
        store.close()


def test_export_fallback_after_partial_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    duckdb = pytest.importorskip("duckdb")
    real_connect = duckdb.connect

    class ScannerFailure:
        def __init__(self, path: str) -> None:
            self.conn = real_connect(path)
            self.copies = 0

        def execute(self, sql: str):
            if sql.startswith("ATTACH "):
                return self.conn
            if sql.startswith("CREATE TABLE ") and " FROM src." in sql:
                self.copies += 1
                if self.copies == 2:
                    raise RuntimeError("injected scanner failure")
                name = sql.split()[2]
                return self.conn.execute(f"CREATE TABLE {name} AS SELECT 1 AS dummy")
            return self.conn.execute(sql)

        def executemany(self, sql: str, rows: list[tuple]) -> None:
            self.conn.executemany(sql, rows)

        def close(self) -> None:
            self.conn.close()

    store = ResearchStore(tmp_path / "source.sqlite")
    store.db.execute("INSERT INTO trials VALUES (?, ?, ?, ?, ?, ?)",
                     ("id", "family", "policy", "dataset", "DEVELOPMENT", 42))
    store.db.commit()
    monkeypatch.setattr(store_module, "duckdb", SimpleNamespace(connect=ScannerFailure))
    dest = tmp_path / "out.duckdb"
    try:
        store.export_duckdb(dest)
        conn = real_connect(str(dest), read_only=True)
        try:
            assert conn.execute("SELECT trial_id, registered_ns FROM trials").fetchone() == ("id", 42)
            assert "trial_id" in [r[1] for r in conn.execute("PRAGMA table_info('trials')").fetchall()]
            expected = {
                row[0] for row in store.db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            actual = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
            assert actual == expected
            for table in expected:
                quoted = '"' + table.replace('"', '""') + '"'
                assert conn.execute(f"SELECT count(*) FROM {quoted}").fetchone()[0] == (
                    store.db.execute(f"SELECT count(*) FROM {quoted}").fetchone()[0]
                )
        finally:
            conn.close()
    finally:
        store.close()
