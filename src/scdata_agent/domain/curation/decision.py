"""Evidence-completeness Decision: the generic core the plan promises.

"Decision = required evidence completeness (per-predicate acceptable
EvidenceClass set) + blocking deterministic validators -- never LLM
confidence." This module is the completeness half. It is deliberately a pure
function over a flat list of ``EvidenceClaim``s: it does not know about
``bundle_id``/``candidate_id``/``pair_unit_id`` at all. Different predicates
live at different subject granularities (``has_pair_relationship`` is
pair-unit scoped, ``has_omics_layer:*`` is candidate scoped,
``resolved_assembly`` is bundle scoped) -- assembling "the claims relevant to
one bundle" is the caller's job (see
``application.agents.progressive_preflight``), which keeps this function
trivially unit-testable and reusable for both the acquisition gate and the
corpus-ready gate.

Set membership, not rank: a predicate is satisfied by any claim whose
``evidence_class`` appears in the gate's declared acceptable list (see
``RequiredEvidence``'s docstring for why -- a global EvidenceClass rank
cannot tell "stronger" from "semantically wrong for this predicate"). The
one exception is ``USER_CORRECTED``, treated as universally acceptable
regardless of what a predicate's own lists contain, since a human reviewer's
explicit correction is appropriate evidence for anything by definition.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from scdata_agent.domain.curation.profile import CurationProfile
from scdata_agent.domain.lifecycle import EvidenceClaim
from scdata_agent.domain.models import EvidenceClass


class EvidenceGate(StrEnum):
    #: Declared evidence is enough -- a candidate may proceed to acquisition.
    ACQUISITION = "acquisition"
    #: The stronger floor a bundle must clear to reach the final corpus state.
    CORPUS_READY = "corpus_ready"


@dataclass(frozen=True)
class PredicateEvidenceResult:
    predicate: str
    acceptable_classes: tuple[EvidenceClass, ...]
    best_claim: EvidenceClaim | None
    satisfied: bool
    notes: str


@dataclass(frozen=True)
class EvidenceCompletenessResult:
    gate: EvidenceGate
    results: list[PredicateEvidenceResult]

    @property
    def satisfied(self) -> bool:
        return all(item.satisfied for item in self.results)

    @property
    def missing(self) -> list[PredicateEvidenceResult]:
        return [item for item in self.results if not item.satisfied]


def evaluate_evidence_completeness(
    profile: CurationProfile,
    gate: EvidenceGate,
    claims: Iterable[EvidenceClaim],
) -> EvidenceCompletenessResult:
    """For each ``profile.required_evidence`` predicate, check whether any
    matching claim's ``evidence_class`` is in the gate's acceptable set (or
    is ``USER_CORRECTED``). A predicate with no matching claim at all is
    unsatisfied, not silently skipped -- *unless* this gate's acceptable-class
    list is itself empty, which means the profile declared this predicate
    ungated at this tier (e.g. an assembly that can only be read from a
    downloaded file's own header has no acceptable pre-download evidence
    class at all; gating acquisition on it would block every candidate
    forever rather than deferring the real check to corpus-ready, where the
    stronger tier's own list still applies).
    """
    by_predicate: dict[str, list[EvidenceClaim]] = defaultdict(list)
    for claim in claims:
        by_predicate[claim.predicate].append(claim)

    results: list[PredicateEvidenceResult] = []
    for requirement in profile.required_evidence:
        acceptable = (
            requirement.acceptable_for_acquisition
            if gate is EvidenceGate.ACQUISITION
            else requirement.acceptable_for_corpus_ready
        )
        candidates = by_predicate.get(requirement.predicate, [])
        if not acceptable:
            results.append(
                PredicateEvidenceResult(
                    predicate=requirement.predicate,
                    acceptable_classes=(),
                    best_claim=candidates[0] if candidates else None,
                    satisfied=True,
                    notes=requirement.notes,
                )
            )
            continue
        satisfying = [
            item for item in candidates
            if item.evidence_class in acceptable or item.evidence_class == EvidenceClass.USER_CORRECTED
        ]
        best = satisfying[0] if satisfying else (candidates[0] if candidates else None)
        results.append(
            PredicateEvidenceResult(
                predicate=requirement.predicate,
                acceptable_classes=tuple(acceptable),
                best_claim=best,
                satisfied=bool(satisfying),
                notes=requirement.notes,
            )
        )
    return EvidenceCompletenessResult(gate=gate, results=results)
