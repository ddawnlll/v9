"""Controlled Oracle — Python port of v8-core/src/oracle (TARGET_ORACLE_SPEC §2, §8, §16).

Ported so the two systems share one vocabulary for counterfactuals: the four orthogonal
authority dimensions, the canonical fail-closed refusal codes, and the outcome type where
UNKNOWN is first-class and never collapses to a zero point estimate.
"""

from v8_next.oracle.artifacts import (
    OpportunityUniverseVersion,
    OracleEvaluationRecord,
)
from v8_next.oracle.authority import (
    CounterfactualAuthority,
    OracleOutcome,
    OracleOutcomeKind,
    OracleRefused,
)
from v8_next.oracle.coverage import (
    CoverageReceipt,
    ExpertProposal,
    OpportunityCoverageMember,
    UnrepresentedCluster,
    reconcile_coverage,
)
from v8_next.oracle.episode import (
    O0_EVALUATOR_ONLY,
    OracleDefinition,
    OracleEpisode,
    OracleEpisodeExtractor,
    OracleTier,
    o0_forward_excursion,
)
from v8_next.oracle.independence import (
    NegativeControlUniverse,
    OracleIndependenceReceipt,
    SubsetEvaluation,
    evaluate_oracle_independence,
)
from v8_next.oracle.information import (
    Feature,
    InformationAdapter,
    InformationField,
    InformationSet,
)
from v8_next.oracle.opportunity import (
    CandidateTemplate,
    Direction,
    GrammarCandidate,
    OpportunityGrammar,
    ParameterGrid,
    PredicateNode,
    PrimitiveDefinition,
    PrimitiveFamily,
    PrimitiveRegistry,
    TemplateRegistry,
    ValueRef,
)
from v8_next.oracle.recoverability import (
    RecoverabilityStageRecord,
    RecoverableGapWaterfall,
    compute_recoverability_chain,
)
from v8_next.oracle.support import (
    Action,
    SupportClassifier,
    SupportRule,
)
from v8_next.oracle.taxonomy import (
    AuditState,
    AuthorityError,
    AuthorityLevel,
    EconomicEvidenceStage,
    Identifiability,
    OracleContext,
    OracleRefusal,
    OracleRole,
    StatisticalVerdict,
    UnknownReasonCode,
    ValueNotion,
    VerificationDimension,
)
from v8_next.oracle.taxonomy import (
    CounterfactualAuthority as CounterfactualAuthorityLevel,
)
from v8_next.oracle.utility import (
    HardConstraints,
    ModelIds,
    ScalarPenalties,
    UtilityContract,
)

__all__ = [
    "Action",
    "AuditState",
    "AuthorityError",
    "AuthorityLevel",
    "CandidateTemplate",
    "CounterfactualAuthority",
    "CounterfactualAuthorityLevel",
    "CoverageReceipt",
    "Direction",
    "EconomicEvidenceStage",
    "ExpertProposal",
    "Feature",
    "GrammarCandidate",
    "HardConstraints",
    "Identifiability",
    "InformationAdapter",
    "InformationField",
    "InformationSet",
    "ModelIds",
    "NegativeControlUniverse",
    "O0_EVALUATOR_ONLY",
    "OpportunityCoverageMember",
    "OpportunityGrammar",
    "OpportunityUniverseVersion",
    "OracleContext",
    "OracleDefinition",
    "OracleEpisode",
    "OracleEpisodeExtractor",
    "OracleEvaluationRecord",
    "OracleIndependenceReceipt",
    "OracleOutcome",
    "OracleOutcomeKind",
    "OracleRefusal",
    "OracleRefused",
    "OracleRole",
    "OracleTier",
    "ParameterGrid",
    "PredicateNode",
    "PrimitiveDefinition",
    "PrimitiveFamily",
    "PrimitiveRegistry",
    "RecoverabilityStageRecord",
    "RecoverableGapWaterfall",
    "ScalarPenalties",
    "StatisticalVerdict",
    "SubsetEvaluation",
    "SupportClassifier",
    "SupportRule",
    "TemplateRegistry",
    "UnknownReasonCode",
    "UnrepresentedCluster",
    "UtilityContract",
    "ValueNotion",
    "ValueRef",
    "VerificationDimension",
    "compute_recoverability_chain",
    "evaluate_oracle_independence",
    "o0_forward_excursion",
    "reconcile_coverage",
]
