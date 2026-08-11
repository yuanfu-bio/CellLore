"""The paired human RNA+ATAC same-cell/same-nucleus curation profile.

Sourced from ``04_thinking/agent.xlsx``'s ``05_Policy`` sheet. Every table this
profile references (``BUNDLE_SCHEMAS``, ``TECHNOLOGY_PROFILES``,
``LAYER_ALIASES``, ``PAIRED_MULTIOME_ZH_V1``) is imported, not copied -- this
module composes existing knowledge into a profile, it does not fork a second
copy of it.
"""

from __future__ import annotations

from celllore.domain.assay_knowledge import LAYER_ALIASES, TECHNOLOGY_PROFILES
from celllore.domain.curation.profile import (
    CurationProfile,
    CurationUnitGrouping,
    HardRequirement,
    RequiredEvidence,
    SearchHints,
    ValidatorRef,
)
from celllore.domain.manifest import PAIRED_MULTIOME_ZH_V1
from celllore.domain.models import (
    EvidenceClass,
    OmicsLayer,
    PairingLevel,
    PairingRequirement,
)
from celllore.domain.requirements import (
    BUNDLE_SCHEMAS,
    SAME_CELL_PAIRING_ALIASES,
    roles_for_layers,
)

_BUNDLE_SCHEMA_IDS = frozenset({"multiome_same_cell", "multiome_trimodal"})
_bundle_schemas = tuple(
    schema for schema in BUNDLE_SCHEMAS if schema.schema_id in _BUNDLE_SCHEMA_IDS
)
_required_roles, _ = roles_for_layers(
    [OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME, OmicsLayer.PROTEIN]
)
# Named technologies whose definition ties RNA to ATAC in one physical unit --
# excludes e.g. CITE-seq, which is RNA+protein with no ATAC layer at all.
_rna_atac_technology_seeds = tuple(
    spec["aliases"][0]
    for spec in TECHNOLOGY_PROFILES
    if OmicsLayer.CHROMATIN_ACCESSIBILITY in spec["layers"]
    and OmicsLayer.TRANSCRIPTOME in spec["layers"]
) + (
    # Bare combinatorial phrasing that names the layer combination by
    # implication rather than through one specific named technology.
    "multiome", "rna+atac", "rna atac", "joint rna and atac", "多组学",
    "rna atac protein", "trimodal", "tri-modal",
)

PAIRED_ATAC_RNA_HUMAN = CurationProfile(
    profile_id="paired_atac_rna_human",
    label="Human same-cell/same-nucleus paired RNA+ATAC single-cell corpus",
    scientific_objective=(
        "Curate human single-cell/single-nucleus datasets in which RNA and ATAC "
        "were measured in one physical cell or nucleus, for cross-modality "
        "supervision."
    ),
    hard_requirements=[
        HardRequirement(
            requirement_id="human_only",
            statement="Organism must be human (Homo sapiens).",
        ),
        HardRequirement(
            requirement_id="physical_pairing",
            statement=(
                "RNA and ATAC must come from the same physical cell or nucleus "
                "(same_cell/same_nucleus), with a traceable pairing key -- not "
                "merely the same sample, donor, or study."
            ),
        ),
        HardRequirement(
            requirement_id="atac_barcode_level",
            statement=(
                "ATAC must be a cell-barcode-level fragment file, not only a "
                "peak matrix or a bulk-aggregated summary."
            ),
        ),
        HardRequirement(
            requirement_id="rna_raw_counts",
            statement="The RNA matrix must be unnormalized, non-negative integer counts.",
        ),
        HardRequirement(
            requirement_id="metadata_traceable",
            statement="Metadata must map each barcode to at least a sample/library identifier.",
        ),
        HardRequirement(
            requirement_id="pairing_key_bijective",
            statement="The pairing key must map one-to-one between the RNA and ATAC sides.",
        ),
        HardRequirement(
            requirement_id="manifest_accession_precision",
            statement="A file-level manifest must give a precise accession, not only a study-level one.",
        ),
    ],
    required_relationship=PairingRequirement(
        acceptable_levels=[PairingLevel.SAME_CELL, PairingLevel.SAME_NUCLEUS]
    ),
    curation_unit_grouping=CurationUnitGrouping(
        grouping_label="pair_unit",
        description=(
            "One independently-curatable RNA+ATAC pairing, typically one "
            "sample/patient's RNA and ATAC files within a study."
        ),
        grouping_signals=[
            "sample_title_similarity",
            "filename_stem_sharing",
            "sample_characteristics_field",
            "source_name_field",
            "donor_or_patient_label",
            "library_identifier",
            "replicate_label",
            "llm_semantic_interpretation_of_study_and_sample_metadata",
        ],
        # Structural, testable form of the hard constraint: an accession
        # number is an assigned serial with no experimental-design semantics
        # and must never be used to group or pair, in any capacity.
        forbidden_signals=[
            "gsm_accession_adjacency",
            "gsm_accession_number_distance",
        ],
    ),
    bundle_schemas=_bundle_schemas,
    required_evidence=[
        RequiredEvidence(
            predicate="has_omics_layer:chromatin_accessibility",
            acceptable_for_acquisition=[
                EvidenceClass.REPOSITORY_REPORTED, EvidenceClass.ARTICLE_REPORTED,
            ],
            acceptable_for_corpus_ready=[
                EvidenceClass.REPOSITORY_REPORTED, EvidenceClass.ARTICLE_REPORTED,
            ],
            notes="The ATAC layer must be reported by the repository or the publication.",
        ),
        RequiredEvidence(
            predicate="has_omics_layer:transcriptome",
            acceptable_for_acquisition=[
                EvidenceClass.REPOSITORY_REPORTED, EvidenceClass.ARTICLE_REPORTED,
            ],
            acceptable_for_corpus_ready=[
                EvidenceClass.REPOSITORY_REPORTED, EvidenceClass.ARTICLE_REPORTED,
            ],
            notes="The RNA layer must be reported by the repository or the publication.",
        ),
        RequiredEvidence(
            predicate="has_pair_relationship",
            acceptable_for_acquisition=[
                EvidenceClass.REPOSITORY_REPORTED, EvidenceClass.ARTICLE_REPORTED,
            ],
            # Deliberately excludes ONTOLOGY_NORMALIZED/MODEL_INFERRED even
            # though both could out-rank REPOSITORY_REPORTED on a global
            # scale: neither is semantically a physical-pairing measurement.
            # Only a real post-download barcode-intersection result counts.
            acceptable_for_corpus_ready=[EvidenceClass.FILE_OBSERVED, EvidenceClass.RECOMPUTED],
            notes=(
                "A declared same-cell/nucleus statement or a documented pairing "
                "key (shared GEM/10x barcode, official barcode translation "
                "table, combinatorial-index relationship) is enough to proceed "
                "into acquisition -- MODEL_INFERRED alone never satisfies this "
                "on its own. Reaching corpus-ready requires the post-download "
                "RNA/ATAC barcode-intersection validator (Phase 5)."
            ),
        ),
        RequiredEvidence(
            predicate="resolved_assembly",
            # No pre-download evidence class is acceptable here on purpose,
            # not an oversight: GEO per-sample supplementary listings almost
            # never state a build, and gating acquisition on repository/
            # article text alone would leave nearly every real candidate
            # stuck before download regardless of how complete its files
            # are. A REPOSITORY_REPORTED/ARTICLE_REPORTED claim is still
            # recorded and shown when the text does state one (see
            # infrastructure.sources.common.infer_genome_build); it just
            # cannot be the sole thing acquisition waits on. An empty list
            # here means "ungated at this tier", not "unsatisfiable" --
            # see evaluate_evidence_completeness.
            acceptable_for_acquisition=[],
            acceptable_for_corpus_ready=[
                EvidenceClass.REPOSITORY_REPORTED, EvidenceClass.ARTICLE_REPORTED,
                EvidenceClass.FILE_OBSERVED,
            ],
            notes=(
                "Genome assembly need not be known before acquisition; the "
                "real determination is the downloaded file's own header, "
                "read by the blocking genome_build_consistency validator "
                "before corpus-ready."
            ),
        ),
    ],
    validators=[
        ValidatorRef(validator_id="genome_build_consistency", blocking=True),
        ValidatorRef(
            validator_id="barcode_linkage", blocking=False,
            notes="WARN when no explicit linkage file is present; not yet a hard blocker.",
        ),
        ValidatorRef(validator_id="frip_computable", blocking=False),
        ValidatorRef(
            validator_id="rna_integer_count", blocking=True,
            notes="Not yet implemented -- Phase 5.",
        ),
        ValidatorRef(
            validator_id="rna_atac_barcode_intersection", blocking=True,
            notes=(
                "Not yet implemented -- Phase 5. Produces the FILE_OBSERVED "
                "evidence has_pair_relationship needs for corpus-ready."
            ),
        ),
    ],
    search_hints=SearchHints(
        layer_aliases=LAYER_ALIASES,
        technology_seeds=_rna_atac_technology_seeds,
        pairing_phrase_seeds=SAME_CELL_PAIRING_ALIASES,
        forbidden_query_terms=(
            "atac", "atac-seq", "scatac-seq", "snatac-seq",
            "rna", "rna-seq", "scrna-seq", "snrna-seq",
            "gene expression", "transcriptome", "chromatin accessibility",
        ),
    ),
    manifest_dialect=PAIRED_MULTIOME_ZH_V1,
    required_data_roles=_required_roles,
)
