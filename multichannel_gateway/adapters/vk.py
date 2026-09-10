from __future__ import annotations

from typing import Any

from multichannel_gateway.core.attachment_filters import is_image_attachment
from multichannel_gateway.core.models import AttachmentRef, DownloadedAttachment, InboundEnvelope, utc_now

from .base import TransportAdapter


class VkCallbackAdapter(TransportAdapter):
    name = "vk"

    @staticmethod
    def _string_or_none(value: Any) -> str | None:
        if value in (None, ""):
            return None
        return str(value)

    @staticmethod
    def _build_doc_attachment(index: int, doc: dict[str, Any], message: dict[str, Any] | None = None) -> AttachmentRef:
        message = message or {}
        file_name = doc.get("title") or f"vk_doc_{doc.get('id', index)}"
        ext = doc.get("ext")
        if ext and "." not in file_name:
            file_name = f"{file_name}.{ext}"
        return AttachmentRef(
            index=index,
            file_name=file_name,
            mime_type=doc.get("mime_type"),
            external_id=str(doc.get("id") or ""),
            size_bytes=doc.get("size"),
            download_url=doc.get("url"),
            adapter_meta={
                "vk_attachment_type": "doc",
                "vk_peer_id": str(message.get("peer_id") or ""),
                "vk_from_id": str(message.get("from_id") or ""),
                "vk_message_date": message.get("date"),
                "vk_message_id": str(message.get("id") or message.get("conversation_message_id") or ""),
            },
        )

    @classmethod
    def _collect_doc_attachments(
        cls,
        message: dict[str, Any],
        attachments: list[AttachmentRef],
        start_index: int = 0,
        counter_message: dict[str, Any] | None = None,
    ) -> int:
        next_index = start_index
        counter_message = counter_message or message

        for item in message.get("attachments") or []:
            if item.get("type") != "doc":
                continue
            doc = item.get("doc") or {}
            attachment = cls._build_doc_attachment(next_index, doc, counter_message)
            next_index += 1
            if is_image_attachment(attachment.file_name, attachment.mime_type) or is_image_attachment(
                f"attachment.{doc.get('ext') or ''}"
            ):
                continue
            attachments.append(attachment)

        reply_message = message.get("reply_message") or {}
        if reply_message:
            next_index = cls._collect_doc_attachments(reply_message, attachments, next_index, counter_message)

        for forwarded in message.get("fwd_messages") or []:
            next_index = cls._collect_doc_attachments(forwarded or {}, attachments, next_index, counter_message)

        return next_index

    @classmethod
    def build_envelope(cls, update: dict[str, Any]) -> InboundEnvelope:
        message = (update.get("object") or {}).get("message") or {}
        sender_id = str(message.get("from_id") or "")
        peer_id = str(message.get("peer_id") or "")
        message_id_value = (
            message.get("id")
            or message.get("conversation_message_id")
            or message.get("cmid")
            or message.get("date")
            or ""
        )
        message_id = str(message_id_value)
        attachments: list[AttachmentRef] = []
        cls._collect_doc_attachments(message, attachments)
        raw_attachment_types = [str(item.get("type") or "") for item in (message.get("attachments") or [])]

        return InboundEnvelope(
            source="vk",
            event_id=f"vk:{peer_id}:{message_id}",
            chat_id=peer_id,
            sender_id=sender_id,
            sender_name=None,
            message_id=message_id,
            reply_to_message_id=cls._string_or_none((message.get("reply_message") or {}).get("id")),
            text=message.get("text"),
            attachments=tuple(attachments),
            received_at=utc_now(),
            raw_payload={
                **update,
                "_debug": {
                    "raw_attachment_types": raw_attachment_types,
                    "has_reply_message": bool(message.get("reply_message")),
                    "forwarded_count": len(message.get("fwd_messages") or []),
                },
            },
        )

    async def download_attachment(self, envelope: InboundEnvelope, attachment: AttachmentRef) -> DownloadedAttachment:
        if not attachment.download_url:
            raise ValueError("VK attachment does not contain a direct download URL")
        return DownloadedAttachment(
            original_file_name=attachment.file_name,
            mime_type=attachment.mime_type,
            remote_url=attachment.download_url,
            size_bytes=attachment.size_bytes,
        )
