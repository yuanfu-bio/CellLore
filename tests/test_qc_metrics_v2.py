from pathlib import Path

from scdata_agent.domain.lifecycle import (
    MaterializationManifest,
    MaterializationStatus,
    TransferReceipt,
    TransferReceiptStatus,
)
from scdata_agent.domain.models import DataRole, GateStatus, TransferProtocol
from scdata_agent.infrastructure.qc_metrics import compute_role_metrics


def _receipt(asset_id: str, path: Path) -> TransferReceipt:
    return TransferReceipt(
        receipt_id=f"receipt_{asset_id}",
        plan_id="plan_1",
        bundle_id="bundle_1",
        asset_id=asset_id,
        source_url=f"https://files.test/{path.name}",
        local_path=str(path),
        protocol=TransferProtocol.HTTPS,
        status=TransferReceiptStatus.COMPLETED,
        bytes_written=path.stat().st_size,
    )


def test_role_aware_qc_recomputes_sampled_frip_and_genome_consistency(tmp_path):
    fragments = tmp_path / "fragments.tsv"
    fragments.write_text(
        "chr1\t10\t20\tcell-1\t1\n"
        "chr1\t100\t110\tcell-2\t1\n",
        encoding="utf-8",
    )
    peaks = tmp_path / "peaks.bed"
    peaks.write_text("chr1\t5\t25\n", encoding="utf-8")
    receipts = [
        _receipt("fragments", fragments),
        _receipt("peaks", peaks),
    ]
    manifest = MaterializationManifest(
        manifest_id="manifest_1",
        bundle_id="bundle_1",
        candidate_id="candidate_1",
        status=MaterializationStatus.MATERIALIZED,
        source_receipt_ids=[item.receipt_id for item in receipts],
        source_asset_ids=["fragments", "peaks"],
        source_asset_roles={
            "fragments": DataRole.FRAGMENTS,
            "peaks": DataRole.PEAKS,
        },
        genome_builds=["GRCh38"],
        package_path=str(tmp_path),
    )
    checks, metrics, unavailable = compute_role_metrics(manifest, receipts)
    assert checks["genome_build_consistency"] == GateStatus.PASS
    assert checks["frip_computable"] == GateStatus.PASS
    assert metrics["frip_sampled"] == 0.5
    assert "frip" not in unavailable
