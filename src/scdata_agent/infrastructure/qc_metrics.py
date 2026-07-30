"""Dependency-light local QC metrics for processed scATAC/multiome bundles."""

from __future__ import annotations

import gzip
from bisect import bisect_left
from collections import defaultdict
from pathlib import Path
from typing import TextIO

from scdata_agent.domain.lifecycle import (
    MaterializationManifest,
    TransferReceipt,
)
from scdata_agent.domain.models import DataRole, GateStatus


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

    linkage_paths = paths_by_role.get(DataRole.BARCODE_LINKAGE, [])
    if linkage_paths:
        rows, left, right = _sample_linkage(linkage_paths[0])
        metrics["barcode_linkage_rows_sampled"] = rows
        metrics["barcode_linkage_left_barcodes"] = left
        metrics["barcode_linkage_right_barcodes"] = right
        checks["barcode_linkage"] = (
            GateStatus.PASS if rows else GateStatus.WARN
        )
    elif (
        paths_by_role.get(DataRole.RNA_MATRIX)
        and paths_by_role.get(DataRole.FRAGMENTS)
    ):
        checks["barcode_linkage"] = GateStatus.UNKNOWN

    fragments = paths_by_role.get(DataRole.FRAGMENTS, [])
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
