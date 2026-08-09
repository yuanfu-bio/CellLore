"""Deterministic helpers shared by progressive resource pipeline agents."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import PurePosixPath

from scdata_agent.application.agents.shared import AgentContext, candidate_layers
from scdata_agent.application.collaboration import AgentArtifact, ArtifactKind
from scdata_agent.domain.identifiers import stable_id
from scdata_agent.domain.lifecycle import (
    EvidenceClaim,
    PointerStatus,
    ResourcePointer,
    SemanticBundle,
)
from scdata_agent.domain.models import (
    AccessLevel,
    AssetAvailability,
    DataAsset,
    DataRole,
    DatasetCandidate,
    EvidenceClass,
    GateStatus,
    ResearchRequirement,
    best_pairing_level,
)
from scdata_agent.domain.requirements import applicable_bundle_schemas_by_layers
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

    Judging against the union of every layer mentioned in the question would
    fail a standalone scATAC study for lacking an RNA matrix it was never
    claimed to have; ``applicable_bundle_schemas_by_layers`` restricts the
    comparison to shapes the candidate could plausibly satisfy, using the
    layers it actually declares plus its observed pairing relationship.
    """
    observed_roles = {item.role for item in candidate_assets}
    observed_relationship = best_pairing_level(candidate.assay_profiles)
    schemas = applicable_bundle_schemas_by_layers(
        candidate_layers(candidate, requirement), observed_relationship
    )
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


def omics_layer_claims(
    candidate: DatasetCandidate, requirement: ResearchRequirement | None = None
) -> list[EvidenceClaim]:
    """One ``has_omics_layer:<layer>`` claim per distinct layer this
    candidate's AssayProfiles declare.

    REPOSITORY_REPORTED regardless of whether the underlying text came from
    the repository record or the article: ``infer_assay_profiles`` mixes both
    without tracking which, and REPOSITORY_REPORTED/ARTICLE_REPORTED share
    the same evidence-class rank, so collapsing to one is not a
    floor-weakening simplification.

    Narrowed by ``requirement.required_omics_layers`` the same way
    ``candidate_layers`` narrows (only narrows, never adds a layer the
    candidate never claimed) -- callers on the discovery path pass the same
    requirement `bundle_shape_for` already uses.

    Discovery-path only. Manifest-ingested candidates declare
    ``assay_profiles=[]`` by design (see manifest_ingestion.py), so this
    always returns an empty list for them -- callers on that path must not
    gate admission on this predicate at all, not treat the empty list as a
    failure to fix.
    """
    layers = candidate_layers(candidate, requirement)
    claims: list[EvidenceClaim] = []
    for layer in sorted(layers, key=str):
        profiles = [item for item in candidate.assay_profiles if layer in item.omics_layers]
        if not profiles:
            continue
        claims.append(
            EvidenceClaim(
                claim_id=stable_id(
                    "claim", f"{candidate.candidate_id}:has_omics_layer:{layer.value}"
                ),
                subject_id=candidate.candidate_id,
                predicate=f"has_omics_layer:{layer.value}",
                object_id=layer.value,
                value=True,
                evidence_ids=list(
                    dict.fromkeys(eid for item in profiles for eid in item.evidence_ids)
                ),
                evidence_class=EvidenceClass.REPOSITORY_REPORTED,
                confidence=max(item.confidence for item in profiles),
                rationale=(
                    f"The {layer.value} layer was observed in repository/article "
                    "assay-profile inference."
                ),
            )
        )
    return claims


def resolved_assembly_claim(
    bundle: SemanticBundle, summary: RemoteBundleInventorySummary | None
) -> EvidenceClaim | None:
    """One ``resolved_assembly`` claim from a bundle's own assets' declared
    genome build, or None when nothing was declared.

    Bundle-scoped, not candidate-scoped: genome_build lives on DataAsset, and
    different pair-units of the same candidate can legitimately carry
    different builds (or a real inconsistency the genome_build_consistency
    validator exists to catch) -- collapsing to one candidate-level claim
    would hide that.
    """
    if summary is None or not summary.genome_builds:
        return None
    return EvidenceClaim(
        claim_id=stable_id("claim", f"{bundle.bundle_id}:resolved_assembly"),
        subject_id=bundle.bundle_id,
        predicate="resolved_assembly",
        # The full list, not just the first entry -- a bundle whose assets
        # disagree on assembly is a real ambiguity worth surfacing, not
        # something to silently resolve by picking one.
        value=sorted(summary.genome_builds),
        evidence_ids=list(bundle.evidence_ids),
        evidence_class=EvidenceClass.REPOSITORY_REPORTED,
        rationale=(
            "Genome build declared on the bundle's assets; not yet "
            "cross-checked against file headers."
        ),
    )


def barcode_overlap_pairing_claim(
    pair_unit_id: str,
    declared_claim: EvidenceClaim | None,
    qc_metrics: dict[str, object],
    qc_checks: dict[str, GateStatus],
) -> EvidenceClaim | None:
    """A ``has_pair_relationship`` claim recomputed from the materialized
    RNA/ATAC barcode overlap (``infrastructure.qc_metrics.compute_role_metrics``),
    for the CORPUS_READY gate's stronger acceptable-evidence-class bar (Phase
    5B: see ``paired_atac_rna_human``'s ``acceptable_for_corpus_ready`` set,
    which requires FILE_OBSERVED/RECOMPUTED rather than declared evidence).

    Barcode overlap alone cannot distinguish same_cell from same_nucleus --
    it only confirms the RNA and ATAC files observed for this pair unit share
    a barcode space. It is therefore confirmatory evidence for whichever
    PairingLevel was already declared for this pair unit, not an independent
    judgment of level: with no prior ``declared_claim`` to carry the value
    forward from, there is nothing to confirm and no claim is produced.
    """
    if declared_claim is None:
        return None
    if qc_checks.get("barcode_linkage") != GateStatus.PASS:
        return None
    return EvidenceClaim(
        claim_id=stable_id(
            "claim", f"{pair_unit_id}:has_pair_relationship:barcode_overlap"
        ),
        subject_id=pair_unit_id,
        predicate="has_pair_relationship",
        value=declared_claim.value,
        evidence_ids=list(declared_claim.evidence_ids),
        evidence_class=EvidenceClass.RECOMPUTED,
        confidence=float(qc_metrics.get("frac_rna_barcodes_matched", 1.0)),
        rationale=(
            "RNA/ATAC barcode sets materially overlap "
            f"({qc_metrics.get('n_barcode_intersection')} of "
            f"{qc_metrics.get('n_rna_barcodes')} RNA barcodes matched); "
            "confirms the declared pairing relationship rather than "
            "establishing its level independently."
        ),
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
        reported_layers=sorted(
            {layer.value for profile in candidate.assay_profiles for layer in profile.omics_layers}
        ),
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

