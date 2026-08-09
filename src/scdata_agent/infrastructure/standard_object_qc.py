"""Optional semantic inspection of AnnData and MuData standard objects."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scdata_agent.domain.models import GateStatus

# Sampling every value in a matrix with millions of cells is not necessary to
# catch a log-normalized or scaled matrix -- a bounded sample of the sparse
# `.data` array (the only values that can be non-integer or negative; zeros
# are trivially fine) is enough, and keeps this check's cost independent of
# corpus size.
_COUNT_SAMPLE_LIMIT = 500_000


def inspect_standard_object(
    path: str | Path,
) -> tuple[dict[str, GateStatus], dict[str, Any], list[str]]:
    target = Path(path)
    if target.suffix.casefold() == ".h5ad":
        return _inspect_h5ad(target)
    if target.suffix.casefold() == ".h5mu":
        return _inspect_h5mu(target)
    return (
        {"standard_object_semantics": GateStatus.UNKNOWN},
        {},
        ["standard_object_semantic_validation"],
    )


def read_standard_object_barcodes(path: str | Path, limit: int = 500_000) -> set[str]:
    """RNA-side cell barcodes from a standard object's obs index.

    Returns an empty set -- never raises -- when anndata/h5py aren't
    installed or the object can't be read this way; callers already treat an
    empty barcode set as "unavailable", matching this module's existing
    degrade-to-UNKNOWN posture rather than failing the whole QC pass.
    """
    target = Path(path)
    if target.suffix.casefold() == ".h5ad":
        try:
            import anndata
        except ImportError:
            return set()
        try:
            value = anndata.read_h5ad(target, backed="r")
            names = {str(item) for item in value.obs_names[:limit]}
            if getattr(value, "file", None) is not None:
                value.file.close()
            return names
        except Exception:
            return set()
    if target.suffix.casefold() == ".h5mu":
        try:
            import h5py
        except ImportError:
            return set()
        try:
            with h5py.File(target, "r") as handle:
                rna = handle.get("mod", {}).get("rna")
                if rna is None:
                    return set()
                index = rna["obs"]["_index"]
                raw = index[: min(len(index), limit)]
                return {
                    item.decode() if isinstance(item, bytes) else str(item)
                    for item in raw
                }
        except Exception:
            return set()
    return set()


def _inspect_h5ad(
    path: Path,
) -> tuple[dict[str, GateStatus], dict[str, Any], list[str]]:
    try:
        import anndata
    except ImportError:
        return (
            {"anndata_semantics": GateStatus.UNKNOWN},
            {},
            ["anndata_semantic_validation"],
        )
    try:
        value = anndata.read_h5ad(path, backed="r")
        metrics = {
            "anndata_n_obs": int(value.n_obs),
            "anndata_n_vars": int(value.n_vars),
            "anndata_obs_names_unique": bool(value.obs_names.is_unique),
            "anndata_var_names_unique": bool(value.var_names.is_unique),
        }
        status = (
            GateStatus.PASS
            if value.n_obs > 0
            and value.n_vars > 0
            and value.obs_names.is_unique
            else GateStatus.FAIL
        )
        checks = {"anndata_semantics": status}
        unavailable: list[str] = []
        # Integer-like count *structure*, checked on file content -- a
        # log-normalized or scaled matrix (fractional, sometimes negative
        # values) fails this even if repository text says "raw counts". But
        # this alone never *establishes* the R03 raw-counts claim: an
        # integer matrix can still be filtered or otherwise processed. The
        # actual raw-counts determination must combine this with declared
        # REPOSITORY_REPORTED/ARTICLE_REPORTED provenance evidence.
        count_status, count_metrics = _check_matrix_counts(value.X)
        if count_status is not None:
            checks["rna_matrix_integer_like"] = count_status
            metrics.update(count_metrics)
        else:
            unavailable.append("rna_matrix_integer_like")
        if getattr(value, "file", None) is not None:
            value.file.close()
        return checks, metrics, unavailable
    except Exception:
        return {"anndata_semantics": GateStatus.FAIL}, {}, []


def _inspect_h5mu(
    path: Path,
) -> tuple[dict[str, GateStatus], dict[str, Any], list[str]]:
    try:
        import h5py
    except ImportError:
        return (
            {"mudata_semantics": GateStatus.UNKNOWN},
            {},
            ["mudata_semantic_validation"],
        )
    try:
        with h5py.File(path, "r") as handle:
            modalities = sorted(handle.get("mod", {}).keys())
            metrics: dict[str, Any] = {
                "mudata_modalities": modalities,
                "mudata_modality_count": len(modalities),
                "mudata_has_rna_atac": {
                    item.casefold() for item in modalities
                }
                >= {"rna", "atac"},
            }
            checks = {}
            status = GateStatus.PASS if modalities else GateStatus.FAIL
            checks["mudata_semantics"] = status
            unavailable: list[str] = []
            rna = handle.get("mod", {}).get("rna")
            count_status, count_metrics = (
                _check_h5_matrix_counts(rna["X"]) if rna is not None and "X" in rna else (None, {})
            )
            if count_status is not None:
                checks["rna_matrix_integer_like"] = count_status
                metrics.update(count_metrics)
            else:
                unavailable.append("rna_matrix_integer_like")
        return checks, metrics, unavailable
    except Exception:
        return {"mudata_semantics": GateStatus.FAIL}, {}, []


def _check_matrix_counts(matrix: Any) -> tuple[GateStatus | None, dict[str, Any]]:
    """Sample an in-memory (numpy or scipy-sparse) matrix's values."""
    try:
        values = matrix.data if hasattr(matrix, "data") else matrix
        values = values[: min(len(values), _COUNT_SAMPLE_LIMIT)]
        sampled = len(values)
        if sampled == 0:
            return None, {}
        negative = int((values < 0).sum())
        non_integer = int((values % 1 != 0).sum())
    except Exception:
        return None, {}
    return (
        GateStatus.PASS if negative == 0 and non_integer == 0 else GateStatus.FAIL,
        {
            "rna_matrix_values_sampled": sampled,
            "rna_matrix_negative_values": negative,
            "rna_matrix_non_integer_values": non_integer,
        },
    )


def inspect_10x_h5_counts(paths: list[Path]) -> tuple[GateStatus | None, dict[str, Any]]:
    """Sample a native 10x CellRanger feature-barcode matrix HDF5 file (a
    distinct binary layout from `.h5ad`/AnnData -- see manifest.py's
    ``filtered_integer_count_h5``/``raw_count_h5`` file kinds) and check the
    matrix's stored values are non-negative integers.

    Supports both the CellRanger 3+ layout (a top-level ``matrix`` group) and
    the older CellRanger 2.x layout (one top-level group per genome, e.g.
    ``/GRCh38/data``) -- reuses ``_check_h5_matrix_counts``'s sparse-on-disk
    convention since both layouts store a ``data`` dataset the same way.
    """
    try:
        import h5py
    except ImportError:
        return None, {}
    sampled = negative = non_integer = 0
    saw_any = False
    try:
        for path in paths:
            with h5py.File(path, "r") as handle:
                group = _cellranger_matrix_group(handle)
                if group is None:
                    continue
                status, metrics = _check_h5_matrix_counts(group)
                if status is None:
                    continue
                saw_any = True
                sampled += metrics["rna_matrix_values_sampled"]
                negative += metrics["rna_matrix_negative_values"]
                non_integer += metrics["rna_matrix_non_integer_values"]
    except Exception:
        return GateStatus.FAIL, {}
    if not saw_any:
        return None, {}
    return (
        GateStatus.PASS if negative == 0 and non_integer == 0 else GateStatus.FAIL,
        {
            "rna_matrix_values_sampled": sampled,
            "rna_matrix_negative_values": negative,
            "rna_matrix_non_integer_values": non_integer,
        },
    )


def _cellranger_matrix_group(handle: Any) -> Any | None:
    if "matrix" in handle and "data" in handle["matrix"]:
        return handle["matrix"]
    for key in handle.keys():
        candidate = handle[key]
        if hasattr(candidate, "keys") and "data" in candidate:
            return candidate
    return None


def _check_h5_matrix_counts(group: Any) -> tuple[GateStatus | None, dict[str, Any]]:
    """Sample an h5py Group/Dataset holding a MuData modality's `X`.

    Handles both the sparse-on-disk convention (a group with a `data`
    dataset, holding only the nonzero values) and a plain dense dataset --
    for the dense case a bounded number of *rows* is sampled rather than a
    byte-count slice, since a dense dataset is multi-dimensional.
    """
    try:
        if hasattr(group, "keys") and "data" in group:
            dataset = group["data"]
            flat = dataset[: min(dataset.shape[0], _COUNT_SAMPLE_LIMIT)]
        else:
            row_limit = min(group.shape[0], max(1, _COUNT_SAMPLE_LIMIT // max(group.shape[1], 1)))
            flat = group[:row_limit].ravel()
        sampled = len(flat)
        if sampled == 0:
            return None, {}
        negative = int((flat < 0).sum())
        non_integer = int((flat % 1 != 0).sum())
    except Exception:
        return None, {}
    return (
        GateStatus.PASS if negative == 0 and non_integer == 0 else GateStatus.FAIL,
        {
            "rna_matrix_values_sampled": sampled,
            "rna_matrix_negative_values": negative,
            "rna_matrix_non_integer_values": non_integer,
        },
    )
