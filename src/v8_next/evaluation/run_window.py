"""NX05 (#426) — one window, profile, run key and resume manifest for both paths.

The D153 benchmark path and the economic portfolio path are different code, but a
run of either is described by the same facts: which physical tape, which window,
which execution profile, which policy/config/cost model. That shared description
is a :class:`RunKey`: a content-addressed digest with no wall clock in it, so the
same inputs always derive the same key and a different input can never collide.

Two rules this module enforces, both of which were previously left to a caller's
discipline:

* **A bar-count shortcut is a smoke run.** ``--bars`` may only describe a
  ``smoke`` window; ``fold``/``benchmark`` profiles must be bounded by explicit
  UTC instants. A smoke run is liveness evidence and is never economic evidence,
  so it mints no capability score: the window's evidence class
  (:attr:`WindowSpec.evidence_class`) travels with the run and into the receipt,
  so a ledger number can never be read apart from the class that produced it
  (#444).
* **A completed window is not re-executed.** The window manifest is written
  atomically and a ``COMPLETED`` key refuses re-execution, so a resume can never
  append a second ledger entry or a second cash flow. A ``RUNNING`` manifest is
  an incomplete run: it is reported as such, never as success.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping

WINDOW_MANIFEST_VERSION = "v87-window-manifest-v1"

RunState = Literal["RUNNING", "COMPLETED"]


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class ExecutionProfileSpec:
    """What a run's length is allowed to prove."""

    name: str
    purpose: str
    economic_evidence: bool
    max_bars: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "purpose": self.purpose,
            "economic_evidence": self.economic_evidence,
            "max_bars": self.max_bars,
        }


EXECUTION_PROFILES: dict[str, ExecutionProfileSpec] = {
    "smoke": ExecutionProfileSpec(
        name="smoke",
        purpose="liveness and mechanics only; never economic sufficiency evidence",
        economic_evidence=False,
        max_bars=500,
    ),
    "fold": ExecutionProfileSpec(
        name="fold",
        purpose="one chronological diagnostic fold (3 calendar months), UTC-bounded",
        economic_evidence=True,
        max_bars=None,
    ),
    "benchmark": ExecutionProfileSpec(
        name="benchmark",
        purpose="release benchmark over the selected window, UTC-bounded",
        economic_evidence=True,
        max_bars=None,
    ),
}


def execution_profile(name: str) -> ExecutionProfileSpec:
    try:
        return EXECUTION_PROFILES[name]
    except KeyError:
        raise ValueError(
            f"unknown execution profile {name!r}; known: {sorted(EXECUTION_PROFILES)}"
        ) from None


@dataclass(frozen=True)
class WindowSpec:
    """The physical data window a run consumed, in explicit units."""

    tape_path: str
    profile: str
    instrument: str | None = None
    start_ms: int | None = None
    end_ms: int | None = None
    fold_id: str | None = None
    bars: int | None = None
    #: #490: the bar count the run *actually loaded* for this window. ``bars``
    #: stays the caller's smoke shortcut (a declarative request, refused for
    #: evidential profiles by :meth:`validate`); ``measured_bars`` is what the
    #: tape yielded and is never accepted in its place. Filled in by the producer
    #: once the tape has been read, never by a caller's declaration.
    measured_bars: int | None = None

    def validate(self) -> None:
        spec = execution_profile(self.profile)
        if self.start_ms is not None and self.end_ms is not None:
            if self.start_ms >= self.end_ms:
                raise ValueError("window start must precede window end")
        if self.bars is not None:
            if self.profile != "smoke":
                # #490: this rule is about the *shortcut*, not about measurement:
                # a loaded bar count belongs in ``measured_bars``, so a UTC-bounded
                # evidential window can carry its sample size without pretending to
                # have been requested as a smoke window.
                raise ValueError(
                    f"a bar-count window is a smoke window, not {self.profile!r}; "
                    "bound fold/benchmark runs by explicit UTC instants"
                )
            if self.bars <= 0:
                raise ValueError("bars must be positive")
            if spec.max_bars is not None and self.bars > spec.max_bars:
                raise ValueError(
                    f"{self.bars} bars exceeds the {self.profile} profile cap "
                    f"of {spec.max_bars}; use a UTC-bounded profile instead"
                )
        if self.profile in ("fold", "benchmark") and (
            self.start_ms is None or self.end_ms is None
        ):
            raise ValueError(
                f"the {self.profile} profile requires explicit UTC start and end"
            )
        if not self.tape_path.strip():
            raise ValueError("window requires a tape path")

    @property
    def is_smoke(self) -> bool:
        return self.profile == "smoke"

    @property
    def carried_bars(self) -> int | None:
        """The bar count this window can be evidenced on: measured first, else declared.

        #490: one source of truth for what a receipt records as the sample size.
        A measured count always wins, because it is what the tape yielded; the
        smoke shortcut is only a fallback for a profile whose window *is* that
        request. Neither is imputed: an evidential window that never measured a
        count answers ``None`` and is refused by name downstream.
        """
        return self.measured_bars if self.measured_bars is not None else self.bars

    @property
    def proves_economic_evidence(self) -> bool:
        return execution_profile(self.profile).economic_evidence

    @property
    def evidence_class(self) -> str:
        """What this window is allowed to prove, named once and carried everywhere.

        A profile that carries economic evidence is named after itself; a profile
        that does not (:data:`EXECUTION_PROFILES` ``smoke``) is named ``smoke``.
        The class is derived from the profile spec, never from a caller flag, so a
        bar-count window cannot describe itself as a benchmark (#444).
        """
        return self.profile if self.proves_economic_evidence else "smoke"

    def as_dict(self) -> dict[str, Any]:
        return {
            "tape_path": self.tape_path,
            "instrument": self.instrument,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "fold_id": self.fold_id,
            "bars": self.bars,
            # #490: the loaded count is published *beside* the declared one, never in
            # its place: a manifest of a UTC-bounded window keeps ``bars: null``
            # (it was never requested as a bar count) and says how many bars the run
            # actually measured.
            "measured_bars": self.measured_bars,
            "profile": self.profile,
            "profile_spec": execution_profile(self.profile).as_dict(),
            "is_smoke": self.is_smoke,
            "economic_evidence": self.proves_economic_evidence,
            "evidence_class": self.evidence_class,
        }

    def label(self) -> str:
        """A one-line, non-flattering description for run output."""
        if self.is_smoke:
            return (
                f"SMOKE WINDOW ({self.bars} bars) — liveness/mechanics only, "
                "NOT economic evidence, NOT a release benchmark"
            )
        span = f"{self.start_ms}..{self.end_ms}"
        return f"{self.profile.upper()} WINDOW {span} ({self.fold_id or 'no fold id'})"


@dataclass(frozen=True)
class RunKey:
    """Content-addressed identity of one run's inputs. No wall clock inside."""

    digest: str
    components: dict[str, str]

    @classmethod
    def build(
        cls,
        *,
        window: WindowSpec,
        case_id: str,
        policy_id: str,
        dataset_sha256: str,
        strategy_config: str,
        capital: str,
        taker_fee: str,
        baseline: str,
        execution_profile_digest: str,
        code_and_lock_hash: str,
        extra: Mapping[str, str] | None = None,
    ) -> RunKey:
        window.validate()
        components: dict[str, str] = {
            "window_tape_path": window.tape_path,
            "window_instrument": window.instrument or "",
            "window_start_ms": "" if window.start_ms is None else str(window.start_ms),
            "window_end_ms": "" if window.end_ms is None else str(window.end_ms),
            "window_fold_id": window.fold_id or "",
            "window_bars": "" if window.bars is None else str(window.bars),
            "profile": window.profile,
            "case_id": case_id,
            "policy_id": policy_id,
            "dataset_sha256": dataset_sha256,
            "strategy_config": strategy_config,
            "capital": capital,
            "taker_fee": taker_fee,
            "baseline": baseline,
            "execution_profile_digest": execution_profile_digest,
            "code_and_lock_hash": code_and_lock_hash,
        }
        if extra:
            for key, value in extra.items():
                if key in components:
                    raise ValueError(f"duplicate run-key component {key!r}")
                components[key] = value
        return cls(
            digest="sha256:" + hashlib.sha256(canonical(components).encode()).hexdigest(),
            components=components,
        )

    def as_dict(self) -> dict[str, Any]:
        return {"run_key": self.digest, "components": dict(sorted(self.components.items()))}


@dataclass(frozen=True)
class WindowRunManifest:
    """On-disk record of one window run: its key, state and bound artifacts."""

    run_key: str
    window: dict[str, Any]
    state: RunState
    artifacts: tuple[dict[str, Any], ...] = ()
    started_ns: int = 0
    finished_ns: int | None = None
    version: str = WINDOW_MANIFEST_VERSION
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def completed(self) -> bool:
        return self.state == "COMPLETED"

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "run_key": self.run_key,
            "window": self.window,
            "state": self.state,
            "completed": self.completed,
            "artifacts": list(self.artifacts),
            "started_ns": self.started_ns,
            "finished_ns": self.finished_ns,
            "detail": dict(self.detail),
        }


class WindowAlreadyCompleted(RuntimeError):
    """Raised when a completed window would be executed a second time."""


def write_window_manifest(path: Path | str, manifest: WindowRunManifest) -> Path:
    """Write atomically: a reader never sees a half-written manifest."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(manifest.as_dict(), indent=2, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".window-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return target


def load_window_manifest(path: Path | str) -> WindowRunManifest | None:
    target = Path(path)
    if not target.is_file():
        return None
    raw = json.loads(target.read_text())
    if raw.get("version") != WINDOW_MANIFEST_VERSION:
        raise ValueError(f"unsupported window manifest version: {raw.get('version')!r}")
    return WindowRunManifest(
        run_key=str(raw["run_key"]),
        window=dict(raw["window"]),
        state=raw["state"],
        artifacts=tuple(raw.get("artifacts") or ()),
        started_ns=int(raw.get("started_ns") or 0),
        finished_ns=raw.get("finished_ns"),
        detail=dict(raw.get("detail") or {}),
    )


def assert_window_resumable(
    manifest: WindowRunManifest | None, run_key: str
) -> WindowRunManifest | None:
    """Refuse to re-run a completed key; surface an unfinished one as incomplete.

    Returns the existing manifest when the run is still ``RUNNING`` (the caller may
    continue it), raises :class:`WindowAlreadyCompleted` when the key is complete
    (no second ledger append, no second cash flow), and returns ``None`` when the
    key has never been seen.
    """
    if manifest is None:
        return None
    if manifest.run_key != run_key:
        raise ValueError(
            "window manifest belongs to another run key: "
            f"{manifest.run_key} != {run_key}"
        )
    if manifest.completed:
        raise WindowAlreadyCompleted(
            f"run key {run_key} is already COMPLETED at {manifest.finished_ns}; "
            "refusing to re-execute a completed window"
        )
    return manifest


def incomplete_runs(paths: list[Path | str]) -> list[WindowRunManifest]:
    """Every manifest that claims a run which never finished."""
    out: list[WindowRunManifest] = []
    for path in paths:
        manifest = load_window_manifest(path)
        if manifest is not None and not manifest.completed:
            out.append(manifest)
    return out


def verify_artifact_run_key(
    manifest: WindowRunManifest, expected_run_key: str
) -> tuple[bool, str]:
    """A gate may only be resolved from this run's own artifacts.

    Returns ``(False, reason)`` when the manifest belongs to another run key or
    when a bound artifact no longer matches its recorded hash, so a foreign run's
    output cannot be presented as this run's evidence.
    """
    if manifest.run_key != expected_run_key:
        return False, f"FOREIGN_RUN_ARTIFACT: {manifest.run_key} != {expected_run_key}"
    if not manifest.completed:
        return False, f"INCOMPLETE_RUN: state={manifest.state}"
    for artifact in manifest.artifacts:
        path = Path(str(artifact.get("path", "")))
        if not path.is_file():
            return False, f"ARTIFACT_MISSING: {path}"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != artifact.get("sha256"):
            return False, f"ARTIFACT_HASH_MISMATCH: {path}"
    return True, "OK"
