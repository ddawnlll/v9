"""A receipt and a run key must bind the SOURCE CONTENT that produced them.

`CodeIdentity` carried `git_rev` plus a one-bit `git_dirty` flag and nothing
else, so two materially different uncommitted trees at the same HEAD shared a
`receipt_id`, an artifact filename tag and a `runs/<key>.json` run key — and a
changed tree re-run into the same `--output-dir` was refused as an
already-completed window (or overwrote the earlier manifest). A receipt could
therefore name a revision whose code does not produce its numbers.

Evidence class: **MECHANICS ONLY** — seeded temporary trees, no tape, no market
data, zero evaluative weight, no economic claim.
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import pytest

from v8_next.evaluation import economic_benchmark as eb
from v8_next.evaluation.run_window import (
    RunKey,
    WindowAlreadyCompleted,
    WindowRunManifest,
    WindowSpec,
    assert_window_resumable,
    load_window_manifest,
    write_window_manifest,
)

HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _tree(
    root: Path,
    estimator: str,
    *,
    lock: str = "uv-lock-bytes",
    project: str = "project-bytes",
) -> Path:
    """A minimal stand-in for the v8-next project tree (MECHANICS ONLY)."""
    pkg = root / "src" / "v8_next" / "evaluation"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "estimator.py").write_text(estimator)
    (root / "pyproject.toml").write_text(project)
    (root / "uv.lock").write_text(lock)
    return root


def _identity(
    source_sha: str, *, rev: str = "66d67762", dirty: str = "yes"
) -> eb.RunIdentity:
    """A run identity that differs from its twin ONLY in the source digest."""
    return eb.RunIdentity(
        dataset=eb.DatasetIdentity(
            tape_path="MECHANICS_ONLY",
            tape_sha256="0" * 64,
            universe=("MECHANICS_ONLY",),
            period_start_ns=0,
            period_end_ns=3_600_000_000_000,
            n_bars=120,
            source_hashes=(),
        ),
        code=eb.CodeIdentity(
            git_rev=rev,
            git_dirty=dirty,
            config_sha256="c" * 64,
            estimator_versions={"numpy": "2.0.0"},
            source_sha256=source_sha,
        ),
        seed=0,
        primary_benchmark="cash",
        diagnostic_benchmarks=("cash",),
        strategy_family=("MECHANICS_ONLY",),
        capital=10_000.0,
        taker_fee=0.0005,
        opex_monthly_usd=0.0,
    )


def _run_key(code_and_lock_hash: str) -> RunKey:
    return RunKey.build(
        window=WindowSpec(tape_path="research/tape/quad-1h-12m", profile="smoke", bars=120),
        case_id="BC-SRC",
        policy_id="pol_src",
        dataset_sha256="a" * 64,
        strategy_config='{"quorum": 1}',
        capital="10000",
        taker_fee="0.0005",
        baseline="cash",
        execution_profile_digest="b" * 64,
        code_and_lock_hash=code_and_lock_hash,
    )


# --------------------------------------------------------------------------- #
# the source digest itself
# --------------------------------------------------------------------------- #
def test_source_digest_separates_two_trees_at_one_revision(tmp_path: Path) -> None:
    """Two trees that differ by one estimator byte must not share a digest."""
    before = _tree(tmp_path / "a", "ESTIMATOR = 'before'\n")
    after = _tree(tmp_path / "b", "ESTIMATOR = 'after'\n")

    digest = eb.source_sha256(before)
    assert HEX64.match(digest)
    # deterministic: repeated reads of the same tree agree (no wall clock)
    assert eb.source_sha256(before) == digest
    assert eb.source_sha256(after) != digest

    # the same bytes at a different absolute path hash the same: no path leaks
    relocated = _tree(tmp_path / "deep" / "nested" / "c", "ESTIMATOR = 'before'\n")
    assert eb.source_sha256(relocated) == digest

    # the pinned members decide the runtime too
    assert (
        eb.source_sha256(_tree(tmp_path / "d", "ESTIMATOR = 'before'\n", lock="other-lock"))
        != digest
    )
    assert (
        eb.source_sha256(_tree(tmp_path / "e", "ESTIMATOR = 'before'\n", project="other-project"))
        != digest
    )

    # build residue is not code
    (before / "src" / "v8_next" / "__pycache__").mkdir()
    (before / "src" / "v8_next" / "__pycache__" / "estimator.cpython-312.pyc").write_bytes(b"\x00")
    (before / "src" / "v8_next" / ".venv" / "site-packages").mkdir(parents=True)
    (before / "src" / "v8_next" / ".venv" / "site-packages" / "other.py").write_text("OTHER = 1\n")
    assert eb.source_sha256(before) == digest

    # a vanished member is visible, not silently equal
    intact = eb.source_sha256(after)
    (after / "uv.lock").unlink()
    assert eb.source_sha256(after) != intact


def test_default_root_is_the_project_that_runs() -> None:
    assert eb.source_sha256() == eb.source_sha256(eb.project_root())
    assert HEX64.match(eb.source_sha256())


# --------------------------------------------------------------------------- #
# receipt_id
# --------------------------------------------------------------------------- #
def test_receipt_id_separates_two_trees_at_one_revision(tmp_path: Path) -> None:
    """Same rev, same `git_dirty="yes"`, different source => different id."""
    trees = [
        _tree(tmp_path / name, body)
        for name, body in (("a", "ESTIMATOR = 1\n"), ("b", "ESTIMATOR = 2\n"))
    ]
    identities = [_identity(eb.source_sha256(tree)) for tree in trees]

    # the declared code identity is identical except for the source digest
    assert identities[0].code.git_rev == identities[1].code.git_rev
    assert identities[0].code.git_dirty == identities[1].code.git_dirty == "yes"
    assert identities[0].code.config_sha256 == identities[1].code.config_sha256
    assert identities[0].code.source_sha256 != identities[1].code.source_sha256

    # ... and that single difference must move the run identity
    assert identities[0].digest() != identities[1].digest()
    receipt_ids = [identity.digest()[:32] for identity in identities]
    assert receipt_ids[0] != receipt_ids[1]
    # the artifact filename tag is derived from receipt_id[:8]
    assert receipt_ids[0][:8] != receipt_ids[1][:8]

    # identical trees reproduce the identity exactly: no entropy was added
    assert _identity(eb.source_sha256(trees[0])).digest() == identities[0].digest()

    # the digest is derived from a payload that carries the source hash
    payload = json.loads(identities[0].model_dump_json())
    assert payload["code"]["source_sha256"] == eb.source_sha256(trees[0])


def test_code_identity_requires_a_source_digest() -> None:
    """No construction site may declare code identity without saying which code."""
    assert "source_sha256" in eb.CodeIdentity.__pydantic_fields__
    assert eb.CodeIdentity.__pydantic_fields__["source_sha256"].is_required()
    assert "source_sha256" in json.loads(_identity("0" * 64).model_dump_json())["code"]


# --------------------------------------------------------------------------- #
# the run key twin (completed-window guard)
# --------------------------------------------------------------------------- #
def test_run_key_separates_two_trees_at_one_revision(tmp_path: Path) -> None:
    """A changed tree at the same rev is a different run, not a completed one."""
    before = _tree(tmp_path / "a", "ESTIMATOR = 1\n")
    after = _tree(tmp_path / "b", "ESTIMATOR = 2\n")

    hashes = [eb.code_and_lock_hash(tree) for tree in (before, after)]
    assert hashes[0] != hashes[1]
    # the discriminator is the source content, not a one-bit dirty flag
    assert eb.source_sha256(before) in hashes[0]
    assert eb.source_sha256(after) in hashes[1]

    keys = [_run_key(value) for value in hashes]
    assert keys[0].digest != keys[1].digest
    paths = [
        tmp_path / "runs" / f"{key.digest.split(':')[1][:16]}.json" for key in keys
    ]
    assert paths[0] != paths[1]

    # a COMPLETED window for the old tree must not refuse the changed tree
    write_window_manifest(
        paths[0],
        WindowRunManifest(
            run_key=keys[0].digest,
            window={"profile": "smoke"},
            state="COMPLETED",
            started_ns=0,
            finished_ns=1,
        ),
    )
    assert not paths[1].exists()
    assert assert_window_resumable(load_window_manifest(paths[1]), keys[1].digest) is None

    # unchanged tree: same key, so the completed-window refusal still holds
    assert _run_key(eb.code_and_lock_hash(before)).digest == keys[0].digest
    with pytest.raises(WindowAlreadyCompleted):
        assert_window_resumable(load_window_manifest(paths[0]), keys[0].digest)


# --------------------------------------------------------------------------- #
# every CLI path that mints an identity must feed it
# --------------------------------------------------------------------------- #
def test_cli_paths_bind_the_source_digest() -> None:
    from v8_next.app import benchmark as bench_mod
    from v8_next.app import economic as econ_mod
    from v8_next.app import portfolio as port_mod

    for module in (port_mod, econ_mod):
        source = inspect.getsource(module)
        assert "source_sha256=eb.source_sha256()" in source, module.__name__
        # the receipt/report text must say what the source hash covers
        assert "eb.SOURCE_IDENTITY_NOTE" in source, module.__name__

    port_source = inspect.getsource(port_mod)
    assert "code_and_lock_hash=eb.code_and_lock_hash()" in port_source
    # the one-bit `rev:dirty:uv.lock` key is gone: it cannot tell dirty trees apart
    assert "git_info()['dirty']}:" not in port_source

    assert "code_and_lock_hash=eb.code_and_lock_hash(" in inspect.getsource(bench_mod)

pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
