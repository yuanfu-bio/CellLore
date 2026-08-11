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

import asyncio
import re
from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

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
from scdata_agent.domain.assets import infer_data_role
from scdata_agent.domain.identifiers import stable_id
from scdata_agent.domain.lifecycle import (
    BundleStatus,
    EvidenceClaim,
    RemoteAssetObservation,
    SemanticBundle,
)
from scdata_agent.domain.models import (
    AccessLevel,
    AssetAvailability,
    AssetLocation,
    DataAsset,
    DataRole,
    DatasetCandidate,
    GeoSampleRecord,
    OmicsLayer,
    PairingLevel,
    TransferProtocol,
)
from scdata_agent.domain.pair_units import ProposedPairUnit
from scdata_agent.domain.resource_pipeline import AssetInventoryDelta
from scdata_agent.infrastructure.remote_probe import RemoteAvailabilityProbe
from scdata_agent.infrastructure.sources.common import infer_assay_profiles, infer_genome_build


async def _verify_with_limit(
    verifier: PairingVerifier,
    semaphore: asyncio.Semaphore,
    unit: ProposedPairUnit,
    candidate: DatasetCandidate,
    samples: list[GeoSampleRecord],
) -> EvidenceClaim | None:
    async with semaphore:
        return await verifier.verify(unit, candidate, samples)


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
        # GeoSampleMetadataAgent fetches each GSM's own supplementary-file
        # locators for PairUnitResolver to read as grouping-hypothesis text,
        # but a resolver can only group asset ids it is given. Without
        # turning them into real DataAssets here, only series-level
        # supplementary files (typically unrelated sidecars or an unopened
        # RAW.tar archive) ever reached the inventory, and a study's actual
        # per-sample RNA/ATAC files never became part of any bundle. Merge any
        # (candidate, file_name) another resolver already produced -- e.g. a
        # direct file_url pointer -- so the same physical file never becomes
        # two DataAssets while its stronger GSM semantics are retained.
        candidate_by_id = {
            item.candidate_id: item for item in discovery.candidates
        }
        allowed_candidate_ids = set(candidate_by_id)
        delta_assets = [
            asset
            for delta in deltas
            for asset in delta.assets
            if asset.candidate_id in allowed_candidate_ids
        ]
        all_geo_sample_assets = [
            asset
            for candidate_id, samples in samples_by_candidate.items()
            if candidate_id in allowed_candidate_ids
            for asset in _geo_sample_assets(
                candidate_id, samples, candidate_by_id.get(candidate_id)
            )
        ]
        geo_assets_by_file = {
            (asset.candidate_id, asset.file_name): asset
            for asset in all_geo_sample_assets
        }
        # A series-level resolver may have already emitted the same URL with
        # role=OTHER because it lacked the owning GSM's title and
        # characteristics. Keep that resolver's verified location/status, but
        # upgrade the semantic role and evidence using the more specific
        # sample-level record instead of letting the earlier weak result mask
        # it forever.
        delta_assets = [
            _upgrade_asset_from_geo_sample(asset, geo_assets_by_file)
            for asset in delta_assets
        ]
        already_resolved = {
            (asset.candidate_id, asset.file_name) for asset in delta_assets
        }
        geo_sample_assets = [
            asset
            for asset in all_geo_sample_assets
            if (asset.candidate_id, asset.file_name) not in already_resolved
        ]
        # These assets are built here, not by ProgressiveAssetScoutAgent, so
        # unlike delta_assets they have never had a chance to earn
        # PUBLIC_VERIFIED/OBSERVED status. Left as PUBLIC_DECLARED, every
        # bundle built from them stays stuck at acquisition's manual_review
        # tier (access:declared_only) regardless of how strong the pairing
        # evidence is. Probing here -- our own HEAD/Range check, not the
        # depositor's claim -- is what lets a fully-verified bundle actually
        # reach "recommended".
        geo_probe_observations: list[RemoteAssetObservation] = []
        remote_probe_enabled = bool(
            context.policy.get("remote_probe_enabled", True)
        )
        if remote_probe_enabled:
            self._publish_progress(
                context,
                phase="remote_probe",
                completed=0,
                total=len(geo_sample_assets),
                percent=0,
            )
            geo_sample_assets, geo_probe_observations = await self._probe(
                context, geo_sample_assets
            )

        assets = self._merge_assets([*delta_assets, *geo_sample_assets])
        observations = list(
            {
                item.observation_id: item
                for delta in deltas
                for item in delta.observations
            }.values()
        )
        observations = list(
            {
                item.observation_id: item
                for item in [*observations, *geo_probe_observations]
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

        candidates_with_assets = [
            candidate
            for candidate in discovery.candidates
            if assets_by_candidate.get(candidate.candidate_id)
        ]
        candidate_total = len(candidates_with_assets)
        progress_base = 20 if remote_probe_enabled and geo_sample_assets else 0
        self._publish_progress(
            context,
            phase="candidate_reconciliation",
            completed=0,
            total=candidate_total,
            percent=progress_base,
        )

        bundles: list[SemanticBundle] = []
        proposed_units: list[ProposedPairUnit] = []
        excluded_pair_units: list[dict[str, object]] = []
        pairing_claims: list[EvidenceClaim] = []
        layer_claims: list[EvidenceClaim] = []
        for candidate_index, candidate in enumerate(candidates_with_assets, 1):
            candidate_assets = assets_by_candidate.get(
                candidate.candidate_id, []
            )
            self._publish_progress(
                context,
                phase="candidate_reconciliation",
                completed=candidate_index - 1,
                total=candidate_total,
                percent=self._candidate_percent(
                    candidate_index - 1,
                    candidate_total,
                    progress_base,
                ),
                current_candidate_id=candidate.candidate_id,
                current_candidate_title=candidate.title,
            )
            # Candidate-scoped (not per-unit): AssayProfile text doesn't
            # distinguish per-pair-unit, so every pair-unit of this candidate
            # shares the same declared-layer evidence.
            layer_claims.extend(
                omics_layer_claims(candidate, context.requirement, candidate_assets)
            )
            samples = samples_by_candidate.get(candidate.candidate_id, [])
            units = await resolver.resolve(candidate, samples, candidate_assets)
            proposed_units.extend(units)
            verification_items: list[tuple[ProposedPairUnit, list[DataAsset]]] = []
            for unit in units:
                unit_assets = [
                    assets_by_id[asset_id]
                    for asset_id in unit.member_assets
                    if asset_id in assets_by_id
                ]
                if not unit_assets:
                    excluded_pair_units.append({
                        "proposed_unit_id": unit.proposed_unit_id,
                        "candidate_id": candidate.candidate_id,
                        "reason": "no_resolved_assets",
                    })
                    continue
                if not _unit_covers_required_omics(
                    unit_assets, candidate, context.requirement
                ):
                    excluded_pair_units.append({
                        "proposed_unit_id": unit.proposed_unit_id,
                        "candidate_id": candidate.candidate_id,
                        "reason": "missing_required_omics_layers",
                    })
                    continue
                verification_items.append((unit, unit_assets))

            # Units are independent once the resolver has fixed membership.
            # A small concurrency bound removes the previous N × model-latency
            # serial bottleneck without changing any unit's evidence scope.
            concurrency = max(
                1,
                min(
                    8,
                    int(context.policy.get("pairing_verifier_concurrency", 3)),
                ),
            )
            semaphore = asyncio.Semaphore(concurrency)

            claims = await asyncio.gather(
                *(
                    _verify_with_limit(
                        verifier, semaphore, unit, candidate, samples
                    )
                    for unit, _ in verification_items
                )
            )
            for (unit, unit_assets), claim in zip(
                verification_items, claims, strict=True
            ):
                if claim is not None:
                    pairing_claims.append(claim)
                verified_relationship = (
                    PairingLevel(claim.value)
                    if claim is not None and isinstance(claim.value, str)
                    else None
                )
                shape = bundle_shape_for(
                    candidate,
                    unit_assets,
                    context.requirement,
                    verified_relationship=verified_relationship,
                )
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

            self._publish_progress(
                context,
                phase="candidate_reconciliation",
                completed=candidate_index,
                total=candidate_total,
                percent=self._candidate_percent(
                    candidate_index,
                    candidate_total,
                    progress_base,
                ),
                current_candidate_id=candidate.candidate_id,
                current_candidate_title=candidate.title,
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
        self._publish_progress(
            context,
            phase="completed",
            completed=candidate_total,
            total=candidate_total,
            percent=100,
        )
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
                    "excluded_pair_units": excluded_pair_units,
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

    async def _probe(
        self, context: AgentContext, assets: list[DataAsset]
    ) -> tuple[list[DataAsset], list[RemoteAssetObservation]]:
        probe = RemoteAvailabilityProbe(
            concurrency=int(context.policy.get("remote_probe_concurrency", 8)),
            timeout_seconds=float(
                context.policy.get("remote_probe_timeout_seconds", 10.0)
            ),
            max_attempts=int(context.policy.get("remote_probe_max_attempts", 2)),
        )
        probeable = [
            item
            for item in assets
            if item.locations and item.locations[0].protocol != TransferProtocol.MANUAL
        ]
        def report(completed: int, total: int) -> None:
            self._publish_progress(
                context,
                phase="remote_probe",
                completed=completed,
                total=total,
                percent=round(20 * completed / total) if total else 20,
            )

        probed, probe_observations = await probe.probe_assets(
            probeable,
            progress_callback=report,
        )
        probed_by_id = {item.asset_id: item for item in probed}
        merged_assets = [probed_by_id.get(item.asset_id, item) for item in assets]
        return merged_assets, probe_observations

    @staticmethod
    def _candidate_percent(completed: int, total: int, base: int) -> int:
        return (
            round(base + (100 - base) * completed / total)
            if total
            else 100
        )

    @staticmethod
    def _publish_progress(
        context: AgentContext,
        *,
        phase: str,
        completed: int,
        total: int,
        percent: int,
        current_candidate_id: str | None = None,
        current_candidate_title: str | None = None,
    ) -> None:
        context.ledger.add_event(
            context.run_id,
            "inventory_progress",
            {
                "phase": phase,
                "completed": completed,
                "total": total,
                "percent": max(0, min(100, percent)),
                "current_candidate_id": current_candidate_id,
                "current_candidate_title": current_candidate_title,
            },
        )

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


def _geo_sample_assets(
    candidate_id: str,
    samples: list[GeoSampleRecord],
    candidate: DatasetCandidate | None,
) -> list[DataAsset]:
    """Turn each GSM's own supplementary-file locators into ``DataAsset``s.

    The basename computed here must agree with
    ``pair_unit_resolver._file_basename``: that is how
    ``PairUnitResolver._assets_for_samples`` matches a proposed unit's member
    samples back to asset ids, by ``file_name``, not by any explicit GSM
    field on ``DataAsset``.

    ``genome_build`` is read from this GSM's own text first (most specific),
    falling back to the series' title/description -- neither is required
    (``paired_atac_rna_human``'s ``resolved_assembly`` evidence is ungated at
    the acquisition tier precisely because GEO per-sample listings almost
    never state a build), but recording it when the text does say so feeds
    the existing bundle-scoped ``resolved_assembly_claim``/catalog assembly
    column for free, without waiting on a real file download.
    """
    candidate_genome_build = (
        infer_genome_build([candidate.title, candidate.description])
        if candidate is not None
        else None
    )
    assets: list[DataAsset] = []
    for sample in samples:
        genome_build = infer_genome_build([sample.raw_text]) or candidate_genome_build
        locators = [
            url
            for url in sample.supplementary_files
            if not _is_missing_geo_file_locator(url)
        ]
        roles_by_url = {
            url: _geo_sample_data_role(_geo_sample_file_name(url), sample)
            for url in locators
        }
        sample_layers = {
            layer
            for profile in infer_assay_profiles([
                sample.title,
                sample.source_name,
                *sample.characteristics,
                sample.raw_text,
            ])
            for layer in profile.omics_layers
        }
        classified_layers = {
            layer
            for role in roles_by_url.values()
            for layer in [_omics_layer_for_data_role(role)]
            if layer is not None
        }
        missing_layers = sample_layers - classified_layers
        ambiguous_objects = [
            url
            for url, role in roles_by_url.items()
            if role == DataRole.OTHER
            and _is_generic_matrix_object(_geo_sample_file_name(url))
        ]
        if len(missing_layers) == 1 and len(ambiguous_objects) == 1:
            inferred_role = _data_role_for_omics_layer(next(iter(missing_layers)))
            if inferred_role is not None:
                roles_by_url[ambiguous_objects[0]] = inferred_role

        for url in locators:
            file_name = _geo_sample_file_name(url)
            assets.append(
                DataAsset(
                    asset_id=stable_id(
                        "asset",
                        f"{candidate_id}:geo_sample:{sample.gsm_accession}:{url}",
                    ),
                    candidate_id=candidate_id,
                    role=roles_by_url[url],
                    file_name=file_name,
                    compression="gzip" if file_name.endswith(".gz") else None,
                    genome_build=genome_build,
                    locations=[
                        AssetLocation(
                            url=_geo_sample_https_url(url),
                            # GEO publishes many locators with an ftp://
                            # prefix, but the URL above is deliberately
                            # normalized to NCBI's HTTPS endpoint. Keep the
                            # protocol field consistent with the actual URL;
                            # marking it FTP caused the transfer backend to
                            # reject every file before making a request.
                            protocol=TransferProtocol.HTTPS,
                            access_level=AccessLevel.PUBLIC_DECLARED,
                            availability=AssetAvailability.DECLARED,
                            evidence_ids=sample.evidence_ids,
                        )
                    ],
                    evidence_ids=sample.evidence_ids,
                )
            )
    return assets


def _is_missing_geo_file_locator(locator: str) -> bool:
    return locator.strip().casefold() in {
        "none", "na", "n/a", "not available", "not applicable", "-"
    }


def _is_generic_matrix_object(file_name: str) -> bool:
    return bool(re.search(
        r"(?:matrix|processed|\.mtx(?:\.gz)?$|\.(?:rds|h5|hdf5)(?:\.gz)?$)",
        file_name.casefold(),
    ))


def _omics_layer_for_data_role(role: DataRole) -> OmicsLayer | None:
    if role in {
        DataRole.FRAGMENTS,
        DataRole.FRAGMENT_INDEX,
        DataRole.PEAKS,
        DataRole.PEAK_MATRIX,
    }:
        return OmicsLayer.CHROMATIN_ACCESSIBILITY
    if role == DataRole.RNA_MATRIX:
        return OmicsLayer.TRANSCRIPTOME
    if role == DataRole.PROTEIN_MATRIX:
        return OmicsLayer.PROTEIN
    return None


def _data_role_for_omics_layer(layer: OmicsLayer) -> DataRole | None:
    return {
        OmicsLayer.CHROMATIN_ACCESSIBILITY: DataRole.PEAK_MATRIX,
        OmicsLayer.TRANSCRIPTOME: DataRole.RNA_MATRIX,
        OmicsLayer.PROTEIN: DataRole.PROTEIN_MATRIX,
    }.get(layer)


def _unit_covers_required_omics(
    assets: list[DataAsset],
    candidate: DatasetCandidate,
    requirement,
) -> bool:
    required = set(requirement.required_omics_layers if requirement else [])
    if not required:
        return True
    observed = {
        layer
        for asset in assets
        for layer in [_omics_layer_for_data_role(asset.role)]
        if layer is not None
    }
    # A self-describing object can carry more than one modality. Only use the
    # candidate's declared assay profile for it; never promote a generic file.
    if any(asset.role == DataRole.STANDARD_OBJECT for asset in assets):
        observed.update(
            layer
            for profile in candidate.assay_profiles
            for layer in profile.omics_layers
        )
    return required <= observed


def _upgrade_asset_from_geo_sample(
    asset: DataAsset,
    geo_assets_by_file: dict[tuple[str, str], DataAsset],
) -> DataAsset:
    contextual = geo_assets_by_file.get((asset.candidate_id, asset.file_name))
    if (
        contextual is None
        or asset.role != DataRole.OTHER
        or contextual.role == DataRole.OTHER
    ):
        return asset
    return asset.model_copy(
        update={
            "role": contextual.role,
            "genome_build": asset.genome_build or contextual.genome_build,
            "evidence_ids": list(
                dict.fromkeys([*asset.evidence_ids, *contextual.evidence_ids])
            ),
        }
    )


def _geo_sample_data_role(file_name: str, sample: GeoSampleRecord) -> DataRole:
    """Classify a GEO supplementary file using its owning GSM as context.

    GEO frequently publishes a generic ``matrix.mtx`` or an ``.rds`` whose
    modality appears only in the Sample title/characteristics. The global
    filename classifier cannot safely infer those files in isolation, but the
    owning GSM supplies exactly the missing context. Explicit filename
    signals win; only otherwise-ambiguous matrix/object files use the sample's
    single observed omics layer.
    """
    inferred = infer_data_role(file_name)
    if inferred is not None:
        return inferred

    normalized = file_name.casefold()
    matrix_or_object = bool(
        re.search(
            r"(?:matrix|counts?|processed|\.mtx(?:\.gz)?$|"
            r"\.(?:rds|h5|hdf5|csv|tsv)(?:\.gz)?$)",
            normalized,
        )
    )
    if matrix_or_object:
        if re.search(
            r"(?:^|[_./-])(?:adt|protein|antibody|epitope)(?:[_./-]|$)",
            normalized,
        ):
            return DataRole.PROTEIN_MATRIX
        if re.search(
            r"(?:^|[_./-])(?:rna|gex|gene[_-]?expression|soupx)(?:[_./-]|$)",
            normalized,
        ):
            return DataRole.RNA_MATRIX
        if re.search(
            r"(?:^|[_./-])(?:atac|chromatin|peak)(?:[_./-]|$)",
            normalized,
        ):
            return DataRole.PEAK_MATRIX

        profiles = infer_assay_profiles(
            [sample.title, sample.source_name, *sample.characteristics]
        )
        layers = {
            layer
            for profile in profiles
            for layer in profile.omics_layers
        }
        # Some GEO records use a generic title and put the only modality clue
        # in fields such as ``!Sample_library_strategy``. Read the sample's
        # own SOFT block as a fallback, but never merge it with an already
        # ambiguous multi-layer interpretation.
        if len(layers) != 1 and sample.raw_text:
            raw_profiles = infer_assay_profiles([sample.raw_text])
            raw_layers = {
                layer
                for profile in raw_profiles
                for layer in profile.omics_layers
            }
            if len(raw_layers) == 1:
                layers = raw_layers
        if layers == {OmicsLayer.TRANSCRIPTOME}:
            return DataRole.RNA_MATRIX
        if layers == {OmicsLayer.CHROMATIN_ACCESSIBILITY}:
            return DataRole.PEAK_MATRIX
        if layers == {OmicsLayer.PROTEIN}:
            return DataRole.PROTEIN_MATRIX

    return DataRole.OTHER


def _geo_sample_file_name(url: str) -> str:
    return PurePosixPath(unquote(urlparse(url).path)).name or url


def _geo_sample_https_url(url: str) -> str:
    if url.startswith("ftp://"):
        return f"https://{url.removeprefix('ftp://')}"
    return url
