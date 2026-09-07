from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from multichannel_gateway.core.outbox import LocalOutboxSpool
from multichannel_gateway.outbound import MaxOutboundClient, OutboundDispatcher, VkOutboundClient, _get_config_setting


class OutboundTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.file_path = Path(self.temp_dir.name) / "result.pdf"
        self.file_path.write_bytes(b"pdf")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @patch("config.Config.get_setting", return_value="value-from-config")
    @patch("config.Config.load_config")
    def test_get_config_setting_loads_config_once(self, mock_load_config: Mock, mock_get_setting: Mock) -> None:
        from multichannel_gateway import outbound

        outbound._config_loaded = False
        try:
            first = _get_config_setting("vk_bot_token")
            second = _get_config_setting("vk_bot_token")
        finally:
            outbound._config_loaded = False

        self.assertEqual("value-from-config", first)
        self.assertEqual("value-from-config", second)
        mock_load_config.assert_called_once()
        self.assertEqual(2, mock_get_setting.call_count)

    @patch("multichannel_gateway.outbound._require_requests")
    def test_vk_send_message_flow(self, mock_require_requests: Mock) -> None:
        api_send = Mock()
        api_send.raise_for_status.return_value = None
        api_send.json.return_value = {"response": 77}

        http = SimpleNamespace(post=Mock(return_value=api_send))
        mock_require_requests.return_value = http

        client = VkOutboundClient(token="vk-token", api_version="5.199")
        result = client.send_message(peer_id=123, text="357445")

        self.assertEqual(77, result)
        params = http.post.call_args.kwargs["data"]
        self.assertEqual(123, params["peer_id"])
        self.assertEqual("357445", params["message"])

    @patch("multichannel_gateway.outbound._require_requests")
    def test_vk_send_document_flow(self, mock_require_requests: Mock) -> None:
        api_upload = Mock()
        api_upload.raise_for_status.return_value = None
        api_upload.json.side_effect = [
            {"response": {"upload_url": "https://upload.example/vk"}},
            {"response": [{"id": 7, "owner_id": 9}]},
            {"response": 11},
        ]

        upload_binary = Mock()
        upload_binary.raise_for_status.return_value = None
        upload_binary.json.return_value = {"file": "file_token"}

        http = SimpleNamespace(post=Mock(side_effect=[api_upload, upload_binary, api_upload, api_upload]))
        mock_require_requests.return_value = http

        client = VkOutboundClient(token="vk-token", api_version="5.199")
        result = client.send_document(peer_id=123, file_path=self.file_path, caption="hello")
        self.assertEqual(11, result)

    @patch("multichannel_gateway.outbound._require_requests")
    def test_max_send_document_flow(self, mock_require_requests: Mock) -> None:
        upload_meta = Mock()
        upload_meta.raise_for_status.return_value = None
        upload_meta.json.return_value = {"url": "https://upload.example/max"}

        upload_binary = Mock()
        upload_binary.raise_for_status.return_value = None
        upload_binary.json.return_value = {"token": "max_file_token"}

        send_message = Mock()
        send_message.raise_for_status.return_value = None
        send_message.json.return_value = {"message": {"message_id": "mid1"}}

        http = SimpleNamespace(
            post=Mock(return_value=upload_binary),
            request=Mock(side_effect=[upload_meta, send_message]),
        )
        mock_require_requests.return_value = http

        client = MaxOutboundClient(token="max-token")
        result = client.send_document(recipient_user_id="555", file_path=self.file_path, caption="done")
        self.assertIn("message", result)

    def test_dispatcher_routes_by_source(self) -> None:
        dispatcher = OutboundDispatcher()
        dispatcher.vk.send_document = Mock(return_value={"ok": "vk"})
        dispatcher.max.send_document = Mock(return_value={"ok": "max"})

        vk_result = dispatcher.send_result("vk", {"chat_id": "1"}, self.file_path, "caption")
        max_result = dispatcher.send_result("max", {"chat_id": "2", "sender_id": "9"}, self.file_path, "caption")

        self.assertEqual({"ok": "vk"}, vk_result)
        self.assertEqual({"ok": "max"}, max_result)

    @patch("multichannel_gateway.outbound.build_outbox")
    def test_dispatcher_spools_when_vk_unavailable(self, mock_build_outbox: Mock) -> None:
        spool = LocalOutboxSpool(Path(self.temp_dir.name) / "outbox")
        mock_build_outbox.return_value = spool

        dispatcher = OutboundDispatcher()
        dispatcher.vk.send_document = Mock(side_effect=RuntimeError("vk not configured"))

        result = dispatcher.send_result("vk", {"chat_id": "1"}, self.file_path, "caption", file_name="result.pdf")
        self.assertEqual("spooled", result["status"])
        entries = spool.list_entries(limit=10, pending_only=True)
        self.assertEqual(1, len(entries))
        self.assertEqual("vk", entries[0]["source_platform"])

    @patch("multichannel_gateway.outbound.build_outbox")
    def test_replay_outbox_marks_sent(self, mock_build_outbox: Mock) -> None:
        spool = LocalOutboxSpool(Path(self.temp_dir.name) / "outbox")
        mock_build_outbox.return_value = spool

        manifest = spool.spool_delivery("max", {"chat_id": "2", "sender_id": "9"}, self.file_path, "done", "result.pdf")

        dispatcher = OutboundDispatcher()
        dispatcher.max.send_document = Mock(return_value={"message": {"id": "x"}})

        results = dispatcher.replay_outbox(limit=5, source_platform="max")
        self.assertEqual("sent", results[0]["status"])
        updated = spool.get_entry(manifest["id"])
        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual("sent", updated["status"])

    def test_archive_sent_moves_manifest_and_file(self) -> None:
        spool = LocalOutboxSpool(Path(self.temp_dir.name) / "outbox")
        manifest = spool.spool_delivery("vk", {"chat_id": "1"}, self.file_path, "done", "result.pdf")
        spool.mark_sent(manifest["id"], {"ok": True})

        archived = spool.archive_sent(limit=10)
        self.assertEqual([manifest["id"]], archived)
        self.assertIsNone(spool.get_entry(manifest["id"]))

        archived_manifest = spool.archive_meta_dir / f"{manifest['id']}.json"
        self.assertTrue(archived_manifest.exists())

    def test_delete_entry_can_keep_file(self) -> None:
        spool = LocalOutboxSpool(Path(self.temp_dir.name) / "outbox")
        manifest = spool.spool_delivery("max", {"chat_id": "2"}, self.file_path, "done", "result.pdf")
        stored_file = Path(manifest["stored_file_path"])

        deleted = spool.delete_entry(manifest["id"], include_file=False)
        self.assertTrue(deleted)
        self.assertTrue(stored_file.exists())
        self.assertIsNone(spool.get_entry(manifest["id"]))


if __name__ == "__main__":
    unittest.main()
