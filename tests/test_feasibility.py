from celllore.domain.feasibility import FeasibilityAssessor
from celllore.domain.models import (
    AccessLevel,
    AssayProfile,
    CurationDecision,
    DataRole,
    DatasetCandidate,
    GateStatus,
    OmicsLayer,
    PairingLevel,
    PairingRequirement,
    ResearchRequirement,
)
from celllore.domain.requirements import RuleBasedRequirementCompiler


def _scatac_profile() -> AssayProfile:
    return AssayProfile(
        label="x", omics_layers=[OmicsLayer.CHROMATIN_ACCESSIBILITY], rationale="x",
    )


def test_feasibility_is_eligible_when_all_known_gates_pass():
    requirement = RuleBasedRequirementCompiler().compile("human scATAC-seq")
    candidate = DatasetCandidate(
        candidate_id="ds_1",
        title="Human scATAC atlas",
        organisms=["Homo sapiens"],
        assay_profiles=[_scatac_profile()],
        observed_data_roles=list(requirement.required_data_roles),
        access_level=AccessLevel.PUBLIC_VERIFIED,
    )
    report = FeasibilityAssessor().assess(requirement, candidate)
    assert report.decision == CurationDecision.ELIGIBLE
    assert report.missing_required_roles == []


def test_feasibility_abstains_when_file_manifest_is_missing():
    requirement = RuleBasedRequirementCompiler().compile("human scATAC-seq")
    candidate = DatasetCandidate(
        candidate_id="ds_1",
        title="Possible human scATAC atlas",
        organisms=["Homo sapiens"],
        assay_profiles=[_scatac_profile()],
        access_level=AccessLevel.PUBLIC_DECLARED,
    )
    report = FeasibilityAssessor().assess(requirement, candidate)
    assert report.decision == CurationDecision.MANUAL_REVIEW
    assert DataRole.FRAGMENTS in report.missing_required_roles


class TestJointLayerRequirementIsNotSatisfiedByOneLayer:
    """A standalone-ATAC record must not
    pass the topic-relevance gate for a same-cell-multiome question just
    because it shares one of the two requested layers. `layer_relevance` may
    use "any overlap" for a single-layer question, but a pairing requirement
    means all requested layers are wanted *together*."""

    def _multiome_requirement(self) -> ResearchRequirement:
        return ResearchRequirement(
            question="paired same-cell multiome RNA and ATAC",
            required_omics_layers=[
                OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME,
            ],
            pairing_requirement=PairingRequirement(
                acceptable_levels=[PairingLevel.SAME_CELL, PairingLevel.SAME_NUCLEUS]
            ),
        )

    def test_standalone_atac_fails_the_joint_layer_requirement(self):
        requirement = self._multiome_requirement()
        candidate = DatasetCandidate(
            candidate_id="ds_1", title="Standalone snATAC-seq",
            assay_profiles=[_scatac_profile()],
        )
        report = FeasibilityAssessor().assess(requirement, candidate)
        assert report.dimensions["layer_relevance"].status == GateStatus.FAIL

    def test_both_layers_together_satisfy_the_joint_requirement(self):
        requirement = self._multiome_requirement()
        candidate = DatasetCandidate(
            candidate_id="ds_1", title="10x Multiome",
            assay_profiles=[AssayProfile(
                label="x",
                omics_layers=[OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME],
                pairing_level=PairingLevel.SAME_NUCLEUS, rationale="x",
            )],
        )
        report = FeasibilityAssessor().assess(requirement, candidate)
        assert report.dimensions["layer_relevance"].status == GateStatus.PASS

    def test_without_a_pairing_requirement_any_overlap_still_passes(self):
        """A single-layer question (no pairing asked for) keeps the looser
        any-overlap semantics -- only a *joint* requirement needs the
        stricter subset check."""
        requirement = ResearchRequirement(
            question="human scATAC-seq",
            required_omics_layers=[
                OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME,
            ],
        )
        candidate = DatasetCandidate(
            candidate_id="ds_1", title="Standalone snATAC-seq",
            assay_profiles=[_scatac_profile()],
        )
        report = FeasibilityAssessor().assess(requirement, candidate)
        assert report.dimensions["layer_relevance"].status == GateStatus.PASS
