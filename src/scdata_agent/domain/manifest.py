"""Dialect-driven parsing of a human-curated file-level acquisition manifest.

A curated manifest is the other way data enters this system: instead of the
agent discovering a study, a human has already audited one down to the file and
written the result in a spreadsheet. Everything that follows -- preflight,
approval, transfer, curation, governance -- is the same path a discovered
candidate takes.

The shape of the spreadsheet is *data*, not code. The reference implementation
this replaces hard-coded one set of column headers as module globals, which
made a second manifest layout a rewrite rather than a registration. A
``ManifestDialect`` names the columns and the controlled vocabularies; adding a
new one adds an instance, not a branch.

Two rules keep a manifest honest, and both are enforced here rather than left
to a convention:

* The role vocabulary is **total**. An unrecognised file role is an error, not
  a silent fall-through to ``OTHER`` -- otherwise a new dialect degrades
  quietly into a pile of unclassified files.
* The curator's role wins over the file name. Filename inference still runs,
  and a disagreement is reported, which makes the manifest a standing
  regression test for ``domain.assets.ROLE_PATTERNS``.
"""

from __future__ import annotations

from collections import Counter
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any

from pydantic import Field

from scdata_agent.domain.assets import infer_data_role
from scdata_agent.domain.models import DataRole, StrictModel
from scdata_agent.domain.repository_urls import (
    canonical_assembly,
    geo_static_url,
    is_geo_accession,
)

MANIFEST_SCHEMA_VERSION = "1.0"

#: The English field names every dialect is normalized onto.
CANONICAL_FIELDS: tuple[str, ...] = (
    "dataset_id",
    "pair_unit_id",
    "accession",
    "modality",
    "file_role",
    "file_name",
    "size_value",
    "size_unit",
    "size_gb",
    "declared_checksum",
    "checksum_policy",
    "url_status",
    "url",
    "relative_path",
    "genome_build",
    "pairing_key",
    "post_download_action",
    "audit_status",
    "stage",
    "license_id",
    "license_url",
)

#: Fields without which a row cannot be turned into a downloadable asset.
REQUIRED_FIELDS: tuple[str, ...] = (
    "dataset_id",
    "pair_unit_id",
    "accession",
    "modality",
    "file_role",
    "file_name",
    "url_status",
    "url",
    "relative_path",
    "genome_build",
)

_BYTES_PER_UNIT: dict[str, int] = {
    "B": 1,
    "KB": 1024,
    "MB": 1024**2,
    "GB": 1024**3,
    "TB": 1024**4,
}

#: Assemblies this project distinguishes. Anything else is an error, because a
#: fragment file whose coordinate system is unknown cannot be joined to
#: anything.
SUPPORTED_ASSEMBLIES = frozenset({"GRCh37", "GRCh38"})


class IssueSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class ManifestIssue(StrictModel):
    severity: IssueSeverity
    code: str
    message: str
    row_index: int | None = None
    dataset_id: str | None = None
    pair_unit_id: str | None = None
    file_name: str | None = None


class ManifestDialect(StrictModel):
    """How one spreadsheet layout maps onto the canonical fields."""

    dialect_id: str
    label: str
    #: canonical field name -> source column header
    columns: dict[str, str]
    #: native file-role token -> semantic role. Must cover every token used.
    role_vocabulary: dict[str, DataRole]
    #: native modality token -> a coarse layer label used only for consistency
    #: checks. It deliberately does not produce a ``Modality``: claiming an
    #: assay from a spreadsheet cell is how modality overclaims start.
    modality_vocabulary: dict[str, str]
    direct_url_statuses: frozenset[str]
    manual_url_statuses: frozenset[str]
    verified_audit_statuses: frozenset[str]
    #: URL statuses whose URL must be recomputed from the accession.
    geo_url_statuses: frozenset[str] = frozenset()
    schema_version: str = MANIFEST_SCHEMA_VERSION

    def header_for(self, field: str) -> str | None:
        return self.columns.get(field)

    def required_headers(self) -> list[str]:
        return [self.columns[field] for field in REQUIRED_FIELDS if field in self.columns]


class ManifestRow(StrictModel):
    """One normalized manifest line: exactly one file to acquire."""

    source_row_index: int
    dataset_id: str
    pair_unit_id: str
    accession: str
    modality: str
    file_role: str
    role: DataRole
    file_name: str
    relative_path: str
    url: str
    url_status: str
    size_bytes: int | None = None
    declared_checksum: str | None = None
    checksum_algorithm: str | None = None
    checksum_policy: str | None = None
    genome_build: str | None = None
    pairing_key: str | None = None
    post_download_action: str | None = None
    audit_status: str | None = None
    stage: str | None = None
    license_id: str | None = None
    license_url: str | None = None
    is_direct: bool = False
    is_manual: bool = False
    is_curator_verified: bool = False
    #: The source line verbatim, so a claim can be traced back to what the
    #: curator actually wrote rather than to our interpretation of it.
    raw: dict[str, str] = Field(default_factory=dict)
    schema_version: str = MANIFEST_SCHEMA_VERSION

    @property
    def pair_unit_key(self) -> tuple[str, str]:
        return (self.dataset_id, self.pair_unit_id)


class ManifestDocument(StrictModel):
    source_path: str
    source_sha256: str
    dialect_id: str
    headers: list[str] = Field(default_factory=list)
    rows: list[ManifestRow] = Field(default_factory=list)
    issues: list[ManifestIssue] = Field(default_factory=list)
    schema_version: str = MANIFEST_SCHEMA_VERSION

    @property
    def has_errors(self) -> bool:
        return any(item.severity == IssueSeverity.ERROR for item in self.issues)

    @property
    def pair_unit_count(self) -> int:
        return len({row.pair_unit_key for row in self.rows})


class ManifestFormatError(ValueError):
    """The file could not be read as a manifest at all."""


# --------------------------------------------------------------------------
# The paired multiome dialect: the Chinese-headed CSV this project starts from
# --------------------------------------------------------------------------

_ZH_COLUMNS: dict[str, str] = {
    "dataset_id": "dataset_id",
    "pair_unit_id": "pair_unit_id",
    "accession": "GSM/文件accession",
    "modality": "模态",
    "file_role": "文件角色",
    "file_name": "文件名",
    "size_value": "大小",
    "size_unit": "单位",
    "size_gb": "大小_GB",
    "declared_checksum": "仓库MD5",
    "checksum_policy": "MD5策略",
    "url_status": "URL状态",
    "url": "下载URL",
    "relative_path": "本地相对路径",
    "genome_build": "参考基因组",
    "pairing_key": "配对键",
    "post_download_action": "下载后动作",
    "audit_status": "审计状态",
    "stage": "下载阶段",
    "license_id": "licence_id",
    "license_url": "licence_url",
}

# Keyed on the role alone. In the source manifest the (modality, role) pair is
# a clean functional dependency -- every role appears under exactly one
# modality -- so modality is validated as a cross-check rather than used as a
# lookup key.
_ZH_ROLES: dict[str, DataRole] = {
    "fragment": DataRole.FRAGMENTS,
    # Fragments emitted before cell calling. Still fragments; that they are
    # pre-filter matters for barcode-overlap interpretation, not for the role.
    "fragment_full": DataRole.FRAGMENTS,
    "tabix_index": DataRole.FRAGMENT_INDEX,
    "filtered_mtx_matrix": DataRole.RNA_MATRIX,
    "raw_mtx_matrix": DataRole.RNA_MATRIX,
    # A 10x ``.h5`` count matrix, not an AnnData object.
    "filtered_integer_count_h5": DataRole.RNA_MATRIX,
    "raw_count": DataRole.RNA_MATRIX,
    "raw_count_h5": DataRole.RNA_MATRIX,
    "filtered_mtx_barcodes": DataRole.CELL_METADATA,
    "raw_mtx_barcodes": DataRole.CELL_METADATA,
    "cell_metadata": DataRole.CELL_METADATA,
    "atac_cell_metadata": DataRole.CELL_METADATA,
    "rna_cell_metadata": DataRole.CELL_METADATA,
    # DataRole has no member for a MEX feature/gene table. OTHER keeps the file
    # in the bundle without inventing a role; the native token survives on the
    # row, which is what the MEX dimension check reads.
    "filtered_mtx_features": DataRole.OTHER,
    "raw_mtx_features": DataRole.OTHER,
    "donor_metadata": DataRole.DONOR_METADATA,
    "clinical_metadata": DataRole.SAMPLE_METADATA,
    "per_barcode_metrics": DataRole.QC_METRICS,
    "web_summary": DataRole.QC_METRICS,
    "multiome_archive": DataRole.ARCHIVE,
}

_ZH_MODALITIES: dict[str, str] = {
    "RNA": "transcriptome",
    "ATAC": "chromatin_accessibility",
    "ADT": "protein",
    "META": "metadata",
    "BUNDLE": "archive",
}

#: Which semantic roles each modality token is allowed to carry. Used only to
#: report a disagreement; the curator's role still wins.
_ZH_MODALITY_ROLES: dict[str, frozenset[DataRole]] = {
    "RNA": frozenset({DataRole.RNA_MATRIX, DataRole.CELL_METADATA, DataRole.OTHER}),
    "ATAC": frozenset(
        {DataRole.FRAGMENTS, DataRole.FRAGMENT_INDEX, DataRole.PEAKS, DataRole.QC_METRICS}
    ),
    "ADT": frozenset({DataRole.PROTEIN_MATRIX, DataRole.CELL_METADATA}),
    "META": frozenset(
        {
            DataRole.CELL_METADATA,
            DataRole.SAMPLE_METADATA,
            DataRole.DONOR_METADATA,
            DataRole.QC_METRICS,
            DataRole.OTHER,
        }
    ),
    "BUNDLE": frozenset({DataRole.ARCHIVE}),
}

PAIRED_MULTIOME_ZH_V1 = ManifestDialect(
    dialect_id="paired_multiome_zh_v1",
    label="Human paired scRNA/scATAC required-files manifest (Chinese headers)",
    columns=_ZH_COLUMNS,
    role_vocabulary=_ZH_ROLES,
    modality_vocabulary=_ZH_MODALITIES,
    direct_url_statuses=frozenset({"DIRECT_GEO", "DIRECT_ZENODO", "DIRECT_HTTPS"}),
    manual_url_statuses=frozenset({"PORTAL_CLICK_REQUIRED"}),
    verified_audit_statuses=frozenset({"VERIFIED"}),
    geo_url_statuses=frozenset({"DIRECT_GEO"}),
)

DIALECTS: tuple[ManifestDialect, ...] = (PAIRED_MULTIOME_ZH_V1,)


def dialect_by_id(dialect_id: str) -> ManifestDialect:
    for dialect in DIALECTS:
        if dialect.dialect_id == dialect_id:
            return dialect
    known = ", ".join(item.dialect_id for item in DIALECTS)
    raise ManifestFormatError(f"Unknown manifest dialect {dialect_id!r}. Known: {known}.")


def detect_dialect(headers: list[str]) -> ManifestDialect | None:
    """Return the one dialect whose required columns are all present.

    Ambiguity returns ``None`` rather than a guess: picking the wrong dialect
    silently mismaps every column, and an explicit ``--dialect`` is cheap.
    """
    present = {item.strip() for item in headers}
    matches = [
        dialect
        for dialect in DIALECTS
        if all(header in present for header in dialect.required_headers())
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def parse_size(value: str | None, unit: str | None, size_gb: str | None = None) -> int | None:
    """Convert a declared size to bytes, or ``None`` when it was not declared.

    ``None`` is not zero. The reference implementation treated a blank size as
    small, which quietly routed unsized multi-gigabyte files into the
    high-concurrency queue meant for sidecar files.
    """
    gigabytes = _to_float(size_gb)
    if gigabytes is not None:
        return round(gigabytes * _BYTES_PER_UNIT["GB"])
    amount = _to_float(value)
    if amount is None:
        return None
    factor = _BYTES_PER_UNIT.get((unit or "").strip().upper())
    if factor is None:
        return None
    return round(amount * factor)


def normalize_row(
    dialect: ManifestDialect, raw: dict[str, str], row_index: int
) -> tuple[ManifestRow | None, list[ManifestIssue]]:
    """Map one source line onto a ``ManifestRow``, collecting its issues.

    Returns ``(None, issues)`` when the line cannot be represented at all --
    a missing required field or an unknown file role. Everything else is
    reported but still produces a row, so a single bad cell does not hide the
    other 129 files from review.
    """
    issues: list[ManifestIssue] = []
    values = {field: _cell(raw, dialect.header_for(field)) for field in CANONICAL_FIELDS}
    dataset_id = values["dataset_id"]
    pair_unit_id = values["pair_unit_id"]
    file_name = values["file_name"]

    def issue(severity: IssueSeverity, code: str, message: str) -> ManifestIssue:
        return ManifestIssue(
            severity=severity,
            code=code,
            message=message,
            row_index=row_index,
            dataset_id=dataset_id or None,
            pair_unit_id=pair_unit_id or None,
            file_name=file_name or None,
        )

    missing = [field for field in REQUIRED_FIELDS if not values[field]]
    if missing:
        issues.append(
            issue(
                IssueSeverity.ERROR,
                "missing_required_field",
                f"Required field(s) empty: {', '.join(missing)}.",
            )
        )
        return None, issues

    native_role = values["file_role"]
    role = dialect.role_vocabulary.get(native_role)
    if role is None:
        issues.append(
            issue(
                IssueSeverity.ERROR,
                "unknown_file_role",
                (
                    f"File role {native_role!r} is not in the {dialect.dialect_id} "
                    "vocabulary. Extend the dialect rather than defaulting the role."
                ),
            )
        )
        return None, issues

    modality = values["modality"]
    if modality not in dialect.modality_vocabulary:
        issues.append(
            issue(
                IssueSeverity.WARNING,
                "unknown_modality",
                f"Modality {modality!r} is not in the {dialect.dialect_id} vocabulary.",
            )
        )
    else:
        allowed = _ZH_MODALITY_ROLES.get(modality)
        if allowed is not None and role not in allowed:
            issues.append(
                issue(
                    IssueSeverity.WARNING,
                    "modality_role_mismatch",
                    f"Role {native_role!r} ({role.value}) is unusual for modality {modality!r}.",
                )
            )

    # The transfer executor accepts only a bare basename; the manifest's own
    # relative path travels separately so the curator's tree can be honoured.
    if PurePosixPath(file_name).name != file_name:
        issues.append(
            issue(
                IssueSeverity.ERROR,
                "file_name_not_basename",
                f"File name {file_name!r} must be a bare basename, not a path.",
            )
        )
        return None, issues

    genome_build = canonical_assembly(values["genome_build"])
    if genome_build not in SUPPORTED_ASSEMBLIES:
        issues.append(
            issue(
                IssueSeverity.ERROR,
                "unsupported_assembly",
                (
                    f"Reference {values['genome_build']!r} does not normalize to "
                    f"one of {sorted(SUPPORTED_ASSEMBLIES)}."
                ),
            )
        )
        return None, issues

    url_status = values["url_status"]
    url = values["url"]
    accession = values["accession"]
    if url_status in dialect.geo_url_statuses:
        if not is_geo_accession(accession):
            issues.append(
                issue(
                    IssueSeverity.ERROR,
                    "unparsable_geo_accession",
                    f"URL status {url_status!r} requires a GEO accession; got {accession!r}.",
                )
            )
            return None, issues
        computed = geo_static_url(accession, file_name)
        if computed != url:
            issues.append(
                issue(
                    IssueSeverity.WARNING,
                    "geo_url_recomputed",
                    f"Declared URL {url!r} replaced by the derived static URL {computed!r}.",
                )
            )
        url = computed

    if url_status in dialect.manual_url_statuses:
        issues.append(
            issue(
                IssueSeverity.INFO,
                "portal_resolution_required",
                (
                    "The URL is a portal landing page, not a file. This row is "
                    "visible but cannot be transferred automatically."
                ),
            )
        )
    elif url_status not in dialect.direct_url_statuses:
        issues.append(
            issue(
                IssueSeverity.WARNING,
                "unknown_url_status",
                f"URL status {url_status!r} is neither a direct nor a manual status.",
            )
        )

    inferred = infer_data_role(file_name)
    if inferred is not None and inferred != role:
        issues.append(
            issue(
                IssueSeverity.WARNING,
                "role_inference_disagreement",
                (
                    f"Manifest role {role.value} kept; filename inference said "
                    f"{inferred.value}. One of the two is wrong."
                ),
            )
        )

    checksum = _normalize_md5(values["declared_checksum"])
    if values["declared_checksum"] and checksum is None:
        issues.append(
            issue(
                IssueSeverity.WARNING,
                "unusable_checksum",
                f"Declared checksum {values['declared_checksum']!r} is not a 32-hex MD5.",
            )
        )

    row = ManifestRow(
        source_row_index=row_index,
        dataset_id=dataset_id,
        pair_unit_id=pair_unit_id,
        accession=accession,
        modality=modality,
        file_role=native_role,
        role=role,
        file_name=file_name,
        relative_path=values["relative_path"],
        url=url,
        url_status=url_status,
        size_bytes=parse_size(values["size_value"], values["size_unit"], values["size_gb"]),
        declared_checksum=checksum,
        checksum_algorithm="md5" if checksum else None,
        checksum_policy=values["checksum_policy"] or None,
        genome_build=genome_build,
        pairing_key=values["pairing_key"] or None,
        post_download_action=values["post_download_action"] or None,
        audit_status=values["audit_status"] or None,
        stage=values["stage"] or None,
        license_id=values["license_id"] or None,
        license_url=values["license_url"] or None,
        is_direct=url_status in dialect.direct_url_statuses,
        is_manual=url_status in dialect.manual_url_statuses,
        is_curator_verified=values["audit_status"] in dialect.verified_audit_statuses,
        raw=dict(raw),
    )
    return row, issues


def preflight_manifest(rows: list[ManifestRow]) -> list[ManifestIssue]:
    """Cross-row checks that a single line cannot detect on its own."""
    issues: list[ManifestIssue] = []
    by_path = Counter(row.relative_path for row in rows)
    for row in rows:
        if by_path[row.relative_path] > 1:
            issues.append(
                ManifestIssue(
                    severity=IssueSeverity.ERROR,
                    code="duplicate_local_path",
                    message=(
                        f"Local path {row.relative_path!r} is claimed by "
                        f"{by_path[row.relative_path]} rows; they would overwrite each other."
                    ),
                    row_index=row.source_row_index,
                    dataset_id=row.dataset_id,
                    pair_unit_id=row.pair_unit_id,
                    file_name=row.file_name,
                )
            )
    for (dataset_id, pair_unit_id), builds in _builds_by_pair_unit(rows).items():
        if len(builds) > 1:
            issues.append(
                ManifestIssue(
                    severity=IssueSeverity.WARNING,
                    code="mixed_assembly_in_pair_unit",
                    message=(
                        f"Pair unit declares more than one assembly: {sorted(builds)}. "
                        "Coordinates from different builds cannot be compared directly."
                    ),
                    dataset_id=dataset_id,
                    pair_unit_id=pair_unit_id,
                )
            )
    return issues


def summarize_issues(issues: list[ManifestIssue]) -> dict[str, int]:
    counts = Counter(item.severity.value for item in issues)
    return {severity.value: counts.get(severity.value, 0) for severity in IssueSeverity}


def _builds_by_pair_unit(rows: list[ManifestRow]) -> dict[tuple[str, str], set[str]]:
    grouped: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        if row.genome_build:
            grouped.setdefault(row.pair_unit_key, set()).add(row.genome_build)
    return grouped


def _cell(raw: dict[str, Any], header: str | None) -> str:
    if header is None:
        return ""
    value = raw.get(header)
    if value is None:
        return ""
    return str(value).strip()


def _to_float(value: str | None) -> float | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _normalize_md5(value: str) -> str | None:
    text = (value or "").strip().casefold()
    if len(text) != 32:
        return None
    if any(char not in "0123456789abcdef" for char in text):
        return None
    return text
