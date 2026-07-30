"""Progressive Asset Scout phases over explicit resource-pointer batches."""

from __future__ import annotations

from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

from scdata_agent.application.agents.shared import AgentContext, need_discovery
from scdata_agent.application.collaboration import AgentName, ArtifactKind, artifact
from scdata_agent.application.resource_pipeline import (
    merge_pointers,
    pointer_batches,
    pointer_key,
)
from scdata_agent.domain.assets import infer_data_role
from scdata_agent.domain.identifiers import stable_id
from scdata_agent.domain.lifecycle import (
    PointerStatus,
    RemoteAssetObservation,
    ResourcePointer,
)
from scdata_agent.domain.models import (
    AccessLevel,
    AssetAvailability,
    AssetLocation,
    DataAsset,
    DataRole,
    DatasetCandidate,
    Identifier,
    TransferProtocol,
)
from scdata_agent.domain.resource_pipeline import (
    AssetInventoryDelta,
    PointerBatchStage,
    ScoutPhase,
)
from scdata_agent.infrastructure.sources.asset_registry import (
    AssetResolverRegistry,
)
from scdata_agent.infrastructure.remote_probe import RemoteAvailabilityProbe


class ProgressiveAssetScoutAgent:
    """Resolve only the pointer delta assigned to one scouting phase."""

    name = AgentName.ASSET_SCOUT

    def __init__(self, phase: ScoutPhase | str) -> None:
        self.phase = ScoutPhase(phase)

    async def run(self, context: AgentContext):
        discovery = need_discovery(context)
        pointers, parent_ids = self._phase_pointers(context)
        by_candidate: dict[str, list[ResourcePointer]] = {}
        for pointer in pointers:
            if pointer.scoutable:
                by_candidate.setdefault(pointer.candidate_id, []).append(
                    pointer
                )

        resolver = AssetResolverRegistry(
            expand_bioproject=bool(
                context.policy.get("expand_bioproject", False)
            ),
            max_assets=int(context.policy.get("max_assets", 500)),
        )
        assets: list[DataAsset] = []
        observations: list[RemoteAssetObservation] = []
        errors: list[str] = []

        for candidate in discovery.candidates:
            candidate_pointers = by_candidate.get(
                candidate.candidate_id, []
            )
            if not candidate_pointers:
                continue
            direct_files = [
                self._direct_file(candidate, pointer)
                for pointer in candidate_pointers
                if pointer.pointer_kind == "file_url"
            ]
            assets.extend(direct_files)
            repository_pointers = [
                pointer
                for pointer in candidate_pointers
                if pointer.pointer_kind == "repository_accession"
            ]
            if repository_pointers:
                scoped_candidate = self._candidate_from_pointers(
                    candidate, repository_pointers
                )
                resolution = await resolver.resolve(scoped_candidate)
                assets.extend(resolution.assets)
                errors.extend(
                    f"{candidate.candidate_id}: {message}"
                    for message in resolution.errors
                )
            unsupported = [
                pointer
                for pointer in candidate_pointers
                if pointer.pointer_kind
                not in {"repository_accession", "file_url"}
            ]
            errors.extend(
                f"{candidate.candidate_id}: unsupported "
                f"{pointer.pointer_kind} pointer {pointer.locator}"
                for pointer in unsupported
            )

        assets = list({item.asset_id: item for item in assets}.values())
        probe = RemoteAvailabilityProbe(
            concurrency=int(context.policy.get("remote_probe_concurrency", 8)),
            timeout_seconds=float(
                context.policy.get("remote_probe_timeout_seconds", 10.0)
            ),
            max_attempts=int(context.policy.get("remote_probe_max_attempts", 2)),
        )
        if bool(context.policy.get("remote_probe_enabled", True)):
            assets, observations = await probe.probe_assets(assets)
        else:
            observations = [
                probe._observation(asset, location)
                for asset in assets
                for location in asset.locations
            ]

        delta = AssetInventoryDelta(
            inventory_id=stable_id(
                "asset_inventory_delta",
                f"{context.run_id}:{self.phase.value}",
            ),
            run_id=context.run_id,
            phase=self.phase,
            consumed_pointer_ids=[
                pointer.pointer_id for pointer in pointers
            ],
            assets=assets,
            observations=observations,
            errors=errors,
        )
        return [
            artifact(
                context.run_id,
                ArtifactKind.ASSET_INVENTORY_DELTA,
                self.name,
                delta.model_dump(mode="json"),
                evidence_ids=[
                    evidence_id
                    for pointer in pointers
                    for evidence_id in pointer.evidence_ids
                ],
                parents=parent_ids,
            )
        ]

    def _phase_pointers(
        self, context: AgentContext
    ) -> tuple[list[ResourcePointer], list[str]]:
        batches = pointer_batches(context)
        direct_stages = {PointerBatchStage.DISCOVERY}
        selected = [
            pointer
            for _, batch in batches
            if (
                batch.stage in direct_stages
                if self.phase == ScoutPhase.DIRECT
                else batch.stage not in direct_stages
            )
            for pointer in batch.pointers
        ]
        if self.phase == ScoutPhase.ENRICHED:
            successful_direct_candidates = {
                asset.candidate_id
                for item in context.artifacts
                if item.kind == ArtifactKind.ASSET_INVENTORY_DELTA
                for delta in [AssetInventoryDelta.model_validate(item.payload)]
                if delta.phase == ScoutPhase.DIRECT
                for asset in delta.assets
            }
            direct_keys = {
                pointer_key(pointer)
                for _, batch in batches
                if batch.stage == PointerBatchStage.DISCOVERY
                for pointer in batch.pointers
                if pointer.candidate_id in successful_direct_candidates
            }
            selected = [
                pointer
                for pointer in selected
                if pointer_key(pointer) not in direct_keys
                or pointer.status
                in {PointerStatus.VALIDATED, PointerStatus.CONFLICTED}
            ]
        parent_ids = [
            item.artifact_id
            for item, batch in batches
            if item is not None
            and (
                batch.stage in direct_stages
                if self.phase == ScoutPhase.DIRECT
                else batch.stage not in direct_stages
            )
        ]
        return merge_pointers(selected), parent_ids

    @staticmethod
    def _candidate_from_pointers(
        candidate: DatasetCandidate,
        pointers: list[ResourcePointer],
    ) -> DatasetCandidate:
        return candidate.model_copy(
            update={
                "identifiers": [
                    Identifier(
                        namespace=pointer.namespace,
                        value=pointer.locator,
                    )
                    for pointer in pointers
                ]
            }
        )

    @staticmethod
    def _direct_file(
        candidate: DatasetCandidate, pointer: ResourcePointer
    ) -> DataAsset:
        parsed = urlparse(pointer.locator)
        file_name = unquote(PurePosixPath(parsed.path).name) or "download"
        role = infer_data_role(file_name) or DataRole.OTHER
        return DataAsset(
            asset_id=stable_id(
                "asset",
                f"{candidate.candidate_id}:direct:{pointer.locator}",
            ),
            candidate_id=candidate.candidate_id,
            role=role,
            file_name=file_name,
            locations=[
                AssetLocation(
                    url=pointer.locator,
                    protocol=(
                        TransferProtocol.FTP
                        if parsed.scheme == "ftp"
                        else TransferProtocol.HTTPS
                    ),
                    access_level=AccessLevel.PUBLIC_DECLARED,
                    availability=AssetAvailability.DECLARED,
                    evidence_ids=pointer.evidence_ids,
                )
            ],
            evidence_ids=pointer.evidence_ids,
        )

