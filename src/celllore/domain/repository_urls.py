"""Deterministic repository URL construction and assembly normalization.

A curated manifest carries a download URL typed by a human. We recompute the
GEO one instead of trusting it: the accession and the file name are the facts
the curator actually checked, and the URL is mechanically derived from them. A
disagreement is then a signal worth reporting rather than a silent 404 later.
"""

from __future__ import annotations

import re
from urllib.parse import quote

GEO_FTP_HOST = "ftp.ncbi.nlm.nih.gov"

_ACCESSION_RE = re.compile(r"([A-Za-z]+)(\d+)")

# GEO shards its FTP tree by accession prefix: GSM5396336 lives under
# GSM5396nnn. Anything with three digits or fewer shares a single bucket.
_GEO_KINDS = {"GSM": "samples", "GSE": "series"}


def geo_bucket(accession: str) -> str:
    """Return the FTP shard directory for a GEO accession, e.g. ``GSM5396nnn``."""
    prefix, digits = _split_accession(accession)
    if len(digits) <= 3:
        return f"{prefix}nnn"
    return f"{prefix}{digits[:-3]}nnn"


def geo_static_url(accession: str, file_name: str, protocol: str = "https") -> str:
    """Build the static FTP URL for a GEO supplementary file.

    The interactive ``/geo/download?...`` endpoint is deliberately never used:
    it is a click-through page, not a file, and it does not survive concurrent
    or resumed transfers.
    """
    if protocol not in {"https", "ftp"}:
        raise ValueError(f"Unsupported protocol for GEO: {protocol!r}")
    prefix, _ = _split_accession(accession)
    kind = _GEO_KINDS.get(prefix)
    if kind is None:
        raise ValueError(f"Unsupported GEO prefix: {prefix!r}")
    bucket = geo_bucket(accession)
    return (
        f"{protocol}://{GEO_FTP_HOST}/geo/{kind}/"
        f"{bucket}/{accession.strip().upper()}/suppl/{quote(file_name)}"
    )


def is_geo_accession(value: str) -> bool:
    """True when the value names a GEO sample or series."""
    try:
        prefix, _ = _split_accession(value)
    except ValueError:
        return False
    return prefix in _GEO_KINDS


def geo_namespace(accession: str) -> str:
    """Identifier namespace for a GEO accession, matching ``domain.identifiers``."""
    prefix, _ = _split_accession(accession)
    if prefix == "GSM":
        return "geo_sample"
    if prefix == "GSE":
        return "geo_series"
    raise ValueError(f"Unsupported GEO prefix: {prefix!r}")


def canonical_assembly(value: str | None) -> str | None:
    """Collapse a reference declaration to GRCh37 or GRCh38.

    Only the major build is normalized. GENCODE, Ensembl and Cell Ranger
    reference-package versions are deliberately not modelled here: they matter
    for reproducing an analysis, not for deciding whether two fragment files
    share a coordinate system.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    low = text.casefold()
    if "grch37" in low or "hg19" in low:
        return "GRCh37"
    if "grch38" in low or "hg38" in low:
        return "GRCh38"
    return text


def _split_accession(value: str) -> tuple[str, str]:
    match = _ACCESSION_RE.fullmatch((value or "").strip())
    if match is None:
        raise ValueError(f"Unsupported repository accession: {value!r}")
    prefix, digits = match.groups()
    return prefix.upper(), digits
