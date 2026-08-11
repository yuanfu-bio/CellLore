"""LLM-driven semantic grouping of a study's samples into candidate pair units.

Answers one question: within a study, which GSM samples, files, and metadata
belong to the same independently-curatable experimental unit? This is a
grouping *hypothesis*, not proof of a physical relationship -- see
``domain.pair_units.ProposedPairUnit``'s docstring. GSM accession-number
adjacency is never used as a signal, here or anywhere downstream of this
module: it is not passed to the model as an ordering hint, and grouping is
keyed on sample content (title, characteristics, source name, organism,
supplementary file names), never on accession order.

When no LLM client is configured, or the model returns nothing usable, this
falls back to one pair unit containing every asset -- the same shape today's
one-bundle-per-candidate behavior already produces, so a profile or study
this resolver cannot meaningfully split degrades to the previous behavior
rather than losing data.
"""

from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Protocol
from urllib.parse import unquote, urlparse

from pydantic import Field

from scdata_agent.domain.identifiers import stable_id
from scdata_agent.domain.models import DataAsset, DatasetCandidate, GeoSampleRecord, StrictModel
from scdata_agent.domain.pair_units import ProposedPairUnit
from scdata_agent.infrastructure.llm.requirement_compiler import _json_object


class ChatCompletionClient(Protocol):
    async def complete(
        self, messages: list[dict[str, object]], *, temperature: float = 0.1, max_tokens: int = 2_000
    ) -> str: ...


class ProposedPairUnitDraft(StrictModel):
    proposed_unit_id: str
    member_samples: list[str] = Field(default_factory=list)
    sample_interpretation: str = ""
    donor_interpretation: str = ""
    library_interpretation: str = ""
    assay_interpretation: str = ""
    grouping_rationale: str = ""
    uncertainties: list[str] = Field(default_factory=list)


class PairUnitResolutionDraft(StrictModel):
    units: list[ProposedPairUnitDraft] = Field(default_factory=list)


_SYSTEM_PROMPT = (
    "You are curating a public single-cell multi-omics repository. Given one "
    "study's GEO sample metadata and file names, group the samples into the "
    "experimental units a curator would treat independently -- typically one "
    "per patient, donor, or biological replicate. Read sample titles, "
    "characteristics, source_name, organism, and supplementary file names for "
    "shared identifiers (a patient/donor label, a library index, a replicate "
    "number) that indicate two samples were produced from the same physical "
    "material. Every GSM accession you are given is an assigned serial number "
    "with no experimental-design meaning -- you must never group samples "
    "because their accessions are numerically close or sequential; ignore "
    "accession order entirely and reason only from the sample content. "
    "First identify each sample/file modality from explicit RNA/GEX, ATAC/"
    "chromatin/fragments, and ADT/protein terms. ADT/protein is not ATAC: an "
    "RNA+ADT CITE-seq pair must never be described as an RNA+ATAC pair. A "
    "paired RNA+ATAC unit should include evidence for both complementary "
    "modalities from the same named patient/donor/replicate. "
    "For every proposed unit, write a grouping_rationale that cites the "
    "specific text (titles, characteristics, file name fragments) that "
    "justifies the grouping, and list any uncertainties honestly. You are "
    "proposing a hypothesis for later verification, not proving a physical "
    "relationship -- do not claim same-cell or same-nucleus pairing here. "
    "Return only JSON matching the supplied schema."
)


class PairUnitResolver:
    def __init__(self, client: ChatCompletionClient | None = None) -> None:
        self.client = client

    async def resolve(
        self,
        candidate: DatasetCandidate,
        samples: list[GeoSampleRecord],
        assets: list[DataAsset],
    ) -> list[ProposedPairUnit]:
        if not samples or self.client is None:
            return [_fallback_unit(candidate, assets)]
        try:
            draft = await self._llm_group(candidate, samples)
        except Exception:
            return [_fallback_unit(candidate, assets)]
        if not draft.units:
            return [_fallback_unit(candidate, assets)]
        samples_by_accession = {item.gsm_accession: item for item in samples}
        return [
            _to_proposed_pair_unit(candidate, item, samples_by_accession, assets)
            for item in draft.units
        ]

    async def _llm_group(
        self, candidate: DatasetCandidate, samples: list[GeoSampleRecord]
    ) -> PairUnitResolutionDraft:
        assert self.client is not None
        user_payload = {
            "study_title": candidate.title,
            "study_description": candidate.description,
            "samples": [
                {
                    "gsm_accession": item.gsm_accession,
                    "title": item.title,
                    "source_name": item.source_name,
                    "organism": item.organism,
                    "characteristics": item.characteristics,
                    "supplementary_file_names": [
                        _file_basename(url) for url in item.supplementary_files
                    ],
                }
                for item in samples
            ],
            "json_schema": PairUnitResolutionDraft.model_json_schema(),
        }
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ]
        # A fixed 2_000 truncated a real 12-sample/6-unit study mid-JSON
        # (verified against GSE306275): each proposed unit's own rationale
        # scales with sample count. The StepFun client applies the global
        # 32k safety ceiling for exceptionally large studies.
        output_budget = max(2_000, 350 * len(samples))
        structured_complete = getattr(self.client, "complete_structured", None)
        if callable(structured_complete):
            response = await structured_complete(
                messages,
                schema=PairUnitResolutionDraft.model_json_schema(),
                schema_name="PairUnitResolution",
                temperature=0.0,
                max_tokens=output_budget,
            )
        else:
            response = await self.client.complete(
                messages,
                temperature=0.0,
                max_tokens=output_budget,
            )
        return PairUnitResolutionDraft.model_validate(_json_object(response))


def _file_basename(locator: str) -> str:
    return PurePosixPath(unquote(urlparse(locator).path)).name or locator


def _to_proposed_pair_unit(
    candidate: DatasetCandidate,
    draft: ProposedPairUnitDraft,
    samples_by_accession: dict[str, GeoSampleRecord],
    assets: list[DataAsset],
) -> ProposedPairUnit:
    member_samples = [
        accession for accession in draft.member_samples if accession in samples_by_accession
    ]
    member_assets = _assets_for_samples(member_samples, samples_by_accession, assets)
    evidence_ids = list(
        dict.fromkeys(
            evidence_id
            for accession in member_samples
            for evidence_id in samples_by_accession[accession].evidence_ids
        )
    )
    return ProposedPairUnit(
        proposed_unit_id=stable_id(
            "pair_unit", f"{candidate.candidate_id}:{draft.proposed_unit_id}"
        ),
        candidate_id=candidate.candidate_id,
        member_assets=member_assets,
        member_samples=member_samples,
        sample_interpretation=draft.sample_interpretation,
        donor_interpretation=draft.donor_interpretation,
        library_interpretation=draft.library_interpretation,
        assay_interpretation=draft.assay_interpretation,
        grouping_rationale=draft.grouping_rationale,
        evidence_ids=evidence_ids,
        uncertainties=draft.uncertainties,
        is_resolved=True,
    )


def _assets_for_samples(
    member_samples: list[str],
    samples_by_accession: dict[str, GeoSampleRecord],
    assets: list[DataAsset],
) -> list[str]:
    wanted_names = {
        _file_basename(url)
        for accession in member_samples
        for url in samples_by_accession[accession].supplementary_files
    }
    return [item.asset_id for item in assets if item.file_name in wanted_names]


def _fallback_unit(
    candidate: DatasetCandidate, assets: list[DataAsset]
) -> ProposedPairUnit:
    return ProposedPairUnit(
        proposed_unit_id=stable_id("pair_unit", f"{candidate.candidate_id}:fallback_whole_study"),
        candidate_id=candidate.candidate_id,
        member_assets=[item.asset_id for item in assets],
        grouping_rationale=(
            "No sample-level metadata was available or no LLM grouping was "
            "configured; every asset for this candidate was kept in one unit "
            "rather than guessed apart."
        ),
        uncertainties=["No per-sample grouping was attempted."],
    )


def configured_pair_unit_resolver(use_llm: bool = True) -> PairUnitResolver:
    """Return the LLM-backed resolver, or the deterministic single-unit
    fallback when explicitly requested via ``use_llm=False``.

    Semantic grouping is this component's entire purpose, not an optional
    enhancement -- if the caller asked for LLM grouping (the default) and no
    model is actually configured, that is a deployment error and must fail
    fast (``ModelRouter.from_environment()``'s ``RuntimeError`` propagates)
    rather than silently returning one undifferentiated unit per candidate,
    which looks like a resolved result but never actually reasoned about the
    study at all.
    """
    if not use_llm:
        return PairUnitResolver(client=None)
    from scdata_agent.infrastructure.llm.router import ModelRouter, ReasoningProfile

    router = ModelRouter.from_environment()
    return PairUnitResolver(client=router.client(ReasoningProfile.EXTRACT))
