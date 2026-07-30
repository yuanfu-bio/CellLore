"""Optional semantic inspection of AnnData and MuData standard objects."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scdata_agent.domain.models import GateStatus


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
        if getattr(value, "file", None) is not None:
            value.file.close()
        return {"anndata_semantics": status}, metrics, []
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
            metrics = {
                "mudata_modalities": modalities,
                "mudata_modality_count": len(modalities),
                "mudata_has_rna_atac": {
                    item.casefold() for item in modalities
                }
                >= {"rna", "atac"},
            }
        status = GateStatus.PASS if modalities else GateStatus.FAIL
        return {"mudata_semantics": status}, metrics, []
    except Exception:
        return {"mudata_semantics": GateStatus.FAIL}, {}, []
