# Open blockers and qualification

- **NOT_QUALIFIED**: published protection feed is buffered/reordered and the per-event audit finds violations; raw native callback identity, chronology, coverage, ACK and partial-fill reconciliation remain required.
- **INCOMPLETE**: adaptive thesis creation, recurrent evaluation, target actions and verified native protective amendments are not connected to the simulation. Cancel-first REDUCE/EXIT are blocked.
- **DATA_BLOCKED**: the checked-in authorized development tape is H1 only; genuine M15 source, availability provenance, and a multiresolution native runner are not yet verified. The experimental profiles are not wired into the simulation.
- **NOT_RUN**: staged C/D arms, matched controls, block-bootstrap comparison, PIT regime attribution, independent management audit, full resource qualification and marked-equity accounting.
- **INCONCLUSIVE**: 24 closed V9 trades are below the 30-trade preregistered floor; no edge claim.
- **TEST FAILURE**: `test_protection_audit_is_clean_over_500_bars` expects zero violations; 13 are observed after checking each published event.
- **ACCOUNTING**: funded versus unfunded runs change more than funding; settlement attribution difference is unresolved and comparison contract is enforced by compute_delta, but runners do not yet populate it.

No live order authority, sealed OOS access, or main merge is implied.

## Real M15 source check

Official Binance USDT-M BTCUSDT monthly `15m` archives were fetched for 2025-01, 02 and 03 from `https://data.binance.vision/data/futures/um/monthly/klines/BTCUSDT/15m/` with their `.CHECKSUM` files. Computed SHA-256 values matched the source checksum files: January `0867b0b8e22069621de45df42cc49a8df1b8217cf777529b1df6d43253927588`, February `44280cf80923ec6ee42e28e4606723b2f36909eab5c334ee5aee132ff989f12b`, March `aea345f1561a16710812658f2d435f0e02de5fc25d7ba8ad821526d67b2ab229`. The 8,640 bars have no 15-minute open-time gaps. Aggregated OHLCV was compared to all 2,160 checked-in H1 bars: two volume mismatches occur at open time 1738112400000 (M15 sum 3267.043, H1 3718.573) and 1738116000000 (M15 sum 3606.955, H1 3752.320); other compared OHLCV fields and hours match. The cause is unverified. Archives were stored under `/tmp/v9-m15-source`; their checksum is source integrity evidence, not historical receipt/publication timing. Native runner authorization, availability assumptions and data reconciliation remain unresolved; no M15 replay was performed.

Reproduce the OHLCV comparison with `uv run --locked --extra dev python tools/verify_m15_against_h1.py /tmp/v9-m15-source` after obtaining the three checked archives.
