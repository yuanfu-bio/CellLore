from celllore.domain.corpus import CorpusGovernor
from celllore.domain.models import (
    AccessLevel,
    AssetBundle,
    CorpusPool,
    CurationDecision,
    DatasetCandidate,
    FeasibilityReport,
)


def _report(decision=CurationDecision.ELIGIBLE):
    return FeasibilityReport(
        report_id="report_1",
        candidate_id="candidate_1",
        dimensions={},
        decision=decision,
    )


def _bundle(complete=True):
    return AssetBundle(
        bundle_id="bundle_1",
        candidate_id="candidate_1",
        bundle_type="processed_atac",
        required_roles=[],
        complete=complete,
    )


def test_evaluation_lineage_is_excluded_from_pretraining():
    candidate = DatasetCandidate(
        candidate_id="candidate_1",
        title="dataset",
        access_level=AccessLevel.PUBLIC_VERIFIED,
    )
    decision = CorpusGovernor().decide(
        candidate,
        _report(),
        [_bundle()],
        lineage_family_ids=["family_1"],
        evaluation_family_ids={"family_1"},
    )

    assert decision.pool == CorpusPool.EXCLUDE
    assert decision.reason_codes == ["evaluation_lineage_leakage"]


def test_incomplete_bundle_is_quarantined():
    candidate = DatasetCandidate(candidate_id="candidate_1", title="dataset")
    decision = CorpusGovernor().decide(
        candidate,
        _report(),
        [_bundle(complete=False)],
    )

    assert decision.pool == CorpusPool.QUARANTINE
    assert not decision.eligible
