"""Portable resolution of optional datasets.

The owner's checkout keeps large, gitignored market data outside the repository.
Nothing here may assume that layout: a machine without the data must get an
explicit outcome it can report, never a silent empty run and never a crash that
looks like a code defect.

Precedence (first match wins):

1. an explicit path handed in by the caller;
2. ``V8_DATA_ROOT`` for the whole dataset tree;
3. ``V8_TAPE_PATH`` / ``V8_TAPE_ROOT`` for the tape subtree;
4. the repository-relative default used by the existing evidence layout.

Relative values resolve against the repository root, not the shell CWD, so
``V8_TAPE_PATH=research/tape/x`` means the same thing from anywhere.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Repository root for the standalone checkout.
REPO_ROOT = Path(__file__).resolve().parents[3]

#: The historical external dataset layout, kept only as a fallback.
EXTERNAL_DEFAULT = REPO_ROOT.parent / "v8"

TAPE_SUBDIR = Path("research") / "tape"


def _absolute(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else REPO_ROOT / path


def data_root() -> Path:
    """Root of the optional evidence tree."""
    configured = os.environ.get("V8_DATA_ROOT")
    if configured:
        return _absolute(configured)
    return EXTERNAL_DEFAULT


def tape_root() -> Path:
    """Root of the optional tape tree."""
    configured = os.environ.get("V8_TAPE_ROOT")
    if configured:
        return _absolute(configured)
    configured = os.environ.get("V8_TAPE_PATH")
    if configured:
        candidate = _absolute(configured)
        return candidate if candidate.is_dir() else candidate.parent
    return data_root() / TAPE_SUBDIR


def tape_path(*parts: str) -> Path:
    """Resolve a dataset below the tape root. The path may legitimately not exist."""
    return tape_root().joinpath(*parts)


def data_path(*parts: str) -> Path:
    """Resolve a dataset below the data root. The path may legitimately not exist."""
    return data_root().joinpath(*parts)


def require_tape(*parts: str) -> Path:
    """Resolve a tape and fail loudly when the dataset is absent."""
    path = tape_path(*parts)
    if not path.exists():
        raise FileNotFoundError(f"external tape not found: {path}")
    return path


def unavailable_reason(path: Path) -> str:
    """Why an optional dataset cannot be used, in one stable phrase."""
    return "MISSING_DATASET" if not path.exists() else "AVAILABLE"
