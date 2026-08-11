import json

import pytest

from celllore.domain.models import OmicsLayer, PairingLevel
from celllore.domain.planning import (
    ExecutionStatus,
    PlanCritique,
    PlanOrigin,
    PlanReadiness,
)
from celllore.infrastructure.llm.research_planner import (
    MultiStageResearchPlanner,
    _normalize_model_enums,
    _structured_call,
)


class ScriptedClient:
    class settings:
        model = "planner-test"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, messages, *, temperature=0.1, max_tokens=2_000):
        self.calls.append(messages)
        if not self.responses:
            raise AssertionError("unexpected planner call")
        return json.dumps(self.responses.pop(0), ensure_ascii=False)


def test_planning_only_execution_status_is_normalized_at_llm_boundary():
    value = {
        "resource_needs": [
            {"execution_status": "planning_only"},
            {"execution_status": "planned"},
        ]
    }

    normalized = _normalize_model_enums(value)

    assert normalized["resource_needs"][0]["execution_status"] == "unsupported"
    assert normalized["resource_needs"][1]["execution_status"] == "partial"


def test_recommended_planning_language_is_normalized_by_field():
    value = {
        "constraints": [{"kind": "recommended", "origin": "recommended"}],
        "data_needs": [{"priority": "recommended"}],
        "resource_needs": [{"priority": "recommended"}],
    }

    normalized = _normalize_model_enums(value)

    assert normalized["constraints"][0] == {
        "kind": "preference",
        "origin": "recommended",
    }
    assert normalized["data_needs"][0]["priority"] == "supporting"
    assert normalized["resource_needs"][0]["priority"] == "supporting"


def test_known_plan_critique_field_typo_is_normalized():
    normalized = _normalize_model_enums(
        {"intefit_fidelity_issues": ["same-cell requirement was weakened"]}
    )

    assert normalized == {
        "intent_fidelity_issues": ["same-cell requirement was weakened"]
    }


@pytest.mark.asyncio
async def test_structured_call_retries_once_after_schema_validation_failure():
    valid_critique = {
        "intent_fidelity_issues": [],
        "scientific_design_issues": [],
        "feasibility_issues": [],
        "leakage_or_bias_risks": [],
        "recommended_changes": [],
        "critical_conflicts": [],
    }
    client = ScriptedClient(
        [
            {"unknown_critique_field": ["invalid"]},
            valid_critique,
        ]
    )

    result = await _structured_call(
        client,
        PlanCritique,
        "Audit the plan.",
        {"strategy": {}},
        max_tokens=1_000,
    )

    assert result == PlanCritique.model_validate(valid_critique)
    assert len(client.calls) == 2
    repair_payload = json.loads(client.calls[1][1]["content"])
    assert repair_payload["invalid_response"]
    assert repair_payload["validation_errors"][0]["type"] == "extra_forbidden"


def _intent(question):
    return {
        "original_request": question,
        "intent_summary": "解析肿瘤免疫逃逸相关的调控状态及其临床关联",
        "primary_question": "哪些调控状态驱动肿瘤免疫逃逸？",
        "scientific_objectives": ["识别免疫逃逸相关细胞状态和调控元件"],
        "hypotheses": ["染色质调控状态与免疫逃逸相关"],
        "comparisons": ["免疫逃逸高与低的样本或细胞状态"],
        "units_of_analysis": ["患者", "细胞状态", "调控区域"],
        "desired_outcomes": ["候选调控程序和外部验证方案"],
        "ambiguities": ["癌种和免疫逃逸定义尚未指定"],
    }


def _constraint(statement, origin):
    return {
        "statement": statement,
        "kind": "preference",
        "origin": origin,
        "confidence": 0.8,
        "rationale": "支持研究目标",
        "verification_question": "候选数据是否提供相应元数据？",
    }


def _atac_need():
    return {
        "need_id": "atac",
        "name": "单细胞染色质可及性队列",
        "data_type": "scATAC-seq or RNA+ATAC multiome",
        "role": "主要调控发现数据",
        "priority": "essential",
        "rationale": "用于解析细胞状态特异调控元件",
        "modalities": ["scATAC-seq", "RNA+ATAC multiome"],
        "required_metadata": ["患者", "疾病状态"],
        "constraints": [_constraint("应包含可比较的免疫状态", "recommended")],
        "execution_status": "unknown",
    }


def _rna_need():
    return {
        "need_id": "rna",
        "name": "转录或空间验证队列",
        "data_type": "scRNA-seq and spatial transcriptomics",
        "role": "外部验证",
        "priority": "supporting",
        "rationale": "验证调控程序的表达和空间背景",
        "modalities": ["scRNA-seq", "spatial transcriptomics"],
        "execution_status": "unknown",
    }


def _strategy():
    return {
        "study_design_summary": "以表观调控发现、转录和空间验证组成多阶段研究",
        "constraints": [_constraint("癌种暂以实体瘤为工作假设", "inferred")],
        "data_needs": [_atac_need(), _rna_need()],
        "resource_needs": [
            {
                "need_id": "papers",
                "resource_type": "literature",
                "purpose": "确定免疫逃逸定义和可用队列",
                "priority": "essential",
            },
            {
                "need_id": "ontology",
                "resource_type": "cell-state ontology",
                "purpose": "统一状态术语",
                "priority": "supporting",
            },
        ],
        "analysis_plan": [
            {
                "step_id": "discover",
                "title": "资源发现",
                "objective": "发现候选研究和数据",
                "inputs": ["papers", "atac", "rna"],
                "methods": ["文献与数据库检索"],
                "outputs": ["候选资源清单"],
                "success_criteria": ["每项候选均有证据来源"],
            }
        ],
        "assumptions": ["先采用广义实体瘤范围"],
        "uncertainties": ["癌种和免疫逃逸表型定义待确认"],
        "alternative_strategies": ["按癌种分支检索"],
    }


def _critique():
    return {
        "intent_fidelity_issues": [],
        "scientific_design_issues": ["需明确免疫逃逸操作性定义"],
        "feasibility_issues": ["空间数据不一定具有配对样本"],
        "leakage_or_bias_risks": ["同一研究不可跨训练与验证集合"],
        "recommended_changes": ["将癌种和免疫逃逸定义列为显式假设"],
        "critical_conflicts": [],
    }


def _final(question):
    return {
        "intent": _intent(question),
        "constraints": [_constraint("癌种暂以实体瘤为工作假设", "inferred")],
        "data_needs": [_atac_need(), _rna_need()],
        "resource_needs": _strategy()["resource_needs"],
        "analysis_plan": _strategy()["analysis_plan"],
        "assumptions": ["先采用广义实体瘤范围"],
        "uncertainties": ["癌种和免疫逃逸表型定义待确认"],
        "clarifying_questions": ["优先关注哪一种癌症？"],
        "alternative_strategies": ["按癌种分支检索"],
        "readiness": "conditional",
        "readiness_rationale": "可以按显式假设开始发现，随后再收窄。",
    }


@pytest.mark.asyncio
async def test_planner_derives_cross_modal_strategy_without_user_data_specification():
    question = "我想研究肿瘤免疫逃逸的调控机制，并找到可验证的临床相关信号。"
    planner = ScriptedClient([_intent(question), _strategy(), _final(question)])
    reviewer = ScriptedClient([_critique()])

    plan = await MultiStageResearchPlanner(planner, review_client=reviewer, mode="deep").plan(question)

    assert plan.readiness == PlanReadiness.CONDITIONAL
    assert plan.intent.original_request == question
    assert len(planner.calls) == 3
    assert len(reviewer.calls) == 1
    assert plan.data_needs[0].execution_status == ExecutionStatus.SUPPORTED
    assert plan.data_needs[1].execution_status == ExecutionStatus.UNSUPPORTED
    assert any(gap.startswith("data_need:转录或空间验证队列") for gap in plan.capability_gaps)
    assert plan.constraints[0].origin == PlanOrigin.INFERRED
    assert OmicsLayer.CHROMATIN_ACCESSIBILITY in plan.executable_contracts[0].required_omics_layers
    assert OmicsLayer.TRANSCRIPTOME in plan.executable_contracts[0].required_omics_layers


def _trimodal_need():
    return {
        "need_id": "trimodal",
        "name": "三模态队列",
        "data_type": "TEA-seq or DOGMA-seq trimodal single-cell data",
        "role": "同细胞RNA+ATAC+蛋白发现队列",
        "priority": "essential",
        "rationale": "同细胞获取三层组学信号",
        "modalities": ["TEA-seq", "DOGMA-seq"],
        "required_metadata": ["细胞类型"],
        "constraints": [],
        "execution_status": "supported",
    }


@pytest.mark.asyncio
async def test_deep_mode_carries_trimodal_and_human_through_to_the_contract():
    """End-to-end regression for the live-run defect: a question asking for
    TEA-seq/DOGMA-seq trimodal human data must produce an executable contract
    whose required_omics_layers actually contains the protein layer, via
    the real deep-mode graph rather than a hand-called compiler function."""
    question = "配对的单细胞多组学数据（human）：TEA-seq/DOGMA-seq三模态数据，用于自监督基础模型预训练"
    strategy = _strategy()
    strategy["data_needs"] = [_trimodal_need()]
    final = _final(question)
    final["data_needs"] = [_trimodal_need()]

    planner = ScriptedClient([_intent(question), strategy, final])
    reviewer = ScriptedClient([_critique()])

    plan = await MultiStageResearchPlanner(
        planner, review_client=reviewer, mode="deep"
    ).plan(question)

    contract = plan.executable_contracts[0]
    assert OmicsLayer.PROTEIN in contract.required_omics_layers
    assert OmicsLayer.CHROMATIN_ACCESSIBILITY in contract.required_omics_layers
    assert OmicsLayer.TRANSCRIPTOME in contract.required_omics_layers
    assert contract.organisms == ["Homo sapiens"]
    assert contract.minimum_pairing_level == PairingLevel.SAME_CELL


@pytest.mark.asyncio
async def test_planner_failure_is_explicitly_degraded_not_silent():
    planner = ScriptedClient(["not-json"])
    plan = await MultiStageResearchPlanner(planner, mode="fast").plan("研究未知疾病机制")

    assert plan.readiness == PlanReadiness.CONDITIONAL
    assert plan.executable_contracts
    assert plan.executable_contracts[0].compiler == "research_plan_v1"
    assert any(item.startswith("planner:fast_intent_failed") for item in plan.planner_diagnostics)
    assert any(item.startswith("planner:fast_strategy_failed") for item in plan.planner_diagnostics)


def _compact_final(question):
    final = _final(question)
    intent = final["intent"]
    return {
        "intent_summary": intent["intent_summary"],
        "primary_question": intent["primary_question"],
        "scientific_objectives": intent["scientific_objectives"],
        "hypotheses": intent["hypotheses"],
        "comparisons": intent["comparisons"],
        "units_of_analysis": intent["units_of_analysis"],
        "desired_outcomes": intent["desired_outcomes"],
        "ambiguities": intent["ambiguities"],
        "constraints": final["constraints"],
        "data_needs": [
            {key: value for key, value in item.items() if key in {
                "name", "data_type", "role", "priority", "rationale",
                "modalities", "required_metadata", "alternatives",
            }}
            for item in final["data_needs"]
        ],
        "resource_needs": [
            {key: value for key, value in item.items() if key in {
                "resource_type", "purpose", "priority", "search_focus",
            }}
            for item in final["resource_needs"]
        ],
        "analysis_plan": [
            {key: value for key, value in item.items() if key in {
                "title", "objective", "inputs", "methods", "outputs", "success_criteria",
            }}
            for item in final["analysis_plan"]
        ],
        "assumptions": final["assumptions"],
        "uncertainties": final["uncertainties"],
        "clarifying_questions": final["clarifying_questions"],
        "alternative_strategies": final["alternative_strategies"],
        "readiness": final["readiness"],
        "readiness_rationale": final["readiness_rationale"],
    }


@pytest.mark.asyncio
async def test_fast_planner_uses_one_model_call_and_local_validation():
    question = "研究肿瘤免疫逃逸并设计公共数据验证方案"
    compact = _compact_final(question)
    intent_fields = {
        key: compact[key]
        for key in (
            "intent_summary", "primary_question", "scientific_objectives",
            "hypotheses", "comparisons", "units_of_analysis",
            "desired_outcomes", "ambiguities",
        )
    }
    strategy_fields = {key: value for key, value in compact.items() if key not in intent_fields}
    planner = ScriptedClient([intent_fields, strategy_fields])

    plan = await MultiStageResearchPlanner(planner, mode="fast").plan(question)

    assert len(planner.calls) == 2
    assert plan.intent.primary_question
    assert plan.data_needs[0].execution_status == ExecutionStatus.SUPPORTED
    assert plan.planner_diagnostics == [
        "planner:fast_parallel_plan",
        "planner:deterministic_validation",
    ]
