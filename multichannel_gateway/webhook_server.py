from __future__ import annotations

import asyncio
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from log_utils import append_channel_log, setup_runtime_logging
from multichannel_gateway.adapters.max_adapter import MaxBotAdapter
from multichannel_gateway.adapters.vk import VkCallbackAdapter
from multichannel_gateway.bootstrap import build_gateway, build_store


class GatewayWebhookHandler(BaseHTTPRequestHandler):
    server_version = "MultichannelGateway/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._write_json({"status": "ok", "queue": build_store().get_stats()})
            return
        self._write_json({"error": "not_found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path not in {"/webhook/vk", "/webhook/max"}:
            self._write_json({"error": "not_found"}, status=HTTPStatus.NOT_FOUND)
            return

        content_length = int(self.headers.get("Content-Length", "0") or 0)
        raw_body = self.rfile.read(content_length)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError:
            self._write_json({"error": "invalid_json"}, status=HTTPStatus.BAD_REQUEST)
            return

        if parsed.path == "/webhook/vk":
            self._handle_vk(payload)
            return

        self._handle_max(payload)

    def _handle_vk(self, payload: dict) -> None:
        event_type = payload.get("type")
        if event_type == "confirmation":
            token = os.getenv("VK_CONFIRMATION_TOKEN")
            if not token:
                self._write_json({"error": "vk_confirmation_token_missing"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self._write_text(token)
            return

        if event_type != "message_new":
            self._write_text("ok")
            return

        expected_secret = os.getenv("VK_CALLBACK_SECRET")
        if expected_secret and payload.get("secret") != expected_secret:
            self._write_json({"error": "vk_secret_mismatch"}, status=HTTPStatus.FORBIDDEN)
            return

        adapter = VkCallbackAdapter()
        try:
            envelope = adapter.build_envelope(payload)
            jobs = asyncio.run(build_gateway().ingest(adapter, envelope))
            self._write_json({"status": "ok", "jobs": [job.job_id for job in jobs]})
        except Exception as exc:
            self._write_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_max(self, payload: dict) -> None:
        expected_secret = os.getenv("MAX_WEBHOOK_SECRET")
        if expected_secret and self.headers.get("X-Webhook-Secret") != expected_secret:
            self._write_json({"error": "max_secret_mismatch"}, status=HTTPStatus.FORBIDDEN)
            return

        update_type = payload.get("update_type")
        if update_type != "message_created":
            self._write_json({"status": "ignored", "update_type": update_type})
            return

        adapter = MaxBotAdapter()
        try:
            envelope = adapter.build_envelope(payload)
            jobs = asyncio.run(build_gateway().ingest(adapter, envelope))
            self._write_json({"status": "ok", "jobs": [job.job_id for job in jobs]})
        except Exception as exc:
            self._write_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def log_message(self, format: str, *args) -> None:
        return

    def _write_text(self, payload: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = payload.encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    setup_runtime_logging()
    host = os.getenv("GATEWAY_HOST", "0.0.0.0")
    port = int(os.getenv("GATEWAY_PORT", "8080"))
    Path("multichannel_gateway/data").mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((host, port), GatewayWebhookHandler)
    print(f"Webhook server listening on http://{host}:{port}")
    append_channel_log("gateway", f"Webhook server listening on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
