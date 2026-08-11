"""Transitive lineage and evaluation-leakage inference."""

from __future__ import annotations

from collections import defaultdict
from urllib.parse import urlsplit, urlunsplit

from celllore.application.agents.shared import AgentContext, latest_artifact, need_discovery
from celllore.application.collaboration import AgentName, ArtifactKind, artifact
from celllore.domain.identifiers import stable_id
from celllore.domain.lifecycle import (
    BundleLineageAssessment,
    LineageFamily,
    LineageGraphSnapshot,
    ResourcePointer,
    SemanticBundle,
    TransferReceipt,
)
from celllore.domain.models import DataAsset


class ProgressiveLineageLeakageAgent:
    """Union candidates across identifiers, publications, URLs, and checksums."""

    name = AgentName.LINEAGE_LEAKAGE

    def __init__(self, phase: str) -> None:
        if phase not in {"early", "late"}:
            raise ValueError("Lineage phase must be early or late.")
        self.phase = phase

    async def run(self, context: AgentContext):
        discovery = need_discovery(context)
        inventory = latest_artifact(context.artifacts, ArtifactKind.ASSET_INVENTORY.value)
        payload = inventory.payload if inventory else {}
        bundles = [SemanticBundle.model_validate(item) for item in payload.get("bundles", [])]
        assets = [DataAsset.model_validate(item) for item in payload.get("assets", [])]
        pointers = [ResourcePointer.model_validate(item) for item in payload.get("pointers", [])]
        candidate_ids = [item.candidate_id for item in discovery.candidates]
        parent = {item: item for item in candidate_ids}

        def find(item: str) -> str:
            while parent[item] != item:
                parent[item] = parent[parent[item]]
                item = parent[item]
            return item

        def union(left: str, right: str) -> None:
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parent[max(left_root, right_root)] = min(left_root, right_root)

        keyed: dict[str, set[str]] = defaultdict(set)
        relation_by_key: dict[str, str] = {}

        def add(candidate_id: str, relation: str, value: str) -> None:
            # Resource pointers originate before GEO Sample hard-constraint
            # refinement, so the inventory artifact can legitimately retain
            # pointers for candidates no longer present in discovery. They
            # must not re-enter the union-find graph or index its parent map.
            if candidate_id not in parent or not value:
                return
            key = f"{relation}:{value.casefold()}"
            keyed[key].add(candidate_id)
            relation_by_key[key] = relation

        for candidate in discovery.candidates:
            for identifier in candidate.identifiers:
                add(candidate.candidate_id, "repository_identifier", identifier.canonical)
            for identifier in candidate.publication_identifiers:
                add(candidate.candidate_id, "publication_identifier", identifier.canonical)
        for pointer in pointers:
            add(pointer.candidate_id, "resource_pointer", f"{pointer.namespace}:{pointer.locator}")
        asset_candidate = {asset.asset_id: asset.candidate_id for asset in assets}
        for asset in assets:
            for location in asset.locations:
                add(asset.candidate_id, "normalized_url", self._normalize_url(location.url))
                if location.checksum:
                    add(asset.candidate_id, "declared_checksum",
                        f"{location.checksum_algorithm or 'unknown'}:{location.checksum}")
        transfer = None
        if self.phase == "late":
            transfer = latest_artifact(context.artifacts, ArtifactKind.TRANSFER_RECEIPTS.value)
            for raw in (transfer.payload.get("receipts", []) if transfer else []):
                receipt = TransferReceipt.model_validate(raw)
                candidate_id = asset_candidate.get(receipt.asset_id)
                if candidate_id and receipt.observed_checksum and receipt.checksum_valid is not False:
                    add(candidate_id, "observed_checksum",
                        f"{receipt.checksum_algorithm or 'unknown'}:{receipt.observed_checksum}")
        for members in keyed.values():
            ordered = sorted(members)
            for item in ordered[1:]:
                union(ordered[0], item)

        components: dict[str, list[str]] = defaultdict(list)
        for candidate_id in candidate_ids:
            components[find(candidate_id)].append(candidate_id)
        bundles_by_candidate: dict[str, list[str]] = defaultdict(list)
        for bundle in bundles:
            bundles_by_candidate[bundle.candidate_id].append(bundle.bundle_id)
        evaluation = {str(item).casefold() for item in context.policy.get("evaluation_identifiers", [])}
        candidate_by_id = {item.candidate_id: item for item in discovery.candidates}
        families: list[LineageFamily] = []
        evaluation_family_ids: list[str] = []
        family_by_candidate: dict[str, str] = {}
        for members in sorted(components.values(), key=lambda values: sorted(values)):
            members = sorted(members)
            component_keys = sorted(
                key for key, owners in keyed.items() if owners & set(members)
            )
            family_id = stable_id("lineage_family", "|".join(component_keys) or "|".join(members))
            relations = sorted({relation_by_key[key] for key in component_keys})
            family = LineageFamily(
                family_id=family_id,
                candidate_ids=members,
                bundle_ids=sorted({bundle_id for member in members for bundle_id in bundles_by_candidate[member]}),
                evidence_ids=sorted({evidence.evidence_id for member in members for evidence in candidate_by_id[member].evidence}),
                relation_types=relations,
            )
            families.append(family)
            for member in members:
                family_by_candidate[member] = family_id
            tokens = {
                value
                for member in members
                for identifier in [
                    *candidate_by_id[member].identifiers,
                    *candidate_by_id[member].publication_identifiers,
                ]
                for value in {identifier.canonical.casefold(), identifier.value.casefold()}
            }
            if tokens & evaluation:
                evaluation_family_ids.append(family_id)

        assessments = []
        for bundle in bundles:
            family_id = family_by_candidate.get(bundle.candidate_id)
            family = next((item for item in families if item.family_id == family_id), None)
            # Redundancy is about the same data being seen under more than one
            # candidate, not about a candidate having more than one bundle (a
            # dataset can legitimately have many samples in one family).
            candidate_count = len(family.candidate_ids) if family else 1
            leaked = family_id in evaluation_family_ids
            assessments.append(BundleLineageAssessment(
                bundle_id=bundle.bundle_id,
                candidate_id=bundle.candidate_id,
                family_ids=[family_id] if family_id else [],
                evaluation_leakage=leaked,
                redundancy_status="candidate" if candidate_count > 1 else "unique",
                reason_codes=[
                    *( ["evaluation_family"] if leaked else [] ),
                    *( ["related_bundle_candidates"] if candidate_count > 1 else [] ),
                ],
            ))
        snapshot = LineageGraphSnapshot(
            snapshot_id=stable_id("lineage_snapshot", f"{context.run_id}:{self.phase}"),
            run_id=context.run_id,
            phase=self.phase,
            families=families,
            evaluation_family_ids=sorted(evaluation_family_ids),
            bundle_assessments=assessments,
        )
        if context.catalog is not None:
            context.catalog.append(record_id=snapshot.snapshot_id,
                                   record_type="lineage_snapshot",
                                   run_id=context.run_id, value=snapshot)
        parents = [item.artifact_id for item in (inventory, transfer) if item is not None]
        return [artifact(context.run_id, ArtifactKind.LINEAGE_REPORT, self.name,
                         snapshot.model_dump(mode="json"), parents=parents)]

    @staticmethod
    def _normalize_url(url: str) -> str:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(),
                           parts.path.rstrip("/"), "", ""))
