"""Receive Jira webhooks.

A delivery is only a signal that a ticket changed. Nothing in its body is
trusted beyond the ticket key; the ticket is fetched again through the API.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

log = logging.getLogger(__name__)

PATH = "/webhooks/jira"
EVENT = "jira:issue_updated"
_MAX_BODY = 10 * 1024 * 1024


def make_server(
    host: str, port: int, on_ticket: Callable[[str], None], secret: str | None
) -> ThreadingHTTPServer:
    """An HTTP server that calls `on_ticket(key)` for each issue-updated delivery.

    With a secret, a delivery must carry a matching X-Hub-Signature header.
    """

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if self.path.split("?")[0] != PATH:
                return self._reply(404)
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                return self._reply(411)
            if length > _MAX_BODY:
                return self._reply(413)
            body = self.rfile.read(length)
            if secret and not _signature_ok(secret, body, self.headers.get("X-Hub-Signature", "")):
                log.warning("webhook delivery rejected: bad or missing signature")
                return self._reply(401)
            try:
                payload = json.loads(body)
                event = payload.get("webhookEvent")
                key = payload["issue"]["key"] if event == EVENT else None
            except (ValueError, AttributeError, KeyError, TypeError):
                return self._reply(400)
            if isinstance(key, str) and key:
                on_ticket(key)
            self._reply(204)

        def _reply(self, status: int) -> None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            log.debug("webhook: " + format, *args)

    return ThreadingHTTPServer((host, port), Handler)


def _signature_ok(secret: str, body: bytes, header: str) -> bool:
    method, _, digest = header.partition("=")
    if method.strip().lower() != "sha256":
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, digest.strip().lower())
