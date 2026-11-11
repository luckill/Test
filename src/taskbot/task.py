"""The internal task format. Its JSON form is described by schemas/task.schema.json."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum


class TaskType(StrEnum):
    BUG = "bug"
    CHORE = "chore"
    FEATURE = "feature"


class TaskStatus(StrEnum):
    """Where a task is in the pipeline. Intake only ever sets RECEIVED."""

    RECEIVED = "received"
    NEEDS_INFO = "needs_info"
    PLAN_REVIEW = "plan_review"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    DONE = "done"


@dataclass
class Task:
    source: str
    ticket_key: str
    type: TaskType
    story: str
    repo: str
    acceptance_criteria: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)
    pointers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {**asdict(self), "type": str(self.type)}
