"""#459 — the P2 pillar publishes ONE bracket fact per decision, and both bracket readings.

The defect: `nx14_paper_trade_4y.py` derived bracketing from the **policy name** when it
replayed a decision (`has_bracket = protection_policy not in (None, "timeout-only-v1")`) while,
in the same loop iteration, it classified the same campaign's failure from the **decision**
(`has_bracket = has_bracket_policy and not bracketless`, where `bracketless` is the per-decision
fact `stop_price == entry_reference`). Measured on the frozen artifact: 916 of the 1,112
`plain_swing` campaigns are bracketless (the squeeze protection never formed), the published
exit histogram charged 914 of them a `STOP`, the published attribution charged them `EXIT`
("a loss on a campaign that carried no bracket"), and the headline `STOP` column was therefore
not a count of protective stops.

Acceptance (measurable; the artifact assertions skip when the artifact is absent):

A1. One bracket fact per decision: the flag handed to ``replay_bracket`` and the flag handed to
    ``classify_exit_failure`` for a published campaign are the same flag, so the published
    attribution can never describe a different contract than the published returns.
A2. The decision-contract reading charges no `STOP` to a bracketless campaign.
A3. The artifact publishes Reading A **and** Reading B with their campaign counts, declares
    which reading its published return columns use, and carries
    ``divergence_class_counts["BRACKET_CONTRACT_DIVERGENCE"] >= 916`` on tape ``b27891e9...``.
A4. The published return columns are unchanged against the frozen
    ``41ab7a0d...`` artifact and are reproducible from the artifact's own ``trades`` rows.

MECHANICS ONLY where the module loads the tool by hand; the artifact assertions read the
published four-year artifact and carry no evaluative weight of their own. Nothing here is an
economic claim.
"""

from __future__ import annotations

import collections
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from v8_next.system_proving.attribution import classify_exit_failure

V8_NEXT = Path(__file__).resolve().parents[1]
REPO_ROOT = V8_NEXT.parent
TOOL_SRC = V8_NEXT / "tools/nx14_paper_trade_4y.py"
ARTIFACT = REPO_ROOT / "docs/evidence/v87-r3/PAPER_4Y/paper_trade_4y.json"
BASELINE = REPO_ROOT / "docs/evidence/v87-r3/BASELINE/readiness_baseline.json"

READING_A = "READING_A_POLICY_NAME"
READING_B = "READING_B_DECISION_CONTRACT"
DIVERGENCE = "BRACKET_CONTRACT_DIVERGENCE"

#: The tape and the predecessor artifact this pillar is read against (issue #459).
TAPE_SHA256 = "b27891e917c38b59aaede1c07fe8316cd0600332389bbefcb667253448a18fa0"
FROZEN_ARTIFACT_SHA256 = "41ab7a0da11812818a67ce8f92709be083b1dd79c80ad4a7670d27a163eb32ec"

#: The published return columns of the frozen ``41ab7a0d...`` artifact, per policy. The
#: publication may add the bracket contract; it may not move one of these numbers.
FROZEN_PUBLISHED: dict[str, dict[str, Any]] = {
    "plain_swing": {
        "campaigns": 1112,
        "win_rate_pct": 6.56,
        "gross_return_sum": 0.16498982,
        "fee_cost_return_sum": -1.112,
        "funding_cost_return_sum": -0.009945,
        "net_measured_sum": -0.95695518,
        "net_with_modelled_slippage_10_sum": -4.66901776,
        "bracketless_campaigns": 916,
    },
    "causal_trend": {
        "campaigns": 1306,
        "win_rate_pct": 43.26,
        "gross_return_sum": 0.65886053,
        "fee_cost_return_sum": -1.306,
        "funding_cost_return_sum": -0.0193954,
        "net_measured_sum": -0.66653487,
        "net_with_modelled_slippage_10_sum": -2.62890401,
        "bracketless_campaigns": 1306,
    },
}

#: The published exit histogram of ``plain_swing`` in the frozen artifact: 1,037 STOP on a
#: contract that bracketed every decision, of which 914 are round-trips at the entry reference.
FROZEN_PLAIN_SWING_STOPS = {
    "stop_exits": 1037,
    "protective_stops": 123,
    "stop_exits_charged_to_decisions_without_protection": 914,
}


def _tool_module() -> Any:
    """Load the producer the way ``app/cli.py`` loads it (``exec_module``, not an import).

    The CLI's path does not register the module in ``sys.modules``, so the tool must stay
    loadable there: a module-level dataclass would raise on this line.
    """
    spec = importlib.util.spec_from_file_location("nx14_paper_trade_4y", TOOL_SRC)
    assert spec and spec.loader, f"cannot load {TOOL_SRC}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _artifact() -> dict[str, Any]:
    if not ARTIFACT.is_file():
        pytest.skip(f"canonical artifact absent: {ARTIFACT}")
    return json.loads(ARTIFACT.read_text())


# --------------------------------------------------------------------------- #
# A1 — one bracket fact per decision, one flag per reading
# --------------------------------------------------------------------------- #
def test_both_readings_are_derived_from_one_per_decision_fact() -> None:
    tool = _tool_module()
    assert tool.PUBLISHED_READING == tool.READING_A_POLICY_NAME == READING_A
    assert tool.READING_B_DECISION_CONTRACT == READING_B
    # a bracketed policy whose protection formed: both readings bracket
    assert tool.bracket_flags(declares_protection=True, bracketless=False) == {
        READING_A: True, READING_B: True,
    }
    # a bracketed policy whose protection did NOT form: the divergence, named per decision
    assert tool.bracket_flags(declares_protection=True, bracketless=True) == {
        READING_A: True, READING_B: False,
    }
    # a timeout-only policy: no bracket under either reading, so there is nothing to diverge
    assert tool.bracket_flags(declares_protection=False, bracketless=True) == {
        READING_A: False, READING_B: False,
    }


def test_every_published_campaign_was_classified_under_the_flag_its_replay_used() -> None:
    report = _artifact()
    checked = 0
    for family in report["families"].values():
        for index, row in enumerate(family["trades"]):
            assert "replayed_bracketed" in row, (
                "a published row must declare the bracket flag its replay used"
            )
            if row["failure_domain"] is None:
                continue
            expected = classify_exit_failure(
                exit_kind=row["exit_kind"],
                net_return=row["net_measured"],
                has_bracket=bool(row["replayed_bracketed"]),
            )
            assert row["failure_domain"] == expected.value, (
                f"campaign {index} was classified under a flag other than the one it was "
                f"replayed with: {row['failure_domain']} != {expected.value}"
            )
            checked += 1
        for row in family["trades"]:
            counter = row["counter_reading"]
            if counter is None or counter["failure_domain"] is None:
                continue
            expected = classify_exit_failure(
                exit_kind=counter["exit_kind"],
                net_return=counter["net_measured"],
                has_bracket=bool(counter["bracketed"]),
            )
            assert counter["failure_domain"] == expected.value, (
                "the counter reading must be classified under its own flag too"
            )
    assert checked, "no losing campaign to check: the pillar published no attribution"


def test_no_published_attribution_claims_a_bracket_its_campaign_lost_under() -> None:
    report = _artifact()
    for policy, family in report["families"].items():
        counts = family["failure_attribution"]["counts_by_domain"]
        exit_rows = [row for row in family["trades"] if row["failure_domain"] == "EXIT"]
        assert len(exit_rows) == counts.get("EXIT", 0), "the published counts are row-derived"
        # EXIT means "a loss on a campaign that carried no bracket"; the published contract
        # replayed every campaign WITH a bracket, so no campaign may be charged a STOP and an
        # EXIT at once (that is exactly the mixture #459 named).
        assert not [row for row in exit_rows if row["exit_kind"] == "STOP"], (
            f"{policy}: a campaign is charged STOP and EXIT/BRAKETLESS in the same reading"
        )
        counter = family["bracket_contract"]["reading_b"]["failure_attribution"]
        assert counter["conservation_verified"] is True
        # and the reading where bracketing IS the decision's property must not be deleting the
        # campaigns the published reading replayed unprotected: they are counted, not lost.
        assert (
            family["bracket_contract"]["reading_b"]["decisions_without_protection"]
            == family["bracketless_campaigns"]
        )


# --------------------------------------------------------------------------- #
# A2 — the decision-contract reading charges no STOP to a bracketless campaign
# --------------------------------------------------------------------------- #
def test_the_decision_contract_reading_never_stops_out_a_bracketless_campaign() -> None:
    report = _artifact()
    plain = report["families"]["plain_swing"]
    assert plain["bracketless_campaigns"] == FROZEN_PUBLISHED["plain_swing"]["bracketless_campaigns"]
    reading_b = plain["bracket_contract"]["reading_b"]
    assert reading_b["replayed_unprotected_campaigns"] == plain["bracketless_campaigns"]
    assert reading_b["stop_exits_charged_to_decisions_without_protection"] == 0
    assert reading_b["protective_stops"] == reading_b["stop_exits"]
    for index, row in enumerate(plain["trades"]):
        counter = row["counter_reading"]
        if not row["bracketless"]:
            assert counter is None, (
                f"campaign {index}: the two readings agree, so there is no counter row to publish"
            )
            continue
        assert counter is not None, f"campaign {index}: a bracketless campaign must be counted"
        assert counter["reading"] == READING_B and counter["bracketed"] is False
        assert counter["exit_kind"] != "STOP", (
            f"campaign {index}: a campaign whose protection never formed was stopped out at "
            "its own entry reference under the per-decision contract"
        )


# --------------------------------------------------------------------------- #
# A3 — both readings published, with counts, and the published one declared
# --------------------------------------------------------------------------- #
def test_the_artifact_publishes_both_readings_and_names_the_published_one() -> None:
    report = _artifact()
    assert report["tape"]["sha256"] == TAPE_SHA256
    assert report["bracket_contract"]["published_reading"] == READING_A
    assert report["bracket_contract"]["divergence_class"] == DIVERGENCE
    for policy, family in report["families"].items():
        contract = family["bracket_contract"]
        assert family["published_reading"] == READING_A
        assert READING_A in contract["published_reading_statement"]
        assert contract["reading_a"]["reading"] == READING_A
        assert contract["reading_b"]["reading"] == READING_B
        for reading in ("reading_a", "reading_b"):
            assert contract[reading]["basis"], f"{policy}: a reading must declare its basis"
            assert contract[reading]["campaigns"] == family["campaigns"]
            assert (
                contract[reading]["replayed_bracketed_campaigns"]
                + contract[reading]["replayed_unprotected_campaigns"]
                == family["campaigns"]
            )
        # the declaration is falsifiable: the published columns ARE reading A's numbers, and
        # reading B's are a different series rather than the same one renamed.
        assert contract["reading_a"]["net_measured_sum"] == family["net_measured_sum"]
        assert contract["reading_a"]["failure_attribution"] == family["failure_attribution"]
        assert report["bracket_contract"]["per_policy"][policy]["bracketless_campaigns"] == (
            family["bracketless_campaigns"]
        )
    plain = report["families"]["plain_swing"]
    assert plain["divergence_class_counts"][DIVERGENCE] >= 916
    assert plain["bracket_contract"]["reading_b"]["net_measured_sum"] != (
        plain["net_measured_sum"]
    ), "the two readings must be two different series, not one series published twice"
    # the headline STOP column is no longer readable as protective stops, in the artifact
    reading_a = plain["bracket_contract"]["reading_a"]
    for key, value in FROZEN_PLAIN_SWING_STOPS.items():
        assert reading_a[key] == value, f"reading A's {key} moved"
    assert reading_a["stop_exits"] == (
        reading_a["protective_stops"]
        + reading_a["stop_exits_charged_to_decisions_without_protection"]
    )


# --------------------------------------------------------------------------- #
# F3 — the divergence counter is reproducible from the artifact's own rows
# --------------------------------------------------------------------------- #
def test_the_divergence_counter_is_reproducible_from_the_artifacts_own_rows() -> None:
    report = _artifact()
    for policy, family in report["families"].items():
        rows = family["trades"]
        declared = family["divergence_class_counts"]
        from_rows = collections.Counter(
            name for row in rows for name in row["divergence_classes"]
        )
        assert dict(sorted(from_rows.items())) == declared, (
            f"{policy}: the declared divergence counts are not the rows' own"
        )
        # derivable a second way, from the two flags the rows carry and the policy's contract
        declares_protection = family["protection_policy"] not in (None, "timeout-only-v1")
        derived = sum(
            1
            for row in rows
            if bool(row["replayed_bracketed"]) != (declares_protection and not row["bracketless"])
        )
        assert derived == declared.get(DIVERGENCE, 0)
        assert sum(1 for row in rows if row["counter_reading"] is not None) == derived
        assert family["bracket_contract"]["readings_identical"] is (declared == {})


# --------------------------------------------------------------------------- #
# A4 — the published return columns, unchanged and row-reproducible
# --------------------------------------------------------------------------- #
def test_published_return_columns_are_unchanged_and_reproducible_from_the_rows() -> None:
    report = _artifact()
    for policy, frozen in FROZEN_PUBLISHED.items():
        family = report["families"][policy]
        for key, value in frozen.items():
            assert family[key] == value, f"{policy}.{key} moved off the frozen artifact"
        rows = family["trades"]
        assert len(rows) == family["campaigns"]
        assert round(sum(row["net_measured"] for row in rows), 8) == family["net_measured_sum"]
        assert round(sum(row["gross_return"] for row in rows), 8) == family["gross_return_sum"]
        assert round(sum(row["fee_cost_return"] for row in rows), 8) == (
            family["fee_cost_return_sum"]
        )
        assert round(sum(row["funding_cost_return"] for row in rows), 8) == (
            family["funding_cost_return_sum"]
        )
        assert round(sum(row["net_with_modelled_slippage_10"] for row in rows), 8) == (
            family["net_with_modelled_slippage_10_sum"]
        )
        wins = sum(1 for row in rows if row["net_measured"] > 0)
        assert round(100.0 * wins / len(rows), 2) == family["win_rate_pct"]
        assert family["conservation_verified"] is True
        receipt = family["system_proving_receipt"]
        assert receipt["attribution"] == family["failure_attribution"]
        assert receipt["total_campaigns"] == family["campaigns"]
        assert len(receipt["receipt_digest"]) == 64
        assert family["published_reading"] == READING_A


def test_the_readiness_baseline_still_pins_the_published_numbers() -> None:
    if not BASELINE.is_file():
        pytest.skip(f"baseline absent: {BASELINE}")
    report = _artifact()
    baseline = json.loads(BASELINE.read_text())
    for policy, pinned in baseline["per_policy"].items():
        family = report["families"][policy]
        assert pinned["net_measured_sum"] == family["net_measured_sum"]
        assert pinned["win_rate_pct"] == family["win_rate_pct"]
        assert pinned["campaigns"] == family["campaigns"]
        assert pinned["conservation_verified"] == family["conservation_verified"]
    assert baseline["claim_status"] == report["claim_status"] == "NO_ECONOMIC_CLAIM"
