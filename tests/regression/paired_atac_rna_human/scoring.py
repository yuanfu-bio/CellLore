"""Direction-match scoring for the paired_atac_rna_human golden case set.

Deliberately decoupled from how an outcome was produced (a real `agent-run`,
a captured-record replay, or a hand-built fixture in a unit test) -- this
module only answers "does this observed outcome agree with the curator's
recorded judgement", given an `ObservedOutcome`. See `golden_cases.yaml`'s
own header comment for what "agree" means: decision *direction*, not an
exact field match.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_GOLDEN_CASES = Path(__file__).resolve().parent / "golden_cases.yaml"

# The three curator-vocabulary decisions (READY/HOLD/EXCLUDE, straight out of
# 01_Studies.catalog_status / 04_Audit_Hold.队列动作) each accept more than
# one of this repo's own BundleStatus values, because "READY" in the
# spreadsheet does not distinguish PREFLIGHT_READY (declared-evidence,
# pre-materialization) from ADMITTED (post-materialization, CORPUS_READY) --
# the curator's own manual process did not separate those two states the way
# the two-tier lifecycle does, and the benchmark should not invent
# a distinction the ground truth itself does not make.
_DECISION_COMPATIBLE_STATUSES: dict[str, frozenset[str]] = {
    "READY": frozenset({"preflight_ready", "ready_for_acquisition", "admitted"}),
    "HOLD": frozenset({"needs_evidence", "quarantined"}),
    "EXCLUDE": frozenset({"rejected", "excluded"}),
}


@dataclass(frozen=True)
class ObservedOutcome:
    """What a real (or fixture-built) pipeline run produced for one case.

    `decision` is a BundleStatus value (lowercase string, e.g.
    "preflight_ready"). `relationship` is a RelationshipKind value or None
    when no pairing claim was resolved at all. `pair_unit_count` is None when
    pair-unit resolution never ran (e.g. the case was rejected before
    reaching that stage).
    """

    decision: str
    relationship: str | None = None
    pair_unit_count: int | None = None


@dataclass(frozen=True)
class CaseScore:
    case_id: str
    decision_match: bool
    relationship_match: bool | None
    pair_unit_delta: int | None
    expected_decision: str
    observed_decision: str
    expected_relationship: str | None
    observed_relationship: str | None

    @property
    def passed(self) -> bool:
        # relationship_match=None means the case declares no expectation --
        # absence of a check is not a failure. pair_unit_delta is reported
        # but, per golden_cases.yaml's own header, never a hard pass/fail
        # criterion on its own until real per-unit membership data with
        # matched/split/merge accounting backs it.
        return self.decision_match and self.relationship_match is not False


def load_golden_cases(path: Path = DEFAULT_GOLDEN_CASES) -> list[dict[str, Any]]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return document["cases"]


def score_case(case: dict[str, Any], observed: ObservedOutcome) -> CaseScore:
    expected = case["expected"]
    expected_decision = expected["decision"]
    expected_relationship = expected.get("relationship")
    expected_pair_units = expected.get("pair_unit_count")

    decision_match = observed.decision in _DECISION_COMPATIBLE_STATUSES.get(
        expected_decision, frozenset()
    )
    relationship_match = (
        None
        if expected_relationship is None
        else observed.relationship == expected_relationship
    )
    pair_unit_delta = (
        None
        if expected_pair_units is None or observed.pair_unit_count is None
        else observed.pair_unit_count - expected_pair_units
    )
    return CaseScore(
        case_id=case["id"],
        decision_match=decision_match,
        relationship_match=relationship_match,
        pair_unit_delta=pair_unit_delta,
        expected_decision=expected_decision,
        observed_decision=observed.decision,
        expected_relationship=expected_relationship,
        observed_relationship=observed.relationship,
    )
