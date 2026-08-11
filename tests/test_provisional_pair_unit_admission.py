"""The exact wrong-admission scenario Phase 4 correction #3 closes:

    PairUnit resolution never runs (no LLM, no sample metadata)
    -> every asset for the study kept in one provisional/unresolved unit
    -> the study happens to be a named same-cell technology (DOGMA-seq)
    -> that assay-level prior must NOT auto-grant the unverified unit a
       same-cell claim, and PREFLIGHT_READY must not be reached.

This chains the real InventoryReconcileAgent (with its default, no-LLM-
configured PairUnitResolver/PairingVerifier) into the real
ProgressivePreflightAssessmentAgent, end to end.
"""

import pytest

from scdata_agent.application.agents.inventory_reconcile import (
    InventoryReconcileAgent,
)
from scdata_agent.application.agents.pair_unit_resolver import PairUnitResolver
from scdata_agent.application.agents.pairing_verifier import PairingVerifier
from scdata_agent.application.agents.progressive_preflight import (
    ProgressivePreflightAssessmentAgent,
)
from scdata_agent.application.agents.shared import AgentContext
from scdata_agent.application.collaboration import (
    ArtifactKind,
    CollaborationLedger,
    artifact,
)
from scdata_agent.domain.lifecycle import BundleStatus
from scdata_agent.domain.models import (
    AccessLevel,
    AssayProfile,
    AssetAvailability,
    AssetLocation,
    DataAsset,
    DataRole,
    DatasetCandidate,
    DiscoveryRunResult,
    GeoSampleRecord,
    OmicsLayer,
    PairingLevel,
    PairingRequirement,
    ProfileBasis,
    QueryPlan,
    ResearchRequirement,
    TransferProtocol,
)
from scdata_agent.domain.resource_pipeline import AssetInventoryDelta, ScoutPhase


def _asset(asset_id, role, file_name):
    return DataAsset(
        asset_id=asset_id, candidate_id="cand_1", role=role, file_name=file_name,
        locations=[AssetLocation(
            url=f"https://example.test/{file_name}", protocol=TransferProtocol.HTTPS,
            access_level=AccessLevel.PUBLIC_VERIFIED,
            availability=AssetAvailability.OBSERVED, size_bytes=10,
        )],
    )


@pytest.mark.asyncio
async def test_unresolved_dogma_seq_study_does_not_reach_preflight_ready(tmp_path):
    candidate = DatasetCandidate(
        candidate_id="cand_1", title="DOGMA-seq study, six patients",
        assay_profiles=[AssayProfile(
            label="dogma_seq",
            omics_layers=[
                OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME, OmicsLayer.PROTEIN,
            ],
            pairing_level=PairingLevel.SAME_CELL,
            inferred_from=ProfileBasis.NAMED_TECHNOLOGY,
            rationale="Named co-assay technology was observed.",
        )],
    )
    assets = [
        _asset("a1", DataRole.FRAGMENTS, "atac_fragments.tsv.gz"),
        _asset("a2", DataRole.RNA_MATRIX, "filtered_feature_bc_matrix.h5"),
        _asset("a3", DataRole.PROTEIN_MATRIX, "protein_matrix.h5"),
        _asset("a4", DataRole.CELL_METADATA, "barcodes.tsv.gz"),
    ]
    requirement = ResearchRequirement(
        question="paired same-cell multiome",
        pairing_requirement=PairingRequirement(
            acceptable_levels=[PairingLevel.SAME_CELL, PairingLevel.SAME_NUCLEUS]
        ),
    )
    database = tmp_path / "provisional.sqlite"
    ledger = CollaborationLedger(database)
    run = ledger.create_run(requirement.question, {"use_llm": False})
    discovery = DiscoveryRunResult(
        run_id=run.run_id, requirement=requirement,
        query_plan=QueryPlan(requirement=requirement, queries=[]),
        candidates=[candidate], source_records=[], feasibility_reports=[],
    )
    context = AgentContext(
        run_id=run.run_id, question=requirement.question, database_path=str(database),
        ledger=ledger, policy={"use_llm": False},
        requirement=requirement, discovery=discovery,
    )
    context.artifacts.append(artifact(
        run.run_id, ArtifactKind.ASSET_INVENTORY_DELTA, InventoryReconcileAgent.name,
        AssetInventoryDelta(
            inventory_id="delta_1", run_id=run.run_id, phase=ScoutPhase.DIRECT, assets=assets,
        ).model_dump(mode="json"),
    ))
    # No GEO_SAMPLE_METADATA artifact at all -- exactly the "no per-sample
    # metadata, no LLM" scenario that forces PairUnitResolver's fallback.

    reconcile_agent = InventoryReconcileAgent(
        resolver=PairUnitResolver(client=None), verifier=PairingVerifier(client=None)
    )
    inventory_outputs = await reconcile_agent.run(context)
    context.artifacts.extend(inventory_outputs)

    # Sanity: confirm this really did hit the fallback (provisional) path,
    # not real per-sample resolution.
    payload = inventory_outputs[0].payload
    assert len(payload["bundles"]) == 1
    assert payload["proposed_pair_units"][0]["is_resolved"] is False
    assert payload["pairing_claims"] == []  # PairingVerifier refused to verify it

    preflight_outputs = await ProgressivePreflightAssessmentAgent().run(context)
    report = preflight_outputs[0].payload["reports"][0]

    assert report["dimensions"]["pairing_evidence"] == "unknown"
    assert report["decision"] != BundleStatus.PREFLIGHT_READY.value
    assert report["decision"] == BundleStatus.NEEDS_EVIDENCE.value


@pytest.mark.asyncio
async def test_a_genuinely_resolved_unit_from_the_same_technology_does_pass(tmp_path):
    """Confirms the guard is about resolution status, not about penalizing
    named technologies -- once a unit really is resolved, its verified claim
    still lets it reach PREFLIGHT_READY."""
    candidate = DatasetCandidate(
        candidate_id="cand_1", title="DOGMA-seq study, one patient",
        assay_profiles=[AssayProfile(
            label="dogma_seq",
            omics_layers=[
                OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME, OmicsLayer.PROTEIN,
            ],
            pairing_level=PairingLevel.SAME_CELL,
            inferred_from=ProfileBasis.NAMED_TECHNOLOGY,
            rationale="Named co-assay technology was observed.",
        )],
    )
    assets = [
        _asset("a1", DataRole.FRAGMENTS, "GSM1_atac_fragments.tsv.gz"),
        _asset("a2", DataRole.RNA_MATRIX, "GSM1_filtered_feature_bc_matrix.h5"),
        _asset("a3", DataRole.PROTEIN_MATRIX, "GSM1_protein_matrix.h5"),
        _asset("a4", DataRole.CELL_METADATA, "GSM1_barcodes.tsv.gz"),
    ]
    sample = GeoSampleRecord(
        gsm_accession="GSM1", series_accession="GSE1", title="Sample 1",
        supplementary_files=[
            "GSM1_atac_fragments.tsv.gz", "GSM1_filtered_feature_bc_matrix.h5",
            "GSM1_protein_matrix.h5", "GSM1_barcodes.tsv.gz",
        ],
        evidence_ids=["ev_gsm1"],
    )
    requirement = ResearchRequirement(
        question="paired same-cell multiome",
        pairing_requirement=PairingRequirement(
            acceptable_levels=[PairingLevel.SAME_CELL, PairingLevel.SAME_NUCLEUS]
        ),
    )
    database = tmp_path / "resolved.sqlite"
    ledger = CollaborationLedger(database)
    run = ledger.create_run(requirement.question, {"use_llm": False})
    discovery = DiscoveryRunResult(
        run_id=run.run_id, requirement=requirement,
        query_plan=QueryPlan(requirement=requirement, queries=[]),
        candidates=[candidate], source_records=[], feasibility_reports=[],
    )
    context = AgentContext(
        run_id=run.run_id, question=requirement.question, database_path=str(database),
        ledger=ledger, policy={"use_llm": False},
        requirement=requirement, discovery=discovery,
    )
    context.artifacts.append(artifact(
        run.run_id, ArtifactKind.ASSET_INVENTORY_DELTA, InventoryReconcileAgent.name,
        AssetInventoryDelta(
            inventory_id="delta_1", run_id=run.run_id, phase=ScoutPhase.DIRECT, assets=assets,
        ).model_dump(mode="json"),
    ))
    context.artifacts.append(artifact(
        run.run_id, ArtifactKind.GEO_SAMPLE_METADATA, InventoryReconcileAgent.name,
        {"samples_by_candidate": {"cand_1": [sample.model_dump(mode="json")]}},
    ))

    from scdata_agent.application.agents.pair_unit_resolver import (
        PairUnitResolutionDraft,
    )

    class _FakeClient:
        async def complete(self, messages, *, temperature=0.1, max_tokens=2_000):
            return PairUnitResolutionDraft(units=[{
                "proposed_unit_id": "only_unit", "member_samples": ["GSM1"],
                "grouping_rationale": "Single patient study, one sample.",
            }]).model_dump_json()

    # A resolver that really ran -- with at least one real GEO sample to
    # reason over -- marks its output resolved.
    resolver = PairUnitResolver(client=_FakeClient())
    verifier = PairingVerifier(client=None)

    reconcile_agent = InventoryReconcileAgent(resolver=resolver, verifier=verifier)
    inventory_outputs = await reconcile_agent.run(context)
    context.artifacts.extend(inventory_outputs)

    payload = inventory_outputs[0].payload
    assert payload["proposed_pair_units"][0]["is_resolved"] is True
    assert len(payload["pairing_claims"]) == 1

    preflight_outputs = await ProgressivePreflightAssessmentAgent().run(context)
    report = preflight_outputs[0].payload["reports"][0]

    assert report["dimensions"]["pairing_evidence"] == "pass"
    assert report["decision"] == BundleStatus.PREFLIGHT_READY.value
