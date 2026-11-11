"""Task records in SQLite.

`tasks` holds one row per ticket. `handoffs` holds one row per move of a
ticket into "Ready for agent", which is what makes a repeat trigger for the
same move a no-op.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from .task import Task, TaskStatus, TaskType

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id                  INTEGER PRIMARY KEY,
    source              TEXT NOT NULL,
    ticket_key          TEXT NOT NULL,
    type                TEXT NOT NULL CHECK (type IN ('bug', 'chore', 'feature')),
    story               TEXT NOT NULL,
    acceptance_criteria TEXT NOT NULL,  -- JSON array of strings
    out_of_scope        TEXT NOT NULL,  -- JSON array of strings
    pointers            TEXT NOT NULL,  -- JSON array of strings
    repo                TEXT NOT NULL,
    status              TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE (source, ticket_key)
);
CREATE TABLE IF NOT EXISTS handoffs (
    source           TEXT NOT NULL,
    ticket_key       TEXT NOT NULL,
    status_change_id TEXT NOT NULL,
    outcome          TEXT NOT NULL,  -- created | reentered
    received_at      TEXT NOT NULL,
    acknowledged_at  TEXT,           -- when the ticket was told; NULL until then
    PRIMARY KEY (source, ticket_key, status_change_id)
);
"""


class Handoff(StrEnum):
    CREATED = "created"  # first time the ticket was handed over: a new task
    REENTERED = "reentered"  # the ticket came back to the column: same task
    DUPLICATE = "duplicate"  # this move was already recorded


@dataclass(frozen=True)
class StoredTask:
    task: Task
    status: TaskStatus
    created_at: str
    updated_at: str


class Store:
    def __init__(self, path: str | Path):
        # Autocommit mode; transactions are opened explicitly below.
        self._db = sqlite3.connect(str(path), isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    def record_handoff(self, task: Task, status_change_id: str) -> Handoff:
        """Record that a ticket was handed over, creating or refreshing its task.

        A repeat of a move already recorded changes nothing.
        """
        now = _now()
        key = (task.source, task.ticket_key)
        lists = [json.dumps(items) for items in (task.acceptance_criteria, task.out_of_scope, task.pointers)]
        self._db.execute("BEGIN IMMEDIATE")
        try:
            seen = self._db.execute(
                "SELECT 1 FROM handoffs WHERE source = ? AND ticket_key = ? AND status_change_id = ?",
                (*key, status_change_id),
            ).fetchone()
            if seen:
                self._db.execute("ROLLBACK")
                return Handoff.DUPLICATE
            exists = self._db.execute(
                "SELECT 1 FROM tasks WHERE source = ? AND ticket_key = ?", key
            ).fetchone()
            if exists:
                outcome = Handoff.REENTERED
                self._db.execute(
                    "UPDATE tasks SET type = ?, story = ?, acceptance_criteria = ?, out_of_scope = ?,"
                    " pointers = ?, repo = ?, updated_at = ? WHERE source = ? AND ticket_key = ?",
                    (str(task.type), task.story, *lists, task.repo, now, *key),
                )
            else:
                outcome = Handoff.CREATED
                self._db.execute(
                    "INSERT INTO tasks (source, ticket_key, type, story, acceptance_criteria, out_of_scope,"
                    " pointers, repo, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (*key, str(task.type), task.story, *lists, task.repo, str(TaskStatus.RECEIVED), now, now),
                )
            self._db.execute(
                "INSERT INTO handoffs (source, ticket_key, status_change_id, outcome, received_at,"
                " acknowledged_at) VALUES (?, ?, ?, ?, ?, ?)",
                # Only a new task is announced on the ticket.
                (*key, status_change_id, str(outcome), now, None if outcome is Handoff.CREATED else now),
            )
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        return outcome

    def is_acknowledged(self, source: str, ticket_key: str, status_change_id: str) -> bool:
        row = self._db.execute(
            "SELECT acknowledged_at FROM handoffs WHERE source = ? AND ticket_key = ? AND status_change_id = ?",
            (source, ticket_key, status_change_id),
        ).fetchone()
        return row is not None and row["acknowledged_at"] is not None

    def mark_acknowledged(self, source: str, ticket_key: str, status_change_id: str) -> None:
        self._db.execute(
            "UPDATE handoffs SET acknowledged_at = ? WHERE source = ? AND ticket_key = ?"
            " AND status_change_id = ? AND acknowledged_at IS NULL",
            (_now(), source, ticket_key, status_change_id),
        )

    def get_task(self, source: str, ticket_key: str) -> StoredTask | None:
        row = self._db.execute(
            "SELECT * FROM tasks WHERE source = ? AND ticket_key = ?", (source, ticket_key)
        ).fetchone()
        return _stored(row) if row else None

    def list_tasks(self) -> list[StoredTask]:
        return [_stored(row) for row in self._db.execute("SELECT * FROM tasks ORDER BY id")]

    def set_status(self, source: str, ticket_key: str, status: TaskStatus) -> None:
        changed = self._db.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE source = ? AND ticket_key = ?",
            (str(status), _now(), source, ticket_key),
        ).rowcount
        if not changed:
            raise KeyError(f"no task for {source} {ticket_key}")


def _stored(row: sqlite3.Row) -> StoredTask:
    return StoredTask(
        task=Task(
            source=row["source"],
            ticket_key=row["ticket_key"],
            type=TaskType(row["type"]),
            story=row["story"],
            repo=row["repo"],
            acceptance_criteria=json.loads(row["acceptance_criteria"]),
            out_of_scope=json.loads(row["out_of_scope"]),
            pointers=json.loads(row["pointers"]),
        ),
        status=TaskStatus(row["status"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
