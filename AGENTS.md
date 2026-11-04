# Agent guidelines

- The sole normative architecture is `docs/V9_ARCHITECTURE_MONOGRAPH.md`, including Appendix A. Existing V8 code is a baseline, not evidence that V9 is implemented.
- Preserve evidence integrity: do not invent metrics, data, missing artifacts, or economic claims. Missing evidence must remain explicitly missing.
- No live trading, private account operations, or production authority is granted by this repository.
- Use Python 3.12 and the locked dependencies. Run `uv sync --locked --extra dev`, then `uv run --locked --extra dev pytest -q tests`, `uv run --locked --extra dev ruff check src tests`, and `uv run --locked --extra dev mypy src` as applicable.
- Fast tests exclude `slow`; run affected slow tests explicitly when appropriate. Tests requiring external tapes must not substitute invented evidence.
- Preserve unrelated changes, validate inputs and handle errors explicitly. Keep changes focused and follow existing conventions.
