import httpx
import pytest

from celllore.domain.models import (
    AssetAvailability,
    DatasetCandidate,
    Identifier,
)
from celllore.infrastructure.sources.ena import ENAAssetResolver


@pytest.mark.asyncio
async def test_ena_resolves_paired_fastq_assets():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "run_accession": "SRR1",
                    "fastq_ftp": "ftp.sra.ebi.ac.uk/a_1.fastq.gz;"
                    "ftp.sra.ebi.ac.uk/a_2.fastq.gz",
                    "fastq_bytes": "10;20",
                    "fastq_md5": "aaa;bbb",
                }
            ],
            request=request,
        )

    candidate = DatasetCandidate(
        candidate_id="candidate_1",
        title="dataset",
        identifiers=[Identifier(namespace="sra_project", value="SRP1")],
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        result = await ENAAssetResolver(client).resolve(candidate)

    assert result.queried_accessions == ["SRP1"]
    assert [asset.file_name for asset in result.assets] == [
        "a_1.fastq.gz",
        "a_2.fastq.gz",
    ]
    assert result.assets[0].locations[0].size_bytes == 10
    assert (
        result.assets[0].locations[0].availability
        == AssetAvailability.DECLARED
    )


@pytest.mark.asyncio
async def test_bioproject_expansion_is_opt_in():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=[], request=request)

    candidate = DatasetCandidate(
        candidate_id="candidate_1",
        title="dataset",
        identifiers=[Identifier(namespace="bioproject", value="PRJNA1")],
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        result = await ENAAssetResolver(client).resolve(candidate)

    assert result.queried_accessions == []
    assert calls == 0
