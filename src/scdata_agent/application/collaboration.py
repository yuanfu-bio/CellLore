"""Persistent contracts and SQLite blackboard for multi-agent collaboration."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import Field

from scdata_agent.domain.identifiers import stable_id
from scdata_agent.domain.models import StrictModel, utc_now


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_DECISION = "waiting_for_decision"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class AgentName(StrEnum):
    SUPERVISOR = "supervisor"
    RESEARCH_PLANNER = "research_planner"
    RESOURCE_DISCOVERY = "resource_discovery"
    # A curated manifest is the second way data enters the system: a human has
    # already audited a study down to the file, and the rows are turned into
    # the same contracts discovery produces.
    MANIFEST_INGEST = "manifest_ingest"
    EVIDENCE_CURATOR = "evidence_curator"
    ASSET_SCOUT = "asset_scout"
    PREFLIGHT_ASSESSMENT = "preflight_assessment"
    ACQUISITION_STRATEGIST = "acquisition_strategist"
    DATASET_CURATOR = "dataset_curator"
    ASSET_TRANSFER = "asset_transfer"
    QUALITY_ASSESSMENT = "quality_assessment"
    LINEAGE_LEAKAGE = "lineage_leakage"
    CORPUS_GOVERNANCE = "corpus_governance"


class ArtifactKind(StrEnum):
    RESEARCH_PLAN = "research_plan"
    QUERY_PLAN = "query_plan"
    DISCOVERY_RESULT = "discovery_result"
    RESOURCE_POINTERS = "resource_pointers"
    EVIDENCE_REVIEW = "evidence_review"
    EVIDENCE_GRAPH = "evidence_graph"
    EVIDENCE_ESCALATION = "evidence_escalation"
    ASSET_INVENTORY_DELTA = "asset_inventory_delta"
    ASSET_INVENTORY = "asset_inventory"
    MANIFEST_INGESTION = "manifest_ingestion"
    PREFLIGHT_REPORT = "preflight_report"
    ACQUISITION_PLAN = "acquisition_plan"
    TRANSFER_RECEIPTS = "transfer_receipts"
    MATERIALIZATION_MANIFEST = "materialization_manifest"
    MATERIALIZED_QC = "materialized_qc"
    CORPUS_SNAPSHOT = "corpus_snapshot"
    ASSET_PLAN = "asset_plan"
    QUALITY_REPORT = "quality_report"
    LINEAGE_REPORT = "lineage_report"
    CORPUS_REPORT = "corpus_report"
    CHAT = "chat"
    LLM_CALL = "llm_call"


class CheckpointStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    CANCELLED = "cancelled"


class AgentArtifact(StrictModel):
    artifact_id: str
    run_id: str
    kind: ArtifactKind
    producer: AgentName
    payload: dict[str, Any]
    evidence_ids: list[str] = Field(default_factory=list)
    parent_artifact_ids: list[str] = Field(default_factory=list)
    schema_version: str = "2.0"
    created_at: datetime = Field(default_factory=utc_now)


class AgentMessage(StrictModel):
    message_id: str
    run_id: str
    sender: AgentName
    recipient: AgentName
    summary: str
    recommendation: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    created_at: datetime = Field(default_factory=utc_now)


class DecisionCheckpoint(StrictModel):
    checkpoint_id: str
    run_id: str
    kind: str
    title: str
    recommendation: str
    options: list[dict[str, Any]]
    status: CheckpointStatus = CheckpointStatus.OPEN
    resolution: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=utc_now)
    resolved_at: datetime | None = None


class TaskRun(StrictModel):
    run_id: str
    question: str
    status: RunStatus
    policy: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AgentTask(StrictModel):
    task_id: str
    run_id: str
    agent: AgentName
    status: TaskStatus
    input_artifact_ids: list[str] = Field(default_factory=list)
    output_artifact_ids: list[str] = Field(default_factory=list)
    error: str | None = None
    attempt: int = 1
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


COLLABORATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_run (
    run_id TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    status TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_task (
    task_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    status TEXT NOT NULL,
    input_artifact_ids_json TEXT NOT NULL,
    output_artifact_ids_json TEXT NOT NULL,
    error TEXT,
    attempt INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES task_run(run_id)
);
CREATE TABLE IF NOT EXISTS agent_artifact (
    artifact_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    producer TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL,
    parent_artifact_ids_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES task_run(run_id)
);
CREATE TABLE IF NOT EXISTS agent_message (
    message_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    sender TEXT NOT NULL,
    recipient TEXT NOT NULL,
    message_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES task_run(run_id)
);
CREATE TABLE IF NOT EXISTS decision_checkpoint (
    checkpoint_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    checkpoint_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY (run_id) REFERENCES task_run(run_id)
);
CREATE TABLE IF NOT EXISTS collaboration_event (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES task_run(run_id)
);
CREATE INDEX IF NOT EXISTS idx_collaboration_event_run ON collaboration_event(run_id, event_id);
"""


class CollaborationLedger:
    """Append-oriented blackboard shared by the supervisor and specialists."""

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
            connection.executescript(COLLABORATION_SCHEMA)

    def create_run(self, question: str, policy: dict[str, Any] | None = None) -> TaskRun:
        now = utc_now()
        run = TaskRun(
            run_id=stable_id("agent_run", f"{question}:{now.isoformat()}"),
            question=question,
            status=RunStatus.QUEUED,
            policy=policy or {},
            created_at=now,
            updated_at=now,
        )
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO task_run VALUES (?, ?, ?, ?, ?, ?)",
                (run.run_id, run.question, run.status.value, _dump(run.policy), now.isoformat(), now.isoformat()),
            )
        self.add_event(run.run_id, "run_created", {"status": run.status.value})
        return run

    def list_runs(self) -> list[TaskRun]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM task_run ORDER BY created_at DESC").fetchall()
        return [TaskRun(
            run_id=row["run_id"], question=row["question"], status=RunStatus(row["status"]),
            policy=json.loads(row["policy_json"]), created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        ) for row in rows]

    def get_run(self, run_id: str) -> TaskRun:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM task_run WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown run_id: {run_id}")
        return TaskRun(
            run_id=row["run_id"], question=row["question"], status=RunStatus(row["status"]),
            policy=json.loads(row["policy_json"]), created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def set_run_status(self, run_id: str, status: RunStatus) -> None:
        now = utc_now().isoformat()
        with self.connect() as connection:
            connection.execute("UPDATE task_run SET status = ?, updated_at = ? WHERE run_id = ?", (status.value, now, run_id))
        self.add_event(run_id, "run_status", {"status": status.value})

    def start_task(self, run_id: str, agent: AgentName, input_artifact_ids: list[str]) -> AgentTask:
        now = utc_now()
        task = AgentTask(
            task_id=stable_id("task", f"{run_id}:{agent.value}:{now.isoformat()}"),
            run_id=run_id, agent=agent, status=TaskStatus.RUNNING,
            input_artifact_ids=input_artifact_ids, created_at=now, updated_at=now,
        )
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO agent_task VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task.task_id, run_id, agent.value, task.status.value, _dump(input_artifact_ids), _dump([]), None, 1, now.isoformat(), now.isoformat()),
            )
        self.add_event(run_id, "task_started", {"task_id": task.task_id, "agent": agent.value})
        return task

    def finish_task(self, task_id: str, output_artifact_ids: list[str], error: str | None = None) -> None:
        status = TaskStatus.FAILED if error else TaskStatus.COMPLETED
        now = utc_now().isoformat()
        with self.connect() as connection:
            row = connection.execute("SELECT run_id, agent FROM agent_task WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown task_id: {task_id}")
            connection.execute(
                "UPDATE agent_task SET status=?, output_artifact_ids_json=?, error=?, updated_at=? WHERE task_id=?",
                (status.value, _dump(output_artifact_ids), error, now, task_id),
            )
        self.add_event(row["run_id"], "task_finished", {"task_id": task_id, "agent": row["agent"], "status": status.value, "error": error})

    def add_artifact(self, artifact: AgentArtifact) -> AgentArtifact:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO agent_artifact VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (artifact.artifact_id, artifact.run_id, artifact.kind.value, artifact.producer.value,
                 _dump(artifact.payload), _dump(artifact.evidence_ids), _dump(artifact.parent_artifact_ids), artifact.created_at.isoformat()),
            )
        self.add_event(artifact.run_id, "artifact_created", {"artifact_id": artifact.artifact_id, "kind": artifact.kind.value, "producer": artifact.producer.value})
        return artifact

    def list_artifacts(self, run_id: str) -> list[AgentArtifact]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM agent_artifact WHERE run_id=? ORDER BY created_at", (run_id,)).fetchall()
        return [AgentArtifact(
            artifact_id=row["artifact_id"], run_id=row["run_id"], kind=ArtifactKind(row["kind"]), producer=AgentName(row["producer"]),
            payload=json.loads(row["payload_json"]), evidence_ids=json.loads(row["evidence_ids_json"]),
            parent_artifact_ids=json.loads(row["parent_artifact_ids_json"]), created_at=datetime.fromisoformat(row["created_at"]),
        ) for row in rows]

    def add_message(self, message: AgentMessage) -> AgentMessage:
        with self.connect() as connection:
            connection.execute("INSERT INTO agent_message VALUES (?, ?, ?, ?, ?, ?)", (message.message_id, message.run_id, message.sender.value, message.recipient.value, message.model_dump_json(), message.created_at.isoformat()))
        self.add_event(message.run_id, "agent_message", {"message_id": message.message_id, "sender": message.sender.value, "recipient": message.recipient.value})
        return message

    def list_messages(self, run_id: str) -> list[AgentMessage]:
        with self.connect() as connection:
            rows = connection.execute("SELECT message_json FROM agent_message WHERE run_id=? ORDER BY created_at", (run_id,)).fetchall()
        return [AgentMessage.model_validate_json(row["message_json"]) for row in rows]

    def add_checkpoint(self, checkpoint: DecisionCheckpoint) -> DecisionCheckpoint:
        with self.connect() as connection:
            connection.execute("INSERT INTO decision_checkpoint VALUES (?, ?, ?, ?, ?, ?, ?)", (checkpoint.checkpoint_id, checkpoint.run_id, checkpoint.kind, checkpoint.status.value, checkpoint.model_dump_json(), checkpoint.created_at.isoformat(), None))
        self.add_event(checkpoint.run_id, "checkpoint_opened", {"checkpoint_id": checkpoint.checkpoint_id, "kind": checkpoint.kind})
        return checkpoint

    def list_checkpoints(self, run_id: str) -> list[DecisionCheckpoint]:
        with self.connect() as connection:
            rows = connection.execute("SELECT checkpoint_json FROM decision_checkpoint WHERE run_id=? ORDER BY created_at", (run_id,)).fetchall()
        return [DecisionCheckpoint.model_validate_json(row["checkpoint_json"]) for row in rows]

    def resolve_checkpoint(self, checkpoint_id: str, resolution: dict[str, Any]) -> DecisionCheckpoint:
        with self.connect() as connection:
            row = connection.execute("SELECT checkpoint_json FROM decision_checkpoint WHERE checkpoint_id=?", (checkpoint_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown checkpoint_id: {checkpoint_id}")
            checkpoint = DecisionCheckpoint.model_validate_json(row["checkpoint_json"])
            if checkpoint.status != CheckpointStatus.OPEN:
                raise ValueError(f"Checkpoint {checkpoint_id} is not open")
            resolved = checkpoint.model_copy(update={"status": CheckpointStatus.RESOLVED, "resolution": resolution, "resolved_at": utc_now()})
            connection.execute("UPDATE decision_checkpoint SET status=?, checkpoint_json=?, resolved_at=? WHERE checkpoint_id=?", (resolved.status.value, resolved.model_dump_json(), resolved.resolved_at.isoformat(), checkpoint_id))
        self.add_event(resolved.run_id, "checkpoint_resolved", {"checkpoint_id": checkpoint_id, "resolution": resolution})
        return resolved

    def list_tasks(self, run_id: str) -> list[AgentTask]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM agent_task WHERE run_id=? ORDER BY created_at", (run_id,)).fetchall()
        return [AgentTask(
            task_id=row["task_id"], run_id=row["run_id"], agent=AgentName(row["agent"]), status=TaskStatus(row["status"]),
            input_artifact_ids=json.loads(row["input_artifact_ids_json"]), output_artifact_ids=json.loads(row["output_artifact_ids_json"]),
            error=row["error"], attempt=row["attempt"], created_at=datetime.fromisoformat(row["created_at"]), updated_at=datetime.fromisoformat(row["updated_at"]),
        ) for row in rows]

    def add_event(self, run_id: str, event_type: str, payload: dict[str, Any]) -> int:
        now = utc_now().isoformat()
        with self.connect() as connection:
            cursor = connection.execute("INSERT INTO collaboration_event (run_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)", (run_id, event_type, _dump(payload), now))
            return int(cursor.lastrowid)

    def events_since(self, run_id: str, after_id: int = 0) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM collaboration_event WHERE run_id=? AND event_id>? ORDER BY event_id", (run_id, after_id)).fetchall()
        return [{"event_id": row["event_id"], "type": row["event_type"], "payload": json.loads(row["payload_json"]), "created_at": row["created_at"]} for row in rows]


def artifact(run_id: str, kind: ArtifactKind, producer: AgentName, payload: dict[str, Any], *, evidence_ids: list[str] | None = None, parents: list[str] | None = None) -> AgentArtifact:
    versioned_payload = {"schema_version": "2.0", **payload}
    return AgentArtifact(
        artifact_id=stable_id("artifact", f"{run_id}:{kind.value}:{producer.value}:{utc_now().isoformat()}"),
        run_id=run_id, kind=kind, producer=producer, payload=versioned_payload,
        schema_version=str(versioned_payload.get("schema_version", "2.0")),
        evidence_ids=evidence_ids or [], parent_artifact_ids=parents or [],
    )


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
