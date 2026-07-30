from scdata_agent.domain.feasibility import FeasibilityAssessor
from scdata_agent.domain.models import (
    AccessLevel,
    CurationDecision,
    DataRole,
    DatasetCandidate,
    Modality,
)
from scdata_agent.domain.requirements import RuleBasedRequirementCompiler


def test_feasibility_is_eligible_when_all_known_gates_pass():
    requirement = RuleBasedRequirementCompiler().compile("human scATAC-seq")
    candidate = DatasetCandidate(
        candidate_id="ds_1",
        title="Human scATAC atlas",
        organisms=["Homo sapiens"],
        modalities=[Modality.SCATAC],
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
        modalities=[Modality.SCATAC],
        access_level=AccessLevel.PUBLIC_DECLARED,
    )
    report = FeasibilityAssessor().assess(requirement, candidate)
    assert report.decision == CurationDecision.MANUAL_REVIEW
    assert DataRole.FRAGMENTS in report.missing_required_roles

