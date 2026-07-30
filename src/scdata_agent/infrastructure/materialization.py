"""Deterministic processed-first package materialization and bounded QC."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

from scdata_agent.domain.identifiers import stable_id
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
        package = (
            Path(curated_root).resolve()
            / bundle.candidate_id
            / bundle.bundle_id
        )
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
        }
        provenance_path = package / "provenance.json"
        _atomic_json(provenance_path, provenance)
        data_card_path = _write_data_card(
            package, bundle, status, data_roles, standard_object
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
                    with gzip.open(path, "rb") as handle:
                        sample = handle.read(1024 * 1024)
                    if not sample:
                        compression = GateStatus.WARN
                if "fragment" in path.name.casefold():
                    rows, barcodes = _sample_fragments(path)
                    tabular_rows_sampled += rows
                    observed_barcodes.update(barcodes)
            except Exception as error:
                readability = GateStatus.FAIL
                reasons.append(f"{path}: {type(error).__name__}: {error}")

        checks["readability"] = readability
        checks["compression"] = compression
        if manifest.standard_object_path:
            checks["standard_object_hdf5"] = hdf5
        else:
            checks["standard_object_hdf5"] = GateStatus.UNKNOWN
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
        if manifest.standard_object_path:
            object_checks, object_metrics, object_unavailable = inspect_standard_object(
                manifest.standard_object_path
            )
            checks.update(object_checks)
            metrics.update(object_metrics)
            unavailable.extend(
                item for item in object_unavailable if item not in unavailable
            )
        if any(value == GateStatus.FAIL for value in checks.values()):
            status = GateStatus.FAIL
        elif manifest.status == MaterializationStatus.RAW_ONLY:
            status = GateStatus.WARN
            reasons.append(
                "FASTQ-only bundle retained; processed training object not produced."
            )
        elif not manifest.standard_object_path:
            status = GateStatus.WARN
            reasons.append(
                "Processed assets exist but no AnnData/MuData object was supplied."
            )
        elif any(
            value in {GateStatus.WARN, GateStatus.UNKNOWN}
            for value in checks.values()
        ):
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


def _write_data_card(
    package: Path,
    bundle: SemanticBundle,
    status: MaterializationStatus,
    data_roles: list[DataRole],
    standard_object: str | None,
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
                "",
                "QC metrics and corpus admission are recorded separately.",
            ]
        ) + "\n",
        encoding="utf-8",
    )
    return path
