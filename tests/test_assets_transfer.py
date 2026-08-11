from celllore.domain.assets import build_asset_bundle, infer_data_role
from celllore.domain.models import (
    AccessLevel,
    AssetAvailability,
    AssetLocation,
    DataAsset,
    DataRole,
    TransferProtocol,
)
from celllore.domain.transfer import TransferPlanner


def test_role_inference_and_complete_transfer_plan(tmp_path):
    assert infer_data_role("sample_fragments.tsv.gz") == DataRole.FRAGMENTS
    assert infer_data_role("sample_fragments.tsv.gz.tbi") == DataRole.FRAGMENT_INDEX

    asset = DataAsset(
        asset_id="asset_1",
        candidate_id="candidate_1",
        role=DataRole.FRAGMENTS,
        file_name="fragments.tsv.gz",
        locations=[
            AssetLocation(
                url="https://example.test/fragments.tsv.gz",
                protocol=TransferProtocol.HTTPS,
                access_level=AccessLevel.PUBLIC_VERIFIED,
                availability=AssetAvailability.OBSERVED,
                size_bytes=12,
            )
        ],
    )
    bundle = build_asset_bundle(
        "candidate_1",
        [asset],
        bundle_type="processed_atac",
        required_roles=[DataRole.FRAGMENTS],
    )
    plan = TransferPlanner().plan(bundle, [asset], tmp_path)

    assert bundle.complete
    assert plan.executable
    assert plan.total_expected_bytes == 12


def test_unknown_location_blocks_execution(tmp_path):
    asset = DataAsset(
        asset_id="asset_1",
        candidate_id="candidate_1",
        role=DataRole.FRAGMENTS,
        file_name="fragments.tsv.gz",
    )
    bundle = build_asset_bundle(
        "candidate_1",
        [asset],
        bundle_type="processed_atac",
        required_roles=[DataRole.FRAGMENTS],
    )
    plan = TransferPlanner().plan(bundle, [asset], tmp_path)

    assert not plan.executable
    assert plan.unresolved_asset_ids == ["asset_1"]
