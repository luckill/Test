"""Jira intake: a ticket in "Ready for agent" becomes a task record."""

from __future__ import annotations

import logging
from enum import StrEnum

from .config import Column, Config
from .jira import JiraClient, Ticket
from .store import Handoff, Store
from .story import parse_story
from .task import Task, TaskType

log = logging.getLogger(__name__)

SOURCE = "jira"
RECEIVED_COMMENT = "Task received"
# Used for a Jira work type the config does not map.
FALLBACK_TYPE = TaskType.CHORE


class Outcome(StrEnum):
    CREATED = "created"
    REENTERED = "reentered"
    DUPLICATE = "duplicate"
    SKIPPED = "skipped"  # not in "Ready for agent", or its project is not configured


def task_from_ticket(ticket: Ticket, config: Config) -> Task:
    """Build a task from a ticket whose project is in the config."""
    task_type = config.task_type(ticket.issue_type)
    if task_type is None:
        log.warning(
            "%s: work type %r is not mapped under [jira.issue_types]; treating it as a %s",
            ticket.key, ticket.issue_type, FALLBACK_TYPE,
        )
        task_type = FALLBACK_TYPE
    sections = parse_story(ticket.description)
    # The task format has no title, so the summary leads the story.
    story = "\n\n".join(filter(None, [ticket.summary.strip(), sections.story]))
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


def handle_ticket(key: str, jira: JiraClient, store: Store, config: Config) -> Outcome:
    """Process one trigger for a ticket.

    The trigger only names the ticket. Everything else is read from Jira
    here, so a stale or repeated trigger is harmless.
    """
    ticket = jira.get_ticket(key)
    ready = config.columns[Column.READY_FOR_AGENT]
    if ticket.status.casefold() != ready.casefold():
        return Outcome.SKIPPED
    if ticket.project_key not in config.projects:
        log.warning("%s: project %s has no repo under [jira.projects]; ignored", ticket.key, ticket.project_key)
        return Outcome.SKIPPED

    task = task_from_ticket(ticket, config)
    handoff = store.record_handoff(task, ticket.status_change_id)
    # The comment is posted after the record is saved. If posting fails, the
    # next trigger for the same move finds it unacknowledged and tries again.
    if not store.is_acknowledged(SOURCE, ticket.key, ticket.status_change_id):
        jira.post_comment(ticket.key, RECEIVED_COMMENT)
        store.mark_acknowledged(SOURCE, ticket.key, ticket.status_change_id)
    if handoff is not Handoff.DUPLICATE:
        log.info("%s: task %s (%s, %s)", ticket.key, handoff, task.type, task.repo)
    return Outcome(handoff)


def poll(jira: JiraClient, store: Store, config: Config) -> list[tuple[str, Outcome]]:
    """Handle every ticket now in "Ready for agent". One failure does not stop the rest."""
    results = []
    for key in jira.tickets_in_column(Column.READY_FOR_AGENT, config.projects):
        try:
            results.append((key, handle_ticket(key, jira, store, config)))
        except Exception:
            log.exception("%s: intake failed; will retry on the next trigger", key)
    return results
