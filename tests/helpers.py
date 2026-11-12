"""Builders for ADF nodes and a fake Jira that speaks the REST API."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import anthropic
import httpx2
from anthropic import DefaultHttpxClient

from taskbot.adf import adf_to_markdown, markdown_to_adf
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
        self._next_comment = 5000

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
        """The body of every comment on a ticket, as Jira stores it."""
        return [comment["body"] for comment in self.issues[key]["comments"]]

    def comment_texts(self, key):
        """The same comments as Markdown."""
        return [adf_to_markdown(body) for body in self.comments(key)]

    def add_comment(self, key, markdown, author="Dana"):
        """A comment written by a person."""
        self._add_comment(key, markdown_to_adf(markdown), author)

    def _add_comment(self, key, body, author):
        self._next_comment += 1
        comment = {"id": str(self._next_comment), "author": {"displayName": author}, "body": body}
        self.issues[key]["comments"].append(comment)
        return comment

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
                return 201, {"id": self._add_comment(issue["key"], payload["body"], "Task Bot")["id"]}
            if parts[2] == "comment" and method == "GET":
                start, size = int(query.get("startAt", 0)), int(query.get("maxResults", 50))
                page = issue["comments"][start:start + size]
                return 200, {"comments": page, "startAt": start, "total": len(issue["comments"])}
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


# -- a stand-in for the Claude API ------------------------------------------

def field(status="present", question=""):
    return {"reason": "Because.", "status": status, "question": question}


def verdict(criteria=(), story=None, out_of_scope=None, pointers=None):
    """A readiness answer. `criteria` has one entry per criterion: True when a
    test could be written from it, otherwise the question to ask."""
    return {
        "story": story or field(),
        "out_of_scope": out_of_scope or field(),
        "pointers": pointers or field(),
        "criteria": [
            {"number": n, "reason": "Because.", "testable": c is True, "question": "" if c is True else c}
            for n, c in enumerate(criteria, 1)
        ],
    }


class FakeClaude:
    """A real Anthropic client whose requests are answered locally.

    `verdict` answers readiness checks and `plan` answers plan requests.
    Statuses in `fail_next` are returned first, as API errors. `requests`
    lists what was asked: ("readiness" | "plan", the text sent as the task).
    """

    def __init__(self, verdict=None, plan="**Approach**\n\nChange the lookup."):
        self.verdict = verdict
        self.plan = plan
        self.fail_next = []
        self.refuse = False
        self.requests = []
        self.http = []  # the raw requests, for checking headers and bodies
        transport = httpx2.MockTransport(self._handle)
        self.client = anthropic.Anthropic(
            api_key="test", http_client=DefaultHttpxClient(transport=transport), max_retries=0
        )

    @property
    def kinds(self):
        return [kind for kind, _ in self.requests]

    def _handle(self, request):
        self.http.append(request)
        body = json.loads(request.content)
        kind = "readiness" if "format" in body.get("output_config", {}) else "plan"
        self.requests.append((kind, body["messages"][0]["content"]))
        if self.fail_next:
            error = {"type": "error", "error": {"type": "api_error", "message": "simulated failure"}}
            return httpx2.Response(self.fail_next.pop(0), json=error)
        message = {
            "id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }
        if self.refuse:
            details = {"type": "refusal", "category": "cyber", "explanation": "declined"}
            return httpx2.Response(200, json={**message, "content": [], "stop_reason": "refusal", "stop_details": details})
        text = json.dumps(self.verdict) if kind == "readiness" else self.plan
        return httpx2.Response(200, json={**message, "content": [{"type": "text", "text": text}]})
