"""Open-world contracts for research understanding and planning."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from celllore.domain.models import ResearchRequirement, StrictModel


class ConstraintKind(StrEnum):
    HARD = "hard"
    PREFERENCE = "preference"
    EXCLUSION = "exclusion"
    UNKNOWN = "unknown"


class PlanOrigin(StrEnum):
    EXPLICIT = "explicit"
    INFERRED = "inferred"
    RECOMMENDED = "recommended"


class NeedPriority(StrEnum):
    ESSENTIAL = "essential"
    PREFERRED = "preferred"
    SUPPORTING = "supporting"


class PlanReadiness(StrEnum):
    READY = "ready"
    CONDITIONAL = "conditional"
    BLOCKED = "blocked"


class ExecutionStatus(StrEnum):
    SUPPORTED = "supported"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class SemanticConstraint(StrictModel):
    statement: str
    kind: ConstraintKind = ConstraintKind.UNKNOWN
    origin: PlanOrigin
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    rationale: str | None = None
    verification_question: str | None = None
    machine_predicate: str | None = None


class ResearchIntent(StrictModel):
    original_request: str
    intent_summary: str
    primary_question: str
    scientific_objectives: list[str] = Field(default_factory=list)
    hypotheses: list[str] = Field(default_factory=list)
    comparisons: list[str] = Field(default_factory=list)
    units_of_analysis: list[str] = Field(default_factory=list)
    desired_outcomes: list[str] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)


class DataNeed(StrictModel):
    need_id: str
    name: str
    data_type: str
    role: str
    priority: NeedPriority
    rationale: str
    modalities: list[str] = Field(default_factory=list)
    population: str | None = None
    conditions: list[str] = Field(default_factory=list)
    required_metadata: list[str] = Field(default_factory=list)
    quality_expectations: list[str] = Field(default_factory=list)
    constraints: list[SemanticConstraint] = Field(default_factory=list)
    alternatives: list[str] = Field(default_factory=list)
    execution_status: ExecutionStatus = ExecutionStatus.UNKNOWN
    supported_sources: list[str] = Field(default_factory=list)


class ResourceNeed(StrictModel):
    need_id: str
    resource_type: str
    purpose: str
    priority: NeedPriority
    search_focus: list[str] = Field(default_factory=list)
    execution_status: ExecutionStatus = ExecutionStatus.UNKNOWN
    supported_sources: list[str] = Field(default_factory=list)


class AnalysisStep(StrictModel):
    step_id: str
    title: str
    objective: str
    inputs: list[str] = Field(default_factory=list)
    methods: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)


class ResearchPlanDraft(StrictModel):
    intent: ResearchIntent
    constraints: list[SemanticConstraint] = Field(default_factory=list)
    data_needs: list[DataNeed] = Field(default_factory=list)
    resource_needs: list[ResourceNeed] = Field(default_factory=list)
    analysis_plan: list[AnalysisStep] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    clarifying_questions: list[str] = Field(default_factory=list)
    alternative_strategies: list[str] = Field(default_factory=list)
    readiness: PlanReadiness = PlanReadiness.CONDITIONAL
    readiness_rationale: str


class ResearchPlan(ResearchPlanDraft):
    plan_version: str = "research_plan_v1"
    executable_contracts: list[ResearchRequirement] = Field(default_factory=list)
    capability_gaps: list[str] = Field(default_factory=list)
    planner_diagnostics: list[str] = Field(default_factory=list)


class StudyStrategyDraft(StrictModel):
    study_design_summary: str
    constraints: list[SemanticConstraint] = Field(default_factory=list)
    data_needs: list[DataNeed] = Field(default_factory=list)
    resource_needs: list[ResourceNeed] = Field(default_factory=list)
    analysis_plan: list[AnalysisStep] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    alternative_strategies: list[str] = Field(default_factory=list)


class PlanCritique(StrictModel):
    intent_fidelity_issues: list[str] = Field(default_factory=list)
    scientific_design_issues: list[str] = Field(default_factory=list)
    feasibility_issues: list[str] = Field(default_factory=list)
    leakage_or_bias_risks: list[str] = Field(default_factory=list)
    recommended_changes: list[str] = Field(default_factory=list)
    critical_conflicts: list[str] = Field(default_factory=list)
