"""Dependency-light local QC metrics for processed scATAC/multiome bundles."""

from __future__ import annotations

import gzip
from bisect import bisect_left
from collections import defaultdict
from pathlib import Path
from typing import TextIO

from celllore.domain.lifecycle import (
    MaterializationManifest,
    TransferReceipt,
)
from celllore.domain.models import DataRole, GateStatus
from celllore.infrastructure.standard_object_qc import (
    inspect_10x_h5_counts,
    read_standard_object_barcodes,
)

# A loose sanity bound, not a strict identity check: an ATAC fragments file is
# typically pre-cell-calling and carries far more barcodes than an RNA matrix
# filtered down to called cells, so `frac_atac_matched` is expected to be
# small even for a genuinely paired sample. `frac_rna_matched` -- how many of
# the RNA-side (already-filtered) cells also show ATAC signal at the same
# barcode -- is the semantically meaningful direction and the one this
# threshold gates on.
_BARCODE_OVERLAP_MIN_FRACTION = 0.5
_BARCODE_SAMPLE_LIMIT = 500_000


def compute_role_metrics(
    manifest: MaterializationManifest,
    receipts: list[TransferReceipt],
) -> tuple[dict[str, GateStatus], dict[str, float | int], list[str]]:
    by_asset = {
        receipt.asset_id: Path(receipt.local_path)
        for receipt in receipts
        if receipt.receipt_id in manifest.source_receipt_ids
    }
    paths_by_role: dict[DataRole, list[Path]] = defaultdict(list)
    for asset_id, role in manifest.source_asset_roles.items():
        if asset_id in by_asset:
            paths_by_role[role].append(by_asset[asset_id])

    checks: dict[str, GateStatus] = {}
    metrics: dict[str, float | int] = {}
    unavailable = ["tss_enrichment", "doublet_rate"]
    if not manifest.genome_builds:
        checks["genome_build_consistency"] = GateStatus.UNKNOWN
    elif len(manifest.genome_builds) == 1:
        checks["genome_build_consistency"] = GateStatus.PASS
    else:
        checks["genome_build_consistency"] = GateStatus.FAIL

    rna_matrix_paths = paths_by_role.get(DataRole.RNA_MATRIX, [])
    mtx_paths = [path for path in rna_matrix_paths if _is_mtx(path)]
    h5_paths = [path for path in rna_matrix_paths if _is_10x_h5(path)]
    if mtx_paths:
        count_status, count_metrics = _inspect_mtx_counts(mtx_paths)
        checks["rna_matrix_integer_like"] = count_status
        metrics.update(count_metrics)
    elif h5_paths:
        count_status, count_metrics = inspect_10x_h5_counts(h5_paths)
        if count_status is not None:
            checks["rna_matrix_integer_like"] = count_status
            metrics.update(count_metrics)
        else:
            unavailable.append("rna_matrix_integer_like")
    elif rna_matrix_paths:
        # A file this RNA_MATRIX-role asset didn't get identified as either
        # MatrixMarket or a native 10x `.h5` -- reported as a named gap
        # rather than silently treated as passing or absent.
        unavailable.append("rna_matrix_integer_like")

    linkage_paths = paths_by_role.get(DataRole.BARCODE_LINKAGE, [])
    fragments = paths_by_role.get(DataRole.FRAGMENTS, [])
    if linkage_paths:
        rows, left, right = _sample_linkage(linkage_paths[0])
        metrics["barcode_linkage_rows_sampled"] = rows
        metrics["barcode_linkage_left_barcodes"] = left
        metrics["barcode_linkage_right_barcodes"] = right
        checks["barcode_linkage"] = (
            GateStatus.PASS if rows else GateStatus.WARN
        )
    elif fragments:
        rna_barcodes = _rna_barcodes(manifest, paths_by_role)
        atac_barcodes = _sample_fragment_barcodes(fragments[0])
        if rna_barcodes and atac_barcodes:
            checks["barcode_linkage"], overlap_metrics = _barcode_overlap(
                rna_barcodes, atac_barcodes
            )
            metrics.update(overlap_metrics)
        else:
            checks["barcode_linkage"] = GateStatus.UNKNOWN

    peaks = paths_by_role.get(DataRole.PEAKS, [])
    if fragments and peaks:
        frip, rows = _sample_frip(fragments[0], peaks[0])
        metrics["frip_sampled"] = frip
        metrics["frip_fragment_rows_sampled"] = rows
        checks["frip_computable"] = GateStatus.PASS
    else:
        unavailable.append("frip")
        checks["frip_computable"] = GateStatus.UNKNOWN
    return checks, metrics, unavailable


def _is_mtx(path: Path) -> bool:
    name = path.name.casefold()
    return name.endswith(".mtx") or name.endswith(".mtx.gz")


def _is_10x_h5(path: Path) -> bool:
    return path.name.casefold().endswith(".h5")


def _inspect_mtx_counts(
    paths: list[Path], limit: int = 500_000
) -> tuple[GateStatus, dict[str, float | int]]:
    """Sample every MatrixMarket RNA-matrix file's data entries and check
    they are non-negative integers.

    This establishes the matrix has an integer-like count *structure* --
    necessary but not sufficient for the R03 "raw/unnormalized counts"
    claim. A log-normalized or scaled matrix (fractional, sometimes negative
    values) fails this even if repository text calls it raw counts, so a
    PASS here can still falsify a declared raw-counts claim. But a PASS
    alone does not *establish* raw-counts status: an integer matrix can
    still be filtered, clipped, or otherwise processed in ways this check
    cannot see. The actual raw-counts determination must combine this
    file-observed structural check with declared REPOSITORY_REPORTED/
    ARTICLE_REPORTED evidence about provenance -- never one alone.
    """
    sampled = negative = non_integer = 0
    for path in paths:
        if sampled >= limit:
            break
        with _open_text(path) as handle:
            header = handle.readline()
            if not header.startswith("%%MatrixMarket"):
                raise ValueError(f"{path}: missing MatrixMarket header")
            line = handle.readline()
            while line.startswith("%"):
                line = handle.readline()
            # `line` is now the dimension line; every following line is a
            # `row col value` data entry.
            for line in handle:
                fields = line.split()
                if len(fields) < 3:
                    continue
                value = float(fields[2])
                if value < 0:
                    negative += 1
                if not value.is_integer():
                    non_integer += 1
                sampled += 1
                if sampled >= limit:
                    break
    metrics: dict[str, float | int] = {
        "rna_matrix_values_sampled": sampled,
        "rna_matrix_negative_values": negative,
        "rna_matrix_non_integer_values": non_integer,
    }
    status = (
        GateStatus.PASS if sampled and negative == 0 and non_integer == 0 else GateStatus.FAIL
    )
    return status, metrics


def _rna_barcodes(
    manifest: MaterializationManifest, paths_by_role: dict[DataRole, list[Path]]
) -> set[str]:
    """RNA-side cell barcodes, preferring a standard object's obs index
    (`processed_first` material) and falling back to a MEX-style
    `*barcode*.tsv[.gz]` file among the CELL_METADATA-role assets.
    """
    if manifest.standard_object_path:
        barcodes = read_standard_object_barcodes(
            manifest.standard_object_path, limit=_BARCODE_SAMPLE_LIMIT
        )
        if barcodes:
            return barcodes
    for path in paths_by_role.get(DataRole.CELL_METADATA, []):
        if "barcode" in path.name.casefold():
            return _read_barcode_list(path, limit=_BARCODE_SAMPLE_LIMIT)
    return set()


def _read_barcode_list(path: Path, limit: int) -> set[str]:
    barcodes: set[str] = set()
    with _open_text(path) as handle:
        for line in handle:
            value = line.strip()
            if not value:
                continue
            barcodes.add(value.split("\t")[0])
            if len(barcodes) >= limit:
                break
    return barcodes


def _sample_fragment_barcodes(path: Path, limit: int = _BARCODE_SAMPLE_LIMIT) -> set[str]:
    barcodes: set[str] = set()
    with _open_text(path) as handle:
        for line in handle:
            if not line or line.startswith("#"):
                continue
            fields = line.rstrip("\n").split("\t")
            if len(fields) < 4:
                continue
            barcodes.add(fields[3])
            if len(barcodes) >= limit:
                break
    return barcodes


def _normalize_barcode(value: str) -> str:
    # Strips a trailing 10x-style GEM/sample well suffix ("-1", "-2", ...),
    # the single most common real-world cause of an otherwise-identical
    # barcode failing exact string comparison across the RNA and ATAC sides.
    # This is a targeted heuristic, not a general per-technology barcode
    # translation table -- that would need reference data this repo doesn't
    # have yet.
    if "-" in value:
        prefix, _, suffix = value.rpartition("-")
        if suffix.isdigit():
            return prefix
    return value


def _barcode_overlap(
    rna_barcodes: set[str], atac_barcodes: set[str]
) -> tuple[GateStatus, dict[str, float | int]]:
    normalized_rna = {_normalize_barcode(item) for item in rna_barcodes}
    normalized_atac = {_normalize_barcode(item) for item in atac_barcodes}
    intersection = normalized_rna & normalized_atac
    frac_rna_matched = len(intersection) / len(normalized_rna) if normalized_rna else 0.0
    frac_atac_matched = len(intersection) / len(normalized_atac) if normalized_atac else 0.0
    metrics: dict[str, float | int] = {
        "n_rna_barcodes": len(rna_barcodes),
        "n_atac_barcodes": len(atac_barcodes),
        "n_barcode_intersection": len(intersection),
        "frac_rna_barcodes_matched": frac_rna_matched,
        "frac_atac_barcodes_matched": frac_atac_matched,
    }
    status = (
        GateStatus.PASS
        if frac_rna_matched >= _BARCODE_OVERLAP_MIN_FRACTION
        else GateStatus.FAIL
    )
    return status, metrics


def _sample_linkage(path: Path, limit: int = 100_000) -> tuple[int, int, int]:
    rows = 0
    left: set[str] = set()
    right: set[str] = set()
    with _open_text(path) as handle:
        for line in handle:
            if not line or line.startswith("#"):
                continue
            fields = line.rstrip("\n").split("\t")
            if len(fields) < 2:
                raise ValueError("Barcode linkage row has fewer than two columns.")
            left.add(fields[0])
            right.add(fields[1])
            rows += 1
            if rows >= limit:
                break
    return rows, len(left), len(right)


def _sample_frip(
    fragments_path: Path,
    peaks_path: Path,
    limit: int = 100_000,
) -> tuple[float, int]:
    intervals = _load_peaks(peaks_path)
    starts = {
        chromosome: [start for start, _ in values]
        for chromosome, values in intervals.items()
    }
    total = 0
    in_peaks = 0
    rows = 0
    with _open_text(fragments_path) as handle:
        for line in handle:
            if not line or line.startswith("#"):
                continue
            fields = line.rstrip("\n").split("\t")
            if len(fields) < 3:
                raise ValueError("Fragment row has fewer than three columns.")
            chromosome = fields[0]
            start = int(fields[1])
            end = int(fields[2])
            weight = int(fields[4]) if len(fields) > 4 else 1
            total += weight
            values = intervals.get(chromosome, [])
            chromosome_starts = starts.get(chromosome, [])
            index = bisect_left(chromosome_starts, end) - 1
            if index >= 0 and values[index][1] > start:
                in_peaks += weight
            rows += 1
            if rows >= limit:
                break
    return (in_peaks / total if total else 0.0), rows


def _load_peaks(path: Path) -> dict[str, list[tuple[int, int]]]:
    raw: dict[str, list[tuple[int, int]]] = defaultdict(list)
    with _open_text(path) as handle:
        for line in handle:
            if not line or line.startswith("#"):
                continue
            fields = line.rstrip("\n").split("\t")
            if len(fields) < 3:
                raise ValueError("Peak row has fewer than three columns.")
            raw[fields[0]].append((int(fields[1]), int(fields[2])))
    merged: dict[str, list[tuple[int, int]]] = {}
    for chromosome, values in raw.items():
        values.sort()
        combined: list[tuple[int, int]] = []
        for start, end in values:
            if combined and start <= combined[-1][1]:
                combined[-1] = (combined[-1][0], max(combined[-1][1], end))
            else:
                combined.append((start, end))
        merged[chromosome] = combined
    return merged


def _open_text(path: Path) -> TextIO:
    if path.name.casefold().endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("rt", encoding="utf-8", errors="replace")
