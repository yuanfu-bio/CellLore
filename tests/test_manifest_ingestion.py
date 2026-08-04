"""A curated manifest must enter the same contracts discovery produces --
and the boundary CONTRIBUTING calls non-negotiable #2 must hold in code, not
just in a comment: a human writing VERIFIED means "I confirmed the URL
resolves," never "I observed the file" or "I read the licence."
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from scdata_agent.application.agents.manifest_ingest import ManifestIngestAgent
from scdata_agent.application.agents.shared import AgentContext
from scdata_agent.application.collaboration import CollaborationLedger
from scdata_agent.application.manifest_ingestion import ingest_manifest
from scdata_agent.domain.models import (
    AccessLevel,
    AssetAvailability,
    EvidenceClass,
    TransferProtocol,
)
from scdata_agent.infrastructure.manifest_reader import read_manifest
from scdata_agent.infrastructure.persistence.catalog import Catalog

FIXTURE = Path(__file__).parent / "data" / "manifest_paired_multiome_sample.csv"


def _ingest(**policy):
    document = read_manifest(FIXTURE)
    return document, ingest_manifest(document, run_id="run_test", filters={}, policy=policy)


def test_direct_rows_produce_one_bundle_per_pair_unit() -> None:
    _, result = _ingest()
    assert result.pair_unit_count == 5
    assert len(result.bundles) == 5
    assert result.row_count == 14  # 15 rows minus the one portal row, excluded by default


def test_neatseq_pair_unit_satisfies_multiome_same_cell() -> None:
    _, result = _ingest()
    bundle = next(
        item for item in result.bundles if item.candidate_id == "GSE178707_NEATseq"
    )
    assert "multiome_same_cell" in bundle.satisfied_schema_ids


def test_kidney_shared_metadata_pair_unit_satisfies_no_schema() -> None:
    _, result = _ingest()
    bundle = next(
        item for item in result.bundles if item.candidate_id == "GSE234788_kidney"
    )
    assert bundle.satisfied_schema_ids == []


def test_declared_assets_never_claim_observation() -> None:
    """Non-negotiable #2, enforced in code: DECLARED/PUBLIC_DECLARED only."""
    _, result = _ingest()
    for asset in result.assets:
        for location in asset.locations:
            assert location.availability != AssetAvailability.OBSERVED
            assert location.access_level != AccessLevel.PUBLIC_VERIFIED
    for observation in result.observations:
        assert observation.availability != AssetAvailability.OBSERVED
        assert observation.access_level != AccessLevel.PUBLIC_VERIFIED
        assert observation.probe_method == "curated_manifest"
        assert observation.http_status is None


def test_verified_rows_are_user_corrected_unverified_rows_are_not() -> None:
    document, result = _ingest(include_portal=True)
    manual_evidence_ids = {
        evidence_id
        for asset in result.assets
        if asset.locations[0].protocol == TransferProtocol.MANUAL
        for evidence_id in asset.evidence_ids
    }
    portal_evidence = [item for item in result.evidence if item.evidence_id in manual_evidence_ids]
    verified_evidence = [
        item for item in result.evidence if item.evidence_id not in manual_evidence_ids
    ]

    assert portal_evidence
    assert all(item.evidence_class == EvidenceClass.REPOSITORY_REPORTED for item in portal_evidence)
    assert all(item.snapshot_hash == document.source_sha256 for item in portal_evidence)

    assert verified_evidence
    assert all(item.evidence_class == EvidenceClass.USER_CORRECTED for item in verified_evidence)


def test_size_and_checksum_land_on_the_asset_location() -> None:
    _, result = _ingest()
    neatseq_fragments = next(
        asset
        for asset in result.assets
        if asset.file_name == "GSM5396336_lane2_atac_fragments.tsv.gz"
    )
    location = neatseq_fragments.locations[0]
    assert location.size_bytes is not None
    assert location.size_bytes > 0

    zenodo_with_checksum = [
        asset
        for asset in result.assets
        if asset.locations[0].checksum is not None
    ]
    assert zenodo_with_checksum
    for asset in zenodo_with_checksum:
        location = asset.locations[0]
        assert location.checksum_algorithm == "md5"
        assert len(location.checksum) == 32


def test_portal_rows_are_manual_with_no_size_and_excluded_by_default() -> None:
    document, default_result = _ingest()
    assert all(
        asset.locations[0].protocol != TransferProtocol.MANUAL
        for asset in default_result.assets
    )

    _, included_result = _ingest(include_portal=True)
    manual_assets = [
        asset
        for asset in included_result.assets
        if asset.locations[0].protocol == TransferProtocol.MANUAL
    ]
    assert manual_assets
    for asset in manual_assets:
        location = asset.locations[0]
        assert location.size_bytes is None
        assert location.availability == AssetAvailability.UNKNOWN


def test_file_name_is_a_bare_basename() -> None:
    _, result = _ingest()
    for asset in result.assets:
        assert asset.file_name == Path(asset.file_name).name
        assert "/" not in asset.file_name


def test_dataset_filter_narrows_candidates() -> None:
    _, result = _ingest()
    all_datasets = {item.candidate_id for item in result.discovery.candidates}
    assert len(all_datasets) == 5

    document = read_manifest(FIXTURE)
    filtered = ingest_manifest(
        document,
        run_id="run_test",
        filters={"dataset": ["GSE178707_NEATseq"]},
        policy={},
    )
    assert {item.candidate_id for item in filtered.discovery.candidates} == {
        "GSE178707_NEATseq"
    }
    assert filtered.filters == {"dataset": ["GSE178707_NEATseq"]}


@pytest.mark.asyncio
async def test_ingest_agent_produces_four_artifacts_without_probing(tmp_path) -> None:
    ledger = CollaborationLedger(tmp_path / "system.sqlite")
    catalog = Catalog(tmp_path / "system.sqlite")
    context = AgentContext(
        run_id="run_agent",
        question="curated manifest ingestion",
        database_path=str(tmp_path / "system.sqlite"),
        ledger=ledger,
        catalog=catalog,
        policy={"manifest_path": str(FIXTURE), "remote_probe_enabled": False},
    )

    artifacts = await ManifestIngestAgent().run(context)

    kinds = [item.kind.value for item in artifacts]
    assert kinds == [
        "resource_pointers",
        "discovery_result",
        "asset_inventory",
        "manifest_ingestion",
    ]
    assert context.discovery is not None
    assert len(context.discovery.candidates) == 5

    inventory = artifacts[2]
    assert len(inventory.payload["bundles"]) == 5
    for asset in inventory.payload["assets"]:
        for location in asset["locations"]:
            assert location["availability"] != "observed"

    ingestion = artifacts[3]
    assert ingestion.payload["dialect_id"] == "paired_multiome_zh_v1"
    assert len(ingestion.payload["sidecars"]) == ingestion.payload["row_count"]


class _FakeProbe:
    """Marks every direct location OBSERVED, without touching the network.

    Stands in for RemoteAvailabilityProbe the same way test_supervisor_v2.py's
    FakeResolver stands in for AssetResolverRegistry: it proves the agent
    wires the probe correctly without asserting anything about the probe's
    own (separately-tested) HTTP behaviour.
    """

    instances: list[_FakeProbe] = []

    def __init__(self, **kwargs) -> None:
        self.seen_assets = None
        _FakeProbe.instances.append(self)

    async def probe_assets(self, assets):
        self.seen_assets = list(assets)
        upgraded = []
        for asset in assets:
            location = asset.locations[0].model_copy(
                update={
                    "availability": AssetAvailability.OBSERVED,
                    "access_level": AccessLevel.PUBLIC_VERIFIED,
                }
            )
            upgraded.append(asset.model_copy(update={"locations": [location]}))
        return upgraded, []


@pytest.mark.asyncio
async def test_ingest_agent_probe_upgrades_direct_assets_and_skips_manual(
    tmp_path, monkeypatch
) -> None:
    _FakeProbe.instances.clear()
    monkeypatch.setattr(
        "scdata_agent.application.agents.manifest_ingest.RemoteAvailabilityProbe",
        _FakeProbe,
    )
    ledger = CollaborationLedger(tmp_path / "system.sqlite")
    context = AgentContext(
        run_id="run_agent_probe",
        question="curated manifest ingestion",
        database_path=str(tmp_path / "system.sqlite"),
        ledger=ledger,
        policy={
            "manifest_path": str(FIXTURE),
            "remote_probe_enabled": True,
            "include_portal": True,
        },
    )

    artifacts = await ManifestIngestAgent().run(context)

    probe = _FakeProbe.instances[0]
    assert all(
        asset.locations[0].protocol != TransferProtocol.MANUAL for asset in probe.seen_assets
    )

    inventory = next(item for item in artifacts if item.kind.value == "asset_inventory")
    by_id = {item["asset_id"]: item for item in inventory.payload["assets"]}
    direct_availabilities = {
        item["locations"][0]["availability"]
        for item in inventory.payload["assets"]
        if item["locations"][0]["protocol"] != "manual"
    }
    assert direct_availabilities == {"observed"}
    manual_availabilities = {
        item["locations"][0]["availability"]
        for item in inventory.payload["assets"]
        if item["locations"][0]["protocol"] == "manual"
    }
    assert manual_availabilities == {"unknown"}
    assert by_id  # sanity: the inventory is not empty


@pytest.mark.asyncio
async def test_remote_availability_probe_can_upgrade_a_declared_manifest_asset() -> None:
    """The real probe class, offline: a manifest DECLARED fact CAN legitimately
    become OBSERVED once our own code independently checks the URL -- that is
    not the human's claim, it is ours, so it does not violate non-negotiable #2.
    """
    from scdata_agent.infrastructure.remote_probe import RemoteAvailabilityProbe

    _, result = _ingest()
    target = result.assets[0]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Length": "123"}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        probed, observations = await RemoteAvailabilityProbe(client=client).probe_assets(
            [target]
        )

    assert probed[0].locations[0].availability == AssetAvailability.OBSERVED
    assert probed[0].locations[0].access_level == AccessLevel.PUBLIC_VERIFIED
    assert observations[0].http_status == 200
    assert observations[0].probe_method == "head"
