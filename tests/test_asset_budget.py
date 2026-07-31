"""Per-resolver asset budgets and explicit truncation.

Regression cover for a defect found by auditing two live runs: the asset budget
was shared across every resolver and exhausting it broke out of the resolver
loop entirely. A study with thousands of ENA runs therefore consumed the whole
budget before the GEO adapter ran, so its processed matrices were never
fetched and the bundle could only ever satisfy the raw_reads schema — losing
exactly the large studies a pretraining corpus wants most.
"""

import pytest

from scdata_agent.application.agents.inventory_reconcile import (
    InventoryReconcileAgent,
)
from scdata_agent.domain.lifecycle import SemanticBundle
from scdata_agent.domain.models import (
    AccessLevel,
    AssetAvailability,
    AssetLocation,
    AssetResolutionResult,
    DataAsset,
    DataRole,
    DatasetCandidate,
    TransferProtocol,
)
from scdata_agent.infrastructure.sources.asset_registry import (
    AssetResolverRegistry,
)


def _asset(index: int, role: DataRole, source: str) -> DataAsset:
    return DataAsset(
        asset_id=f"{source}_{index}",
        candidate_id="cand_1",
        role=role,
        file_name=f"{source}_{index}.dat",
        locations=[
            AssetLocation(
                url=f"https://{source}.test/{index}.dat",
                protocol=TransferProtocol.HTTPS,
                access_level=AccessLevel.PUBLIC_VERIFIED,
                availability=AssetAvailability.OBSERVED,
                size_bytes=100,
            )
        ],
    )


class _StubResolver:
    """Stands in for a repository adapter with a fixed listing."""

    def __init__(self, name, assets, max_assets):
        self.name = name
        self.max_assets = max_assets
        self._assets = assets
        self.called = False

    async def resolve(self, candidate):
        self.called = True
        return AssetResolutionResult(
            candidate_id=candidate.candidate_id,
            assets=self._assets[: self.max_assets],
        )


def _registry(resolvers, max_assets=5):
    registry = AssetResolverRegistry(max_assets=max_assets)
    registry.resolvers = resolvers
    return registry


@pytest.mark.asyncio
async def test_an_exhausted_budget_does_not_starve_later_resolvers():
    """The core regression: GEO must still run after ENA fills its budget."""
    candidate = DatasetCandidate(candidate_id="cand_1", title="study")
    raw = _StubResolver(
        "ena", [_asset(i, DataRole.RAW_READS, "ena") for i in range(20)], 5
    )
    processed = _StubResolver(
        "geo", [_asset(i, DataRole.FRAGMENTS, "geo") for i in range(3)], 5
    )

    result = await _registry([raw, processed]).resolve(candidate)

    assert processed.called, "the second resolver never ran"
    roles = {item.role for item in result.assets}
    assert DataRole.FRAGMENTS in roles and DataRole.RAW_READS in roles


@pytest.mark.asyncio
async def test_truncation_is_reported_per_resolver():
    candidate = DatasetCandidate(candidate_id="cand_1", title="study")
    raw = _StubResolver(
        "ena", [_asset(i, DataRole.RAW_READS, "ena") for i in range(20)], 5
    )
    processed = _StubResolver(
        "geo", [_asset(i, DataRole.FRAGMENTS, "geo") for i in range(2)], 5
    )

    result = await _registry([raw, processed]).resolve(candidate)

    # Only the adapter that actually hit its cap is named.
    assert result.truncated_resolvers == ["ena"]


@pytest.mark.asyncio
async def test_a_complete_listing_reports_no_truncation():
    candidate = DatasetCandidate(candidate_id="cand_1", title="study")
    small = _StubResolver(
        "geo", [_asset(i, DataRole.FRAGMENTS, "geo") for i in range(2)], 5
    )

    result = await _registry([small]).resolve(candidate)

    assert result.truncated_resolvers == []


def test_processed_data_resolvers_run_before_raw_reads():
    """Matrices decide schema satisfaction and are far smaller than FASTQ."""
    names = [item.name for item in AssetResolverRegistry().resolvers]

    assert names.index("geo_supplementary") < names.index("ena_file_report")


class TestSummaryWithholdsSizeWhenTruncated:
    def _summary(self, truncated):
        bundle = SemanticBundle(
            bundle_id="bundle_1", candidate_id="cand_1", bundle_type="raw_reads",
            asset_ids=["ena_0"],
        )
        assets = [_asset(0, DataRole.RAW_READS, "ena")]
        candidate = DatasetCandidate(candidate_id="cand_1", title="study")
        return InventoryReconcileAgent._summarize(
            bundle, assets, candidate, truncated
        )

    def test_complete_listing_reports_a_total(self):
        summary = self._summary([])

        assert summary.total_bytes == 100
        assert summary.inventory_truncated is False

    def test_truncated_listing_withholds_the_total(self):
        """Summing a subset would state a size guaranteed to be too low."""
        summary = self._summary(["ena"])

        assert summary.total_bytes is None
        assert summary.inventory_truncated is True
        assert summary.truncated_resolvers == ["ena"]
        # The lower bound is still available to anyone who wants it.
        assert summary.known_bytes == 100
