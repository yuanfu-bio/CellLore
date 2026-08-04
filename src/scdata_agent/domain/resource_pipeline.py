"""Typed v2.2 contracts for progressive resource-to-asset resolution."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from scdata_agent.domain.lifecycle import RemoteAssetObservation, ResourcePointer
from scdata_agent.domain.models import DataAsset, StrictModel

RESOURCE_PIPELINE_SCHEMA_VERSION = "2.2"


class PointerBatchStage(StrEnum):
    DISCOVERY = "discovery"
    EVIDENCE_METADATA = "evidence_metadata"
    EVIDENCE_FULLTEXT = "evidence_fulltext"
    LEGACY_EVIDENCE_GRAPH = "legacy_evidence_graph"
    # A curated manifest is the other way a run's pointers originate: a human
    # audit rather than a discovery adapter.
    CURATED_MANIFEST = "curated_manifest"


class ScoutPhase(StrEnum):
    DIRECT = "direct"
    ENRICHED = "enriched"


class BundleAccessStatus(StrEnum):
    VERIFIED = "verified"
    PARTIAL = "partial"
    DECLARED_ONLY = "declared_only"
    AUTH_REQUIRED = "auth_required"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class RemoteBundleInventorySummary(StrictModel):
    summary_id: str
    bundle_id: str
    candidate_id: str
    access_status: BundleAccessStatus
    asset_count: int = Field(ge=0)
    verified_asset_count: int = Field(default=0, ge=0)
    declared_asset_count: int = Field(default=0, ge=0)
    unavailable_asset_count: int = Field(default=0, ge=0)
    unknown_asset_count: int = Field(default=0, ge=0)
    auth_required_asset_count: int = Field(default=0, ge=0)
    known_bytes: int = Field(default=0, ge=0)
    total_bytes: int | None = Field(default=None, ge=0)
    unknown_size_asset_count: int = Field(default=0, ge=0)
    # True when some adapter's listing was cut short: the asset set is a subset
    # of what the repository holds, so total_bytes is withheld rather than
    # reported as a figure that is certain to be too low.
    inventory_truncated: bool = False
    truncated_resolvers: list[str] = Field(default_factory=list)
    role_counts: dict[str, int] = Field(default_factory=dict)
    file_type_counts: dict[str, int] = Field(default_factory=dict)
    media_type_counts: dict[str, int] = Field(default_factory=dict)
    compression_counts: dict[str, int] = Field(default_factory=dict)
    license_ids: list[str] = Field(default_factory=list)
    reported_modalities: list[str] = Field(default_factory=list)
    genome_builds: list[str] = Field(default_factory=list)
    schema_version: str = RESOURCE_PIPELINE_SCHEMA_VERSION


class ResourcePointerBatch(StrictModel):
    batch_id: str
    run_id: str
    stage: PointerBatchStage
    pointers: list[ResourcePointer] = Field(default_factory=list)
    parent_batch_ids: list[str] = Field(default_factory=list)
    schema_version: str = RESOURCE_PIPELINE_SCHEMA_VERSION


class AssetInventoryDelta(StrictModel):
    inventory_id: str
    run_id: str
    phase: ScoutPhase
    consumed_pointer_ids: list[str] = Field(default_factory=list)
    assets: list[DataAsset] = Field(default_factory=list)
    observations: list[RemoteAssetObservation] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    # Candidate ID -> adapters whose listing was cut short by a budget. Kept
    # per candidate because completeness is judged per bundle downstream.
    truncated_by_candidate: dict[str, list[str]] = Field(default_factory=dict)
    schema_version: str = RESOURCE_PIPELINE_SCHEMA_VERSION


class CandidateEvidenceEscalation(StrictModel):
    candidate_id: str
    requires_fulltext: bool = False
    requires_enriched_scout: bool = False
    unresolved_fields: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class EvidenceEscalationReport(StrictModel):
    report_id: str
    run_id: str
    candidates: list[CandidateEvidenceEscalation] = Field(default_factory=list)
    fulltext_candidate_ids: list[str] = Field(default_factory=list)
    enriched_scout_candidate_ids: list[str] = Field(default_factory=list)
    schema_version: str = RESOURCE_PIPELINE_SCHEMA_VERSION

