"""A registered ``CurationProfile``: declared curation policy, not code.

Precedent: ``domain.manifest.ManifestDialect`` -- "adding a new one adds an
instance, not a branch." ``CurationProfile`` does the same for an entire
research objective (what evidence is required, what relationship kinds are
acceptable, what a curation unit is, which validators must pass, what search
vocabulary to seed with) instead of just a spreadsheet layout.

This module must stay pure declared data. The kernel (``domain.requirements``,
``application.agents.*``) reads fields off a ``CurationProfile`` instance; it
must never branch on ``profile_id``. A behaviour that cannot be expressed as a
field here is a sign this schema needs a new field, not that the kernel needs
an ``if profile_id == ...`` clause.
"""

from __future__ import annotations

from pydantic import Field

from scdata_agent.domain.manifest import ManifestDialect
from scdata_agent.domain.models import (
    BundleSchema,
    DataRole,
    EvidenceClass,
    OmicsLayer,
    PairingRequirement,
    StrictModel,
)


class HardRequirement(StrictModel):
    """One non-negotiable admission rule, stated for a human reviewer.

    Deliberately free text rather than a structured predicate language: these
    are read and audited by people (see 05_Policy's R01-R10), and forcing them
    into a DSL would just relocate the knowledge into a place nobody checks.
    Machine-checkable consequences of a hard requirement belong in
    ``required_evidence``/``validators`` instead.
    """

    requirement_id: str
    statement: str


class CurationUnitGrouping(StrictModel):
    """What counts as one independently-curatable unit for this profile.

    ``grouping_signals`` names what a resolver (e.g. ``PairUnitResolver``) may
    use to *propose* a grouping. ``forbidden_signals`` is the structural,
    testable form of a hard architectural constraint: GSM accession-number
    adjacency carries no experimental-design semantics and must never be used
    for grouping or pairing, in any weighted or fallback capacity -- see
    ``tests/test_curation_profile.py`` for the regression that pins this.
    """

    grouping_label: str
    description: str
    grouping_signals: list[str] = Field(default_factory=list)
    forbidden_signals: list[str] = Field(default_factory=list)


class RequiredEvidence(StrictModel):
    """The evidence classes a predicate accepts, at two different gates.

    Explicit allow-lists, not a rank floor: ``EvidenceClass`` is closer to a
    provenance/type tag than a single totally-ordered strength scale --
    ``ONTOLOGY_NORMALIZED`` and ``FILE_OBSERVED`` being the same rank does not
    mean either is an appropriate substitute for the other on every
    predicate, and a future evidence class ranked "higher" is not guaranteed
    to be semantically right for a predicate nobody designed it for. Naming
    exactly which classes each predicate accepts, per gate, is what rules
    that out -- the same "set membership, not rank" principle already applied
    to ``PairingRequirement`` (domain.models), now applied to evidence class.

    Two gates, not one: declared/reported evidence (``REPOSITORY_REPORTED``/
    ``ARTICLE_REPORTED``) is enough to let a candidate continue into
    acquisition -- it must not be blocked pending post-download evidence that
    does not exist yet. Reaching the final corpus-ready state is a stronger
    claim and may require post-download deterministic verification (e.g. a
    barcode-intersection validator) that only exists once the files are
    actually in hand. Collapsing these into one gate either blocks
    acquisition on evidence that cannot exist yet, or lets a merely-declared
    claim stand in for the strongest one.

    ``USER_CORRECTED`` is a deliberate universal exception, not something
    every predicate needs to list: a human reviewer's explicit correction is
    appropriate evidence for any predicate by definition, so
    ``evaluate_evidence_completeness`` (domain.curation.decision) always
    accepts it regardless of what a given predicate's lists contain.
    """

    predicate: str
    acceptable_for_acquisition: list[EvidenceClass]
    acceptable_for_corpus_ready: list[EvidenceClass]
    notes: str = ""


class ValidatorRef(StrictModel):
    """A reference to a ``Validator`` (see ``infrastructure.qc_metrics``) plus
    whether this profile treats a failure as blocking."""

    validator_id: str
    blocking: bool = True
    notes: str = ""


class SearchHints(StrictModel):
    """Vocabulary and seeds for query generation -- never a query template.

    These are handed to an LLM as context/vocabulary it may draw on when
    authoring a search query; deterministic code may only validate/bound the
    result. Assembling a query by string-formatting these fields back into a
    fixed template just relocates ``MODALITY_QUERY_TERMS`` into a profile
    field and does not solve the closed-vocabulary problem this profile
    system exists to fix.
    """

    layer_aliases: dict[OmicsLayer, tuple[str, ...]] = Field(default_factory=dict)
    technology_seeds: tuple[str, ...] = Field(default_factory=tuple)
    pairing_phrase_seeds: tuple[str, ...] = Field(default_factory=tuple)
    # Terms that name only one layer and would broaden a relational profile
    # into unrelated single-assay records when OR-ed into a repository query.
    forbidden_query_terms: tuple[str, ...] = Field(default_factory=tuple)


class CurationProfile(StrictModel):
    profile_id: str
    label: str
    scientific_objective: str
    hard_requirements: list[HardRequirement] = Field(default_factory=list)
    required_relationship: PairingRequirement | None = None
    curation_unit_grouping: CurationUnitGrouping
    bundle_schemas: tuple[BundleSchema, ...] = Field(default_factory=tuple)
    required_evidence: list[RequiredEvidence] = Field(default_factory=list)
    validators: list[ValidatorRef] = Field(default_factory=list)
    search_hints: SearchHints = Field(default_factory=SearchHints)
    manifest_dialect: ManifestDialect | None = None
    required_data_roles: list[DataRole] = Field(default_factory=list)
    output_mapping: dict[str, str] = Field(default_factory=dict)
