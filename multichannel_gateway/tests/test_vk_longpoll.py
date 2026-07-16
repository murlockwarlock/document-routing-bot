from __future__ import annotations

import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from multichannel_gateway.vk_longpoll import (
    VkLongPollClient,
    VkLongPollConfigurationError,
    VkLongPollRunner,
    VkLongPollSettings,
)
from multichannel_gateway.adapters.vk import VkCallbackAdapter


class VkLongPollSettingsTests(unittest.TestCase):
    @patch.dict(os.environ, {}, clear=True)
    @patch("multichannel_gateway.vk_longpoll._get_config_setting")
    def test_settings_fall_back_to_vk_bot_token_and_config(self, mock_get_config_setting: Mock) -> None:
        config_values = {
            "vk_longpoll_token": None,
            "vk_bot_token": "config-vk-token",
            "vk_group_id": 321,
            "vk_longpoll_wait": 25,
            "vk_api_version": "5.199",
        }
        mock_get_config_setting.side_effect = lambda key, default=None: config_values.get(key, default)

        settings = VkLongPollSettings.from_sources()

        self.assertEqual("config-vk-token", settings.token)
        self.assertEqual(321, settings.group_id)
        self.assertEqual(25, settings.wait_seconds)
        self.assertEqual("5.199", settings.api_version)

    @patch.dict(os.environ, {"VK_GROUP_ID": "oops"}, clear=True)
    def test_settings_reject_invalid_group_id(self) -> None:
        with self.assertRaises(VkLongPollConfigurationError):
            VkLongPollSettings.from_sources()


class VkLongPollClientTests(unittest.TestCase):
    @patch("multichannel_gateway.vk_longpoll._require_requests")
    def test_poll_once_refreshes_server_and_normalizes_updates(self, mock_require_requests: Mock) -> None:
        api_response = Mock()
        api_response.raise_for_status.return_value = None
        api_response.json.return_value = {
            "response": {"server": "https://lp.vk.example", "key": "secret", "ts": "11"}
        }

        poll_response = Mock()
        poll_response.raise_for_status.return_value = None
        poll_response.json.return_value = {
            "ts": "12",
            "updates": [
                {
                    "type": "message_new",
                    "group_id": 1,
                    "object": {"message": {"id": 101, "peer_id": 2000000001, "from_id": 42, "attachments": []}},
                }
            ],
        }

        http = SimpleNamespace(post=Mock(return_value=api_response), get=Mock(return_value=poll_response))
        mock_require_requests.return_value = http

        client = VkLongPollClient(VkLongPollSettings(token="token", group_id=1, api_version="5.199", wait_seconds=25))
        events = client.poll_once()

        self.assertEqual(1, len(events))
        self.assertEqual("message_new", events[0]["type"])
        self.assertEqual("12", client.ts)

    @patch("multichannel_gateway.vk_longpoll._require_requests")
    def test_poll_once_updates_ts_when_vk_returns_failed_1(self, mock_require_requests: Mock) -> None:
        poll_response = Mock()
        poll_response.raise_for_status.return_value = None
        poll_response.json.return_value = {"failed": 1, "ts": "222"}

        http = SimpleNamespace(post=Mock(), get=Mock(return_value=poll_response))
        mock_require_requests.return_value = http

        client = VkLongPollClient(VkLongPollSettings(token="token", group_id=1, api_version="5.199", wait_seconds=25))
        client.server = "https://lp.vk.example"
        client.key = "secret"
        client.ts = "111"

        events = client.poll_once()

        self.assertEqual([], events)
        self.assertEqual("222", client.ts)

    @patch("multichannel_gateway.vk_longpoll._require_requests")
    def test_poll_once_refreshes_server_on_failed_2(self, mock_require_requests: Mock) -> None:
        """failed=2 (key expired) should call refresh_server()."""
        poll_response = Mock()
        poll_response.raise_for_status.return_value = None
        poll_response.json.return_value = {"failed": 2}

        # refresh_server needs an API response
        api_response = Mock()
        api_response.raise_for_status.return_value = None
        api_response.json.return_value = {
            "response": {"server": "https://lp.vk.example/new", "key": "new_key", "ts": "500"}
        }

        http = SimpleNamespace(
            post=Mock(return_value=api_response),
            get=Mock(return_value=poll_response),
        )
        mock_require_requests.return_value = http

        client = VkLongPollClient(VkLongPollSettings(token="token", group_id=1, api_version="5.199", wait_seconds=25))
        client.server = "https://lp.vk.example/old"
        client.key = "old_key"
        client.ts = "100"

        events = client.poll_once()

        self.assertEqual([], events)
        # After failed=2, refresh_server is called → new server info
        self.assertEqual("https://lp.vk.example/new", client.server)
        self.assertEqual("new_key", client.key)
        self.assertEqual("500", client.ts)

    @patch("multichannel_gateway.vk_longpoll._require_requests")
    def test_poll_once_refreshes_server_on_failed_3(self, mock_require_requests: Mock) -> None:
        """failed=3 (ts/key lost) should also call refresh_server()."""
        poll_response = Mock()
        poll_response.raise_for_status.return_value = None
        poll_response.json.return_value = {"failed": 3}

        api_response = Mock()
        api_response.raise_for_status.return_value = None
        api_response.json.return_value = {
            "response": {"server": "https://lp.vk.example/fresh", "key": "fresh_key", "ts": "999"}
        }

        http = SimpleNamespace(
            post=Mock(return_value=api_response),
            get=Mock(return_value=poll_response),
        )
        mock_require_requests.return_value = http

        client = VkLongPollClient(VkLongPollSettings(token="token", group_id=1, api_version="5.199", wait_seconds=25))
        client.server = "https://old"
        client.key = "old"
        client.ts = "1"

        events = client.poll_once()

        self.assertEqual([], events)
        self.assertEqual("999", client.ts)

    @patch("multichannel_gateway.vk_longpoll._require_requests")
    def test_poll_once_raises_on_unsupported_failed_code(self, mock_require_requests: Mock) -> None:
        """failed=4+ should raise VkLongPollError."""
        from multichannel_gateway.vk_longpoll import VkLongPollError

        poll_response = Mock()
        poll_response.raise_for_status.return_value = None
        poll_response.json.return_value = {"failed": 4}

        http = SimpleNamespace(post=Mock(), get=Mock(return_value=poll_response))
        mock_require_requests.return_value = http

        client = VkLongPollClient(VkLongPollSettings(token="token", group_id=1, api_version="5.199", wait_seconds=25))
        client.server = "https://lp.vk.example"
        client.key = "key"
        client.ts = "1"

        with self.assertRaises(VkLongPollError):
            client.poll_once()


class VkLongPollSettingsExtendedTests(unittest.TestCase):
    @patch.dict(os.environ, {"VK_LONGPOLL_TOKEN": "env-token", "VK_GROUP_ID": "777"}, clear=True)
    def test_env_variables_override_config(self) -> None:
        """Environment variables should take priority over config."""
        settings = VkLongPollSettings.from_sources()
        self.assertEqual("env-token", settings.token)
        self.assertEqual(777, settings.group_id)

    def test_missing_fields_reports_token_and_group_id(self) -> None:
        settings = VkLongPollSettings(token=None, group_id=None, api_version="5.199", wait_seconds=25)
        missing = settings.missing_fields()
        self.assertEqual(2, len(missing))
        self.assertTrue(any("TOKEN" in f for f in missing))
        self.assertTrue(any("GROUP_ID" in f for f in missing))

    def test_missing_fields_empty_when_configured(self) -> None:
        settings = VkLongPollSettings(token="tok", group_id=1, api_version="5.199", wait_seconds=25)
        self.assertEqual([], settings.missing_fields())

    @patch.dict(os.environ, {"VK_LONGPOLL_WAIT": "0"}, clear=True)
    def test_wait_seconds_below_minimum_raises(self) -> None:
        with self.assertRaises(VkLongPollConfigurationError):
            VkLongPollSettings.from_sources()

class VkLongPollRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_runner_processes_only_message_new(self) -> None:
        client = Mock()
        client.poll_once = Mock(
            return_value=[
                {"type": "message_new", "group_id": 1, "object": {"message": {"id": 1, "peer_id": 2, "from_id": 3, "attachments": []}}},
                {"type": "message_event", "group_id": 1, "object": {}},
            ]
        )
        gateway = Mock()
        gateway.ingest = AsyncMock(return_value=["job-1"])

        runner = VkLongPollRunner(client=client, gateway=gateway)
        jobs = await runner.run_once()

        self.assertEqual(["job-1"], jobs)
        gateway.ingest.assert_awaited_once()

    async def test_runner_skips_messages_older_than_start(self) -> None:
        client = Mock()
        gateway = Mock()
        gateway.ingest = AsyncMock(return_value=["job-1"])
        runner = VkLongPollRunner(client=client, gateway=gateway)
        runner.started_at_epoch = int(time.time())

        jobs = await runner.process_event(
            {
                "type": "message_new",
                "group_id": 1,
                "object": {
                    "message": {
                        "id": 1,
                        "peer_id": 2,
                        "from_id": 3,
                        "date": runner.started_at_epoch - 60,
                        "attachments": [{"type": "doc", "doc": {"title": "old.docx", "url": "https://example.com/old.docx"}}],
                    }
                },
            }
        )

        self.assertEqual([], jobs)
        gateway.ingest.assert_not_awaited()


class VkCallbackAdapterTests(unittest.TestCase):
    def test_build_envelope_collects_multiple_docs_from_same_vk_message(self) -> None:
        payload = {
            "type": "message_new",
            "group_id": 1,
            "object": {
                "message": {
                    "id": 23,
                    "peer_id": 2000000001,
                    "from_id": 1106569752,
                    "attachments": [
                        {
                            "type": "doc",
                            "doc": {
                                "id": 1,
                                "title": "357517",
                                "ext": "docx",
                                "size": 100,
                                "url": "https://example.com/357517.docx",
                            },
                        },
                        {
                            "type": "doc",
                            "doc": {
                                "id": 2,
                                "title": "357521.pdf",
                                "size": 200,
                                "url": "https://example.com/357521.pdf",
                            },
                        },
                    ],
                }
            },
        }

        envelope = VkCallbackAdapter.build_envelope(payload)

        self.assertEqual(2, len(envelope.attachments))
        self.assertEqual("357517.docx", envelope.attachments[0].file_name)
        self.assertEqual("357521.pdf", envelope.attachments[1].file_name)

    def test_build_envelope_collects_docs_from_reply_and_forwarded_messages(self) -> None:
        payload = {
            "type": "message_new",
            "group_id": 1,
            "object": {
                "message": {
                    "id": 101,
                    "peer_id": 2000000001,
                    "from_id": 42,
                    "attachments": [],
                    "reply_message": {
                        "id": 102,
                        "attachments": [
                            {
                                "type": "doc",
                                "doc": {
                                    "id": 9001,
                                    "title": "reply-file",
                                    "ext": "docx",
                                    "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                    "size": 123,
                                    "url": "https://example.com/reply.docx",
                                },
                            }
                        ],
                    },
                    "fwd_messages": [
                        {
                            "id": 103,
                            "attachments": [
                                {
                                    "type": "doc",
                                    "doc": {
                                        "id": 9002,
                                        "title": "forward-file.docx",
                                        "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                        "size": 456,
                                        "url": "https://example.com/forward.docx",
                                    },
                                }
                            ],
                        }
                    ],
                }
            },
        }

        envelope = VkCallbackAdapter.build_envelope(payload)

        self.assertEqual("2000000001", envelope.chat_id)
        self.assertEqual("42", envelope.sender_id)
        self.assertEqual(2, len(envelope.attachments))
        self.assertEqual("reply-file.docx", envelope.attachments[0].file_name)
        self.assertEqual("https://example.com/reply.docx", envelope.attachments[0].download_url)
        self.assertEqual("forward-file.docx", envelope.attachments[1].file_name)
        self.assertEqual("https://example.com/forward.docx", envelope.attachments[1].download_url)


if __name__ == "__main__":
    unittest.main()
