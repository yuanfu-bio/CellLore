"""EvidenceClass soft ranking: for display/soft-ranking only, never used to
gate CurationProfile.required_evidence (see domain.curation.decision, which
uses explicit per-predicate acceptable-class allow-lists instead -- the same
"set membership, not rank" principle PairingRequirement already applies to
PairingLevel).
"""

from celllore.domain.models import EvidenceClass, evidence_class_rank, evidence_class_satisfies


def test_model_inferred_is_the_weakest_tier():
    assert evidence_class_rank(EvidenceClass.MODEL_INFERRED) < evidence_class_rank(
        EvidenceClass.REPOSITORY_REPORTED
    )


def test_declared_tiers_are_equal_rank():
    assert evidence_class_rank(EvidenceClass.REPOSITORY_REPORTED) == evidence_class_rank(
        EvidenceClass.ARTICLE_REPORTED
    )


def test_measured_tiers_outrank_declared_tiers():
    for measured in (
        EvidenceClass.FILE_OBSERVED, EvidenceClass.RECOMPUTED, EvidenceClass.ONTOLOGY_NORMALIZED,
    ):
        assert evidence_class_rank(measured) > evidence_class_rank(EvidenceClass.REPOSITORY_REPORTED)


def test_user_corrected_outranks_everything():
    for other in EvidenceClass:
        if other == EvidenceClass.USER_CORRECTED:
            continue
        assert evidence_class_rank(EvidenceClass.USER_CORRECTED) > evidence_class_rank(other)


class TestEvidenceClassSatisfies:
    def test_equal_to_floor_satisfies(self):
        assert evidence_class_satisfies(
            EvidenceClass.REPOSITORY_REPORTED, EvidenceClass.REPOSITORY_REPORTED
        )

    def test_above_floor_satisfies(self):
        assert evidence_class_satisfies(EvidenceClass.FILE_OBSERVED, EvidenceClass.REPOSITORY_REPORTED)

    def test_below_floor_does_not_satisfy(self):
        assert not evidence_class_satisfies(
            EvidenceClass.MODEL_INFERRED, EvidenceClass.REPOSITORY_REPORTED
        )

    def test_user_corrected_satisfies_any_floor(self):
        for floor in EvidenceClass:
            assert evidence_class_satisfies(EvidenceClass.USER_CORRECTED, floor)
