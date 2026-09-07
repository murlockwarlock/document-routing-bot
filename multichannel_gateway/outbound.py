from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from multichannel_gateway.bootstrap import build_outbox

try:
    import requests
except ImportError:  # pragma: no cover - fallback for thin test/runtime environments
    requests = None


class OutboundDispatchError(RuntimeError):
    pass


_config_loaded = False


def _require_requests():
    if requests is None:
        raise OutboundDispatchError("The 'requests' package is required for VK/MAX outbound delivery")
    return requests


def _get_config_setting(key: str, default=None):
    """Read a setting from Config (lazy import to avoid circular deps)."""
    global _config_loaded
    try:
        from config import Config
        if not _config_loaded:
            Config.load_config()
            _config_loaded = True
        return Config.get_setting(key, default)
    except Exception:
        return default


class VkOutboundClient:
    def __init__(self, token: str | None = None, api_version: str | None = None):
        self._token_override = token
        self._api_version_override = api_version
        self.base_url = "https://api.vk.com/method"

    @property
    def token(self) -> str | None:
        return os.getenv("VK_BOT_TOKEN") or self._token_override or _get_config_setting("vk_bot_token")

    @property
    def api_version(self) -> str:
        return os.getenv("VK_API_VERSION") or self._api_version_override or _get_config_setting("vk_api_version", "5.199")

    def is_configured(self) -> bool:
        return bool(self.token)

    def send_message(self, peer_id: str | int, text: str) -> dict[str, Any]:
        if not self.token:
            raise OutboundDispatchError("VK_BOT_TOKEN is not configured")
        peer_id = int(peer_id)
        return self._api_call(
            "messages.send",
            {
                "peer_id": peer_id,
                "message": text[:4096],
                "random_id": int.from_bytes(os.urandom(4), "big") & 0x7FFFFFFF,
            },
        )

    def send_document(self, peer_id: str | int, file_path: str | Path, caption: str | None = None, file_name: str | None = None) -> dict[str, Any]:
        if not self.token:
            raise OutboundDispatchError("VK_BOT_TOKEN is not configured")
        http = _require_requests()

        peer_id = int(peer_id)
        file_path = str(file_path)

        upload_data = self._api_call(
            "docs.getMessagesUploadServer",
            {
                "peer_id": peer_id,
                "type": "doc",
            },
        )
        upload_url = upload_data.get("upload_url")
        if not upload_url:
            raise OutboundDispatchError("VK upload_url was not returned")

        upload_name = file_name or os.path.basename(file_path)
        with open(file_path, "rb") as handle:
            upload_response = http.post(upload_url, files={"file": (upload_name, handle)}, timeout=120)
        upload_response.raise_for_status()
        upload_payload = upload_response.json()
        file_token = upload_payload.get("file")
        if not file_token:
            raise OutboundDispatchError(f"VK upload did not return file token: {upload_payload}")

        saved = self._api_call("docs.save", {"file": file_token})
        items = saved if isinstance(saved, list) else saved.get("doc") if isinstance(saved, dict) else None
        if isinstance(items, list):
            doc = items[0] if items else None
        else:
            doc = items
        if not doc:
            raise OutboundDispatchError(f"VK docs.save did not return document: {saved}")

        attachment = f"doc{doc['owner_id']}_{doc['id']}"
        send_payload = {
            "peer_id": peer_id,
            "attachment": attachment,
            "random_id": int.from_bytes(os.urandom(4), "big") & 0x7FFFFFFF,
        }
        if caption:
            send_payload["message"] = caption[:900]

        return self._api_call("messages.send", send_payload)

    def _api_call(self, method: str, params: dict[str, Any]) -> Any:
        http = _require_requests()
        response = http.post(
            f"{self.base_url}/{method}",
            data={**params, "access_token": self.token, "v": self.api_version},
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise OutboundDispatchError(f"VK API {method} failed: {payload['error']}")
        return payload.get("response")


class MaxOutboundClient:
    def __init__(self, token: str | None = None):
        self._token_override = token
        self.base_url = "https://platform-api.max.ru"

    @property
    def token(self) -> str | None:
        return os.getenv("MAX_BOT_TOKEN") or self._token_override or _get_config_setting("max_bot_token")

    def is_configured(self) -> bool:
        return bool(self.token)

    def send_document(
        self,
        recipient_user_id: str | int | None,
        file_path: str | Path,
        caption: str | None = None,
        chat_id: str | int | None = None,
    ) -> dict[str, Any]:
        if not self.token:
            raise OutboundDispatchError("MAX_BOT_TOKEN is not configured")
        http = _require_requests()

        upload_meta = self._request("POST", "/uploads", params={"type": "file"})
        upload_url = upload_meta.get("url")
        if not upload_url:
            raise OutboundDispatchError(f"MAX /uploads did not return url: {upload_meta}")

        with open(file_path, "rb") as handle:
            upload_response = http.post(
                upload_url,
                headers={"Authorization": self.token},
                files={"data": handle},
                timeout=120,
            )
        upload_response.raise_for_status()
        upload_payload = upload_response.json()
        file_token = upload_payload.get("token")
        if not file_token:
            raise OutboundDispatchError(f"MAX upload did not return token: {upload_payload}")

        params: dict[str, Any] = {}
        if recipient_user_id not in (None, ""):
            params["user_id"] = recipient_user_id
        elif chat_id not in (None, ""):
            params["chat_id"] = chat_id
        else:
            raise OutboundDispatchError("MAX recipient is missing: provide user_id or chat_id")

        body = {
            "text": caption or "Результат проверки",
            "attachments": [
                {
                    "type": "file",
                    "payload": {"token": file_token},
                }
            ],
        }
        return self._request("POST", "/messages", params=params, json_body=body)

    def _request(self, method: str, path: str, params: dict[str, Any] | None = None, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
        http = _require_requests()
        response = http.request(
            method,
            f"{self.base_url}{path}",
            headers={"Authorization": self.token},
            params=params or {},
            json=json_body,
            timeout=60,
        )
        response.raise_for_status()
        return response.json()


class OutboundDispatcher:
    def __init__(self):
        self.vk = VkOutboundClient()
        self.max = MaxOutboundClient()
        self.outbox = build_outbox()

    def send_result(
        self,
        source_platform: str,
        route: dict[str, Any],
        file_path: str | Path,
        caption: str | None = None,
        file_name: str | None = None,
        allow_spool: bool = True,
    ) -> dict[str, Any]:
        source = (source_platform or "").lower()
        try:
            if source == "vk":
                return self.vk.send_document(peer_id=route["chat_id"], file_path=file_path, caption=caption, file_name=file_name)
            if source == "max":
                return self.max.send_document(
                    recipient_user_id=route.get("sender_id"),
                    chat_id=route.get("chat_id"),
                    file_path=file_path,
                    caption=caption,
                )
        except Exception as exc:
            if allow_spool and source in {"vk", "max"}:
                manifest = self.outbox.spool_delivery(
                    source_platform=source,
                    route=route,
                    file_path=file_path,
                    caption=caption,
                    file_name=file_name,
                    reason=str(exc),
                )
                return {
                    "platform": source,
                    "status": "spooled",
                    "outbox_id": manifest["id"],
                    "reason": str(exc),
                }
            raise

        if allow_spool and source in {"vk", "max"}:
            manifest = self.outbox.spool_delivery(
                source_platform=source,
                route=route,
                file_path=file_path,
                caption=caption,
                file_name=file_name,
                reason="client_not_configured",
            )
            return {
                "platform": source,
                "status": "spooled",
                "outbox_id": manifest["id"],
                "reason": "client_not_configured",
            }
        raise OutboundDispatchError(f"Unsupported outbound source_platform: {source_platform}")

    def list_outbox(self, limit: int = 50, pending_only: bool = False) -> list[dict[str, Any]]:
        return self.outbox.list_entries(limit=limit, pending_only=pending_only)

    def replay_outbox(self, limit: int = 20, source_platform: str | None = None) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        entries = self.outbox.list_entries(limit=limit * 5, pending_only=True)
        for entry in entries:
            if source_platform and entry.get("source_platform") != source_platform:
                continue
            try:
                response = self.send_result(
                    entry["source_platform"],
                    entry["route"],
                    entry["stored_file_path"],
                    entry.get("caption"),
                    file_name=entry.get("file_name"),
                    allow_spool=False,
                )
                self.outbox.mark_sent(entry["id"], response)
                results.append({"id": entry["id"], "status": "sent"})
            except Exception as exc:
                self.outbox.mark_failed(entry["id"], str(exc))
                results.append({"id": entry["id"], "status": "pending", "error": str(exc)})
            if len(results) >= limit:
                break
        return results
