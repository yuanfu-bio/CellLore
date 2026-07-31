"""Structured, evidence-safe research requirement compilation."""

from __future__ import annotations

import json
import re
from typing import Any, Protocol

from pydantic import Field

from scdata_agent.domain.models import (
    AccessLevel,
    DataRole,
    EvaluationPolicy,
    Modality,
    ResearchRequirement,
    StrictModel,
)
from scdata_agent.domain.requirements import (
    TASK_ALIASES,
    RuleBasedRequirementCompiler,
    restrict_modalities_to_pairing,
    roles_for_modalities,
)


class ChatCompletionClient(Protocol):
    async def complete(self, messages: list[dict[str, object]], *, temperature: float = 0.1, max_tokens: int = 2_000) -> str: ...


class LLMRequirementDraft(StrictModel):
    organisms: list[str] = Field(default_factory=list)
    tissues: list[str] = Field(default_factory=list)
    diseases: list[str] = Field(default_factory=list)
    cell_types: list[str] = Field(default_factory=list)
    modalities: list[Modality] = Field(default_factory=list)
    acceptable_modalities: list[Modality] = Field(default_factory=list)
    preferred_modalities: list[Modality] = Field(default_factory=list)
    required_data_roles: list[DataRole] = Field(default_factory=list)
    preferred_data_roles: list[DataRole] = Field(default_factory=list)
    minimum_donors: int | None = Field(default=None, ge=1)
    excluded_access_levels: list[AccessLevel] = Field(default_factory=list)
    excluded_biological_sources: list[str] = Field(default_factory=list)
    downstream_tasks: list[str] = Field(default_factory=list)
    evaluation_policy: EvaluationPolicy = Field(default_factory=EvaluationPolicy)
    unresolved_constraints: list[str] = Field(default_factory=list)
    search_terms: list[str] = Field(default_factory=list)


class RequirementCritique(StrictModel):
    missing_constraints: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    revised_draft: LLMRequirementDraft | None = None


class StepFunRequirementCompiler:
    """Compile user intent with native structured output and bounded review."""

    def __init__(self, client: ChatCompletionClient, fallback: RuleBasedRequirementCompiler | None = None, *, structured_model: Any | None = None, critic_client: ChatCompletionClient | None = None) -> None:
        self.client = client
        self.fallback = fallback or RuleBasedRequirementCompiler()
        self.structured_model = structured_model
        self.critic_client = critic_client

    async def compile(self, question: str) -> ResearchRequirement:
        baseline = self.fallback.compile(question)
        diagnostics: list[str] = []
        draft: LLMRequirementDraft | None = None
        if self.structured_model is not None:
            try:
                draft = await self._native_structured_draft(question)
                diagnostics.append("planner:native_structured_output")
            except Exception as error:
                diagnostics.append(_diagnostic("planner:native_structured_failed", error))
        if draft is None:
            try:
                draft = LLMRequirementDraft.model_validate(_json_object(await self.client.complete(_requirement_messages(question), temperature=0.0, max_tokens=1_600)))
                diagnostics.append("planner:json_fallback")
            except Exception as error:
                return baseline.model_copy(update={
                    "compiler": "rule_based_v1_fallback",
                    "compiler_warnings": ["Planner model extraction failed; deterministic fallback was used."],
                    "planner_diagnostics": [*diagnostics, _diagnostic("planner:json_fallback_failed", error)],
                })

        compiled = _merge_requirement_draft(baseline, draft, self.client, diagnostics)
        if self.critic_client is None:
            return compiled
        try:
            critique = RequirementCritique.model_validate(_json_object(await self.critic_client.complete(_critic_messages(question, compiled), temperature=0.0, max_tokens=1_400)))
            revised = _merge_requirement_draft(baseline, critique.revised_draft, self.client, [*compiled.planner_diagnostics, "critic:applied"]) if critique.revised_draft else compiled
            return revised.model_copy(update={
                "planner_diagnostics": [*revised.planner_diagnostics, *(f"critic:missing:{item}" for item in critique.missing_constraints), *(f"critic:conflict:{item}" for item in critique.conflicts)],
            })
        except Exception as error:
            return compiled.model_copy(update={"planner_diagnostics": [*compiled.planner_diagnostics, _diagnostic("critic:failed", error)]})

    async def _native_structured_draft(self, question: str) -> LLMRequirementDraft:
        structured = self.structured_model.with_structured_output(LLMRequirementDraft)
        result = await structured.ainvoke(_requirement_messages(question))
        return LLMRequirementDraft.model_validate(result)


def _requirement_messages(question: str) -> list[dict[str, object]]:
    return [{"role": "system", "content": "Extract an executable public single-cell epigenomics search requirement. Return only fields explicitly stated by the user or direct standard normalizations. Preserve must/prefer/exclude/test-set distinctions. Do not invent biology, access, donors, files, or availability."}, {"role": "user", "content": json.dumps({"question": question, "json_schema": LLMRequirementDraft.model_json_schema()}, ensure_ascii=False)}]


def _critic_messages(question: str, requirement: ResearchRequirement) -> list[dict[str, object]]:
    return [{"role": "system", "content": "Audit whether an executable requirement preserves every explicit user constraint. Return only JSON matching the supplied schema. revised_draft may correct omissions but may not invent constraints."}, {"role": "user", "content": json.dumps({"question": question, "compiled_requirement": requirement.model_dump(mode="json"), "json_schema": RequirementCritique.model_json_schema()}, ensure_ascii=False)}]


def _json_object(response: str) -> dict[str, object]:
    content = response.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content).strip()
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end < start:
        raise ValueError("response did not contain a JSON object")
    decoded = json.loads(content[start:end + 1])
    if not isinstance(decoded, dict):
        raise ValueError("response JSON must be an object")
    return decoded


def _merge_requirement_draft(baseline: ResearchRequirement, draft: LLMRequirementDraft | None, client: ChatCompletionClient, diagnostics: list[str]) -> ResearchRequirement:
    if draft is None:
        return baseline
    acceptable = _unique([*baseline.acceptable_modalities, *baseline.modalities, *draft.modalities, *draft.acceptable_modalities])
    # The LLM draft can reintroduce a modality the rule-based baseline already
    # excluded for a same-cell pairing requirement (e.g. standalone scATAC);
    # narrowing must be re-applied after the union, not only inside the baseline.
    acceptable = restrict_modalities_to_pairing(acceptable, baseline.minimum_pairing_level)
    preferred = _unique([*baseline.preferred_modalities, *draft.preferred_modalities])
    required_roles, preferred_roles = roles_for_modalities(acceptable)
    required_roles = _unique([*required_roles, *baseline.required_data_roles, *draft.required_data_roles])
    preferred_roles = _unique([*preferred_roles, *baseline.preferred_data_roles, *draft.preferred_data_roles])
    min_donors = baseline.minimum_donors or draft.minimum_donors
    hard = list(baseline.hard_constraints)
    if min_donors:
        hard.append(f"minimum_donors>={min_donors}")
    if baseline.public_only:
        hard.append("public_only")
    model = getattr(getattr(client, "settings", None), "model", "configured")
    return baseline.model_copy(update={
        "organisms": _safe_terms([*baseline.organisms, *draft.organisms]),
        "tissues": _safe_terms([*baseline.tissues, *draft.tissues]),
        "diseases": _safe_terms([*baseline.diseases, *draft.diseases]),
        "cell_types": _safe_terms([*baseline.cell_types, *draft.cell_types]),
        "modalities": acceptable, "acceptable_modalities": acceptable, "preferred_modalities": preferred,
        "required_data_roles": required_roles, "preferred_data_roles": preferred_roles,
        "minimum_donors": min_donors,
        "excluded_access_levels": _unique([*baseline.excluded_access_levels, *draft.excluded_access_levels]),
        "excluded_biological_sources": _safe_terms([*baseline.excluded_biological_sources, *draft.excluded_biological_sources]),
        "evaluation_policy": _merge_evaluation(baseline.evaluation_policy, draft.evaluation_policy),
        "downstream_tasks": _normalize_tasks([*baseline.downstream_tasks, *draft.downstream_tasks]),
        "hard_constraints": _safe_terms(hard),
        "unresolved_constraints": _safe_terms([*baseline.unresolved_constraints, *draft.unresolved_constraints]),
        "search_terms": _safe_terms([*baseline.search_terms, *draft.search_terms]),
        # The LLM's own free-text search terms extend recall for technologies
        # the modality enum cannot yet name, without waiting on an alias-table
        # update; QueryPlanner ORs a bounded number of these into the query.
        "supplementary_search_terms": _safe_terms(
            [*baseline.supplementary_search_terms, *draft.search_terms]
        ),
        "compiler": f"stepfun_structured_v2:{model}", "compiler_warnings": [], "planner_diagnostics": diagnostics,
    })


def _merge_evaluation(base: EvaluationPolicy, draft: EvaluationPolicy) -> EvaluationPolicy:
    return EvaluationPolicy(reserve_independent_studies=base.reserve_independent_studies or draft.reserve_independent_studies, holdout_unit=base.holdout_unit or draft.holdout_unit, prevent_lineage_leakage=base.prevent_lineage_leakage or draft.prevent_lineage_leakage)


def _normalize_tasks(values: list[str]) -> list[str]:
    aliases = {re.sub(r"[_-]+", " ", alias.casefold()).strip(): canonical for canonical, names in TASK_ALIASES.items() for alias in (canonical, *names)}
    return _safe_terms([aliases.get(re.sub(r"[_-]+", " ", value.casefold()).strip(), value) for value in values])


def _safe_terms(values: list[str]) -> list[str]:
    return _unique([re.sub(r"[\x00-\x1f\"']", " ", str(value)).strip() for value in values if str(value).strip()])[:40]


def _unique(values: list[Any]) -> list[Any]:
    return list(dict.fromkeys(value for value in values if value))


def _diagnostic(stage: str, error: Exception) -> str:
    return f"{stage}:{type(error).__name__}:{str(error)[:180]}"
