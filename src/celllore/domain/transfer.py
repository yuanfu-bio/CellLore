"""Side-effect-free transfer planning for verified public data assets."""

from __future__ import annotations

from pathlib import Path

from celllore.domain.identifiers import stable_id
from celllore.domain.models import (
    AccessLevel,
    AssetAvailability,
    AssetBundle,
    DataAsset,
    TransferItem,
    TransferPlan,
)

PUBLIC_ACCESS = {
    AccessLevel.PUBLIC_VERIFIED,
    AccessLevel.PUBLIC_DECLARED,
}


class TransferPlanner:
    """Select one usable location per asset without downloading any bytes."""

    def plan(
        self,
        bundle: AssetBundle,
        assets: list[DataAsset],
        destination: str | Path,
    ) -> TransferPlan:
        by_id = {asset.asset_id: asset for asset in assets}
        destination = Path(destination)
        items: list[TransferItem] = []
        unresolved: list[str] = []

        for asset_id in bundle.asset_ids:
            asset = by_id.get(asset_id)
            location = self._best_location(asset) if asset else None
            if asset is None or location is None:
                unresolved.append(asset_id)
                continue
            items.append(
                TransferItem(
                    asset_id=asset.asset_id,
                    source_url=location.url,
                    destination=str(destination / asset.file_name),
                    protocol=location.protocol,
                    expected_size_bytes=location.size_bytes,
                    checksum_algorithm=location.checksum_algorithm,
                    checksum=location.checksum,
                )
            )

        sizes = [item.expected_size_bytes for item in items]
        total = (
            sum(size for size in sizes if size is not None)
            if items and all(size is not None for size in sizes)
            else None
        )
        return TransferPlan(
            plan_id=stable_id(
                "transfer", f"{bundle.bundle_id}:{destination.as_posix()}"
            ),
            bundle_id=bundle.bundle_id,
            items=items,
            unresolved_asset_ids=unresolved,
            total_expected_bytes=total,
            executable=bundle.complete and not unresolved,
        )

    @staticmethod
    def _best_location(asset: DataAsset | None):
        if asset is None:
            return None
        usable = [
            location
            for location in asset.locations
            if location.access_level in PUBLIC_ACCESS
            and location.availability
            in {AssetAvailability.OBSERVED, AssetAvailability.DECLARED}
        ]
        usable.sort(
            key=lambda item: (
                item.availability != AssetAvailability.OBSERVED,
                item.access_level != AccessLevel.PUBLIC_VERIFIED,
                item.protocol.value,
            )
        )
        return usable[0] if usable else None
