"""aria2 queue generation, argument building, and receipt reconciliation --
all offline. The queue text is the one thing this backend generates that
aria2 actually reads, so it is checked byte-exact against real manifest URLs
rather than against synthetic fixtures.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from celllore.application.manifest_ingestion import ingest_manifest
from celllore.domain.lifecycle import AcquisitionPlan, SemanticBundle, TransferReceiptStatus
from celllore.infrastructure.aria2_executor import (
    Aria2TransferBackend,
    QueueEntry,
    aria_common_args,
    bucket_for,
    bucket_settings,
    contact_email_and_user_agent,
    proxy_free_download_environment,
    queue_entry_text,
)
from celllore.infrastructure.manifest_reader import read_manifest
from celllore.infrastructure.transfer_backend import (
    safe_destination_path,
    select_transfer_backend,
)

FIXTURE = Path(__file__).parent / "data" / "manifest_paired_multiome_sample.csv"


def _real_assets():
    document = read_manifest(FIXTURE)
    result = ingest_manifest(document, run_id="t", filters={}, policy={})
    fragments = next(
        item for item in result.assets
        if item.file_name == "GSM5396336_lane2_atac_fragments.tsv.gz"
    )
    zenodo = next(
        item for item in result.assets if item.file_name == "tonsil_atlas_donor_metadata.csv"
    )
    return fragments, zenodo


def test_queue_entry_text_omits_checksum_when_none_declared():
    fragments, _ = _real_assets()
    location = fragments.locations[0]
    entry = QueueEntry(
        asset_id=fragments.asset_id, bundle_id="b1", url=location.url,
        path=Path("/data/raw/b1") / fragments.file_name,
        expected_size=location.size_bytes, checksum_md5=None,
    )
    text = queue_entry_text(entry)
    assert text == f"{location.url}\n  dir=/data/raw/b1\n  out={fragments.file_name}"
    assert "checksum=" not in text


def test_queue_entry_text_includes_checksum_when_declared():
    _, zenodo = _real_assets()
    location = zenodo.locations[0]
    assert location.checksum  # the fixture's Zenodo row is one of the manifest's two MD5s
    entry = QueueEntry(
        asset_id=zenodo.asset_id, bundle_id="b2", url=location.url,
        path=Path("/data/raw/b2") / zenodo.file_name,
        expected_size=location.size_bytes, checksum_md5=location.checksum,
    )
    text = queue_entry_text(entry)
    assert text == (
        f"{location.url}\n  dir=/data/raw/b2\n  out={zenodo.file_name}\n"
        f"  checksum=md5={location.checksum}"
    )


def test_bucket_assignment_by_host_and_size():
    fragments, zenodo = _real_assets()
    # This fixture's fragments file is ~0.3 GB, under the 0.5 GB large
    # threshold, on an NCBI host: file-level concurrency, not segmenting.
    assert bucket_for(fragments.locations[0].url, fragments.locations[0].size_bytes) == "geo_small"
    assert bucket_for("https://ftp.ncbi.nlm.nih.gov/x.gz", 600 * 1024**2) == "geo_large"
    assert bucket_for(zenodo.locations[0].url, zenodo.locations[0].size_bytes) == "other_direct"


def test_bucket_settings_match_the_validated_reference_profiles():
    assert bucket_settings("stable") == {
        "geo_large": (2, 1), "geo_small": (4, 1), "other_direct": (4, 1),
    }
    assert bucket_settings("balanced") == {
        "geo_large": (4, 4), "geo_small": (12, 1), "other_direct": (4, 4),
    }
    with pytest.raises(ValueError):
        bucket_settings("nonexistent")


def test_common_args_include_tuning_and_the_404_guard():
    args = aria_common_args("CellLore/1.0.0 (contact: a@b.com)", Path("/s"), Path("/l"))
    assert "--no-conf=true" in args
    assert "--max-file-not-found=2" in args
    assert "--user-agent=CellLore/1.0.0 (contact: a@b.com)" in args
    assert "--save-session=/s" in args
    assert "--log=/l" in args


def test_contact_email_is_cleanly_omitted_when_unset(monkeypatch):
    monkeypatch.delenv("NCBI_CONTACT_EMAIL", raising=False)
    email, user_agent = contact_email_and_user_agent({})
    assert email is None
    assert user_agent == "CellLore/1.0.0"
    assert "(contact:" not in user_agent


def test_contact_email_from_policy_appears_in_user_agent():
    email, user_agent = contact_email_and_user_agent({"contact_email": "me@example.org"})
    assert email == "me@example.org"
    assert user_agent == "CellLore/1.0.0 (contact: me@example.org)"


def test_download_environment_removes_proxy_without_mutating_source():
    source = {
        "PATH": "/usr/bin",
        "HTTP_PROXY": "http://127.0.0.1:8010",
        "https_proxy": "http://127.0.0.1:8010",
        "ALL_PROXY": "socks5://127.0.0.1:1080",
        "NO_PROXY": "localhost",
    }
    environment = proxy_free_download_environment(source)
    assert source["HTTP_PROXY"] == "http://127.0.0.1:8010"
    assert environment["PATH"] == "/usr/bin"
    assert "HTTP_PROXY" not in environment
    assert "https_proxy" not in environment
    assert "ALL_PROXY" not in environment
    assert environment["NO_PROXY"] == "*"
    assert environment["no_proxy"] == "*"


def test_auto_downloader_falls_back_to_httpx_when_aria2_missing(monkeypatch):
    monkeypatch.setattr("celllore.infrastructure.transfer_backend.shutil.which", lambda *_: None)
    backend = select_transfer_backend({"downloader": "auto"})
    assert backend.name == "httpx"


def test_explicit_aria2_without_binary_raises(monkeypatch):
    monkeypatch.setattr("celllore.infrastructure.transfer_backend.shutil.which", lambda *_: None)
    with pytest.raises(RuntimeError):
        select_transfer_backend({"downloader": "aria2"})


def test_safe_destination_path_rejects_escape_and_absolute_paths():
    root = Path("/data/root")
    for bad in ("../escape/f.gz", "/abs/f.gz", "a/../../b.gz"):
        with pytest.raises(ValueError):
            safe_destination_path(root, bundle_id="b", file_name="f.gz", relative_path=bad)


def test_safe_destination_path_honours_manifest_layout_when_given():
    root = Path("/data/root")
    result = safe_destination_path(
        root, bundle_id="b", file_name="f.gz",
        relative_path="GSE1/pu1/03_atac_fragments/f.gz",
    )
    assert result == root / "GSE1" / "pu1" / "03_atac_fragments" / "f.gz"


def test_safe_destination_path_falls_back_to_bundle_layout():
    root = Path("/data/root")
    result = safe_destination_path(root, bundle_id="b1", file_name="f.gz", relative_path=None)
    assert result == root / "b1" / "f.gz"


class _FakeAria2Runner:
    """Parses the queue file it's handed and writes the named bytes to the
    named path. This proves queue generation, not aria2 itself.
    """

    def __init__(self, *, leave_control_file_for: frozenset[str] = frozenset()) -> None:
        self.leave_control_file_for = leave_control_file_for
        self.calls: list[list[str]] = []

    async def run(self, args: list[str]) -> int:
        self.calls.append(args)
        queue_path = next(a.split("=", 1)[1] for a in args if a.startswith("--input-file="))
        text = Path(queue_path).read_text(encoding="utf-8")
        for block in (item for item in text.strip().split("\n\n") if item.strip()):
            lines = block.splitlines()
            fields = dict(line.strip().split("=", 1) for line in lines[1:])
            path = Path(fields["dir"]) / fields["out"]
            path.parent.mkdir(parents=True, exist_ok=True)
            # Real aria2 writes straight to the final filename and tracks an
            # incomplete transfer with a sibling `.aria2` control file (no
            # `.part` suffix, unlike the httpx backend) -- so an incomplete
            # transfer still has partial bytes at `path`, plus the control file.
            path.write_bytes(f"bytes for {fields['out']}".encode())
            if fields["out"] in self.leave_control_file_for:
                path.with_suffix(path.suffix + ".aria2").write_bytes(b"")
        return 0


class _NoOpRunner:
    async def run(self, args: list[str]) -> int:
        return 1


def _single_asset_plan(tmp_path: Path, asset, run_id: str):
    bundle = SemanticBundle(
        bundle_id="b1", candidate_id=asset.candidate_id, bundle_type="raw",
        asset_ids=[asset.asset_id],
    )
    plan = AcquisitionPlan(
        plan_id=f"p_{run_id}", run_id=run_id, destination_root=str(tmp_path / "data" / "raw"),
    )
    return bundle, plan


@pytest.mark.asyncio
async def test_fake_runner_writes_bytes_and_reconciles_completed(tmp_path) -> None:
    fragments, _ = _real_assets()
    bundle, plan = _single_asset_plan(tmp_path, fragments, "r1")
    backend = Aria2TransferBackend(
        policy={"profile": "balanced"}, aria2_bin="/usr/bin/aria2c", runner=_FakeAria2Runner(),
    )
    receipts = await backend.execute(plan, [bundle], [fragments], {"b1"})
    assert len(receipts) == 1
    assert receipts[0].status == TransferReceiptStatus.COMPLETED
    assert receipts[0].bytes_written > 0
    assert backend.last_run_exit_codes  # run-level metadata was recorded


@pytest.mark.asyncio
async def test_control_file_present_fails_as_incomplete(tmp_path) -> None:
    fragments, _ = _real_assets()
    bundle, plan = _single_asset_plan(tmp_path, fragments, "r2")
    backend = Aria2TransferBackend(
        policy={}, aria2_bin="/usr/bin/aria2c",
        runner=_FakeAria2Runner(leave_control_file_for={fragments.file_name}),
    )
    receipts = await backend.execute(plan, [bundle], [fragments], {"b1"})
    assert receipts[0].status == TransferReceiptStatus.FAILED
    assert "control file" in receipts[0].error


@pytest.mark.asyncio
async def test_missing_output_file_fails(tmp_path) -> None:
    fragments, _ = _real_assets()
    bundle, plan = _single_asset_plan(tmp_path, fragments, "r3")
    backend = Aria2TransferBackend(policy={}, aria2_bin="/usr/bin/aria2c", runner=_NoOpRunner())
    receipts = await backend.execute(plan, [bundle], [fragments], {"b1"})
    assert receipts[0].status == TransferReceiptStatus.FAILED
    assert "no file" in receipts[0].error


@pytest.mark.asyncio
async def test_checksum_mismatch_fails_even_though_the_file_exists(tmp_path) -> None:
    _, zenodo = _real_assets()
    bundle, plan = _single_asset_plan(tmp_path, zenodo, "r4")
    backend = Aria2TransferBackend(policy={}, aria2_bin="/usr/bin/aria2c", runner=_FakeAria2Runner())
    receipts = await backend.execute(plan, [bundle], [zenodo], {"b1"})
    # The fake runner writes placeholder bytes, not the real file, so the
    # declared MD5 on this Zenodo row cannot possibly match.
    assert receipts[0].status == TransferReceiptStatus.FAILED
    assert receipts[0].error == "Checksum mismatch."
