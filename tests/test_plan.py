import json

import pytest

from helpers import FakeClaude
from taskbot.llm import ModelError
from taskbot.plan import SYSTEM_PROMPT, write_plan
from taskbot.task import Task, TaskType

TASK = Task(
    source="jira", ticket_key="PAY-1", type=TaskType.BUG, repo="acme/api",
    story="Lookups for unknown IDs return 500.", acceptance_criteria=["Returns 404 for unknown IDs"],
    out_of_scope=["Changing the ID format"], pointers=["api/lookup.py"],
)


def test_plan_is_requested_for_the_task_and_returned_as_written():
    claude = FakeClaude(plan="\n**Approach**\n\nReturn 404.\n")
    assert write_plan(TASK, claude.client, model="claude-sonnet-5-5") == "**Approach**\n\nReturn 404."

    [(kind, sent)] = claude.requests
    assert kind == "plan"
    assert sent == (
        "Repository: acme/api\nTask type: bug\n\n"
        "<story>\nLookups for unknown IDs return 500.\n</story>\n\n"
        "<acceptance_criteria>\n1. Returns 404 for unknown IDs\n</acceptance_criteria>\n\n"
        "<out_of_scope>\n- Changing the ID format\n</out_of_scope>\n\n"
        "<pointers>\n- api/lookup.py\n</pointers>"
    )
    [request] = claude.http
    body = json.loads(request.content)
    assert body["model"] == "claude-sonnet-5-5"
    assert body["system"] == SYSTEM_PROMPT
    assert body["output_config"] == {"effort": "medium"}
    assert body["fallbacks"] == "default"
    assert request.headers["anthropic-beta"] == "server-side-fallback-2026-07-01"
    assert "thinking" not in body


def test_no_plan_is_an_error():
    with pytest.raises(ModelError, match="empty plan"):
        write_plan(TASK, FakeClaude(plan="  ").client)
    refusing = FakeClaude()
    refusing.refuse = True
    with pytest.raises(ModelError, match=r"declined the request \(cyber\)"):
        write_plan(TASK, refusing.client)
