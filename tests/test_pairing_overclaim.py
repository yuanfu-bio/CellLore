"""Regression tests for the pairing overclaim (roadmap P0.0).

Every payload below is a verbatim excerpt of what an archive actually served,
taken from `eval/records.jsonl`. Hand-invented strings would not have caught
either defect: both depend on the exact shape of NCBI's SRA ESummary and on
ENCODE's habit of filing every experiment under one BioProject.

The failure these pin down is a false positive. A standalone snATAC-seq run
reporting itself as same-nucleus paired multiome is admitted to a corpus its
data cannot support, and nothing downstream objects -- so it has to be caught
here or not at all.
"""

from celllore.domain.models import (
    AccessLevel,
    Identifier,
    OmicsLayer,
    PairingLevel,
    SourceRecord,
)
from celllore.domain.resolution import merge_source_records
from celllore.infrastructure.sources.common import (
    infer_assay_profiles,
    prose_text,
)

# src_22124b1fe971220e, an ENCODE snATAC-seq deposition brokered by GEO. The
# submitter's name contains "Gene Expression"; the library protocol is a PDF
# named for the 10x Multiome kit. Neither says anything about this experiment.
ENCODE_SRA_EXPXML = (
    '<Summary><Title>snATAC-seq of heart left ventricle</Title>'
    '<Platform instrument_model="Illumina NovaSeq 6000">ILLUMINA</Platform>'
    '<Statistics total_runs="4"/></Summary>'
    '<Submitter acc="SRA2493201" center_name="GEO" '
    'contact_name="Gene Expression Omnibus (GEO), NCBI, NLM, NIH, '
    'https://www.ncbi.nlm.nih.gov/geo" lab_name=""/>'
    '<Experiment acc="SRX34494284" name="snATAC-seq"/>'
    '<Library_descriptor><LIBRARY_STRATEGY>ATAC-seq</LIBRARY_STRATEGY>'
    '<LIBRARY_CONSTRUCTION_PROTOCOL>general protocol: '
    'https://www.encodeproject.org/documents/4d42b1ef-c9e6-4b31-a443-'
    '1266079545d0/@@download/attachment/Chromium_Multiome_ATAC_GEX.pdf'
    '</LIBRARY_CONSTRUCTION_PROTOCOL></Library_descriptor>'
)


def test_submitter_boilerplate_does_not_create_a_transcriptome_layer():
    """`contact_name="Gene Expression Omnibus ..."` is not an RNA assay."""
    profiles = infer_assay_profiles(("NCBI record 45847608", ENCODE_SRA_EXPXML))

    assert [profile.label for profile in profiles] == ["standalone_single_cell_atac"]
    assert profiles[0].omics_layers == [OmicsLayer.CHROMATIN_ACCESSIBILITY]
    assert OmicsLayer.TRANSCRIPTOME not in profiles[0].omics_layers


def test_protocol_filename_does_not_promote_pairing_to_same_nucleus():
    """Citing `Chromium_Multiome_ATAC_GEX.pdf` is not a same-nucleus claim."""
    profiles = infer_assay_profiles(("NCBI record 45847608", ENCODE_SRA_EXPXML))

    assert profiles[0].pairing_level == PairingLevel.NOT_APPLICABLE
    assert profiles[0].pairing_level != PairingLevel.SAME_NUCLEUS


def test_prose_extraction_drops_markup_and_urls_but_keeps_sentences():
    prose = prose_text(("Title here", ENCODE_SRA_EXPXML))

    assert "gene expression omnibus" not in prose
    assert "chromium_multiome_atac_gex" not in prose
    assert "encodeproject.org" not in prose
    # Element *text* survives; only attributes, URLs and filenames are dropped.
    assert "snatac-seq of heart left ventricle" in prose
    assert "atac-seq" in prose


def test_multiome_stated_in_prose_is_still_believed():
    """The fix must not deafen the inference to a real claim.

    src_d0078a6526f54d97's abstract states the assay outright, and the golden
    set requires this record to reach same-nucleus.
    """
    profiles = infer_assay_profiles((
        "Single-nucleus multi-omics delineates distinct epigenetic programs "
        "in lung adenocarcinoma.",
        "<h4>Background</h4>We performed single-nucleus RNA sequencing "
        "(snRNA-seq) and single-nucleus Assay for Transposase-Accessible "
        "Chromatin sequencing (snATAC-seq) as a multiome assay.",
    ))

    assert set(profiles[0].omics_layers) == {
        OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME,
    }
    assert profiles[0].pairing_level == PairingLevel.SAME_NUCLEUS


def test_multiomics_as_a_study_design_is_not_a_same_nucleus_claim():
    """`multiomics` describes what was studied, not what shared a nucleus.

    src_e59419eab003451a says "single cell multiomics" and never states the
    measurement unit, so the strongest honest answer is that both layers are
    present and their relationship is unstated.
    """
    profiles = infer_assay_profiles((
        "Single cell multiomics revealed fibrotic trajectories of "
        "endometrial cells",
        "We profiled chromatin accessibility with scATAC-seq and the "
        "transcriptome with scRNA-seq.",
    ))

    assert profiles[0].pairing_level == PairingLevel.STUDY_LEVEL
    assert set(profiles[0].omics_layers) == {
        OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME,
    }


def _encode_record(record_id: str, series: str, title: str) -> SourceRecord:
    """One ENCODE experiment: its own GEO Series, the shared ENCODE umbrella."""
    return SourceRecord(
        record_id=record_id,
        source_name="ncbi_sra",
        external_id=record_id,
        title=title,
        identifiers=[
            Identifier(namespace="geo_series", value=series),
            Identifier(namespace="bioproject", value="PRJNA63443"),
            Identifier(namespace="sra_project", value="SRP012412"),
        ],
        access_level=AccessLevel.PUBLIC_DECLARED,
    )


def test_shared_umbrella_project_does_not_merge_distinct_studies():
    """PRJNA63443 covers all of ENCODE; it is not a dataset identity.

    Merging on it collapsed 13 GEO Series into one candidate whose modality
    list was the union of everything in the group, so a single genuinely
    multiome member let the standalone snATAC ones pass a same-cell question.
    """
    records = [
        _encode_record("a", "GSE340993", "snATAC-seq from mucosa"),
        _encode_record("b", "GSE340999", "snATAC-seq from Panc1"),
        _encode_record("c", "GSE341008", "snATAC-seq from SK-N-SH"),
    ]

    candidates = merge_source_records(records)

    assert len(candidates) == 3
    assert sorted(item.source_record_ids[0] for item in candidates) == ["a", "b", "c"]


def test_records_sharing_a_study_identifier_still_merge():
    """The umbrella rule must not stop a genuine GEO/SRA pairing from merging."""
    records = [
        _encode_record("a", "GSE340993", "snATAC-seq from mucosa"),
        _encode_record("b", "GSE340993", "SRA mirror of the same series"),
    ]

    candidates = merge_source_records(records)

    assert len(candidates) == 1
    assert candidates[0].source_record_ids == ["a", "b"]


def test_a_record_without_a_study_identifier_can_still_join_a_group():
    """Only a *disagreement* blocks a merge; silence does not."""
    anchored = _encode_record("a", "GSE340993", "snATAC-seq from mucosa")
    unanchored = SourceRecord(
        record_id="b",
        source_name="europe_pmc",
        external_id="b",
        title="Paper citing the deposition",
        identifiers=[Identifier(namespace="bioproject", value="PRJNA63443")],
        access_level=AccessLevel.PUBLIC_DECLARED,
    )

    candidates = merge_source_records([anchored, unanchored])

    assert len(candidates) == 1
    assert candidates[0].source_record_ids == ["a", "b"]


def test_merge_outcome_does_not_depend_on_input_order():
    records = [
        _encode_record("a", "GSE340993", "first"),
        _encode_record("b", "GSE340999", "second"),
        _encode_record("c", "GSE340993", "third"),
    ]

    def grouping(values: list[SourceRecord]) -> set[tuple[str, ...]]:
        # Candidates come back ordered by candidate_id, so compare the grouping
        # itself rather than the sequence it happens to be emitted in.
        return {
            tuple(sorted(item.source_record_ids))
            for item in merge_source_records(values)
        }

    assert grouping(records) == {("a", "c"), ("b",)}
    assert grouping(list(reversed(records))) == {("a", "c"), ("b",)}


def test_short_aliases_do_not_match_inside_longer_words():
    """`adt` is a protein alias; `adtDNA` and friends must not trigger it."""
    profiles = infer_assay_profiles((
        "scATAC-seq of tissue",
        "Reads were adtrimmed before alignment.",
    ))

    assert profiles[0].label == "standalone_single_cell_atac"
