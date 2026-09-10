"""Tests for VkCallbackAdapter — covers edge cases not present in test_vk_longpoll.py.

Focus areas:
  - Empty / text-only messages (no doc attachments)
  - Corrupted doc entries (missing url, missing title)
  - Title already containing extension vs title without extension
  - Negative from_id (community/group messages)
  - Deeply nested fwd_messages with recursive replies
  - download_attachment() validation
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from multichannel_gateway.adapters.vk import VkCallbackAdapter
from multichannel_gateway.core.models import AttachmentRef, InboundEnvelope
from multichannel_gateway.core.router import InboundGateway


def _wrap_message(message: dict, group_id: int = 1) -> dict:
    """Wrap a raw message dict into VK Callback update structure."""
    return {
        "type": "message_new",
        "group_id": group_id,
        "object": {"message": message},
    }


class VkAdapterTextOnlyTest(unittest.TestCase):
    """Message without any attachments should produce an envelope with zero attachments."""

    def test_text_only_message(self) -> None:
        payload = _wrap_message({
            "id": 1,
            "peer_id": 2000000001,
            "from_id": 42,
            "text": "Привет, это текстовое сообщение",
            "attachments": [],
        })
        envelope = VkCallbackAdapter.build_envelope(payload)

        self.assertEqual("vk", envelope.source)
        self.assertEqual("42", envelope.sender_id)
        self.assertEqual(0, len(envelope.attachments))

    def test_doc_attachment_contains_counter_metadata(self) -> None:
        payload = _wrap_message({
            "id": 10,
            "peer_id": 2000000001,
            "from_id": 1106569752,
            "date": 1777550440,
            "attachments": [
                {
                    "type": "doc",
                    "doc": {
                        "id": 9001,
                        "title": "work.docx",
                        "size": 123,
                        "url": "https://example.com/work.docx",
                    },
                }
            ],
        })

        envelope = VkCallbackAdapter.build_envelope(payload)

        meta = envelope.attachments[0].adapter_meta
        self.assertEqual("doc", meta["vk_attachment_type"])
        self.assertEqual("2000000001", meta["vk_peer_id"])
        self.assertEqual("1106569752", meta["vk_from_id"])
        self.assertEqual(1777550440, meta["vk_message_date"])
        self.assertEqual("10", meta["vk_message_id"])

    def test_reply_and_forwarded_docs_use_outer_message_counter_metadata(self) -> None:
        payload = _wrap_message({
            "id": 11,
            "peer_id": 2000000001,
            "from_id": 1106569752,
            "date": 1777550500,
            "attachments": [],
            "reply_message": {
                "id": 99,
                "from_id": 777,
                "date": 100,
                "attachments": [
                    {
                        "type": "doc",
                        "doc": {
                            "id": 9002,
                            "title": "reply.docx",
                            "url": "https://example.com/reply.docx",
                        },
                    }
                ],
            },
            "fwd_messages": [
                {
                    "id": 100,
                    "from_id": 888,
                    "date": 200,
                    "attachments": [
                        {
                            "type": "doc",
                            "doc": {
                                "id": 9003,
                                "title": "forward.docx",
                                "url": "https://example.com/forward.docx",
                            },
                        }
                    ],
                }
            ],
        })

        envelope = VkCallbackAdapter.build_envelope(payload)

        self.assertEqual(2, len(envelope.attachments))
        for attachment in envelope.attachments:
            self.assertEqual("2000000001", attachment.adapter_meta["vk_peer_id"])
            self.assertEqual("1106569752", attachment.adapter_meta["vk_from_id"])
            self.assertEqual(1777550500, attachment.adapter_meta["vk_message_date"])
            self.assertEqual("11", attachment.adapter_meta["vk_message_id"])

    def test_forwarded_doc_uses_forwarding_user_as_sender_and_return_peer(self) -> None:
        victoria_id = 1106569752
        group_peer_id = 2000000001
        payload = _wrap_message({
            "id": 12,
            "peer_id": group_peer_id,
            "from_id": victoria_id,
            "date": 1777550600,
            "attachments": [],
            "fwd_messages": [
                {
                    "id": 77,
                    "from_id": 999999,
                    "date": 1777550000,
                    "attachments": [
                        {
                            "type": "doc",
                            "doc": {
                                "id": 9004,
                                "title": "forwarded_from_other_user.docx",
                                "url": "https://example.com/forwarded.docx",
                            },
                        }
                    ],
                }
            ],
        })

        envelope = VkCallbackAdapter.build_envelope(payload)

        self.assertEqual(str(victoria_id), envelope.sender_id)
        self.assertEqual(str(group_peer_id), envelope.chat_id)
        self.assertEqual(1, len(envelope.attachments))
        meta = envelope.attachments[0].adapter_meta
        self.assertEqual(str(victoria_id), meta["vk_from_id"])
        self.assertEqual(str(group_peer_id), meta["vk_peer_id"])

    def test_forwarding_user_is_checked_against_allowed_authors_not_original_sender(self) -> None:
        victoria_id = 1106569752
        payload = _wrap_message({
            "id": 13,
            "peer_id": 2000000001,
            "from_id": victoria_id,
            "attachments": [],
            "fwd_messages": [
                {
                    "id": 88,
                    "from_id": 777777,
                    "attachments": [
                        {
                            "type": "doc",
                            "doc": {
                                "id": 9005,
                                "title": "forwarded.docx",
                                "url": "https://example.com/forwarded.docx",
                            },
                        }
                    ],
                }
            ],
        })
        envelope = VkCallbackAdapter.build_envelope(payload)
        gateway = InboundGateway(store=MagicMock(), file_store=MagicMock())

        with patch("config.Config.get_setting", return_value=[str(victoria_id)]):
            self.assertTrue(__import__("asyncio").run(gateway._is_sender_allowed(envelope)))

    def test_message_with_missing_attachments_key(self) -> None:
        payload = _wrap_message({
            "id": 2,
            "peer_id": 100,
            "from_id": 50,
        })
        envelope = VkCallbackAdapter.build_envelope(payload)
        self.assertEqual(0, len(envelope.attachments))


class VkAdapterCorruptedDocTest(unittest.TestCase):
    """Edge cases for document entries with missing or partial fields."""

    def test_doc_without_url(self) -> None:
        """Doc with no download URL should still create attachment — download_url=None."""
        payload = _wrap_message({
            "id": 3,
            "peer_id": 100,
            "from_id": 42,
            "attachments": [
                {"type": "doc", "doc": {"id": 999, "title": "report", "ext": "docx", "size": 100}},
            ],
        })
        envelope = VkCallbackAdapter.build_envelope(payload)
        self.assertEqual(1, len(envelope.attachments))
        self.assertIsNone(envelope.attachments[0].download_url)
        self.assertEqual("report.docx", envelope.attachments[0].file_name)

    def test_doc_without_title_uses_fallback(self) -> None:
        """Doc with no title uses fallback name vk_doc_<id>."""
        payload = _wrap_message({
            "id": 4,
            "peer_id": 100,
            "from_id": 42,
            "attachments": [
                {"type": "doc", "doc": {"id": 555, "size": 200, "url": "https://example.com/f"}},
            ],
        })
        envelope = VkCallbackAdapter.build_envelope(payload)
        self.assertEqual(1, len(envelope.attachments))
        self.assertIn("vk_doc_555", envelope.attachments[0].file_name)

    def test_empty_doc_dict(self) -> None:
        """Completely empty doc dict should not crash."""
        payload = _wrap_message({
            "id": 5,
            "peer_id": 100,
            "from_id": 42,
            "attachments": [{"type": "doc", "doc": {}}],
        })
        envelope = VkCallbackAdapter.build_envelope(payload)
        self.assertEqual(1, len(envelope.attachments))


class VkAdapterFileNameExtensionTest(unittest.TestCase):
    """Tests for file name / extension logic in _build_doc_attachment."""

    def test_title_without_extension_gets_ext_appended(self) -> None:
        """Title='357517', ext='docx' → '357517.docx'."""
        payload = _wrap_message({
            "id": 10,
            "peer_id": 100,
            "from_id": 42,
            "attachments": [
                {"type": "doc", "doc": {"id": 1, "title": "357517", "ext": "docx", "url": "https://example.com/1"}},
            ],
        })
        envelope = VkCallbackAdapter.build_envelope(payload)
        self.assertEqual("357517.docx", envelope.attachments[0].file_name)

    def test_title_with_extension_does_not_duplicate(self) -> None:
        """Title='report.docx', ext='docx' → 'report.docx' (no double ext)."""
        payload = _wrap_message({
            "id": 11,
            "peer_id": 100,
            "from_id": 42,
            "attachments": [
                {"type": "doc", "doc": {"id": 2, "title": "report.docx", "ext": "docx", "url": "https://example.com/2"}},
            ],
        })
        envelope = VkCallbackAdapter.build_envelope(payload)
        # Title already contains '.', so ext should NOT be appended
        self.assertEqual("report.docx", envelope.attachments[0].file_name)

    def test_title_with_different_extension_and_ext_field(self) -> None:
        """Title='image.jpeg', ext='png' → 'image.jpeg' (title dot prevents ext append)."""
        payload = _wrap_message({
            "id": 12,
            "peer_id": 100,
            "from_id": 42,
            "attachments": [
                {"type": "doc", "doc": {"id": 3, "title": "image.jpeg", "ext": "png", "url": "https://example.com/3"}},
            ],
        })
        envelope = VkCallbackAdapter.build_envelope(payload)
        # Current logic: if '.' in title, don't append ext
        doc = payload["object"]["message"]["attachments"][0]["doc"]
        attachment = VkCallbackAdapter._build_doc_attachment(0, doc)
        self.assertEqual("image.jpeg", attachment.file_name)
        self.assertEqual((), envelope.attachments)

    def test_no_ext_and_no_dot_in_title(self) -> None:
        """Title='document', no ext field → 'document' as-is."""
        payload = _wrap_message({
            "id": 13,
            "peer_id": 100,
            "from_id": 42,
            "attachments": [
                {"type": "doc", "doc": {"id": 4, "title": "document", "url": "https://example.com/4"}},
            ],
        })
        envelope = VkCallbackAdapter.build_envelope(payload)
        self.assertEqual("document", envelope.attachments[0].file_name)


class VkAdapterNegativeFromIdTest(unittest.TestCase):
    """Negative from_id indicates a community/group sender."""

    def test_negative_from_id_preserved_as_string(self) -> None:
        payload = _wrap_message({
            "id": 20,
            "peer_id": 2000000001,
            "from_id": -234351996,
            "attachments": [],
        })
        envelope = VkCallbackAdapter.build_envelope(payload)
        self.assertEqual("-234351996", envelope.sender_id)


class VkAdapterDeepFwdMessagesTest(unittest.TestCase):
    """Deeply nested forwarded messages — recursive doc collection."""

    def test_nested_fwd_with_reply_inside(self) -> None:
        """fwd_messages[0] has its own reply_message with a doc attachment."""
        payload = _wrap_message({
            "id": 30,
            "peer_id": 100,
            "from_id": 42,
            "attachments": [],
            "fwd_messages": [
                {
                    "id": 31,
                    "attachments": [
                        {"type": "doc", "doc": {"id": 1, "title": "fwd.docx", "size": 100, "url": "https://example.com/fwd"}},
                    ],
                    "reply_message": {
                        "id": 32,
                        "attachments": [
                            {"type": "doc", "doc": {"id": 2, "title": "nested_reply.docx", "size": 200, "url": "https://example.com/nested"}},
                        ],
                    },
                },
            ],
        })
        envelope = VkCallbackAdapter.build_envelope(payload)
        self.assertEqual(2, len(envelope.attachments))
        names = [a.file_name for a in envelope.attachments]
        self.assertIn("fwd.docx", names)
        self.assertIn("nested_reply.docx", names)

    def test_multiple_fwd_messages(self) -> None:
        """Three forwarded messages, each with one doc."""
        fwd_msgs = [
            {
                "id": 40 + i,
                "attachments": [
                    {"type": "doc", "doc": {"id": 100 + i, "title": f"file_{i}.docx", "url": f"https://example.com/{i}"}},
                ],
            }
            for i in range(3)
        ]
        payload = _wrap_message({
            "id": 40,
            "peer_id": 100,
            "from_id": 42,
            "attachments": [],
            "fwd_messages": fwd_msgs,
        })
        envelope = VkCallbackAdapter.build_envelope(payload)
        self.assertEqual(3, len(envelope.attachments))

    def test_non_doc_attachments_ignored(self) -> None:
        """photo and audio_message should be ignored — only doc collected."""
        payload = _wrap_message({
            "id": 50,
            "peer_id": 100,
            "from_id": 42,
            "attachments": [
                {"type": "photo", "photo": {"id": 1, "url": "https://example.com/photo.jpg"}},
                {"type": "audio_message", "audio_message": {"id": 2}},
                {"type": "doc", "doc": {"id": 3, "title": "real.docx", "url": "https://example.com/real"}},
                {"type": "graffiti", "graffiti": {"id": 4}},
            ],
        })
        envelope = VkCallbackAdapter.build_envelope(payload)
        self.assertEqual(1, len(envelope.attachments))
        self.assertEqual("real.docx", envelope.attachments[0].file_name)
        # Verify debug info logs all raw types
        debug = envelope.raw_payload.get("_debug", {})
        self.assertEqual(["photo", "audio_message", "doc", "graffiti"], debug.get("raw_attachment_types"))


class VkAdapterDownloadAttachmentTest(unittest.IsolatedAsyncioTestCase):
    """Tests for download_attachment()."""

    async def test_download_attachment_with_url(self) -> None:
        adapter = VkCallbackAdapter()
        envelope = InboundEnvelope(
            source="vk", event_id="vk:1:1", chat_id="1", sender_id="42",
            sender_name=None, message_id="1",
        )
        attachment = AttachmentRef(
            index=0, file_name="test.docx", download_url="https://example.com/test.docx",
        )
        result = await adapter.download_attachment(envelope, attachment)
        self.assertEqual("test.docx", result.original_file_name)
        self.assertEqual("https://example.com/test.docx", result.remote_url)

    async def test_download_attachment_without_url_raises(self) -> None:
        adapter = VkCallbackAdapter()
        envelope = InboundEnvelope(
            source="vk", event_id="vk:1:1", chat_id="1", sender_id="42",
            sender_name=None, message_id="1",
        )
        attachment = AttachmentRef(index=0, file_name="broken.docx", download_url=None)
        with self.assertRaises(ValueError):
            await adapter.download_attachment(envelope, attachment)


class VkAdapterEventIdTest(unittest.TestCase):
    """Event ID format and message_id fallback."""

    def test_event_id_uses_peer_and_message_id(self) -> None:
        payload = _wrap_message({
            "id": 77,
            "peer_id": 2000000001,
            "from_id": 42,
            "attachments": [],
        })
        envelope = VkCallbackAdapter.build_envelope(payload)
        self.assertEqual("vk:2000000001:77", envelope.event_id)

    def test_message_id_falls_back_to_conversation_message_id(self) -> None:
        payload = _wrap_message({
            "conversation_message_id": 88,
            "peer_id": 100,
            "from_id": 42,
            "attachments": [],
        })
        envelope = VkCallbackAdapter.build_envelope(payload)
        self.assertEqual("88", envelope.message_id)


if __name__ == "__main__":
    unittest.main()
