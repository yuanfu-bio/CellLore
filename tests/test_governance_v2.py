import pytest

from scdata_agent.application.agents.shared import AgentContext
from scdata_agent.application.agents.snapshot_governance import (
    SnapshotCorpusGovernanceAgent,
)
from scdata_agent.application.collaboration import (
    AgentName,
    ArtifactKind,
    CollaborationLedger,
    artifact,
)
from scdata_agent.domain.lifecycle import (
    BundleStatus,
    LicenseStatus,
    LineageFamily,
    LineageGraphSnapshot,
    MaterializationManifest,
    MaterializationStatus,
    MaterializedQCReport,
    PreflightBundleReport,
)
from scdata_agent.domain.models import GateStatus
from scdata_agent.infrastructure.persistence.catalog import Catalog


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
