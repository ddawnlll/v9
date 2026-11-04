"""Pinned V9 baseline provenance (M0-01).

This module makes the migrated V8 baseline reproducible and explicitly *partial*:
a dataset that is not present is recorded as ``BLOCKED`` with a reason, never as
an available hash. Verification therefore returns ``BLOCKED`` rather than
``PASS`` when authorized evidence is missing.

It grants no authority. Every V9 adaptive/order feature flag defaults to off and
:func:`assert_safe_defaults` refuses a manifest that would claim otherwise.
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import sys
from collections.abc import Mapping, Sequence
from enum import StrEnum
from importlib import metadata
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "v9-baseline-manifest-v1"
LOCKFILE = "uv.lock"
CONFIG_FILES: tuple[str, ...] = (
    "pyproject.toml",
    "AGENTS.md",
    "docs/V9_ARCHITECTURE_MONOGRAPH.md",
)
PACKAGE_NAMES: tuple[str, ...] = (
    "nautilus-trader",
    "polars",
    "numpy",
    "pydantic",
    "pandas",
)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX40 = re.compile(r"^[0-9a-f]{40}$")


class ProvenanceError(ValueError):
    """Base class for typed, fail-closed provenance failures."""

    code = "PROVENANCE_ERROR"


class ProvenanceUnavailable(ProvenanceError):
    """A required artifact cannot be read, so no verdict can be produced."""

    code = "PROVENANCE_UNAVAILABLE"


class ProvenanceDrift(ProvenanceError):
    """A pinned artifact no longer matches its recorded identity."""

    code = "PROVENANCE_DRIFT"


class ManifestIntegrityError(ProvenanceError):
    """The manifest body does not hash to its recorded digest."""

    code = "MANIFEST_INTEGRITY"


class ArtifactStatus(StrEnum):
    """Evidence state of one artifact or of a whole verification."""

    AVAILABLE = "AVAILABLE"
    BLOCKED = "BLOCKED"
    PASS = "PASS"
    DRIFT = "DRIFT"


def canonical_json(payload: Mapping[str, Any]) -> str:
    """Deterministic JSON used for every digest in this module."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class FileIdentity(BaseModel):
    """A pinned source/config file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    sha256: str
    size_bytes: int = Field(ge=0)

    @field_validator("sha256")
    @classmethod
    def _digest(cls, value: str) -> str:
        if not _HEX64.match(value):
            raise ValueError("sha256 must be 64 lowercase hex characters")
        return value


class DatasetIdentity(BaseModel):
    """A dataset is either hashed or explicitly blocked. There is no third state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    status: ArtifactStatus
    sha256: str | None = None
    size_bytes: int | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _status_consistent(self) -> DatasetIdentity:
        if self.status is ArtifactStatus.AVAILABLE:
            if self.sha256 is None or not _HEX64.match(self.sha256):
                raise ValueError("AVAILABLE dataset requires a 64-hex sha256")
            if self.size_bytes is None or self.size_bytes < 0:
                raise ValueError("AVAILABLE dataset requires a non-negative size")
            if self.reason is not None:
                raise ValueError("AVAILABLE dataset cannot carry a blocked reason")
            return self
        if self.status is ArtifactStatus.BLOCKED:
            if not self.reason:
                raise ValueError("BLOCKED dataset requires a reason")
            if self.sha256 is not None or self.size_bytes is not None:
                raise ValueError("BLOCKED dataset cannot fabricate identity fields")
            return self
        raise ValueError(f"{self.status} is not a dataset evidence state")


class V9FeatureFlags(BaseModel):
    """Baseline-off configuration. All V9 capabilities are disabled by default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    adaptive_orders: bool = False
    market_belief: bool = False
    decision_policy: bool = False
    portfolio_allocator: bool = False
    position_thesis: bool = False

    def any_enabled(self) -> bool:
        return any(
            (
                self.adaptive_orders,
                self.market_belief,
                self.decision_policy,
                self.portfolio_allocator,
                self.position_thesis,
            )
        )


BASELINE_FLAGS = V9FeatureFlags()


def assert_safe_defaults(flags: V9FeatureFlags) -> None:
    """Refuse any manifest that presents a V9 capability as baseline-on."""
    if flags.any_enabled():
        raise ProvenanceError("baseline manifest must keep all V9 capability flags off")


class BaselineVerification(BaseModel):
    """Outcome of re-deriving a manifest against a working tree."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: ArtifactStatus
    checked_files: int = Field(ge=0)
    mismatches: tuple[str, ...] = ()
    blocked_datasets: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.status is ArtifactStatus.PASS


class BaselineManifest(BaseModel):
    """Immutable reproducibility receipt for one pinned baseline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = SCHEMA_VERSION
    code_sha: str
    generated_ns: int = Field(gt=0)
    python_version: str
    platform: str
    package_versions: dict[str, str]
    lockfile: FileIdentity
    config_files: tuple[FileIdentity, ...]
    datasets: tuple[DatasetIdentity, ...] = ()
    flags: V9FeatureFlags = BASELINE_FLAGS
    baseline_tests: dict[str, int] = Field(default_factory=dict)
    manifest_digest: str

    @field_validator("code_sha")
    @classmethod
    def _code_sha(cls, value: str) -> str:
        if not _HEX40.match(value):
            raise ValueError("code_sha must be a 40-character git SHA")
        return value

    @field_validator("manifest_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if not _HEX64.match(value):
            raise ValueError("manifest_digest must be 64 lowercase hex characters")
        return value

    @model_validator(mode="after")
    def _integrity(self) -> BaselineManifest:
        assert_safe_defaults(self.flags)
        expected = manifest_digest_for(self)
        if expected != self.manifest_digest:
            raise ManifestIntegrityError(
                f"manifest digest mismatch: recorded {self.manifest_digest}, computed {expected}"
            )
        return self

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), indent=2, sort_keys=True)


def manifest_digest_for(manifest: BaselineManifest) -> str:
    body = manifest.model_dump(mode="json", exclude={"manifest_digest"})
    return hashlib.sha256(canonical_json(body).encode()).hexdigest()


def _file_identity(repo_root: Path, relative: str) -> FileIdentity:
    path = repo_root / relative
    if not path.is_file():
        raise ProvenanceUnavailable(f"required baseline artifact is missing: {relative}")
    return FileIdentity(path=relative, sha256=sha256_file(path), size_bytes=path.stat().st_size)


def dataset_identity(repo_root: Path, relative: str, *, reason: str | None = None) -> DatasetIdentity:
    """Hash a present dataset, or record it as BLOCKED. Never invent an identity."""
    path = repo_root / relative
    if path.is_file():
        return DatasetIdentity(
            path=relative,
            status=ArtifactStatus.AVAILABLE,
            sha256=sha256_file(path),
            size_bytes=path.stat().st_size,
        )
    return DatasetIdentity(
        path=relative,
        status=ArtifactStatus.BLOCKED,
        reason=reason or "DATASET_ABSENT",
    )


def package_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for name in PACKAGE_NAMES:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = "NOT_INSTALLED"
    return versions


def build_manifest(
    repo_root: Path,
    *,
    code_sha: str,
    generated_ns: int,
    authorized_datasets: Sequence[str] = (),
    blocked_datasets: Mapping[str, str] | None = None,
    flags: V9FeatureFlags | None = None,
    baseline_tests: Mapping[str, int] | None = None,
) -> BaselineManifest:
    """Build a manifest from what is actually on disk.

    ``authorized_datasets`` that are absent become BLOCKED; ``blocked_datasets``
    names evidence we know we do not have. Both are explicit, never a PASS.
    """
    selected = BASELINE_FLAGS if flags is None else flags
    datasets = [dataset_identity(repo_root, path) for path in authorized_datasets]
    datasets.extend(
        dataset_identity(repo_root, path, reason=reason)
        for path, reason in sorted((blocked_datasets or {}).items())
    )
    body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "code_sha": code_sha,
        "generated_ns": generated_ns,
        "python_version": platform.python_version(),
        "platform": f"{sys.platform}-{platform.machine()}",
        "package_versions": package_versions(),
        "lockfile": _file_identity(repo_root, LOCKFILE).model_dump(mode="json"),
        "config_files": [
            _file_identity(repo_root, path).model_dump(mode="json") for path in CONFIG_FILES
        ],
        "datasets": [item.model_dump(mode="json") for item in datasets],
        "flags": selected.model_dump(mode="json"),
        "baseline_tests": dict(baseline_tests or {}),
    }
    digest = hashlib.sha256(canonical_json(body).encode()).hexdigest()
    return BaselineManifest.model_validate({**body, "manifest_digest": digest})


def verify_manifest(manifest: BaselineManifest, repo_root: Path) -> BaselineVerification:
    """Re-derive every pinned identity. Missing evidence downgrades to BLOCKED."""
    mismatches: list[str] = []
    blocked: list[str] = []
    checked = 0
    pinned: list[FileIdentity] = [manifest.lockfile, *manifest.config_files]
    for identity in pinned:
        path = repo_root / identity.path
        checked += 1
        if not path.is_file():
            blocked.append(identity.path)
            continue
        actual = sha256_file(path)
        if actual != identity.sha256 or path.stat().st_size != identity.size_bytes:
            mismatches.append(
                f"{identity.path}: expected {identity.sha256}/{identity.size_bytes}, "
                f"found {actual}/{path.stat().st_size}"
            )
    for dataset in manifest.datasets:
        if dataset.status is ArtifactStatus.BLOCKED:
            blocked.append(dataset.path)
            continue
        path = repo_root / dataset.path
        checked += 1
        if not path.is_file():
            blocked.append(dataset.path)
            continue
        actual = sha256_file(path)
        if actual != dataset.sha256:
            mismatches.append(f"{dataset.path}: expected {dataset.sha256}, found {actual}")
    if mismatches:
        status = ArtifactStatus.DRIFT
    elif blocked:
        status = ArtifactStatus.BLOCKED
    else:
        status = ArtifactStatus.PASS
    return BaselineVerification(
        status=status,
        checked_files=checked,
        mismatches=tuple(mismatches),
        blocked_datasets=tuple(blocked),
    )


def write_manifest(manifest: BaselineManifest, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(manifest.to_json() + "\n", encoding="utf-8")


def load_manifest(path: Path) -> BaselineManifest:
    if not path.is_file():
        raise ProvenanceUnavailable(f"manifest not found: {path}")
    return BaselineManifest.model_validate_json(path.read_text(encoding="utf-8"))
