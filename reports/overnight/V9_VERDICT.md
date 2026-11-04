# V9 scientific verdict and promotion matrix

This document is generated deterministically by `v8_next.evaluation.verdict.collect_from_artifacts`; it contains no clock, no network access and no live-trading authority.

**Overall verdict (worst of five): `BLOCKED`**

**Scientific verdict (MECHANICAL/PIT/ECONOMIC/RESOURCE): `NOT_RUN`**

**Live approved: `False`**; requires owner authorization: `True`.

## Dimensions

| Dimension | Verdict | Evidence (path + sha256) | Notes |
|---|---|---|---|
| MECHANICAL | PASS | `reports/provenance/baseline.json` `60fcbb285016fc23480c1c65190145a2f74f29ecd9a73139142d5766cb34477c`<br>`reports/overnight/evidence/M0-02.md` `5d6c59149fce430121c97fe1b1f2dd3a0cdb5e470afcb86162ad714851ae3cd3`<br>`reports/overnight/evidence/M0-03.md` `907df7def9a63dedf9ee376733677b260158aa9c51194399cd6f7486452517e9` | provenance code_sha: 0b69fc45c197354600256963249ca7b1f19b6bbd; baseline passed: 1089<br>lineage evidence present: M0-02 (two-campaign oracle), M0-03 (opportunity lineage)<br>recorded test-run log not present in this checkout; provenance baseline carries the recorded test counts |
| PIT | PASS | `reports/overnight/evidence/M1-01.md` `03cdb0724ec867e3a94f609fffffc4144d5de8363ee979d9a80b44a97238f49e`<br>`reports/overnight/evidence/M1-02.md` `6f4c999c4a5ae6923977756848b7cbadaeb17563be784e3d6ee576a1a8122d97`<br>`reports/overnight/evidence/M1-03.md` `4410db81b9d74774ed15218604deb8ebdbdfa41cf2cb1b6bf665fab43fd76f6e`<br>`reports/overnight/evidence/M1-04.md` `b2c1eb653418d9cef208f42f29d5c5e0fe28ad05193cb977e5a58bc25feabc22`<br>`reports/provenance/baseline.json` `60fcbb285016fc23480c1c65190145a2f74f29ecd9a73139142d5766cb34477c` | observation contract M1-01 and multi-horizon context M1-02 evidenced<br>belief state M1-03 and incremental update M1-04 evidenced<br>dataset(s) AVAILABLE: 1 |
| ECONOMIC | NOT_RUN | none | blocked prerequisite: no registered preregistration record (reports/evaluation/preregistration.json); M4-01 has not published one for this repository<br>blocked prerequisite: no costed economic result (M3-07 / M4-02) |
| RESOURCE | INCONCLUSIVE | `reports/overnight/evidence/receipts/M0-08-ensemble-baseline-receipt.json` `b3c9736682cb23cac4b79d5550df83f8db46bad7250f8910cd3bd13d72bcf56b`<br>`reports/overnight/evidence/receipts/M0-08-ensemble-candidate-receipt.json` `f516986b7472b3f3a1b1bbb67eff893edeec50130c2af864e76815a75de51700` | M0-08-ensemble-baseline-receipt.json: peak RSS 226344960 B within target but scope NOT_RUN: SCOPE_BELOW_TARGET: measured 1 leg(s) / 2.96 month(s), target is 40 legs / 12 months<br>M0-08-ensemble-candidate-receipt.json: peak RSS 230752256 B within target but scope NOT_RUN: SCOPE_BELOW_TARGET: measured 1 leg(s) / 2.96 month(s), target is 40 legs / 12 months |
| LIVE_APPROVAL | BLOCKED | none | blocked prerequisite: explicit owner authorization; this module has no authority to promote to live trading<br>live_approved is False by construction (requires_owner_authorization=True) |

## Blocked prerequisites

- blocked prerequisite: no registered preregistration record (reports/evaluation/preregistration.json); M4-01 has not published one for this repository
- blocked prerequisite: no costed economic result (M3-07 / M4-02)
- blocked prerequisite: explicit owner authorization; this module has no authority to promote to live trading

## Failed or inconclusive dimensions

- RESOURCE: INCONCLUSIVE (M0-08-ensemble-baseline-receipt.json: peak RSS 226344960 B within target but scope NOT_RUN: SCOPE_BELOW_TARGET: measured 1 leg(s) / 2.96 month(s), target is 40 legs / 12 months; M0-08-ensemble-candidate-receipt.json: peak RSS 230752256 B within target but scope NOT_RUN: SCOPE_BELOW_TARGET: measured 1 leg(s) / 2.96 month(s), target is 40 legs / 12 months)

## Next step

Run the missing measurement(s) for ECONOMIC. For ECONOMIC that means a registered preregistration, a permitted dataset role and a costed replay (M3-07 / M4-02); no edge claim is made before then. LIVE_APPROVAL remains BLOCKED pending explicit owner authorization.

## Promotion policy

- `live_approved` is defined as `False` by construction; this module has no assignment path that can set it `True`.
- `requires_owner_authorization` is `True`; only the repository owner may authorize live trading, outside this artifact.
- The overall verdict is the worst dimension under the fail-closed ordering `BLOCKED > NOT_RUN > FAIL > INCONCLUSIVE > NO_EDGE > PASS`.

