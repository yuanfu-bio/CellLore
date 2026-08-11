import pytest

from celllore.application.discovery import DiscoveryWorkflow
from celllore.domain.identifiers import stable_id
from celllore.domain.models import (
    AccessLevel,
    Identifier,
    QuerySpec,
    SourceRecord,
)
from celllore.infrastructure.persistence.evidence_ledger import EvidenceLedger


class FakeAdapter:
    name = "ncbi_geo"

    async def search(self, query: QuerySpec):
        return [
            SourceRecord(
                record_id=stable_id("src", "fake:GSE12345"),
                source_name=self.name,
                external_id="GSE12345",
                title="Human lung scATAC-seq atlas",
                identifiers=[
                    Identifier(namespace="geo_series", value="GSE12345")
                ],
                organisms=["Homo sapiens"],
                access_level=AccessLevel.PUBLIC_DECLARED,
            )
        ]


@pytest.mark.asyncio
async def test_end_to_end_workflow_with_offline_adapter(tmp_path):
    ledger = EvidenceLedger(tmp_path / "ledger.sqlite")
    workflow = DiscoveryWorkflow(adapters=[FakeAdapter()], ledger=ledger)
    result = await workflow.run("human lung scATAC-seq", max_results=2)

    assert len(result.source_records) == 1
    assert len(result.candidates) == 1
    assert result.candidates[0].identifiers[0].value == "GSE12345"
    assert ledger.load_candidates(result.run_id)[0].title.endswith("atlas")

