"""A small client for the Jira Cloud REST API, version 3."""

from __future__ import annotations

import base64
import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import quote, urlencode

from .adf import adf_to_markdown, markdown_to_adf
from .config import Column

log = logging.getLogger(__name__)

# (method, url, headers, body) -> (status, response headers, response body)
Transport = Callable[[str, str, Mapping[str, str], bytes | None], tuple[int, Mapping[str, str], bytes]]

_RETRY_STATUSES = {429, 502, 503, 504}
_MAX_RETRIES = 3


class JiraError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Ticket:
    key: str
    project_key: str
    issue_type: str
    summary: str
    description: str  # Markdown
    status: str  # the status name, which is the board column
    # Identifies the move into the current status. A ticket that leaves a
    # column and comes back gets a new one.
    status_change_id: str


class JiraClient:
    def __init__(
        self,
        base_url: str,
        email: str,
        api_token: str,
        columns: Mapping[Column, str],
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._base_url = base_url.rstrip("/")
        token = base64.b64encode(f"{email}:{api_token}".encode()).decode()
        self._headers = {
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        self._columns = columns
        self._transport = transport or _urllib_transport
        self._sleep = sleep

    # -- reading -----------------------------------------------------------

    def tickets_in_column(self, column: Column, project_keys: Iterable[str]) -> list[str]:
        """Keys of the tickets in a column, across the given projects."""
        status = self._columns[column].replace("\\", "\\\\").replace('"', '\\"')
        jql = f'project in ({", ".join(sorted(project_keys))}) AND status = "{status}" ORDER BY updated ASC'
        keys: list[str] = []
        body: dict[str, Any] = {"jql": jql, "fields": ["status"], "maxResults": 100}
        while True:
            page = self._request("POST", "/rest/api/3/search/jql", body=body)
            issues = page.get("issues") or []
            keys += [issue["key"] for issue in issues]
            token = page.get("nextPageToken")
            if not token or not issues or page.get("isLast"):
                return keys
            body["nextPageToken"] = token

    def get_ticket(self, key: str) -> Ticket:
        """Fetch a ticket in full, with its description converted to Markdown."""
        issue = self._request(
            "GET",
            f"/rest/api/3/issue/{quote(key, safe='')}",
            params={"fields": "summary,description,issuetype,project,status", "expand": "changelog"},
        )
        fields = issue["fields"]
        status = fields["status"]
        return Ticket(
            key=issue["key"],
            project_key=fields["project"]["key"],
            issue_type=fields["issuetype"]["name"],
            summary=fields.get("summary") or "",
            description=adf_to_markdown(fields.get("description")),
            status=status["name"],
            status_change_id=self._status_change_id(issue["key"], status, issue.get("changelog")),
        )

    def _status_change_id(self, key: str, status: Mapping, changelog: Mapping | None) -> str:
        histories = (changelog or {}).get("histories") or []
        found = _latest_move_into(status, histories)
        complete = changelog is not None and len(histories) >= int(changelog.get("total", 0))
        if found is None and not complete:
            # The embedded changelog is capped, so read the whole one.
            histories, start = [], 0
            while True:
                page = self._request(
                    "GET",
                    f"/rest/api/3/issue/{quote(key, safe='')}/changelog",
                    params={"startAt": start, "maxResults": 100},
                )
                values = page.get("values") or []
                histories += values
                start += len(values)
                if not values or page.get("isLast") or start >= int(page.get("total", start)):
                    break
            found = _latest_move_into(status, histories)
        # A ticket created straight into the status has no move to point at.
        return found or "created"

    # -- writing -----------------------------------------------------------

    def post_comment(self, key: str, markdown: str) -> None:
        """Post a comment on a ticket. The text is Markdown."""
        self._request(
            "POST",
            f"/rest/api/3/issue/{quote(key, safe='')}/comment",
            body={"body": markdown_to_adf(markdown)},
        )

    def move_ticket(self, key: str, column: Column) -> None:
        """Move a ticket to another column. Does nothing if it is already there."""
        target = self._columns[column]
        path = f"/rest/api/3/issue/{quote(key, safe='')}"
        transitions = self._request("GET", f"{path}/transitions").get("transitions") or []
        for transition in transitions:
            if transition["to"]["name"].casefold() == target.casefold():
                self._request("POST", f"{path}/transitions", body={"transition": {"id": transition["id"]}})
                return
        current = self._request("GET", path, params={"fields": "status"})["fields"]["status"]["name"]
        if current.casefold() == target.casefold():
            return
        reachable = ", ".join(sorted(t["to"]["name"] for t in transitions)) or "none"
        raise JiraError(
            f"{key}: cannot move from {current!r} to {target!r}. "
            f"The workflow allows: {reachable}"
        )

    # -- setup check -------------------------------------------------------

    def myself(self) -> str:
        """The display name of the account the client signs in as."""
        return self._request("GET", "/rest/api/3/myself").get("displayName", "")

    def project_statuses(self, project_key: str) -> set[str]:
        """Every status name the project's work types can be in."""
        types = self._request("GET", f"/rest/api/3/project/{quote(project_key, safe='')}/statuses")
        return {status["name"] for work_type in types for status in work_type.get("statuses") or []}

    # -- transport ---------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        body: Any = None,
    ) -> Any:
        url = self._base_url + path + (f"?{urlencode(params)}" if params else "")
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(_MAX_RETRIES + 1):
            status, headers, payload = self._transport(method, url, self._headers, data)
            if status in _RETRY_STATUSES and attempt < _MAX_RETRIES:
                delay = _retry_after(headers) or 2.0 ** attempt
                log.warning("Jira returned %s for %s %s; retrying in %.0fs", status, method, path, delay)
                self._sleep(delay)
                continue
            if status >= 400:
                detail = payload.decode("utf-8", "replace")[:500]
                raise JiraError(f"{method} {path} failed with {status}: {detail}", status)
            return json.loads(payload) if payload.strip() else {}
        raise AssertionError("unreachable")


def _latest_move_into(status: Mapping, histories: Iterable[Mapping]) -> str | None:
    """The id of the newest changelog entry that moved the ticket into `status`."""
    moves = [
        str(history["id"])
        for history in histories
        for item in history.get("items") or []
        if item.get("field") == "status"
        and (
            str(item.get("to")) == str(status.get("id"))
            or str(item.get("toString", "")).casefold() == str(status.get("name", "")).casefold()
        )
    ]
    # Changelog ids grow over time; do not rely on the order of the response.
    return max(moves, key=lambda move: (len(move), move), default=None)


def _retry_after(headers: Mapping[str, str]) -> float | None:
    for name, value in headers.items():
        if name.lower() == "retry-after":
            try:
                return min(max(float(value), 0.0), 60.0)
            except ValueError:
                return None
    return None


def _urllib_transport(
    method: str, url: str, headers: Mapping[str, str], body: bytes | None
) -> tuple[int, Mapping[str, str], bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()
    except (urllib.error.URLError, TimeoutError) as error:
        raise JiraError(f"{method} {url} failed: {error}") from error
