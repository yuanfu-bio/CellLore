from pathlib import Path

from celllore.domain.lifecycle import (
    MaterializationManifest,
    MaterializationStatus,
    TransferReceipt,
    TransferReceiptStatus,
)
from celllore.domain.models import DataRole, GateStatus, TransferProtocol
from celllore.infrastructure.qc_metrics import compute_role_metrics


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


def _manifest(receipts, roles, **overrides):
    defaults = {
        "manifest_id": "manifest_1",
        "bundle_id": "bundle_1",
        "candidate_id": "candidate_1",
        "status": MaterializationStatus.MATERIALIZED,
        "source_receipt_ids": [item.receipt_id for item in receipts],
        "source_asset_ids": list(roles),
        "source_asset_roles": roles,
        "package_path": "/tmp/x",
    }
    defaults.update(overrides)
    return MaterializationManifest(**defaults)


class TestRnaMatrixCounts:
    """Integer-like count *structure*, verified from file content -- a
    log-normalized or scaled matrix (fractional or negative values) must
    fail even if repository text calls it raw counts. A PASS here is
    necessary but not sufficient for an R03 raw/unnormalized-counts claim,
    which also needs declared repository/article provenance evidence; this
    check alone never establishes that claim by itself."""

    def test_pass_for_valid_matrix_market_integers(self, tmp_path):
        matrix = tmp_path / "matrix.mtx.gz"
        import gzip

        with gzip.open(matrix, "wt") as handle:
            handle.write(
                "%%MatrixMarket matrix coordinate integer general\n"
                "2 2 2\n1 1 3\n2 2 5\n"
            )
        receipts = [_receipt("matrix", matrix)]
        manifest = _manifest(receipts, {"matrix": DataRole.RNA_MATRIX})
        checks, metrics, unavailable = compute_role_metrics(manifest, receipts)

        assert checks["rna_matrix_integer_like"] == GateStatus.PASS
        assert metrics["rna_matrix_values_sampled"] == 2
        assert "rna_matrix_integer_like" not in unavailable

    def test_fail_for_fractional_values(self, tmp_path):
        matrix = tmp_path / "matrix.mtx"
        matrix.write_text(
            "%%MatrixMarket matrix coordinate real general\n2 2 1\n1 1 3.5\n",
            encoding="utf-8",
        )
        receipts = [_receipt("matrix", matrix)]
        manifest = _manifest(receipts, {"matrix": DataRole.RNA_MATRIX})
        checks, _, _ = compute_role_metrics(manifest, receipts)

        assert checks["rna_matrix_integer_like"] == GateStatus.FAIL

    def test_fail_for_negative_values(self, tmp_path):
        matrix = tmp_path / "matrix.mtx"
        matrix.write_text(
            "%%MatrixMarket matrix coordinate integer general\n2 2 1\n1 1 -3\n",
            encoding="utf-8",
        )
        receipts = [_receipt("matrix", matrix)]
        manifest = _manifest(receipts, {"matrix": DataRole.RNA_MATRIX})
        checks, _, _ = compute_role_metrics(manifest, receipts)

        assert checks["rna_matrix_integer_like"] == GateStatus.FAIL

    def test_corrupt_h5_bytes_fail_rather_than_silently_pass(self, tmp_path):
        """A file that cannot even be opened as HDF5 is a real finding
        (FAIL), not an unsupported-format gap -- native 10x `.h5` is now a
        parsed format, not a named unavailable one."""
        matrix = tmp_path / "filtered_feature_bc_matrix.h5"
        matrix.write_bytes(b"not actually hdf5")
        receipts = [_receipt("matrix", matrix)]
        manifest = _manifest(receipts, {"matrix": DataRole.RNA_MATRIX})
        checks, _, _ = compute_role_metrics(manifest, receipts)

        assert checks["rna_matrix_integer_like"] == GateStatus.FAIL

    def test_pass_for_a_valid_cellranger_3_h5(self, tmp_path):
        """CellRanger 3+ layout: a top-level `matrix` group with a `data`
        dataset holding the nonzero sparse values."""
        import h5py
        import numpy as np

        matrix = tmp_path / "filtered_feature_bc_matrix.h5"
        with h5py.File(matrix, "w") as handle:
            handle.create_dataset("matrix/data", data=np.array([3, 5, 0, 2], dtype="int32"))
        receipts = [_receipt("matrix", matrix)]
        manifest = _manifest(receipts, {"matrix": DataRole.RNA_MATRIX})
        checks, metrics, unavailable = compute_role_metrics(manifest, receipts)

        assert checks["rna_matrix_integer_like"] == GateStatus.PASS
        assert metrics["rna_matrix_values_sampled"] == 4
        assert "rna_matrix_integer_like" not in unavailable

    def test_pass_for_a_valid_cellranger_2_h5(self, tmp_path):
        """CellRanger 2.x layout: one top-level group per genome (no
        `matrix` group at all)."""
        import h5py
        import numpy as np

        matrix = tmp_path / "GRCh38_filtered_gene_bc_matrices_h5.h5"
        with h5py.File(matrix, "w") as handle:
            handle.create_dataset("GRCh38/data", data=np.array([1, 4, 9], dtype="int32"))
        receipts = [_receipt("matrix", matrix)]
        manifest = _manifest(receipts, {"matrix": DataRole.RNA_MATRIX})
        checks, metrics, _ = compute_role_metrics(manifest, receipts)

        assert checks["rna_matrix_integer_like"] == GateStatus.PASS
        assert metrics["rna_matrix_values_sampled"] == 3

    def test_fail_for_a_cellranger_h5_with_fractional_values(self, tmp_path):
        import h5py
        import numpy as np

        matrix = tmp_path / "filtered_feature_bc_matrix.h5"
        with h5py.File(matrix, "w") as handle:
            handle.create_dataset("matrix/data", data=np.array([1.5, 2.0], dtype="float32"))
        receipts = [_receipt("matrix", matrix)]
        manifest = _manifest(receipts, {"matrix": DataRole.RNA_MATRIX})
        checks, _, _ = compute_role_metrics(manifest, receipts)

        assert checks["rna_matrix_integer_like"] == GateStatus.FAIL


class TestRnaAtacBarcodeOverlap:
    """The FILE_OBSERVED/RECOMPUTED-level pairing evidence
    `has_pair_relationship`'s corpus-ready bar is meant to be built from --
    see paired_atac_rna_human's acceptable_for_corpus_ready set."""

    def _fragments(self, tmp_path, barcodes):
        path = tmp_path / "fragments.tsv"
        path.write_text(
            "".join(f"chr1\t{i * 10}\t{i * 10 + 5}\t{bc}\t1\n" for i, bc in enumerate(barcodes)),
            encoding="utf-8",
        )
        return path

    def _barcodes_file(self, tmp_path, barcodes):
        path = tmp_path / "barcodes.tsv"
        path.write_text("\n".join(barcodes) + "\n", encoding="utf-8")
        return path

    def test_pass_when_rna_and_atac_share_most_barcodes(self, tmp_path):
        fragments = self._fragments(tmp_path, ["bcA", "bcB", "bcC"])
        barcodes = self._barcodes_file(tmp_path, ["bcA", "bcB"])
        receipts = [_receipt("fragments", fragments), _receipt("barcodes", barcodes)]
        manifest = _manifest(receipts, {
            "fragments": DataRole.FRAGMENTS, "barcodes": DataRole.CELL_METADATA,
        })
        checks, metrics, _ = compute_role_metrics(manifest, receipts)

        assert checks["barcode_linkage"] == GateStatus.PASS
        assert metrics["n_rna_barcodes"] == 2
        assert metrics["n_atac_barcodes"] == 3
        assert metrics["n_barcode_intersection"] == 2
        assert metrics["frac_rna_barcodes_matched"] == 1.0

    def test_fail_when_barcodes_dont_match(self, tmp_path):
        fragments = self._fragments(tmp_path, ["bcA", "bcB"])
        barcodes = self._barcodes_file(tmp_path, ["bcX", "bcY"])
        receipts = [_receipt("fragments", fragments), _receipt("barcodes", barcodes)]
        manifest = _manifest(receipts, {
            "fragments": DataRole.FRAGMENTS, "barcodes": DataRole.CELL_METADATA,
        })
        checks, metrics, _ = compute_role_metrics(manifest, receipts)

        assert checks["barcode_linkage"] == GateStatus.FAIL
        assert metrics["frac_rna_barcodes_matched"] == 0.0

    def test_normalizes_a_trailing_gem_suffix(self, tmp_path):
        fragments = self._fragments(tmp_path, ["bcA", "bcB"])
        barcodes = self._barcodes_file(tmp_path, ["bcA-1", "bcB-1"])
        receipts = [_receipt("fragments", fragments), _receipt("barcodes", barcodes)]
        manifest = _manifest(receipts, {
            "fragments": DataRole.FRAGMENTS, "barcodes": DataRole.CELL_METADATA,
        })
        checks, metrics, _ = compute_role_metrics(manifest, receipts)

        assert checks["barcode_linkage"] == GateStatus.PASS
        assert metrics["frac_rna_barcodes_matched"] == 1.0

    def test_unknown_without_any_barcode_source(self, tmp_path):
        fragments = self._fragments(tmp_path, ["cell-1", "cell-2"])
        matrix = tmp_path / "matrix.mtx"
        matrix.write_text(
            "%%MatrixMarket matrix coordinate integer general\n1 1 1\n1 1 1\n",
            encoding="utf-8",
        )
        receipts = [_receipt("fragments", fragments), _receipt("matrix", matrix)]
        manifest = _manifest(receipts, {
            "fragments": DataRole.FRAGMENTS, "matrix": DataRole.RNA_MATRIX,
        })
        checks, _, _ = compute_role_metrics(manifest, receipts)

        assert checks["barcode_linkage"] == GateStatus.UNKNOWN
