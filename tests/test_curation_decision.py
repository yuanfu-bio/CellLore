"""evaluate_evidence_completeness in total isolation: no agents, no bundles,
just CurationProfile + EvidenceClaim in, EvidenceCompletenessResult out.

Set membership, not rank: a predicate declares exactly which EvidenceClass
values it accepts, at each gate. USER_CORRECTED is a universal exception --
never something a predicate needs to list to accept it.
"""

from scdata_agent.domain.curation.decision import (
    EvidenceGate,
    evaluate_evidence_completeness,
)
from scdata_agent.domain.curation.profile import (
    CurationProfile,
    CurationUnitGrouping,
    RequiredEvidence,
)
from scdata_agent.domain.curation.profiles.paired_atac_rna_human import PAIRED_ATAC_RNA_HUMAN
from scdata_agent.domain.lifecycle import EvidenceClaim
from scdata_agent.domain.models import EvidenceClass


def _profile(*required_evidence: RequiredEvidence) -> CurationProfile:
    return CurationProfile(
        profile_id="test_profile", label="Test", scientific_objective="x",
        curation_unit_grouping=CurationUnitGrouping(grouping_label="unit", description="x"),
        required_evidence=list(required_evidence),
    )


def _requirement(
    predicate: str,
    acceptable_for_acquisition: list[EvidenceClass],
    acceptable_for_corpus_ready: list[EvidenceClass] | None = None,
) -> RequiredEvidence:
    return RequiredEvidence(
        predicate=predicate,
        acceptable_for_acquisition=acceptable_for_acquisition,
        acceptable_for_corpus_ready=acceptable_for_corpus_ready or acceptable_for_acquisition,
    )


def _claim(predicate: str, evidence_class: EvidenceClass, claim_id: str = "c1") -> EvidenceClaim:
    return EvidenceClaim(
        claim_id=claim_id, subject_id="s1", predicate=predicate, value=True,
        evidence_class=evidence_class,
    )


def test_no_matching_claim_is_unsatisfied():
    profile = _profile(_requirement("has_x", [EvidenceClass.REPOSITORY_REPORTED]))
    result = evaluate_evidence_completeness(profile, EvidenceGate.ACQUISITION, [])

    assert not result.satisfied
    assert [item.predicate for item in result.missing] == ["has_x"]
    assert result.results[0].best_claim is None


def test_claim_outside_the_acceptable_set_is_unsatisfied():
    profile = _profile(_requirement("has_x", [EvidenceClass.REPOSITORY_REPORTED]))
    claims = [_claim("has_x", EvidenceClass.MODEL_INFERRED)]
    result = evaluate_evidence_completeness(profile, EvidenceGate.ACQUISITION, claims)

    assert not result.satisfied


def test_claim_in_the_acceptable_set_is_satisfied():
    profile = _profile(_requirement("has_x", [EvidenceClass.REPOSITORY_REPORTED]))
    claims = [_claim("has_x", EvidenceClass.REPOSITORY_REPORTED)]
    result = evaluate_evidence_completeness(profile, EvidenceGate.ACQUISITION, claims)

    assert result.satisfied


def test_a_higher_ranked_class_not_in_the_acceptable_set_still_fails():
    """The exact scenario acceptable-sets exist to prevent: ONTOLOGY_NORMALIZED
    outranks REPOSITORY_REPORTED on the old global scale, but it is not
    semantically appropriate evidence for a predicate that never listed it."""
    profile = _profile(_requirement("has_x", [EvidenceClass.REPOSITORY_REPORTED]))
    claims = [_claim("has_x", EvidenceClass.ONTOLOGY_NORMALIZED)]
    result = evaluate_evidence_completeness(profile, EvidenceGate.ACQUISITION, claims)

    assert not result.satisfied


def test_user_corrected_satisfies_any_predicate_without_being_listed():
    profile = _profile(_requirement("has_x", [EvidenceClass.REPOSITORY_REPORTED]))
    claims = [_claim("has_x", EvidenceClass.USER_CORRECTED)]
    result = evaluate_evidence_completeness(profile, EvidenceGate.ACQUISITION, claims)

    assert result.satisfied


def test_a_satisfying_claim_among_several_is_found_regardless_of_order():
    profile = _profile(_requirement("has_x", [EvidenceClass.FILE_OBSERVED]))
    claims = [
        _claim("has_x", EvidenceClass.MODEL_INFERRED, "weak"),
        _claim("has_x", EvidenceClass.FILE_OBSERVED, "strong"),
    ]
    result = evaluate_evidence_completeness(profile, EvidenceGate.ACQUISITION, claims)

    assert result.satisfied
    assert result.results[0].best_claim.claim_id == "strong"


def test_each_predicate_is_judged_independently_all_not_any():
    profile = _profile(
        _requirement("has_x", [EvidenceClass.REPOSITORY_REPORTED]),
        _requirement("has_y", [EvidenceClass.REPOSITORY_REPORTED]),
    )
    claims = [_claim("has_x", EvidenceClass.REPOSITORY_REPORTED)]  # has_y missing entirely
    result = evaluate_evidence_completeness(profile, EvidenceGate.ACQUISITION, claims)

    assert not result.satisfied
    assert [item.predicate for item in result.missing] == ["has_y"]


def test_acquisition_and_corpus_ready_gates_can_disagree_on_the_same_claims():
    """Uses the real paired_atac_rna_human sets for has_pair_relationship:
    REPOSITORY_REPORTED clears acquisition but not the
    FILE_OBSERVED/RECOMPUTED-only corpus-ready set."""
    claims = [_claim("has_pair_relationship", EvidenceClass.REPOSITORY_REPORTED)]

    acquisition = evaluate_evidence_completeness(PAIRED_ATAC_RNA_HUMAN, EvidenceGate.ACQUISITION, claims)
    corpus_ready = evaluate_evidence_completeness(PAIRED_ATAC_RNA_HUMAN, EvidenceGate.CORPUS_READY, claims)

    pairing_acquisition = next(r for r in acquisition.results if r.predicate == "has_pair_relationship")
    pairing_corpus_ready = next(r for r in corpus_ready.results if r.predicate == "has_pair_relationship")
    assert pairing_acquisition.satisfied
    assert not pairing_corpus_ready.satisfied


def test_an_empty_acceptable_list_means_ungated_not_unsatisfiable():
    """resolved_assembly's real shape: no claim exists yet (nothing infers a
    genome build before download), and the profile declares it ungated at
    acquisition on purpose. An empty acceptable-class list must not act like
    the strictest possible requirement -- that would block every candidate
    forever, the opposite of what an empty list is meant to express."""
    profile = _profile(_requirement("resolved_assembly", []))
    result = evaluate_evidence_completeness(profile, EvidenceGate.ACQUISITION, [])

    assert result.satisfied
    assert result.results[0].best_claim is None


def test_an_empty_acceptable_list_still_surfaces_a_claim_if_one_exists():
    """Ungated does not mean the claim is thrown away -- if the text did
    state a build, it should still be visible on the result for display."""
    profile = _profile(_requirement("resolved_assembly", []))
    claims = [_claim("resolved_assembly", EvidenceClass.REPOSITORY_REPORTED)]
    result = evaluate_evidence_completeness(profile, EvidenceGate.ACQUISITION, claims)

    assert result.satisfied
    assert result.results[0].best_claim is not None


def test_resolved_assembly_never_blocks_paired_atac_rna_human_acquisition():
    """The real profile change this pins: acquisition must proceed on
    pairing/role evidence alone, with genome build left for post-download
    corpus-ready determination."""
    acquisition = evaluate_evidence_completeness(PAIRED_ATAC_RNA_HUMAN, EvidenceGate.ACQUISITION, [])
    corpus_ready = evaluate_evidence_completeness(PAIRED_ATAC_RNA_HUMAN, EvidenceGate.CORPUS_READY, [])

    assembly_acquisition = next(r for r in acquisition.results if r.predicate == "resolved_assembly")
    assembly_corpus_ready = next(r for r in corpus_ready.results if r.predicate == "resolved_assembly")
    assert assembly_acquisition.satisfied
    assert not assembly_corpus_ready.satisfied
