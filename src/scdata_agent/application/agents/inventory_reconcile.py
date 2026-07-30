"""Merge progressive Asset Scout deltas into one lifecycle-bearing inventory."""

from __future__ import annotations

from collections import Counter
from pathlib import PurePosixPath

from scdata_agent.application.agents.shared import (
    AgentContext,
    candidate_modalities,
    need_discovery,
)
from scdata_agent.application.collaboration import AgentName, ArtifactKind, artifact
from scdata_agent.application.resource_pipeline import (
    merge_pointers,
    pointer_batches,
)
from scdata_agent.domain.identifiers import stable_id
from scdata_agent.domain.lifecycle import (
    BundleStatus,
    RemoteAssetObservation,
    SemanticBundle,
)
from scdata_agent.domain.models import (
    AccessLevel, AssetAvailability, DataAsset, DataRole,
)
from scdata_agent.domain.requirements import applicable_bundle_schemas
from scdata_agent.domain.resource_pipeline import (
    AssetInventoryDelta,
    BundleAccessStatus,
    RemoteBundleInventorySummary,
)


class InventoryReconcileAgent:
    name = AgentName.ASSET_SCOUT

    async def run(self, context: AgentContext):
        discovery = need_discovery(context)
        delta_artifacts = [
            item
            for item in context.artifacts
            if item.kind == ArtifactKind.ASSET_INVENTORY_DELTA
        ]
        deltas = [
            AssetInventoryDelta.model_validate(item.payload)
            for item in delta_artifacts
        ]
        assets = self._merge_assets(
            [asset for delta in deltas for asset in delta.assets]
        )
        observations = list(
            {
                item.observation_id: item
                for delta in deltas
                for item in delta.observations
            }.values()
        )
        pointers = merge_pointers(
            [
                pointer
                for _, batch in pointer_batches(context)
                for pointer in batch.pointers
            ]
        )
        evidence_by_candidate: dict[str, list[str]] = {}
        for pointer in pointers:
            evidence_by_candidate.setdefault(
                pointer.candidate_id, []
            ).extend(pointer.evidence_ids)

        assets_by_candidate: dict[str, list[DataAsset]] = {}
        for asset in assets:
            assets_by_candidate.setdefault(
                asset.candidate_id, []
            ).append(asset)
        bundles: list[SemanticBundle] = []
        for candidate in discovery.candidates:
            candidate_assets = assets_by_candidate.get(
                candidate.candidate_id, []
            )
            if not candidate_assets:
                continue
            observed_roles = {item.role for item in candidate_assets}
            # Judge the bundle against every shape it could legitimately take,
            # not against the union of every modality mentioned in the question.
            schemas = applicable_bundle_schemas(
                candidate_modalities(candidate, context.requirement)
            )
            satisfied = [
                schema.schema_id
                for schema in schemas
                if schema.is_satisfied_by(observed_roles)
            ]
            # `required_roles`/`missing_roles` stay populated for readers that
            # predate schemas; they describe the closest schema, not the union.
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
            bundles.append(
                SemanticBundle(
                    bundle_id=stable_id(
                        "semantic_bundle",
                        f"{candidate.candidate_id}:"
                        f"{','.join(sorted(item.asset_id for item in candidate_assets))}",
                    ),
                    candidate_id=candidate.candidate_id,
                    bundle_type=(
                        "processed_training_input"
                        if observed_roles - {DataRole.RAW_READS}
                        else "raw_reads"
                    ),
                    asset_ids=[
                        item.asset_id for item in candidate_assets
                    ],
                    required_roles=required_roles,
                    missing_roles=missing_roles,
                    applicable_schema_ids=[item.schema_id for item in schemas],
                    satisfied_schema_ids=satisfied,
                    observed_roles=sorted(observed_roles, key=str),
                    status=BundleStatus.REMOTE_INVENTORIED,
                    evidence_ids=list(
                        dict.fromkeys(
                            [
                                *evidence_by_candidate.get(
                                    candidate.candidate_id, []
                                ),
                                *[
                                    evidence_id
                                    for asset in candidate_assets
                                    for evidence_id in asset.evidence_ids
                                ],
                            ]
                        )
                    ),
                )
            )

        candidate_by_id = {
            item.candidate_id: item for item in discovery.candidates
        }
        # Truncation is a property of the whole run's scouting, so union the
        # phases: a listing cut short in the direct phase stays partial even if
        # the enriched phase completed.
        truncated_by_candidate: dict[str, list[str]] = {}
        for delta in deltas:
            for candidate_id, names in delta.truncated_by_candidate.items():
                truncated_by_candidate.setdefault(candidate_id, []).extend(names)
        summaries = [
            self._summarize(
                bundle, assets_by_candidate[bundle.candidate_id],
                candidate_by_id[bundle.candidate_id],
                list(dict.fromkeys(
                    truncated_by_candidate.get(bundle.candidate_id, [])
                )),
            )
            for bundle in bundles
        ]

        if context.catalog is not None:
            for item in assets:
                context.catalog.append(
                    record_id=item.asset_id,
                    record_type="data_asset",
                    run_id=context.run_id,
                    value=item,
                )
            for item in observations:
                context.catalog.append(
                    record_id=item.observation_id,
                    record_type="remote_asset_observation",
                    run_id=context.run_id,
                    value=item,
                )
            for item in bundles:
                context.catalog.append(
                    record_id=item.bundle_id,
                    record_type="semantic_bundle",
                    run_id=context.run_id,
                    value=item,
                )
                context.catalog.transition_bundle(
                    item.bundle_id,
                    BundleStatus.REMOTE_INVENTORIED,
                    run_id=context.run_id,
                )
            for item in summaries:
                context.catalog.append(
                    record_id=item.summary_id,
                    record_type="remote_bundle_inventory_summary",
                    run_id=context.run_id,
                    value=item,
                )

        escalation = [
            item
            for item in context.artifacts
            if item.kind == ArtifactKind.EVIDENCE_ESCALATION
        ]
        return [
            artifact(
                context.run_id,
                ArtifactKind.ASSET_INVENTORY,
                self.name,
                {
                    "pointers": [
                        item.model_dump(mode="json")
                        for item in pointers
                    ],
                    "assets": [
                        item.model_dump(mode="json") for item in assets
                    ],
                    "observations": [
                        item.model_dump(mode="json")
                        for item in observations
                    ],
                    "bundles": [
                        item.model_dump(mode="json")
                        for item in bundles
                    ],
                    "bundle_summaries": [
                        item.model_dump(mode="json")
                        for item in summaries
                    ],
                    "errors": list(
                        dict.fromkeys(
                            error
                            for delta in deltas
                            for error in delta.errors
                        )
                    ),
                    "delta_ids": [
                        item.inventory_id for item in deltas
                    ],
                    "bulk_download_performed": False,
                },
                parents=[
                    *[item.artifact_id for item in delta_artifacts],
                    *[item.artifact_id for item in escalation[-1:]],
                ],
            )
        ]

    @staticmethod
    def _summarize(bundle, assets, candidate, truncated_resolvers=None):
        locations = [location for asset in assets for location in asset.locations[:1]]
        verified = sum(
            location.availability == AssetAvailability.OBSERVED
            and location.access_level == AccessLevel.PUBLIC_VERIFIED
            for location in locations
        )
        declared = sum(location.availability == AssetAvailability.DECLARED for location in locations)
        unavailable = sum(location.availability == AssetAvailability.UNAVAILABLE for location in locations)
        auth = sum(location.access_level in {
            AccessLevel.AUTH_REQUIRED, AccessLevel.CONTROLLED, AccessLevel.EMBARGOED,
        } for location in locations)
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
            total_bytes=(
                known_bytes if unknown_sizes == 0 and not truncated else None
            ),
            unknown_size_asset_count=unknown_sizes,
            inventory_truncated=bool(truncated),
            truncated_resolvers=truncated,
            role_counts=dict(Counter(asset.role.value for asset in assets)),
            file_type_counts=dict(Counter(InventoryReconcileAgent._file_type(asset.file_name) for asset in assets)),
            media_type_counts=dict(Counter(asset.media_type or "unknown" for asset in assets)),
            compression_counts=dict(Counter(asset.compression or "none" for asset in assets)),
            license_ids=sorted({asset.license_id for asset in assets if asset.license_id}),
            reported_modalities=sorted({modality.value for modality in candidate.modalities}),
            genome_builds=sorted({asset.genome_build for asset in assets if asset.genome_build}),
        )

    @staticmethod
    def _file_type(file_name: str) -> str:
        suffixes = PurePosixPath(file_name).suffixes
        if len(suffixes) >= 2 and suffixes[-1].lower() in {".gz", ".bgz", ".bz2"}:
            return "".join(suffixes[-2:]).lower()
        return suffixes[-1].lower() if suffixes else "unknown"

    @staticmethod
    def _merge_assets(assets: list[DataAsset]) -> list[DataAsset]:
        merged: dict[str, DataAsset] = {}
        for asset in assets:
            existing = merged.get(asset.asset_id)
            if existing is None:
                merged[asset.asset_id] = asset
                continue
            locations = {
                (
                    location.url,
                    location.checksum_algorithm,
                    location.checksum,
                ): location
                for location in [
                    *existing.locations,
                    *asset.locations,
                ]
            }
            merged[asset.asset_id] = existing.model_copy(
                update={
                    "locations": list(locations.values()),
                    "evidence_ids": list(
                        dict.fromkeys(
                            [
                                *existing.evidence_ids,
                                *asset.evidence_ids,
                            ]
                        )
                    ),
                }
            )
        return sorted(
            merged.values(),
            key=lambda item: (item.candidate_id, item.asset_id),
        )

