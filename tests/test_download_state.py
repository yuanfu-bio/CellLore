"""The download state machine, httpx-based failure classification, the
DownloadStateStore, and repair's most important line: it must refuse to run
at all unless a human already approved a download.
"""

from __future__ import annotations

import httpx
import pytest

from scdata_agent.application.collaboration import CollaborationLedger, DecisionCheckpoint
from scdata_agent.application.manifest_repair import (
    RepairNotAuthorized,
    classify_repair_candidates,
)
from scdata_agent.domain.transfer_state import (
    ALLOWED_FILE_TRANSITIONS,
    RETRYABLE_CLASSES,
    FailureClass,
    FileTransferStatus,
    classify_http_outcome,
    is_allowed_transition,
)
from scdata_agent.infrastructure.download_triage import probe_and_classify
from scdata_agent.infrastructure.persistence.catalog import Catalog
from scdata_agent.infrastructure.persistence.download_store import DownloadStateStore


def test_every_status_has_an_entry_in_the_transition_table():
    assert set(ALLOWED_FILE_TRANSITIONS) == set(FileTransferStatus)


def test_legal_transitions():
    assert is_allowed_transition(FileTransferStatus.PENDING, FileTransferStatus.DOWNLOADING)
    assert is_allowed_transition(FileTransferStatus.DOWNLOADING, FileTransferStatus.COMPLETED)
    assert is_allowed_transition(FileTransferStatus.DOWNLOADING, FileTransferStatus.RETRYABLE)
    assert is_allowed_transition(FileTransferStatus.RETRYABLE, FileTransferStatus.DOWNLOADING)
    assert is_allowed_transition(FileTransferStatus.COMPLETED, FileTransferStatus.VALIDATED)
    assert is_allowed_transition(FileTransferStatus.VALIDATION_FAILED, FileTransferStatus.DOWNLOADING)


def test_illegal_transitions():
    assert not is_allowed_transition(FileTransferStatus.PENDING, FileTransferStatus.COMPLETED)
    assert not is_allowed_transition(FileTransferStatus.VALIDATED, FileTransferStatus.DOWNLOADING)
    # MANIFEST_ERROR is terminal: a 404 is a fact about the manifest, not
    # something a retry loop can fix.
    assert not is_allowed_transition(FileTransferStatus.MANIFEST_ERROR, FileTransferStatus.DOWNLOADING)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (200, FailureClass.RETRYABLE),
        (206, FailureClass.RETRYABLE),
        (404, FailureClass.MANIFEST_ERROR_404),
        (403, FailureClass.RATE_OR_ACCESS_LIMIT),
        (429, FailureClass.RATE_OR_ACCESS_LIMIT),
        (503, FailureClass.SERVER_TRANSIENT),
        (500, FailureClass.SERVER_TRANSIENT),
    ],
)
def test_classify_http_outcome_by_status(status, expected):
    assert classify_http_outcome(status, None) == expected


def test_classify_http_outcome_transport_error_is_network_error():
    assert classify_http_outcome(None, "ConnectError: refused") == FailureClass.NETWORK_ERROR


def test_manifest_error_404_is_never_retryable():
    assert FailureClass.MANIFEST_ERROR_404 not in RETRYABLE_CLASSES


@pytest.mark.asyncio
async def test_probe_and_classify_uses_a_range_request_and_reads_status():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["range"] = request.headers.get("Range")
        return httpx.Response(404, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        probe, classification = await probe_and_classify(client, "https://files.test/x.gz")
    assert seen["range"] == "bytes=0-0"
    assert probe["status"] == 404
    assert classification == FailureClass.MANIFEST_ERROR_404


@pytest.mark.asyncio
async def test_probe_and_classify_handles_transport_errors_offline():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        probe, classification = await probe_and_classify(client, "https://files.test/x.gz")
    assert probe["status"] is None
    assert classification == FailureClass.NETWORK_ERROR


def test_download_state_store_round_trip(tmp_path):
    store = DownloadStateStore(tmp_path / "system.sqlite")
    assert store.get("run1", "asset1") is None
    store.transition("run1", "asset1", FileTransferStatus.PENDING)
    store.transition("run1", "asset1", FileTransferStatus.DOWNLOADING)
    store.transition("run1", "asset1", FileTransferStatus.COMPLETED)
    store.transition("run1", "asset1", FileTransferStatus.VALIDATED)
    assert store.get("run1", "asset1") == FileTransferStatus.VALIDATED
    assert store.summary("run1") == {"validated": 1}
    with pytest.raises(ValueError):
        store.transition("run1", "asset1", FileTransferStatus.DOWNLOADING)


def test_download_state_store_mirrors_into_the_catalog(tmp_path):
    path = tmp_path / "system.sqlite"
    catalog = Catalog(path)
    store = DownloadStateStore(path, catalog=catalog)
    store.transition("run1", "asset1", FileTransferStatus.PENDING)
    store.transition("run1", "asset1", FileTransferStatus.DOWNLOADING)
    records = catalog.list_records(record_type="file_transfer_record", run_id="run1")
    assert [item["payload"]["status"] for item in records] == ["pending", "downloading"]


@pytest.mark.asyncio
async def test_repair_refuses_when_download_checkpoint_is_still_open(tmp_path):
    ledger = CollaborationLedger(tmp_path / "system.sqlite")
    run = ledger.create_run("manifest run", {"source": "manifest"})
    ledger.add_checkpoint(
        DecisionCheckpoint(
            checkpoint_id="checkpoint_1",
            run_id=run.run_id,
            kind="download_selection",
            title="Approve semantic bundles for transfer",
            recommendation="test",
            options=[{"id": "recommended", "label": "Download recommended bundles"}],
        )
    )
    with pytest.raises(RepairNotAuthorized):
        await classify_repair_candidates(ledger, run.run_id)


@pytest.mark.asyncio
async def test_repair_refuses_when_no_download_checkpoint_exists_at_all(tmp_path):
    ledger = CollaborationLedger(tmp_path / "system.sqlite")
    run = ledger.create_run("manifest run", {"source": "manifest"})
    with pytest.raises(RepairNotAuthorized):
        await classify_repair_candidates(ledger, run.run_id)


@pytest.mark.asyncio
async def test_repair_refuses_when_resolved_to_plan_only(tmp_path):
    ledger = CollaborationLedger(tmp_path / "system.sqlite")
    run = ledger.create_run("manifest run", {"source": "manifest"})
    checkpoint = ledger.add_checkpoint(
        DecisionCheckpoint(
            checkpoint_id="checkpoint_1",
            run_id=run.run_id,
            kind="download_selection",
            title="Approve semantic bundles for transfer",
            recommendation="test",
            options=[{"id": "plan_only", "label": "Keep plans only"}],
        )
    )
    ledger.resolve_checkpoint(checkpoint.checkpoint_id, {"option": "plan_only"})
    with pytest.raises(RepairNotAuthorized):
        await classify_repair_candidates(ledger, run.run_id)
