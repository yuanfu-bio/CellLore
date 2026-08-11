"""aria2-backed transfer execution for large, resumable downloads.

httpx (`transfer_executor.py`) streams one connection per file: fine for a
few-hundred-KB sidecar, ruinous for a 13.7 GB fragments file where one
stalled connection means restarting from zero. aria2 segments a large file
across several connections and resumes from its own control file, so this
backend exists purely as a throughput/resilience upgrade -- `TransferBackend`
keeps the two interchangeable from the caller's side, and `prepare_transfer`
keeps their pre-flight refusals identical.

aria2 does not produce a per-file JSON receipt. Reconciliation into
`TransferReceipt` therefore happens by stat-ing the path each queue entry
named, against the size/checksum recorded when the queue was written -- not
by parsing aria2's own output, which is why the queue itself, not aria2's
exit code, is the source of truth for what a file was supposed to become.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from scdata_agent.domain.identifiers import stable_id
from scdata_agent.domain.lifecycle import (
    AcquisitionPlan,
    SemanticBundle,
    TransferReceipt,
    TransferReceiptStatus,
)
from scdata_agent.domain.models import DataAsset, TransferProtocol
from scdata_agent.infrastructure.transfer_backend import prepare_transfer, safe_destination_path

#: concurrent_downloads, connections_per_file (segments), keyed by profile
#: name and bucket. Ported verbatim from the validated prototype
#: (multiome_downloader.py PROFILES + the settings dict in cmd_download):
#: geo_small always uses 1 segment (nothing to gain splitting a small file),
#: other_direct always uses 4 concurrent downloads regardless of profile.
_PROFILE_TUPLES: dict[str, tuple[int, int, int, int]] = {
    # large_concurrency, large_segments, small_concurrency, other_segments
    "stable": (2, 1, 4, 1),
    "balanced": (4, 4, 12, 4),
    "perfile": (2, 8, 12, 4),
    "aggressive": (6, 4, 20, 4),
}


def bucket_settings(profile: str) -> dict[str, tuple[int, int]]:
    if profile not in _PROFILE_TUPLES:
        raise ValueError(f"Unknown aria2 profile {profile!r}; expected one of {sorted(_PROFILE_TUPLES)}.")
    large_j, large_x, small_j, other_x = _PROFILE_TUPLES[profile]
    return {
        "geo_large": (large_j, large_x),
        "geo_small": (small_j, 1),
        "other_direct": (4, other_x),
    }


#: Files at or above this size get split across connections; below it,
#: file-level concurrency does more for throughput than segmenting one file.
LARGE_FILE_THRESHOLD_BYTES = 500 * 1024**2

_NCBI_HOST_SUFFIXES = ("ncbi.nlm.nih.gov", "nih.gov")

_PROXY_ENVIRONMENT_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "FTP_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "ftp_proxy",
    "NO_PROXY",
    "no_proxy",
)


def proxy_free_download_environment(
    source: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return a child environment that forces downloads to connect directly.

    This intentionally does not mutate ``os.environ``: only the aria2 child
    process has its proxy variables cleared, so the user's shell and metadata
    discovery clients keep their existing network configuration.
    """
    environment = dict(os.environ if source is None else source)
    for key in _PROXY_ENVIRONMENT_KEYS:
        environment.pop(key, None)
    environment["NO_PROXY"] = "*"
    environment["no_proxy"] = "*"
    return environment


def _is_ncbi_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").casefold()
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in _NCBI_HOST_SUFFIXES)


def bucket_for(url: str, size_bytes: int | None) -> str:
    if not _is_ncbi_url(url):
        return "other_direct"
    return "geo_large" if (size_bytes or 0) >= LARGE_FILE_THRESHOLD_BYTES else "geo_small"


@dataclass(frozen=True)
class QueueEntry:
    asset_id: str
    bundle_id: str
    url: str
    path: Path
    expected_size: int | None
    checksum_md5: str | None
    #: Whether aria2's own control file already existed when this queue was
    #: built -- i.e. before this run, not after. After a successful run the
    #: control file is gone, so checking post-hoc would call every completed
    #: transfer "not resumed" even when it continued a prior attempt.
    resumed_from_prior_attempt: bool = False


def queue_entry_text(entry: QueueEntry) -> str:
    lines = [entry.url, f"  dir={entry.path.parent}", f"  out={entry.path.name}"]
    if entry.checksum_md5 and re.fullmatch(r"[0-9a-fA-F]{32}", entry.checksum_md5):
        lines.append(f"  checksum=md5={entry.checksum_md5}")
    return "\n".join(lines)


def write_queue_file(path: Path, entries: list[QueueEntry]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = ("\n\n".join(queue_entry_text(entry) for entry in entries) + "\n") if entries else ""
    path.write_text(text, encoding="utf-8")
    return path


def aria_common_args(user_agent: str, session: Path, log: Path) -> list[str]:
    """Ported verbatim from the validated prototype's ``aria_common_args``.

    ``--max-file-not-found=2`` is the one that matters most: without it aria2
    retries a genuine 404 up to ``--max-tries`` times before giving up, which
    is exactly the "retried 15 times for nothing" failure mode the manifest
    error class exists to short-circuit.
    """
    return [
        # Ignore ~/.aria2/aria2.conf as it may contain a workstation proxy;
        # the subprocess environment is also stripped of proxy variables.
        "--no-conf=true",
        "--continue=true",
        "--always-resume=true",
        "--auto-file-renaming=false",
        "--allow-overwrite=false",
        "--max-tries=15",
        "--retry-wait=30",
        "--connect-timeout=45",
        "--timeout=300",
        "--lowest-speed-limit=0",
        "--summary-interval=15",
        "--console-log-level=notice",
        "--log-level=notice",
        "--file-allocation=none",
        "--disk-cache=128M",
        "--check-integrity=false",
        f"--user-agent={user_agent}",
        "--max-file-not-found=2",
        "--remote-time=true",
        "--save-session-interval=60",
        "--enable-http-keep-alive=true",
        "--stream-piece-selector=geom",
        f"--save-session={session}",
        f"--log={log}",
    ]


def contact_email_and_user_agent(policy: dict[str, Any]) -> tuple[str | None, str]:
    """The contact email is advisory to NCBI, never required to run.

    A one-off script can afford ``raise SystemExit`` when it is missing; a
    library node cannot -- that would kill a run *after* a human already
    approved it. Missing it just means a less identifiable User-Agent and a
    recorded ``contact_email_unset`` issue.
    """
    email = policy.get("contact_email") or os.environ.get("NCBI_CONTACT_EMAIL")
    if email:
        return email, f"CellLore/0.1.0 (contact: {email})"
    return None, "CellLore/0.1.0"


class Aria2Runner(Protocol):
    async def run(self, args: list[str]) -> int: ...


class SubprocessAria2Runner:
    async def run(self, args: list[str]) -> int:
        process = await asyncio.create_subprocess_exec(
            *args,
            env=proxy_free_download_environment(),
        )
        return await process.wait()


class Aria2TransferBackend:
    name = "aria2"

    def __init__(
        self,
        *,
        policy: dict[str, Any],
        aria2_bin: str,
        runner: Aria2Runner | None = None,
    ) -> None:
        self.policy = policy
        self.aria2_bin = aria2_bin
        self.profile = str(policy.get("profile", "balanced"))
        self._runner = runner or SubprocessAria2Runner()
        # Run-level, not receipt-level: aria2 exit 3 means "one file 404'd
        # somewhere in this bucket," which says nothing about which of forty
        # others succeeded. Reconciliation below never consults this; it
        # exists only for a human or a repair pass to look at afterward.
        self.last_run_exit_codes: dict[str, int] = {}

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
    ) -> list[TransferReceipt]:
        selected, by_asset, destination = prepare_transfer(
            plan, bundles, assets, selected_bundle_ids,
            allow_unknown_size=allow_unknown_size,
            max_download_bytes=max_download_bytes,
        )
        layout = layout or {}
        bundle_by_asset = {
            asset_id: bundle.bundle_id for bundle in selected for asset_id in bundle.asset_ids
        }

        entries_by_bucket: dict[str, list[QueueEntry]] = {
            "geo_large": [], "geo_small": [], "other_direct": [],
        }
        skipped: list[TransferReceipt] = []
        for asset in by_asset.values():
            bundle_id = bundle_by_asset[asset.asset_id]
            if not asset.locations:
                skipped.append(self._failed(plan, bundle_id, asset, "", "No asset location."))
                continue
            location = asset.locations[0]
            if location.protocol not in {TransferProtocol.HTTPS, TransferProtocol.S3}:
                skipped.append(self._failed(
                    plan, bundle_id, asset, location.url,
                    f"Unsupported protocol: {location.protocol.value}",
                ))
                continue
            try:
                path = safe_destination_path(
                    destination, bundle_id=bundle_id, file_name=asset.file_name,
                    relative_path=layout.get(asset.asset_id),
                )
            except ValueError as error:
                skipped.append(self._failed(plan, bundle_id, asset, location.url, str(error)))
                continue
            control_path = path.with_suffix(path.suffix + ".aria2")
            entries_by_bucket[bucket_for(location.url, location.size_bytes)].append(
                QueueEntry(
                    asset_id=asset.asset_id, bundle_id=bundle_id, url=location.url,
                    path=path, expected_size=location.size_bytes,
                    checksum_md5=(
                        location.checksum
                        if (location.checksum_algorithm or "").casefold() == "md5"
                        else None
                    ),
                    resumed_from_prior_attempt=control_path.exists(),
                )
            )

        session_root = Path(plan.destination_root).resolve().parent / ".aria2" / plan.run_id
        _, user_agent = contact_email_and_user_agent(self.policy)
        settings = bucket_settings(self.profile)
        self.last_run_exit_codes = {}
        for bucket, entries in entries_by_bucket.items():
            if not entries:
                continue
            queue_path = write_queue_file(session_root / f"{bucket}.aria2.txt", entries)
            session_path = session_root / f"{bucket}.session"
            log_path = session_root / f"{bucket}.log"
            concurrent, segments = settings[bucket]
            min_split = "32M" if bucket == "geo_large" else "20M"
            args = [
                self.aria2_bin,
                *aria_common_args(user_agent, session_path, log_path),
                f"--input-file={queue_path}",
                f"--max-concurrent-downloads={concurrent}",
                f"--max-connection-per-server={segments}",
                f"--split={segments}",
                f"--min-split-size={min_split}",
            ]
            self.last_run_exit_codes[bucket] = await self._runner.run(args)

        receipts = [
            self._reconcile(plan, entry)
            for entries in entries_by_bucket.values()
            for entry in entries
        ]
        return [*receipts, *skipped]

    def _reconcile(self, plan: AcquisitionPlan, entry: QueueEntry) -> TransferReceipt:
        receipt_id = stable_id("receipt", f"{plan.plan_id}:{entry.bundle_id}:{entry.asset_id}")
        control_path = entry.path.with_suffix(entry.path.suffix + ".aria2")
        resumed = entry.resumed_from_prior_attempt
        if not entry.path.exists():
            return self._failed(
                plan, entry.bundle_id, entry.asset_id, entry.url,
                "aria2 produced no file.", resumed=resumed,
            )
        size = entry.path.stat().st_size
        if size == 0:
            return self._failed(
                plan, entry.bundle_id, entry.asset_id, entry.url,
                "Downloaded file is zero bytes.", local_path=str(entry.path), resumed=resumed,
            )
        if control_path.exists():
            return self._failed(
                plan, entry.bundle_id, entry.asset_id, entry.url,
                "control file present; transfer incomplete.",
                local_path=str(entry.path), resumed=resumed,
            )
        observed = _file_digest(entry.path, "md5") if entry.checksum_md5 else None
        valid = observed.casefold() == entry.checksum_md5.casefold() if observed else None
        return TransferReceipt(
            receipt_id=receipt_id,
            plan_id=plan.plan_id,
            bundle_id=entry.bundle_id,
            asset_id=entry.asset_id,
            source_url=entry.url,
            local_path=str(entry.path),
            protocol=TransferProtocol.HTTPS,
            status=TransferReceiptStatus.COMPLETED if valid is not False else TransferReceiptStatus.FAILED,
            bytes_written=size,
            checksum_algorithm="md5" if entry.checksum_md5 else None,
            expected_checksum=entry.checksum_md5,
            observed_checksum=observed,
            checksum_valid=valid,
            resumed=resumed,
            error="Checksum mismatch." if valid is False else None,
        )

    @staticmethod
    def _failed(
        plan: AcquisitionPlan,
        bundle_id: str,
        asset_or_id: DataAsset | str,
        source_url: str,
        error: str,
        *,
        local_path: str = "",
        resumed: bool = False,
    ) -> TransferReceipt:
        asset_id = asset_or_id.asset_id if isinstance(asset_or_id, DataAsset) else asset_or_id
        protocol = (
            asset_or_id.locations[0].protocol
            if isinstance(asset_or_id, DataAsset) and asset_or_id.locations
            else TransferProtocol.HTTPS
        )
        return TransferReceipt(
            receipt_id=stable_id("receipt", f"{plan.plan_id}:{bundle_id}:{asset_id}"),
            plan_id=plan.plan_id,
            bundle_id=bundle_id,
            asset_id=asset_id,
            source_url=source_url,
            local_path=local_path,
            protocol=protocol,
            status=TransferReceiptStatus.FAILED,
            error=error,
            resumed=resumed,
        )


def _file_digest(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
