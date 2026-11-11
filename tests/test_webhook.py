import hashlib
import hmac
import json
import threading
import urllib.error
import urllib.request

import pytest

from taskbot.webhook import make_server

BODY = json.dumps({"webhookEvent": "jira:issue_updated", "issue": {"key": "PAY-1", "fields": {"summary": "ignored"}}}).encode()


@pytest.fixture
def serve():
    servers = []

    def start(secret=None):
        keys = []
        server = make_server("127.0.0.1", 0, keys.append, secret)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_port}", keys

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def post(url, body=BODY, headers=None):
    request = urllib.request.Request(url, data=body, method="POST", headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def test_issue_updated_delivery_signals_the_ticket_key(serve):
    url, keys = serve()
    assert post(url + "/webhooks/jira") == 204
    assert keys == ["PAY-1"]


def test_other_events_paths_and_bodies_signal_nothing(serve):
    url, keys = serve()
    other = json.dumps({"webhookEvent": "comment_created", "issue": {"key": "PAY-1"}}).encode()
    assert post(url + "/webhooks/jira", other) == 204
    assert post(url + "/webhooks/jira", b"not json") == 400
    assert post(url + "/webhooks/jira", b'{"webhookEvent": "jira:issue_updated"}') == 400
    assert post(url + "/elsewhere") == 404
    assert keys == []


def test_with_a_secret_only_signed_deliveries_are_accepted(serve):
    url, keys = serve(secret="s3cret")
    good = "sha256=" + hmac.new(b"s3cret", BODY, hashlib.sha256).hexdigest()
    bad = "sha256=" + hmac.new(b"wrong", BODY, hashlib.sha256).hexdigest()
    assert post(url + "/webhooks/jira") == 401
    assert post(url + "/webhooks/jira", headers={"X-Hub-Signature": bad}) == 401
    assert keys == []
    assert post(url + "/webhooks/jira", headers={"X-Hub-Signature": good}) == 204
    assert keys == ["PAY-1"]
