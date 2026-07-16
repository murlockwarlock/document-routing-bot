from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
from pathlib import Path
from typing import Protocol

from .files import AtomicFileStore
from .models import AttachmentRef, InboundEnvelope, JobRecord, ProcessingResult
from .storage import SqliteJobStore


class InboundTransport(Protocol):
    name: str

    async def download_attachment(self, envelope: InboundEnvelope, attachment: AttachmentRef):
        ...


class JobProcessor(Protocol):
    async def process(self, job: JobRecord) -> ProcessingResult:
        ...


class InboundGateway:
    def __init__(self, store: SqliteJobStore, file_store: AtomicFileStore, logger: logging.Logger | None = None):
        self.store = store
        self.file_store = file_store
        self.log = logger or logging.getLogger(__name__)
        self._vk_sender_cache: dict[str, str | None] = {}

    @staticmethod
    def _normalize_vk_id(value: str) -> str:
        v = value.strip()
        lowered = v.lower()
        for prefix in ("https://vk.com/", "http://vk.com/", "vk.com/"):
            if lowered.startswith(prefix):
                v = v[len(prefix):]
                lowered = v.lower()
                break
        if v.lower().startswith("vk:"):
            v = v[3:]
        if v.lower().startswith("id") and v[2:].isdigit():
            v = v[2:]
        if v.startswith("-") and v[1:].isdigit():
            v = v[1:]
        return v

    @staticmethod
    def _normalize_vk_name(value: str) -> str:
        v = value.strip()
        if v.startswith("@"):
            v = v[1:]
        if v.lower().startswith("https://vk.com/"):
            v = v[len("https://vk.com/"):]
        elif v.lower().startswith("http://vk.com/"):
            v = v[len("http://vk.com/"):]
        elif v.lower().startswith("vk.com/"):
            v = v[len("vk.com/"):]
        elif v.lower().startswith("vk:"):
            v = v[3:]
        return v.strip().strip("/").lower()

    def _fetch_vk_sender_name(self, sender_id: str) -> str | None:
        cached = self._vk_sender_cache.get(sender_id)
        if sender_id in self._vk_sender_cache:
            return cached

        try:
            from config import Config
            Config.load_config()
            config_get = Config.get_setting
        except Exception:
            config_get = lambda key, default=None: default

        try:
            import requests
        except ImportError:
            self._vk_sender_cache[sender_id] = None
            return None

        token = (
            os.getenv("VK_LONGPOLL_TOKEN")
            or os.getenv("VK_BOT_TOKEN")
            or config_get("vk_longpoll_token")
            or config_get("vk_bot_token")
        )
        api_version = os.getenv("VK_API_VERSION") or config_get("vk_api_version", "5.199")
        if not token:
            self._vk_sender_cache[sender_id] = None
            return None

        try:
            if str(sender_id).startswith("-"):
                response = requests.post(
                    "https://api.vk.com/method/groups.getById",
                    data={
                        "group_ids": str(sender_id).lstrip("-"),
                        "access_token": token,
                        "v": api_version,
                    },
                    timeout=30,
                )
            else:
                response = requests.post(
                    "https://api.vk.com/method/users.get",
                    data={
                        "user_ids": sender_id,
                        "fields": "screen_name",
                        "access_token": token,
                        "v": api_version,
                    },
                    timeout=30,
                )
            response.raise_for_status()
            payload = response.json()
            if "error" in payload:
                self.log.warning("VK sender_name lookup failed for %s: %s", sender_id, payload["error"])
                self._vk_sender_cache[sender_id] = None
                return None

            items = payload.get("response") or []
            if not items:
                self._vk_sender_cache[sender_id] = None
                return None

            raw_name = items[0].get("screen_name")
            normalized = self._normalize_vk_name(str(raw_name)) if raw_name else None
            self._vk_sender_cache[sender_id] = normalized
            return normalized
        except Exception as exc:
            self.log.warning("VK sender_name lookup exception for %s: %s", sender_id, exc)
            self._vk_sender_cache[sender_id] = None
            return None

    async def _is_sender_allowed(self, envelope: InboundEnvelope) -> bool:
        """Check if the sender is in allowed_authors.
        Supports: vk:123, 123, @username, username, vk.com/username.

        Пользователи из plagiscan_users считаются разрешёнными автоматически —
        они проходят принудительную проверку в Плагискане независимо от allowed_authors.
        """
        try:
            from config import Config
            allowed = Config.get_setting("allowed_authors", [])
            plagiscan_users = Config.get_setting("plagiscan_users", [])
        except Exception:
            allowed = []
            plagiscan_users = []
        if not allowed:
            return True  # empty list = accept all
        sender_id = envelope.sender_id or ""
        source = (envelope.source or "").lower()
        if source == "vk":
            normalized_sender_id = InboundGateway._normalize_vk_id(sender_id)
            for entry in allowed:
                bare = InboundGateway._normalize_vk_id(str(entry))
                if bare.isdigit() and bare == normalized_sender_id:
                    return True

            sender_name = self._normalize_vk_name(envelope.sender_name or "")
            if not sender_name:
                sender_name = await asyncio.to_thread(self._fetch_vk_sender_name, sender_id)

            for entry in allowed:
                normalized_name = self._normalize_vk_name(str(entry))
                if sender_name and normalized_name and not self._normalize_vk_id(str(entry)).isdigit() and normalized_name == sender_name:
                    return True

            # Если пользователь в plagiscan_users — разрешаем, даже если не в allowed_authors.
            # Файл позже будет направлен в Плагискан принудительно.
            for entry in plagiscan_users:
                bare = InboundGateway._normalize_vk_id(str(entry))
                if bare.isdigit() and bare == normalized_sender_id:
                    return True
                normalized_name = self._normalize_vk_name(str(entry))
                if sender_name and normalized_name and normalized_name == sender_name:
                    return True
            return False
        # Telegram: author check is already performed by bot_handlers before calling ingest_pyrogram_message.
        # Skip duplicate check here to avoid rejecting files when allowed_authors contains only VK IDs.
        if source == "telegram":
            return True
        # Telegram: match by username OR numeric user_id (allowed_authors may contain either)
        sender_name = (envelope.sender_name or "").replace("@", "")
        sender_id_str = str(envelope.sender_id or "")
        allowed_bare = [str(a).replace("@", "") for a in allowed]
        return sender_name in allowed_bare or sender_id_str in allowed_bare

    async def ingest(self, adapter: InboundTransport, envelope: InboundEnvelope) -> list[JobRecord]:
        jobs: list[JobRecord] = []
        if not envelope.attachments:
            debug = (envelope.raw_payload or {}).get("_debug") or {}
            self.log.info(
                "Skipping message without supported attachments: event=%s source=%s chat_id=%s sender_id=%s raw_types=%s has_reply=%s forwarded=%s",
                envelope.event_id,
                envelope.source,
                envelope.chat_id,
                envelope.sender_id,
                debug.get("raw_attachment_types"),
                debug.get("has_reply_message"),
                debug.get("forwarded_count"),
            )
            return jobs

        if not await self._is_sender_allowed(envelope):
            self.log.info("Sender %s:%s not in allowed_authors, skipping", envelope.source, envelope.sender_id)
            return jobs

        # Для VK: если sender_name не задан адаптером, подтягиваем screen_name через API.
        # Это нужно чтобы route_sender_name дошёл до is_force_plagiscan_author.
        if envelope.source == "vk" and not envelope.sender_name and envelope.sender_id:
            resolved_name = await asyncio.to_thread(self._fetch_vk_sender_name, envelope.sender_id)
            if resolved_name:
                envelope = dataclasses.replace(envelope, sender_name=resolved_name)

        for attachment in envelope.attachments:
            dedupe_key = envelope.attachment_dedupe_key(attachment)
            existing = await asyncio.to_thread(self.store.get_by_dedupe_key, dedupe_key)
            if existing is not None:
                self.log.info("Duplicate skipped: %s", dedupe_key)
                jobs.append(existing)
                continue

            downloaded = await adapter.download_attachment(envelope, attachment)
            saved_file = await asyncio.to_thread(
                self.file_store.persist_downloaded_attachment,
                dedupe_key,
                downloaded,
            )
            job = await asyncio.to_thread(self.store.enqueue_file, envelope, attachment, saved_file)
            self.log.info("Queued job %s from %s", job.job_id, envelope.source)
            jobs.append(job)

        return jobs


class QueueWorker:
    def __init__(self, store: SqliteJobStore, processor: JobProcessor, worker_name: str = "worker-1"):
        self.store = store
        self.processor = processor
        self.worker_name = worker_name
        self._stop_event = asyncio.Event()

    async def run_forever(self, idle_sleep_seconds: float = 2.0) -> None:
        while not self._stop_event.is_set():
            job = await asyncio.to_thread(self.store.claim_next_queued, self.worker_name)
            if job is None:
                await asyncio.sleep(idle_sleep_seconds)
                continue

            try:
                result = await self.processor.process(job)
                if result.success:
                    await asyncio.to_thread(self.store.mark_done, job.job_id, result.result_path, result.note)
                else:
                    await asyncio.to_thread(
                        self.store.mark_failed,
                        job.job_id,
                        result.note or result.status,
                        result.retry_delay_seconds,
                    )
            except Exception as exc:
                await asyncio.to_thread(self.store.mark_failed, job.job_id, str(exc), 60)

    def stop(self) -> None:
        self._stop_event.set()


class PassthroughProcessor:
    """Temporary processor placeholder until the old Telegram pipeline is bridged."""

    def __init__(self, inbox_dir: str | Path):
        self.inbox_dir = Path(inbox_dir)

    async def process(self, job: JobRecord) -> ProcessingResult:
        if not Path(job.file_path).exists():
            return ProcessingResult(success=False, status="missing_file", note="Local file disappeared", retry_delay_seconds=30)
        return ProcessingResult(success=True, status="ready_for_bridge", result_path=job.file_path, note="Stored and ready for downstream pipeline")
