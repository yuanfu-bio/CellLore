"""Human-readable dataset identities and production-tree layout helpers."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from scdata_agent.domain.lifecycle import SemanticBundle
from scdata_agent.domain.models import DataAsset, DataRole, DatasetCandidate
from scdata_agent.domain.pair_units import ProposedPairUnit

_GSM_RE = re.compile(r"GSM\d+", re.IGNORECASE)
_PAIR_LABEL_RE = re.compile(
    r"\b(patient|donor|subject|sample|replicate)\s*[-_:#]?\s*([a-z0-9.-]+)",
    re.IGNORECASE,
)
_CONDITION_LABEL_RE = re.compile(
    r"\b(batch|condition|group|treatment)\s*[:#=_-]?\s*([a-z0-9α-ωΑ-Ω.-]+)",
    re.IGNORECASE,
)

_ATAC_ROLES = {
    DataRole.FRAGMENTS,
    DataRole.FRAGMENT_INDEX,
    DataRole.PEAKS,
    DataRole.PEAK_MATRIX,
}
_RNA_ROLES = {DataRole.RNA_MATRIX, DataRole.STANDARD_OBJECT}
_METADATA_ROLES = {
    DataRole.CELL_METADATA,
    DataRole.SAMPLE_METADATA,
    DataRole.DONOR_METADATA,
    DataRole.BARCODE_LINKAGE,
    DataRole.QC_METRICS,
}


def safe_path_segment(value: str, *, fallback: str = "unknown") -> str:
    """Return one filesystem-safe, readable path segment."""
    cleaned = re.sub(r"[^\w.-]+", "_", value.strip(), flags=re.UNICODE).strip("._")
    return (cleaned or fallback)[:120]


def production_run_root(
    data_root: str | Path,
    run_id: str,
    created_at: datetime | None,
) -> Path:
    """Return a production root isolated by UTC creation date and run ID."""
    stamp = (
        created_at.astimezone(UTC).date().isoformat()
        if created_at is not None
        else "undated"
    )
    run_segment = safe_path_segment(run_id, fallback="run")
    return Path(data_root) / "production" / f"{stamp}_RUN_{run_segment}"


def study_accession(candidate: DatasetCandidate) -> str:
    for identifier in candidate.identifiers:
        if identifier.namespace.casefold() == "geo_series":
            return safe_path_segment(identifier.value.upper(), fallback=candidate.candidate_id)
    return safe_path_segment(candidate.candidate_id, fallback="dataset")


def readable_pair_unit_id(
    bundle: SemanticBundle,
    unit: ProposedPairUnit | None,
    assets: list[DataAsset],
) -> str:
    """Build a curator-facing identity while leaving internal IDs untouched.

    The preferred shape mirrors the reference workbook, for example
    ``Patient3_GSM9196533__GSM9196532``.  ATAC accessions precede RNA
    accessions so the same pair is named consistently in tables and on disk.
    """
    label = _pair_label(unit, assets)
    atac = _gsms_for_roles(assets, _ATAC_ROLES)
    rna = _gsms_for_roles(assets, _RNA_ROLES)
    members = [item.upper() for item in (unit.member_samples if unit else []) if _GSM_RE.fullmatch(item)]
    accessions = list(dict.fromkeys([*atac, *rna, *members]))
    if accessions:
        return safe_path_segment(f"{label or 'Pair'}_{'__'.join(accessions)}")
    if label:
        return safe_path_segment(label)
    return "WholeStudy"


def raw_stage_for_role(role: DataRole) -> str:
    if role in _METADATA_ROLES:
        return "01_metadata"
    if role in _RNA_ROLES:
        return "02_rna_raw_counts"
    if role in _ATAC_ROLES:
        return "03_atac_fragments"
    if role is DataRole.RAW_READS:
        return "99_raw_optional"
    return "05_auxiliary"


def _pair_label(unit: ProposedPairUnit | None, assets: list[DataAsset]) -> str:
    if unit is None:
        return ""
    for value in (
        unit.donor_interpretation,
        unit.sample_interpretation,
        unit.library_interpretation,
    ):
        match = _PAIR_LABEL_RE.search(value or "")
        if match:
            return f"{match.group(1).capitalize()}{match.group(2)}"
    for value in (
        unit.sample_interpretation,
        unit.donor_interpretation,
        unit.grouping_rationale,
    ):
        match = _CONDITION_LABEL_RE.search(value or "")
        if match:
            return f"{match.group(1).capitalize()}{match.group(2)}"
    if structured := _structured_pair_label(unit, assets):
        return structured
    for value in (unit.sample_interpretation, unit.donor_interpretation):
        if value:
            return safe_path_segment(re.split(r"[;+|,]", value, maxsplit=1)[0])
    return ""


def _structured_pair_label(
    unit: ProposedPairUnit,
    assets: list[DataAsset],
) -> str:
    library = safe_path_segment(unit.library_interpretation, fallback="")
    library = re.sub(r"^10x_", "", library, flags=re.IGNORECASE)
    library = library.replace("-seq", "seq").replace("_library", "")
    text = " ".join(
        [unit.sample_interpretation, *(asset.file_name for asset in assets)]
    )
    material = "nuclei" if re.search(r"\bnucle(?:i|us)\b", text, re.IGNORECASE) else "PBMC" if re.search(r"\bPBMCs?\b|leukopak", text, re.IGNORECASE) else ""
    well_match = re.search(r"\bwell\s*(\d+)\b", text, re.IGNORECASE)
    if well_match is None:
        well_match = re.search(r"W(\d+)(?=[_.-]|$)", text, re.IGNORECASE)
    well = f"W{well_match.group(1)}" if well_match else ""
    return "_".join(item for item in (library, material, well) if item)


def _gsms_for_roles(assets: list[DataAsset], roles: set[DataRole]) -> list[str]:
    values = {
        match.group().upper()
        for asset in assets
        if asset.role in roles
        for match in _GSM_RE.finditer(asset.file_name)
    }
    return sorted(values)
