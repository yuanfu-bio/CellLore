from scdata_agent.domain.feasibility import FeasibilityAssessor
from scdata_agent.domain.models import (
    AssayProfile,
    DatasetCandidate,
    Modality,
    OmicsLayer,
    PairingLevel,
    ResearchRequirement,
)
from scdata_agent.domain.requirements import CorpusQueryPlanner, RuleBasedRequirementCompiler
from scdata_agent.infrastructure.sources.common import infer_assay_profiles


def _profile(text):
    profiles = infer_assay_profiles([text])
    assert profiles
    return profiles[0]


def test_named_rna_atac_coassays_have_explicit_pairing_levels():
    tenx = _profile("10x Multiome ATAC + Gene Expression from PBMC nuclei")
    share = _profile("SHARE-seq profiling of chromatin accessibility and RNA")

    assert tenx.modalities == [Modality.MULTIOME_RNA_ATAC]
    assert tenx.pairing_level == PairingLevel.SAME_NUCLEUS
    assert share.pairing_level == PairingLevel.SAME_CELL
    assert set(share.omics_layers) == {
        OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME
    }


def test_atac_protein_and_trimodal_technologies_are_distinct():
    asap = _profile("ASAP-seq measures chromatin accessibility and surface proteins")
    tea = _profile("TEA-seq joint RNA, epitopes, and chromatin accessibility")

    assert asap.modalities == [Modality.MULTIOME_ATAC_PROTEIN]
    assert tea.modalities == [Modality.MULTIOME_RNA_ATAC_PROTEIN]
    assert OmicsLayer.PROTEIN in tea.omics_layers


def test_matched_samples_are_not_mislabeled_as_same_cell():
    matched = _profile("paired scRNA-seq and scATAC-seq from matched samples")
    co_reported = _profile("scRNA-seq and scATAC-seq profiles from the study")

    assert matched.modalities == [Modality.MATCHED_SCRNA_SCATAC]
    assert matched.pairing_level == PairingLevel.SAME_SAMPLE
    assert co_reported.pairing_level == PairingLevel.STUDY_LEVEL
    assert Modality.MATCHED_SCRNA_SCATAC not in co_reported.modalities


def test_standalone_scatac_profile_is_not_paired():
    profile = _profile("single-cell ATAC-seq atlas of human tissues")
    assert profile.modalities == [Modality.SCATAC]
    assert profile.pairing_level == PairingLevel.NOT_APPLICABLE


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
        modalities=[Modality.MULTIOME_RNA_ATAC],
        acceptable_modalities=[Modality.MULTIOME_RNA_ATAC],
    )
    verified = DatasetCandidate(
        candidate_id="verified",
        title="10x multiome",
        modalities=[Modality.MULTIOME_RNA_ATAC],
        assay_profiles=[AssayProfile(
            label="10x",
            modalities=[Modality.MULTIOME_RNA_ATAC],
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
