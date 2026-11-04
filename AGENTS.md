# V9 agent guidelines

## Source of truth
- Sole normative architecture: `docs/V9_ARCHITECTURE_MONOGRAPH.md` (**RFC 1.4.1**, engineering approved). The accompanying HTML is a reader, not an independent spec. Archive RFC 1.0/Appendix A and `MIGRATION_REPORT.md` are historical provenance.
- V9 active repository: `ddawnlll/v9`; implementation: `src/v8_next/`. Do not treat the historical `ddawnlll/v8` paths, obsolete ADR-001, or the archived monograph as active instructions.
- Preserve the V9 product vision: PIT MarketBelief; all 28 candidate experts (including abstention); sovereign persistent OpportunityBook; unified DecisionAction WAIT/ENTER/HOLD/REDUCE/EXIT/ABSTAIN; position-thesis and portfolio opportunity evaluation; single native execution/risk authority.
- GitHub work milestones are M0 Foundation/Safety, M1 Market/Experts, M2 Trader Brain, M3 Adaptive Position/Portfolio, M4 Integrated Validation. Factory and live qualification are separate tracks.

## Safety and evidence
- New adaptive order side effects are OFF until specific independent native risk/audit and research gates are passed and explicitly authorized. Never place live orders or access account credentials. No automatic `main` merge, production deploy, or sealed OOS access for agents.
- Distinguish DecisionAction, IntentKind, OpportunityStatus, ReasonCode and native venue states. Never create duplicate position/OMS/ledger authorities.
- Fail closed on unknown venue status, unlinked audit evidence or missing PIT data. Do not invent data, fill quality, economic superiority or successful tests. Negative results remain first-class.
- The offline coding/research Factory may select issues, create branches, run bounded tests and open PRs; restart-safe durable leases, time/token/compute budgets and human review are mandatory.

## Development
- Python 3.12, locked dependencies. Use `uv sync --locked --extra dev`, `uv run --locked --extra dev pytest -q tests`, `uv run --locked --extra dev ruff check src tests`, `uv run --locked --extra dev mypy src` as applicable.
- Default test loop excludes `slow`; explicitly run appropriate slow/native fixtures. External tapes must not be silently synthesized.
- Respect the measured 4 GiB memory target; profile RSS/OOM/time rather than assuming any implementation is efficient.
- Keep changes focused, avoid new backtesting engine or runtime rewrites without a benchmark, preserve unrelated work and report blockers accurately.
