"""CurationProfile is declared data the kernel reads, not code it branches on.

Phase 2 landed the profile skeleton and the `paired_atac_rna_human` instance
before any production caller starts using it (Phase 3). These tests pin the
structural guarantees the plan promised: GSM accession adjacency is excluded
by construction, the pairing requirement is a set-membership allow-list (not
a rank), and evidence has two distinct floors for acquisition vs corpus-ready.
"""

import pytest

from scdata_agent.domain.curation.profile import CurationProfile, CurationUnitGrouping
from scdata_agent.domain.curation.profiles import PROFILES, profile_by_id
from scdata_agent.domain.curation.profiles.paired_atac_rna_human import (
    PAIRED_ATAC_RNA_HUMAN,
)
from scdata_agent.domain.models import EvidenceClass, PairingLevel
from scdata_agent.domain.requirements import RuleBasedRequirementCompiler


def test_profile_by_id_resolves_the_registered_instance():
    assert profile_by_id("paired_atac_rna_human") is PAIRED_ATAC_RNA_HUMAN


def test_profile_by_id_raises_on_unknown_id():
    with pytest.raises(KeyError):
        profile_by_id("no_such_profile")


def test_registry_has_no_duplicate_ids():
    ids = [profile.profile_id for profile in PROFILES]
    assert len(ids) == len(set(ids))


class TestGsmAccessionAdjacencyIsStructurallyExcluded:
    """The hard architectural constraint: an accession number carries no
    experimental-design semantics and must never be used for grouping or
    pairing, in any weighted or fallback capacity."""

    def test_forbidden_signals_names_gsm_accession_adjacency(self):
        forbidden = PAIRED_ATAC_RNA_HUMAN.curation_unit_grouping.forbidden_signals
        assert "gsm_accession_adjacency" in forbidden

    def test_gsm_accession_adjacency_is_not_among_the_allowed_grouping_signals(self):
        signals = PAIRED_ATAC_RNA_HUMAN.curation_unit_grouping.grouping_signals
        assert not any("gsm" in signal and "accession" in signal for signal in signals)


class TestRequiredRelationshipIsSetMembership:
    def test_acceptable_levels_are_exactly_the_physical_co_measurement_levels(self):
        requirement = PAIRED_ATAC_RNA_HUMAN.required_relationship
        assert requirement is not None
        assert set(requirement.acceptable_levels) == {
            PairingLevel.SAME_CELL, PairingLevel.SAME_NUCLEUS,
        }

    def test_provenance_match_levels_are_excluded(self):
        requirement = PAIRED_ATAC_RNA_HUMAN.required_relationship
        assert not requirement.is_satisfied_by(PairingLevel.SAME_SAMPLE)
        assert not requirement.is_satisfied_by(PairingLevel.SAME_DONOR)


class TestTwoStageEvidenceAdmission:
    """Declared evidence must be enough to reach acquisition; corpus-ready is
    a strictly stronger, separate bar -- these must never collapse."""

    def _pair_relationship_evidence(self):
        return next(
            item for item in PAIRED_ATAC_RNA_HUMAN.required_evidence
            if item.predicate == "has_pair_relationship"
        )

    def test_acquisition_accepts_declared_evidence(self):
        evidence = self._pair_relationship_evidence()
        assert EvidenceClass.REPOSITORY_REPORTED in evidence.acceptable_for_acquisition

    def test_corpus_ready_requires_a_strictly_stronger_set(self):
        evidence = self._pair_relationship_evidence()
        assert EvidenceClass.FILE_OBSERVED in evidence.acceptable_for_corpus_ready
        assert EvidenceClass.REPOSITORY_REPORTED not in evidence.acceptable_for_corpus_ready
        assert set(evidence.acceptable_for_corpus_ready) != set(evidence.acceptable_for_acquisition)


class TestBundleSchemasExcludeProvenanceOnlyPairing:
    def test_matched_scrna_scatac_is_not_offered_by_this_profile(self):
        """Separate assays on matched material are a provenance match, not a
        physical one, and this profile's hard requirement excludes it."""
        schema_ids = {schema.schema_id for schema in PAIRED_ATAC_RNA_HUMAN.bundle_schemas}
        assert "matched_scrna_scatac" not in schema_ids
        assert "multiome_same_cell" in schema_ids


class TestCompilerPlumbingIsBehaviorNeutral:
    """Phase 2 wires `compile()` to read from a profile without changing what
    it returns -- the profile's search hints are the same alias tables the
    module-constant path already used. Phase 3 is where behavior can diverge."""

    @pytest.mark.parametrize("question", [
        "配对的scATAC-seq多组学数据",
        "human lung scATAC-seq",
        "10x Multiome joint profiling",
    ])
    def test_compiling_with_the_profile_matches_compiling_without_it(self, question):
        compiler = RuleBasedRequirementCompiler()
        without_profile = compiler.compile(question)
        with_profile = compiler.compile(question, profile=PAIRED_ATAC_RNA_HUMAN)

        assert without_profile.model_dump(exclude={"profile_id"}) == (
            with_profile.model_dump(exclude={"profile_id"})
        )
        assert without_profile.profile_id is None
        assert with_profile.profile_id == "paired_atac_rna_human"


def test_curation_unit_grouping_requires_a_label_and_description():
    grouping = CurationUnitGrouping(grouping_label="pair_unit", description="x")
    assert grouping.grouping_signals == []
    assert grouping.forbidden_signals == []


def test_curation_profile_defaults_are_empty_not_none():
    profile = CurationProfile(
        profile_id="minimal",
        label="Minimal",
        scientific_objective="x",
        curation_unit_grouping=CurationUnitGrouping(grouping_label="unit", description="x"),
    )
    assert profile.hard_requirements == []
    assert profile.required_relationship is None
    assert profile.bundle_schemas == ()
    assert profile.manifest_dialect is None


def test_technology_seeds_exclude_technologies_without_an_atac_layer():
    """CITE-seq is RNA+protein with no chromatin-accessibility layer, so it
    must not be offered as a seed for this ATAC-pairing-specific profile."""
    assert not any(
        "cite" in seed.casefold() for seed in PAIRED_ATAC_RNA_HUMAN.search_hints.technology_seeds
    )


def test_search_hints_still_cover_the_multiome_modality():
    assert "multiome" in PAIRED_ATAC_RNA_HUMAN.search_hints.technology_seeds
