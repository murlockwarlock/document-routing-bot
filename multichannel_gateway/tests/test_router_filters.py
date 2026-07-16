from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from multichannel_gateway.core.models import InboundEnvelope
from multichannel_gateway.core.router import InboundGateway


class RouterAuthorFilterTests(unittest.TestCase):
    def test_vk_sender_allowed_by_bare_numeric_id(self) -> None:
        envelope = InboundEnvelope(
            source="vk",
            event_id="vk:1:1",
            chat_id="1",
            sender_id="123456",
            sender_name=None,
            message_id="1",
        )
        gateway = InboundGateway(store=None, file_store=None)  # type: ignore[arg-type]
        with patch("config.Config.get_setting", return_value=["123456"]):
            self.assertTrue(asyncio.run(gateway._is_sender_allowed(envelope)))

    def test_vk_sender_allowed_by_prefixed_numeric_id(self) -> None:
        envelope = InboundEnvelope(
            source="vk",
            event_id="vk:1:1",
            chat_id="1",
            sender_id="123456",
            sender_name=None,
            message_id="1",
        )
        gateway = InboundGateway(store=None, file_store=None)  # type: ignore[arg-type]
        with patch("config.Config.get_setting", return_value=["vk:123456"]):
            self.assertTrue(asyncio.run(gateway._is_sender_allowed(envelope)))

    def test_vk_group_sender_allowed_with_or_without_minus(self) -> None:
        envelope = InboundEnvelope(
            source="vk",
            event_id="vk:1:1",
            chat_id="1",
            sender_id="-234351996",
            sender_name=None,
            message_id="1",
        )
        gateway = InboundGateway(store=None, file_store=None)  # type: ignore[arg-type]

        with patch("config.Config.get_setting", return_value=["234351996"]):
            self.assertTrue(asyncio.run(gateway._is_sender_allowed(envelope)))

        envelope = InboundEnvelope(
            source="vk",
            event_id="vk:1:2",
            chat_id="1",
            sender_id="234351996",
            sender_name=None,
            message_id="2",
        )
        with patch("config.Config.get_setting", return_value=["vk:-234351996"]):
            self.assertTrue(asyncio.run(gateway._is_sender_allowed(envelope)))

    def test_vk_sender_allowed_by_screen_name(self) -> None:
        envelope = InboundEnvelope(
            source="vk",
            event_id="vk:1:1",
            chat_id="1",
            sender_id="123456",
            sender_name=None,
            message_id="1",
        )
        gateway = InboundGateway(store=None, file_store=None)  # type: ignore[arg-type]
        with (
            patch("config.Config.get_setting", return_value=["username_example"]),
            patch.object(gateway, "_fetch_vk_sender_name", return_value="username_example"),
        ):
            self.assertTrue(asyncio.run(gateway._is_sender_allowed(envelope)))

    def test_vk_sender_allowed_by_vk_url(self) -> None:
        envelope = InboundEnvelope(
            source="vk",
            event_id="vk:1:1",
            chat_id="1",
            sender_id="123456",
            sender_name="username_example",
            message_id="1",
        )
        gateway = InboundGateway(store=None, file_store=None)  # type: ignore[arg-type]
        with patch("config.Config.get_setting", return_value=["https://vk.com/username_example"]):
            self.assertTrue(asyncio.run(gateway._is_sender_allowed(envelope)))

    def test_vk_sender_rejected_when_not_listed(self) -> None:
        envelope = InboundEnvelope(
            source="vk",
            event_id="vk:1:1",
            chat_id="1",
            sender_id="123456",
            sender_name=None,
            message_id="1",
        )
        gateway = InboundGateway(store=None, file_store=None)  # type: ignore[arg-type]
        with patch("config.Config.get_setting", return_value=["999999"]):
            self.assertFalse(asyncio.run(gateway._is_sender_allowed(envelope)))

    def test_telegram_sender_allowed_with_or_without_at_prefix(self) -> None:
        envelope = InboundEnvelope(
            source="telegram",
            event_id="tg:1:1",
            chat_id="1",
            sender_id="42",
            sender_name="manager_name",
            message_id="1",
        )
        gateway = InboundGateway(store=None, file_store=None)  # type: ignore[arg-type]
        with patch("config.Config.get_setting", return_value=["@manager_name"]):
            self.assertTrue(asyncio.run(gateway._is_sender_allowed(envelope)))
        with patch("config.Config.get_setting", return_value=["manager_name"]):
            self.assertTrue(asyncio.run(gateway._is_sender_allowed(envelope)))

    def test_vk_group_chat_filters_by_sender_not_peer(self) -> None:
        envelope = InboundEnvelope(
            source="vk",
            event_id="vk:2000000001:23",
            chat_id="2000000001",
            sender_id="1106569752",
            sender_name="allowed_manager",
            message_id="23",
        )
        gateway = InboundGateway(store=None, file_store=None)  # type: ignore[arg-type]
        with patch("config.Config.get_setting", return_value=["1106569752"]):
            self.assertTrue(asyncio.run(gateway._is_sender_allowed(envelope)))
        with patch("config.Config.get_setting", return_value=["2000000001"]):
            self.assertFalse(asyncio.run(gateway._is_sender_allowed(envelope)))


if __name__ == "__main__":
    unittest.main()
