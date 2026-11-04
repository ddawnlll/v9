"""Recovery must reject altered accounting before acquiring new market data."""

import hashlib
import json

import pytest

from v8_next.app import paper

CONFIG = {
    "maker_fee": "0",
    "taker_fee": "0",
    "initial_balance": "10000",
    "max_notional": "100",
    "max_exposure_fraction": "0.1",
}


def test_restart_reconciles_accounting_before_capture(tmp_path, monkeypatch):
    manifest = tmp_path / "capture-1" / "manifest.json"
    manifest.parent.mkdir()
    manifest.write_text("{}")
    native = {"campaigns": []}
    checkpoint = {
        "policy_hash": "frozen",
        "manifests": ["capture-1/manifest.json"],
        "manifest_hashes": [hashlib.sha256(manifest.read_bytes()).hexdigest()],
        "native_state": native,
        "revised_accounting": {"accounting_as_of_ns": 123, "balance_total": "9999"},
    }
    (tmp_path / "paper-state.json").write_text(json.dumps(checkpoint))
    monkeypatch.setattr(paper, "initialize", lambda *_: {"policy_hash": "frozen"})
    monkeypatch.setattr(paper, "replay_account", lambda *_: native)
    calls = []

    def accounting(paths, campaigns, config, cutoff):
        calls.append((paths, campaigns, cutoff))
        return {"accounting_as_of_ns": 123, "balance_total": "10000"}

    def forbidden_capture(*_):
        pytest.fail("must reconcile before capturing or advancing the session")

    monkeypatch.setattr(paper, "replay_frozen_campaigns", accounting)
    monkeypatch.setattr(paper, "capture", forbidden_capture)
    with pytest.raises(ValueError):
        paper.step(tmp_path, CONFIG)
    assert calls == [([manifest.resolve()], (), 123)]
    assert json.loads((tmp_path / "paper-state.json").read_text()) == checkpoint


def test_active_writer_blocks_second_process_and_exit_releases_lock(tmp_path, monkeypatch):
    import subprocess
    import sys

    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; from v8_next.app.paper import _acquire_writer_lock; "
            "f=open(sys.argv[1],'a+'); _acquire_writer_lock(f); "
            "print('locked',flush=True); sys.stdin.read()",
            str(tmp_path / ".paper.lock"),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout.readline().strip() == "locked"
        monkeypatch.setattr(paper, "_step_locked", lambda *args, **kwargs: {"entered": True})
        with pytest.raises(RuntimeError, match="active writer"):
            paper.step(tmp_path, CONFIG)
    finally:
        process.communicate(timeout=5)
    assert process.returncode == 0
    assert paper.step(tmp_path, CONFIG) == {"entered": True}


@pytest.mark.parametrize(
    "age,expected", [(None, 10000), (3_000_000_000, 7000), (20_000_000_000, 0)]
)
def test_initial_capture_includes_fresh_pre_session_funding(tmp_path, monkeypatch, age, expected):
    frozen_ns = 10_000_000_000
    monkeypatch.setattr(paper, "initialize", lambda *_: {"frozen_ns": frozen_ns})
    seen = []

    class Captured(Exception):
        pass

    def capture_boundary(destination, **kwargs):
        seen.append(kwargs)
        raise Captured

    monkeypatch.setattr(paper, "capture", capture_boundary)
    with pytest.raises(Captured):
        paper.step(tmp_path, {**CONFIG, "funding_max_age_ns": age})
    assert seen[0]["funding_start_ms"] == expected
    assert seen[0]["include_open_interest"] is False
    assert seen[0]["account_ratio_period"] is None
