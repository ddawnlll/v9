"""Instruments command: read-only venue catalogue via the upstream adapter.

MECHANICS ONLY — no network, no evaluative weight. The live fetch path
(`fetch_manifest`) is exercised only against the real venue by an operator;
these tests pin the read-only import surface and the CLI plumbing.
"""

import ast
from pathlib import Path

from v8_next.adapters import binance_instruments as instruments_mod
from v8_next.app import cli as cli_mod

_FORBIDDEN_IMPORT_FRAGMENTS = (
    "ExecClient",
    "exec_client",
    "execution_client",
    "submit_order",
    "SubmitOrder",
    "OrderManager",
)


def _module_path() -> Path:
    return Path(instruments_mod.__file__)


def test_instruments_module_imports_no_execution_path() -> None:
    tree = ast.parse(_module_path().read_text())
    imported_names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported_names.extend(a.asname or a.name for a in node.names)
            imported_names.append(node.module or "")
        elif isinstance(node, ast.Import):
            imported_names.extend(a.asname or a.name for a in node.names)
    blob = "\n".join(imported_names)
    for fragment in _FORBIDDEN_IMPORT_FRAGMENTS:
        assert fragment not in blob, fragment
    assert "load_binance_instruments" in blob


def test_cli_exposes_instruments_passthrough() -> None:
    assert cli_mod._SYSV_APP_MODULES["instruments"] == (
        "v8_next.adapters.binance_instruments"
    )
    assert callable(instruments_mod.main)


def test_fetch_manifest_rejects_unknown_venue_without_network() -> None:
    try:
        instruments_mod.fetch_manifest(venue="NOT_A_VENUE")
    except ValueError as exc:
        assert "unsupported venue" in str(exc)
    else:
        raise AssertionError("unknown venue must fail closed")


def test_write_manifest_refuses_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "manifest.json"
    target.write_text("{}")
    try:
        instruments_mod.write_manifest({"schema_version": 1}, target)
    except ValueError as exc:
        assert "refusing to overwrite" in str(exc)
    else:
        raise AssertionError("overwrite must fail closed")
