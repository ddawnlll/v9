# V9 — Market-Aware Adaptive Trading Intelligence

A clean continuation of the V8 Python/NautilusTrader implementation, with **RFC 1.4.1** approved as the V9 engineering architecture. The implementation is still a migrated V8 baseline; RFC 1.4.1 features are **not yet implemented or economically qualified**.

## Architecture — single normative source

- **[V9 Architecture Monograph — RFC 1.4.1](docs/V9_ARCHITECTURE_MONOGRAPH.md)** is the sole normative architecture for product scope and native safety.
- [HTML reader (figures preserved)](docs/V9_ARCHITECTURE_MONOGRAPH_RFC_1.4.1.html) is a formatted, non-normative companion to the Markdown.
- [RFC 1.0 + Appendix A archive](docs/archive/V9_ARCHITECTURE_MONOGRAPH_RFC_1.0.md) and the original `ddawnlll/v8` repository are historical provenance, not competing current specifications.
- [MIGRATION_REPORT.md](MIGRATION_REPORT.md) records the **migration-time** baseline, not acceptance of RFC 1.4.1 or of live execution.

RFC 1.4.1 preserves PIT MarketBelief, 28 candidate experts, OpportunityBook, unified WAIT/ENTER/HOLD/REDUCE/EXIT decision policy, active position/portfolio evaluation, native execution and independent audit. The offline Cloud Development Factory is separate from live trading authority. Development tracks M0–M4; live qualification is a separate approval program.

## Setup and fast checks

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --locked --extra dev
uv run --locked --extra dev pytest -q tests
uv run --locked --extra dev ruff check src tests
uv run --locked --extra dev mypy src
```

The default test loop excludes `slow` tests. External market tapes and sealed evidence are not included. Optional research dependencies can be installed with `--extra research`.

The migrated distribution and import package remain `v8-next` and `v8_next`, and the CLI remains `uv run v8-next --help`. No behavior changes or new runtime capabilities are asserted by this documentation update.

**No economic claim, production deployment or live trading authority is implied by architecture approval.**
