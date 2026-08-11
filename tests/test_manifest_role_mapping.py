"""The curator's file role wins, and the vocabulary is total.

Both properties are what keep a manifest from degrading quietly: an unmapped
role must stop the row rather than land in OTHER, and a filename-inference
disagreement must be reported rather than resolved in favour of the regex.
"""

from __future__ import annotations

from pathlib import Path

from celllore.domain.assets import infer_data_role
from celllore.domain.manifest import (
    PAIRED_MULTIOME_ZH_V1,
    IssueSeverity,
    normalize_row,
)
from celllore.domain.models import DataRole
from celllore.infrastructure.manifest_reader import read_manifest, read_tabular

FIXTURE = Path(__file__).parent / "data" / "manifest_paired_multiome_sample.csv"

# Every file role the curated manifest uses, and the role each denotes. Kept
# explicit rather than derived from the dialect so that changing the dialect
# has to be a deliberate edit here too.
EXPECTED_ROLES: dict[str, DataRole] = {
    "fragment": DataRole.FRAGMENTS,
    "fragment_full": DataRole.FRAGMENTS,
    "tabix_index": DataRole.FRAGMENT_INDEX,
    "filtered_mtx_matrix": DataRole.RNA_MATRIX,
    "raw_mtx_matrix": DataRole.RNA_MATRIX,
    "filtered_integer_count_h5": DataRole.RNA_MATRIX,
    "raw_count": DataRole.RNA_MATRIX,
    "raw_count_h5": DataRole.RNA_MATRIX,
    "filtered_mtx_barcodes": DataRole.CELL_METADATA,
    "raw_mtx_barcodes": DataRole.CELL_METADATA,
    "cell_metadata": DataRole.CELL_METADATA,
    "atac_cell_metadata": DataRole.CELL_METADATA,
    "rna_cell_metadata": DataRole.CELL_METADATA,
    "filtered_mtx_features": DataRole.OTHER,
    "raw_mtx_features": DataRole.OTHER,
    "donor_metadata": DataRole.DONOR_METADATA,
    "clinical_metadata": DataRole.SAMPLE_METADATA,
    "per_barcode_metrics": DataRole.QC_METRICS,
    "web_summary": DataRole.QC_METRICS,
    "multiome_archive": DataRole.ARCHIVE,
}


def test_every_manifest_role_maps_to_the_expected_data_role() -> None:
    assert PAIRED_MULTIOME_ZH_V1.role_vocabulary == EXPECTED_ROLES


def test_an_unmapped_role_stops_the_row_instead_of_defaulting_to_other() -> None:
    _, raw_rows = read_tabular(FIXTURE)
    raw = dict(raw_rows[0])
    raw["文件角色"] = "peak_matrix_v2"
    row, issues = normalize_row(PAIRED_MULTIOME_ZH_V1, raw, 1)
    assert row is None
    codes = {item.code for item in issues if item.severity == IssueSeverity.ERROR}
    assert codes == {"unknown_file_role"}


def test_manifest_role_beats_filename_inference_on_a_real_dogma_row() -> None:
    # GSM5065531_DIG_CTRL_GExp_ATAC_filtered_matrix.mtx.gz is an RNA count
    # matrix from a DOGMA-seq library whose name mentions both modalities.
    # ROLE_PATTERNS reads "ATAC ... matrix" and says peak matrix; the curator,
    # who read the GEO record, says RNA. The curator wins, loudly.
    document = read_manifest(FIXTURE)
    name = "GSM5065531_DIG_CTRL_GExp_ATAC_filtered_matrix.mtx.gz"
    row = next(item for item in document.rows if item.file_name == name)

    assert row.role == DataRole.RNA_MATRIX
    assert infer_data_role(name) == DataRole.PEAK_MATRIX

    disagreements = [
        item
        for item in document.issues
        if item.code == "role_inference_disagreement" and item.file_name == name
    ]
    assert len(disagreements) == 1
    assert disagreements[0].severity == IssueSeverity.WARNING
    assert "rna_count_matrix" in disagreements[0].message
    assert "peak_by_cell_matrix" in disagreements[0].message


def test_native_role_survives_so_downstream_can_tell_mex_parts_apart() -> None:
    document = read_manifest(FIXTURE)
    features = [item for item in document.rows if item.file_role == "filtered_mtx_features"]
    barcodes = [item for item in document.rows if item.file_role == "filtered_mtx_barcodes"]
    assert features and barcodes
    # Both would be indistinguishable if only DataRole were kept: features has
    # no DataRole member at all, and barcodes shares CELL_METADATA with the
    # study's own cell annotation table.
    assert {item.role for item in features} == {DataRole.OTHER}
    assert {item.role for item in barcodes} == {DataRole.CELL_METADATA}


def test_a_tabix_index_is_never_classified_as_fragments() -> None:
    # The QC layer decides whether to parse a file as fragments from its role.
    # A .tbi carrying "fragments" in its name must not reach that parser.
    assert PAIRED_MULTIOME_ZH_V1.role_vocabulary["tabix_index"] == DataRole.FRAGMENT_INDEX
    assert (
        infer_data_role("GSM7102986_CNA_10xmultiome_1.FULL.fragments.tsv.gz.tbi.gz")
        == DataRole.FRAGMENT_INDEX
    )
