"""PairUnitResolver: LLM-driven grouping, never GSM accession adjacency.

A resolver only ever produces a grouping hypothesis (ProposedPairUnit) --
proving the physical relationship is PairingVerifier's job, not this one's.
"""

import json

import pytest

from celllore.application.agents.pair_unit_resolver import (
    _SYSTEM_PROMPT,
    PairUnitResolutionDraft,
    PairUnitResolver,
)
from celllore.domain.models import DataAsset, DataRole, DatasetCandidate, GeoSampleRecord


class _FakeClient:
    def __init__(self, response: str | None = None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.calls: list[dict] = []

    async def complete(self, messages, *, temperature=0.1, max_tokens=2_000):
        self.calls.append({"messages": messages, "temperature": temperature})
        if self.error is not None:
            raise self.error
        return self.response


class _StructuredFakeClient(_FakeClient):
    def __init__(self, response: str):
        super().__init__(response=response)
        self.structured_kwargs = None

    async def complete_structured(self, messages, **kwargs):
        self.calls.append({"messages": messages, "temperature": kwargs["temperature"]})
        self.structured_kwargs = kwargs
        return self.response


def _candidate():
    return DatasetCandidate(candidate_id="cand_1", title="A paired multiome study")


def _samples():
    return [
        GeoSampleRecord(
            gsm_accession="GSM1000", series_accession="GSE1",
            title="Sample 1-A: Pit_HF1_snATAC", source_name="Pituitary",
            organism="Homo sapiens", characteristics=["age: 1"],
            supplementary_files=["ftp://x/GSM1000_HF1_atac_fragments.tsv.gz"],
            raw_text="...", evidence_ids=["ev_atac"],
        ),
        GeoSampleRecord(
            gsm_accession="GSM1050", series_accession="GSE1",
            title="Sample 1-R: Pit_HF1_snRNA", source_name="Pituitary",
            organism="Homo sapiens", characteristics=["age: 1"],
            supplementary_files=["ftp://x/GSM1050_HF1_filtered_feature_bc_matrix.h5"],
            raw_text="...", evidence_ids=["ev_rna"],
        ),
        GeoSampleRecord(
            gsm_accession="GSM1005", series_accession="GSE1",
            title="Sample 2-A: Pit_HF2_snATAC", source_name="Pituitary",
            organism="Homo sapiens", characteristics=["age: 2"],
            supplementary_files=["ftp://x/GSM1005_HF2_atac_fragments.tsv.gz"],
            raw_text="...", evidence_ids=["ev_atac2"],
        ),
    ]


def _assets():
    return [
        DataAsset(asset_id="a1", candidate_id="cand_1", role=DataRole.FRAGMENTS, file_name="GSM1000_HF1_atac_fragments.tsv.gz"),
        DataAsset(asset_id="a2", candidate_id="cand_1", role=DataRole.RNA_MATRIX, file_name="GSM1050_HF1_filtered_feature_bc_matrix.h5"),
        DataAsset(asset_id="a3", candidate_id="cand_1", role=DataRole.FRAGMENTS, file_name="GSM1005_HF2_atac_fragments.tsv.gz"),
    ]


@pytest.mark.asyncio
async def test_no_client_falls_back_to_one_unit_with_all_assets():
    resolver = PairUnitResolver(client=None)
    units = await resolver.resolve(_candidate(), _samples(), _assets())

    assert len(units) == 1
    assert set(units[0].member_assets) == {"a1", "a2", "a3"}


@pytest.mark.asyncio
async def test_no_samples_falls_back_even_with_a_client_configured():
    resolver = PairUnitResolver(client=_FakeClient(response="{}"))
    units = await resolver.resolve(_candidate(), [], _assets())

    assert len(units) == 1
    assert set(units[0].member_assets) == {"a1", "a2", "a3"}


@pytest.mark.asyncio
async def test_llm_grouping_maps_member_samples_to_the_right_assets():
    draft = PairUnitResolutionDraft(
        units=[
            {
                "proposed_unit_id": "hf1",
                "member_samples": ["GSM1000", "GSM1050"],
                "sample_interpretation": "Patient HF1, ATAC + RNA",
                "grouping_rationale": "Both titles name HF1 and share the same age characteristic.",
            },
            {
                "proposed_unit_id": "hf2",
                "member_samples": ["GSM1005"],
                "sample_interpretation": "Patient HF2, ATAC only",
                "grouping_rationale": "Title names HF2; no matching RNA sample in this study.",
            },
        ]
    )
    client = _FakeClient(response=draft.model_dump_json())
    resolver = PairUnitResolver(client=client)

    units = await resolver.resolve(_candidate(), _samples(), _assets())

    assert len(units) == 2
    hf1 = next(item for item in units if set(item.member_samples) == {"GSM1000", "GSM1050"})
    assert set(hf1.member_assets) == {"a1", "a2"}
    assert set(hf1.evidence_ids) == {"ev_atac", "ev_rna"}
    hf2 = next(item for item in units if item.member_samples == ["GSM1005"])
    assert hf2.member_assets == ["a3"]


@pytest.mark.asyncio
async def test_llm_grouping_prefers_structured_output():
    draft = PairUnitResolutionDraft(
        units=[{
            "proposed_unit_id": "hf1",
            "member_samples": ["GSM1000", "GSM1050"],
            "grouping_rationale": "Both titles name HF1.",
        }]
    )
    client = _StructuredFakeClient(draft.model_dump_json())
    resolver = PairUnitResolver(client=client)

    await resolver.resolve(_candidate(), _samples(), _assets())

    assert client.structured_kwargs["schema_name"] == "PairUnitResolution"
    assert client.structured_kwargs["max_tokens"] == 2_000


@pytest.mark.asyncio
async def test_llm_error_falls_back_to_one_unit():
    resolver = PairUnitResolver(client=_FakeClient(error=RuntimeError("boom")))
    units = await resolver.resolve(_candidate(), _samples(), _assets())

    assert len(units) == 1
    assert set(units[0].member_assets) == {"a1", "a2", "a3"}


@pytest.mark.asyncio
async def test_llm_returning_no_units_falls_back():
    resolver = PairUnitResolver(client=_FakeClient(response=json.dumps({"units": []})))
    units = await resolver.resolve(_candidate(), _samples(), _assets())

    assert len(units) == 1


@pytest.mark.asyncio
async def test_unknown_accession_in_llm_response_is_dropped_not_fabricated():
    """The model can hallucinate an accession that was never in the input;
    it must not silently become a phantom member."""
    draft_json = json.dumps({"units": [{
        "proposed_unit_id": "hf1",
        "member_samples": ["GSM1000", "GSM9999_NOT_REAL"],
        "grouping_rationale": "x",
    }]})
    resolver = PairUnitResolver(client=_FakeClient(response=draft_json))

    units = await resolver.resolve(_candidate(), _samples(), _assets())

    assert units[0].member_samples == ["GSM1000"]


def test_system_prompt_explicitly_forbids_accession_adjacency():
    """Regression guard: this must never quietly regress into an
    accession-proximity heuristic."""
    lowered = _SYSTEM_PROMPT.casefold()
    assert "accession" in lowered
    assert "never group" in lowered or "must never" in lowered
