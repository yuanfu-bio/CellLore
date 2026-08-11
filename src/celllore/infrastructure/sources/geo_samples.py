"""Fetch and parse per-sample (GSM) GEO metadata for one series.

``acc.cgi?targ=self`` (used by ``asset_registry.GEOSupplementaryAssetResolver``)
returns only series-level fields plus a bare list of ``!Series_sample_id``
accessions -- no titles, characteristics, or per-sample file lists.
``acc.cgi?targ=gsm`` on the same series returns every sample's full metadata
block (title/characteristics/source_name/organism/supplementary files) in one
request, verified directly against a real GEO series. This is what lets
``PairUnitResolver`` reason about which files belong to which patient/sample
from real text instead of guessing from GSM accession-number adjacency.
"""

from __future__ import annotations

import re

import httpx

from celllore.domain.models import GeoSampleRecord
from celllore.infrastructure.sources.base import (
    default_async_client,
    request_with_retry,
)

GEO_ACC_URL = "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi"

_SAMPLE_HEADER_RE = re.compile(r"^\^SAMPLE\s*=\s*(GSM\d+)", re.MULTILINE)
_FIELD_RE = re.compile(r"^!Sample_(\S+?)\s*=\s*(.*)$", re.MULTILINE)


async def fetch_series_samples(
    series_accession: str, *, client: httpx.AsyncClient | None = None
) -> list[GeoSampleRecord]:
    """Fetch every GSM sample block for one GEO series in a single request."""
    owns_client = client is None
    http_client = client or default_async_client()
    try:
        response = await request_with_retry(
            http_client,
            "GET",
            GEO_ACC_URL,
            params={
                "acc": series_accession,
                "targ": "gsm",
                "form": "text",
                "view": "full",
            },
        )
    finally:
        if owns_client:
            await http_client.aclose()
    return parse_sample_blocks(response.text, series_accession)


def parse_sample_blocks(text: str, series_accession: str) -> list[GeoSampleRecord]:
    """Split a GEO ``acc.cgi?targ=gsm`` response into per-sample records."""
    headers = list(_SAMPLE_HEADER_RE.finditer(text))
    records: list[GeoSampleRecord] = []
    for index, match in enumerate(headers):
        start = match.start()
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        records.append(
            _parse_block(match.group(1), series_accession, text[start:end])
        )
    return records


def _parse_block(
    gsm_accession: str, series_accession: str, block: str
) -> GeoSampleRecord:
    title: str | None = None
    source_name: str | None = None
    organism: str | None = None
    characteristics: list[str] = []
    supplementary_files: list[str] = []
    for field_match in _FIELD_RE.finditer(block):
        key, value = field_match.group(1), field_match.group(2).strip()
        if key == "title" and title is None:
            title = value
        elif key.startswith("source_name") and source_name is None:
            source_name = value
        elif key.startswith("organism") and organism is None:
            organism = value
        elif key.startswith("characteristics"):
            characteristics.append(value)
        elif key.startswith("supplementary_file"):
            supplementary_files.append(value)
    return GeoSampleRecord(
        gsm_accession=gsm_accession,
        series_accession=series_accession,
        title=title,
        source_name=source_name,
        organism=organism,
        characteristics=characteristics,
        supplementary_files=supplementary_files,
        raw_text=block.strip(),
    )
