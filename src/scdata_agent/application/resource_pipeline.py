"""Deterministic helpers shared by progressive resource pipeline agents."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import PurePosixPath

from scdata_agent.application.agents.shared import AgentContext, candidate_modalities
from scdata_agent.application.collaboration import AgentArtifact, ArtifactKind
from scdata_agent.domain.identifiers import stable_id
from scdata_agent.domain.lifecycle import PointerStatus, ResourcePointer, SemanticBundle
from scdata_agent.domain.models import (
    AccessLevel,
    AssetAvailability,
    DataAsset,
    DataRole,
    DatasetCandidate,
    ResearchRequirement,
)
from scdata_agent.domain.requirements import applicable_bundle_schemas
from scdata_agent.domain.resource_pipeline import (
    BundleAccessStatus,
    PointerBatchStage,
    RemoteBundleInventorySummary,
    ResourcePointerBatch,
)

_POINTER_STATUS_PRIORITY = {
    PointerStatus.INFERRED: 0,
    PointerStatus.REPORTED: 1,
    PointerStatus.VALIDATED: 2,
    PointerStatus.CONFLICTED: 3,
}


def pointer_key(pointer: ResourcePointer) -> str:
    return (
        f"{pointer.candidate_id.casefold()}::"
        f"{pointer.namespace.casefold()}::{pointer.locator.strip().upper()}"
    )


def pointer_kind(namespace: str, locator: str) -> str:
    normalized = namespace.casefold()
    if normalized in {"direct_file_url", "file_url"}:
        return "file_url"
    if normalized in {"manifest_url", "file_manifest_url"}:
        return "manifest_url"
    if locator.startswith(("http://", "https://", "ftp://")):
        return "record_url"
    return "repository_accession"


def merge_pointers(pointers: list[ResourcePointer]) -> list[ResourcePointer]:
    merged: dict[str, ResourcePointer] = {}
    for pointer in pointers:
        key = pointer_key(pointer)
        existing = merged.get(key)
        if existing is None:
            merged[key] = pointer
            continue
        winner, other = (
            (pointer, existing)
            if _POINTER_STATUS_PRIORITY[pointer.status]
            >= _POINTER_STATUS_PRIORITY[existing.status]
            else (existing, pointer)
        )
        merged[key] = winner.model_copy(
            update={
                "evidence_ids": list(
                    dict.fromkeys(
                        [*winner.evidence_ids, *other.evidence_ids]
                    )
                )
            }
        )
    return sorted(
        merged.values(),
        key=lambda item: (
            item.candidate_id,
            item.namespace,
            item.locator,
        ),
    )


@dataclass(frozen=True)
class BundleShape:
    """The role-schema judgment for one candidate's observed assets.

    Shared by discovery-based inventory reconcile and curated-manifest
    ingestion so a change to how schema satisfaction is judged applies to
    both paths identically rather than risking two implementations drifting
    apart.
    """

    bundle_type: str
    required_roles: list[DataRole]
    missing_roles: list[DataRole]
    applicable_schema_ids: list[str]
    satisfied_schema_ids: list[str]
    observed_roles: list[DataRole]


def bundle_shape_for(
    candidate: DatasetCandidate,
    candidate_assets: list[DataAsset],
    requirement: ResearchRequirement | None,
) -> BundleShape:
    """Judge one candidate's assets against every bundle shape it could take.

    Judging against the union of every modality mentioned in the question
    would fail a standalone scATAC study for lacking an RNA matrix it was
    never claimed to have; ``applicable_bundle_schemas`` restricts the
    comparison to shapes the candidate could plausibly satisfy.
    """
    observed_roles = {item.role for item in candidate_assets}
    schemas = applicable_bundle_schemas(candidate_modalities(candidate, requirement))
    satisfied = [
        schema.schema_id for schema in schemas if schema.is_satisfied_by(observed_roles)
    ]
    # `required_roles`/`missing_roles` stay populated for readers that predate
    # schemas; they describe the closest schema, not the union of all of them.
    closest = min(
        schemas,
        key=lambda item: len(item.unmet_groups(observed_roles)),
        default=None,
    )
    required_roles = (
        [group[0] for group in closest.required_role_groups]
        if closest is not None
        else [DataRole.RAW_READS]
    )
    missing_roles = (
        [group[0] for group in closest.unmet_groups(observed_roles)]
        if closest is not None
        else [DataRole.RAW_READS]
    )
    return BundleShape(
        bundle_type=(
            "processed_training_input"
            if observed_roles - {DataRole.RAW_READS}
            else "raw_reads"
        ),
        required_roles=required_roles,
        missing_roles=missing_roles,
        applicable_schema_ids=[item.schema_id for item in schemas],
        satisfied_schema_ids=satisfied,
        observed_roles=sorted(observed_roles, key=str),
    )


def asset_file_type(file_name: str) -> str:
    suffixes = PurePosixPath(file_name).suffixes
    if len(suffixes) >= 2 and suffixes[-1].lower() in {".gz", ".bgz", ".bz2"}:
        return "".join(suffixes[-2:]).lower()
    return suffixes[-1].lower() if suffixes else "unknown"


def summarize_bundle(
    bundle: SemanticBundle,
    assets: list[DataAsset],
    candidate: DatasetCandidate,
    truncated_resolvers: list[str] | None = None,
) -> RemoteBundleInventorySummary:
    """Describe what is remotely known about one bundle, without overclaiming.

    Shared by the discovery path and by curated-manifest ingestion so both
    produce the same summary, and so a change to how access status or size is
    judged cannot drift between them.
    """
    locations = [location for asset in assets for location in asset.locations[:1]]
    verified = sum(
        location.availability == AssetAvailability.OBSERVED
        and location.access_level == AccessLevel.PUBLIC_VERIFIED
        for location in locations
    )
    declared = sum(location.availability == AssetAvailability.DECLARED for location in locations)
    unavailable = sum(
        location.availability == AssetAvailability.UNAVAILABLE for location in locations
    )
    auth = sum(
        location.access_level
        in {
            AccessLevel.AUTH_REQUIRED,
            AccessLevel.CONTROLLED,
            AccessLevel.EMBARGOED,
        }
        for location in locations
    )
    unknown = max(0, len(assets) - verified - declared - unavailable - auth)
    if auth:
        status = BundleAccessStatus.AUTH_REQUIRED
    elif assets and unavailable == len(assets):
        status = BundleAccessStatus.UNAVAILABLE
    elif assets and verified == len(assets):
        status = BundleAccessStatus.VERIFIED
    elif verified or unavailable:
        status = BundleAccessStatus.PARTIAL
    elif assets and declared == len(assets):
        status = BundleAccessStatus.DECLARED_ONLY
    else:
        status = BundleAccessStatus.UNKNOWN
    sizes = [location.size_bytes for location in locations]
    known_bytes = sum(size for size in sizes if size is not None)
    unknown_sizes = len(assets) - sum(size is not None for size in sizes)
    truncated = list(truncated_resolvers or [])
    return RemoteBundleInventorySummary(
        summary_id=stable_id("remote_bundle_summary", bundle.bundle_id),
        bundle_id=bundle.bundle_id,
        candidate_id=bundle.candidate_id,
        access_status=status,
        asset_count=len(assets),
        verified_asset_count=verified,
        declared_asset_count=declared,
        unavailable_asset_count=unavailable,
        unknown_asset_count=unknown,
        auth_required_asset_count=auth,
        known_bytes=known_bytes,
        # Withheld when the listing was truncated: summing a subset would
        # report a figure guaranteed to be too low as though it were the
        # bundle's size. known_bytes still carries the lower bound.
        total_bytes=(known_bytes if unknown_sizes == 0 and not truncated else None),
        unknown_size_asset_count=unknown_sizes,
        inventory_truncated=bool(truncated),
        truncated_resolvers=truncated,
        role_counts=dict(Counter(asset.role.value for asset in assets)),
        file_type_counts=dict(Counter(asset_file_type(asset.file_name) for asset in assets)),
        media_type_counts=dict(Counter(asset.media_type or "unknown" for asset in assets)),
        compression_counts=dict(Counter(asset.compression or "none" for asset in assets)),
        license_ids=sorted({asset.license_id for asset in assets if asset.license_id}),
        reported_modalities=sorted({modality.value for modality in candidate.modalities}),
        genome_builds=sorted({asset.genome_build for asset in assets if asset.genome_build}),
    )


def build_discovery_pointer_batch(
    context: AgentContext,
    discovery_artifact: AgentArtifact,
) -> ResourcePointerBatch:
    if context.discovery is None:
        raise RuntimeError("Discovery is required to build resource pointers.")
    records = {
        record.record_id: record for record in context.discovery.source_records
    }
    pointers: list[ResourcePointer] = []
    for candidate in context.discovery.candidates:
        for identifier in candidate.identifiers:
            matching_record = next(
                (
                    records[record_id]
                    for record_id in candidate.source_record_ids
                    if record_id in records
                    and any(
                        item.canonical == identifier.canonical
                        for item in records[record_id].identifiers
                    )
                ),
                None,
            )
            evidence_ids = (
                [
                    evidence.evidence_id
                    for evidence in matching_record.evidence
                ]
                if matching_record is not None
                else [
                    evidence.evidence_id
                    for evidence in candidate.evidence
                ]
            )
            pointers.append(
                ResourcePointer(
                    pointer_id=stable_id(
                        "pointer",
                        f"{candidate.candidate_id}:{identifier.canonical}",
                    ),
                    candidate_id=candidate.candidate_id,
                    namespace=identifier.namespace,
                    locator=identifier.value,
                    source_name=(
                        matching_record.source_name
                        if matching_record is not None
                        else "resource_discovery"
                    ),
                    status=PointerStatus.REPORTED,
                    pointer_kind=pointer_kind(
                        identifier.namespace, identifier.value
                    ),
                    origin_stage=PointerBatchStage.DISCOVERY.value,
                    source_record_id=(
                        matching_record.record_id
                        if matching_record is not None
                        else None
                    ),
                    origin_artifact_id=discovery_artifact.artifact_id,
                    evidence_ids=evidence_ids,
                    schema_version="2.1",
                )
            )
    return ResourcePointerBatch(
        batch_id=stable_id(
            "pointer_batch", f"{context.run_id}:discovery"
        ),
        run_id=context.run_id,
        stage=PointerBatchStage.DISCOVERY,
        pointers=merge_pointers(pointers),
    )


def pointer_batches(
    context: AgentContext,
) -> list[tuple[AgentArtifact | None, ResourcePointerBatch]]:
    batches = [
        (
            item,
            ResourcePointerBatch.model_validate(item.payload),
        )
        for item in context.artifacts
        if item.kind == ArtifactKind.RESOURCE_POINTERS
    ]
    if batches:
        return batches

    # Compatibility for runs paused before v2.1.
    legacy: list[ResourcePointer] = []
    parent: AgentArtifact | None = None
    for item in context.artifacts:
        if item.kind == ArtifactKind.EVIDENCE_GRAPH:
            parent = item
            legacy.extend(
                ResourcePointer.model_validate(raw)
                for raw in item.payload.get("resource_pointers", [])
            )
    if not legacy:
        return []
    return [
        (
            parent,
            ResourcePointerBatch(
                batch_id=stable_id(
                    "pointer_batch", f"{context.run_id}:legacy"
                ),
                run_id=context.run_id,
                stage=PointerBatchStage.LEGACY_EVIDENCE_GRAPH,
                pointers=merge_pointers(legacy),
            ),
        )
    ]

