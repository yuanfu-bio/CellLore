"""Prove (or decline to claim) the physical relationship within a pair unit.

The only component allowed to emit a ``has_pair_relationship`` EvidenceClaim.
``PairUnitResolver`` only proposes groupings; this verifies them, in
descending order of evidence strength, following 05_Policy's R06 accept
list. A provisional/unresolved unit (``ProposedPairUnit.is_resolved is
False`` -- every asset kept together because grouping was never actually
attempted) is refused outright, before any tier runs: an assay-level
technology prior only tells you the *assay* supports physical
co-measurement, never that a specific, unverified grouping of files is one
correct physical unit.

1. **Named technology** -- an ``AssayProfile`` whose pairing was asserted by
   a named co-assay technology (10x Multiome, SNARE-seq, ...) states the
   relationship by definition. ``REPOSITORY_REPORTED``.
2. **Explicit same-cell/same-nucleus statement** in the pair unit's own
   sample text (not the whole study's -- a statement about a *different*
   sample in the same series should not leak into this unit's claim).
   ``REPOSITORY_REPORTED``.
3. **LLM evidence inference**, only once 1 and 2 have both come up empty --
   not a first-resort. ``MODEL_INFERRED``, and this evidence class alone must
   never be read as satisfying a hard physical-pairing requirement (see
   ``CurationProfile.required_evidence`` -- ``paired_atac_rna_human`` sets
   ``has_pair_relationship``'s acquisition floor to ``REPOSITORY_REPORTED``).
4. Otherwise: no claim. Absence is not disproof, and a low-confidence
   fabricated claim is worse than admitting the relationship is unresolved.

Post-download barcode intersection (the strongest tier, ``FILE_OBSERVED``) is
Phase 5's new validator -- not available yet, so it is not attempted here.
GSM accession-number adjacency is never evidence at any tier, matching R06's
own "not accepted" column.
"""

from __future__ import annotations

import json
from typing import Protocol

from scdata_agent.domain.identifiers import stable_id
from scdata_agent.domain.lifecycle import EvidenceClaim
from scdata_agent.domain.models import (
    DatasetCandidate,
    EvidenceClass,
    GeoSampleRecord,
    PairingLevel,
    ProfileBasis,
)
from scdata_agent.domain.pair_units import ProposedPairUnit
from scdata_agent.infrastructure.llm.requirement_compiler import _json_object
from scdata_agent.infrastructure.sources.common import _infer_pairing_level, prose_text

_PHYSICAL_LEVELS = {PairingLevel.SAME_CELL, PairingLevel.SAME_NUCLEUS}


class ChatCompletionClient(Protocol):
    async def complete(
        self, messages: list[dict[str, object]], *, temperature: float = 0.1, max_tokens: int = 2_000
    ) -> str: ...


_SYSTEM_PROMPT = (
    "You are verifying whether a group of GEO samples were measured in the "
    "same physical cell or nucleus (not merely the same donor, sample, or "
    "study). Read the sample metadata text given and decide whether it states "
    "or clearly implies same-cell/same-nucleus co-measurement, a shared "
    "barcode system, or an official barcode translation -- versus only a "
    "weaker administrative match (same donor/sample) or no stated "
    "relationship at all. GSM accession numbers carry no meaning; ignore "
    "them. Return only JSON: "
    '{"pairing_level": one of "same_cell"|"same_nucleus"|"same_sample"|'
    '"same_donor"|"unverified", "confidence": 0..1, "rationale": str}.'
)


class PairingVerifier:
    def __init__(self, client: ChatCompletionClient | None = None) -> None:
        self.client = client

    async def verify(
        self,
        pair_unit: ProposedPairUnit,
        candidate: DatasetCandidate,
        samples: list[GeoSampleRecord],
    ) -> EvidenceClaim | None:
        # A provisional unit's membership was never actually resolved -- an
        # assay-level technology prior (e.g. "this is a DOGMA-seq study")
        # says the *assay* supports physical co-measurement, not that this
        # particular unresolved blob (possibly several patients' files kept
        # together only because grouping was never attempted) is one correct
        # physical unit. No tier below may be applied to it.
        if not pair_unit.is_resolved:
            return None

        named = _named_technology_relationship(candidate)
        if named is not None:
            level, evidence_ids = named
            return _claim(
                pair_unit, level, EvidenceClass.REPOSITORY_REPORTED, evidence_ids,
                "A named co-assay technology asserts this relationship by definition.",
            )

        member_samples = [
            item for item in samples if item.gsm_accession in pair_unit.member_samples
        ]
        explicit = _explicit_statement_relationship(member_samples)
        if explicit is not None:
            level, evidence_ids = explicit
            return _claim(
                pair_unit, level, EvidenceClass.REPOSITORY_REPORTED, evidence_ids,
                "An explicit same-cell/same-nucleus statement was found in this "
                "unit's own sample metadata.",
            )

        if self.client is not None:
            try:
                inferred = await self._llm_infer(pair_unit, candidate, member_samples)
            except Exception:
                inferred = None
            if inferred is not None:
                return inferred

        return None

    async def _llm_infer(
        self,
        pair_unit: ProposedPairUnit,
        candidate: DatasetCandidate,
        member_samples: list[GeoSampleRecord],
    ) -> EvidenceClaim | None:
        assert self.client is not None
        payload = {
            "study_title": candidate.title,
            "grouping_rationale": pair_unit.grouping_rationale,
            "samples": [
                {
                    "gsm_accession": item.gsm_accession,
                    "title": item.title,
                    "characteristics": item.characteristics,
                }
                for item in member_samples
            ],
        }
        response = await self.client.complete(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            temperature=0.0,
            max_tokens=600,
        )
        decoded = _json_object(response)
        level = PairingLevel(decoded.get("pairing_level", PairingLevel.UNVERIFIED.value))
        confidence = float(decoded.get("confidence", 0.5))
        rationale = str(decoded.get("rationale", "LLM evidence inference."))
        evidence_ids = list(dict.fromkeys(pair_unit.evidence_ids))
        return EvidenceClaim(
            claim_id=stable_id(
                "claim", f"{pair_unit.proposed_unit_id}:has_pair_relationship:model"
            ),
            subject_id=pair_unit.proposed_unit_id,
            predicate="has_pair_relationship",
            value=level.value,
            evidence_ids=evidence_ids,
            evidence_class=EvidenceClass.MODEL_INFERRED,
            confidence=max(0.0, min(1.0, confidence)),
            rationale=rationale,
        )


def _named_technology_relationship(
    candidate: DatasetCandidate,
) -> tuple[PairingLevel, list[str]] | None:
    for profile in candidate.assay_profiles:
        if (
            profile.inferred_from == ProfileBasis.NAMED_TECHNOLOGY
            and profile.pairing_level in _PHYSICAL_LEVELS
        ):
            return profile.pairing_level, list(profile.evidence_ids)
    return None


def _explicit_statement_relationship(
    member_samples: list[GeoSampleRecord],
) -> tuple[PairingLevel, list[str]] | None:
    if not member_samples:
        return None
    content = prose_text(item.raw_text for item in member_samples)
    level = _infer_pairing_level(content)
    if level not in _PHYSICAL_LEVELS:
        return None
    evidence_ids = list(
        dict.fromkeys(
            evidence_id
            for item in member_samples
            for evidence_id in item.evidence_ids
        )
    )
    return level, evidence_ids


def _claim(
    pair_unit: ProposedPairUnit,
    level: PairingLevel,
    evidence_class: EvidenceClass,
    evidence_ids: list[str],
    rationale: str,
) -> EvidenceClaim:
    return EvidenceClaim(
        claim_id=stable_id(
            "claim", f"{pair_unit.proposed_unit_id}:has_pair_relationship:{evidence_class.value}"
        ),
        subject_id=pair_unit.proposed_unit_id,
        predicate="has_pair_relationship",
        value=level.value,
        evidence_ids=evidence_ids,
        evidence_class=evidence_class,
        confidence=1.0 if evidence_class == EvidenceClass.REPOSITORY_REPORTED else 0.5,
        rationale=rationale,
    )


def configured_pairing_verifier(use_llm: bool = True) -> PairingVerifier:
    """Return the LLM-backed verifier, or the deterministic-tiers-only
    fallback when explicitly requested via ``use_llm=False``.

    Tiers 1/2 (named technology, explicit statement) stay fully deterministic
    either way. But if the caller asked for LLM evidence inference (the
    default, tier 3) and no model is actually configured, that is a
    deployment error and must fail fast rather than silently skipping tier 3
    -- a study whose relationship can only be established by reading
    ambiguous phrasing would otherwise silently end up with no claim and no
    indication that inference was never attempted.
    """
    if not use_llm:
        return PairingVerifier(client=None)
    from scdata_agent.infrastructure.llm.router import ModelRouter, ReasoningProfile

    router = ModelRouter.from_environment()
    return PairingVerifier(client=router.client(ReasoningProfile.EXTRACT))
