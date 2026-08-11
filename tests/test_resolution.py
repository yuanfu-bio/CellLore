from celllore.domain.identifiers import extract_identifiers
from celllore.domain.models import (
    AccessLevel,
    Identifier,
    SourceRecord,
)
from celllore.domain.resolution import merge_source_records


def test_extract_common_accessions():
    text = "Data are in GSE12345, PRJNA765432 and SRR987654."
    values = {identifier.value for identifier in extract_identifiers(text)}
    assert values == {"GSE12345", "PRJNA765432", "SRR987654"}


def test_merge_records_with_shared_identifier():
    left = SourceRecord(
        record_id="left",
        source_name="europe_pmc",
        external_id="1",
        title="Paper",
        identifiers=[Identifier(namespace="geo_series", value="GSE12345")],
        access_level=AccessLevel.PUBLIC_DECLARED,
    )
    right = SourceRecord(
        record_id="right",
        source_name="ncbi_geo",
        external_id="GSE12345",
        title="Repository record",
        identifiers=[Identifier(namespace="geo_series", value="GSE12345")],
        access_level=AccessLevel.PUBLIC_DECLARED,
    )
    candidates = merge_source_records([left, right])

    assert len(candidates) == 1
    assert candidates[0].source_record_ids == ["left", "right"]
    assert candidates[0].title == "Repository record"

