"""Turn a parsed curated manifest into the same contracts discovery produces.

A curated manifest is the other way data enters this system (see
``domain.manifest``): a human has already audited a study down to the file.
``ingest_manifest`` is the pure, synchronous mapping from that audit to
``DatasetCandidate``/``DataAsset``/``SemanticBundle`` -- the shapes every
downstream agent (preflight, lineage, acquisition, transfer) already knows how
to read, so a curated bundle takes the identical path a discovered one does.

The one rule this module exists to enforce in code, not in a comment on the
caller's side: a human writing ``VERIFIED`` in a spreadsheet cell means "I
confirmed this URL resolves," not "I read the licence" and not "I opened the
file." Every asset this module produces is therefore ``DECLARED``/
``PUBLIC_DECLARED`` evidence, never ``OBSERVED``/``PUBLIC_VERIFIED`` -- those
require a network probe or a downloaded byte, which happens later (if at all)
and produces its own, separately-attributed observation.
"""

from __future__ import annotations

from typing import Any

from scdata_agent.application.resource_pipeline import (
    bundle_shape_for,
    pointer_kind,
    summarize_bundle,
)
from scdata_agent.domain.identifiers import stable_id
from scdata_agent.domain.lifecycle import (
    BundleStatus,
    PointerStatus,
    RemoteAssetObservation,
    ResourcePointer,
    SemanticBundle,
)
from scdata_agent.domain.manifest import (
    ManifestAssetSidecar,
    ManifestDocument,
    ManifestIngestion,
    ManifestRow,
    filter_manifest_issues,
    filter_manifest_rows,
)
from scdata_agent.domain.models import (
    AccessLevel,
    AssetAvailability,
    AssetLocation,
    DataAsset,
    DatasetCandidate,
    DepositionRelation,
    DiscoveryRunResult,
    Evidence,
    EvidenceClass,
    QueryPlan,
    ResearchRequirement,
    TransferProtocol,
)

#: Namespaces fed to ``pointer_kind``. A portal row's URL is a landing page,
#: not a fetchable file, so it must not collapse into the same "file_url"
#: kind a direct row gets -- that distinction is what lets a downstream
#: consumer skip portal rows without re-parsing ``url_status``.
_DIRECT_POINTER_NAMESPACE = "direct_file_url"
_PORTAL_POINTER_NAMESPACE = "portal_url"


def ingest_manifest(
    document: ManifestDocument,
    *,
    run_id: str,
    filters: dict[str, list[str]] | None = None,
    policy: dict[str, Any] | None = None,
) -> ManifestIngestion:
    """Map a manifest slice onto discovery's contracts.

    ``filters`` narrows the manifest to the rows a run should act on
    (``--dataset``/``--pair-unit``/``--stage``). ``policy["include_portal"]``
    (default ``False``, mirroring the CLI's ``--direct-only`` default)
    additionally drops rows that only point at a click-through portal page --
    they cannot be transferred automatically, and including them by default
    would just seed every manifest run with dead-end ``manual_review`` bundles.
    """
    filters = filters or {}
    policy = policy or {}
    rows = filter_manifest_rows(
        document.rows,
        filters.get("dataset"),
        filters.get("pair_unit"),
        filters.get("stage"),
    )
    if not policy.get("include_portal", False):
        rows = [row for row in rows if not row.is_manual]
    issues = filter_manifest_issues(document.issues, document.rows, rows)

    assets: list[DataAsset] = []
    observations: list[RemoteAssetObservation] = []
    bundles: list[SemanticBundle] = []
    bundle_summaries = []
    sidecars: list[ManifestAssetSidecar] = []
    evidence: list[Evidence] = []
    pointers: list[ResourcePointer] = []

    rows_by_dataset: dict[str, list[ManifestRow]] = {}
    rows_by_pair_unit: dict[tuple[str, str], list[ManifestRow]] = {}
    for row in rows:
        rows_by_dataset.setdefault(row.dataset_id, []).append(row)
        rows_by_pair_unit.setdefault(row.pair_unit_key, []).append(row)

    candidates = [
        _build_candidate(dataset_id, dataset_rows)
        for dataset_id, dataset_rows in rows_by_dataset.items()
    ]
    candidates_by_id = {item.candidate_id: item for item in candidates}

    assets_by_pair_unit: dict[tuple[str, str], list[DataAsset]] = {}
    for row in rows:
        row_evidence = _build_evidence(document, row)
        evidence.append(row_evidence)
        asset = _build_asset(row, row_evidence)
        assets.append(asset)
        assets_by_pair_unit.setdefault(row.pair_unit_key, []).append(asset)
        observations.append(_build_observation(row, asset, row_evidence))
        pointers.append(_build_pointer(row, row_evidence))

    for pair_unit_key, pair_rows in rows_by_pair_unit.items():
        dataset_id, pair_unit_id = pair_unit_key
        candidate = candidates_by_id[dataset_id]
        pair_assets = assets_by_pair_unit[pair_unit_key]
        shape = bundle_shape_for(candidate, pair_assets, requirement=None)
        bundle = SemanticBundle(
            bundle_id=stable_id("manifest_bundle", f"{dataset_id}::{pair_unit_id}"),
            candidate_id=dataset_id,
            bundle_type=shape.bundle_type,
            asset_ids=[item.asset_id for item in pair_assets],
            required_roles=shape.required_roles,
            missing_roles=shape.missing_roles,
            applicable_schema_ids=shape.applicable_schema_ids,
            satisfied_schema_ids=shape.satisfied_schema_ids,
            observed_roles=shape.observed_roles,
            status=BundleStatus.REMOTE_INVENTORIED,
            evidence_ids=list(
                dict.fromkeys(
                    evidence_id
                    for asset in pair_assets
                    for evidence_id in asset.evidence_ids
                )
            ),
        )
        bundles.append(bundle)
        bundle_summaries.append(summarize_bundle(bundle, pair_assets, candidate, []))
        sidecars.extend(
            _build_sidecar(row, bundle.bundle_id, asset)
            for row, asset in zip(pair_rows, pair_assets, strict=True)
        )

    requirement = ResearchRequirement(question=f"Curated manifest: {document.source_path}")
    discovery = DiscoveryRunResult(
        run_id=run_id,
        requirement=requirement,
        query_plan=QueryPlan(requirement=requirement, queries=[]),
        source_records=[],
        candidates=candidates,
        feasibility_reports=[],
    )

    return ManifestIngestion(
        ingestion_id=stable_id("manifest_ingestion", f"{run_id}:{document.source_sha256}"),
        run_id=run_id,
        source_path=document.source_path,
        source_sha256=document.source_sha256,
        dialect_id=document.dialect_id,
        filters={key: list(value) for key, value in filters.items()},
        requirement=requirement,
        discovery=discovery,
        pointers=pointers,
        assets=assets,
        observations=observations,
        bundles=bundles,
        bundle_summaries=bundle_summaries,
        sidecars=sidecars,
        evidence=evidence,
        issues=issues,
        row_count=len(rows),
        pair_unit_count=len(rows_by_pair_unit),
        bundle_count=len(bundles),
    )


def _build_candidate(dataset_id: str, dataset_rows: list[ManifestRow]) -> DatasetCandidate:
    return DatasetCandidate(
        candidate_id=dataset_id,
        title=f"Curated manifest: {dataset_id}",
        # Left empty rather than inferred from which roles co-occur: claiming
        # an assay from co-occurring RNA and ATAC rows is exactly the
        # overclaim P0.0 had to walk back. An empty assay_profiles list makes
        # `applicable_bundle_schemas_by_layers` offer every schema, so
        # admission is judged on the assets actually present, not on a
        # guessed layer combination.
        assay_profiles=[],
        deposition_relation=DepositionRelation.UNKNOWN,
        observed_data_roles=sorted({row.role for row in dataset_rows}, key=str),
        access_level=AccessLevel.UNKNOWN,
    )


def _build_evidence(document: ManifestDocument, row: ManifestRow) -> Evidence:
    # `VERIFIED` means the curator confirmed the URL resolves; nothing weaker
    # than that earns USER_CORRECTED. The manifest's own PORTAL_RESOLVE rows
    # are the concrete case this guards: the curator listed the row but could
    # not confirm it, so it stays REPOSITORY_REPORTED.
    evidence_class = (
        EvidenceClass.USER_CORRECTED if row.is_curator_verified else EvidenceClass.REPOSITORY_REPORTED
    )
    return Evidence(
        evidence_id=stable_id(
            "manifest_evidence",
            f"{row.dataset_id}::{row.pair_unit_id}::{row.relative_path}",
        ),
        source_name="curated_manifest",
        locator=f"{document.source_path}#row={row.source_row_index}",
        raw_value=row.raw,
        evidence_class=evidence_class,
        snapshot_hash=document.source_sha256,
    )


def _build_asset(row: ManifestRow, row_evidence: Evidence) -> DataAsset:
    location = AssetLocation(
        url=row.url,
        protocol=TransferProtocol.MANUAL if row.is_manual else TransferProtocol.HTTPS,
        access_level=AccessLevel.UNKNOWN if row.is_manual else AccessLevel.PUBLIC_DECLARED,
        size_bytes=None if row.is_manual else row.size_bytes,
        checksum_algorithm=row.checksum_algorithm,
        checksum=row.declared_checksum,
        availability=AssetAvailability.UNKNOWN if row.is_manual else AssetAvailability.DECLARED,
        evidence_ids=[row_evidence.evidence_id],
    )
    return DataAsset(
        asset_id=stable_id(
            "manifest_asset",
            f"{row.dataset_id}::{row.pair_unit_id}::{row.relative_path}",
        ),
        candidate_id=row.dataset_id,
        role=row.role,
        file_name=row.file_name,
        genome_build=row.genome_build,
        license_id=row.license_id,
        license_url=row.license_url,
        locations=[location],
        evidence_ids=[row_evidence.evidence_id],
    )


def _build_observation(
    row: ManifestRow, asset: DataAsset, row_evidence: Evidence
) -> RemoteAssetObservation:
    return RemoteAssetObservation(
        observation_id=stable_id(
            "manifest_observation",
            f"{row.dataset_id}::{row.pair_unit_id}::{row.relative_path}",
        ),
        asset_id=asset.asset_id,
        candidate_id=row.dataset_id,
        access_level=AccessLevel.UNKNOWN if row.is_manual else AccessLevel.PUBLIC_DECLARED,
        availability=AssetAvailability.UNKNOWN if row.is_manual else AssetAvailability.DECLARED,
        size_bytes=None if row.is_manual else row.size_bytes,
        checksum_algorithm=row.checksum_algorithm,
        checksum_declared=row.declared_checksum,
        probe_method="curated_manifest",
        evidence_ids=[row_evidence.evidence_id],
    )


def _build_pointer(row: ManifestRow, row_evidence: Evidence) -> ResourcePointer:
    namespace = _PORTAL_POINTER_NAMESPACE if row.is_manual else _DIRECT_POINTER_NAMESPACE
    return ResourcePointer(
        pointer_id=stable_id(
            "manifest_pointer",
            f"{row.dataset_id}::{row.pair_unit_id}::{row.relative_path}",
        ),
        candidate_id=row.dataset_id,
        namespace=namespace,
        locator=row.url,
        source_name="curated_manifest",
        # REPORTED, not VALIDATED: the manifest reports the URL, but nothing
        # in this module has independently confirmed it resolves. That is the
        # optional remote-probe step's job.
        status=PointerStatus.REPORTED,
        pointer_kind=pointer_kind(namespace, row.url),
        origin_stage="curated_manifest",
        evidence_ids=[row_evidence.evidence_id],
    )


def _build_sidecar(row: ManifestRow, bundle_id: str, asset: DataAsset) -> ManifestAssetSidecar:
    return ManifestAssetSidecar(
        asset_id=asset.asset_id,
        bundle_id=bundle_id,
        dataset_id=row.dataset_id,
        pair_unit_id=row.pair_unit_id,
        source_row_index=row.source_row_index,
        relative_path=row.relative_path,
        native_file_role=row.file_role,
        pairing_key=row.pairing_key,
        post_download_action=row.post_download_action,
        checksum_policy=row.checksum_policy,
        stage=row.stage,
    )
