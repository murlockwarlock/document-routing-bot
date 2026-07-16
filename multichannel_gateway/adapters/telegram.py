from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from multichannel_gateway.core.files import AtomicFileStore
from multichannel_gateway.core.models import AttachmentRef, DownloadedAttachment, InboundEnvelope, utc_now

from .base import TransportAdapter


class TelegramPyrogramAdapter(TransportAdapter):
    name = "telegram"

    @staticmethod
    def _string_or_none(value: Any) -> str | None:
        if value in (None, ""):
            return None
        return str(value)

    @staticmethod
    def build_envelope(message: Any) -> InboundEnvelope:
        document = getattr(message, "document", None)
        attachments: tuple[AttachmentRef, ...] = ()
        if document is not None:
            attachments = (
                AttachmentRef(
                    index=0,
                    file_name=getattr(document, "file_name", None) or "document.bin",
                    mime_type=getattr(document, "mime_type", None),
                    external_id=getattr(document, "file_id", None),
                    size_bytes=getattr(document, "file_size", None),
                    adapter_meta={"pyrogram_message": message},
                ),
            )

        chat_id = str(getattr(getattr(message, "chat", None), "id", ""))
        from_user = getattr(message, "from_user", None)
        sender_id = str(getattr(from_user, "id", ""))
        sender_name = getattr(from_user, "username", None) or getattr(from_user, "first_name", None)

        return InboundEnvelope(
            source="telegram",
            event_id=f"telegram:{chat_id}:{getattr(message, 'id', '')}",
            chat_id=chat_id,
            sender_id=sender_id,
            sender_name=sender_name,
            message_id=str(getattr(message, "id", "")),
            reply_to_message_id=TelegramPyrogramAdapter._string_or_none(getattr(message, "reply_to_message_id", None)),
            text=getattr(message, "text", None),
            attachments=attachments,
            received_at=utc_now(),
        )

    async def download_attachment(self, envelope: InboundEnvelope, attachment: AttachmentRef) -> DownloadedAttachment:
        message = attachment.adapter_meta.get("pyrogram_message")
        if message is None:
            raise ValueError("pyrogram_message is missing from attachment meta")

        suffix = Path(AtomicFileStore.sanitize_name(attachment.file_name)).suffix
        with tempfile.NamedTemporaryFile(prefix="tg_", suffix=suffix, delete=False) as tmp:
            temp_path = Path(tmp.name)

        downloaded_path = await message.download(str(temp_path))
        return DownloadedAttachment(
            original_file_name=attachment.file_name,
            mime_type=attachment.mime_type,
            file_path=Path(downloaded_path),
            size_bytes=attachment.size_bytes,
            cleanup_after_persist=True,
        )
