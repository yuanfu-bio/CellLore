"""Per-file download state, alongside the catalog in the same SQLite file.

Owns its own table (`download_file_state`) rather than extending
`catalog.py`'s append-only schema -- a change here should never risk the
catalog's own guarantees. Every transition is also mirrored into the catalog
as a `file_transfer_record`, so the catalog stays the one place to look for a
run's complete audit trail even though this store is what a repair pass
actually queries.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from scdata_agent.domain.models import utc_now
from scdata_agent.domain.transfer_state import FileTransferStatus, is_allowed_transition
from scdata_agent.infrastructure.persistence.catalog import Catalog

DOWNLOAD_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS download_file_state (
    run_id TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, asset_id)
);
"""


class DownloadStateStore:
    def __init__(self, path: str | Path, *, catalog: Catalog | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(DOWNLOAD_STATE_SCHEMA)
        self._catalog = catalog

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def get(self, run_id: str, asset_id: str) -> FileTransferStatus | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM download_file_state WHERE run_id=? AND asset_id=?",
                (run_id, asset_id),
            ).fetchone()
        return FileTransferStatus(row["status"]) if row else None

    def transition(
        self,
        run_id: str,
        asset_id: str,
        target: FileTransferStatus,
        *,
        error: str | None = None,
    ) -> FileTransferStatus:
        current = self.get(run_id, asset_id)
        # A file with no prior record has no state to violate; the first
        # transition just establishes one. Every transition after that must
        # follow the state machine.
        if current is not None and not is_allowed_transition(current, target):
            raise ValueError(
                f"Illegal transition for {asset_id}: {current.value} -> {target.value}"
            )
        now = utc_now().isoformat()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT attempt FROM download_file_state WHERE run_id=? AND asset_id=?",
                (run_id, asset_id),
            ).fetchone()
            previous_attempt = existing["attempt"] if existing is not None else 0
            # Each entry into DOWNLOADING is a new attempt; every other
            # transition just relabels the outcome of the current one.
            next_attempt = (
                previous_attempt + 1
                if target == FileTransferStatus.DOWNLOADING
                else previous_attempt
            )
            connection.execute(
                """
                INSERT INTO download_file_state
                    (run_id, asset_id, status, attempt, last_error, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, asset_id) DO UPDATE SET
                    status=excluded.status, attempt=excluded.attempt,
                    last_error=excluded.last_error, updated_at=excluded.updated_at
                """,
                (run_id, asset_id, target.value, next_attempt, error, now),
            )
        if self._catalog is not None:
            self._catalog.append(
                record_id=f"{run_id}:{asset_id}:{now}",
                record_type="file_transfer_record",
                run_id=run_id,
                value={
                    "asset_id": asset_id,
                    "run_id": run_id,
                    "status": target.value,
                    "attempt": next_attempt,
                    "error": error,
                    "updated_at": now,
                    "schema_version": "1.0",
                },
            )
        return target

    def summary(self, run_id: str) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS n FROM download_file_state WHERE run_id=? GROUP BY status",
                (run_id,),
            ).fetchall()
        return {row["status"]: row["n"] for row in rows}

    def records(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM download_file_state WHERE run_id=? ORDER BY asset_id",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]
