import pytest

from scdata_agent.application.agents import geo_sample_metadata as module
from scdata_agent.application.agents.geo_sample_metadata import (
    GeoSampleMetadataAgent,
    _refine_candidates_from_geo_samples,
)
from scdata_agent.application.agents.shared import AgentContext
from scdata_agent.application.collaboration import ArtifactKind, CollaborationLedger
from scdata_agent.domain.models import (
    DatasetCandidate,
    DiscoveryRunResult,
    GeoSampleRecord,
    Identifier,
    OmicsLayer,
    QueryPlan,
    ResearchRequirement,
)


def test_geo_samples_filter_pbmc_scope_and_required_rna_atac_layers():
    requirement = ResearchRequirement(
        question="human PBMC paired RNA and ATAC",
        cell_types=["human PBMC"],
        required_omics_layers=[
            OmicsLayer.CHROMATIN_ACCESSIBILITY,
            OmicsLayer.TRANSCRIPTOME,
        ],
    )
    valid = DatasetCandidate(candidate_id="valid", title="Multiomic immune profiling")
    unrelated = DatasetCandidate(candidate_id="unrelated", title="Tumor multiome")
    cite = DatasetCandidate(candidate_id="cite", title="PBMC CITE-seq")
    samples = {
        "valid": [
            GeoSampleRecord(
                gsm_accession="GSM1", series_accession="GSE1",
                title="PBMC donor 1 RNA",
                supplementary_files=["GSM1_RNA_counts.csv.gz"],
            ),
            GeoSampleRecord(
                gsm_accession="GSM2", series_accession="GSE1",
                title="PBMC donor 1 ATAC",
                supplementary_files=["GSM2_ATAC_fragments.tsv.gz"],
            ),
        ],
        "unrelated": [
            GeoSampleRecord(
                gsm_accession="GSM3", series_accession="GSE2",
                title="Tumor RNA and ATAC",
                supplementary_files=[
                    "GSM3_RNA_counts.csv.gz", "GSM3_ATAC_fragments.tsv.gz"
                ],
            )
        ],
        "cite": [
            GeoSampleRecord(
                gsm_accession="GSM4", series_accession="GSE3",
                title="PBMC RNA and ADT",
                supplementary_files=[
                    "GSM4_RNA_counts.csv.gz", "GSM4_ADT_counts.csv.gz"
                ],
            )
        ],
    }

    kept, report = _refine_candidates_from_geo_samples(
        [valid, unrelated, cite], requirement, samples
    )

    assert [item.candidate_id for item in kept] == ["valid"]
    excluded = {
        item["candidate_id"]: item["reasons"]
        for item in report["excluded_candidates"]
    }
    assert "pbmc_scope_not_observed" in excluded["unrelated"]
    assert any("chromatin_accessibility" in reason for reason in excluded["cite"])


def _context(tmp_path, candidates):
    requirement = ResearchRequirement(question="paired multiome")
    discovery = DiscoveryRunResult(
        run_id="run_1", requirement=requirement,
        query_plan=QueryPlan(requirement=requirement, queries=[]),
        source_records=[], candidates=candidates, feasibility_reports=[],
    )
    database = tmp_path / "geo_samples.sqlite"
    return AgentContext(
        run_id="run_1", question="paired multiome", database_path=str(database),
        ledger=CollaborationLedger(database), requirement=requirement, discovery=discovery,
    )


@pytest.mark.asyncio
async def test_skips_candidates_without_a_geo_series_identifier(tmp_path):
    candidate = DatasetCandidate(candidate_id="cand_1", title="study")
    context = _context(tmp_path, [candidate])

    outputs = await GeoSampleMetadataAgent().run(context)

    assert outputs[0].payload["samples_by_candidate"] == {}


@pytest.mark.asyncio
async def test_fetches_and_attaches_evidence_for_a_geo_candidate(tmp_path, monkeypatch):
    candidate = DatasetCandidate(
        candidate_id="cand_1", title="study",
        identifiers=[Identifier(namespace="geo_series", value="GSE178454")],
    )
    context = _context(tmp_path, [candidate])

    async def fake_fetch(accession, *, client=None):
        assert accession == "GSE178454"
        return [
            GeoSampleRecord(
                gsm_accession="GSM1", series_accession="GSE178454",
                title="Sample 1-A: Pit_HF1_snATAC", source_name="Pituitary",
                organism="Homo sapiens", characteristics=["age: 1"],
                supplementary_files=["GSM1_atac_fragments.tsv.gz"],
                raw_text="^SAMPLE = GSM1\n!Sample_title = Sample 1-A: Pit_HF1_snATAC",
            ),
            GeoSampleRecord(
                gsm_accession="GSM2", series_accession="GSE178454",
                title="Sample 1-R: Pit_HF1_snRNA", source_name="Pituitary",
                organism="Homo sapiens", characteristics=["age: 1"],
                supplementary_files=["GSM2_filtered_feature_bc_matrix.h5"],
                raw_text="^SAMPLE = GSM2\n!Sample_title = Sample 1-R: Pit_HF1_snRNA",
            ),
        ]

    monkeypatch.setattr(module, "fetch_series_samples", fake_fetch)

    outputs = await GeoSampleMetadataAgent().run(context)
    artifact_out = outputs[0]

    assert artifact_out.kind == ArtifactKind.GEO_SAMPLE_METADATA
    samples = artifact_out.payload["samples_by_candidate"]["cand_1"]
    assert [item["gsm_accession"] for item in samples] == ["GSM1", "GSM2"]
    # Each sample got its own Evidence, distinct per sample (not one blob for
    # the whole series) -- that's what lets a downstream claim be traced back
    # to the exact GSM it was read from.
    assert all(item["evidence_ids"] for item in samples)
    assert samples[0]["evidence_ids"] != samples[1]["evidence_ids"]
    assert set(artifact_out.evidence_ids) == {
        item["evidence_ids"][0] for item in samples
    }


@pytest.mark.asyncio
async def test_a_fetch_failure_is_recorded_without_aborting_other_candidates(
    tmp_path, monkeypatch
):
    candidate = DatasetCandidate(
        candidate_id="cand_1", title="study",
        identifiers=[Identifier(namespace="geo_series", value="GSE_BROKEN")],
    )
    context = _context(tmp_path, [candidate])

    async def failing_fetch(accession, *, client=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(module, "fetch_series_samples", failing_fetch)

    outputs = await GeoSampleMetadataAgent().run(context)

    assert outputs[0].payload["samples_by_candidate"] == {}
    assert any("boom" in error for error in outputs[0].payload["errors"])
