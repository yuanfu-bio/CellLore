from types import SimpleNamespace

import pytest

from scdata_agent.application.agents.evidence_escalation import (
    EvidenceEscalationAgent,
)
from scdata_agent.application.agents.progressive_evidence import (
    ProgressiveEvidenceCuratorAgent,
)
from scdata_agent.application.agents.shared import AgentContext
from scdata_agent.application.collaboration import (
    ArtifactKind,
    CollaborationLedger,
    artifact,
)
from scdata_agent.application.orchestration_v2 import (
    MultiAgentSupervisor,
    workflow_topology,
)
from scdata_agent.application.resource_pipeline import merge_pointers
from scdata_agent.domain.lifecycle import PointerStatus, ResourcePointer
from scdata_agent.domain.models import (
    AccessLevel,
    AssayProfile,
    DatasetCandidate,
    DiscoveryRunResult,
    Identifier,
    OmicsLayer,
    QueryPlan,
    ResearchRequirement,
    SourceRecord,
)
from scdata_agent.domain.resource_pipeline import (
    AssetInventoryDelta,
    EvidenceEscalationReport,
    PointerBatchStage,
    ResourcePointerBatch,
    ScoutPhase,
)


def _context(tmp_path, discovery, requirement=None):
    requirement = requirement or discovery.requirement
    database = tmp_path / "resource_flow.sqlite"
    return AgentContext(
        run_id="run_1",
        question=requirement.question,
        database_path=str(database),
        ledger=CollaborationLedger(database),
        policy={"use_llm": False, "max_fulltexts": 5},
        requirement=requirement,
        discovery=discovery,
    )


def test_studio_graph_exposes_both_asset_scout_inputs(tmp_path):
    database = tmp_path / "graph.sqlite"
    supervisor = MultiAgentSupervisor(
        CollaborationLedger(database), str(database)
    )
    graph = supervisor.build_graph().get_graph()
    edges = {(item.source, item.target) for item in graph.edges}

    assert (
        "resource_discovery",
        "asset_scout_direct",
    ) in edges
    assert (
        "resource_discovery",
        "evidence_metadata",
    ) in edges
    assert (
        "evidence_metadata",
        "evidence_escalation_gate",
    ) in edges
    assert (
        "evidence_fulltext",
        "asset_scout_enriched",
    ) in edges
    assert (
        "asset_scout_enriched",
        "geo_sample_metadata",
    ) in edges
    assert (
        "geo_sample_metadata",
        "inventory_reconcile",
    ) in edges
    assert ("inventory_reconcile", "preflight_assessment") in edges
    assert ("inventory_reconcile", "early_lineage") in edges
    assert ("preflight_assessment", "acquisition_strategy") in edges
    assert ("early_lineage", "acquisition_strategy") in edges


def test_control_room_topology_includes_geo_sample_metadata_step():
    topology = workflow_topology()
    node_ids = {item["id"] for item in topology["nodes"]}
    edges = {(item["source"], item["target"]) for item in topology["edges"]}

    assert "geo_sample_metadata" in node_ids
    assert ("asset_scout_enriched", "geo_sample_metadata") in edges
    assert ("geo_sample_metadata", "inventory_reconcile") in edges
    assert ("asset_scout_enriched", "inventory_reconcile") not in edges


def test_pointer_merge_preserves_provenance_and_blocks_conflicts():
    reported = ResourcePointer(
        pointer_id="pointer_1",
        candidate_id="candidate_1",
        namespace="geo_series",
        locator="GSE1",
        source_name="ncbi_geo",
        status=PointerStatus.REPORTED,
        origin_stage="discovery",
        evidence_ids=["evidence_1"],
    )
    conflicted = reported.model_copy(
        update={
            "status": PointerStatus.CONFLICTED,
            "origin_stage": "evidence_metadata",
            "evidence_ids": ["evidence_2"],
        }
    )
    merged = merge_pointers([reported, conflicted])

    assert len(merged) == 1
    assert merged[0].status == PointerStatus.CONFLICTED
    assert merged[0].evidence_ids == ["evidence_2", "evidence_1"]
    assert not merged[0].scoutable


@pytest.mark.asyncio
async def test_missing_discovery_pointer_escalates_to_open_fulltext(
    tmp_path, monkeypatch
):
    requirement = ResearchRequirement(question="human lung scATAC")
    candidate = DatasetCandidate(
        candidate_id="candidate_1",
        title="Article-derived dataset",
        source_record_ids=["record_1"],
        access_level=AccessLevel.PUBLIC_DECLARED,
    )
    record = SourceRecord(
        record_id="record_1",
        source_name="europe_pmc",
        external_id="1",
        title="Article",
        publication_identifiers=[
            Identifier(namespace="pmcid", value="PMC1")
        ],
    )
    discovery = DiscoveryRunResult(
        run_id="discovery_1",
        requirement=requirement,
        query_plan=QueryPlan(requirement=requirement, queries=[]),
        source_records=[record],
        candidates=[candidate],
        feasibility_reports=[],
    )
    context = _context(tmp_path, discovery)
    empty_direct = AssetInventoryDelta(
        inventory_id="delta_direct",
        run_id=context.run_id,
        phase=ScoutPhase.DIRECT,
    )
    context.artifacts.append(
        artifact(
            context.run_id,
            ArtifactKind.ASSET_INVENTORY_DELTA,
            EvidenceEscalationAgent.name,
            empty_direct.model_dump(mode="json"),
        )
    )

    escalation_outputs = await EvidenceEscalationAgent().run(context)
    context.artifacts.extend(escalation_outputs)
    report = EvidenceEscalationReport.model_validate(
        escalation_outputs[0].payload
    )
    assert report.fulltext_candidate_ids == ["candidate_1"]

    class FakeArticleAccess:
        async def fetch(self, source_record):
            assert source_record.record_id == "record_1"
            return SimpleNamespace(
                text="Data are available from GSE12345.",
                source_url="https://example.test/PMC1",
                snapshot_hash="sha256:test",
            )

    monkeypatch.setattr(
        "scdata_agent.application.agents.progressive_evidence.OpenArticleAccessService",
        FakeArticleAccess,
    )
    outputs = await ProgressiveEvidenceCuratorAgent("fulltext").run(
        context
    )
    batch_artifact = next(
        item
        for item in outputs
        if item.kind == ArtifactKind.RESOURCE_POINTERS
    )
    batch = ResourcePointerBatch.model_validate(batch_artifact.payload)

    assert batch.stage == PointerBatchStage.EVIDENCE_FULLTEXT
    assert [
        (item.namespace, item.locator)
        for item in batch.pointers
    ] == [("geo_series", "GSE12345")]


@pytest.mark.asyncio
async def test_native_repository_metadata_does_not_require_fulltext(
    tmp_path,
):
    requirement = ResearchRequirement(question="human lung scATAC")
    candidate = DatasetCandidate(
        candidate_id="candidate_1",
        title="Repository dataset",
        identifiers=[
            Identifier(namespace="geo_series", value="GSE1")
        ],
        source_record_ids=["record_1"],
        access_level=AccessLevel.PUBLIC_DECLARED,
    )
    record = SourceRecord(
        record_id="record_1",
        source_name="ncbi_geo",
        external_id="GSE1",
        title="Repository dataset",
        identifiers=[
            Identifier(namespace="geo_series", value="GSE1")
        ],
    )
    discovery = DiscoveryRunResult(
        run_id="discovery_1",
        requirement=requirement,
        query_plan=QueryPlan(requirement=requirement, queries=[]),
        source_records=[record],
        candidates=[candidate],
        feasibility_reports=[],
    )
    context = _context(tmp_path, discovery)
    pointer = ResourcePointer(
        pointer_id="pointer_1",
        candidate_id="candidate_1",
        namespace="geo_series",
        locator="GSE1",
        source_name="ncbi_geo",
        status=PointerStatus.REPORTED,
        origin_stage="discovery",
    )
    batch = ResourcePointerBatch(
        batch_id="batch_1",
        run_id=context.run_id,
        stage=PointerBatchStage.DISCOVERY,
        pointers=[pointer],
    )
    context.artifacts.extend(
        [
            artifact(
                context.run_id,
                ArtifactKind.RESOURCE_POINTERS,
                EvidenceEscalationAgent.name,
                batch.model_dump(mode="json"),
            ),
            artifact(
                context.run_id,
                ArtifactKind.ASSET_INVENTORY_DELTA,
                EvidenceEscalationAgent.name,
                AssetInventoryDelta(
                    inventory_id="delta_1",
                    run_id=context.run_id,
                    phase=ScoutPhase.DIRECT,
                    consumed_pointer_ids=["pointer_1"],
                    assets=[
                        {
                            "asset_id": "asset_1",
                            "candidate_id": "candidate_1",
                            "role": "chromatin_fragments",
                            "file_name": "fragments.tsv.gz",
                            "locations": [],
                        }
                    ],
                ).model_dump(mode="json"),
            ),
        ]
    )
    outputs = await EvidenceEscalationAgent().run(context)
    report = EvidenceEscalationReport.model_validate(outputs[0].payload)

    assert report.fulltext_candidate_ids == []
    assert report.candidates[0].unresolved_fields == []


async def _pairing_escalation(tmp_path, assay_profiles):
    """Escalate one candidate against a question naming BOTH scATAC and multiome."""
    requirement = ResearchRequirement(
        question="scATAC-seq and multiome for representation learning",
    )
    candidate = DatasetCandidate(
        candidate_id="candidate_1",
        title="Repository dataset",
        assay_profiles=assay_profiles,
        identifiers=[Identifier(namespace="geo_series", value="GSE1")],
        source_record_ids=["record_1"],
    )
    record = SourceRecord(
        record_id="record_1", source_name="ncbi_geo", external_id="GSE1",
        title="Repository dataset",
        identifiers=[Identifier(namespace="geo_series", value="GSE1")],
    )
    discovery = DiscoveryRunResult(
        run_id="discovery_1", requirement=requirement,
        query_plan=QueryPlan(requirement=requirement, queries=[]),
        source_records=[record], candidates=[candidate], feasibility_reports=[],
    )
    context = _context(tmp_path, discovery, requirement)
    batch = ResourcePointerBatch(
        batch_id="batch_1", run_id=context.run_id,
        stage=PointerBatchStage.DISCOVERY,
        pointers=[ResourcePointer(
            pointer_id="pointer_1", candidate_id="candidate_1",
            namespace="geo_series", locator="GSE1", source_name="ncbi_geo",
            status=PointerStatus.REPORTED, origin_stage="discovery",
        )],
    )
    context.artifacts.extend([
        artifact(context.run_id, ArtifactKind.RESOURCE_POINTERS,
                 EvidenceEscalationAgent.name, batch.model_dump(mode="json")),
        artifact(context.run_id, ArtifactKind.ASSET_INVENTORY_DELTA,
                 EvidenceEscalationAgent.name,
                 AssetInventoryDelta(
                     inventory_id="delta_1", run_id=context.run_id,
                     phase=ScoutPhase.DIRECT, consumed_pointer_ids=["pointer_1"],
                     assets=[{
                         "asset_id": "asset_1", "candidate_id": "candidate_1",
                         "role": "chromatin_fragments",
                         "file_name": "fragments.tsv.gz", "locations": [],
                     }],
                 ).model_dump(mode="json")),
    ])
    outputs = await EvidenceEscalationAgent().run(context)
    return EvidenceEscalationReport.model_validate(outputs[0].payload)


@pytest.mark.asyncio
async def test_standalone_scatac_is_not_asked_to_prove_multiome_pairing(tmp_path):
    """Pairing is owed by candidates that claim a paired assay, not by the question.

    Deriving this from the requirement's union of modalities made standalone
    scATAC depositions carry an unresolvable `multiome_pairing` field, which
    held them at needs_evidence and sent them for open full text. Two
    separate single-layer profiles (a standalone ATAC assay and a standalone
    RNA assay merely co-reported in the same study) must not be conflated
    with one profile that itself spans both layers.
    """
    report = await _pairing_escalation(
        tmp_path,
        [
            AssayProfile(
                label="scatac", omics_layers=[OmicsLayer.CHROMATIN_ACCESSIBILITY], rationale="x",
            ),
            AssayProfile(
                label="scrna", omics_layers=[OmicsLayer.TRANSCRIPTOME], rationale="x",
            ),
        ],
    )

    assert "multiome_pairing" not in report.candidates[0].unresolved_fields
    assert report.fulltext_candidate_ids == []


@pytest.mark.asyncio
async def test_a_multiome_candidate_still_owes_pairing_evidence(tmp_path):
    """Guard against over-correcting: real multiome must still be challenged."""
    report = await _pairing_escalation(
        tmp_path,
        [AssayProfile(
            label="10x_multiome_rna_atac",
            omics_layers=[OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME],
            rationale="x",
        )],
    )

    assert "multiome_pairing" in report.candidates[0].unresolved_fields
