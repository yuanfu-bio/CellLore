"""Versioned contracts for evidence maturity and bundle materialization."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from celllore.domain.models import (
    AccessLevel,
    AssetAvailability,
    DataAsset,
    DataRole,
    EvidenceClass,
    GateStatus,
    StrictModel,
    TransferProtocol,
    ValidationStatus,
    utc_now,
)

SCHEMA_VERSION = "2.0"


class PointerStatus(StrEnum):
    REPORTED = "reported"
    VALIDATED = "validated"
    CONFLICTED = "conflicted"
    INFERRED = "inferred"


class LicenseStatus(StrEnum):
    APPROVED = "approved"
    # No per-record licence exists, but the hosting archive's own terms cover
    # redistribution. Deliberately distinct from APPROVED: nobody read a
    # licence for this record, so the basis must stay visible to a reviewer.
    REPOSITORY_POLICY = "repository_policy"
    RESTRICTED = "restricted"
    UNKNOWN = "unknown"


class BundleStatus(StrEnum):
    """A bundle's lifecycle stage.

    This is the two-stage evidence-admission lifecycle a ``CurationProfile``
    gates against (see ``RequiredEvidence.acceptable_for_acquisition`` vs
    ``acceptable_for_corpus_ready``), already present rather than needing a new
    enum: declared/reported evidence is enough to reach ``PREFLIGHT_READY``
    (ready to acquire) without waiting on evidence that cannot exist before a
    download happens; only ``ADMITTED`` (via ``QC_PASSED``/``QC_WARN``) claims
    the stronger, post-download, deterministically-validated state.

        LEAD               -- discovered, no evidence linked yet
        EVIDENCE_LINKED     -- declared/reported evidence attached
        NEEDS_EVIDENCE      -- a pre-acquisition hold: evidence gap
        PREFLIGHT_READY     -- cleared to acquire on declared evidence alone
        ...TRANSFERRED...
        MATERIALIZED        -- files downloaded
        QC_PASSED/QC_WARN   -- post-download deterministic validators ran
        QUARANTINED         -- a post-materialization hold
        ADMITTED            -- final corpus-ready terminal state
        REJECTED/EXCLUDED   -- terminal exclusion, at either stage
    """

    LEAD = "lead"
    EVIDENCE_LINKED = "evidence_linked"
    REMOTE_INVENTORIED = "remote_inventoried"
    PREFLIGHT_READY = "preflight_ready"
    NEEDS_EVIDENCE = "needs_evidence"
    REJECTED = "rejected"
    PROPOSED = "proposed"
    APPROVED = "approved"
    TRANSFERRED = "transferred"
    TRANSFER_FAILED = "transfer_failed"
    MATERIALIZED = "materialized"
    RAW_ONLY = "raw_only"
    MATERIALIZATION_FAILED = "materialization_failed"
    QC_PASSED = "qc_passed"
    QC_WARN = "qc_warn"
    QUARANTINED = "quarantined"
    ADMITTED = "admitted"
    EXCLUDED = "excluded"


ALLOWED_BUNDLE_TRANSITIONS: dict[BundleStatus, set[BundleStatus]] = {
    BundleStatus.LEAD: {BundleStatus.EVIDENCE_LINKED, BundleStatus.REJECTED},
    BundleStatus.EVIDENCE_LINKED: {
        BundleStatus.REMOTE_INVENTORIED,
        BundleStatus.NEEDS_EVIDENCE,
        BundleStatus.REJECTED,
    },
    BundleStatus.NEEDS_EVIDENCE: {
        BundleStatus.EVIDENCE_LINKED,
        BundleStatus.REMOTE_INVENTORIED,
        BundleStatus.REJECTED,
    },
    BundleStatus.REMOTE_INVENTORIED: {
        BundleStatus.PREFLIGHT_READY,
        BundleStatus.NEEDS_EVIDENCE,
        BundleStatus.REJECTED,
    },
    BundleStatus.PREFLIGHT_READY: {
        BundleStatus.PROPOSED,
        BundleStatus.REJECTED,
        BundleStatus.QUARANTINED,
    },
    BundleStatus.PROPOSED: {BundleStatus.APPROVED, BundleStatus.EXCLUDED},
    BundleStatus.APPROVED: {
        BundleStatus.TRANSFERRED,
        BundleStatus.TRANSFER_FAILED,
    },
    BundleStatus.TRANSFER_FAILED: {
        BundleStatus.APPROVED,
        BundleStatus.QUARANTINED,
        BundleStatus.EXCLUDED,
    },
    BundleStatus.TRANSFERRED: {
        BundleStatus.MATERIALIZED,
        BundleStatus.RAW_ONLY,
        BundleStatus.MATERIALIZATION_FAILED,
    },
    BundleStatus.MATERIALIZATION_FAILED: {
        BundleStatus.TRANSFERRED,
        BundleStatus.QUARANTINED,
    },
    BundleStatus.MATERIALIZED: {
        BundleStatus.QC_PASSED,
        BundleStatus.QC_WARN,
        BundleStatus.QUARANTINED,
    },
    BundleStatus.RAW_ONLY: {BundleStatus.QUARANTINED, BundleStatus.EXCLUDED},
    BundleStatus.QC_PASSED: {
        BundleStatus.ADMITTED,
        BundleStatus.QUARANTINED,
        BundleStatus.EXCLUDED,
    },
    BundleStatus.QC_WARN: {
        BundleStatus.ADMITTED,
        BundleStatus.QUARANTINED,
        BundleStatus.EXCLUDED,
    },
    BundleStatus.QUARANTINED: {
        BundleStatus.ADMITTED,
        BundleStatus.EXCLUDED,
    },
    BundleStatus.REJECTED: set(),
    BundleStatus.ADMITTED: set(),
    BundleStatus.EXCLUDED: set(),
}


def validate_bundle_transition(current: BundleStatus, target: BundleStatus) -> None:
    """Reject lifecycle jumps that would erase an audit boundary."""
    if current == target:
        return
    if target not in ALLOWED_BUNDLE_TRANSITIONS[current]:
        raise ValueError(
            f"Invalid bundle transition: {current.value} -> {target.value}"
        )


class EvidenceClaim(StrictModel):
    claim_id: str
    subject_id: str
    predicate: str
    object_id: str | None = None
    value: Any | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    evidence_class: EvidenceClass
    validation_status: ValidationStatus = ValidationStatus.UNVALIDATED
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    rationale: str = ""
    schema_version: str = SCHEMA_VERSION

    @model_validator(mode="after")
    def require_object_or_value(self) -> EvidenceClaim:
        if self.object_id is None and self.value is None:
            raise ValueError("A claim requires object_id or value.")
        return self


class ResourcePointer(StrictModel):
    pointer_id: str
    candidate_id: str
    namespace: str
    locator: str
    source_name: str
    status: PointerStatus
    pointer_kind: str = "repository_accession"
    origin_stage: str = "legacy"
    source_record_id: str | None = None
    origin_artifact_id: str | None = None
    supersedes_pointer_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    schema_version: str = SCHEMA_VERSION

    @property
    def scoutable(self) -> bool:
        return self.status in {
            PointerStatus.REPORTED,
            PointerStatus.VALIDATED,
        }


class RemoteAssetObservation(StrictModel):
    observation_id: str
    asset_id: str
    candidate_id: str
    observed_at: datetime = Field(default_factory=utc_now)
    access_level: AccessLevel
    availability: AssetAvailability
    size_bytes: int | None = Field(default=None, ge=0)
    checksum_algorithm: str | None = None
    checksum_declared: str | None = None
    probe_method: str
    http_status: int | None = None
    final_url: str | None = None
    content_type: str | None = None
    accept_ranges: bool | None = None
    redirected: bool = False
    probe_error: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    schema_version: str = SCHEMA_VERSION


class SemanticBundle(StrictModel):
    bundle_id: str
    candidate_id: str
    # The ProposedPairUnit (domain.pair_units) this bundle was built from, when
    # the candidate's assets were split by PairUnitResolver rather than kept as
    # one bundle per candidate. None for candidates with no resolved units
    # (e.g. no GEO sample metadata was available) and for bundles built via the
    # curated-manifest path, which already carries its own `pair_unit_id`
    # concept at the manifest-row level.
    pair_unit_id: str | None = None
    bundle_type: str
    asset_ids: list[str] = Field(default_factory=list)
    required_roles: list[DataRole] = Field(default_factory=list)
    optional_roles: list[DataRole] = Field(default_factory=list)
    missing_roles: list[DataRole] = Field(default_factory=list)
    status: BundleStatus = BundleStatus.REMOTE_INVENTORIED
    evidence_ids: list[str] = Field(default_factory=list)
    # Alternative bundle shapes this candidate could satisfy, and the ones it
    # actually does. Satisfying any one schema makes the bundle complete, so a
    # standalone scATAC study is not judged against multiome requirements.
    applicable_schema_ids: list[str] = Field(default_factory=list)
    satisfied_schema_ids: list[str] = Field(default_factory=list)
    observed_roles: list[DataRole] = Field(default_factory=list)
    schema_version: str = SCHEMA_VERSION

    @property
    def complete(self) -> bool:
        if not self.asset_ids:
            return False
        if self.applicable_schema_ids:
            return bool(self.satisfied_schema_ids)
        # Bundles built before schema evaluation keep the flat-role semantics.
        return not self.missing_roles


class PreflightBundleReport(StrictModel):
    report_id: str
    bundle_id: str
    candidate_id: str
    dimensions: dict[str, GateStatus]
    rationale: dict[str, str] = Field(default_factory=dict)
    license_status: LicenseStatus = LicenseStatus.UNKNOWN
    # Populated only for REPOSITORY_POLICY: the archive policies being relied
    # on, so the licence verdict can be audited rather than merely trusted. A
    # bundle split across archives legitimately cites more than one.
    license_policy_ids: list[str] = Field(default_factory=list)
    license_basis: str | None = None
    expected_bytes: int | None = Field(default=None, ge=0)
    storage_scale: dict[str, Any] = Field(default_factory=dict)
    biological_scale: dict[str, int | None] = Field(default_factory=dict)
    file_type_counts: dict[str, int] = Field(default_factory=dict)
    role_counts: dict[str, int] = Field(default_factory=dict)
    access_status: str = "unknown"
    hard_blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    decision: BundleStatus
    evidence_ids: list[str] = Field(default_factory=list)
    schema_version: str = SCHEMA_VERSION

    @model_validator(mode="after")
    def require_preflight_decision(self) -> PreflightBundleReport:
        if self.decision not in {
            BundleStatus.PREFLIGHT_READY,
            BundleStatus.NEEDS_EVIDENCE,
            BundleStatus.REJECTED,
        }:
            raise ValueError("Preflight decision must be a pre-acquisition state.")
        return self


class AcquisitionBundle(StrictModel):
    bundle_id: str
    candidate_id: str
    asset_ids: list[str] = Field(default_factory=list)
    priority: int = Field(ge=1)
    score: float
    expected_bytes: int | None = Field(default=None, ge=0)
    requires_size_override: bool = False
    decision_tier: str = "manual_review"
    lineage_family_ids: list[str] = Field(default_factory=list)
    representative_bundle_id: str | None = None
    score_components: dict[str, float] = Field(default_factory=dict)
    hard_blockers: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class AcquisitionPlan(StrictModel):
    plan_id: str
    run_id: str
    bundles: list[AcquisitionBundle] = Field(default_factory=list)
    recommended_bundle_ids: list[str] = Field(default_factory=list)
    manual_review_bundle_ids: list[str] = Field(default_factory=list)
    excluded_bundle_ids: list[str] = Field(default_factory=list)
    total_expected_bytes: int | None = Field(default=None, ge=0)
    destination_root: str
    schema_version: str = SCHEMA_VERSION


class TransferReceiptStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class TransferReceipt(StrictModel):
    receipt_id: str
    plan_id: str
    bundle_id: str
    asset_id: str
    source_url: str
    local_path: str
    protocol: TransferProtocol
    status: TransferReceiptStatus
    bytes_written: int = Field(default=0, ge=0)
    checksum_algorithm: str | None = None
    expected_checksum: str | None = None
    observed_checksum: str | None = None
    checksum_valid: bool | None = None
    resumed: bool = False
    error: str | None = None
    completed_at: datetime = Field(default_factory=utc_now)
    schema_version: str = SCHEMA_VERSION


class MaterializationStatus(StrEnum):
    MATERIALIZED = "materialized"
    RAW_ONLY = "raw_only"
    FAILED = "failed"


class MaterializationManifest(StrictModel):
    manifest_id: str
    bundle_id: str
    candidate_id: str
    status: MaterializationStatus
    source_receipt_ids: list[str] = Field(default_factory=list)
    source_asset_ids: list[str] = Field(default_factory=list)
    package_path: str
    standard_object_path: str | None = None
    data_roles: list[DataRole] = Field(default_factory=list)
    source_asset_roles: dict[str, DataRole] = Field(default_factory=dict)
    genome_builds: list[str] = Field(default_factory=list)
    provenance_path: str | None = None
    data_card_path: str | None = None
    # The structured source the Data Card is rendered from. Kept on the manifest
    # so tests and downstream consumers can assert on fields rather than parse
    # markdown, and so a card is never richer than what was actually recorded.
    data_card_fields: dict[str, Any] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    schema_version: str = SCHEMA_VERSION


class MaterializedQCReport(StrictModel):
    report_id: str
    bundle_id: str
    candidate_id: str
    status: GateStatus
    structural_checks: dict[str, GateStatus] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    unavailable_metrics: list[str] = Field(default_factory=list)
    inspected_paths: list[str] = Field(default_factory=list)
    evidence_class: EvidenceClass = EvidenceClass.RECOMPUTED
    reasons: list[str] = Field(default_factory=list)
    schema_version: str = SCHEMA_VERSION


class LineageFamily(StrictModel):
    family_id: str
    candidate_ids: list[str] = Field(default_factory=list)
    bundle_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    relation_types: list[str] = Field(default_factory=list)


class BundleLineageAssessment(StrictModel):
    bundle_id: str
    candidate_id: str
    family_ids: list[str] = Field(default_factory=list)
    evaluation_leakage: bool = False
    redundancy_status: str = "unique"
    representative_bundle_id: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
    schema_version: str = "2.2"


class LineageGraphSnapshot(StrictModel):
    snapshot_id: str
    run_id: str
    phase: str
    families: list[LineageFamily] = Field(default_factory=list)
    evaluation_family_ids: list[str] = Field(default_factory=list)
    bundle_assessments: list[BundleLineageAssessment] = Field(
        default_factory=list
    )
    schema_version: str = SCHEMA_VERSION


class CorpusBundleDecision(StrictModel):
    bundle_id: str
    candidate_id: str
    pool: str
    eligible: bool
    reasons: list[str] = Field(default_factory=list)


class CorpusSnapshot(StrictModel):
    snapshot_id: str
    run_id: str
    policy_version: str
    decisions: list[CorpusBundleDecision] = Field(default_factory=list)
    admitted_bundle_ids: list[str] = Field(default_factory=list)
    quarantined_bundle_ids: list[str] = Field(default_factory=list)
    excluded_bundle_ids: list[str] = Field(default_factory=list)
    manifest_hash: str
    approved: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    schema_version: str = SCHEMA_VERSION


class AuditQueueAction(StrEnum):
    """What kind of human action resolves this hold -- distinct from
    ``AuditTask.blocking_category``, which says *what* is wrong. Grounded
    against 04_thinking/agent.xlsx's 04_Audit_Hold sheet's real
    队列动作(queue action) column, whose five real-world values (verify-then-
    archive/defer/exclude/reprocess/partial-archive) are exactly these."""

    VERIFY_THEN_ARCHIVE = "verify_then_archive"
    DEFERRED = "deferred"
    EXCLUDED = "excluded"
    #: Needs reprocessing -- e.g. a re-download or re-run pipeline step --
    #: distinct from merely awaiting more evidence.
    REPROCESS = "reprocess"
    #: One study, some pair-units archived and others not -- the pair-unit
    #: admission granularity Phase 4 introduced made this a real, common case.
    PARTIAL_ARCHIVE = "partial_archive"


class AuditTaskCategoryFamily(StrEnum):
    """Coarse grouping for aggregate stats only -- never a substitute for
    ``AuditTask.blocking_category``, which stays an open string."""

    PAIRING = "pairing"
    MISSING_FILE = "missing_file"
    METADATA = "metadata"
    ASSEMBLY = "assembly"
    DOWNLOAD = "download"
    VALIDATION = "validation"
    OTHER = "other"


class AuditTask(StrictModel):
    """One row of the human audit queue.

    ``blocking_category`` is deliberately an open string, not this model's
    closed enums: 04_Audit_Hold's real data has 46 distinct values for it,
    which is the evidence this dimension must never be closed into a fixed
    vocabulary, unlike ``queue_action``/``category_family`` above.
    """

    task_id: str
    run_id: str
    #: The curation-unit-grained identity this task is about: a
    #: ProposedPairUnit.proposed_unit_id when one exists, else a candidate_id.
    subject_id: str
    bundle_id: str | None = None
    queue_action: AuditQueueAction
    blocking_category: str
    category_family: AuditTaskCategoryFamily = AuditTaskCategoryFamily.OTHER
    audit_method: str = ""
    audit_steps: str = ""
    pass_criteria: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    rationale: str = ""
    created_at: datetime = Field(default_factory=utc_now)
    schema_version: str = SCHEMA_VERSION


def assets_by_id(assets: list[DataAsset]) -> dict[str, DataAsset]:
    return {asset.asset_id: asset for asset in assets}
