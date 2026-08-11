"""Per-file download lifecycle across possibly many attempts.

``TransferReceiptStatus`` (domain/lifecycle.py) describes one already-finished
attempt. It has nothing to say about whether a failed attempt is worth a
retry, or whether retrying at all would be pointless -- a 404 and a stalled
connection are both "failed" at that level, but only one of them should ever
be retried. This module is what a repair pass reasons over instead.
"""

from __future__ import annotations

from enum import StrEnum


class FileTransferStatus(StrEnum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    RETRYABLE = "retryable"
    MANIFEST_ERROR = "manifest_error"
    RATE_LIMITED = "rate_limited"
    VALIDATED = "validated"
    VALIDATION_FAILED = "validation_failed"


ALLOWED_FILE_TRANSITIONS: dict[FileTransferStatus, frozenset[FileTransferStatus]] = {
    FileTransferStatus.PENDING: frozenset({FileTransferStatus.DOWNLOADING}),
    FileTransferStatus.DOWNLOADING: frozenset(
        {
            FileTransferStatus.COMPLETED,
            FileTransferStatus.RETRYABLE,
            FileTransferStatus.MANIFEST_ERROR,
            FileTransferStatus.RATE_LIMITED,
        }
    ),
    FileTransferStatus.RETRYABLE: frozenset({FileTransferStatus.DOWNLOADING}),
    FileTransferStatus.RATE_LIMITED: frozenset({FileTransferStatus.DOWNLOADING}),
    # Terminal: a 404 or a withdrawn file is a fact about the manifest, not
    # about the network, so nothing here retries it -- the manifest is what
    # needs auditing.
    FileTransferStatus.MANIFEST_ERROR: frozenset(),
    FileTransferStatus.COMPLETED: frozenset(
        {FileTransferStatus.VALIDATED, FileTransferStatus.VALIDATION_FAILED}
    ),
    # A corrupt file can still be re-fetched; the bytes were wrong, not the URL.
    FileTransferStatus.VALIDATION_FAILED: frozenset({FileTransferStatus.DOWNLOADING}),
    FileTransferStatus.VALIDATED: frozenset(),
}


def is_allowed_transition(
    current: FileTransferStatus, target: FileTransferStatus
) -> bool:
    return target in ALLOWED_FILE_TRANSITIONS.get(current, frozenset())


class FailureClass(StrEnum):
    RETRYABLE = "retryable"
    MANIFEST_ERROR_404 = "manifest_error_404"
    RATE_OR_ACCESS_LIMIT = "rate_or_access_limit"
    SERVER_TRANSIENT = "server_transient"
    NETWORK_ERROR = "network_error"
    UNKNOWN = "unknown"


#: A 404 is deliberately absent here: it means the manifest names a file, an
#: accession, or a URL that is wrong, or that the record was withdrawn.
#: Retrying it 15 times finds out nothing a repair pass doesn't already know.
RETRYABLE_CLASSES = frozenset(
    {FailureClass.RETRYABLE, FailureClass.SERVER_TRANSIENT, FailureClass.NETWORK_ERROR}
)


def classify_http_outcome(
    status: int | None, transport_error: str | None = None
) -> FailureClass:
    """Classify one probe outcome for retry policy, not for display.

    ``status`` is the HTTP status of the last response received, if any;
    ``transport_error`` names the exception when the request never got a
    response at all (DNS failure, connection reset, timeout).
    """
    if status in {200, 206}:
        return FailureClass.RETRYABLE
    if status == 404:
        return FailureClass.MANIFEST_ERROR_404
    if status in {401, 403, 429}:
        return FailureClass.RATE_OR_ACCESS_LIMIT
    if status is not None and 500 <= status < 600:
        return FailureClass.SERVER_TRANSIENT
    if transport_error:
        return FailureClass.NETWORK_ERROR
    return FailureClass.UNKNOWN
