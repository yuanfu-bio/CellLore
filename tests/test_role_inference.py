"""Role inference against file names actually observed in GEO/ENA/10x payloads.

The names below were taken from a live discovery run; before these cases were
covered, almost every real supplementary file fell through to ``OTHER`` and
every semantic bundle was reported as incomplete.
"""

import pytest

from scdata_agent.domain.assets import infer_data_role
from scdata_agent.domain.models import DataRole


@pytest.mark.parametrize(
    ("file_name", "expected"),
    [
        # --- 10x Cell Ranger / Cell Ranger ARC ---
        ("GSM1234_filtered_feature_bc_matrix.h5", DataRole.RNA_MATRIX),
        ("GSM1234_raw_feature_bc_matrix.h5", DataRole.RNA_MATRIX),
        ("GSM1234_filtered_peak_bc_matrix.h5", DataRole.PEAK_MATRIX),
        ("GSM1234_atac_fragments.tsv.gz", DataRole.FRAGMENTS),
        ("GSM1234_atac_fragments.tsv.gz.tbi", DataRole.FRAGMENT_INDEX),
        ("GSM1234_per_barcode_metrics.csv", DataRole.QC_METRICS),
        ("GSM1234_singlecell.csv", DataRole.QC_METRICS),
        ("GSM1234_atac_peaks.bed.gz", DataRole.PEAKS),
        # --- MTX triplet: modality lives in the directory, not the file ---
        ("filtered_peak_bc_matrix/matrix.mtx.gz", DataRole.PEAK_MATRIX),
        ("filtered_gene_bc_matrix/matrix.mtx.gz", DataRole.RNA_MATRIX),
        ("filtered_peak_bc_matrix/barcodes.tsv.gz", DataRole.CELL_METADATA),
        ("barcodes.tsv.gz", DataRole.CELL_METADATA),
        # --- Standard single-cell objects, including compressed ---
        ("study.h5ad", DataRole.STANDARD_OBJECT),
        ("study.h5ad.gz", DataRole.STANDARD_OBJECT),
        ("study.h5mu", DataRole.STANDARD_OBJECT),
        # --- Reads ---
        ("SRR12345678_1.fastq.gz", DataRole.RAW_READS),
        ("possorted_bam.bam", DataRole.ALIGNED_READS),
        # --- Archives: contents unknowable without downloading ---
        ("GSE123456_RAW.tar", DataRole.ARCHIVE),
        ("supplementary.tar.gz", DataRole.ARCHIVE),
        ("processed.zip", DataRole.ARCHIVE),
        # --- Explicit cross-modality maps ---
        ("atac_rna_barcode_mapping.tsv", DataRole.BARCODE_LINKAGE),
        # --- SNARE-seq (GSE126074): a canonical public multiome deposition ---
        ("GSE126074_AdBrainCortex_SNAREseq_cDNA.counts.mtx.gz", DataRole.RNA_MATRIX),
        ("GSE126074_AdBrainCortex_SNAREseq_chromatin.counts.mtx.gz", DataRole.PEAK_MATRIX),
        ("GSE126074_AdBrainCortex_SNAREseq_cDNA.barcodes.tsv.gz", DataRole.CELL_METADATA),
        ("GSE126074_CellLineMixture_SNAREseq_cDNA_counts.tsv.gz", DataRole.RNA_MATRIX),
        # Observed in the wild: a doubly-compressed tabix index.
        ("GSE277496_HEALTHY_2_atac_fragments.tsv.gz.tbi.gz", DataRole.FRAGMENT_INDEX),
    ],
)
def test_real_repository_file_names_resolve_to_roles(file_name, expected):
    assert infer_data_role(file_name) == expected


def test_archives_are_distinguishable_from_unclassified_files():
    """An archive is 'unknown contents', which is not the same as 'no role'."""
    assert infer_data_role("GSE123456_RAW.tar") == DataRole.ARCHIVE
    assert infer_data_role("readme.txt") is None


def test_inference_does_not_invent_roles_for_ambiguous_names():
    """A bare matrix with no modality context must not be guessed at."""
    assert infer_data_role("matrix.mtx.gz") is None
    assert infer_data_role("notes.pdf") is None
