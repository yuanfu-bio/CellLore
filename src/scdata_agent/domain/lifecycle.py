"""Versioned contracts for evidence maturity and bundle materialization."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from scdata_agent.domain.models import (
    AccessLevel,
    DataAsset,
    AssetAvailability,
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
    RESTRICTED = "restricted"
    UNKNOWN = "unknown"


class BundleStatus(StrEnum):
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
    BundleStatus.PREFLIGHT_READY: {BundleStatus.PROPOSED, BundleStatus.REJECTED},
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
    schema_version: str = SCHEMA_VERSION

    @model_validator(mode="after")
    def require_object_or_value(self) -> "EvidenceClaim":
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
    bundle_type: str
    asset_ids: list[str] = Field(default_factory=list)
    required_roles: list[DataRole] = Field(default_factory=list)
    optional_roles: list[DataRole] = Field(default_factory=list)
    missing_roles: list[DataRole] = Field(default_factory=list)
    status: BundleStatus = BundleStatus.REMOTE_INVENTORIED
    evidence_ids: list[str] = Field(default_factory=list)
    schema_version: str = SCHEMA_VERSION

    @property
    def complete(self) -> bool:
        return bool(self.asset_ids) and not self.missing_roles


class PreflightBundleReport(StrictModel):
    report_id: str
    bundle_id: str
    candidate_id: str
    dimensions: dict[str, GateStatus]
    rationale: dict[str, str] = Field(default_factory=dict)
    license_status: LicenseStatus = LicenseStatus.UNKNOWN
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
    def require_preflight_decision(self) -> "PreflightBundleReport":
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


def assets_by_id(assets: list[DataAsset]) -> dict[str, DataAsset]:
    return {asset.asset_id: asset for asset in assets}
