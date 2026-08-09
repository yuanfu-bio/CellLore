"""Bounded iterative search: coverage-gap detection and the search budget.

`ResourceDiscoveryAgent._review_results` used to allow at most one fixed
supplementary search and returned only a summary string. It now returns a
structured trajectory alongside the note, and gates the search budget via
`inspect_coverage_gaps` instead of a template. `_coverage_gaps` is the pure
part of that -- comparing what the requirement asked for against what
discovery has found so far -- and is what's tested directly here; the LLM
tool loop itself is exercised end to end by the `use_llm=False` policy path.
"""

import pytest

from scdata_agent.application.agents.discovery import (
    MAX_SUPPLEMENTARY_SEARCHES,
    ResourceDiscoveryAgent,
    _coverage_gaps,
)
from scdata_agent.application.agents.shared import AgentContext
from scdata_agent.application.collaboration import CollaborationLedger
from scdata_agent.domain.models import (
    DiscoveryRunResult,
    OmicsLayer,
    PairingLevel,
    PairingRequirement,
    QueryPlan,
    ResearchRequirement,
)


def test_max_supplementary_searches_is_more_than_the_old_fixed_cap_of_one():
    assert MAX_SUPPLEMENTARY_SEARCHES > 1


class TestCoverageGaps:
    def test_no_requirement_reports_no_gaps(self):
        result = _coverage_gaps(None, [])
        assert result["gaps"] == []

    def test_missing_required_layer_is_reported(self):
        requirement = ResearchRequirement(
            question="q", required_omics_layers=[OmicsLayer.PROTEIN],
        )
        result = _coverage_gaps(requirement, [{"assay_profiles": []}])
        assert any("protein" in gap for gap in result["gaps"])

    def test_observed_required_layer_is_not_reported_as_a_gap(self):
        requirement = ResearchRequirement(
            question="q", required_omics_layers=[OmicsLayer.PROTEIN],
        )
        candidates = [{"assay_profiles": [{"omics_layers": ["protein"]}]}]
        result = _coverage_gaps(requirement, candidates)
        assert result["gaps"] == []

    def test_missing_pairing_relationship_is_reported(self):
        requirement = ResearchRequirement(
            question="q",
            pairing_requirement=PairingRequirement(
                acceptable_levels=[PairingLevel.SAME_CELL, PairingLevel.SAME_NUCLEUS]
            ),
        )
        candidates = [{"assay_profiles": [{"pairing_level": "study_level"}]}]
        result = _coverage_gaps(requirement, candidates)
        assert any("pairing relationship" in gap for gap in result["gaps"])

    def test_observed_pairing_relationship_is_not_reported_as_a_gap(self):
        requirement = ResearchRequirement(
            question="q",
            pairing_requirement=PairingRequirement(
                acceptable_levels=[PairingLevel.SAME_CELL, PairingLevel.SAME_NUCLEUS]
            ),
        )
        candidates = [{"assay_profiles": [{"pairing_level": "same_cell"}]}]
        result = _coverage_gaps(requirement, candidates)
        assert result["gaps"] == []

    def test_candidate_count_is_always_reported(self):
        result = _coverage_gaps(None, [{"assay_profiles": []}, {"assay_profiles": []}])
        assert result["candidate_count"] == 2


@pytest.mark.asyncio
async def test_review_results_short_circuits_without_use_llm(tmp_path):
    """The offline/deterministic policy path must not attempt an LLM call and
    must still return the (note, trajectory) shape callers now expect."""
    requirement = ResearchRequirement(question="human lung scATAC-seq")
    discovery = DiscoveryRunResult(
        run_id="run_1", requirement=requirement,
        query_plan=QueryPlan(requirement=requirement, queries=[]),
        source_records=[], candidates=[], feasibility_reports=[],
    )
    context = AgentContext(
        run_id="run_1", question="human lung scATAC-seq",
        database_path=str(tmp_path / "discovery.sqlite"),
        ledger=CollaborationLedger(tmp_path / "discovery.sqlite"),
        policy={"use_llm": False}, requirement=requirement, discovery=discovery,
    )
    note, trajectory = await ResourceDiscoveryAgent()._review_results(
        context, workflow=None, extra_records=[]
    )
    assert note is None
    assert trajectory == []
