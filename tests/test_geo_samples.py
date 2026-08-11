"""Per-GSM metadata parsing: the raw material PairUnitResolver reasons over.

Fixture text below is a trimmed, real excerpt of GSE178454's
`acc.cgi?targ=gsm&form=text&view=full` response (fetched and verified against
the live GEO endpoint), covering two samples whose titles name the same
patient (HF1) across an ATAC sample and an RNA sample -- exactly the kind of
signal PairUnitResolver should reason over, and exactly the kind GSM
accession-number adjacency is not: GSM5391762 and GSM5391769 are seven
accessions apart, not adjacent, yet clearly the same patient by title.
"""

from celllore.infrastructure.sources.geo_samples import parse_sample_blocks

_FIXTURE = """\
^SAMPLE = GSM5391762
!Sample_title = Sample 1-A: Pit_Pedia_HF1_fragm_snATAC
!Sample_geo_accession = GSM5391762
!Sample_source_name_ch1 = Pituitary gland
!Sample_organism_ch1 = Homo sapiens
!Sample_characteristics_ch1 = age: 1
!Sample_characteristics_ch1 = gender: Female
!Sample_library_strategy = ATAC-seq
!Sample_supplementary_file_1 = ftp://ftp.ncbi.nlm.nih.gov/geo/samples/GSM5391nnn/GSM5391762/suppl/GSM5391762_HF1_atac_fragments.tsv.gz
!Sample_series_id = GSE178454
^SAMPLE = GSM5391769
!Sample_title = Sample 1-R: Pit_Pedia_HF1_fragm_snRNA
!Sample_geo_accession = GSM5391769
!Sample_source_name_ch1 = Pituitary gland
!Sample_organism_ch1 = Homo sapiens
!Sample_characteristics_ch1 = age: 1
!Sample_characteristics_ch1 = gender: Female
!Sample_library_strategy = RNA-seq
!Sample_supplementary_file_1 = ftp://ftp.ncbi.nlm.nih.gov/geo/samples/GSM5391nnn/GSM5391769/suppl/GSM5391769_HF1_filtered_feature_bc_matrix.h5
!Sample_series_id = GSE178454
"""


def test_splits_into_one_record_per_sample():
    records = parse_sample_blocks(_FIXTURE, "GSE178454")
    assert [item.gsm_accession for item in records] == ["GSM5391762", "GSM5391769"]


def test_extracts_title_source_organism():
    records = parse_sample_blocks(_FIXTURE, "GSE178454")
    atac = records[0]
    assert atac.title == "Sample 1-A: Pit_Pedia_HF1_fragm_snATAC"
    assert atac.source_name == "Pituitary gland"
    assert atac.organism == "Homo sapiens"


def test_collects_all_characteristics_lines():
    records = parse_sample_blocks(_FIXTURE, "GSE178454")
    assert records[0].characteristics == ["age: 1", "gender: Female"]


def test_collects_supplementary_files_per_sample_not_series_wide():
    """The gap this module exists to close: file-to-GSM linkage. Each
    sample's files must stay attached to that sample, not pooled."""
    records = parse_sample_blocks(_FIXTURE, "GSE178454")
    atac, rna = records
    assert "GSM5391762_HF1_atac_fragments.tsv.gz" in atac.supplementary_files[0]
    assert "GSM5391769_HF1_filtered_feature_bc_matrix.h5" in rna.supplementary_files[0]

    assert atac.supplementary_files != rna.supplementary_files


def test_series_accession_is_attached_to_every_record():
    records = parse_sample_blocks(_FIXTURE, "GSE178454")
    assert all(item.series_accession == "GSE178454" for item in records)


def test_raw_text_preserves_the_source_block_for_evidence():
    records = parse_sample_blocks(_FIXTURE, "GSE178454")
    assert "Pit_Pedia_HF1_fragm_snATAC" in records[0].raw_text


def test_same_patient_identifier_appears_in_both_titles_not_accession_adjacency():
    """The point of fetching this at all: the shared signal for grouping is
    the title text ("HF1"), which is exactly what accession numbers do not
    carry -- GSM5391762 and GSM5391769 are seven accessions apart."""
    records = parse_sample_blocks(_FIXTURE, "GSE178454")
    assert "HF1" in records[0].title
    assert "HF1" in records[1].title
    accession_gap = int(records[1].gsm_accession[3:]) - int(records[0].gsm_accession[3:])
    assert accession_gap > 1


def test_empty_response_yields_no_records():
    assert parse_sample_blocks("", "GSE1") == []


def test_malformed_text_without_sample_headers_yields_no_records():
    assert parse_sample_blocks("!Series_title = something\n", "GSE1") == []
