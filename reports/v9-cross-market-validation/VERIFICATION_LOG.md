# Independent verification log

This log records a second, independent execution of the frozen four-arm matrix from
the same verified tapes. It exists to answer one question: **are the published
numbers reproducible, or were they produced once and never checked?**

- Session start (UTC): `2026-11-05T12:04:48Z`
- Verification complete (UTC): see the final line of this file
- Frozen candidate: `09908af840864a20e5bc4836ea4d21f4ceb93b35`
- Validation branch: `validation/cross-market`
- Engine: NautilusTrader `2.0.0rc4`, Python `3.12.12`
- Accounts: independent 10,000 USDT book per symbol per arm

---

## 1. BTC development baseline still reproduces bit-for-bit

After every source change in this branch (instrument filters, exposure-id mapping,
research-only structural flags, campaign detail), the frozen BTC 2025-Q1 arms were
re-run with `research/tape/btcusdt-1h-2025Q1` and
`research/tape/btcusdt-15m-2025Q1`:

| Arm | Trades | Net PnL | Net R | Fees | Funding (booked) |
|---|---:|---:|---:|---:|---:|
| A — V8 static H1 | 44 | −21.234951 | −0.021638104 | 37.915357 | 0.36718638 |
| B — V9 legacy H1 | 24 | −23.102285 | −0.049251559 | 20.918686 | 0.36718638 |
| C — V9 multi-horizon static | 18 | −13.880205 | −0.045609209 | 15.617577 | 0.16675542 |
| D — V9 multi-horizon adaptive | 23 | **+20.162531** | **+0.052743995** | 20.585532 | 0.21907481 |

Every value matches the frozen report `reports/v9-adaptive-integration/ECONOMIC_COMPARISON.md`
to the printed precision. The defaults of the new optional arguments are therefore
semantically neutral for BTC.

## 2. Cross-market aggregate reproduced

A second full run of A/B/C/D over the ten symbols, using the same tapes, produced:

| Arm | Trades | Net PnL (sum of 10 books) | Total net R | Mean net R per symbol | Pooled net R per trade |
|---|---:|---:|---:|---:|---:|
| A | 1,308 | **+127.0654** | +6.7924 | +0.6792 | +0.0052 |
| B | 591 | **−393.6705** | −19.6673 | −1.9667 | −0.0333 |
| C | 531 | **+496.8774** | +25.0373 | +2.5037 | +0.0472 |
| D | 581 | **−157.3716** | −7.7939 | −0.7794 | −0.0134 |

The per-symbol mean is the registered view in `STATISTICAL_VALIDATION.md`; the
pooled per-trade value is shown only to make clear that D is negative under both
weightings. `PER_SYMBOL_RESULTS.csv` is the authoritative per-campaign source.

These totals agree with `PER_SYMBOL_RESULTS.csv` to the last published digit
(A +127.07, B −393.67, C +496.88, D −157.37). The verdict does not depend on a
single run.

## 3. Archive verification

Two distinct starting pools were found on disk, and both are now reconciled:

1. **Four-year 1h/funding pool** (`mac-storage-audit/.../research/tape/multi-1h-4y`):
   960 archives across 10 symbols and 48 months. For the 2025-H1 window it contains
   **120** archives (10 symbols × 6 months × 2 channels: 1h + fundingRate), all 120
   matching their vendor `.CHECKSUM`. It contains **no M15 channel** and **no
   DOTUSDT series**.
2. **Three-channel scratch pool** described in `DATA_MANIFEST.md`: 174 archives
   present, of which 40 failed the published digest (truncated transfers) and 6 were
   absent.

The validation downloaded the missing public archives (54 M15, 12 DOT 1h/funding, 6
DOT M15) and verified every one against `data.binance.vision`'s published
`.CHECKSUM`. The final 2025-H1 grid is **180/180 verified**:

| Channel | Archives | Verified |
|---|---:|---:|
| 1h klines | 60 | 60 |
| 15m klines | 60 | 60 |
| fundingRate | 60 | 60 |
| **Total** | **180** | **180** |

The earlier “174/180” note was therefore a statement about a different, partly
corrupt scratch pool, not about the final dataset. No truncated archive entered a
tape: `tools/build_tape.py` refuses any file absent from or mismatching the hash
manifest.

## 4. Funding attribution defect confirmed independently

The frozen `sim_arm._measure` sums funding from `engine.cache.positions()` after the
run. With NETTING position-id reuse, that cache holds only live records, so the
booked funding line undercounts closed campaigns. A validation-only observer read
each position inside `on_position_closed`, while it was still in the cache, and
accumulated every `FUNDING` adjustment.

ETHUSDT 2025-H1, four core arms:

| Arm | Booked funding (frozen) | Exact captured funding | Independent estimate in CSV | Net PnL |
|---|---:|---:|---:|---:|
| A | −0.1411 | **−4.5292** | −4.5886 | +44.3592 |
| B | −0.1411 | **−4.5043** | −4.6876 | −196.7702 |
| C | −0.0004 | **−4.1082** | −4.1545 | −125.3943 |
| D | −0.0004 | **−2.3691** | −2.2377 | +30.3989 |

The exact capture and the independent rate-integration estimate agree within
0.06–0.13 USDT per arm. This confirms that **`net_pnl` is correct** (the account
balance delta already includes funding) but the separate booked-funding column is
not. `EXECUTION_SAFETY.md` §5 states this limitation and uses the estimate only for
magnitude.

## 5. Rejected protective orders: scope of the observation

In the final core A/B/C/D matrix the persisted order-status census contains **zero
`OrderRejected` events**, matching `EXECUTION_SAFETY.md` §3.

A research-only ablation variant that is **not part of the verdict** did produce one
rejection. On DOTUSDT, the variant with structural geometry and no amendments
(`A1_no_amend`) submitted a `STOP_MARKET SELL` at `7.4333` when the market was
`7.3000`; NautilusTrader refused it as “in the market”, and that variant also
reported a zero-closed-campaign measurement anomaly. The variant is excluded from the
A/B/C/D and ablation tables for that reason.

The lesson is recorded rather than hidden: **a stop amendment computed on one bar
can be in-market by the time the next bar fills it**, especially on low-priced,
fast-moving contracts. The core adaptive arm had no such rejection on this sample,
but the mechanism is not risk-free and must be re-audited on any new window.

## 6. Mechanical checks

- Default test suite: **2,845 passed, 67 skipped, 512 deselected** (13.8 s).
- Target-identity and config digests are unchanged for BTC (`config_sha256` covers
  the two new research flags; both default `True`).
- No strategy threshold, stop percentage, target percentage, quorum, sizing formula
  or entry condition was changed.
- No credential, venue adapter, testnet or network order path was used.
- Sealed OOS was not accessed.

## 7. Conclusion of the verification

The four core arm results are reproducible, the frozen BTC baseline is bit-identical,
and the only material accounting defect found (funding attribution) does not change
`net_pnl`. The verdict in `FINAL_VERDICT.md` is therefore supported by two independent
executions, not one.

Verification complete (UTC): `2026-11-05T13:42:00Z` (session elapsed ≈ 1 h 37 min).
