"""SQLite evidence ledger for reproducible discovery runs."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from scdata_agent.domain.models import (
    DatasetCandidate,
    DiscoveryRunResult,
    FeasibilityReport,
    QueryPlan,
    ResearchRequirement,
    SourceRecord,
    utc_now,
)


SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS discovery_run (
    run_id TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    requirement_json TEXT NOT NULL,
    query_plan_json TEXT NOT NULL,
    status TEXT NOT NULL,
    errors_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_record (
    run_id TEXT NOT NULL,
    record_id TEXT NOT NULL,
    source_name TEXT NOT NULL,
    external_id TEXT NOT NULL,
    record_json TEXT NOT NULL,
    PRIMARY KEY (run_id, record_id),
    FOREIGN KEY (run_id) REFERENCES discovery_run(run_id)
);

CREATE TABLE IF NOT EXISTS candidate (
    run_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    candidate_json TEXT NOT NULL,
    PRIMARY KEY (run_id, candidate_id),
    FOREIGN KEY (run_id) REFERENCES discovery_run(run_id)
);

CREATE TABLE IF NOT EXISTS evidence (
    run_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    PRIMARY KEY (run_id, candidate_id, evidence_id),
    FOREIGN KEY (run_id, candidate_id)
        REFERENCES candidate(run_id, candidate_id)
);

CREATE TABLE IF NOT EXISTS feasibility_report (
    run_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    report_id TEXT NOT NULL,
    report_json TEXT NOT NULL,
    PRIMARY KEY (run_id, report_id),
    FOREIGN KEY (run_id, candidate_id)
        REFERENCES candidate(run_id, candidate_id)
);
"""


class EvidenceLedger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    def save_run(self, result: DiscoveryRunResult) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO discovery_run
                (run_id, question, requirement_json, query_plan_json, status,
                 errors_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.run_id,
                    result.requirement.question,
                    result.requirement.model_dump_json(),
                    result.query_plan.model_dump_json(),
                    "completed_with_errors" if result.errors else "completed",
                    _json_list(result.errors),
                    utc_now().isoformat(),
                ),
            )
            for record in result.source_records:
                self._save_source_record(connection, result.run_id, record)
            reports_by_candidate = {
                report.candidate_id: report
                for report in result.feasibility_reports
            }
            for candidate in result.candidates:
                self._save_candidate(connection, result.run_id, candidate)
                report = reports_by_candidate.get(candidate.candidate_id)
                if report:
                    self._save_report(connection, result.run_id, report)

    def _save_source_record(
        self, connection: sqlite3.Connection, run_id: str, record: SourceRecord
    ) -> None:
        connection.execute(
            """
            INSERT OR REPLACE INTO source_record
            (run_id, record_id, source_name, external_id, record_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                run_id,
                record.record_id,
                record.source_name,
                record.external_id,
                record.model_dump_json(),
            ),
        )

    def _save_candidate(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        candidate: DatasetCandidate,
    ) -> None:
        connection.execute(
            """
            INSERT OR REPLACE INTO candidate
            (run_id, candidate_id, candidate_json)
            VALUES (?, ?, ?)
            """,
            (run_id, candidate.candidate_id, candidate.model_dump_json()),
        )
        for evidence in candidate.evidence:
            connection.execute(
                """
                INSERT OR REPLACE INTO evidence
                (run_id, candidate_id, evidence_id, evidence_json)
                VALUES (?, ?, ?, ?)
                """,
                (
                    run_id,
                    candidate.candidate_id,
                    evidence.evidence_id,
                    evidence.model_dump_json(),
                ),
            )

    def _save_report(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        report: FeasibilityReport,
    ) -> None:
        connection.execute(
            """
            INSERT OR REPLACE INTO feasibility_report
            (run_id, candidate_id, report_id, report_json)
            VALUES (?, ?, ?, ?)
            """,
            (
                run_id,
                report.candidate_id,
                report.report_id,
                report.model_dump_json(),
            ),
        )

    def list_runs(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT run_id, question, status, created_at
                FROM discovery_run
                ORDER BY created_at DESC
                """
            ).fetchall()

    def load_requirement(self, run_id: str) -> ResearchRequirement:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT requirement_json FROM discovery_run WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown run_id: {run_id}")
        return ResearchRequirement.model_validate_json(row["requirement_json"])

    def load_plan(self, run_id: str) -> QueryPlan:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT query_plan_json FROM discovery_run WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown run_id: {run_id}")
        return QueryPlan.model_validate_json(row["query_plan_json"])

    def load_candidates(self, run_id: str) -> list[DatasetCandidate]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT candidate_json
                FROM candidate
                WHERE run_id = ?
                ORDER BY candidate_id
                """,
                (run_id,),
            ).fetchall()
        return [
            DatasetCandidate.model_validate_json(row["candidate_json"])
            for row in rows
        ]


def _json_list(values: list[str]) -> str:
    import json

    return json.dumps(values, ensure_ascii=False)

