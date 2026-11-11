"""Tests for the HTTP API. Jira is replaced with a fake, so no network is needed."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.jira_client import ENV_NAMES, JiraError, TicketRef
from app.main import app, get_jira_client

TICKET = TicketRef(
    key="PROJ-7",
    summary="Fix date parsing",
    status="Ready for agent",
    issue_type="Bug",
    updated="2026-11-11T09:00:00.000-0800",
)


class FakeJira:
    def __init__(self, tickets: list[TicketRef] | None = None, error: JiraError | None = None):
        self.tickets = tickets or []
        self.error = error
        self.projects_asked: list[str] = []

    def find_ready_tickets(self, project_key: str) -> list[TicketRef]:
        self.projects_asked.append(project_key)
        if self.error:
            raise self.error
        return self.tickets


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(app)
    app.dependency_overrides.clear()


def use_jira(fake: FakeJira) -> None:
    app.dependency_overrides[get_jira_client] = lambda: fake


def test_health(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_lists_ready_tickets(client: TestClient) -> None:
    fake = FakeJira(tickets=[TICKET])
    use_jira(fake)

    response = client.get("/jira/ready-tickets", params={"project": "PROJ"})

    assert response.status_code == 200
    assert response.json() == [
        {
            "key": "PROJ-7",
            "summary": "Fix date parsing",
            "status": "Ready for agent",
            "issue_type": "Bug",
            "updated": "2026-11-11T09:00:00.000-0800",
        }
    ]
    assert fake.projects_asked == ["PROJ"]


def test_project_is_required(client: TestClient) -> None:
    use_jira(FakeJira())

    assert client.get("/jira/ready-tickets").status_code == 422


def test_jira_failure_becomes_502(client: TestClient) -> None:
    use_jira(FakeJira(error=JiraError("Jira returned 400: no such project")))

    response = client.get("/jira/ready-tickets", params={"project": "NOPE"})

    assert response.status_code == 502
    assert "no such project" in response.json()["detail"]


def test_missing_settings_become_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)

    response = client.get("/jira/ready-tickets", params={"project": "PROJ"})

    assert response.status_code == 503
    assert "JIRA_BASE_URL" in response.json()["detail"]
