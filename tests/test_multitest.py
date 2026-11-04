import pytest

from v8_next.evaluation.multitest import multiple_testing_correction


def test_multiple_testing_correction_standard_fdr_and_bonferroni():
    pytest.importorskip("statsmodels")
    pytest.importorskip("scipy")

    # Benjamini & Hochberg (1995) 15 p-values example
    pvals = [
        0.0001, 0.0004, 0.0019, 0.0095, 0.0201, 0.0278, 0.0298, 0.0344,
        0.0459, 0.3240, 0.4262, 0.5719, 0.6528, 0.7590, 1.0000,
    ]
    p_dict = {f"hyp_{i}": p for i, p in enumerate(pvals)}

    result = multiple_testing_correction(p_dict, alpha=0.05)

    assert result["family_size"] == 15
    assert result["alpha"] == 0.05
    assert not result["promotion_eligible"]
    assert result["claim_status"] == "NO_ECONOMIC_CLAIM"

    # Bonferroni: first 3 rejected at alpha=0.05
    bonf = result["adjustments"]["bonferroni"]
    assert bonf["rejected"]["hyp_0"] is True
    assert bonf["rejected"]["hyp_1"] is True
    assert bonf["rejected"]["hyp_2"] is True
    assert bonf["rejected"]["hyp_3"] is False
    assert bonf["adjusted_pvalues"]["hyp_0"] == pytest.approx(0.0001 * 15)

    # FDR BH: first 4 rejected at FDR q=0.05
    fdr_bh = result["adjustments"]["fdr_bh"]
    assert fdr_bh["rejected"]["hyp_0"] is True
    assert fdr_bh["rejected"]["hyp_1"] is True
    assert fdr_bh["rejected"]["hyp_2"] is True
    assert fdr_bh["rejected"]["hyp_3"] is True
    assert fdr_bh["rejected"]["hyp_4"] is False

    # Check that scipy cross-check matches statsmodels
    scipy_bh = result["scipy_cross_check"]["bh"]
    for k in p_dict:
        assert fdr_bh["adjusted_pvalues"][k] == pytest.approx(scipy_bh[k], abs=1e-7)

    # Check FDR BY cross check
    fdr_by = result["adjustments"]["fdr_by"]
    scipy_by = result["scipy_cross_check"]["by"]
    for k in p_dict:
        assert fdr_by["adjusted_pvalues"][k] == pytest.approx(scipy_by[k], abs=1e-7)


def test_sequence_input_and_identical_single_pvalue():
    pytest.importorskip("statsmodels")
    res = multiple_testing_correction([0.03], alpha=0.05)
    assert res["family_size"] == 1
    assert res["adjustments"]["bonferroni"]["adjusted_pvalues"]["trial_0"] == pytest.approx(0.03)
    assert res["adjustments"]["bonferroni"]["rejected"]["trial_0"] is True


def test_invalid_inputs_fail_closed():
    with pytest.raises(ValueError, match="empty"):
        multiple_testing_correction([])
    with pytest.raises(ValueError, match="empty"):
        multiple_testing_correction({})
    with pytest.raises(ValueError, match="alpha"):
        multiple_testing_correction([0.1], alpha=0.0)
    with pytest.raises(ValueError, match="alpha"):
        multiple_testing_correction([0.1], alpha=1.0)
    with pytest.raises(ValueError, match="finite"):
        multiple_testing_correction([0.1, float("nan")])
    with pytest.raises(ValueError, match="within"):
        multiple_testing_correction([-0.01, 0.5])
    with pytest.raises(ValueError, match="within"):
        multiple_testing_correction([0.5, 1.05])
    with pytest.raises(ValueError, match="unsupported"):
        multiple_testing_correction([0.1], methods=("invalid_method",))  # type: ignore[arg-type]
