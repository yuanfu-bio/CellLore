import gzip
import hashlib
from pathlib import Path

import httpx
import pytest

from scdata_agent.domain.lifecycle import (
    AcquisitionBundle,
    AcquisitionPlan,
    BundleStatus,
    MaterializationStatus,
    PointerStatus,
    ResourcePointer,
    SemanticBundle,
    validate_bundle_transition,
)
from scdata_agent.domain.models import (
    AccessLevel,
    AssetAvailability,
    AssetLocation,
    DataAsset,
    DataRole,
    DatasetCandidate,
    Identifier,
    TransferProtocol,
)
from scdata_agent.infrastructure.materialization import (
    DatasetMaterializer,
    MaterializedQualityInspector,
)
from scdata_agent.infrastructure.persistence.catalog import Catalog
from scdata_agent.infrastructure.sources.article_access import (
    OpenArticleAccessService,
)
from scdata_agent.infrastructure.sources.asset_registry import (
    AssetResolverRegistry,
)
from scdata_agent.infrastructure.transfer_executor import TransferExecutor


def test_pointer_maturity_and_bundle_transitions(tmp_path):
    pointer = ResourcePointer(
        pointer_id="pointer_1",
        candidate_id="candidate_1",
        namespace="sra_run",
        locator="SRR1",
        source_name="article",
        status=PointerStatus.REPORTED,
    )
    inferred = pointer.model_copy(update={"status": PointerStatus.INFERRED})
    assert pointer.scoutable
    assert not inferred.scoutable

    validate_bundle_transition(
        BundleStatus.REMOTE_INVENTORIED,
        BundleStatus.PREFLIGHT_READY,
    )
    with pytest.raises(ValueError):
        validate_bundle_transition(
            BundleStatus.REMOTE_INVENTORIED,
            BundleStatus.ADMITTED,
        )

    catalog = Catalog(tmp_path / "catalog.sqlite")
    catalog.append(
        record_id=pointer.pointer_id,
        record_type="resource_pointer",
        run_id="run_1",
        value=pointer,
    )
    catalog.append(
        record_id=pointer.pointer_id,
        record_type="resource_pointer",
        run_id="run_2",
        value=pointer.model_copy(update={"status": PointerStatus.VALIDATED}),
    )
    assert len(
        catalog.list_records(record_type="resource_pointer")
    ) == 2
    assert (
        catalog.latest("resource_pointer", pointer.pointer_id)["payload"][
            "status"
        ]
        == "validated"
    )


@pytest.mark.asyncio
async def test_open_article_access_uses_only_pmc_xml():
    xml = b"<article><body><p>Data are in GSE12345.</p></body></article>"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/PMC123/fullTextXML")
        return httpx.Response(200, content=xml, request=request)

    candidate_record = DatasetCandidate  # keep import boundary explicit
    del candidate_record
    from scdata_agent.domain.models import SourceRecord

    record = SourceRecord(
        record_id="record_1",
        source_name="europe_pmc",
        external_id="123",
        title="paper",
        publication_identifiers=[
            Identifier(namespace="pmcid", value="PMC123")
        ],
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        content = await OpenArticleAccessService(client).fetch(record)
    assert content is not None
    assert "GSE12345" in content.text
    assert content.snapshot_hash.startswith("sha256:")


@pytest.mark.asyncio
async def test_asset_registry_resolves_zenodo_and_figshare():
    def handler(request: httpx.Request) -> httpx.Response:
        if "zenodo.org" in request.url.host:
            return httpx.Response(
                200,
                json={
                    "files": [
                        {
                            "key": "sample_fragments.tsv.gz",
                            "size": 12,
                            "checksum": "md5:abc",
                            "links": {
                                "content": "https://files.test/fragments"
                            },
                        }
                    ]
                },
                request=request,
            )
        if "figshare.com" in request.url.host:
            return httpx.Response(
                200,
                json={
                    "files": [
                        {
                            "name": "cells.tsv",
                            "size": 4,
                            "supplied_md5": "def",
                            "download_url": "https://files.test/cells",
                        }
                    ]
                },
                request=request,
            )
        raise AssertionError(request.url)

    candidate = DatasetCandidate(
        candidate_id="candidate_1",
        title="dataset",
        identifiers=[
            Identifier(namespace="zenodo_record", value="10"),
            Identifier(namespace="figshare_article", value="20"),
        ],
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        result = await AssetResolverRegistry(client=client).resolve(candidate)
    assert {item.file_name for item in result.assets} == {
        "sample_fragments.tsv.gz",
        "cells.tsv",
    }
    assert result.assets[0].locations[0].availability == AssetAvailability.DECLARED


@pytest.mark.asyncio
async def test_transfer_requires_size_override_and_writes_checksum_receipt(
    tmp_path,
):
    content = b"verified payload"
    checksum = hashlib.md5(content).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content, request=request)

    asset = DataAsset(
        asset_id="asset_1",
        candidate_id="candidate_1",
        role=DataRole.FRAGMENTS,
        file_name="fragments.tsv",
        locations=[
            AssetLocation(
                url="https://files.test/fragments.tsv",
                protocol=TransferProtocol.HTTPS,
                access_level=AccessLevel.PUBLIC_VERIFIED,
                availability=AssetAvailability.OBSERVED,
                checksum_algorithm="md5",
                checksum=checksum,
            )
        ],
    )
    bundle = SemanticBundle(
        bundle_id="bundle_1",
        candidate_id="candidate_1",
        bundle_type="processed",
        asset_ids=["asset_1"],
        required_roles=[DataRole.FRAGMENTS],
    )
    plan = AcquisitionPlan(
        plan_id="plan_1",
        run_id="run_1",
        bundles=[
            AcquisitionBundle(
                bundle_id="bundle_1",
                candidate_id="candidate_1",
                asset_ids=["asset_1"],
                priority=1,
                score=100,
                requires_size_override=True,
            )
        ],
        recommended_bundle_ids=[],
        destination_root=str(tmp_path / "raw"),
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        executor = TransferExecutor(client)
        with pytest.raises(ValueError):
            await executor.execute(
                plan, [bundle], [asset], {"bundle_1"}
            )
        receipts = await executor.execute(
            plan,
            [bundle],
            [asset],
            {"bundle_1"},
            allow_unknown_size=True,
        )
    assert receipts[0].checksum_valid
    assert Path(receipts[0].local_path).read_bytes() == content


def test_processed_first_materialization_and_bounded_local_qc(tmp_path):
    fragments = tmp_path / "fragments.tsv.gz"
    with gzip.open(fragments, "wt") as handle:
        handle.write("chr1\t10\t20\tcell-1\t1\n")
        handle.write("chr1\t30\t40\tcell-2\t1\n")
    from scdata_agent.domain.lifecycle import (
        TransferReceipt,
        TransferReceiptStatus,
    )

    receipt = TransferReceipt(
        receipt_id="receipt_1",
        plan_id="plan_1",
        bundle_id="bundle_1",
        asset_id="asset_1",
        source_url="https://files.test/fragments",
        local_path=str(fragments),
        protocol=TransferProtocol.HTTPS,
        status=TransferReceiptStatus.COMPLETED,
        bytes_written=fragments.stat().st_size,
    )
    asset = DataAsset(
        asset_id="asset_1",
        candidate_id="candidate_1",
        role=DataRole.FRAGMENTS,
        file_name=fragments.name,
    )
    bundle = SemanticBundle(
        bundle_id="bundle_1",
        candidate_id="candidate_1",
        bundle_type="processed",
        asset_ids=["asset_1"],
        required_roles=[DataRole.FRAGMENTS],
    )
    manifest = DatasetMaterializer().materialize(
        bundle=bundle,
        assets=[asset],
        receipts=[receipt],
        curated_root=tmp_path / "curated",
    )
    report = MaterializedQualityInspector().inspect(
        manifest, [receipt]
    )
    assert manifest.status == MaterializationStatus.MATERIALIZED
    assert Path(manifest.data_card_path).is_file()
    assert report.metrics["fragment_rows_sampled"] == 2
    assert report.metrics["sampled_fragment_barcodes"] == 2
    assert report.status.value == "warn"
    assert "tss_enrichment" in report.unavailable_metrics
