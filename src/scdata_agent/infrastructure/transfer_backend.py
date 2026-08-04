"""Selects and gates the byte-transfer implementation.

httpx and aria2 share one contract (`TransferBackend`) and one set of
pre-flight refusals (`prepare_transfer`): unknown size without an explicit
override, a budget the selection would exceed, and insufficient free disk.
Gating this once means a policy tightened for one backend cannot be silently
bypassed by picking the other.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Protocol

from scdata_agent.domain.lifecycle import AcquisitionPlan, SemanticBundle, TransferReceipt
from scdata_agent.domain.models import DataAsset

#: Manifest-layout stage directories, in acquisition order. A relative_path
#: outside this vocabulary still works (the guard only cares about safety,
#: not about which stage it names); this exists purely as shared documentation
#: of what a curated manifest's sidecar rows are expected to use.
MANIFEST_LAYOUT_STAGES: tuple[str, ...] = (
    "00_manifest",
    "01_metadata",
    "02_rna_raw_counts",
    "03_atac_fragments",
    "05_auxiliary",
    "99_raw_optional",
)


class TransferBackend(Protocol):
    name: str

    async def execute(
        self,
        plan: AcquisitionPlan,
        bundles: list[SemanticBundle],
        assets: list[DataAsset],
        selected_bundle_ids: set[str],
        *,
        allow_unknown_size: bool = False,
        max_download_bytes: int | None = None,
        layout: dict[str, str] | None = None,
    ) -> list[TransferReceipt]: ...


def prepare_transfer(
    plan: AcquisitionPlan,
    bundles: list[SemanticBundle],
    assets: list[DataAsset],
    selected_bundle_ids: set[str],
    *,
    allow_unknown_size: bool,
    max_download_bytes: int | None,
) -> tuple[list[SemanticBundle], dict[str, DataAsset], Path]:
    """Common refusals every backend must honour before it fetches a byte."""
    selected = [bundle for bundle in bundles if bundle.bundle_id in selected_bundle_ids]
    selected_asset_ids = {
        asset_id for bundle in selected for asset_id in bundle.asset_ids
    }
    by_asset = {
        asset.asset_id: asset for asset in assets if asset.asset_id in selected_asset_ids
    }
    expected_sizes = [
        location.size_bytes for asset in by_asset.values() for location in asset.locations[:1]
    ]
    if any(size is None for size in expected_sizes) and not allow_unknown_size:
        raise ValueError(
            "Selected assets include unknown sizes; explicit override required."
        )
    known_total = sum(size or 0 for size in expected_sizes)
    if max_download_bytes is not None and known_total > max_download_bytes:
        raise ValueError("Selected assets exceed max_download_bytes.")
    destination = Path(plan.destination_root).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    free_bytes = shutil.disk_usage(destination).free
    if known_total > free_bytes:
        raise ValueError("Insufficient free disk space for selected assets.")
    return selected, by_asset, destination


def safe_destination_path(
    root: Path, *, bundle_id: str, file_name: str, relative_path: str | None
) -> Path:
    """Resolve where one asset lands, honouring a manifest layout when given.

    A strict generalization of the bare-basename guard `TransferExecutor`
    always applied: a curator's `relative_path` is a whole tree, not one
    name, so it must be checked for `..` escape and absolute-path smuggling
    the same way a single filename was checked before.
    """
    safe_name = Path(file_name).name
    if safe_name != file_name or safe_name in {"", ".", ".."}:
        raise ValueError(f"Unsafe asset file name: {file_name!r}")
    if not relative_path:
        return root / bundle_id / safe_name
    candidate = Path(relative_path)
    if candidate.is_absolute() or candidate.drive or ".." in candidate.parts:
        raise ValueError(f"Unsafe manifest relative_path: {relative_path!r}")
    resolved = (root / candidate).resolve()
    if resolved != root.resolve() and root.resolve() not in resolved.parents:
        raise ValueError(f"Manifest relative_path escapes the data root: {relative_path!r}")
    return resolved


def find_aria2_binary() -> str | None:
    return shutil.which(os.environ.get("SCDATA_AGENT_ARIA2_BIN", "aria2c"))


def resolves_to_aria2(policy: dict[str, Any]) -> bool:
    """Preview what ``select_transfer_backend`` would pick, without building it.

    Used to warn a reviewer *before* the download-approval gate that
    ``NCBI_CONTACT_EMAIL`` is unset when aria2 is about to be used, without
    constructing the backend (and its session/log paths) just to ask.
    """
    choice = policy.get("downloader", "auto")
    if choice not in {"auto", "aria2", "httpx"}:
        raise ValueError(f"Unknown downloader {choice!r}; expected auto, aria2, or httpx.")
    aria2_bin = find_aria2_binary()
    if choice == "aria2" and aria2_bin is None:
        raise RuntimeError(
            "downloader=aria2 was requested but aria2c is not on PATH "
            "(set SCDATA_AGENT_ARIA2_BIN or install aria2)."
        )
    return aria2_bin is not None and choice in {"auto", "aria2"}


def select_transfer_backend(policy: dict[str, Any]) -> TransferBackend:
    """Pick httpx or aria2 per ``policy["downloader"]`` (default ``auto``).

    ``auto`` prefers aria2 when it is actually on PATH (throughput on large
    files is why S5 exists at all) and falls back to httpx otherwise, so a
    machine without aria2c installed still works. Asking for aria2 by name
    when it is not available is a configuration error, not something to
    silently degrade past.
    """
    if resolves_to_aria2(policy):
        from scdata_agent.infrastructure.aria2_executor import Aria2TransferBackend

        return Aria2TransferBackend(policy=policy, aria2_bin=find_aria2_binary())
    from scdata_agent.infrastructure.transfer_executor import TransferExecutor

    return TransferExecutor(concurrency=int(policy.get("transfer_concurrency", 3)))
