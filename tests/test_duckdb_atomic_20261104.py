"""MECHANICS ONLY: injected scanner and fallback failure preserve destination."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from v8_next.evaluation import store as store_module
from v8_next.evaluation.store import ResearchStore


def test_failure_after_partial_copy_preserves_destination_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    duckdb = pytest.importorskip("duckdb")
    real_connect = duckdb.connect

    class Failure:
        def __init__(self, path: str) -> None:
            self.conn = real_connect(path)
            self.copies = 0

        def execute(self, sql: str):
            if sql.startswith("ATTACH "):
                return self.conn
            if sql.startswith("CREATE TABLE ") and " FROM src." in sql:
                self.copies += 1
                if self.copies == 2:
                    raise RuntimeError("scanner failed")
                return self.conn.execute(f"CREATE TABLE {sql.split()[2]} AS SELECT 1 AS dummy")
            if sql.startswith("DROP TABLE"):
                raise RuntimeError("fallback failed")
            return self.conn.execute(sql)

        def close(self) -> None:
            self.conn.close()

    monkeypatch.setattr(store_module, "duckdb", SimpleNamespace(connect=Failure))
    store = ResearchStore(tmp_path / "source.sqlite")
    destination = tmp_path / "result.duckdb"
    destination.write_bytes(b"previous destination")
    try:
        with pytest.raises(RuntimeError, match="fallback failed"):
            store.export_duckdb(destination)
        assert destination.read_bytes() == b"previous destination"
        assert not list(tmp_path.glob(".result.duckdb.*.duckdb"))
    finally:
        store.close()
