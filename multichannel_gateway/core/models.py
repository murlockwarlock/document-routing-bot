from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class AttachmentRef:
    index: int
    file_name: str
    mime_type: str | None = None
    external_id: str | None = None
    size_bytes: int | None = None
    download_url: str | None = None
    adapter_meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InboundEnvelope:
    source: str
    event_id: str
    chat_id: str
    sender_id: str
    sender_name: str | None
    message_id: str
    reply_to_message_id: str | None = None
    text: str | None = None
    attachments: tuple[AttachmentRef, ...] = ()
    received_at: datetime = field(default_factory=utc_now)
    raw_payload: dict[str, Any] | None = None

    def attachment_dedupe_key(self, attachment: AttachmentRef) -> str:
        return f"{self.source}:{self.chat_id}:{self.message_id}:{attachment.index}"


@dataclass(frozen=True)
class SavedFile:
    path: Path
    sha256: str
    size_bytes: int
    original_file_name: str
    mime_type: str | None = None


@dataclass(frozen=True)
class DownloadedAttachment:
    original_file_name: str
    mime_type: str | None = None
    file_path: Path | None = None
    content_bytes: bytes | None = None
    remote_url: str | None = None
    size_bytes: int | None = None
    cleanup_after_persist: bool = False


@dataclass(frozen=True)
class ProcessingResult:
    success: bool
    status: str
    result_path: str | None = None
    note: str | None = None
    retry_delay_seconds: int | None = None


@dataclass(frozen=True)
class JobRecord:
    job_id: int
    dedupe_key: str
    source: str
    chat_id: str
    sender_id: str
    sender_name: str | None
    message_id: str
    reply_to_message_id: str | None
    text: str | None
    attachment_index: int
    original_file_name: str
    mime_type: str | None
    file_path: str
    file_sha256: str
    file_size_bytes: int
    status: str
    attempts: int
    last_error: str | None
    locked_by: str | None
    created_at: str
    updated_at: str
    result_path: str | None
    transport_meta: dict[str, Any]
