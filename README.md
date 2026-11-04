# V9

A clean continuation of the V8 Python/NautilusTrader implementation. The existing distribution and Python package remain `v8-next` and `v8_next`; V9 features have not yet been implemented.

## Architecture

[The original V9 RFC 1.0, including Appendix A](docs/V9_ARCHITECTURE_MONOGRAPH.md) is the sole normative architecture. The existing code is the migration baseline, not a claim that the RFC is implemented.

## Setup and checks

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --locked --extra dev
uv run --locked --extra dev pytest -q tests
uv run --locked --extra dev ruff check src tests
uv run --locked --extra dev mypy src
```

The default test loop excludes `slow` tests. Some tests and tools require external market tapes or evidence not included in this repository; see their configuration and skip reasons. Optional research dependencies can be installed with `--extra research`.

The CLI is available as `uv run v8-next --help`. Source provenance and verification results are recorded in [MIGRATION_REPORT.md](MIGRATION_REPORT.md).

No economic claim, production deployment, or live trading authority is implied by this migration.
