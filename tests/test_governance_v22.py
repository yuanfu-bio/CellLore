import pytest

from scdata_agent.application.agents.acquisition_v2 import AcquisitionStrategistAgent
from scdata_agent.application.agents.progressive_lineage_v2 import ProgressiveLineageLeakageAgent
from scdata_agent.application.agents.shared import AgentContext
from scdata_agent.application.collaboration import (
    AgentName,
    ArtifactKind,
    CollaborationLedger,
    artifact,
)
from scdata_agent.domain.lifecycle import (
    BundleLineageAssessment,
    BundleStatus,
    LicenseStatus,
    LineageFamily,
    LineageGraphSnapshot,
    PreflightBundleReport,
    SemanticBundle,
)
from scdata_agent.domain.models import (
    AssetLocation,
    DataAsset,
    DataRole,
    DatasetCandidate,
    DiscoveryRunResult,
    Identifier,
    QueryPlan,
    ResearchRequirement,
    TransferProtocol,
)
from scdata_agent.domain.resource_pipeline import BundleAccessStatus, RemoteBundleInventorySummary


def context(tmp_path, candidates):
    requirement = ResearchRequirement(question="multiome")
    discovery = DiscoveryRunResult(
        run_id="discovery", requirement=requirement,
        query_plan=QueryPlan(requirement=requirement, queries=[]),
        source_records=[], candidates=candidates, feasibility_reports=[],
    )
    database = tmp_path / "v22.sqlite"
    return AgentContext(
        run_id="run_1", question="multiome", database_path=str(database),
        ledger=CollaborationLedger(database), policy={}, requirement=requirement,
        discovery=discovery,
    )


@pytest.mark.asyncio
async def test_lineage_is_transitive_and_evaluation_leakage_propagates(tmp_path):
    candidates = [
        DatasetCandidate(candidate_id="a", title="a", identifiers=[Identifier(namespace="geo", value="GSE1")]),
        DatasetCandidate(candidate_id="b", title="b", publication_identifiers=[Identifier(namespace="doi", value="10/x")]),
        DatasetCandidate(candidate_id="c", title="c", publication_identifiers=[Identifier(namespace="doi", value="10/x")]),
    ]
    ctx = context(tmp_path, candidates)
    ctx.policy["evaluation_identifiers"] = ["10/x"]
    assets = [
        DataAsset(asset_id="asset_a", candidate_id="a", role=DataRole.FRAGMENTS,
                  file_name="a.tsv", locations=[AssetLocation(
                      url="https://files.test/shared", protocol=TransferProtocol.HTTPS)]),
        DataAsset(asset_id="asset_b", candidate_id="b", role=DataRole.FRAGMENTS,
                  file_name="b.tsv", locations=[AssetLocation(
                      url="https://files.test/shared?token=1", protocol=TransferProtocol.HTTPS)]),
    ]
    bundles = [SemanticBundle(
        bundle_id=f"bundle_{item.candidate_id}", candidate_id=item.candidate_id,
        bundle_type="processed", asset_ids=[item.asset_id], required_roles=[DataRole.FRAGMENTS],
    ) for item in assets]
    bundles.append(SemanticBundle(bundle_id="bundle_c", candidate_id="c",
                                  bundle_type="processed", asset_ids=["asset_c"],
                                  required_roles=[DataRole.FRAGMENTS]))
    ctx.artifacts.append(artifact("run_1", ArtifactKind.ASSET_INVENTORY,
                                  AgentName.ASSET_SCOUT,
                                  {"assets": [item.model_dump(mode="json") for item in assets],
                                   "bundles": [item.model_dump(mode="json") for item in bundles],
                                   "pointers": []}))
    output = await ProgressiveLineageLeakageAgent("early").run(ctx)
    snapshot = LineageGraphSnapshot.model_validate(output[0].payload)
    assert len(snapshot.families) == 1
    assert snapshot.families[0].candidate_ids == ["a", "b", "c"]
    assert all(item.evaluation_leakage for item in snapshot.bundle_assessments)


@pytest.mark.asyncio
async def test_acquisition_uses_representative_manual_and_excluded_tiers(tmp_path):
    candidates = [DatasetCandidate(candidate_id=item, title=item) for item in ["a", "b", "c"]]
    ctx = context(tmp_path, candidates)
    bundles = [SemanticBundle(
        bundle_id=f"bundle_{item}", candidate_id=item, bundle_type="processed",
        asset_ids=[f"asset_{item}"], required_roles=[DataRole.FRAGMENTS],
    ) for item in ["a", "b", "c"]]
    summaries = [
        RemoteBundleInventorySummary(summary_id=f"summary_{item}", bundle_id=f"bundle_{item}",
            candidate_id=item, access_status=status, asset_count=1,
            verified_asset_count=1 if status == BundleAccessStatus.VERIFIED else 0,
            declared_asset_count=1 if status == BundleAccessStatus.DECLARED_ONLY else 0,
            known_bytes=10, total_bytes=10)
        for item, status in [("a", BundleAccessStatus.VERIFIED),
                             ("b", BundleAccessStatus.DECLARED_ONLY),
                             ("c", BundleAccessStatus.VERIFIED)]
    ]
    reports = [PreflightBundleReport(
        report_id=f"report_{item}", bundle_id=f"bundle_{item}", candidate_id=item,
        dimensions={}, license_status=LicenseStatus.APPROVED, expected_bytes=10,
        access_status=summaries[index].access_status.value,
        decision=BundleStatus.PREFLIGHT_READY,
    ) for index, item in enumerate(["a", "b", "c"])]
    lineage = LineageGraphSnapshot(
        snapshot_id="lineage", run_id="run_1", phase="early",
        families=[LineageFamily(family_id="family_ab", candidate_ids=["a", "b"],
                                bundle_ids=["bundle_a", "bundle_b"]),
                  LineageFamily(family_id="family_c", candidate_ids=["c"], bundle_ids=["bundle_c"])],
        evaluation_family_ids=["family_c"],
        bundle_assessments=[
            BundleLineageAssessment(bundle_id="bundle_a", candidate_id="a", family_ids=["family_ab"]),
            BundleLineageAssessment(bundle_id="bundle_b", candidate_id="b", family_ids=["family_ab"]),
            BundleLineageAssessment(bundle_id="bundle_c", candidate_id="c", family_ids=["family_c"], evaluation_leakage=True),
        ],
    )
    ctx.artifacts.extend([
        artifact("run_1", ArtifactKind.ASSET_INVENTORY, AgentName.ASSET_SCOUT,
                 {"assets": [], "bundles": [item.model_dump(mode="json") for item in bundles],
                  "bundle_summaries": [item.model_dump(mode="json") for item in summaries]}),
        artifact("run_1", ArtifactKind.PREFLIGHT_REPORT, AgentName.PREFLIGHT_ASSESSMENT,
                 {"reports": [item.model_dump(mode="json") for item in reports]}),
        artifact("run_1", ArtifactKind.LINEAGE_REPORT, AgentName.LINEAGE_LEAKAGE,
                 lineage.model_dump(mode="json")),
    ])
    output = await AcquisitionStrategistAgent().run(ctx)
    payload = output[0].payload
    assert payload["recommended_bundle_ids"] == ["bundle_a"]
    assert payload["manual_review_bundle_ids"] == ["bundle_b"]
    assert payload["excluded_bundle_ids"] == ["bundle_c"]


@pytest.mark.asyncio
async def test_unknown_license_alone_holds_back_an_otherwise_ready_bundle(tmp_path):
    """The licence gate is what keeps GEO data out of the recommended tier.

    GEO and SRA publish no structured licence field, so a bundle can be
    complete, access-verified and preflight-ready and still be held in manual
    review on `license:unknown` alone. This pins that behaviour so the reason a
    run recommends nothing stays visible rather than being mistaken for a bug.
    """
    candidates = [DatasetCandidate(candidate_id="a", title="a")]
    context(tmp_path, candidates)
    bundle = SemanticBundle(
        bundle_id="bundle_a", candidate_id="a", bundle_type="processed",
        asset_ids=["asset_a"], applicable_schema_ids=["scatac_standalone"],
        satisfied_schema_ids=["scatac_standalone"],
    )
    summary = RemoteBundleInventorySummary(
        summary_id="summary_a", bundle_id="bundle_a", candidate_id="a",
        access_status=BundleAccessStatus.VERIFIED, asset_count=1,
        verified_asset_count=1, known_bytes=10, total_bytes=10,
    )

    async def plan_with(license_status):
        local = context(tmp_path, candidates)
        report = PreflightBundleReport(
            report_id="report_a", bundle_id="bundle_a", candidate_id="a",
            dimensions={}, license_status=license_status, expected_bytes=10,
            access_status=summary.access_status.value,
            decision=BundleStatus.PREFLIGHT_READY,
        )
        local.artifacts.extend([
            artifact("run_1", ArtifactKind.ASSET_INVENTORY, AgentName.ASSET_SCOUT,
                     {"assets": [], "bundles": [bundle.model_dump(mode="json")],
                      "bundle_summaries": [summary.model_dump(mode="json")]}),
            artifact("run_1", ArtifactKind.PREFLIGHT_REPORT, AgentName.PREFLIGHT_ASSESSMENT,
                     {"reports": [report.model_dump(mode="json")]}),
        ])
        return (await AcquisitionStrategistAgent().run(local))[0].payload

    unknown = await plan_with(LicenseStatus.UNKNOWN)
    assert unknown["recommended_bundle_ids"] == []
    assert unknown["manual_review_bundle_ids"] == ["bundle_a"]
    assert unknown["bundles"][0]["risks"] == ["license:unknown"]

    # Asserting the licence is the only change needed to reach `recommended`.
    approved = await plan_with(LicenseStatus.APPROVED)
    assert approved["recommended_bundle_ids"] == ["bundle_a"]
    assert approved["bundles"][0]["risks"] == []
