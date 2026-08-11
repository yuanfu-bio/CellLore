from celllore.domain.models import (
    AccessLevel,
    DatasetCandidate,
    DiscoveryRunResult,
)
from celllore.domain.requirements import QueryPlanner, RuleBasedRequirementCompiler
from celllore.infrastructure.persistence.evidence_ledger import EvidenceLedger


def test_ledger_roundtrip(tmp_path):
    requirement = RuleBasedRequirementCompiler().compile("human brain scATAC-seq")
    plan = QueryPlanner().plan(requirement)
    candidate = DatasetCandidate(
        candidate_id="ds_test",
        title="Test dataset",
        access_level=AccessLevel.PUBLIC_DECLARED,
    )
    result = DiscoveryRunResult(
        run_id="run_test",
        requirement=requirement,
        query_plan=plan,
        source_records=[],
        candidates=[candidate],
        feasibility_reports=[],
    )
    ledger = EvidenceLedger(tmp_path / "ledger.sqlite")
    ledger.save_run(result)

    assert ledger.load_requirement("run_test") == requirement
    assert ledger.load_plan("run_test") == plan
    assert ledger.load_candidates("run_test") == [candidate]

