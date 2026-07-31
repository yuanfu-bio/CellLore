import json

import pytest

from scdata_agent.domain.models import DataRole, Modality
from scdata_agent.infrastructure.llm.requirement_compiler import StepFunRequirementCompiler


class FakeStepFun:
    class settings:
        model = "stepfun-test"

    def __init__(self, response: str) -> None:
        self.response = response
        self.messages = []

    async def complete(self, messages, *, temperature=0.1, max_tokens=2_000):
        self.messages = messages
        return self.response


@pytest.mark.asyncio
async def test_stepfun_compiler_validates_and_merges_semantic_draft():
    client = FakeStepFun(
        json.dumps(
            {
                "organisms": ["Homo sapiens"],
                "tissues": ["lung"],
                "diseases": ["lung adenocarcinoma"],
                "cell_types": ["CD8 T cell"],
                "modalities": ["multiome_rna_atac"],
                "minimum_donors": 6,
                "downstream_tasks": ["sparse_signal_recovery"],
                "search_terms": ["LUAD", "CD8 T cell"],
            }
        )
    )
    requirement = await StepFunRequirementCompiler(client).compile(
        "找人类肺癌 multiome 数据"
    )

    assert requirement.compiler == "stepfun_structured_v2:stepfun-test"
    assert "lung adenocarcinoma" in requirement.diseases
    assert requirement.minimum_donors == 6
    assert Modality.MULTIOME_RNA_ATAC in requirement.modalities
    assert DataRole.RNA_MATRIX in requirement.required_data_roles
    assert DataRole.BARCODE_LINKAGE in requirement.required_data_roles
    assert requirement.downstream_tasks == ["sparse_signal_recovery"]
    assert client.messages[0]["role"] == "system"


@pytest.mark.asyncio
async def test_stepfun_compiler_falls_back_on_invalid_schema():
    client = FakeStepFun('{"unknown_field": true}')
    requirement = await StepFunRequirementCompiler(client).compile(
        "human lung scATAC-seq"
    )

    assert requirement.compiler == "rule_based_v1_fallback"
    assert requirement.compiler_warnings
    assert Modality.SCATAC in requirement.modalities


@pytest.mark.asyncio
async def test_critic_revises_explicitly_missing_requirement_fields():
    planner = FakeStepFun(json.dumps({"modalities": ["scatac_seq"]}))
    critic = FakeStepFun(json.dumps({
        "missing_constraints": ["CD8 T cell"],
        "conflicts": [],
        "revised_draft": {"cell_types": ["CD8 T cell"], "minimum_donors": 30},
    }))
    requirement = await StepFunRequirementCompiler(planner, critic_client=critic).compile(
        "human lung cancer scATAC, at least 30 donors, CD8 T cells"
    )
    assert "CD8 T cell" in requirement.cell_types
    assert requirement.minimum_donors == 30
    assert any(item.startswith("critic:missing") for item in requirement.planner_diagnostics)
