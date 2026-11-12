import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from helpers import FakeJira, make_config, text
from taskbot.config import Column
from taskbot.jira import JiraClient, JiraError


@pytest.fixture
def site():
    site = FakeJira()
    site.add_issue("PAY-1")
    return site


@pytest.fixture
def jira(site, tmp_path):
    return site.client(make_config(tmp_path))


def test_search_sends_bounded_jql_for_the_column(site, jira):
    site.add_issue("PAY-2")
    site.add_issue("OPS-1")
    for key in ("PAY-2", "OPS-1"):
        site.move(key, "Ready for agent")
    assert jira.tickets_in_column(Column.READY_FOR_AGENT, ["PAY", "WEB"]) == ["PAY-2"]
    method, path, _, body = site.requests[-1]
    assert (method, path) == ("POST", "/rest/api/3/search/jql")
    assert body["jql"] == 'project in (PAY, WEB) AND status = "Ready for agent" ORDER BY updated ASC'


def test_search_follows_page_tokens(tmp_path):
    pages = [
        {"issues": [{"key": "PAY-1"}], "nextPageToken": "t1"},
        {"issues": [{"key": "PAY-2"}], "nextPageToken": "t2", "isLast": True},
    ]
    sent = []

    def transport(method, url, headers, body):
        sent.append(json.loads(body))
        return 200, {}, json.dumps(pages[len(sent) - 1]).encode()

    config = make_config(tmp_path)
    client = JiraClient(config.jira_base_url, "e", "t", config.columns, transport=transport)
    assert client.tickets_in_column(Column.READY_FOR_AGENT, ["PAY"]) == ["PAY-1", "PAY-2"]
    assert [body.get("nextPageToken") for body in sent] == [None, "t1"]


def test_get_ticket_reads_fields_and_converts_the_description(site, jira):
    site.move("PAY-1", "Ready for agent")
    ticket = jira.get_ticket("PAY-1")
    assert (ticket.key, ticket.project_key, ticket.issue_type) == ("PAY-1", "PAY", "Bug")
    assert (ticket.summary, ticket.status) == ("Wrong refund currency", "Ready for agent")
    assert ticket.description.startswith("## Story\n\nRefunds use the wrong currency.")
    _, _, query, _ = site.requests[-1]
    assert query == {"fields": "summary,description,issuetype,project,status", "expand": "changelog"}


def test_requests_carry_basic_auth(site, tmp_path):
    seen = {}

    def transport(method, url, headers, body):
        seen.update(headers)
        return site.transport(method, url, headers, body)

    config = make_config(tmp_path)
    JiraClient(config.jira_base_url, "bot@example.com", "token", config.columns, transport=transport).get_ticket("PAY-1")
    assert seen["Authorization"] == "Basic " + base64.b64encode(b"bot@example.com:token").decode()


def test_status_change_id_names_the_latest_move_into_the_current_status(site, jira):
    assert jira.get_ticket("PAY-1").status_change_id == "created"
    site.move("PAY-1", "Ready for agent")
    first = jira.get_ticket("PAY-1").status_change_id
    site.edit("PAY-1", summary="Edited")
    assert jira.get_ticket("PAY-1").status_change_id == first
    site.move("PAY-1", "Needs info")
    site.move("PAY-1", "Ready for agent")
    assert jira.get_ticket("PAY-1").status_change_id not in (first, "created")


def test_status_change_is_found_past_the_embedded_changelog(tmp_path):
    issue = {
        "key": "PAY-1",
        "fields": {
            "summary": "s", "description": None, "issuetype": {"name": "Bug"},
            "project": {"key": "PAY"}, "status": {"id": "5", "name": "Ready for agent"},
        },
        "changelog": {"total": 3, "histories": [{"id": "30", "items": [{"field": "summary"}]}]},
    }
    full = {"values": [
        {"id": "10", "items": [{"field": "status", "to": "5"}]},
        {"id": "9", "items": [{"field": "status", "to": "5"}]},
        {"id": "30", "items": [{"field": "summary"}]},
    ], "total": 3, "isLast": True}

    def transport(method, url, headers, body):
        return 200, {}, json.dumps(full if "/changelog" in url else issue).encode()

    config = make_config(tmp_path)
    client = JiraClient(config.jira_base_url, "e", "t", config.columns, transport=transport)
    assert client.get_ticket("PAY-1").status_change_id == "10"


def test_post_comment_sends_the_markdown_as_a_document(site, jira):
    jira.post_comment("PAY-1", "Task **received**")
    assert site.comments("PAY-1") == [{
        "version": 1, "type": "doc",
        "content": [{"type": "paragraph", "content": [text("Task "), text("received", "strong")]}],
    }]


def test_move_ticket_uses_the_transition_into_the_column(site, jira):
    jira.move_ticket("PAY-1", Column.NEEDS_INFO)
    assert site.issues["PAY-1"]["status"] == "Needs info"
    jira.move_ticket("PAY-1", Column.NEEDS_INFO)  # already there
    assert site.issues["PAY-1"]["status"] == "Needs info"


def test_move_ticket_explains_a_missing_transition(site, tmp_path):
    config = make_config(tmp_path)
    config.columns[Column.DONE] = "Shipped"
    with pytest.raises(JiraError, match="cannot move from 'Backlog' to 'Shipped'"):
        site.client(config).move_ticket("PAY-1", Column.DONE)


def test_rate_limits_are_retried_and_other_errors_raised(site, jira):
    site.fail_next = [429, 503]
    assert jira.get_ticket("PAY-1").key == "PAY-1"
    site.fail_next = [401]
    with pytest.raises(JiraError, match="failed with 401") as error:
        jira.get_ticket("PAY-1")
    assert error.value.status == 401
    with pytest.raises(JiraError, match="404"):
        jira.get_ticket("PAY-404")


def test_default_transport_speaks_http(site, tmp_path):
    """The same calls over a real socket, through the urllib transport."""

    class Handler(BaseHTTPRequestHandler):
        def _serve(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0)) or None
            status, _, payload = site.transport(self.command, self.path, dict(self.headers), body)
            self.send_response(status)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = _serve

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        config = make_config(tmp_path)
        client = JiraClient(f"http://127.0.0.1:{server.server_port}", "e", "t", config.columns)
        client.move_ticket("PAY-1", Column.READY_FOR_AGENT)
        assert client.tickets_in_column(Column.READY_FOR_AGENT, ["PAY"]) == ["PAY-1"]
        assert client.get_ticket("PAY-1").status == "Ready for agent"
        client.post_comment("PAY-1", "Task received")
        assert len(site.comments("PAY-1")) == 1
        with pytest.raises(JiraError, match="404"):
            client.get_ticket("PAY-404")
    finally:
        server.shutdown()
        server.server_close()


def test_post_comment_returns_the_new_comment_id(site, jira):
    first = jira.post_comment("PAY-1", "one")
    second = jira.post_comment("PAY-1", "two")
    assert first != second
    assert [comment.id for comment in jira.get_comments("PAY-1")] == [first, second]


def test_comments_are_read_oldest_first_across_pages(site, jira):
    for number in range(150):
        site.add_comment("PAY-1", f"Reply **{number}**", author="Dana" if number % 2 else "Sam")
    comments = jira.get_comments("PAY-1")
    assert len(comments) == 150
    assert (comments[0].author, comments[0].body) == ("Sam", "Reply **0**")
    assert (comments[149].author, comments[149].body) == ("Dana", "Reply **149**")
    pages = [query for method, path, query, _ in site.requests if method == "GET" and path.endswith("/comment")]
    assert [page["startAt"] for page in pages] == ["0", "100"]
