"""The readiness check, run through the real SDK with the HTTP layer mocked."""

import json

import anthropic
import httpx2
import pytest
from anthropic import DefaultHttpxClient

from taskbot.readiness import FieldStatus, ReadinessError, SYSTEM_PROMPT, check_readiness
from taskbot.task import Task, TaskType

OK = {"reason": "Clear as written.", "status": "present"}


def make_task(**changes):
    values = dict(
        source="jira", ticket_key="PAY-1", type=TaskType.BUG, repo="acme/api",
        story="Lookups for unknown IDs return 500.\n\nThey should return 404.",
        acceptance_criteria=["Returns 404 for unknown IDs", "Handles errors well"],
        out_of_scope=["Changing the ID format"],
        pointers=["api/lookup.py"],
    )
    values.update(changes)
    return Task(**values)


def answer(criteria, story=OK, out_of_scope=OK, pointers=OK):
    return {
        "story": story, "out_of_scope": out_of_scope, "pointers": pointers,
        "criteria": [{"number": n, "reason": reason, "testable": testable} for n, testable, reason in criteria],
    }


def make_client(verdict=None, status=200, **message):
    """A real client whose requests are answered locally. Returns it with the requests seen."""
    seen = []
    body = {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": [{"type": "thinking", "thinking": "", "signature": "sig"}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 20},
        **message,
    }
    if verdict is not None:
        body["content"] = body["content"] + [{"type": "text", "text": json.dumps(verdict)}]

    def handler(request):
        seen.append(request)
        return httpx2.Response(status, json=body)

    transport = httpx2.MockTransport(handler)
    return anthropic.Anthropic(api_key="test", http_client=DefaultHttpxClient(transport=transport), max_retries=0), seen


BOTH = [(1, True, "The input and the expected result are stated."), (2, False, "Say which errors, and what should happen for each.")]


def test_each_criterion_is_judged_and_one_untestable_criterion_makes_the_field_unclear():
    client, _ = make_client(answer(BOTH))
    result = check_readiness(make_task(), client=client)

    assert [(c.criterion, c.testable) for c in result.criteria] == [
        ("Returns 404 for unknown IDs", True),
        ("Handles errors well", False),
    ]
    assert result.criteria[1].reason == "Say which errors, and what should happen for each."
    assert result.acceptance_criteria.status is FieldStatus.UNCLEAR
    assert "criterion 2" in result.acceptance_criteria.reason
    assert result.story.status is FieldStatus.PRESENT
    assert not result.ready


def test_task_with_testable_criteria_is_ready_even_without_the_optional_fields():
    client, _ = make_client(answer([(1, True, "Stated.")]))
    task = make_task(acceptance_criteria=["Returns 404 for unknown IDs"], out_of_scope=[], pointers=[])
    result = check_readiness(task, client=client)

    assert result.acceptance_criteria.status is FieldStatus.PRESENT
    assert result.out_of_scope.status is result.pointers.status is FieldStatus.MISSING
    assert result.ready
    assert result.to_dict()["ready"] is True


@pytest.mark.parametrize("verdict_changes, task_changes", [
    ({"story": {"reason": "It does not say what should happen.", "status": "unclear"}}, {}),
    ({"story": {"reason": "Only a title.", "status": "missing"}}, {}),
    ({"pointers": {"reason": "Which file is 'the usual one'?", "status": "unclear"}}, {}),
    ({"out_of_scope": {"reason": "'Other stuff' could mean anything.", "status": "unclear"}}, {}),
    ({}, {"acceptance_criteria": []}),
])
def test_not_ready(verdict_changes, task_changes):
    task = make_task(**{"acceptance_criteria": ["Returns 404 for unknown IDs"], **task_changes})
    criteria = [(n, True, "Stated.") for n in range(1, len(task.acceptance_criteria) + 1)]
    client, _ = make_client(answer(criteria, **verdict_changes))
    assert not check_readiness(task, client=client).ready


def test_an_empty_field_is_missing_whatever_the_model_says():
    client, _ = make_client(answer([]))  # the model calls every field present
    result = check_readiness(make_task(story="  ", acceptance_criteria=[], out_of_scope=[], pointers=[]), client=client)
    assert {result.story.status, result.acceptance_criteria.status, result.out_of_scope.status, result.pointers.status} == {
        FieldStatus.MISSING
    }


def test_request_asks_for_a_structured_verdict_with_the_two_examples():
    client, seen = make_client(answer(BOTH))
    check_readiness(make_task(), client=client)
    [request] = seen
    body = json.loads(request.content)

    assert body["model"] == "claude-opus-5-5"
    assert request.headers["anthropic-beta"] == "server-side-fallback-2026-07-01"
    assert body["fallbacks"] == "default"
    assert "thinking" not in body and "tool_choice" not in body
    assert body["output_config"]["effort"] == "low"
    schema = body["output_config"]["format"]["schema"]
    assert set(schema["properties"]) == {"story", "out_of_scope", "pointers", "criteria"}
    assert schema["$defs"]["_FieldVerdict"]["properties"]["status"]["enum"] == ["present", "missing", "unclear"]

    assert body["system"] == SYSTEM_PROMPT
    assert '"Returns 404 for unknown IDs" is testable' in SYSTEM_PROMPT
    assert '"Handles errors well" is not testable' in SYSTEM_PROMPT
    [message] = body["messages"]
    assert "<story>\nLookups for unknown IDs return 500.\n\nThey should return 404.\n</story>" in message["content"]
    assert "<acceptance_criteria>\n1. Returns 404 for unknown IDs\n2. Handles errors well\n</acceptance_criteria>" in message["content"]
    assert "<out_of_scope>\n- Changing the ID format\n</out_of_scope>" in message["content"]
    assert "<pointers>\n- api/lookup.py\n</pointers>" in message["content"]


def test_model_can_be_chosen():
    client, seen = make_client(answer(BOTH))
    check_readiness(make_task(), client=client, model="claude-haiku-5-5")
    assert json.loads(seen[0].content)["model"] == "claude-haiku-5-5"


@pytest.mark.parametrize("criteria", [
    [(1, True, "ok")],                                   # one verdict short
    [(1, True, "ok"), (1, False, "again")],              # the same criterion twice
    [(1, True, "ok"), (2, True, "ok"), (3, True, "?")],  # one too many
])
def test_verdicts_for_the_wrong_criteria_are_rejected(criteria):
    client, _ = make_client(answer(criteria))
    with pytest.raises(ReadinessError, match=r"expected verdicts for criteria \[1, 2\]"):
        check_readiness(make_task(), client=client)


def test_refusal_and_cut_off_answers_are_errors_not_verdicts():
    refused, _ = make_client(content=[], stop_reason="refusal", stop_details={"type": "refusal", "category": "cyber", "explanation": "x"})
    with pytest.raises(ReadinessError, match=r"declined to assess the task \(cyber\)"):
        check_readiness(make_task(), client=refused)

    thinking_only, _ = make_client(stop_reason="max_tokens")
    with pytest.raises(ReadinessError, match="cut off"):
        check_readiness(make_task(), client=thinking_only)

    half_json, _ = make_client(content=[{"type": "text", "text": '{"story": {"reason": "ok", "sta'}], stop_reason="max_tokens")
    with pytest.raises(ReadinessError, match="cut off"):
        check_readiness(make_task(), client=half_json)


def test_api_errors_are_raised_as_they_are():
    client, _ = make_client(status=401, type="error", error={"type": "authentication_error", "message": "invalid x-api-key"})
    with pytest.raises(anthropic.AuthenticationError):
        check_readiness(make_task(), client=client)
