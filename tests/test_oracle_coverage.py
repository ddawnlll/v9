"""Coverage reconciliation + independence contracts (MECHANICS ONLY).

Hand-written two-candidate fixtures exercising the receipt arithmetic and the negative
controls; no assertion carries evaluative weight. The real-window receipt sample lives in
`docs/evidence/v87-port-chain/452/`.
"""

from __future__ import annotations

from v8_next.oracle import (
    AuthorityLevel,
    CandidateTemplate,
    Direction,
    ExpertProposal,
    GrammarCandidate,
    InformationField,
    InformationSet,
    OpportunityGrammar,
    OpportunityUniverseVersion,
    OracleContext,
    OracleRole,
    ParameterGrid,
    PredicateNode,
    PrimitiveDefinition,
    PrimitiveFamily,
    PrimitiveRegistry,
    SupportClassifier,
    TemplateRegistry,
    UnrepresentedCluster,
    ValueNotion,
    ValueRef,
    evaluate_oracle_independence,
    reconcile_coverage,
)


def _grammar(direction: Direction) -> OpportunityGrammar:
    predicate = PredicateNode.above("close", ValueRef.parameter("threshold"))
    template = CandidateTemplate(
        template_id="template-breakout" if direction is Direction.LONG else "template-reversal",
        mechanism_family_id="price",
        behavior_family_id="breakout",
        habitat_predicate=predicate,
        setup_predicate=predicate,
        trigger_predicate=predicate,
        direction=direction,
        invalidation="stop-v1",
        expiry="24bar",
        risk_geometry={},
        parameter_names=("threshold",),
    )
    return OpportunityGrammar(
        version="predicate-ir-v1",
        primitives=PrimitiveRegistry(
            version="registered-v1",
            primitives={
                "close": PrimitiveDefinition(
                    family=PrimitiveFamily.PRICE_RETURN, source_version="state-v1"
                )
            },
            allowed_operators=frozenset({"ABOVE"}),
        ),
        templates=TemplateRegistry(version="template-v1", templates=(template,)),
        grid=ParameterGrid(
            grid_id="grid-v1", values={"threshold": (100.0,)}
        ),
    )


def _universe(grammar: OpportunityGrammar) -> OpportunityUniverseVersion:
    universe = OpportunityUniverseVersion(
        version="1",
        parent_universe_id=None,
        instrument_universe=["BTCUSDT"],
        timeframe_set=["1h"],
        information_contract_id="pit-v1",
        primitive_registry_hash=grammar.primitives.identity(),
        predicate_ir_version=grammar.version,
        behavior_template_registry_hash=grammar.templates.identity(),
        parameter_grid_hash=grammar.grid.identity(),
        tradability_rule_id="detect-v1",
        support_rule_id="canonical-l1-support-v1",
        authority_contract_id="l1-v1",
        search_universe_size=grammar.search_universe_size(1, 1),
        complexity_budget=1,
        created_at=0,
        code_hash="code-v1",
        execution_mode_id="canonical-l1",
    )
    universe.bind_identity()
    return universe


def _context(universe: OpportunityUniverseVersion) -> OracleContext:
    return OracleContext(
        role=OracleRole.HINDSIGHT,
        authority=AuthorityLevel.L1,
        information_contract_id=universe.information_contract_id,
        opportunity_universe_id=universe.universe_id,
        utility_contract_id="utility-v1",
        policy_class_id="policy-v1",
        cost_model_id="cost-v1",
        capacity_model_id="cap-v1",
        environment_target_id="binance-usdt-perp-l1",
    )


def _candidate(
    universe_id: str, template: str, direction: Direction, decision_time: int = 1000
) -> GrammarCandidate:
    candidate = GrammarCandidate(
        grammar_candidate_id="",
        universe_id=universe_id,
        template_id=template,
        instrument="BTCUSDT",
        timeframe="1h",
        direction=direction,
        decision_time=decision_time,
        parameters={},
    )
    candidate.bind_identity()
    return candidate


def test_grammar_generate_binds_universe_and_sorts() -> None:
    grammar = _grammar(Direction.LONG)
    universe = _universe(grammar)
    information = InformationSet(decision_time=100)
    information.insert(
        InformationField(
            name="close",
            value=101.0,
            event_time=99,
            knowledge_time=100,
            availability_time=100,
            source_id="state",
            source_version="v1",
        )
    )
    candidates = grammar.generate(universe, information)
    assert len(candidates) == 1
    assert candidates[0].direction is Direction.LONG
    assert candidates[0].universe_id == universe.universe_id
    assert OpportunityGrammar.population_hash(candidates) == OpportunityGrammar.population_hash(
        list(reversed(candidates))
    )


def test_grammar_refuses_hash_mismatch() -> None:
    grammar = _grammar(Direction.LONG)
    universe = _universe(grammar)
    universe.primitive_registry_hash = "tampered"
    universe.bind_identity()
    information = InformationSet(decision_time=100)
    from v8_next.oracle.authority import OracleRefused

    try:
        grammar.generate(universe, information)
    except OracleRefused as exc:
        assert exc.refusal.value == "INSUFFICIENT_SUPPORT"
    else:
        raise AssertionError("hash mismatch must refuse")


def test_every_numerator_member_reconciles_to_supported_denominator() -> None:
    grammar = _grammar(Direction.LONG)
    universe = _universe(grammar)
    context = _context(universe)
    classifier = SupportClassifier.canonical_l1()
    candidates = [
        _candidate(universe.universe_id, "template-breakout", Direction.LONG),
        _candidate(universe.universe_id, "template-reversal", Direction.SHORT),
    ]
    proposals = [
        ExpertProposal(expert_id="bollinger_breakout", decision="CANDIDATE", direction=Direction.LONG)
    ]
    receipt, records = reconcile_coverage(
        universe, candidates, classifier, proposals, 30, AuthorityLevel.L1, context, "lineage-01"
    )
    assert receipt.total_opportunity_count == 2
    assert receipt.supported_opportunity_count == 2
    assert receipt.represented_supported_count == 1
    assert receipt.unrepresented_supported_count == 1
    assert receipt.representational_coverage == 0.5
    assert receipt.representational_coverage_gap == 0.5
    assert receipt.claim == "NO_ECONOMIC_CLAIM"
    assert len(records) == 2
    for member in receipt.members:
        if member.is_represented:
            assert member.is_supported
    assert receipt.unrepresented_clusters == [
        UnrepresentedCluster(
            template_id="template-reversal", direction=Direction.SHORT, count=1
        )
    ]
    assert receipt.receipt_id == receipt.identity()
    supported_record = records[0]
    assert supported_record.point_estimate == 0.0
    assert supported_record.value_notion is ValueNotion.RETROSPECTIVE


def test_independence_negative_controls() -> None:
    grammar = _grammar(Direction.LONG)
    universe = _universe(grammar)
    candidates = [
        _candidate(universe.universe_id, "template-breakout", Direction.LONG, 1000 + i * 3600)
        for i in range(10)
    ]
    proposals = {
        "ExpertA": {(1000 + i * 3600, Direction.LONG, "BTCUSDT") for i in range(7)},
        "ExpertB": {(1000 + i * 3600, Direction.LONG, "BTCUSDT") for i in range(4, 10)},
    }
    receipt, negative = evaluate_oracle_independence("test_u", candidates, proposals)
    assert receipt.total_universe_opportunities == 10
    assert receipt.population_invariance_verified
    assert receipt.unique_contribution_formula_verified
    assert receipt.synthetic_gap_detection_verified
    assert receipt.permutation_invariance_verified
    assert receipt.status == "INDEPENDENCE_VERIFIED"
    assert receipt.claim == "NO_ECONOMIC_CLAIM"
    assert negative.degraded_coverage < negative.baseline_coverage
    assert negative.synthetic_injected_count == 100
