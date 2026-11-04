import pytest

from v8_next.evaluation.reference import DSRReference


def artifact():
    return dict(
        currency="USDT",
        convention="NEGATIVE_REFERENCE_RETURN_OVER_FIXED_INITIAL_CAPITAL",
        capital="100",
        selected_variant="a",
        registered_variants=["a", "b"],
        effective_independent_trials=2,
        independence_basis="test assumption",
        reference_basis="test-only fixture",
        source_identity="fixture",
        intervals=[
            dict(start_ns=i * 10, end_ns=(i + 1) * 10, available_ns=100, loss="-.01")
            for i in range(1, 5)
        ],
    )


def test_explicit_reference_artifact_maps_without_defaults():
    reference = DSRReference.model_validate(artifact())
    assert reference.plan().registered_variants == ("a", "b")
    assert len(reference.losses()) == 4
    assert str(reference.losses()[0].loss) == "-0.01"


@pytest.mark.parametrize(
    "field,value",
    [
        ("currency", "BTC"),
        ("capital", "0"),
        ("reference_basis", " "),
        ("source_identity", ""),
        ("effective_independent_trials", float("nan")),
        ("intervals", []),
    ],
)
def test_invalid_reference_metadata_rejects(field, value):
    source = artifact()
    source[field] = value
    with pytest.raises(ValueError):
        DSRReference.model_validate(source)


def test_missing_reference_loss_is_not_zero():
    source = artifact()
    del source["intervals"][0]["loss"]
    with pytest.raises(ValueError):
        DSRReference.model_validate(source)
