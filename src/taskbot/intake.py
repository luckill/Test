"""Jira intake: a ticket in "Ready for agent" becomes a task record."""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Callable, Mapping, Sequence

from .config import Column, Config
from .jira import Comment, JiraClient, Ticket
from .store import Handoff, Store
from .story import parse_story
from .task import Task, TaskType

log = logging.getLogger(__name__)

SOURCE = "jira"
RECEIVED_COMMENT = "Task received"
# Used for a Jira work type the config does not map.
FALLBACK_TYPE = TaskType.CHORE

# What happens to a task after it is recorded: called with the task and the
# id of the hand-off. taskbot.triage provides the real one.
Triage = Callable[[Task, str], object]


class Outcome(StrEnum):
    CREATED = "created"
    REENTERED = "reentered"
    DUPLICATE = "duplicate"
    SKIPPED = "skipped"  # not in "Ready for agent", or its project is not configured


def task_from_ticket(ticket: Ticket, config: Config, clarifications: str = "") -> Task:
    """Build a task from a ticket whose project is in the config."""
    task_type = config.task_type(ticket.issue_type)
    if task_type is None:
        log.warning(
            "%s: work type %r is not mapped under [jira.issue_types]; treating it as a %s",
            ticket.key, ticket.issue_type, FALLBACK_TYPE,
        )
        task_type = FALLBACK_TYPE
    sections = parse_story(ticket.description)
    # The task format has no title, so the summary leads the story. Answers
    # given in the comments have no field of their own either, so they close it.
    story = "\n\n".join(filter(None, [ticket.summary.strip(), sections.story, clarifications]))
    return Task(
        source=SOURCE,
        ticket_key=ticket.key,
        type=task_type,
        story=story,
        repo=config.projects[ticket.project_key],
        acceptance_criteria=sections.acceptance_criteria,
        out_of_scope=sections.out_of_scope,
        pointers=sections.pointers,
    )


def clarifications(comments: Sequence[Comment], bot_comments: Mapping[str, str]) -> str:
    """The agent's questions and the replies to them, as Markdown for the story.

    The thread starts at the first comment in which the agent asked
    questions. The agent's other comments are left out. Returns "" when
    nobody has replied.
    """
    parts: list[str] = []
    asked = replied = False
    for comment in comments:
        kind = bot_comments.get(comment.id)
        if kind == "questions":
            asked = True
            parts.append(f"**The agent asked:**\n\n{comment.body}")
        elif asked and kind is None and comment.body.strip():
            replied = True
            parts.append(f"**{comment.author} replied:**\n\n{comment.body}")
    if not replied:
        return ""
    return "## Clarifications from the ticket's comments\n\n" + "\n\n".join(parts)


def handle_ticket(
    key: str, jira: JiraClient, store: Store, config: Config, triage: Triage | None = None
) -> Outcome:
    """Process one trigger for a ticket.

    The trigger only names the ticket. Everything else is read from Jira
    here, so a stale or repeated trigger is harmless. With `triage`, a
    recorded task is passed on to it once per hand-off.
    """
    ticket = jira.get_ticket(key)
    ready = config.columns[Column.READY_FOR_AGENT]
    if ticket.status.casefold() != ready.casefold():
        return Outcome.SKIPPED
    if ticket.project_key not in config.projects:
        log.warning("%s: project %s has no repo under [jira.projects]; ignored", ticket.key, ticket.project_key)
        return Outcome.SKIPPED

    handoff_key = (SOURCE, ticket.key, ticket.status_change_id)
    if store.get_handoff(*handoff_key) is None:
        answers = ""
        bot_comments = store.bot_comments(SOURCE, ticket.key)
        if "questions" in bot_comments.values():
            # The agent asked something earlier, so the replies are part of the task.
            answers = clarifications(jira.get_comments(ticket.key), bot_comments)
        task = task_from_ticket(ticket, config, answers)
        handoff = store.record_handoff(task, ticket.status_change_id)
        log.info("%s: task %s (%s, %s)", ticket.key, handoff, task.type, task.repo)
    else:
        handoff = Handoff.DUPLICATE
        task = store.get_task(SOURCE, ticket.key).task

    # The comment is posted after the record is saved. If posting fails, the
    # next trigger for the same move finds it unacknowledged and tries again.
    if not store.is_acknowledged(*handoff_key):
        store.record_comment(*handoff_key, "received", jira.post_comment(ticket.key, RECEIVED_COMMENT))
        store.mark_acknowledged(*handoff_key)
    # Triage follows the same rule: unfinished work is picked up again.
    if triage and not store.get_handoff(*handoff_key).triaged:
        triage(task, ticket.status_change_id)
    return Outcome(handoff)


def poll(
    jira: JiraClient, store: Store, config: Config, triage: Triage | None = None
) -> list[tuple[str, Outcome]]:
    """Handle every ticket now in "Ready for agent". One failure does not stop the rest."""
    results = []
    for key in jira.tickets_in_column(Column.READY_FOR_AGENT, config.projects):
        try:
            results.append((key, handle_ticket(key, jira, store, config, triage)))
        except Exception:
            log.exception("%s: failed; will retry on the next trigger", key)
    return results
