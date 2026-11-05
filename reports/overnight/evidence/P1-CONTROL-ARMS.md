# P1 — matched random-entry control arms, PIT regime split and the null comparison

Status: **IMPLEMENTED** (module + tests). No V9 adaptive treatment arm exists yet, so every
end-to-end comparison in this repo state is `NOT_RUN`; the module refuses to invent one.

Owned files:

- `src/v8_next/evaluation/control_arms.py` (new)
- `tests/test_control_arms.py` (new)

## What the module owns

### 1. Matched control arms (`random_entry_controls`)

`RANDOM_ENTRY_MATCHED_EXITS` arms (`ArmKind` value recorded on every `ArmResult`) are built
from a pool of **measured** per-opportunity random-entry outcomes. Each draw selects exactly
`exposure_budget` opportunities without replacement — the treatment's campaign count — so the
null spends the same opportunity budget. The pool is sorted by `(bar_index, opportunity_id)`
before sampling: draws depend on the pool's content and the seed, never on caller ordering
(asserted by `test_pool_ordering_does_not_change_the_draws`).

Matching is a refusal, not a comment. `MatchSpec` carries calendar, capital, venue,
stop/target bracket in bps, the three cost terms in bps and the exposure rule; every arm's
`risk_basis` string records `BRACKET_STOP_BPS`, `TARGET_BPS`, `CAPITAL`, `VENUE`,
`CALENDAR`, and `compare_to_controls` refuses:

- any differing `MatchSpec` field, named (`ControlArmMismatch.fields`),
- a control measured on another tape/engine/execution profile, via
  `ablations._require_comparable_provenance` — the same guard `compute_delta` uses,
- a control priced by another fee model (`fee_model`/`maker_fee`/`taker_fee`),
- an exposure budget that is not the treatment's campaign count,
- an uncosted control arm, or a treatment with no provenance.

### 2. Point-in-time regime split (`label_regimes`)

Rule (documented in `RegimeConfig`), applied to every bar with data at or before it:

1. `i < warmup` (`warmup = max(trend_lookback, 2 * vol_lookback)`) → `UNLABELLED`;
2. trailing realised vol > `high_vol_multiple` × the previous `vol_lookback` vols → `HIGH_VOL`;
3. log-close least-squares slope > `+slope_threshold_per_bar` → `TREND_UP`, `< -threshold` →
   `TREND_DOWN`;
4. otherwise `RANGE`.

`UNLABELLED` is a real bucket, so per-regime bar counts sum to the total bar count
(`test_regime_counts_sum_to_the_total`, `test_per_regime_buckets_sum_to_the_total`). The PIT
property is tested by recomputing every prefix and comparing it to the full label sequence,
including a mutated future bar (`test_regime_labels_are_point_in_time`).

### 3. Honest comparison (`compare_to_controls`, `compare_by_regime`)

Decision rule, single-sourced in `_evaluate`:

- `NOT_RUN` if the treatment is uncosted or has no measured per-campaign net R — the absence
  is named, never defaulted;
- `INCONCLUSIVE` for too few campaigns, too few draws, or a p-value resolution
  `1/(draws+1) > alpha` (a PASS that is arithmetically unreachable is not reported);
- `NO_EDGE` when the treatment is **inside** the control spread (not strictly above the
  95th percentile) or the empirical p exceeds alpha or the delta is non-positive;
- `PASS` only when the treatment is strictly outside the control spread **and**
  `p = (1 + #{draws >= treatment}) / (draws + 1) <= alpha`.

## Measured evidence (commands run in this worktree)

All numbers below come from the commands quoted; none is estimated. The control pool is a
declared arithmetic fixture (60 outcomes, `net_r = -0.8 + 0.03*i`, exposure budget 40), used
only to exercise the decision rule — it is **not** market data and no economic claim is made
from it.

```
$ PYTHONPATH=src .venv/bin/python - <<'PY'   # script quoted in this report body
DISTRIBUTION draws=50 seed=20251105 min=-0.037250 mean=0.081265 max=0.158500
             q05=0.027250 q50=0.086500 q95=0.136000
SAME-DISTRIBUTION verdict=NO_EDGE gate=INSIDE_CONTROL_SPREAD treatment_net_r=0.082750
                  empirical_p=0.529412 percentile_rank=0.4800 control_mean=0.081265
                  delta=0.001485
FAR-OUTSIDE verdict=PASS gate=OUTSIDE_CONTROL_DISTRIBUTION empirical_p=0.019608
            (=1/51=0.019608) rank=1.0000 delta=4.918735
ABSENT verdict=NOT_RUN gate=TREATMENT_NOT_MEASURED empirical_p=None
       reason='the treatment arm carries no measured per-campaign net R; there is nothing
               to place inside the control distribution'
```

The NO_EDGE case is a treatment that is itself a draw from the same pool (seed 999): it sits
at the 48th percentile of the null with `p = 0.529` and is reported `NO_EDGE`, not `PASS`.
The far-outside case is all-5.0R campaigns: `p = 1/51 = 0.019608`, the smallest value a
50-draw battery can attain, and `PASS`. A 10-draw battery is refused as
`INCONCLUSIVE/P_RESOLUTION_INSUFFICIENT` because its floor is `1/11 = 0.0909 > 0.05`
(`test_p_value_resolution_is_enforced`).

### Regime split of the authorised development tape

```
$ PYTHONPATH=src .venv/bin/python - <<'PY'   # load_bars + label_regimes, default config
TAPE bars=2160 warmup=40
  TREND_UP     333
  TREND_DOWN   329
  RANGE       1239
  HIGH_VOL     219
  UNLABELLED    40
SUM 2160 == bars 2160
PIT-PREFIX-VIOLATIONS 0 []      # every one of the 2160 prefixes reproduced its own labels
```

Source: `research/tape/btcusdt-1h-2025Q1/tape.jsonl` via
`v8_next.research.screen.load_bars`. No campaign exists on that tape for a V9 arm, so the
per-regime **arm** table is `NOT_RUN`; only the bar partition is measured here.

## Tests

```
$ .venv/bin/python -m pytest -q tests/test_control_arms.py
52 passed

$ .venv/bin/python -m ruff check src/v8_next/evaluation/control_arms.py tests/test_control_arms.py
All checks passed!

$ .venv/bin/python -m ruff format --check src/v8_next/evaluation/control_arms.py tests/test_control_arms.py
(clean)

$ .venv/bin/python -m mypy src/v8_next/evaluation/control_arms.py tests/test_control_arms.py
Success: no issues found in 2 source files
```

Coverage of the acceptance list:

| Requirement | Test |
|---|---|
| Shared calendar/capital/venue/bracket/costs | `test_controls_carry_the_matched_dimensions` |
| Deliberately mismatched arm refused (9 dimensions) | `test_mismatched_matched_dimension_is_refused` |
| Tape / cost-model / exposure / uncosted refusal | `test_control_measured_on_another_tape_is_refused`, `test_control_priced_by_another_cost_model_is_refused`, `test_control_exposure_budget_must_equal_treatment_campaigns`, `test_uncosted_control_arm_is_refused` |
| Same seed → identical draws | `test_same_seed_reproduces_identical_draws` |
| Different seed → different draws | `test_different_seed_changes_the_draws` |
| No global RNG use | `test_generator_never_uses_the_global_rng` (module-level RNG entry points booby-trapped; `random.getstate()` unchanged) |
| Same-distribution treatment is `NO_EDGE` | `test_treatment_drawn_from_the_same_distribution_is_no_edge`, `test_treatment_at_the_control_mean_is_no_edge` |
| Outside the distribution → small empirical p | `test_far_outside_treatment_gets_the_smallest_attainable_p`, `test_inside_spread_with_two_exceedances_is_no_edge_not_pass` |
| Too few samples → `INCONCLUSIVE` | `test_too_few_campaigns_is_inconclusive`, `test_too_few_draws_is_inconclusive`, `test_p_value_resolution_is_enforced`, `test_sparse_regime_bucket_is_inconclusive` |
| Regime labels PIT + deterministic | `test_regime_labels_are_point_in_time`, `test_regime_labels_are_deterministic`, `test_regime_split_of_the_authorised_development_tape_is_pit_and_complete` |
| Per-regime counts sum to the total | `test_regime_counts_sum_to_the_total`, `test_per_regime_buckets_sum_to_the_total` |
| Absent treatment → `NOT_RUN` with reason | `test_absent_treatment_is_not_run_with_the_reason`, `test_uncosted_treatment_is_not_run` |

## Known gaps / residual risk

- **No treatment arm exists**, so no end-to-end number is published. The comparison API is
  exercised only against declared arithmetic fixtures; the first real comparison must come
  from a measured V9 arm measured through the same BacktestEngine profile as the controls.
- The control pool must be priced by whoever runs the random-entry replay (same tape, bracket,
  fees). This module records the claim and refuses a mismatched provenance; it cannot verify
  that the supplied `net_r` values are net of the declared costs — that check belongs to the
  replay that produced them.
- Regime splits assume the default 1-hour bar cadence for the lookbacks (20/20). A different
  cadence changes the meaning of "20 bars" and must be re-declared in `RegimeConfig`.
- Regime buckets compare per-campaign net R within the bucket; the per-draw exposure budget in
  a bucket varies (a draw may place few campaigns in a regime). The exposure match is enforced
  at the full-arm level, and bucket draw counts are reported, not smoothed.
- `HIGH_VOL` takes precedence over trend/range, so high-volatility trend bars are counted as
  `HIGH_VOL`. That precedence is deliberate and documented; an alternative partition would
  need its own preregistration.
