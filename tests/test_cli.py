import json
import threading
import time
import urllib.request

from helpers import FakeJira, doc, make_config, para
from taskbot import cli
from taskbot.config import WebhookConfig
from taskbot.store import Store


def test_run_once_polls_and_records(tmp_path):
    config = make_config(tmp_path)
    site = FakeJira()
    site.add_issue("PAY-1")
    site.move("PAY-1", "Ready for agent")
    store = Store(config.database_path)
    cli.run(config, site.client(config), store, once=True)
    assert [stored.task.ticket_key for stored in store.list_tasks()] == ["PAY-1"]
    store.close()


def test_webhook_delivery_is_handled_without_waiting_for_the_next_poll(tmp_path, monkeypatch):
    config = make_config(tmp_path, poll_interval_seconds=3600, webhook=WebhookConfig("127.0.0.1", 0, None))
    site = FakeJira()
    site.add_issue("PAY-1")
    ports = []
    real_make_server = cli.make_server

    def recording_make_server(*args):
        server = real_make_server(*args)
        ports.append(server.server_port)
        return server

    monkeypatch.setattr(cli, "make_server", recording_make_server)

    def service():
        # A SQLite connection belongs to the thread that opens it.
        store = Store(config.database_path)
        cli.run(config, site.client(config), store)

    threading.Thread(target=service, daemon=True).start()
    wait_for(lambda: ports and any(path.endswith("search/jql") for _, path, _, _ in site.requests))

    site.move("PAY-1", "Ready for agent")
    body = json.dumps({"webhookEvent": "jira:issue_updated", "issue": {"key": "PAY-1"}}).encode()
    urllib.request.urlopen(
        urllib.request.Request(f"http://127.0.0.1:{ports[0]}/webhooks/jira", data=body, method="POST"), timeout=5
    )
    wait_for(lambda: site.comments("PAY-1"))
    assert site.comments("PAY-1") == [doc(para("Task received"))]


def test_check_reports_missing_columns(tmp_path, capsys):
    config = make_config(tmp_path)

    class Jira:
        def myself(self):
            return "Task Bot"

        def project_statuses(self, key):
            return {"Backlog", "Ready for agent", "In Progress", "Done"}

    assert cli.check(config, Jira()) is False
    out = capsys.readouterr().out
    assert "Signed in to https://example.atlassian.net as Task Bot" in out
    assert "PAY -> acme/payments: missing columns: Needs info, Plan review, In review" in out


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)
