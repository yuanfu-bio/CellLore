"""GEO static URL construction, checked against real accessions and filenames.

Every case here uses an accession and a supplementary file name taken from the
curated manifest, which is why it catches bucket-arithmetic mistakes that
hand-invented accessions would not.
"""

from __future__ import annotations

import pytest

from celllore.domain.repository_urls import (
    canonical_assembly,
    geo_bucket,
    geo_namespace,
    geo_static_url,
    is_geo_accession,
)


def test_sample_url_for_a_real_bmmc_fragment_file() -> None:
    assert geo_static_url(
        "GSM6284346", "GSM6284346_BMMC.ATAC.hg19.fragments.tsv.gz"
    ) == (
        "https://ftp.ncbi.nlm.nih.gov/geo/samples/GSM6284nnn/GSM6284346/suppl/"
        "GSM6284346_BMMC.ATAC.hg19.fragments.tsv.gz"
    )


def test_series_url_routes_to_the_series_tree() -> None:
    assert geo_static_url(
        "GSE234788", "GSE234788_GEO_RNA_446267cells_meta_clinical_data.csv.gz"
    ) == (
        "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE234nnn/GSE234788/suppl/"
        "GSE234788_GEO_RNA_446267cells_meta_clinical_data.csv.gz"
    )


@pytest.mark.parametrize(
    ("accession", "bucket"),
    [
        ("GSM5396336", "GSM5396nnn"),
        ("GSE178707", "GSE178nnn"),
        # Three digits or fewer share one bucket rather than producing "nnn"
        # with an empty prefix.
        ("GSM123", "GSMnnn"),
        ("GSE1", "GSEnnn"),
        ("GSM1000", "GSM1nnn"),
    ],
)
def test_bucket_arithmetic(accession: str, bucket: str) -> None:
    assert geo_bucket(accession) == bucket


def test_accession_case_and_whitespace_are_normalized() -> None:
    assert geo_static_url(" gsm5396336 ", "x.tsv.gz").endswith(
        "/geo/samples/GSM5396nnn/GSM5396336/suppl/x.tsv.gz"
    )


def test_spaces_in_a_file_name_are_percent_encoded() -> None:
    url = geo_static_url("GSM5396336", "lane 2 fragments.tsv.gz")
    assert url.endswith("/suppl/lane%202%20fragments.tsv.gz")


def test_ftp_protocol_is_supported_for_slow_https_routes() -> None:
    assert geo_static_url("GSM5396336", "x.gz", protocol="ftp").startswith(
        "ftp://ftp.ncbi.nlm.nih.gov/"
    )


@pytest.mark.parametrize("accession", ["SRR123", "PRJNA63443", "", "GSM", "12345"])
def test_non_geo_accessions_are_refused_rather_than_guessed(accession: str) -> None:
    assert not is_geo_accession(accession)
    with pytest.raises(ValueError):
        geo_static_url(accession, "x.gz")


def test_unsupported_protocol_is_refused() -> None:
    with pytest.raises(ValueError, match="protocol"):
        geo_static_url("GSM5396336", "x.gz", protocol="gopher")


def test_namespaces_match_the_identifier_vocabulary() -> None:
    assert geo_namespace("GSM5396336") == "geo_sample"
    assert geo_namespace("GSE178707") == "geo_series"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("GRCh38", "GRCh38"),
        ("hg38", "GRCh38"),
        ("GRCh38 (10x refdata-cellranger-arc-GRCh38-2020-A)", "GRCh38"),
        ("GRCh37", "GRCh37"),
        ("hg19", "GRCh37"),
        ("", None),
        (None, None),
        # Anything unrecognised is returned unchanged so the caller can refuse
        # it by name rather than silently defaulting to a build.
        ("T2T-CHM13", "T2T-CHM13"),
    ],
)
def test_canonical_assembly(value: str | None, expected: str | None) -> None:
    assert canonical_assembly(value) == expected
