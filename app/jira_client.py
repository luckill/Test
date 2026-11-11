"""Jira Cloud client for the coding agent.

Finds the tickets waiting in the "Ready for agent" column.

Try it from the command line:
    python -m app.jira_client PROJ
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

import httpx2
from pydantic import BaseModel, ConfigDict

log = logging.getLogger(__name__)

# The old /rest/api/3/search endpoint has been removed. This is its replacement.
SEARCH_PATH = "/rest/api/3/search/jql"
READY_STATUS = "Ready for agent"

# The search returns only issue ids unless the request names the fields it wants.
SEARCH_FIELDS = ["summary", "status", "issuetype", "updated"]

PAGE_SIZE = 50
MAX_PAGES = 20

ENV_NAMES = ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")


class JiraError(Exception):
    """A Jira API call failed."""


class JiraConfigError(JiraError):
    """The Jira connection settings are missing."""


class TicketRef(BaseModel):
    """The few facts needed to decide whether to fetch a ticket in full."""

    model_config = ConfigDict(frozen=True)

    key: str
    summary: str
    status: str
    issue_type: str
    updated: str


class JiraClient:
    def __init__(
        self,
        base_url: str,
        email: str,
        api_token: str,
        *,
        timeout: float = 15.0,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        # `transport` exists so tests can swap in a fake Jira.
        self._http = httpx2.Client(
            base_url=base_url.rstrip("/"),
            auth=httpx2.BasicAuth(email, api_token),
            headers={"Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )

    @classmethod
    def from_env(cls) -> JiraClient:
        missing = [name for name in ENV_NAMES if not os.environ.get(name)]
        if missing:
            raise JiraConfigError("Missing environment variables: " + ", ".join(missing))
        return cls(*(os.environ[name] for name in ENV_NAMES))

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> JiraClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def find_ready_tickets(
        self, project_key: str, status: str = READY_STATUS
    ) -> list[TicketRef]:
        """Return every ticket in `project_key` whose status is `status`.

        Oldest update first, so tickets are picked up in the order they were moved.
        """
        jql = (
            f"project = {_jql_quote(project_key)} "
            f"AND status = {_jql_quote(status)} "
            "ORDER BY updated ASC"
        )
        tickets: dict[str, TicketRef] = {}
        seen_tokens: set[str] = set()
        token: str | None = None

        for _ in range(MAX_PAGES):
            body: dict[str, Any] = {
                "jql": jql,
                "maxResults": PAGE_SIZE,
                "fields": SEARCH_FIELDS,
            }
            if token:
                body["nextPageToken"] = token
            data = self._post(SEARCH_PATH, body)

            for issue in data.get("issues") or []:
                ref = _to_ticket_ref(issue)
                tickets.setdefault(ref.key, ref)

            # Stop on the last page. Also stop if Jira sends no token or repeats
            # one, since this endpoint has been reported to do both.
            token = data.get("nextPageToken")
            if data.get("isLast") or not token or token in seen_tokens:
                break
            seen_tokens.add(token)
        else:
            log.warning("Stopped after %d pages of search results", MAX_PAGES)

        return list(tickets.values())

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._http.post(path, json=body)
        except httpx2.HTTPError as exc:
            raise JiraError(f"Could not reach Jira: {exc}") from exc
        if response.status_code >= 400:
            raise JiraError(_describe_error(response))
        data = response.json()
        if not isinstance(data, dict):
            raise JiraError(f"Unexpected response from {path}: {type(data).__name__}")
        return data


def _jql_quote(value: str) -> str:
    """Wrap a value in double quotes for JQL, escaping what needs it."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _to_ticket_ref(issue: dict[str, Any]) -> TicketRef:
    fields = issue.get("fields") or {}
    return TicketRef(
        key=issue["key"],
        summary=fields.get("summary") or "",
        status=(fields.get("status") or {}).get("name", ""),
        issue_type=(fields.get("issuetype") or {}).get("name", ""),
        updated=fields.get("updated") or "",
    )


def _describe_error(response: httpx2.Response) -> str:
    """Turn a failed response into a message, without echoing credentials."""
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    messages: list[str] = []
    if isinstance(payload, dict):
        messages += [str(m) for m in payload.get("errorMessages") or []]
        messages += [f"{k}: {v}" for k, v in (payload.get("errors") or {}).items()]
    detail = "; ".join(messages) or response.text[:200]
    hint = ""
    if response.status_code in (401, 403):
        hint = " Check JIRA_EMAIL and JIRA_API_TOKEN, and that the bot can see the project."
    return f"Jira returned {response.status_code}: {detail}{hint}"


if __name__ == "__main__":
    project = sys.argv[1] if len(sys.argv) > 1 else ""
    if not project:
        sys.exit("Usage: python -m app.jira_client PROJECT_KEY")
    try:
        with JiraClient.from_env() as jira:
            found = jira.find_ready_tickets(project)
    except JiraError as exc:
        sys.exit(str(exc))
    for ticket in found:
        print(f"{ticket.key}  [{ticket.issue_type}]  {ticket.summary}")
    print(f"{len(found)} ticket(s) in '{READY_STATUS}'")
