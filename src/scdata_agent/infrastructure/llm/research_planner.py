"""Multi-stage LLM workflow for open-world research planning."""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any, Protocol, TypedDict, TypeVar

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from scdata_agent.domain.identifiers import stable_id
from scdata_agent.domain.models import DataRole, Modality, ResearchRequirement
from scdata_agent.domain.planning import (
    AnalysisStep,
    ConstraintKind,
    DataNeed,
    ExecutionStatus,
    NeedPriority,
    PlanCritique,
    PlanOrigin,
    PlanReadiness,
    ResearchIntent,
    ResearchPlan,
    ResearchPlanDraft,
    ResourceNeed,
    SemanticConstraint,
    StudyStrategyDraft,
)
from scdata_agent.domain.requirements import RuleBasedRequirementCompiler


class ChatCompletionClient(Protocol):
    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.1,
        max_tokens: int = 2_000,
    ) -> str: ...


class PlannerState(TypedDict, total=False):
    question: str
    capabilities: dict[str, Any]
    intent: ResearchIntent
    strategy: StudyStrategyDraft
    critique: PlanCritique
    draft: ResearchPlanDraft
    fast_diagnostics: list[str]


T = TypeVar("T", bound=BaseModel)


class CompactConstraint(BaseModel):
    statement: str = Field(max_length=300)
    kind: ConstraintKind = ConstraintKind.UNKNOWN
    origin: PlanOrigin
    rationale: str | None = Field(default=None, max_length=300)


class CompactDataNeed(BaseModel):
    name: str = Field(max_length=120)
    data_type: str = Field(max_length=120)
    role: str = Field(max_length=120)
    priority: NeedPriority
    rationale: str = Field(max_length=300)
    modalities: list[str] = Field(default_factory=list, max_length=5)
    required_metadata: list[str] = Field(default_factory=list, max_length=8)
    alternatives: list[str] = Field(default_factory=list, max_length=3)


class CompactResourceNeed(BaseModel):
    resource_type: str = Field(max_length=120)
    purpose: str = Field(max_length=300)
    priority: NeedPriority
    search_focus: list[str] = Field(default_factory=list, max_length=6)


class CompactAnalysisStep(BaseModel):
    title: str = Field(max_length=120)
    objective: str = Field(max_length=300)
    inputs: list[str] = Field(default_factory=list, max_length=6)
    methods: list[str] = Field(default_factory=list, max_length=6)
    outputs: list[str] = Field(default_factory=list, max_length=6)
    success_criteria: list[str] = Field(default_factory=list, max_length=4)


class CompactIntentDraft(BaseModel):
    intent_summary: str = Field(max_length=500)
    primary_question: str = Field(max_length=300)
    scientific_objectives: list[str] = Field(default_factory=list, max_length=4)
    hypotheses: list[str] = Field(default_factory=list, max_length=4)
    comparisons: list[str] = Field(default_factory=list, max_length=4)
    units_of_analysis: list[str] = Field(default_factory=list, max_length=4)
    desired_outcomes: list[str] = Field(default_factory=list, max_length=4)
    ambiguities: list[str] = Field(default_factory=list, max_length=4)


class CompactStrategyDraft(BaseModel):
    constraints: list[CompactConstraint] = Field(default_factory=list, max_length=8)
    data_needs: list[CompactDataNeed] = Field(default_factory=list, max_length=5)
    resource_needs: list[CompactResourceNeed] = Field(default_factory=list, max_length=5)
    analysis_plan: list[CompactAnalysisStep] = Field(default_factory=list, max_length=6)
    assumptions: list[str] = Field(default_factory=list, max_length=5)
    uncertainties: list[str] = Field(default_factory=list, max_length=5)
    clarifying_questions: list[str] = Field(default_factory=list, max_length=4)
    alternative_strategies: list[str] = Field(default_factory=list, max_length=4)
    readiness: PlanReadiness = PlanReadiness.CONDITIONAL
    readiness_rationale: str = Field(max_length=300)


DEFAULT_CAPABILITIES: dict[str, Any] = {
    "literature": {"status": "supported", "sources": ["europe_pmc"]},
    "single_cell_epigenomics": {
        "status": "supported",
        "modalities": ["scATAC-seq", "snATAC-seq", "RNA+ATAC multiome"],
        "sources": ["ncbi_geo", "ncbi_sra"],
    },
    "other_public_resources": {
        "status": "planning_only",
        "examples": [
            "scRNA-seq",
            "spatial omics",
            "bulk omics",
            "clinical cohorts",
            "perturbation data",
            "ontologies",
            "protocols",
            "software",
            "pretrained models",
        ],
    },
}


class MultiStageResearchPlanner:
    """Interpret scientific intent, design resources, critique, then compile execution."""

    def __init__(
        self,
        planner_client: ChatCompletionClient,
        *,
        review_client: ChatCompletionClient | None = None,
        fallback: RuleBasedRequirementCompiler | None = None,
        capabilities: dict[str, Any] | None = None,
        mode: str | None = None,
    ) -> None:
        self.planner_client = planner_client
        self.review_client = review_client or planner_client
        self.fallback = fallback or RuleBasedRequirementCompiler()
        self.capabilities = capabilities or DEFAULT_CAPABILITIES
        self.mode = (mode or os.getenv("SCDATA_AGENT_PLANNER_MODE", "fast")).lower()
        if self.mode not in {"fast", "deep"}:
            raise ValueError("Planner mode must be fast or deep.")
        self.graph = self._build_graph()

    def _build_graph(self):
        graph = StateGraph(PlannerState)
        if self.mode == "fast":
            graph.add_node("direct_plan", self._direct_plan)
            graph.add_edge(START, "direct_plan")
            graph.add_edge("direct_plan", END)
            return graph.compile()
        graph.add_node("intent_analysis", self._intent_analysis)
        graph.add_node("study_design", self._study_design)
        graph.add_node("critical_review", self._critical_review)
        graph.add_node("synthesis", self._synthesis)
        graph.add_edge(START, "intent_analysis")
        graph.add_edge("intent_analysis", "study_design")
        graph.add_edge("study_design", "critical_review")
        graph.add_edge("critical_review", "synthesis")
        graph.add_edge("synthesis", END)
        return graph.compile()

    async def plan(self, question: str) -> ResearchPlan:
        try:
            timeout_seconds = float(
                os.getenv(
                    "SCDATA_AGENT_PLANNER_TIMEOUT_SECONDS",
                    "35" if self.mode == "fast" else "600",
                )
            )
            async with asyncio.timeout(timeout_seconds):
                state = await self.graph.ainvoke(
                    {"question": question, "capabilities": self.capabilities},
                    config={"tags": ["scdata-agent", "research-planner", self.mode]},
                )
            draft = ResearchPlanDraft.model_validate(state["draft"])
            if self.mode == "fast":
                draft = _validate_fast_draft(question, draft, self.fallback)
                diagnostics = [
                    "planner:fast_parallel_plan",
                    *state.get("fast_diagnostics", []),
                    "planner:deterministic_validation",
                ]
            else:
                diagnostics = [
                    "planner:intent_analysis",
                    "planner:study_design",
                    "planner:critical_review",
                    "planner:synthesis",
                ]
            return _finalize_plan(question, draft, self.fallback, self.capabilities, diagnostics)
        except Exception as error:
            return fallback_research_plan(
                question,
                self.fallback,
                f"planner:degraded:{type(error).__name__}:{str(error)[:180]}",
            )

    async def _direct_plan(self, state: PlannerState) -> dict[str, Any]:
        intent_call = _structured_call(
            self.planner_client,
            CompactIntentDraft,
            (
                "You are a senior biomedical scientist. Deeply interpret the user's research goal even "
                "when no data type is specified. Identify the primary scientific question, objectives, "
                "testable hypotheses, comparisons, units of analysis, desired outcomes, and genuine "
                "ambiguities. Do not design the data workflow in this response. Be concise, use at most "
                "4 items per list, answer in the same language as the user, and return JSON only."
            ),
            {"question": state["question"]},
            max_tokens=1_800,
        )
        strategy_call = _structured_call(
            self.planner_client,
            CompactStrategyDraft,
            (
                "You are a biomedical study designer and data architect. From the user's research question, "
                "design the most appropriate verifiable public-resource strategy. Recommend primary, validation, "
                "clinical, spatial, perturbation, literature, ontology, protocol, software, atlas, or model resources "
                "when justified. Mark constraints explicit, inferred, or recommended; never claim resources exist. "
                "Provide at most 5 data needs, 5 resource needs, and 6 ordered analysis steps. Missing details should "
                "normally be assumptions rather than blockers. Keep every field concise, answer in the same "
                "language as the user, and return JSON only."
            ),
            {
                "question": state["question"],
                "available_capabilities": state["capabilities"],
            },
            max_tokens=2_600,
        )
        intent_task = asyncio.create_task(intent_call)
        strategy_task = asyncio.create_task(strategy_call)
        branch_timeout = float(
            os.getenv("SCDATA_AGENT_PLANNER_BRANCH_TIMEOUT_SECONDS", "30")
        )
        done, pending = await asyncio.wait(
            {intent_task, strategy_task}, timeout=branch_timeout
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        intent_result = _task_result(
            intent_task, done, "intent branch exceeded latency budget"
        )
        strategy_result = _task_result(
            strategy_task, done, "strategy branch exceeded latency budget"
        )
        diagnostics: list[str] = []
        if isinstance(intent_result, BaseException):
            diagnostics.append(
                f"planner:fast_intent_failed:{type(intent_result).__name__}:{str(intent_result)[:120]}"
            )
            intent = _fallback_compact_intent(state["question"])
        else:
            intent = intent_result
        if isinstance(strategy_result, BaseException):
            diagnostics.append(
                f"planner:fast_strategy_failed:{type(strategy_result).__name__}:{str(strategy_result)[:120]}"
            )
            strategy = _fallback_compact_strategy(state["question"], intent)
        else:
            strategy = strategy_result
        return {
            "draft": _expand_fast_drafts(state["question"], intent, strategy),
            "fast_diagnostics": diagnostics,
        }

    async def _intent_analysis(self, state: PlannerState) -> dict[str, Any]:
        return {
            "intent": await _structured_call(
                self.planner_client,
                ResearchIntent,
                (
                    "You are a senior biomedical research strategist. Deeply interpret the research "
                    "problem rather than merely extracting named entities. Infer the scientific objective, "
                    "comparison, unit of analysis, desired conclusion, and ambiguities. The user may not "
                    "name any data type. Preserve their original request verbatim. Do not claim that any "
                    "dataset exists or is accessible. Be concise: at most 4 items per list and one sentence per item. Return JSON only."
                ),
                {"question": state["question"]},
                max_tokens=2_400,
            )
        }

    async def _study_design(self, state: PlannerState) -> dict[str, Any]:
        return {
            "strategy": await _structured_call(
                self.planner_client,
                StudyStrategyDraft,
                (
                    "You are a biomedical study designer and data architect. Starting from the interpreted "
                    "intent, propose the most scientifically appropriate study design and resource strategy. "
                    "You may recommend single-cell epigenomics, transcriptomics, spatial, bulk, clinical, "
                    "perturbation, literature, ontologies, protocols, software, reference atlases, or models "
                    "when justified. Separate essential, preferred, and supporting needs. Mark every constraint "
                    "as explicit, inferred, or recommended. A recommendation is not evidence that a resource "
                    "exists. Use free-text data_type values when needed; do not force the problem into a fixed "
                    "modality enum. Keep at most 5 data needs, 5 resource needs, and 6 analysis steps; keep each field concise. Return JSON only."
                ),
                {
                    "question": state["question"],
                    "intent": state["intent"].model_dump(mode="json"),
                    "available_capabilities": state["capabilities"],
                },
                max_tokens=4_000,
            )
        }

    async def _critical_review(self, state: PlannerState) -> dict[str, Any]:
        return {
            "critique": await _structured_call(
                self.review_client,
                PlanCritique,
                (
                    "Audit the proposed research strategy. Check fidelity to the user's actual objective, "
                    "scientific coherence, missing controls or validation, feasibility, confounding, cohort "
                    "and lineage leakage, and whether model recommendations are being mistaken for user "
                    "requirements or observed facts. Identify a critical conflict only when it prevents a "
                    "meaningful plan; missing details should normally become explicit assumptions or branches. "
                    "Return JSON only."
                ),
                {
                    "question": state["question"],
                    "intent": state["intent"].model_dump(mode="json"),
                    "strategy": state["strategy"].model_dump(mode="json"),
                    "available_capabilities": state["capabilities"],
                },
                max_tokens=2_800,
            )
        }

    async def _synthesis(self, state: PlannerState) -> dict[str, Any]:
        return {
            "draft": await _structured_call(
                self.planner_client,
                ResearchPlanDraft,
                (
                    "Produce the final research plan from the original request, intent, strategy, and critique. "
                    "Optimize for understanding the scientific goal and proposing the most appropriate downstream "
                    "plan. Apply valid critique recommendations. Continue with explicit assumptions and alternatives "
                    "when details are missing. Set readiness=blocked only if the research objective is unidentifiable "
                    "or core requirements are mutually contradictory. Do not state that datasets, files, sample sizes, "
                    "or access conditions have been verified. Preserve explicit/inferred/recommended provenance. "
                    "Keep the final plan concise: at most 5 data needs, 5 resources, and 6 steps. Return JSON only."
                ),
                {
                    "question": state["question"],
                    "intent": state["intent"].model_dump(mode="json"),
                    "strategy": state["strategy"].model_dump(mode="json"),
                    "critique": state["critique"].model_dump(mode="json"),
                    "available_capabilities": state["capabilities"],
                },
                max_tokens=5_000,
            )
        }


async def _structured_call(
    client: ChatCompletionClient,
    model: type[T],
    system_prompt: str,
    payload: dict[str, Any],
    *,
    max_tokens: int,
) -> T:
    structured_complete = getattr(client, "complete_structured", None)
    if callable(structured_complete):
        response = await structured_complete(
            [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"input": payload, "json_schema": model.model_json_schema()},
                        ensure_ascii=False,
                    ),
                },
            ],
            schema=model.model_json_schema(),
            schema_name=model.__name__,
            temperature=0.0,
            max_tokens=max_tokens,
        )
    else:
        response = await client.complete(
            [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"input": payload, "json_schema": model.model_json_schema()},
                        ensure_ascii=False,
                    ),
                },
            ],
            temperature=0.0,
            max_tokens=max_tokens,
        )
    return model.model_validate(_json_object(response))


def _json_object(response: str) -> dict[str, Any]:
    content = response.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content).strip()
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end < start:
        raise ValueError("response did not contain a JSON object")
    decoded = json.loads(content[start : end + 1])
    if not isinstance(decoded, dict):
        raise ValueError("response JSON must be an object")
    return decoded


def _task_result(task: asyncio.Task[Any], done: set[asyncio.Task[Any]], timeout_message: str):
    if task not in done or task.cancelled():
        return TimeoutError(timeout_message)
    try:
        return task.result()
    except BaseException as error:
        return error


def _fallback_compact_intent(question: str) -> CompactIntentDraft:
    return CompactIntentDraft(
        intent_summary=question,
        primary_question=question,
        scientific_objectives=[question],
        ambiguities=["Semantic intent branch was unavailable."],
    )


def _fallback_compact_strategy(
    question: str, intent: CompactIntentDraft
) -> CompactStrategyDraft:
    chinese = bool(re.search(r"[\u4e00-\u9fff]", question))
    metadata = list(
        dict.fromkeys([*intent.units_of_analysis, *intent.desired_outcomes])
    )[:6]
    if chinese:
        data_needs = [
            CompactDataNeed(
                name="单细胞调控机制发现队列",
                data_type="scATAC-seq或RNA+ATAC multiome",
                role="发现细胞状态及其调控元件",
                priority=NeedPriority.ESSENTIAL,
                rationale="将研究表型定位到细胞类型、状态和候选调控程序。",
                modalities=["scATAC-seq", "RNA+ATAC multiome"],
                required_metadata=metadata,
            ),
            CompactDataNeed(
                name="转录与细胞状态验证数据",
                data_type="scRNA-seq或配对multiome RNA",
                role="验证调控程序的表达与细胞状态",
                priority=NeedPriority.PREFERRED,
                rationale="避免仅凭染色质可及性解释功能状态。",
                modalities=["scRNA-seq", "RNA+ATAC multiome"],
                required_metadata=metadata,
            ),
            CompactDataNeed(
                name="独立临床结局验证队列",
                data_type="带治疗响应和生存随访的bulk或多组学临床队列",
                role="验证治疗响应和预后关联",
                priority=NeedPriority.ESSENTIAL,
                rationale="将机制发现与患者层面的可验证结局连接。",
                required_metadata=["治疗方案", "治疗响应", "生存结局", "关键临床协变量"],
            ),
            CompactDataNeed(
                name="空间或扰动正交验证数据",
                data_type="空间组学或扰动实验数据",
                role="验证空间生态位或因果方向",
                priority=NeedPriority.SUPPORTING,
                rationale="降低观察性关联被误解为机制或因果关系的风险。",
            ),
        ]
        resources = [
            CompactResourceNeed(resource_type="文献与预印本", purpose="定义研究表型、候选机制及其数据入口。", priority=NeedPriority.ESSENTIAL),
            CompactResourceNeed(resource_type="细胞本体、参考图谱与基因调控知识库", purpose="统一细胞状态术语并解释候选调控网络。", priority=NeedPriority.SUPPORTING),
        ]
        steps = [
            CompactAnalysisStep(title="定义可操作研究表型", objective="把抽象研究问题转成可证伪的状态、比较和结局定义。", outputs=["表型定义与证据问题"], success_criteria=["每个主要目标均有可观测指标"]),
            CompactAnalysisStep(title="发现并审计公共资源", objective="检索发现、验证和临床队列并核验元数据与访问条件。", inputs=[item.name for item in data_needs], outputs=["证据关联的候选资源清单"], success_criteria=["候选结论均有来源证据"]),
            CompactAnalysisStep(title="构建细胞状态与调控程序", objective="整合单细胞表观与转录信号，控制批次和研究来源。", methods=["跨研究协调", "细胞状态建模", "调控网络推断"], outputs=["候选状态和调控程序"]),
            CompactAnalysisStep(title="独立临床和正交验证", objective="在独立队列验证关联，并用空间或扰动证据限定机制解释。", methods=["研究级留出", "临床协变量校正", "正交证据验证"], outputs=["泛化、响应和预后评估"]),
        ]
        assumptions = ["在用户补充具体疾病和研究范围前，先生成跨癌种候选方案。"]
        uncertainties = ["语义数据策略分支未在交互延迟预算内完成；当前为可审计的推荐模板。"]
        rationale = "可以在显式假设下开始资源发现，并在获得元数据后收窄方案。"
    else:
        data_needs = [
            CompactDataNeed(name="Single-cell regulatory discovery cohorts", data_type="scATAC-seq or RNA+ATAC multiome", role="discover cell states and regulatory programs", priority=NeedPriority.ESSENTIAL, rationale="Resolve the phenotype at cell-state and regulatory-element level.", modalities=["scATAC-seq", "RNA+ATAC multiome"], required_metadata=metadata),
            CompactDataNeed(name="Transcriptomic state validation data", data_type="scRNA-seq or paired multiome RNA", role="validate expression and cell states", priority=NeedPriority.PREFERRED, rationale="Avoid functional claims based only on accessibility.", modalities=["scRNA-seq", "RNA+ATAC multiome"], required_metadata=metadata),
            CompactDataNeed(name="Independent clinical-outcome cohorts", data_type="bulk or multi-omics cohorts with treatment and survival outcomes", role="validate response and prognosis", priority=NeedPriority.ESSENTIAL, rationale="Connect mechanistic findings to patient-level outcomes.", required_metadata=["treatment", "response", "survival", "clinical covariates"]),
            CompactDataNeed(name="Spatial or perturbational validation", data_type="spatial omics or perturbation data", role="orthogonal validation", priority=NeedPriority.SUPPORTING, rationale="Limit causal over-interpretation of observational associations."),
        ]
        resources = [
            CompactResourceNeed(resource_type="literature and preprints", purpose="Define phenotypes, mechanisms, and linked data.", priority=NeedPriority.ESSENTIAL),
            CompactResourceNeed(resource_type="ontologies, reference atlases, and regulatory knowledge bases", purpose="Normalize states and interpret regulatory networks.", priority=NeedPriority.SUPPORTING),
        ]
        steps = [
            CompactAnalysisStep(title="Operationalize the research phenotype", objective="Turn the research question into falsifiable states, comparisons, and outcomes.", outputs=["phenotype definitions and evidence questions"]),
            CompactAnalysisStep(title="Discover and audit public resources", objective="Find discovery, validation, and clinical cohorts and verify metadata.", inputs=[item.name for item in data_needs], outputs=["evidence-linked resource inventory"]),
            CompactAnalysisStep(title="Model cell states and regulatory programs", objective="Integrate epigenomic and transcriptomic evidence across studies.", outputs=["candidate states and regulatory programs"]),
            CompactAnalysisStep(title="Perform independent clinical and orthogonal validation", objective="Test generalization, response, prognosis, and mechanistic limits.", outputs=["validated associations and uncertainty report"]),
        ]
        assumptions = ["Use a broad disease scope until the user supplies a specific cohort definition."]
        uncertainties = ["The semantic strategy branch exceeded the interactive budget; this is an auditable recommended template."]
        rationale = "Resource discovery can start under explicit assumptions and narrow after metadata review."
    return CompactStrategyDraft(
        constraints=[
            CompactConstraint(
                statement="Use study-level independent validation and prevent cohort leakage",
                kind=ConstraintKind.PREFERENCE,
                origin=PlanOrigin.RECOMMENDED,
                rationale="Protect cross-dataset validity.",
            )
        ],
        data_needs=data_needs,
        resource_needs=resources,
        analysis_plan=steps,
        assumptions=assumptions,
        uncertainties=uncertainties,
        readiness=PlanReadiness.CONDITIONAL,
        readiness_rationale=rationale,
    )


def _expand_fast_drafts(
    question: str, intent_draft: CompactIntentDraft, strategy: CompactStrategyDraft
) -> ResearchPlanDraft:
    intent = ResearchIntent(
        original_request=question,
        intent_summary=intent_draft.intent_summary,
        primary_question=intent_draft.primary_question,
        scientific_objectives=intent_draft.scientific_objectives,
        hypotheses=intent_draft.hypotheses,
        comparisons=intent_draft.comparisons,
        units_of_analysis=intent_draft.units_of_analysis,
        desired_outcomes=intent_draft.desired_outcomes,
        ambiguities=intent_draft.ambiguities,
    )
    constraints = [
        SemanticConstraint(
            statement=item.statement,
            kind=item.kind,
            origin=item.origin,
            confidence=1.0 if item.origin == PlanOrigin.EXPLICIT else 0.8,
            rationale=item.rationale,
        )
        for item in strategy.constraints
    ]
    data_needs = [
        DataNeed(
            need_id=stable_id("need", f"{question}:data:{index}:{item.name}"),
            name=item.name,
            data_type=item.data_type,
            role=item.role,
            priority=item.priority,
            rationale=item.rationale,
            modalities=item.modalities,
            required_metadata=item.required_metadata,
            alternatives=item.alternatives,
        )
        for index, item in enumerate(strategy.data_needs)
    ]
    resource_needs = [
        ResourceNeed(
            need_id=stable_id("need", f"{question}:resource:{index}:{item.resource_type}"),
            resource_type=item.resource_type,
            purpose=item.purpose,
            priority=item.priority,
            search_focus=item.search_focus,
        )
        for index, item in enumerate(strategy.resource_needs)
    ]
    analysis_plan = [
        AnalysisStep(
            step_id=stable_id("step", f"{question}:{index}:{item.title}"),
            title=item.title,
            objective=item.objective,
            inputs=item.inputs,
            methods=item.methods,
            outputs=item.outputs,
            success_criteria=item.success_criteria,
        )
        for index, item in enumerate(strategy.analysis_plan)
    ]
    return ResearchPlanDraft(
        intent=intent,
        constraints=constraints,
        data_needs=data_needs,
        resource_needs=resource_needs,
        analysis_plan=analysis_plan,
        assumptions=strategy.assumptions,
        uncertainties=strategy.uncertainties,
        clarifying_questions=strategy.clarifying_questions,
        alternative_strategies=strategy.alternative_strategies,
        readiness=strategy.readiness,
        readiness_rationale=strategy.readiness_rationale,
    )


def _validate_fast_draft(
    question: str,
    draft: ResearchPlanDraft,
    fallback: RuleBasedRequirementCompiler,
) -> ResearchPlanDraft:
    intent = draft.intent
    if intent.original_request != question:
        intent = intent.model_copy(update={"original_request": question})
    constraints = list(draft.constraints)
    baseline = fallback.compile(question)
    existing = {item.statement for item in constraints}
    for statement in baseline.hard_constraints:
        if statement not in existing:
            constraints.append(
                SemanticConstraint(
                    statement=statement,
                    origin=PlanOrigin.EXPLICIT,
                    confidence=1.0,
                    rationale="Recovered by deterministic validation.",
                )
            )
    uncertainties = list(draft.uncertainties)
    readiness = draft.readiness
    rationale = draft.readiness_rationale
    if not draft.data_needs:
        uncertainties.append("The model proposed no concrete data need; the executable contract is exploratory.")
        readiness = PlanReadiness.CONDITIONAL
        rationale = "A concrete data strategy must be validated before resource collection."
    return draft.model_copy(
        update={
            "intent": intent,
            "constraints": constraints,
            "uncertainties": list(dict.fromkeys(uncertainties)),
            "readiness": readiness,
            "readiness_rationale": rationale,
        }
    )


def _finalize_plan(
    question: str,
    draft: ResearchPlanDraft,
    fallback: RuleBasedRequirementCompiler,
    capabilities: dict[str, Any],
    diagnostics: list[str],
) -> ResearchPlan:
    data_needs, gaps = _resolve_data_capabilities(draft.data_needs, capabilities)
    resource_needs, resource_gaps = _resolve_resource_capabilities(
        draft.resource_needs, capabilities
    )
    normalized = draft.model_copy(
        update={"data_needs": data_needs, "resource_needs": resource_needs}
    )
    contracts = _compile_contracts(question, normalized, fallback)
    if not contracts:
        contracts = [
            fallback.compile(question).model_copy(
                update={
                    "compiler": "research_plan_v1_bridge",
                    "compiler_warnings": [
                        "No directly supported data need was identified; the epigenomics bridge contract is exploratory."
                    ],
                }
            )
        ]
    return ResearchPlan(
        **normalized.model_dump(),
        executable_contracts=contracts,
        capability_gaps=list(dict.fromkeys([*gaps, *resource_gaps])),
        planner_diagnostics=diagnostics,
    )


def _resolve_data_capabilities(
    needs: list[DataNeed], capabilities: dict[str, Any]
) -> tuple[list[DataNeed], list[str]]:
    supported_terms = ("scatac", "snatac", "atac-seq", "chromatin accessibility", "multiome")
    resolved: list[DataNeed] = []
    gaps: list[str] = []
    sources = capabilities["single_cell_epigenomics"]["sources"]
    for need in needs:
        text = " ".join([need.data_type, need.name, *need.modalities]).casefold()
        if any(term in text for term in supported_terms):
            resolved.append(
                need.model_copy(
                    update={
                        "execution_status": ExecutionStatus.SUPPORTED,
                        "supported_sources": sources,
                    }
                )
            )
        else:
            resolved.append(
                need.model_copy(
                    update={
                        "execution_status": ExecutionStatus.UNSUPPORTED,
                        "supported_sources": [],
                    }
                )
            )
            gaps.append(f"data_need:{need.name}:{need.data_type}")
    return resolved, gaps


def _resolve_resource_capabilities(
    needs: list[ResourceNeed], capabilities: dict[str, Any]
) -> tuple[list[ResourceNeed], list[str]]:
    resolved: list[ResourceNeed] = []
    gaps: list[str] = []
    literature_sources = capabilities["literature"]["sources"]
    for need in needs:
        if any(term in need.resource_type.casefold() for term in ("literature", "publication", "paper", "文献")):
            resolved.append(
                need.model_copy(
                    update={
                        "execution_status": ExecutionStatus.SUPPORTED,
                        "supported_sources": literature_sources,
                    }
                )
            )
        else:
            resolved.append(
                need.model_copy(
                    update={
                        "execution_status": ExecutionStatus.UNSUPPORTED,
                        "supported_sources": [],
                    }
                )
            )
            gaps.append(f"resource_need:{need.resource_type}:{need.purpose}")
    return resolved, gaps


def _compile_contracts(
    question: str,
    draft: ResearchPlanDraft,
    fallback: RuleBasedRequirementCompiler,
) -> list[ResearchRequirement]:
    baseline = fallback.compile(question)
    modalities: list[Modality] = []
    for need in draft.data_needs:
        if need.execution_status != ExecutionStatus.SUPPORTED:
            continue
        for value in [need.data_type, *need.modalities]:
            modality = _known_modality(value)
            if modality and modality not in modalities:
                modalities.append(modality)
    if not modalities:
        return []
    required_roles, preferred_roles = _roles_for_modalities(modalities)
    inferred_constraints = [
        item.statement
        for item in draft.constraints
        if item.origin != PlanOrigin.EXPLICIT
    ]
    return [
        baseline.model_copy(
            update={
                "modalities": modalities,
                "acceptable_modalities": modalities,
                "required_data_roles": required_roles,
                "preferred_data_roles": preferred_roles,
                "unresolved_constraints": list(
                    dict.fromkeys(
                        [*baseline.unresolved_constraints, *inferred_constraints]
                    )
                ),
                "compiler": "research_plan_v1",
                "planner_diagnostics": [
                    "compiled_from_open_world_research_plan"
                ],
            }
        )
    ]


def _known_modality(value: str) -> Modality | None:
    normalized = re.sub(r"[^a-z0-9]+", "", value.casefold())
    if "multiome" in normalized or ("rna" in normalized and "atac" in normalized):
        return Modality.MULTIOME_RNA_ATAC
    if "snatac" in normalized or "singlenucleusatac" in normalized:
        return Modality.SNATAC
    if "scatac" in normalized or "singlecellatac" in normalized or "chromatinaccessibility" in normalized:
        return Modality.SCATAC
    return None


def _roles_for_modalities(
    modalities: list[Modality],
) -> tuple[list[DataRole], list[DataRole]]:
    required = {
        DataRole.CELL_METADATA,
        DataRole.SAMPLE_METADATA,
        DataRole.FRAGMENTS,
    }
    preferred = {
        DataRole.RAW_READS,
        DataRole.PEAK_MATRIX,
        DataRole.QC_METRICS,
        DataRole.FRAGMENT_INDEX,
    }
    if Modality.MULTIOME_RNA_ATAC in modalities:
        required.update({DataRole.RNA_MATRIX, DataRole.BARCODE_LINKAGE})
    return sorted(required, key=str), sorted(preferred, key=str)


def fallback_research_plan(
    question: str,
    fallback: RuleBasedRequirementCompiler,
    diagnostic: str,
) -> ResearchPlan:
    requirement = fallback.compile(question).model_copy(
        update={
            "compiler": "rule_based_research_plan_fallback",
            "compiler_warnings": [
                "Deep research planning failed; a conservative executable bridge was generated."
            ],
            "planner_diagnostics": [diagnostic],
        }
    )
    constraints = [
        SemanticConstraint(
            statement=item,
            origin=PlanOrigin.EXPLICIT,
            rationale="Recovered by the deterministic requirement compiler.",
        )
        for item in requirement.hard_constraints
    ]
    modalities = [item.value for item in requirement.modalities]
    need = DataNeed(
        need_id=stable_id("need", f"{question}:epigenomics"),
        name="Public single-cell epigenomics data",
        data_type="single-cell epigenomics",
        role="primary discovery data",
        priority=NeedPriority.ESSENTIAL,
        rationale="Conservative project-domain fallback; requires user or model review.",
        modalities=modalities,
        execution_status=ExecutionStatus.SUPPORTED,
        supported_sources=["ncbi_geo", "ncbi_sra"],
    )
    intent = ResearchIntent(
        original_request=question,
        intent_summary=question,
        primary_question=question,
        scientific_objectives=[question],
        ambiguities=["Deep semantic interpretation was unavailable."],
    )
    return ResearchPlan(
        intent=intent,
        constraints=constraints,
        data_needs=[need],
        resource_needs=[
            ResourceNeed(
                need_id=stable_id("need", f"{question}:literature"),
                resource_type="literature",
                purpose="Identify relevant studies and linked public data.",
                priority=NeedPriority.ESSENTIAL,
                execution_status=ExecutionStatus.SUPPORTED,
                supported_sources=["europe_pmc"],
            )
        ],
        analysis_plan=[
            AnalysisStep(
                step_id="discover",
                title="Discover candidate studies and datasets",
                objective="Find evidence-linked candidate resources.",
                inputs=[need.need_id],
                outputs=["candidate studies", "candidate datasets"],
                success_criteria=["Every candidate has a resolvable source record."],
            )
        ],
        assumptions=["The project focus on single-cell epigenomics remains applicable."],
        uncertainties=["The user's full scientific intent was not semantically reviewed."],
        clarifying_questions=[],
        alternative_strategies=[],
        readiness=PlanReadiness.CONDITIONAL,
        readiness_rationale="Executable only as a conservative degraded plan.",
        executable_contracts=[requirement],
        capability_gaps=[],
        planner_diagnostics=[diagnostic],
    )
