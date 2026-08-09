"""Merge progressive Asset Scout deltas into pair-unit-grained bundles.

Used to build exactly one ``SemanticBundle`` per candidate regardless of how
many independent RNA+ATAC pairings a study actually contained -- a six-patient
GSE306275-shaped study reconciled to one undifferentiated bundle instead of
six independently-judgeable ones. ``PairUnitResolver`` proposes how a
candidate's assets split into curatable units (an LLM-driven grouping
hypothesis, never GSM accession adjacency); ``PairingVerifier`` then decides,
per proposed unit, whether the physical relationship the profile requires is
actually proven. Neither component may be skipped or reordered: only the
verifier's output may become a ``has_pair_relationship`` claim.
"""

from __future__ import annotations

from scdata_agent.application.agents.pair_unit_resolver import (
    PairUnitResolver,
    configured_pair_unit_resolver,
)
from scdata_agent.application.agents.pairing_verifier import (
    PairingVerifier,
    configured_pairing_verifier,
)
from scdata_agent.application.agents.shared import (
    AgentContext,
    latest_artifact,
    need_discovery,
)
from scdata_agent.application.collaboration import AgentName, ArtifactKind, artifact
from scdata_agent.application.resource_pipeline import (
    asset_file_type,
    bundle_shape_for,
    merge_pointers,
    omics_layer_claims,
    pointer_batches,
    summarize_bundle,
)
from scdata_agent.domain.identifiers import stable_id
from scdata_agent.domain.lifecycle import (
    BundleStatus,
    EvidenceClaim,
    SemanticBundle,
)
from scdata_agent.domain.models import DataAsset, GeoSampleRecord
from scdata_agent.domain.pair_units import ProposedPairUnit
from scdata_agent.domain.resource_pipeline import AssetInventoryDelta


class InventoryReconcileAgent:
    name = AgentName.ASSET_SCOUT

    def __init__(
        self,
        resolver: PairUnitResolver | None = None,
        verifier: PairingVerifier | None = None,
    ) -> None:
        # None means "resolve for real at run time" (see `run`), reading
        # `context.policy["use_llm"]` the same way every other LLM-backed
        # agent in this graph decides per-run rather than at construction
        # time (construction happens once, before any run's policy exists).
        # Passing an explicit resolver/verifier -- as tests do -- overrides
        # that and is used for every run this agent instance handles.
        self._resolver_override = resolver
        self._verifier_override = verifier

    async def run(self, context: AgentContext):
        use_llm = context.policy.get("use_llm", True)
        # Both fall back to a safe, fully deterministic no-LLM shape when
        # unavailable: PairUnitResolver keeps every asset in one unit,
        # PairingVerifier makes no claim beyond its deterministic tiers. A
        # study this pair silently cannot process degrades to today's
        # one-bundle-per-candidate behavior instead of losing data.
        resolver = self._resolver_override or configured_pair_unit_resolver(use_llm)
        verifier = self._verifier_override or configured_pairing_verifier(use_llm)

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
        assets_by_id = {item.asset_id: item for item in assets}

        geo_samples_artifact = latest_artifact(
            context.artifacts, ArtifactKind.GEO_SAMPLE_METADATA.value
        )
        samples_by_candidate: dict[str, list[GeoSampleRecord]] = {
            candidate_id: [GeoSampleRecord.model_validate(item) for item in raw_records]
            for candidate_id, raw_records in (
                geo_samples_artifact.payload.get("samples_by_candidate", {})
                if geo_samples_artifact
                else {}
            ).items()
        }

        candidate_by_id = {
            item.candidate_id: item for item in discovery.candidates
        }
        bundles: list[SemanticBundle] = []
        proposed_units: list[ProposedPairUnit] = []
        pairing_claims: list[EvidenceClaim] = []
        layer_claims: list[EvidenceClaim] = []
        for candidate in discovery.candidates:
            candidate_assets = assets_by_candidate.get(
                candidate.candidate_id, []
            )
            if not candidate_assets:
                continue
            # Candidate-scoped (not per-unit): AssayProfile text doesn't
            # distinguish per-pair-unit, so every pair-unit of this candidate
            # shares the same declared-layer evidence.
            layer_claims.extend(omics_layer_claims(candidate, context.requirement))
            samples = samples_by_candidate.get(candidate.candidate_id, [])
            units = await resolver.resolve(candidate, samples, candidate_assets)
            proposed_units.extend(units)
            for unit in units:
                unit_assets = [
                    assets_by_id[asset_id]
                    for asset_id in unit.member_assets
                    if asset_id in assets_by_id
                ]
                if not unit_assets:
                    continue
                claim = await verifier.verify(unit, candidate, samples)
                if claim is not None:
                    pairing_claims.append(claim)
                shape = bundle_shape_for(candidate, unit_assets, context.requirement)
                bundles.append(
                    SemanticBundle(
                        bundle_id=stable_id(
                            "semantic_bundle",
                            f"{candidate.candidate_id}:{unit.proposed_unit_id}:"
                            f"{','.join(sorted(item.asset_id for item in unit_assets))}",
                        ),
                        candidate_id=candidate.candidate_id,
                        pair_unit_id=unit.proposed_unit_id,
                        bundle_type=shape.bundle_type,
                        asset_ids=[
                            item.asset_id for item in unit_assets
                        ],
                        required_roles=shape.required_roles,
                        missing_roles=shape.missing_roles,
                        applicable_schema_ids=shape.applicable_schema_ids,
                        satisfied_schema_ids=shape.satisfied_schema_ids,
                        observed_roles=shape.observed_roles,
                        status=BundleStatus.REMOTE_INVENTORIED,
                        evidence_ids=list(
                            dict.fromkeys(
                                [
                                    *evidence_by_candidate.get(
                                        candidate.candidate_id, []
                                    ),
                                    *[
                                        evidence_id
                                        for asset in unit_assets
                                        for evidence_id in asset.evidence_ids
                                    ],
                                    *unit.evidence_ids,
                                    *([claim.claim_id] if claim is not None else []),
                                ]
                            )
                        ),
                    )
                )

        # Truncation is a property of the whole run's scouting, so union the
        # phases: a listing cut short in the direct phase stays partial even if
        # the enriched phase completed.
        truncated_by_candidate: dict[str, list[str]] = {}
        for delta in deltas:
            for candidate_id, names in delta.truncated_by_candidate.items():
                truncated_by_candidate.setdefault(candidate_id, []).extend(names)
        summaries = [
            self._summarize(
                bundle,
                [
                    assets_by_id[asset_id]
                    for asset_id in bundle.asset_ids
                    if asset_id in assets_by_id
                ],
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
            for item in proposed_units:
                context.catalog.append(
                    record_id=item.proposed_unit_id,
                    record_type="proposed_pair_unit",
                    run_id=context.run_id,
                    value=item,
                )
            for item in (*pairing_claims, *layer_claims):
                context.catalog.append(
                    record_id=item.claim_id,
                    record_type="evidence_claim",
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
                    "proposed_pair_units": [
                        item.model_dump(mode="json") for item in proposed_units
                    ],
                    "pairing_claims": [
                        item.model_dump(mode="json") for item in pairing_claims
                    ],
                    "layer_claims": [
                        item.model_dump(mode="json") for item in layer_claims
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
                    *([geo_samples_artifact.artifact_id] if geo_samples_artifact else []),
                ],
            )
        ]

    # Summarizing moved to application.resource_pipeline so curated-manifest
    # ingestion produces the identical summary rather than a parallel one.
    _summarize = staticmethod(summarize_bundle)
    _file_type = staticmethod(asset_file_type)

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
