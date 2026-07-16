from __future__ import annotations

import asyncio
import os
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from log_utils import append_channel_log, build_file_logger, setup_runtime_logging
from multichannel_gateway.adapters.vk import VkCallbackAdapter
from multichannel_gateway.bootstrap import build_gateway
from multichannel_gateway.outbound import _get_config_setting, _require_requests

try:
    from requests.exceptions import ReadTimeout as _RequestsReadTimeout
except ImportError:  # pragma: no cover
    _RequestsReadTimeout = None

try:
    from urllib3.exceptions import ReadTimeoutError as _Urllib3ReadTimeout
except ImportError:  # pragma: no cover
    _Urllib3ReadTimeout = None


def _is_longpoll_timeout(exc: BaseException) -> bool:
    """Возвращает True если исключение — это штатный таймаут Long Poll."""
    if _RequestsReadTimeout and isinstance(exc, _RequestsReadTimeout):
        return True
    if _Urllib3ReadTimeout and isinstance(exc, _Urllib3ReadTimeout):
        return True
    # На случай если класс недоступен — проверяем имя
    return type(exc).__name__ in ("ReadTimeout", "ReadTimeoutError")


class VkLongPollError(RuntimeError):
    pass


class VkLongPollConfigurationError(VkLongPollError):
    pass


@dataclass(frozen=True)
class VkLongPollSettings:
    token: str | None
    group_id: int | None
    api_version: str
    wait_seconds: int

    @classmethod
    def from_sources(cls) -> "VkLongPollSettings":
        raw_group_id = os.getenv("VK_GROUP_ID") or _get_config_setting("vk_group_id")
        if raw_group_id in (None, ""):
            group_id = None
        else:
            try:
                group_id = int(raw_group_id)
            except (TypeError, ValueError) as exc:
                raise VkLongPollConfigurationError(f"VK_GROUP_ID must be an integer, got: {raw_group_id!r}") from exc

        raw_wait = os.getenv("VK_LONGPOLL_WAIT") or _get_config_setting("vk_longpoll_wait", 25)
        try:
            wait_seconds = int(raw_wait)
        except (TypeError, ValueError) as exc:
            raise VkLongPollConfigurationError(f"VK_LONGPOLL_WAIT must be an integer, got: {raw_wait!r}") from exc
        if wait_seconds < 1:
            raise VkLongPollConfigurationError("VK_LONGPOLL_WAIT must be >= 1")

        token = (
            os.getenv("VK_LONGPOLL_TOKEN")
            or os.getenv("VK_BOT_TOKEN")
            or _get_config_setting("vk_longpoll_token")
            or _get_config_setting("vk_bot_token")
        )
        api_version = os.getenv("VK_API_VERSION") or _get_config_setting("vk_api_version", "5.199")
        return cls(token=token, group_id=group_id, api_version=api_version, wait_seconds=wait_seconds)

    def missing_fields(self) -> list[str]:
        missing: list[str] = []
        if not self.token:
            missing.append("VK_LONGPOLL_TOKEN or VK_BOT_TOKEN")
        if self.group_id is None:
            missing.append("VK_GROUP_ID")
        return missing


class VkLongPollClient:
    def __init__(self, settings: VkLongPollSettings | None = None):
        self.settings = settings or VkLongPollSettings.from_sources()
        self.http = _require_requests()
        self.server: str | None = None
        self.key: str | None = None
        self.ts: str | None = None

    def ensure_configured(self) -> None:
        missing = self.settings.missing_fields()
        if missing:
            raise VkLongPollConfigurationError(
                "VK long poll is not configured: missing " + ", ".join(missing)
            )

    @staticmethod
    def normalize_event(update: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": update.get("type"),
            "group_id": update.get("group_id"),
            "object": update.get("object") or {},
            "event_id": update.get("event_id"),
            "v": update.get("v"),
        }

    def refresh_server(self) -> dict[str, Any]:
        self.ensure_configured()
        response = self.http.post(
            "https://api.vk.com/method/groups.getLongPollServer",
            data={
                "group_id": self.settings.group_id,
                "access_token": self.settings.token,
                "v": self.settings.api_version,
            },
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise VkLongPollError(f"VK API groups.getLongPollServer failed: {payload['error']}")

        server_info = payload.get("response") or {}
        self.server = server_info.get("server")
        self.key = server_info.get("key")
        ts = server_info.get("ts")
        self.ts = str(ts) if ts is not None else None

        if not self.server or not self.key or self.ts is None:
            raise VkLongPollError(f"VK long poll server data is incomplete: {server_info}")
        return server_info

    def poll_once(self) -> list[dict[str, Any]]:
        if not self.server or not self.key or self.ts is None:
            self.refresh_server()

        response = self.http.get(
            self.server,
            params={
                "act": "a_check",
                "key": self.key,
                "ts": self.ts,
                "wait": self.settings.wait_seconds,
                "mode": 2,
            },
            timeout=self.settings.wait_seconds + 10,
        )
        response.raise_for_status()
        payload = response.json()

        failed = payload.get("failed")
        if failed == 1:
            self.ts = str(payload["ts"])
            return []
        if failed in {2, 3}:
            self.refresh_server()
            return []
        if failed:
            raise VkLongPollError(f"VK long poll returned unsupported failed={failed}: {payload}")

        ts = payload.get("ts")
        if ts is not None:
            self.ts = str(ts)
        updates = payload.get("updates") or []
        return [self.normalize_event(update) for update in updates]


class VkLongPollRunner:
    def __init__(
        self,
        client: VkLongPollClient | None = None,
        gateway=None,
        adapter: VkCallbackAdapter | None = None,
    ):
        self.client = client or VkLongPollClient()
        self.gateway = gateway or build_gateway()
        self.adapter = adapter or VkCallbackAdapter()
        self.log = build_file_logger("multichannel.vk_longpoll", "gateway")
        self.started_at_epoch = int(datetime.now(timezone.utc).timestamp())

    async def process_event(self, event: dict[str, Any]) -> list[Any]:
        event_type = (event.get("type") or "").lower()
        event_object = event.get("object") or {}
        message = event_object.get("message") or {}
        event_peer_id = message.get("peer_id") or event_object.get("peer_id")
        event_from_id = message.get("from_id") or event_object.get("from_id") or event_object.get("user_id")
        event_message_id = message.get("id") or event_object.get("conversation_message_id") or event_object.get("id")
        self.log.info(
            "VK Long Poll raw event: type=%s peer_id=%s from_id=%s message_id=%s",
            event.get("type"),
            event_peer_id,
            event_from_id,
            event_message_id,
        )
        if event_type != "message_new":
            self.log.info("Ignoring VK Long Poll event type: %s", event.get("type"))
            return []

        message_date = message.get("date") or event_object.get("date")
        if message_date is not None:
            try:
                if int(message_date) < self.started_at_epoch:
                    self.log.info(
                        "Skipping old VK event before runner start: event_date=%s started_at=%s peer_id=%s from_id=%s",
                        message_date,
                        self.started_at_epoch,
                        event_peer_id,
                        event_from_id,
                    )
                    return []
            except (TypeError, ValueError):
                pass

        raw_types = [str(item.get("type") or "") for item in (message.get("attachments") or [])]
        self.log.info(
            "VK Long Poll message_new: peer_id=%s from_id=%s message_id=%s raw_types=%s text_len=%s",
            message.get("peer_id"),
            message.get("from_id"),
            message.get("id"),
            raw_types,
            len(str(message.get("text") or "")),
        )
        envelope = self.adapter.build_envelope(event)
        jobs = await self.gateway.ingest(self.adapter, envelope)
        if jobs:
            self.log.info("VK Long Poll queued %s job(s) for %s", len(jobs), envelope.event_id)
        else:
            self.log.info("VK Long Poll produced no jobs for %s", envelope.event_id)
        return jobs

    async def run_once(self) -> list[Any]:
        events = await asyncio.to_thread(self.client.poll_once)
        jobs: list[Any] = []
        for event in events:
            jobs.extend(await self.process_event(event))
        return jobs

    async def run_forever(self, retry_delay_seconds: float = 5.0) -> None:
        self.client.ensure_configured()
        append_channel_log(
            "gateway",
            f"VK Long Poll started for group_id={self.client.settings.group_id} wait={self.client.settings.wait_seconds}",
        )
        while True:
            try:
                await self.run_once()
            except Exception as exc:
                if _is_longpoll_timeout(exc):
                    # Штатный таймаут Long Poll — VK намеренно держит соединение open
                    # и закрывает его через wait_seconds. Это не ошибка.
                    self.log.debug("VK Long Poll: штатный таймаут ожидания, переподключаемся")
                    continue
                print(f"❌ Ошибка VK Long Poll: {exc}")
                traceback.print_exc()
                self.log.exception("VK Long Poll cycle failed")
                append_channel_log("gateway", f"VK Long Poll error: {exc}")
                await asyncio.sleep(retry_delay_seconds)


async def run_vk_longpoll(once: bool = False) -> list[Any]:
    runner = VkLongPollRunner()
    runner.client.ensure_configured()
    if once:
        return await runner.run_once()
    await runner.run_forever()
    return []


def main() -> None:
    setup_runtime_logging()
    runner = VkLongPollRunner()
    print(
        f"VK Long Poll listening for group_id={runner.client.settings.group_id} "
        f"(wait={runner.client.settings.wait_seconds}, api={runner.client.settings.api_version})"
    )
    try:
        asyncio.run(runner.run_forever())
    except VkLongPollConfigurationError as exc:
        raise SystemExit(str(exc)) from exc
    except KeyboardInterrupt:
        append_channel_log("gateway", "VK Long Poll stopped by keyboard interrupt")
        print("\nVK Long Poll stopped")


if __name__ == "__main__":
    main()
