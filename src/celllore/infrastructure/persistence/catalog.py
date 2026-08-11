"""Append-only cross-run catalog for normalized curation artifacts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from celllore.domain.lifecycle import BundleStatus, validate_bundle_transition
from celllore.domain.models import utc_now

CATALOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog_record (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id TEXT NOT NULL,
    record_type TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    run_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_catalog_record_identity
ON catalog_record(record_type, record_id, sequence);
CREATE INDEX IF NOT EXISTS idx_catalog_record_run
ON catalog_record(run_id, sequence);
CREATE TABLE IF NOT EXISTS catalog_bundle_state (
    bundle_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    version INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class Catalog:
    """Persist immutable records while maintaining a guarded lifecycle cursor."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(CATALOG_SCHEMA)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def append(
        self,
        *,
        record_id: str,
        record_type: str,
        run_id: str,
        value: BaseModel | dict[str, Any],
        schema_version: str | None = None,
    ) -> str:
        payload = (
            value.model_dump(mode="json")
            if isinstance(value, BaseModel)
            else value
        )
        effective_schema_version = (
            schema_version or str(payload.get("schema_version", "2.0"))
        )
        rendered = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO catalog_record
                (record_id, record_type, schema_version, run_id, content_hash,
                 payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    record_type,
                    effective_schema_version,
                    run_id,
                    digest,
                    rendered,
                    utc_now().isoformat(),
                ),
            )
        return digest

    def latest(
        self, record_type: str, record_id: str
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM catalog_record
                WHERE record_type=? AND record_id=?
                ORDER BY sequence DESC LIMIT 1
                """,
                (record_type, record_id),
            ).fetchone()
        return self._row(row) if row else None

    def list_records(
        self,
        *,
        record_type: str | None = None,
        run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[str] = []
        if record_type is not None:
            clauses.append("record_type=?")
            values.append(record_type)
        if run_id is not None:
            clauses.append("run_id=?")
            values.append(run_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM catalog_record{where} ORDER BY sequence",
                values,
            ).fetchall()
        return [self._row(row) for row in rows]

    def transition_bundle(
        self,
        bundle_id: str,
        target: BundleStatus,
        *,
        run_id: str,
        initial: BundleStatus | None = None,
    ) -> BundleStatus:
        now = utc_now().isoformat()
        state_key = f"{run_id}:{bundle_id}"
        with self.connect() as connection:
            row = connection.execute(
                "SELECT status, version FROM catalog_bundle_state WHERE bundle_id=?",
                (state_key,),
            ).fetchone()
            if row is None:
                current = initial or target
                if initial is not None:
                    validate_bundle_transition(initial, target)
                connection.execute(
                    """
                    INSERT INTO catalog_bundle_state
                    (bundle_id, status, version, run_id, updated_at)
                    VALUES (?, ?, 1, ?, ?)
                    """,
                    (state_key, target.value, run_id, now),
                )
                return current if initial is None else target
            current = BundleStatus(row["status"])
            validate_bundle_transition(current, target)
            connection.execute(
                """
                UPDATE catalog_bundle_state
                SET status=?, version=?, run_id=?, updated_at=?
                WHERE bundle_id=?
                """,
                (target.value, int(row["version"]) + 1, run_id, now, state_key),
            )
        return target

    def bundle_status(
        self, bundle_id: str, *, run_id: str | None = None
    ) -> BundleStatus | None:
        with self.connect() as connection:
            if run_id is not None:
                row = connection.execute(
                    "SELECT status FROM catalog_bundle_state WHERE bundle_id=?",
                    (f"{run_id}:{bundle_id}",),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT status FROM catalog_bundle_state WHERE bundle_id=? OR bundle_id LIKE ? ORDER BY updated_at DESC LIMIT 1",
                    (bundle_id, f"%:{bundle_id}"),
                ).fetchone()
        return BundleStatus(row["status"]) if row else None

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "sequence": row["sequence"],
            "record_id": row["record_id"],
            "record_type": row["record_type"],
            "schema_version": row["schema_version"],
            "run_id": row["run_id"],
            "content_hash": row["content_hash"],
            "payload": json.loads(row["payload_json"]),
            "created_at": datetime.fromisoformat(row["created_at"]),
        }
