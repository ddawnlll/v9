"""MECHANICS ONLY: path precedence and missing-data behavior."""

from pathlib import Path

import pytest

from v8_next.adapters.catalog_tape import resolve_tape_path


def test_override_precedence_and_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    configured = tmp_path / "path with spaces" / "tape.jsonl"
    configured.parent.mkdir()
    configured.write_text("", encoding="utf-8")
    other = tmp_path / "explicit.jsonl"
    other.write_text("", encoding="utf-8")
    monkeypatch.setenv("V8_TAPE_PATH", str(configured))
    assert resolve_tape_path() == configured
    assert resolve_tape_path(other) == other
    with pytest.raises(FileNotFoundError, match="tape"):
        resolve_tape_path(tmp_path / "missing")


def test_relative_override_independent_of_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from v8_next.adapters.catalog_tape import REPO_ROOT

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("V8_TAPE_PATH", raising=False)
    assert resolve_tape_path("pyproject.toml") == REPO_ROOT / "pyproject.toml"
