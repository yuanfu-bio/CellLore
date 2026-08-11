from celllore.domain.feasibility import FeasibilityAssessor
from celllore.domain.models import (
    AssayProfile,
    DatasetCandidate,
    OmicsLayer,
    PairingLevel,
    PairingRequirement,
    ProfileBasis,
    ResearchRequirement,
)
from celllore.domain.requirements import CorpusQueryPlanner, RuleBasedRequirementCompiler
from celllore.infrastructure.sources.common import infer_assay_profiles


def _profile(text):
    profiles = infer_assay_profiles([text])
    assert profiles
    return profiles[0]


def test_named_rna_atac_coassays_have_explicit_pairing_levels():
    tenx = _profile("10x Multiome ATAC + Gene Expression from PBMC nuclei")
    share = _profile("SHARE-seq profiling of chromatin accessibility and RNA")

    assert set(tenx.omics_layers) == {
        OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME,
    }
    assert tenx.pairing_level == PairingLevel.SAME_NUCLEUS
    assert share.pairing_level == PairingLevel.SAME_CELL
    assert set(share.omics_layers) == {
        OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME
    }


def test_atac_protein_and_trimodal_technologies_are_distinct():
    asap = _profile("ASAP-seq measures chromatin accessibility and surface proteins")
    tea = _profile("TEA-seq joint RNA, epitopes, and chromatin accessibility")

    assert set(asap.omics_layers) == {OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.PROTEIN}
    assert set(tea.omics_layers) == {
        OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME, OmicsLayer.PROTEIN,
    }


def test_matched_samples_are_not_mislabeled_as_same_cell():
    matched = _profile("paired scRNA-seq and scATAC-seq from matched samples")
    co_reported = _profile("scRNA-seq and scATAC-seq profiles from the study")

    assert matched.label == "matched_scrna_scatac"
    assert matched.pairing_level == PairingLevel.SAME_SAMPLE
    assert co_reported.pairing_level == PairingLevel.STUDY_LEVEL
    assert co_reported.label != "matched_scrna_scatac"


def test_standalone_scatac_profile_is_not_paired():
    profile = _profile("single-cell ATAC-seq atlas of human tissues")
    assert profile.omics_layers == [OmicsLayer.CHROMATIN_ACCESSIBILITY]
    assert profile.pairing_level == PairingLevel.NOT_APPLICABLE


def test_named_technology_match_sets_named_technology_basis():
    """PairingVerifier's strongest tier gates on exactly this field: a named
    co-assay technology (e.g. DOGMA-seq) asserts its pairing relationship by
    definition, which is a materially stronger claim than assembling the
    same reading from separate layer mentions. It must not silently default
    to LAYER_INFERENCE."""
    profile = _profile("DOGMA-seq joint RNA, ATAC, and protein profiling")
    assert profile.inferred_from == ProfileBasis.NAMED_TECHNOLOGY


def test_named_cite_seq_is_classified_without_a_chromatin_accessibility_layer():
    profile = _profile("CITE-seq profiling of PBMC surface protein and gene expression")
    assert set(profile.omics_layers) == {OmicsLayer.TRANSCRIPTOME, OmicsLayer.PROTEIN}
    assert OmicsLayer.CHROMATIN_ACCESSIBILITY not in profile.omics_layers


def test_unnamed_rna_and_protein_coassay_is_classified_generically():
    profile = _profile("single-cell RNA and antibody-derived tag profiling of tumor-infiltrating cells")
    assert set(profile.omics_layers) == {OmicsLayer.TRANSCRIPTOME, OmicsLayer.PROTEIN}
    assert OmicsLayer.CHROMATIN_ACCESSIBILITY not in profile.omics_layers


def test_rna_only_text_is_classified_instead_of_returning_nothing():
    profile = _profile("single-cell RNA-seq atlas of human bone marrow")
    assert profile.omics_layers == [OmicsLayer.TRANSCRIPTOME]


def test_corpus_query_planner_covers_four_families_across_three_sources():
    requirement = RuleBasedRequirementCompiler().compile("public human scATAC corpus")
    plan = CorpusQueryPlanner().plan(requirement, max_results=250)

    assert len(plan.queries) == 12
    assert {query.source for query in plan.queries} == {
        "europe_pmc", "ncbi_geo", "ncbi_sra"
    }
    assert len({query.purpose for query in plan.queries}) == 4
    assert all(query.max_results == 250 for query in plan.queries)
    assert any("DOGMA-seq" in query.query for query in plan.queries)


def test_pairing_gate_requires_verified_relationship_for_multiome_only_request():
    requirement = ResearchRequirement(
        question="RNA ATAC multiome",
        required_omics_layers=[OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME],
        pairing_requirement=PairingRequirement(
            acceptable_levels=[PairingLevel.SAME_CELL, PairingLevel.SAME_NUCLEUS]
        ),
    )
    verified = DatasetCandidate(
        candidate_id="verified",
        title="10x multiome",
        assay_profiles=[AssayProfile(
            label="10x",
            omics_layers=[OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME],
            pairing_level=PairingLevel.SAME_NUCLEUS,
            rationale="named technology",
        )],
        access_level="public_declared",
    )
    unverified = verified.model_copy(update={
        "candidate_id": "unverified",
        "assay_profiles": [verified.assay_profiles[0].model_copy(update={
            "pairing_level": PairingLevel.UNVERIFIED
        })],
    })

    assert FeasibilityAssessor().assess(requirement, verified).dimensions["pairing_evidence"].status == "pass"
    assert FeasibilityAssessor().assess(requirement, unverified).dimensions["pairing_evidence"].status == "unknown"
