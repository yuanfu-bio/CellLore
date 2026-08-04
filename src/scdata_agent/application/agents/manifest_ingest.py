"""Specialist agent wrapping curated-manifest ingestion.

Reads a manifest from disk, maps it onto discovery's contracts (the pure work
happens in ``application.manifest_ingestion``), optionally upgrades declared
facts to independently-observed ones with a metadata-only network probe, and
emits the same artifact shapes ``resource_discovery``/``inventory_reconcile``
would -- so everything downstream of this node cannot tell whether a bundle
was discovered or curated.
"""

from __future__ import annotations

from scdata_agent.application.agents.shared import AgentContext
from scdata_agent.application.collaboration import AgentName, ArtifactKind, artifact
from scdata_agent.application.manifest_ingestion import ingest_manifest
from scdata_agent.domain.identifiers import stable_id
from scdata_agent.domain.lifecycle import BundleStatus
from scdata_agent.domain.manifest import ManifestIngestion
from scdata_agent.domain.models import TransferProtocol
from scdata_agent.domain.resource_pipeline import PointerBatchStage, ResourcePointerBatch
from scdata_agent.infrastructure.manifest_reader import read_manifest
from scdata_agent.infrastructure.remote_probe import RemoteAvailabilityProbe


class ManifestIngestAgent:
    name = AgentName.MANIFEST_INGEST

    async def run(self, context: AgentContext):
        manifest_path = context.policy["manifest_path"]
        document = read_manifest(manifest_path, context.policy.get("manifest_dialect"))
        result = ingest_manifest(
            document,
            run_id=context.run_id,
            filters=context.policy.get("manifest_filters") or {},
            policy=context.policy,
        )

        assets, observations = result.assets, list(result.observations)
        if bool(context.policy.get("remote_probe_enabled", True)):
            assets, observations = await self._probe(context, result, assets, observations)

        context.requirement = result.requirement
        context.discovery = result.discovery

        if context.catalog is not None:
            self._record(context, result, assets, observations)

        pointer_batch = ResourcePointerBatch(
            batch_id=stable_id("pointer_batch", f"{context.run_id}:curated_manifest"),
            run_id=context.run_id,
            stage=PointerBatchStage.CURATED_MANIFEST,
            pointers=result.pointers,
        )
        pointers_artifact = artifact(
            context.run_id,
            ArtifactKind.RESOURCE_POINTERS,
            self.name,
            pointer_batch.model_dump(mode="json"),
            evidence_ids=[
                evidence_id
                for pointer in result.pointers
                for evidence_id in pointer.evidence_ids
            ],
        )
        discovery_artifact = artifact(
            context.run_id,
            ArtifactKind.DISCOVERY_RESULT,
            self.name,
            result.discovery.model_dump(mode="json"),
        )
        inventory_artifact = artifact(
            context.run_id,
            ArtifactKind.ASSET_INVENTORY,
            self.name,
            {
                "pointers": [item.model_dump(mode="json") for item in result.pointers],
                "assets": [item.model_dump(mode="json") for item in assets],
                "observations": [item.model_dump(mode="json") for item in observations],
                "bundles": [item.model_dump(mode="json") for item in result.bundles],
                "bundle_summaries": [
                    item.model_dump(mode="json") for item in result.bundle_summaries
                ],
                "errors": list(result.errors),
                "delta_ids": [],
                "bulk_download_performed": False,
            },
            evidence_ids=[item.evidence_id for item in result.evidence],
            parents=[pointers_artifact.artifact_id, discovery_artifact.artifact_id],
        )
        ingestion_artifact = artifact(
            context.run_id,
            ArtifactKind.MANIFEST_INGESTION,
            self.name,
            self._ingestion_payload(result),
            evidence_ids=[item.evidence_id for item in result.evidence],
            parents=[inventory_artifact.artifact_id],
        )
        return [pointers_artifact, discovery_artifact, inventory_artifact, ingestion_artifact]

    async def _probe(self, context, result, assets, observations):
        probe = RemoteAvailabilityProbe(
            concurrency=int(context.policy.get("remote_probe_concurrency", 8)),
            timeout_seconds=float(context.policy.get("remote_probe_timeout_seconds", 10.0)),
            max_attempts=int(context.policy.get("remote_probe_max_attempts", 2)),
        )
        # A HEAD on a portal page's own URL says nothing about whether the
        # file behind its click-through UI is reachable, so only assets with
        # a direct location are worth probing.
        probeable = [
            item
            for item in assets
            if item.locations and item.locations[0].protocol != TransferProtocol.MANUAL
        ]
        probed, probe_observations = await probe.probe_assets(probeable)
        probed_by_id = {item.asset_id: item for item in probed}
        merged_assets = [probed_by_id.get(item.asset_id, item) for item in assets]
        return merged_assets, [*observations, *probe_observations]

    def _record(self, context, result: ManifestIngestion, assets, observations) -> None:
        for item in assets:
            context.catalog.append(
                record_id=item.asset_id, record_type="data_asset",
                run_id=context.run_id, value=item,
            )
        for item in observations:
            context.catalog.append(
                record_id=item.observation_id, record_type="remote_asset_observation",
                run_id=context.run_id, value=item,
            )
        for item in result.bundles:
            context.catalog.append(
                record_id=item.bundle_id, record_type="semantic_bundle",
                run_id=context.run_id, value=item,
            )
            context.catalog.transition_bundle(
                item.bundle_id, BundleStatus.REMOTE_INVENTORIED, run_id=context.run_id,
            )
        for item in result.bundle_summaries:
            context.catalog.append(
                record_id=item.summary_id, record_type="remote_bundle_inventory_summary",
                run_id=context.run_id, value=item,
            )

    @staticmethod
    def _ingestion_payload(result: ManifestIngestion) -> dict:
        return {
            "ingestion_id": result.ingestion_id,
            "source_path": result.source_path,
            "source_sha256": result.source_sha256,
            "dialect_id": result.dialect_id,
            "filters": result.filters,
            "row_count": result.row_count,
            "pair_unit_count": result.pair_unit_count,
            "bundle_count": result.bundle_count,
            "sidecars": [item.model_dump(mode="json") for item in result.sidecars],
            "evidence": [item.model_dump(mode="json") for item in result.evidence],
            "issues": [item.model_dump(mode="json") for item in result.issues],
        }
