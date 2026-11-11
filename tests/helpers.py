"""Builders for ADF nodes and a fake Jira that speaks the REST API."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

from taskbot.config import DEFAULT_COLUMN_NAMES, DEFAULT_ISSUE_TYPES, Config
from taskbot.jira import JiraClient


def text(value, *marks):
    node = {"type": "text", "text": value}
    if marks:
        node["marks"] = [
            {"type": "link", "attrs": {"href": m[5:]}} if m.startswith("link:") else {"type": m}
            for m in marks
        ]
    return node


def para(*content):
    return {"type": "paragraph", "content": [text(c) if isinstance(c, str) else c for c in content]}


def heading(level, value):
    return {"type": "heading", "attrs": {"level": level}, "content": [text(value)]}


def bullets(*items):
    return {
        "type": "bulletList",
        "content": [{"type": "listItem", "content": [para(i) if isinstance(i, str) else i]} for i in items],
    }


def doc(*content):
    return {"version": 1, "type": "doc", "content": list(content)}


STORY_DOC = doc(
    heading(2, "Story"),
    para("Refunds use the wrong currency."),
    heading(2, "Acceptance criteria"),
    bullets("Refund uses the payment currency.", "A test covers it."),
    heading(2, "Out of scope"),
    bullets("Partial refunds."),
    heading(2, "Pointers"),
    bullets(para(text("src/payments/refund_service.py", "code")), "RefundService.issue"),
)


def make_config(tmp_path, **overrides) -> Config:
    values = dict(
        jira_base_url="https://example.atlassian.net",
        jira_email="bot@example.com",
        jira_api_token="token",
        poll_interval_seconds=180,
        columns=dict(DEFAULT_COLUMN_NAMES),
        projects={"PAY": "acme/payments"},
        issue_types=dict(DEFAULT_ISSUE_TYPES),
        database_path=tmp_path / "tasks.db",
        webhook=None,
    )
    values.update(overrides)
    return Config(**values)


class FakeJira:
    """An in-memory Jira site. Use `.transport` as a JiraClient transport."""

    def __init__(self):
        self.issues = {}
        self.requests = []  # (method, path, query, body)
        self.fail_next = []  # statuses to answer with before behaving
        self._next_history = 100

    def add_issue(self, key, status="Backlog", issue_type="Bug", summary="Wrong refund currency", description=STORY_DOC):
        self.issues[key] = {
            "key": key,
            "summary": summary,
            "description": description,
            "type": issue_type,
            "status": status,
            "histories": [],
            "comments": [],
        }

    def move(self, key, status):
        issue = self.issues[key]
        self._next_history += 1
        issue["histories"].append({
            "id": str(self._next_history),
            "items": [{"field": "status", "to": _status_id(status), "toString": status}],
        })
        issue["status"] = status

    def edit(self, key, **fields):
        """A change that is not a move, such as an edited description."""
        issue = self.issues[key]
        issue.update(fields)
        self._next_history += 1
        issue["histories"].append({"id": str(self._next_history), "items": [{"field": "description"}]})

    def comments(self, key):
        return self.issues[key]["comments"]

    def client(self, config: Config) -> JiraClient:
        return JiraClient(
            config.jira_base_url, config.jira_email, config.jira_api_token, config.columns,
            transport=self.transport, sleep=lambda seconds: None,
        )

    def transport(self, method, url, headers, body):
        parsed = urlparse(url)
        path, query = parsed.path, {k: v[0] for k, v in parse_qs(parsed.query).items()}
        payload = json.loads(body) if body else None
        self.requests.append((method, path, query, payload))
        if self.fail_next:
            return self.fail_next.pop(0), {}, b"simulated failure"
        status, result = self._route(method, path, query, payload)
        return status, {}, json.dumps(result).encode() if result is not None else b""

    def _route(self, method, path, query, payload):
        parts = path.removeprefix("/rest/api/3/").split("/")
        if parts == ["search", "jql"]:
            wanted = payload["jql"].split('status = "')[1].split('"')[0]
            projects = payload["jql"].split("(")[1].split(")")[0].split(", ")
            issues = [
                {"key": key}
                for key, issue in self.issues.items()
                if issue["status"] == wanted and key.split("-")[0] in projects
            ]
            return 200, {"issues": issues, "isLast": True}
        if parts[0] == "issue" and parts[1] in self.issues:
            issue = self.issues[parts[1]]
            if len(parts) == 2:
                return 200, {
                    "key": issue["key"],
                    "fields": {
                        "summary": issue["summary"],
                        "description": issue["description"],
                        "issuetype": {"name": issue["type"]},
                        "project": {"key": issue["key"].split("-")[0]},
                        "status": {"id": _status_id(issue["status"]), "name": issue["status"]},
                    },
                    "changelog": {"total": len(issue["histories"]), "histories": list(reversed(issue["histories"]))},
                }
            if parts[2] == "comment" and method == "POST":
                issue["comments"].append(payload["body"])
                return 201, {"id": str(len(issue["comments"]))}
            if parts[2] == "transitions" and method == "GET":
                names = [name for name in DEFAULT_COLUMN_NAMES.values() if name != issue["status"]]
                return 200, {"transitions": [{"id": _status_id(n), "to": {"name": n}} for n in names]}
            if parts[2] == "transitions" and method == "POST":
                target = next(n for n in DEFAULT_COLUMN_NAMES.values() if _status_id(n) == payload["transition"]["id"])
                self.move(issue["key"], target)
                return 204, None
        return 404, {"errorMessages": ["not found"]}


def _status_id(name):
    return str(10000 + sorted(DEFAULT_COLUMN_NAMES.values()).index(name))
