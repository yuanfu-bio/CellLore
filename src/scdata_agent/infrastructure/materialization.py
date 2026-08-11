"""Deterministic processed-first package materialization and bounded QC."""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
from typing import Any

from scdata_agent.domain.identifiers import stable_id
from scdata_agent.domain.licensing import policies_for_urls
from scdata_agent.domain.lifecycle import (
    MaterializationManifest,
    MaterializationStatus,
    MaterializedQCReport,
    SemanticBundle,
    TransferReceipt,
    TransferReceiptStatus,
)
from scdata_agent.domain.models import DataAsset, DataRole, GateStatus
from scdata_agent.infrastructure.qc_metrics import compute_role_metrics
from scdata_agent.infrastructure.standard_object_qc import inspect_standard_object
from scdata_agent.infrastructure.transfer_backend import MANIFEST_LAYOUT_STAGES


class DatasetMaterializer:
    def materialize(
        self,
        *,
        bundle: SemanticBundle,
        assets: list[DataAsset],
        receipts: list[TransferReceipt],
        curated_root: str | Path,
    ) -> MaterializationManifest:
        successful = [
            receipt
            for receipt in receipts
            if receipt.bundle_id == bundle.bundle_id
            and receipt.status == TransferReceiptStatus.COMPLETED
        ]
        by_asset = {item.asset_id: item for item in assets}
        data_roles = sorted(
            {
                by_asset[receipt.asset_id].role
                for receipt in successful
                if receipt.asset_id in by_asset
            },
            key=lambda item: item.value,
        )
        source_asset_roles = {
            receipt.asset_id: by_asset[receipt.asset_id].role
            for receipt in successful
            if receipt.asset_id in by_asset
        }
        genome_builds = sorted(
            {
                by_asset[receipt.asset_id].genome_build
                for receipt in successful
                if receipt.asset_id in by_asset and by_asset[receipt.asset_id].genome_build
            }
        )
        package = _production_pair_root(successful, curated_root, bundle) / "04_standardized"
        package.mkdir(parents=True, exist_ok=True)
        errors: list[str] = []
        if not successful:
            status = MaterializationStatus.FAILED
            errors.append("No successful transfer receipt is available.")
        elif set(data_roles) <= {DataRole.RAW_READS}:
            status = MaterializationStatus.RAW_ONLY
        else:
            status = MaterializationStatus.MATERIALIZED

        standard_object = next(
            (
                receipt.local_path
                for receipt in successful
                if Path(receipt.local_path).suffix.casefold()
                in {".h5ad", ".h5mu"}
            ),
            None,
        )
        # The published package must state what its reuse terms rest on, so a
        # downstream consumer is never left guessing why the data was admitted.
        license_terms = _license_terms(
            [by_asset[receipt.asset_id] for receipt in successful if receipt.asset_id in by_asset]
        )
        provenance = {
            "schema_version": "2.0",
            "bundle_id": bundle.bundle_id,
            "candidate_id": bundle.candidate_id,
            "processed_first": True,
            "source_receipts": [
                receipt.model_dump(mode="json") for receipt in successful
            ],
            "data_roles": [role.value for role in data_roles],
            "standard_object": standard_object,
            "source_asset_roles": {key: value.value for key, value in source_asset_roles.items()},
            "genome_builds": genome_builds,
            "license": license_terms,
        }
        metadata_root = package / "metadata"
        qc_root = package / "qc"
        metadata_root.mkdir(parents=True, exist_ok=True)
        qc_root.mkdir(parents=True, exist_ok=True)
        provenance_path = metadata_root / "provenance.json"
        _atomic_json(provenance_path, provenance)
        data_card_path = _write_data_card(
            package, bundle, status, data_roles, standard_object, license_terms
        )
        manifest = MaterializationManifest(
            manifest_id=stable_id(
                "materialization", f"{bundle.bundle_id}:{len(successful)}"
            ),
            bundle_id=bundle.bundle_id,
            candidate_id=bundle.candidate_id,
            status=status,
            source_receipt_ids=[item.receipt_id for item in successful],
            source_asset_ids=[item.asset_id for item in successful],
            package_path=str(package),
            data_card_path=str(data_card_path),
            standard_object_path=standard_object,
            source_asset_roles=source_asset_roles,
            genome_builds=genome_builds,
            data_roles=data_roles,
            provenance_path=str(provenance_path),
            errors=errors,
        )
        _atomic_json(
            package / "manifest.json", manifest.model_dump(mode="json")
        )
        _atomic_json(
            package / "status.json",
            {
                "bundle_id": manifest.bundle_id,
                "candidate_id": manifest.candidate_id,
                "materialization_status": manifest.status.value,
                "source_file_count": len(manifest.source_receipt_ids),
                "data_roles": [item.value for item in manifest.data_roles],
                "genome_builds": manifest.genome_builds,
                "qc_status": "pending",
                "errors": manifest.errors,
            },
        )
        return manifest


class MaterializedQualityInspector:
    """Inspect local bytes without claiming unavailable biological metrics."""

    def inspect(
        self,
        manifest: MaterializationManifest,
        receipts: list[TransferReceipt],
    ) -> MaterializedQCReport:
        selected = [
            receipt
            for receipt in receipts
            if receipt.receipt_id in manifest.source_receipt_ids
        ]
        checks: dict[str, GateStatus] = {}
        metrics: dict[str, Any] = {
            "file_count": len(selected),
            "total_bytes": sum(item.bytes_written for item in selected),
        }
        reasons: list[str] = []
        paths: list[str] = []
        if not selected:
            checks["files_present"] = GateStatus.FAIL
            reasons.append("No transferred file is available for inspection.")
        else:
            checks["files_present"] = GateStatus.PASS

        readability = GateStatus.PASS
        compression = GateStatus.PASS
        hdf5 = GateStatus.PASS
        tabular_rows_sampled = 0
        observed_barcodes: set[str] = set()
        for receipt in selected:
            path = Path(receipt.local_path)
            paths.append(str(path))
            if not path.is_file() or path.stat().st_size == 0:
                readability = GateStatus.FAIL
                reasons.append(f"Missing or empty file: {path}")
                continue
            try:
                with path.open("rb") as handle:
                    prefix = handle.read(8)
                if path.suffix.casefold() in {".h5ad", ".h5mu"}:
                    if prefix != b"\x89HDF\r\n\x1a\n":
                        hdf5 = GateStatus.FAIL
                        reasons.append(f"Invalid HDF5 signature: {path}")
                if path.name.casefold().endswith(".gz"):
                    # The full stream, not a 1 MiB sample: a 260 MB fragments
                    # file truncated at byte 200,000,000 reads fine for the
                    # first megabyte and only fails decompression later.
                    total_bytes = 0
                    with gzip.open(path, "rb") as handle:
                        while chunk := handle.read(1024 * 1024):
                            total_bytes += len(chunk)
                    if total_bytes == 0:
                        compression = GateStatus.WARN
                # Gated by role, not filename: a *.fragments.tsv.gz.tbi.gz
                # index also has "fragment" in its name but is a binary index,
                # not a tab-separated fragments file, and must never reach the
                # fragments parser.
                if manifest.source_asset_roles.get(receipt.asset_id) == DataRole.FRAGMENTS:
                    rows, barcodes = _sample_fragments(path)
                    tabular_rows_sampled += rows
                    observed_barcodes.update(barcodes)
            except Exception as error:
                readability = GateStatus.FAIL
                reasons.append(f"{path}: {type(error).__name__}: {error}")

        checks["readability"] = readability
        checks["compression"] = compression
        # A check that could not run at all (no standard object was supplied
        # to inspect) is a gap in what we know, not a WARN-worthy finding.
        # Recording it as "absent" here rather than GateStatus.UNKNOWN keeps
        # that distinction out of the status-collapsing logic below.
        structurally_unavailable: list[str] = []
        if manifest.standard_object_path:
            checks["standard_object_hdf5"] = hdf5
        else:
            structurally_unavailable.append("standard_object")
        metrics["fragment_rows_sampled"] = tabular_rows_sampled
        metrics["sampled_fragment_barcodes"] = len(observed_barcodes)
        unavailable = ["tss_enrichment", "frip", "doublet_rate"]
        try:
            role_checks, role_metrics, unavailable = compute_role_metrics(
                manifest, selected
            )
            checks.update(role_checks)
            metrics.update(role_metrics)
        except Exception as error:
            checks["semantic_role_metrics"] = GateStatus.FAIL
            reasons.append(f"Role-aware QC failed: {type(error).__name__}: {error}")
        unavailable = [*unavailable, *structurally_unavailable]
        if manifest.standard_object_path:
            object_checks, object_metrics, object_unavailable = inspect_standard_object(
                manifest.standard_object_path
            )
            checks.update(object_checks)
            metrics.update(object_metrics)
            unavailable.extend(
                item for item in object_unavailable if item not in unavailable
            )
        # A check nobody could run (e.g. barcode_linkage or frip_computable on
        # a native mtx/fragments package, with no standard object to derive
        # them from) is a boundary of what we know, not a quality finding.
        # Move it out of `checks` so it cannot be read as a WARN.
        for key in [item for item, value in checks.items() if value == GateStatus.UNKNOWN]:
            del checks[key]
            if key not in unavailable:
                unavailable.append(key)
        if any(value == GateStatus.FAIL for value in checks.values()):
            status = GateStatus.FAIL
        elif manifest.status == MaterializationStatus.RAW_ONLY:
            status = GateStatus.WARN
            reasons.append(
                "FASTQ-only bundle retained; processed training object not produced."
            )
        elif any(value == GateStatus.WARN for value in checks.values()):
            status = GateStatus.WARN
        else:
            status = GateStatus.PASS
        return MaterializedQCReport(
            report_id=stable_id("materialized_qc", manifest.manifest_id),
            bundle_id=manifest.bundle_id,
            candidate_id=manifest.candidate_id,
            status=status,
            structural_checks=checks,
            metrics=metrics,
            unavailable_metrics=unavailable,
            inspected_paths=paths,
            reasons=reasons,
        )


def _production_pair_root(
    receipts: list[TransferReceipt],
    curated_root: str | Path,
    bundle: SemanticBundle,
) -> Path:
    """Locate the pair-unit root shared by role-specific raw directories."""
    parents = [Path(item.local_path).resolve().parent for item in receipts]
    if not parents:
        return Path(curated_root).resolve() / bundle.candidate_id / bundle.bundle_id
    common = Path(os.path.commonpath([str(item) for item in parents]))
    if common.name in MANIFEST_LAYOUT_STAGES:
        common = common.parent
    return common


def _sample_fragments(path: Path, limit: int = 10_000) -> tuple[int, set[str]]:
    opener = gzip.open if path.name.casefold().endswith(".gz") else open
    rows = 0
    barcodes: set[str] = set()
    with opener(path, "rt", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line or line.startswith("#"):
                continue
            fields = line.rstrip("\n").split("\t")
            if len(fields) < 4:
                raise ValueError("Fragment row has fewer than four columns.")
            int(fields[1])
            int(fields[2])
            barcodes.add(fields[3])
            rows += 1
            if rows >= limit:
                break
    return rows, barcodes


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)


def _license_terms(assets: list[DataAsset]) -> dict[str, Any]:
    """Describe the reuse basis for a package: a read licence, or archive terms.

    Mirrors the precedence used at preflight. A licence declared on the assets
    is reported as observed; only when none exists does the hosting archive's
    published policy stand in, and it is labelled as such.
    """
    declared = sorted({item.license_id for item in assets if item.license_id})
    if declared:
        return {
            "basis": "declared_license",
            "license_ids": declared,
            "license_urls": sorted(
                {item.license_url for item in assets if item.license_url}
            ),
        }
    policies = policies_for_urls(
        [location.url for item in assets for location in item.locations]
    )
    if not policies:
        return {
            "basis": "unknown",
            "detail": "No licence was declared and no archive policy applies.",
        }
    return {
        "basis": "repository_policy",
        "policies": [
            {
                "policy_id": policy.policy_id,
                "repository": policy.repository,
                "policy_url": policy.policy_url,
                "summary": policy.summary,
                "caveat": policy.caveat,
            }
            for policy in policies
        ],
    }


def _license_card_lines(license_terms: dict[str, Any]) -> list[str]:
    basis = license_terms.get("basis")
    if basis == "declared_license":
        ids = ", ".join(f"`{item}`" for item in license_terms["license_ids"])
        return [f"- Licence: {ids} (declared by the source repository)"]
    if basis == "repository_policy":
        lines = [
            "- Licence: no per-record licence is published; reuse rests on the "
            "terms of use of the hosting archive(s):",
        ]
        for policy in license_terms["policies"]:
            lines.extend([
                f"  - {policy['repository']} ({policy['policy_url']})",
                f"    - {policy['summary']}",
                f"    - Caveat: {policy['caveat']}",
            ])
        return lines
    return ["- Licence: **unknown**; confirm reuse terms before training on this bundle."]


def _write_data_card(
    package: Path,
    bundle: SemanticBundle,
    status: MaterializationStatus,
    data_roles: list[DataRole],
    standard_object: str | None,
    license_terms: dict[str, Any] | None = None,
) -> Path:
    path = package / "DATA_CARD.md"
    roles = ", ".join(f"`{role.value}`" for role in data_roles) or "none"
    standard = f"`{standard_object}`" if standard_object else "not available"
    path.write_text(
        "\n".join(
            [
                f"# Dataset bundle {bundle.bundle_id}",
                "",
                f"- Candidate: `{bundle.candidate_id}`",
                f"- Materialization status: `{status.value}`",
                "- Policy: processed-first; source assets remain immutable.",
                f"- Data roles: {roles}",
                f"- Standard object: {standard}",
                *_license_card_lines(license_terms or {"basis": "unknown"}),
                "",
                "QC metrics and corpus admission are recorded separately.",
            ]
        ) + "\n",
        encoding="utf-8",
    )
    return path
