"""Deterministic helpers shared by progressive resource pipeline agents."""

from __future__ import annotations

from scdata_agent.application.agents.shared import AgentContext
from scdata_agent.application.collaboration import AgentArtifact, ArtifactKind
from scdata_agent.domain.identifiers import stable_id
from scdata_agent.domain.lifecycle import PointerStatus, ResourcePointer
from scdata_agent.domain.resource_pipeline import (
    PointerBatchStage,
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

