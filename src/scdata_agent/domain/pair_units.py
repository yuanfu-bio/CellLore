"""A proposed grouping of one study's resources into curatable units.

``ProposedPairUnit`` is a grouping *hypothesis*, never proof of a physical
relationship. It is produced by a resolver that reasons over sample metadata,
file names, and study description -- title/characteristics similarity,
shared donor or library identifiers, and an LLM's semantic reading of the
experimental design are all legitimate grouping signals. GSM accession-number
adjacency is explicitly not: an accession is an assigned serial with no
experimental-design semantics, and both this model and any resolver that
produces it must never use it, in any weighted or fallback capacity.

Only a separate verifier that proves the physical relationship (shared GEM/
barcode, official barcode translation, an explicit same-cell/nucleus
statement, or a post-download barcode intersection) may turn a
``ProposedPairUnit`` into a claim -- see ``EvidenceClaim`` with predicate
``has_pair_relationship`` in ``domain.lifecycle``. This model's own fields
never assert that relationship.
"""

from __future__ import annotations

from pydantic import Field

from scdata_agent.domain.models import StrictModel


class ProposedPairUnit(StrictModel):
    proposed_unit_id: str
    candidate_id: str
    member_assets: list[str] = Field(default_factory=list)
    member_samples: list[str] = Field(default_factory=list)
    sample_interpretation: str = ""
    donor_interpretation: str = ""
    library_interpretation: str = ""
    assay_interpretation: str = ""
    grouping_rationale: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    # False (the safe default) means this is a provisional/unresolved unit --
    # every asset for the candidate kept together because no real per-sample
    # grouping was attempted (no LLM, or no sample metadata), not because a
    # resolver concluded they belong together. A verifier must never treat a
    # provisional unit as eligible for a relationship claim: an assay-level
    # technology prior (e.g. "this is a DOGMA-seq study") says the *assay*
    # supports physical co-measurement, it says nothing about whether this
    # particular blob of possibly-multiple-patients' files is one correct
    # unit. Only a resolver that did real grouping work may set this True.
    is_resolved: bool = False
