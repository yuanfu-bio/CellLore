import pytest
from pydantic import ValidationError

from scdata_agent.domain.lifecycle import (
    AuditQueueAction,
    AuditTask,
    AuditTaskCategoryFamily,
)


def test_blocking_category_accepts_arbitrary_open_strings():
    """Regression pin: 04_Audit_Hold has 46 distinct real values for this
    field, so it must never become a closed enum."""
    task = AuditTask(
        task_id="t1", run_id="run_1", subject_id="pair_unit_1",
        queue_action=AuditQueueAction.DEFERRED,
        blocking_category="a never-before-seen blocking reason",
    )
    assert task.blocking_category == "a never-before-seen blocking reason"


def test_category_family_defaults_to_other():
    task = AuditTask(
        task_id="t1", run_id="run_1", subject_id="pair_unit_1",
        queue_action=AuditQueueAction.DEFERRED, blocking_category="x",
    )
    assert task.category_family == AuditTaskCategoryFamily.OTHER


def test_queue_action_is_a_closed_set_of_five_real_values():
    assert {item.value for item in AuditQueueAction} == {
        "verify_then_archive", "deferred", "excluded", "reprocess", "partial_archive",
    }


def test_extra_fields_are_rejected():
    with pytest.raises(ValidationError):
        AuditTask(
            task_id="t1", run_id="run_1", subject_id="pair_unit_1",
            queue_action=AuditQueueAction.DEFERRED, blocking_category="x",
            made_up_field="nope",
        )


def test_bundle_id_is_optional_for_study_level_tasks():
    task = AuditTask(
        task_id="t1", run_id="run_1", subject_id="cand_1",
        queue_action=AuditQueueAction.EXCLUDED, blocking_category="access:auth_required",
    )
    assert task.bundle_id is None
