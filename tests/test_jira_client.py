"""Tests for jira_client. They run against a fake Jira, so no network is needed."""

from __future__ import annotations

import base64
import json
from typing import Any, Callable

import httpx2
import pytest

from app.jira_client import SEARCH_PATH, JiraClient, JiraConfigError, JiraError, TicketRef


def issue(key: str, summary: str = "Fix date parsing", type_name: str = "Bug") -> dict[str, Any]:
    return {
        "id": key.split("-")[1],
        "key": key,
        "fields": {
            "summary": summary,
            "status": {"name": "Ready for agent"},
            "issuetype": {"name": type_name},
            "updated": "2026-11-11T09:00:00.000-0800",
        },
    }


def make_client(handler: Callable[[httpx2.Request], httpx2.Response]) -> JiraClient:
    return JiraClient(
        "https://example.atlassian.net/",
        "bot@example.com",
        "secret-token",
        transport=httpx2.MockTransport(handler),
    )


def test_sends_the_expected_request() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"issues": [], "isLast": True})

    make_client(handler).find_ready_tickets("PROJ")

    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == SEARCH_PATH
    body = json.loads(request.content)
    assert body["jql"] == 'project = "PROJ" AND status = "Ready for agent" ORDER BY updated ASC'
    assert body["fields"] == ["summary", "status", "issuetype", "updated"]
    assert "nextPageToken" not in body
    expected = base64.b64encode(b"bot@example.com:secret-token").decode()
    assert request.headers["Authorization"] == f"Basic {expected}"


def test_parses_tickets() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"issues": [issue("PROJ-7")], "isLast": True})

    tickets = make_client(handler).find_ready_tickets("PROJ")

    assert tickets == [
        TicketRef(
            key="PROJ-7",
            summary="Fix date parsing",
            status="Ready for agent",
            issue_type="Bug",
            updated="2026-11-11T09:00:00.000-0800",
        )
    ]


def test_follows_pages_until_the_last_one() -> None:
    tokens_sent: list[str | None] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        token = json.loads(request.content).get("nextPageToken")
        tokens_sent.append(token)
        if token is None:
            return httpx2.Response(
                200, json={"issues": [issue("PROJ-1")], "nextPageToken": "page2", "isLast": False}
            )
        return httpx2.Response(200, json={"issues": [issue("PROJ-2")], "isLast": True})

    tickets = make_client(handler).find_ready_tickets("PROJ")

    assert [t.key for t in tickets] == ["PROJ-1", "PROJ-2"]
    assert tokens_sent == [None, "page2"]


def test_stops_when_jira_repeats_a_page_token() -> None:
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(
            200, json={"issues": [issue("PROJ-1")], "nextPageToken": "same", "isLast": False}
        )

    tickets = make_client(handler).find_ready_tickets("PROJ")

    assert [t.key for t in tickets] == ["PROJ-1"]
    assert calls == 2


def test_stops_when_the_response_has_no_paging_fields() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"issues": [issue("PROJ-1")]})

    assert len(make_client(handler).find_ready_tickets("PROJ")) == 1


def test_escapes_quotes_in_the_status_name() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        return httpx2.Response(200, json={"issues": [], "isLast": True})

    make_client(handler).find_ready_tickets("PROJ", status='Ready "now"')

    assert 'status = "Ready \\"now\\""' in bodies[0]["jql"]


def test_reports_jira_error_messages() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            400,
            json={"errorMessages": ["The value 'NOPE' does not exist for the field 'project'."]},
        )

    with pytest.raises(JiraError, match="400.*does not exist"):
        make_client(handler).find_ready_tickets("NOPE")


def test_bad_credentials_get_a_hint_without_leaking_the_token() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, text="Unauthorized")

    with pytest.raises(JiraError) as excinfo:
        make_client(handler).find_ready_tickets("PROJ")

    message = str(excinfo.value)
    assert "JIRA_API_TOKEN" in message
    assert "secret-token" not in message


def test_network_failures_become_jira_errors() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route to host")

    with pytest.raises(JiraError, match="Could not reach Jira"):
        make_client(handler).find_ready_tickets("PROJ")


def test_from_env_names_the_missing_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIRA_BASE_URL", "https://example.atlassian.net")
    monkeypatch.delenv("JIRA_EMAIL", raising=False)
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)

    with pytest.raises(JiraConfigError, match="JIRA_EMAIL, JIRA_API_TOKEN"):
        JiraClient.from_env()
