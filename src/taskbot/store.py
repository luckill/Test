"""Task records in SQLite.

`tasks` holds one row per ticket. `handoffs` holds one row per move of a
ticket into "Ready for agent", which is what makes a repeat trigger for the
same move a no-op, and records how that hand-off was triaged. `bot_comments`
remembers which comments on a ticket the service posted itself.
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
    plan                TEXT,           -- the technical plan, once one is posted
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
    classification   TEXT,           -- complete | partial | none; NULL until decided
    reply            TEXT,           -- the comment to post in answer, if any
    triaged_at       TEXT,           -- when the answer was carried out
    PRIMARY KEY (source, ticket_key, status_change_id)
);
CREATE TABLE IF NOT EXISTS bot_comments (
    source           TEXT NOT NULL,
    ticket_key       TEXT NOT NULL,
    status_change_id TEXT NOT NULL,
    kind             TEXT NOT NULL,  -- received | questions | plan | failed
    comment_id       TEXT NOT NULL,
    PRIMARY KEY (source, ticket_key, status_change_id, kind)
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
    plan: str | None = None


@dataclass(frozen=True)
class HandoffRecord:
    acknowledged: bool
    classification: str | None
    reply: str | None
    triaged: bool

    @property
    def decided(self) -> bool:
        """Whether triage has already chosen what to do with this hand-off."""
        return self.classification is not None or self.reply is not None


class Store:
    def __init__(self, path: str | Path):
        # Autocommit mode; transactions are opened explicitly below.
        self._db = sqlite3.connect(str(path), isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    # -- hand-offs ---------------------------------------------------------

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

    def get_handoff(self, source: str, ticket_key: str, status_change_id: str) -> HandoffRecord | None:
        row = self._db.execute(
            "SELECT * FROM handoffs WHERE source = ? AND ticket_key = ? AND status_change_id = ?",
            (source, ticket_key, status_change_id),
        ).fetchone()
        if row is None:
            return None
        return HandoffRecord(
            acknowledged=row["acknowledged_at"] is not None,
            classification=row["classification"],
            reply=row["reply"],
            triaged=row["triaged_at"] is not None,
        )

    def is_acknowledged(self, source: str, ticket_key: str, status_change_id: str) -> bool:
        handoff = self.get_handoff(source, ticket_key, status_change_id)
        return handoff is not None and handoff.acknowledged

    def mark_acknowledged(self, source: str, ticket_key: str, status_change_id: str) -> None:
        self._db.execute(
            "UPDATE handoffs SET acknowledged_at = ? WHERE source = ? AND ticket_key = ?"
            " AND status_change_id = ? AND acknowledged_at IS NULL",
            (_now(), source, ticket_key, status_change_id),
        )

    def save_decision(
        self,
        source: str,
        ticket_key: str,
        status_change_id: str,
        classification: str | None,
        reply: str | None,
        plan: str | None = None,
    ) -> None:
        """Remember what triage decided, so a retry acts on the same decision."""
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._db.execute(
                "UPDATE handoffs SET classification = ?, reply = ? WHERE source = ? AND ticket_key = ?"
                " AND status_change_id = ?",
                (classification, reply, source, ticket_key, status_change_id),
            )
            if plan is not None:
                self._db.execute(
                    "UPDATE tasks SET plan = ?, updated_at = ? WHERE source = ? AND ticket_key = ?",
                    (plan, _now(), source, ticket_key),
                )
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def mark_triaged(self, source: str, ticket_key: str, status_change_id: str, status: TaskStatus) -> None:
        """Close the hand-off and set the task's status in one step."""
        now = _now()
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._db.execute(
                "UPDATE handoffs SET triaged_at = ? WHERE source = ? AND ticket_key = ? AND status_change_id = ?",
                (now, source, ticket_key, status_change_id),
            )
            self._db.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE source = ? AND ticket_key = ?",
                (str(status), now, source, ticket_key),
            )
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    # -- comments the service posted ---------------------------------------

    def record_comment(self, source: str, ticket_key: str, status_change_id: str, kind: str, comment_id: str) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO bot_comments (source, ticket_key, status_change_id, kind, comment_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (source, ticket_key, status_change_id, kind, comment_id),
        )

    def has_comment(self, source: str, ticket_key: str, status_change_id: str, kind: str) -> bool:
        return self._db.execute(
            "SELECT 1 FROM bot_comments WHERE source = ? AND ticket_key = ? AND status_change_id = ? AND kind = ?",
            (source, ticket_key, status_change_id, kind),
        ).fetchone() is not None

    def bot_comments(self, source: str, ticket_key: str) -> dict[str, str]:
        """Every comment the service posted on a ticket, as comment id -> kind."""
        rows = self._db.execute(
            "SELECT comment_id, kind FROM bot_comments WHERE source = ? AND ticket_key = ?", (source, ticket_key)
        )
        return {row["comment_id"]: row["kind"] for row in rows}

    # -- tasks -------------------------------------------------------------

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
        plan=row["plan"],
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
