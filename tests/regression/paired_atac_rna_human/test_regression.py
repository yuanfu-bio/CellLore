"""Two layers of coverage, deliberately kept separate.

1. Structural sanity on golden_cases.yaml itself, and unit tests of the
   scoring logic in scoring.py against synthetic ObservedOutcome fixtures --
   both run today, no network or captured data required.
2. The real per-case replay (run each accession through the actual pipeline,
   score the result against the golden case) -- explicitly not implemented
   yet. See README.md in this directory for why and what it needs; the
   skipped test below exists so the gap shows up in `pytest` output instead
   of silently not existing.
"""

from __future__ import annotations

import pytest
from scoring import ObservedOutcome, load_golden_cases, score_case

CASES = load_golden_cases()
VALID_DECISIONS = {"READY", "HOLD", "EXCLUDE"}
VALID_RELATIONSHIPS = {"physical_co_measurement", "provenance_match", "not_applicable"}


class TestGoldenCasesAreWellFormed:
    def test_case_ids_are_unique(self):
        ids = [case["id"] for case in CASES]
        assert len(ids) == len(set(ids))

    def test_every_case_declares_a_valid_expected_decision(self):
        for case in CASES:
            assert case["expected"]["decision"] in VALID_DECISIONS, case["id"]

    def test_every_case_with_a_relationship_expectation_uses_a_real_kind(self):
        for case in CASES:
            relationship = case["expected"].get("relationship")
            if relationship is not None:
                assert relationship in VALID_RELATIONSHIPS, case["id"]

    def test_every_case_has_an_accession_and_a_curator_reason(self):
        for case in CASES:
            assert case["accession"].startswith("GSE"), case["id"]
            assert case["curator_reason"].strip(), case["id"]

    def test_all_three_decisions_are_represented(self):
        decisions = {case["expected"]["decision"] for case in CASES}
        assert decisions == VALID_DECISIONS

    def test_exclude_cases_cover_both_provenance_match_and_not_applicable(self):
        """The load-bearing distinction this benchmark exists to pin: EXCLUDE
        for "matched but not same-cell" (provenance_match) must not collapse
        into the same bucket as EXCLUDE for "no second layer at all"
        (not_applicable) -- they are excluded for entirely different reasons
        and a fix to one must not be graded against the other."""
        exclude_relationships = {
            case["expected"]["relationship"]
            for case in CASES
            if case["expected"]["decision"] == "EXCLUDE"
        }
        assert "provenance_match" in exclude_relationships
        assert "not_applicable" in exclude_relationships


class TestScoreCase:
    def _case(self, case_id: str) -> dict:
        return next(item for item in CASES if item["id"] == case_id)

    def test_matching_ready_outcome_passes(self):
        case = self._case("ready_exact_gsm")
        observed = ObservedOutcome(
            decision="preflight_ready",
            relationship="physical_co_measurement",
            pair_unit_count=7,
        )
        score = score_case(case, observed)
        assert score.passed
        assert score.decision_match
        assert score.relationship_match
        assert score.pair_unit_delta == 0

    def test_admitted_also_counts_as_the_curator_s_ready(self):
        """READY in the spreadsheet doesn't distinguish PREFLIGHT_READY from
        the fuller post-materialization ADMITTED state -- either satisfies
        the curator's "READY" judgement."""
        case = self._case("ready_exact_gsm")
        observed = ObservedOutcome(decision="admitted", relationship="physical_co_measurement")
        assert score_case(case, observed).decision_match

    def test_needs_evidence_does_not_satisfy_a_ready_expectation(self):
        case = self._case("ready_exact_gsm")
        observed = ObservedOutcome(decision="needs_evidence", relationship="physical_co_measurement")
        score = score_case(case, observed)
        assert not score.passed
        assert not score.decision_match

    def test_matched_provenance_only_case_fails_if_admitted(self):
        """The exact regression this benchmark exists to catch: a
        provenance-match-only study (GSE295489-shaped) reaching an admitted
        state would be the P0.0-class overclaim all over again."""
        case = self._case("exclude_matched_separate_chemistry_5p_v2")
        observed = ObservedOutcome(decision="admitted", relationship="provenance_match")
        score = score_case(case, observed)
        assert not score.passed
        assert not score.decision_match

    def test_matched_provenance_only_case_passes_when_correctly_rejected(self):
        case = self._case("exclude_matched_separate_chemistry_5p_v2")
        observed = ObservedOutcome(decision="rejected", relationship="provenance_match")
        score = score_case(case, observed)
        assert score.passed

    def test_correct_decision_with_wrong_relationship_kind_still_fails(self):
        """Getting the pool right (EXCLUDE) for the wrong reason
        (not_applicable instead of provenance_match) is still a miss -- the
        relationship kind is what the case is actually testing."""
        case = self._case("exclude_matched_separate_chemistry_5p_v2")
        observed = ObservedOutcome(decision="rejected", relationship="not_applicable")
        score = score_case(case, observed)
        assert score.decision_match
        assert score.relationship_match is False
        assert not score.passed

    def test_a_case_with_no_relationship_expectation_is_not_penalized_for_it(self):
        case = self._case("hold_missing_atac_fragment")
        assert case["expected"]["relationship"] == "physical_co_measurement"
        # This case's pair_unit_count is null (unresolved study) -- confirm
        # that produces no delta rather than a spurious mismatch.
        observed = ObservedOutcome(decision="needs_evidence", relationship="physical_co_measurement")
        score = score_case(case, observed)
        assert score.pair_unit_delta is None

    def test_pair_unit_delta_is_informational_not_a_hard_failure(self):
        """An off-by-a-little pair-unit count is reported but must not, on
        its own, flip `passed` -- see golden_cases.yaml's header on why this
        stays loose until real per-unit membership data is available."""
        case = self._case("ready_exact_gsm")
        observed = ObservedOutcome(
            decision="preflight_ready", relationship="physical_co_measurement",
            pair_unit_count=6,
        )
        score = score_case(case, observed)
        assert score.pair_unit_delta == -1
        assert score.passed


@pytest.mark.skip(
    reason=(
        "No captured replay records yet -- this benchmark's ground truth "
        "(golden_cases.yaml) and scoring logic (scoring.py) are ready and "
        "covered above, but running each case through the real pipeline "
        "needs a real `agent-run`/LLM capture per accession, deliberately "
        "deferred (see README.md). Wiring this in is a tracked follow-up, "
        "not a silent gap."
    )
)
@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_real_pipeline_matches_the_curator(case):
    raise NotImplementedError
