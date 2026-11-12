"""Triage: decide what to do with a ticket that was handed to the agent.

The readiness check classifies the ticket's story, and the classification
picks the path:

- complete: post the technical plan, move the ticket to "In progress", start.
- partial:  post questions about the gaps only, move the ticket to "Needs
            info". Moving it back to "Ready for agent" is a new hand-off, so
            the check runs again.
- none:     nothing yet; this path is not built.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Callable

import anthropic

from .config import Column
from .jira import JiraClient
from .llm import DEFAULT_MODEL, ModelError
from .plan import write_plan
from .readiness import FieldStatus, Readiness, check_readiness
from .store import Store, StoredTask
from .task import Task, TaskStatus

log = logging.getLogger(__name__)


class Classification(StrEnum):
    COMPLETE = "complete"  # ready to start as written
    PARTIAL = "partial"  # something to work from, with gaps
    NONE = "none"  # no story and no acceptance criteria: nothing to work from


def classify(readiness: Readiness) -> Classification:
    if readiness.ready:
        return Classification.COMPLETE
    nothing = FieldStatus.MISSING
    if readiness.story.status is nothing and readiness.acceptance_criteria.status is nothing:
        return Classification.NONE
    return Classification.PARTIAL


def questions_comment(readiness: Readiness) -> str:
    """The comment for a partial story: one question per gap, and nothing else."""
    sections: list[tuple[str, list[str]]] = []
    if readiness.story.status is not FieldStatus.PRESENT:
        sections.append(("Story", [readiness.story.question]))
    if readiness.acceptance_criteria.status is FieldStatus.MISSING:
        sections.append(("Acceptance criteria", [readiness.acceptance_criteria.question]))
    else:
        asked = [
            f'"{" ".join(check.criterion.split())}": {check.question}'
            for check in readiness.criteria
            if not check.testable
        ]
        if asked:
            sections.append(("Acceptance criteria", asked))
    # The optional fields only raise a question when what they say is unclear.
    for title, check in (("Out of scope", readiness.out_of_scope), ("Pointers", readiness.pointers)):
        if check.status is FieldStatus.UNCLEAR:
            sections.append((title, [check.question]))

    lines = [
        "I can't start on this yet. Please answer the questions below, in a comment or by "
        "editing the description, then move the ticket back to **Ready for agent**."
    ]
    for title, questions in sections:
        lines += ["", f"**{title}**", ""] + [f"- {question}" for question in questions]
    return "\n".join(lines)


def plan_comment(plan: str) -> str:
    return f"**Technical plan**\n\n{plan}\n\nStarting work now."


def failure_comment(error: Exception) -> str:
    return (
        "I could not assess this ticket automatically, so I have not started.\n\n"
        f"Reason: {error}\n\n"
        "Move the ticket back to **Ready for agent** to try again."
    )


def start_work(task: StoredTask) -> None:
    """Begin implementing a task. The implementation step is not built yet."""
    log.info("%s: ready to implement in %s", task.task.ticket_key, task.task.repo)


def triage(
    task: Task,
    status_change_id: str,
    *,
    jira: JiraClient,
    store: Store,
    claude: anthropic.Anthropic,
    model: str = DEFAULT_MODEL,
    start: Callable[[StoredTask], None] = start_work,
) -> Classification | None:
    """Classify a hand-off and carry out its path. Returns None if the model could not assess it.

    The decision is saved before anything is done on the ticket, and each
    step is skipped if it already happened, so a run that fails part-way is
    finished by the next trigger without asking the model again or posting
    a comment twice.
    """
    key = (task.source, task.ticket_key, status_change_id)
    handoff = store.get_handoff(*key)
    if handoff is None:
        raise KeyError(f"no hand-off recorded for {task.ticket_key} ({status_change_id})")

    if handoff.decided:
        classification = Classification(handoff.classification) if handoff.classification else None
        reply = handoff.reply
    else:
        classification, reply, plan = _decide(task, claude, model)
        store.save_decision(*key, classification, reply, plan)
    if handoff.triaged:
        return classification

    kind, column, status = {
        Classification.COMPLETE: ("plan", Column.IN_PROGRESS, TaskStatus.IN_PROGRESS),
        Classification.PARTIAL: ("questions", Column.NEEDS_INFO, TaskStatus.NEEDS_INFO),
        Classification.NONE: (None, None, TaskStatus.RECEIVED),
        None: ("failed", Column.NEEDS_INFO, TaskStatus.NEEDS_INFO),
    }[classification]

    if reply and kind and not store.has_comment(*key, kind):
        store.record_comment(*key, kind, jira.post_comment(task.ticket_key, reply))
    if column:
        jira.move_ticket(task.ticket_key, column)
    if classification is Classification.COMPLETE:
        start(store.get_task(task.source, task.ticket_key))
    store.mark_triaged(*key, status)
    log.info("%s: story is %s", task.ticket_key, classification or "unassessed")
    return classification


def _decide(
    task: Task, claude: anthropic.Anthropic, model: str
) -> tuple[Classification | None, str | None, str | None]:
    """Ask the model and return (classification, comment to post, plan)."""
    try:
        readiness = check_readiness(task, claude, model)
        classification = classify(readiness)
        if classification is Classification.COMPLETE:
            plan = write_plan(task, claude, model)
            return classification, plan_comment(plan), plan
        if classification is Classification.PARTIAL:
            return classification, questions_comment(readiness), None
        return classification, None, None
    except ModelError as error:
        # Retrying would ask the same question again and pay for it again.
        log.warning("%s: could not be assessed: %s", task.ticket_key, error)
        return None, failure_comment(error), None
