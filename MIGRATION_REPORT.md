# Migration report

## Source and scope

- Source: `ddawnlll/v8`, commit `7abc821f2163aeb7136ab9e414107b14ca6db6ed`, tracked `v8-next/` Python source, tests, tools and lockfile. The distribution remains `v8-next` and the import package `v8_next`.
- The 28 canonical expert IDs were imported and counted: 28 unique entries. All 445 tracked source, test and tool files were copied initially. Runtime and tool paths were adapted to the standalone root; no V9 features or intentional trading-behavior changes were introduced.
- `docs/V9_ARCHITECTURE_MONOGRAPH.md` is the verbatim source RFC 1.0 including Appendix A (EK A), Git blob `49b475e7bd613101682aa97b498d614e6567d3d7`, SHA-256 `860b100cacfea69f08237dc8f1bb88ad14506d06ce76da1e3db794443ce37dab`. It is the sole normative architecture in this repository.
- Archived Rust and legacy Python trees, obsolete V8 documentation, generated audit output and external data were not copied. Historical evidence fixtures remain as fixtures, not current evidence. The retained historical dashboard test skips because the dashboard was not migrated.

## Verification

Run from the target root with Python 3.12.12 and the locked dependencies:

| Command | Exit | Result |
| --- | ---: | --- |
| `uv sync --offline --locked --extra dev` | 0 | Installed successfully from the local cache. The initial online `uv sync --locked --extra dev` failed because fetching setuptools from PyPI timed out. |
| `uv run --offline --locked --extra dev pytest -q tests` | 0 | 1070 passed, 66 skipped, 506 deselected. Default selection excludes slow tests; external evidence/data and the unmigrated historical dashboard account for skips. |
| `uv run --offline --locked --extra dev ruff check src tests` | 0 | All checks passed. |
| `uv run --offline --locked --extra dev mypy src` | 0 | Success: no issues found in 208 source files, after fixing the inherited errors. |
| `uv run --offline --locked --extra dev mypy --cache-dir /tmp/v8-source-mypy-cache /Users/hootie/src/v8-v9/v8-next/src` | 1 | Same 18 errors in the same 6 files on the V8 source, using the target environment. |
| `uv run --offline --locked --extra dev v8-next nx08-gate-manifest --help` | 0 | Relocated tool delegation works. |

## Remaining blockers and limitations

- The original 18 mypy errors reproduced on the source. Following explicit user authorization, the target now narrows checked optional values, reports missing research-lifecycle inputs before conversion, and handles platform-specific typing without changing valid trading decisions. Mypy now passes.
- External market tapes, historical evidence and the old dashboard are not included. Tests and tools that need them cannot establish those results from this checkout alone. No economic or live-trading claim is made.
- The local installation used cached packages after a network timeout; fresh installation requires access to the locked dependencies.
- Published at `https://github.com/ddawnlll/v9` on `main`; this is a migration baseline, not an economic or live-trading claim.
