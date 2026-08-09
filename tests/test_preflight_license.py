"""Licence precedence at preflight.

Archive terms may only fill a genuine gap. Everything else — a licence that was
read, an operator approval, an explicit restriction, strict mode — must take
priority over them.
"""

import pytest

from scdata_agent.application.agents.progressive_preflight import (
    ProgressivePreflightAssessmentAgent,
)
from scdata_agent.application.agents.shared import AgentContext
from scdata_agent.application.collaboration import (
    AgentName,
    ArtifactKind,
    CollaborationLedger,
    artifact,
)
from scdata_agent.domain.lifecycle import LicenseStatus, SemanticBundle
from scdata_agent.domain.models import (
    AccessLevel,
    AssayProfile,
    AssetAvailability,
    AssetLocation,
    DataAsset,
    DataRole,
    DatasetCandidate,
    DiscoveryRunResult,
    OmicsLayer,
    QueryPlan,
    ResearchRequirement,
    TransferProtocol,
)
from scdata_agent.domain.resource_pipeline import (
    BundleAccessStatus,
    RemoteBundleInventorySummary,
)

NCBI_URL = "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE1nnn/fragments.tsv.gz"
ENA_URL = "ftp://ftp.sra.ebi.ac.uk/vol1/fastq/SRR1/SRR1_1.fastq.gz"
OTHER_URL = "https://example.com/fragments.tsv.gz"


def _asset(url: str, license_id: str | None = None) -> DataAsset:
    return DataAsset(
        asset_id="asset_1",
        candidate_id="cand_1",
        role=DataRole.FRAGMENTS,
        file_name="fragments.tsv.gz",
        license_id=license_id,
        locations=[
            AssetLocation(
                url=url,
                protocol=TransferProtocol.HTTPS,
                access_level=AccessLevel.PUBLIC_VERIFIED,
                availability=AssetAvailability.OBSERVED,
                size_bytes=10,
            )
        ],
    )


async def _report(tmp_path, assets, **policy):
    requirement = ResearchRequirement(
        question="scATAC",
        required_omics_layers=[OmicsLayer.CHROMATIN_ACCESSIBILITY],
    )
    candidate = DatasetCandidate(
        candidate_id="cand_1", title="study",
        assay_profiles=[AssayProfile(
            label="scatac", omics_layers=[OmicsLayer.CHROMATIN_ACCESSIBILITY], rationale="x",
        )],
    )
    discovery = DiscoveryRunResult(
        run_id="run_1", requirement=requirement,
        query_plan=QueryPlan(requirement=requirement, queries=[]),
        source_records=[], candidates=[candidate], feasibility_reports=[],
    )
    bundle = SemanticBundle(
        bundle_id="bundle_1", candidate_id="cand_1", bundle_type="processed",
        asset_ids=[item.asset_id for item in assets],
        applicable_schema_ids=["scatac_standalone"],
        satisfied_schema_ids=["scatac_standalone"],
    )
    summary = RemoteBundleInventorySummary(
        summary_id="summary_1", bundle_id="bundle_1", candidate_id="cand_1",
        access_status=BundleAccessStatus.VERIFIED, asset_count=len(assets),
        verified_asset_count=len(assets), known_bytes=10, total_bytes=10,
        reported_layers=[OmicsLayer.CHROMATIN_ACCESSIBILITY.value],
    )
    database = tmp_path / "preflight.sqlite"
    context = AgentContext(
        run_id="run_1", question="scATAC", database_path=str(database),
        ledger=CollaborationLedger(database),
        policy={"use_llm": False, **policy},
        requirement=requirement, discovery=discovery,
    )
    context.artifacts.append(
        artifact("run_1", ArtifactKind.ASSET_INVENTORY, AgentName.ASSET_SCOUT, {
            "assets": [item.model_dump(mode="json") for item in assets],
            "bundles": [bundle.model_dump(mode="json")],
            "bundle_summaries": [summary.model_dump(mode="json")],
        })
    )
    outputs = await ProgressivePreflightAssessmentAgent().run(context)
    return outputs[0].payload["reports"][0]


@pytest.mark.asyncio
async def test_archive_terms_fill_a_genuine_gap(tmp_path):
    report = await _report(tmp_path, [_asset(NCBI_URL)])

    assert report["license_status"] == LicenseStatus.REPOSITORY_POLICY
    assert report["license_policy_ids"] == ["ncbi_public_domain_policy"]
    # The basis must be legible, not just a status code.
    assert "ncbi.nlm.nih.gov" in report["license_basis"]
    assert any(item.startswith("license:repository_policy:") for item in report["warnings"])


@pytest.mark.asyncio
async def test_a_declared_allowlisted_licence_outranks_archive_terms(tmp_path):
    report = await _report(
        tmp_path, [_asset(NCBI_URL, "CC-BY-4.0")],
        approved_license_ids=["cc-by-4.0"],
    )

    assert report["license_status"] == LicenseStatus.APPROVED
    # Policy was never consulted, so no basis is recorded.
    assert report["license_policy_ids"] == []


@pytest.mark.asyncio
async def test_an_unrecognised_declared_licence_does_not_fall_through(tmp_path):
    """A real but un-allowlisted licence stays unknown; silence is not consent."""
    report = await _report(tmp_path, [_asset(NCBI_URL, "CC-BY-NC-4.0")])

    assert report["license_status"] == LicenseStatus.UNKNOWN
    assert report["license_policy_ids"] == []
    assert "license:unknown" in report["warnings"]


@pytest.mark.asyncio
async def test_an_explicit_restriction_beats_archive_terms(tmp_path):
    report = await _report(
        tmp_path, [_asset(NCBI_URL, "no-redistribution")],
        restricted_license_ids=["no-redistribution"],
    )

    assert report["license_status"] == LicenseStatus.RESTRICTED
    assert "license:restricted" in report["hard_blockers"]


@pytest.mark.asyncio
async def test_strict_mode_disables_archive_terms(tmp_path):
    report = await _report(tmp_path, [_asset(NCBI_URL)], strict_license=True)

    assert report["license_status"] == LicenseStatus.UNKNOWN
    assert report["license_policy_ids"] == []


@pytest.mark.asyncio
async def test_a_bundle_spanning_an_uncovered_host_gets_no_policy(tmp_path):
    assets = [_asset(NCBI_URL), _asset(OTHER_URL).model_copy(update={"asset_id": "asset_2"})]
    report = await _report(tmp_path, assets)

    assert report["license_status"] == LicenseStatus.UNKNOWN


@pytest.mark.asyncio
async def test_a_deposition_split_across_archives_cites_every_policy(tmp_path):
    """Processed files on GEO with raw reads on ENA is the normal shape."""
    assets = [
        _asset(NCBI_URL),
        _asset(ENA_URL).model_copy(update={"asset_id": "asset_2"}),
    ]
    report = await _report(tmp_path, assets)

    assert report["license_status"] == LicenseStatus.REPOSITORY_POLICY
    assert report["license_policy_ids"] == [
        "embl_ebi_terms_of_use", "ncbi_public_domain_policy",
    ]
    # Both bases travel with the report, not just the first one found.
    assert "ebi.ac.uk" in report["license_basis"]
    assert "ncbi.nlm.nih.gov" in report["license_basis"]
