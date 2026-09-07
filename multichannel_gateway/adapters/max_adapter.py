from __future__ import annotations

from typing import Any

from multichannel_gateway.core.models import AttachmentRef, DownloadedAttachment, InboundEnvelope, utc_now

from .base import TransportAdapter


class MaxBotAdapter(TransportAdapter):
    name = "max"

    @staticmethod
    def _string_or_none(value: Any) -> str | None:
        if value in (None, ""):
            return None
        return str(value)

    @staticmethod
    def build_envelope(update: dict[str, Any]) -> InboundEnvelope:
        message = update.get("message") or {}
        body = message.get("body") or {}
        sender = message.get("sender") or {}
        recipient = message.get("recipient") or {}
        raw_attachments = body.get("attachments") or []
        attachments: list[AttachmentRef] = []

        for index, item in enumerate(raw_attachments):
            attachment_type = item.get("type")
            payload = item.get("payload") or {}
            if attachment_type not in {"file", "document"}:
                continue

            file_name = (
                payload.get("file_name")
                or payload.get("name")
                or payload.get("title")
                or f"max_file_{index}.bin"
            )
            download_url = (
                payload.get("url")
                or payload.get("download_url")
                or payload.get("downloadUrl")
                or item.get("url")
            )
            external_id = (
                payload.get("file_id")
                or payload.get("id")
                or payload.get("token")
                or item.get("file_id")
            )
            attachments.append(
                AttachmentRef(
                    index=index,
                    file_name=file_name,
                    mime_type=payload.get("mime_type") or payload.get("content_type"),
                    external_id=str(external_id) if external_id is not None else None,
                    size_bytes=payload.get("size"),
                    download_url=download_url,
                    adapter_meta={"max_attachment_type": attachment_type, "payload": payload},
                )
            )

        sender_id = str(sender.get("user_id") or sender.get("id") or "")
        sender_name = sender.get("username") or sender.get("first_name") or sender.get("name")
        chat_id = str(recipient.get("chat_id") or recipient.get("user_id") or "")
        message_id = str(message.get("message_id") or message.get("mid") or "")

        linked = message.get("link") or {}
        reply_to_message_id = MaxBotAdapter._string_or_none(linked.get("message_id") or linked.get("mid"))

        return InboundEnvelope(
            source="max",
            event_id=f"max:{chat_id}:{message_id}",
            chat_id=chat_id,
            sender_id=sender_id,
            sender_name=sender_name,
            message_id=message_id,
            reply_to_message_id=reply_to_message_id,
            text=body.get("text"),
            attachments=tuple(attachments),
            received_at=utc_now(),
            raw_payload=update,
        )

    async def download_attachment(self, envelope: InboundEnvelope, attachment: AttachmentRef) -> DownloadedAttachment:
        if not attachment.download_url:
            raise ValueError(
                "MAX attachment does not expose download_url in webhook payload. "
                "You likely need one extra lookup step through MAX HTTP API for this attachment schema."
            )
        return DownloadedAttachment(
            original_file_name=attachment.file_name,
            mime_type=attachment.mime_type,
            remote_url=attachment.download_url,
            size_bytes=attachment.size_bytes,
        )
