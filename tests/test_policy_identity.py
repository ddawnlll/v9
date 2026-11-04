import pytest

from v8_next.app import observe


def test_dependency_version_change_cannot_resume_frozen_policy(tmp_path, monkeypatch):
    first = observe.initialize(tmp_path)
    original = observe.importlib.metadata.version
    monkeypatch.setattr(
        observe.importlib.metadata,
        "version",
        lambda name: "changed-test-only" if name == "nautilus-trader" else original(name),
    )
    assert observe.source_hash() != first["policy"]["code_and_lock_hash"]
    with pytest.raises(ValueError):
        observe.initialize(tmp_path)
