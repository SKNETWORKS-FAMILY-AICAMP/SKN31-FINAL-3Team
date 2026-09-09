#!/usr/bin/env python3
"""Minimal authenticated HTTPS-to-local deployment trigger.

Nginx terminates TLS and proxies only the exact trigger path to this service.
The service validates a timestamped HMAC request, then launches the existing
root-owned deployment dispatcher through its narrow sudo rule.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


HOST = os.getenv("BIDDINGFLOW_DEPLOY_TRIGGER_HOST", "127.0.0.1")
PORT = int(os.getenv("BIDDINGFLOW_DEPLOY_TRIGGER_PORT", "8091"))
TOKEN = os.environ["BIDDINGFLOW_DEPLOY_TOKEN"].encode("utf-8")
MAX_BODY_BYTES = 2048
MAX_CLOCK_SKEW_SECONDS = 300
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
ALLOWED_COMPONENTS = {"backend", "frontend"}
DISPATCHER = "/usr/local/sbin/biddingflow-github-dispatch"


class DeployTriggerHandler(BaseHTTPRequestHandler):
    server_version = "BiddingFlowDeployTrigger/1"

    def _reply(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path != "/trigger":
            self._reply(404, {"detail": "not found"})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if not 0 < content_length <= MAX_BODY_BYTES:
            self._reply(400, {"detail": "invalid request"})
            return

        body = self.rfile.read(content_length)
        timestamp_text = self.headers.get("X-BiddingFlow-Timestamp", "")
        supplied_signature = self.headers.get("X-BiddingFlow-Signature", "")

        try:
            timestamp = int(timestamp_text)
        except ValueError:
            self._reply(401, {"detail": "invalid signature"})
            return

        if abs(int(time.time()) - timestamp) > MAX_CLOCK_SKEW_SECONDS:
            self._reply(401, {"detail": "expired signature"})
            return

        signed_body = timestamp_text.encode("ascii") + b"." + body
        expected_signature = hmac.new(TOKEN, signed_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            self._reply(401, {"detail": "invalid signature"})
            return

        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._reply(400, {"detail": "invalid request"})
            return
        if not isinstance(payload, dict):
            self._reply(400, {"detail": "invalid request"})
            return

        component = payload.get("component")
        commit_sha = payload.get("commit_sha")
        if component not in ALLOWED_COMPONENTS or not isinstance(commit_sha, str):
            self._reply(400, {"detail": "invalid deployment target"})
            return
        if not SHA_PATTERN.fullmatch(commit_sha):
            self._reply(400, {"detail": "invalid deployment target"})
            return

        try:
            subprocess.Popen(
                ["sudo", "-n", DISPATCHER, component, commit_sha],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        except OSError:
            self._reply(500, {"detail": "deployment trigger unavailable"})
            return
        self._reply(202, {"accepted": True, "component": component, "commit_sha": commit_sha})

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        self._reply(405, {"detail": "method not allowed"})

    def log_message(self, message_format: str, *args: Any) -> None:
        print(
            "%s - - [%s] %s"
            % (self.client_address[0], self.log_date_time_string(), message_format % args),
            flush=True,
        )


if __name__ == "__main__":
    if len(TOKEN) < 32:
        raise RuntimeError("BIDDINGFLOW_DEPLOY_TOKEN must contain at least 32 characters")
    ThreadingHTTPServer((HOST, PORT), DeployTriggerHandler).serve_forever()
