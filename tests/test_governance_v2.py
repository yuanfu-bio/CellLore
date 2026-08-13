import pytest

from celllore.application.agents.shared import AgentContext
from celllore.application.agents.snapshot_governance import (
    SnapshotCorpusGovernanceAgent,
)
from celllore.application.collaboration import (
    AgentName,
    ArtifactKind,
    CollaborationLedger,
    artifact,
)
from celllore.domain.curation.profiles.paired_atac_rna_human import (
    PAIRED_ATAC_RNA_HUMAN,
)
from celllore.domain.identifiers import stable_id
from celllore.domain.lifecycle import (
    BundleStatus,
    EvidenceClaim,
    LicenseStatus,
    LineageFamily,
    LineageGraphSnapshot,
    MaterializationManifest,
    MaterializationStatus,
    MaterializedQCReport,
    PreflightBundleReport,
)
from celllore.domain.models import EvidenceClass, GateStatus, PairingLevel, ResearchRequirement
from celllore.infrastructure.persistence.catalog import Catalog


@pytest.mark.asyncio
async def test_governance_excludes_evaluation_lineage_and_admits_verified_bundle(
    tmp_path,
):
    database = tmp_path / "system.sqlite"
    ledger = CollaborationLedger(database)
    run = ledger.create_run("test")
    catalog = Catalog(database)
    reports = [
        PreflightBundleReport(
            report_id=f"preflight_{index}",
            bundle_id=f"bundle_{index}",
            candidate_id=f"candidate_{index}",
            dimensions={},
            license_status=LicenseStatus.APPROVED,
            decision=BundleStatus.PREFLIGHT_READY,
        )
        for index in (1, 2)
    ]
    manifests = [
        MaterializationManifest(
            manifest_id=f"manifest_{index}",
            bundle_id=f"bundle_{index}",
            candidate_id=f"candidate_{index}",
            status=MaterializationStatus.MATERIALIZED,
            package_path=str(tmp_path / f"bundle_{index}"),
            standard_object_path=str(tmp_path / f"bundle_{index}.h5ad"),
        )
        for index in (1, 2)
    ]
    quality = [
        MaterializedQCReport(
            report_id=f"qc_{index}",
            bundle_id=f"bundle_{index}",
            candidate_id=f"candidate_{index}",
            status=GateStatus.PASS,
        )
        for index in (1, 2)
    ]
    lineage = LineageGraphSnapshot(
        snapshot_id="lineage_late",
        run_id=run.run_id,
        phase="late",
        families=[
            LineageFamily(
                family_id="evaluation_family",
                candidate_ids=["candidate_1"],
                bundle_ids=["bundle_1"],
            )
        ],
        evaluation_family_ids=["evaluation_family"],
    )
    artifacts = [
        artifact(
            run.run_id,
            ArtifactKind.PREFLIGHT_REPORT,
            AgentName.PREFLIGHT_ASSESSMENT,
            {"reports": [item.model_dump(mode="json") for item in reports]},
        ),
        artifact(
            run.run_id,
            ArtifactKind.MATERIALIZATION_MANIFEST,
            AgentName.DATASET_CURATOR,
            {"manifests": [item.model_dump(mode="json") for item in manifests]},
        ),
        artifact(
            run.run_id,
            ArtifactKind.MATERIALIZED_QC,
            AgentName.QUALITY_ASSESSMENT,
            {"reports": [item.model_dump(mode="json") for item in quality]},
        ),
        artifact(
            run.run_id,
            ArtifactKind.LINEAGE_REPORT,
            AgentName.LINEAGE_LEAKAGE,
            lineage.model_dump(mode="json"),
        ),
    ]
    for item in artifacts:
        ledger.add_artifact(item)
    for bundle_id in ("bundle_1", "bundle_2"):
        catalog.transition_bundle(
            bundle_id, BundleStatus.QC_PASSED, run_id=run.run_id
        )
    context = AgentContext(
        run_id=run.run_id,
        question="test",
        database_path=str(database),
        ledger=ledger,
        artifacts=artifacts,
        catalog=catalog,
    )
    outputs = await SnapshotCorpusGovernanceAgent().run(context)
    snapshot = outputs[1].payload
    assert snapshot["excluded_bundle_ids"] == ["bundle_1"]
    assert snapshot["admitted_bundle_ids"] == ["bundle_2"]
    assert catalog.bundle_status("bundle_1") == BundleStatus.EXCLUDED
    assert catalog.bundle_status("bundle_2") == BundleStatus.ADMITTED


@pytest.mark.asyncio
async def test_governance_does_not_overwrite_a_rejected_bundle(tmp_path):
    database = tmp_path / "rejected_bundle.sqlite"
    ledger = CollaborationLedger(database)
    run = ledger.create_run("test")
    catalog = Catalog(database)
    report = PreflightBundleReport(
        report_id="preflight_1",
        bundle_id="bundle_1",
        candidate_id="candidate_1",
        dimensions={},
        license_status=LicenseStatus.REPOSITORY_POLICY,
        decision=BundleStatus.REJECTED,
    )
    preflight_artifact = artifact(
        run.run_id,
        ArtifactKind.PREFLIGHT_REPORT,
        AgentName.PREFLIGHT_ASSESSMENT,
        {"reports": [report.model_dump(mode="json")]},
    )
    ledger.add_artifact(preflight_artifact)
    catalog.transition_bundle(
        "bundle_1", BundleStatus.REJECTED, run_id=run.run_id
    )

    outputs = await SnapshotCorpusGovernanceAgent().run(
        AgentContext(
            run_id=run.run_id,
            question="test",
            database_path=str(database),
            ledger=ledger,
            artifacts=[preflight_artifact],
            catalog=catalog,
        )
    )

    snapshot = outputs[1].payload
    assert snapshot["quarantined_bundle_ids"] == ["bundle_1"]
    assert catalog.bundle_status("bundle_1") == BundleStatus.REJECTED


@pytest.mark.asyncio
async def test_governance_quarantines_preflight_ready_bundle_without_materialization(tmp_path):
    database = tmp_path / "preflight_ready_bundle.sqlite"
    ledger = CollaborationLedger(database)
    run = ledger.create_run("test")
    catalog = Catalog(database)
    report = PreflightBundleReport(
        report_id="preflight_1",
        bundle_id="bundle_1",
        candidate_id="candidate_1",
        dimensions={},
        license_status=LicenseStatus.REPOSITORY_POLICY,
        decision=BundleStatus.PREFLIGHT_READY,
    )
    preflight_artifact = artifact(
        run.run_id,
        ArtifactKind.PREFLIGHT_REPORT,
        AgentName.PREFLIGHT_ASSESSMENT,
        {"reports": [report.model_dump(mode="json")]},
    )
    ledger.add_artifact(preflight_artifact)
    catalog.transition_bundle(
        "bundle_1", BundleStatus.PREFLIGHT_READY, run_id=run.run_id
    )

    outputs = await SnapshotCorpusGovernanceAgent().run(
        AgentContext(
            run_id=run.run_id,
            question="test",
            database_path=str(database),
            ledger=ledger,
            artifacts=[preflight_artifact],
            catalog=catalog,
        )
    )

    snapshot = outputs[1].payload
    assert snapshot["quarantined_bundle_ids"] == ["bundle_1"]
    assert catalog.bundle_status("bundle_1") == BundleStatus.QUARANTINED


def _corpus_ready_gate_context(tmp_path, *, with_recomputed_pairing_claim: bool):
    """A profile-selected, pair-unit-scoped bundle with declared-only pairing
    evidence (REPOSITORY_REPORTED, sufficient for ACQUISITION) and everything
    else it needs for CORPUS_READY except the barcode-overlap-recomputed
    pairing claim -- present only when `with_recomputed_pairing_claim`."""
    database = tmp_path / "corpus_ready_gate.sqlite"
    ledger = CollaborationLedger(database)
    run = ledger.create_run("test")
    catalog = Catalog(database)
    requirement = ResearchRequirement(
        question="paired multiome", profile_id=PAIRED_ATAC_RNA_HUMAN.profile_id,
    )
    report = PreflightBundleReport(
        report_id="preflight_1", bundle_id="bundle_1", candidate_id="candidate_1",
        dimensions={}, license_status=LicenseStatus.APPROVED,
        decision=BundleStatus.PREFLIGHT_READY,
    )
    manifest = MaterializationManifest(
        manifest_id="manifest_1", bundle_id="bundle_1", candidate_id="candidate_1",
        status=MaterializationStatus.MATERIALIZED, package_path=str(tmp_path / "bundle_1"),
        genome_builds=["GRCh38"],
    )
    qc = MaterializedQCReport(
        report_id="qc_1", bundle_id="bundle_1", candidate_id="candidate_1",
        status=GateStatus.PASS,
    )
    layer_claims = [
        EvidenceClaim(
            claim_id=stable_id("claim", "atac_layer"), subject_id="candidate_1",
            predicate="has_omics_layer:chromatin_accessibility", value=True,
            evidence_class=EvidenceClass.REPOSITORY_REPORTED,
        ),
        EvidenceClaim(
            claim_id=stable_id("claim", "rna_layer"), subject_id="candidate_1",
            predicate="has_omics_layer:transcriptome", value=True,
            evidence_class=EvidenceClass.REPOSITORY_REPORTED,
        ),
    ]
    declared_pairing_claim = EvidenceClaim(
        claim_id=stable_id("claim", "declared_pairing"), subject_id="pair_unit_1",
        predicate="has_pair_relationship", value=PairingLevel.SAME_CELL.value,
        evidence_class=EvidenceClass.REPOSITORY_REPORTED,
    )
    assembly_claim = EvidenceClaim(
        claim_id=stable_id("claim", "assembly"), subject_id="bundle_1",
        predicate="resolved_assembly", value=["GRCh38"],
        evidence_class=EvidenceClass.REPOSITORY_REPORTED,
    )
    quality_payload = {
        "reports": [qc.model_dump(mode="json")],
        "pairing_claims": (
            [
                EvidenceClaim(
                    claim_id=stable_id("claim", "recomputed_pairing"),
                    subject_id="pair_unit_1", predicate="has_pair_relationship",
                    value=PairingLevel.SAME_CELL.value,
                    evidence_class=EvidenceClass.RECOMPUTED,
                ).model_dump(mode="json")
            ]
            if with_recomputed_pairing_claim
            else []
        ),
    }
    artifacts = [
        artifact(
            run.run_id, ArtifactKind.PREFLIGHT_REPORT, AgentName.PREFLIGHT_ASSESSMENT,
            {
                "reports": [report.model_dump(mode="json")],
                "assembly_claims": [assembly_claim.model_dump(mode="json")],
            },
        ),
        artifact(
            run.run_id, ArtifactKind.MATERIALIZATION_MANIFEST, AgentName.DATASET_CURATOR,
            {"manifests": [manifest.model_dump(mode="json")]},
        ),
        artifact(
            run.run_id, ArtifactKind.MATERIALIZED_QC, AgentName.QUALITY_ASSESSMENT,
            quality_payload,
        ),
        artifact(
            run.run_id, ArtifactKind.ASSET_INVENTORY, AgentName.ASSET_SCOUT,
            {
                "bundles": [{"bundle_id": "bundle_1", "pair_unit_id": "pair_unit_1"}],
                "layer_claims": [item.model_dump(mode="json") for item in layer_claims],
                "pairing_claims": [declared_pairing_claim.model_dump(mode="json")],
            },
        ),
    ]
    for item in artifacts:
        ledger.add_artifact(item)
    catalog.transition_bundle("bundle_1", BundleStatus.QC_PASSED, run_id=run.run_id)
    return AgentContext(
        run_id=run.run_id, question="test", database_path=str(database), ledger=ledger,
        artifacts=artifacts, catalog=catalog, requirement=requirement,
    )


class TestCorpusReadyEvidenceCompleteness:
    """A declared-only pairing claim
    (REPOSITORY_REPORTED) clears preflight's ACQUISITION gate but must not,
    by itself, also clear governance's stronger CORPUS_READY gate -- which
    requires FILE_OBSERVED/RECOMPUTED barcode-overlap evidence from the
    post-materialization validators."""

    @pytest.mark.asyncio
    async def test_declared_only_pairing_evidence_is_quarantined_at_corpus_ready(
        self, tmp_path
    ):
        context = _corpus_ready_gate_context(tmp_path, with_recomputed_pairing_claim=False)
        outputs = await SnapshotCorpusGovernanceAgent().run(context)
        snapshot = outputs[1].payload

        assert snapshot["quarantined_bundle_ids"] == ["bundle_1"]
        decision = snapshot["decisions"][0]
        assert any(
            reason.startswith("corpus_ready_evidence_incomplete:has_pair_relationship")
            for reason in decision["reasons"]
        )

    @pytest.mark.asyncio
    async def test_recomputed_barcode_overlap_evidence_reaches_admitted(self, tmp_path):
        context = _corpus_ready_gate_context(tmp_path, with_recomputed_pairing_claim=True)
        outputs = await SnapshotCorpusGovernanceAgent().run(context)
        snapshot = outputs[1].payload

        assert snapshot["admitted_bundle_ids"] == ["bundle_1"]
