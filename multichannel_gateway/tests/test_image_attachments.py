import asyncio
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from multichannel_gateway.tests.test_forced_author_routing import (
    base_settings, make_handlers, make_manager, settings_lookup,
)
from multichannel_gateway.adapters.vk import VkCallbackAdapter
from multichannel_gateway.core.router import InboundGateway


class TelegramImageRegressionTests(unittest.TestCase):
    def run_input(self, name, mime, mode, route, photo=False, working_hours=True):
        settings = base_settings(
            ["ordinary"] if route == "forced" else [],
            mode=mode,
            allowed_authors=["someone_else"] if route.startswith("group") else ["ordinary"],
            forced_authors_destination="editor",
            forced_authors_editor_nickname="@fixed-editor",
            telegram_group_routes={"-100123": {
                "destination": "plagiscan" if route == "group_plagiscan" else "editor",
                "editor_nickname": "@group-editor",
            }} if route.startswith("group") else {},
        )
        manager = make_manager()
        handlers = make_handlers(manager)
        self.addCleanup(handlers._safety_test_dir.cleanup)
        handlers.process_queue = AsyncMock()
        handlers.send_to_24_7_editor = AsyncMock()
        handlers.send_to_nik2 = AsyncMock()
        handlers.process_anti_file = AsyncMock()
        handlers.process_normal_file = AsyncMock()
        handlers._increment_mode2_limit_counter = MagicMock()
        handlers.is_within_working_hours = MagicMock(return_value=working_hours)
        handlers.check_daily_limit = MagicMock(return_value=True)
        handlers.get_force_author_route = MagicMock(wraps=handlers.get_force_author_route)
        before = deepcopy(handlers.files_today)
        before_mode2 = deepcopy(handlers.daily_counter)
        message = SimpleNamespace(
            id=777, chat=SimpleNamespace(id=-100123, type="supergroup" if route.startswith("group") else "private"),
            from_user=SimpleNamespace(id=42, username="ordinary"),
            document=None if photo else SimpleNamespace(file_name=name, mime_type=mime),
            photo=object() if photo else None, text=None, reply_to_message_id=None,
        )
        job = SimpleNamespace(job_id=77, file_path="/tmp/work.docx", dedupe_key="telegram:-100123:777:0")
        client = MagicMock()
        with patch("bot_handlers.Config.get_setting", side_effect=settings_lookup(settings)), patch(
            "bot_handlers.ingest_pyrogram_message", new=AsyncMock(return_value=[job])
        ) as ingest:
            asyncio.run(handlers.handle_main_account(client, message))
        if name == "work.docx" and not photo:
            ingest.assert_awaited_once_with(message)
            self.assertEqual(1, len(manager.file_queue))
            self.assertEqual(1, handlers.files_today["count"])
            handlers.process_queue.assert_awaited_once()
        else:
            ingest.assert_not_awaited()
            self.assertEqual([], manager.file_queue)
            self.assertEqual(before, handlers.files_today)
            self.assertEqual(before_mode2, handlers.daily_counter)
            manager.get_file_lock.assert_not_awaited()
            manager.set_file_status.assert_not_awaited()
            handlers.get_force_author_route.assert_not_called()
            handlers._increment_mode2_limit_counter.assert_not_called()
            client.assert_not_called()
            self.assertEqual([], client.mock_calls)
            for name in ("process_queue", "send_to_24_7_editor", "send_to_nik2", "process_anti_file", "process_normal_file"):
                getattr(handlers, name).assert_not_awaited()

    def test_image_documents_blocked_before_all_routes(self):
        images = [(f"Screenshot.{ext}", None) for ext in (
            "jpg", "JPEG", "png", "webp", "gif", "bmp", "tif", "tiff", "heic", "heif", "avif", "svg", "ico",
        )] + [("screen.png", "application/octet-stream"), ("screen.jpg", "application/pdf"),
             ("unknown.blob", "image/jpeg"), ("unknown", "image/png")]
        for mode in ("mode1", "mode2"):
            for route in ("ordinary", "forced", "group_editor", "group_plagiscan"):
                for name, mime in images:
                    with self.subTest(mode=mode, route=route, name=name, mime=mime):
                        self.run_input(name, mime, mode, route)

    def test_photo_ignored(self):
        self.run_input("photo", None, "mode1", "ordinary", photo=True)

    def test_mode2_images_do_not_reach_outside_hours_fallback(self):
        for route in ("ordinary", "forced", "group_editor", "group_plagiscan"):
            with self.subTest(route=route):
                self.run_input("screen.png", None, "mode2", route, working_hours=False)

    def test_docx_controls_keep_ingest_and_queue(self):
        for mode in ("mode1", "mode2"):
            for route in ("ordinary", "forced", "group_editor", "group_plagiscan"):
                with self.subTest(mode=mode, route=route):
                    self.run_input("work.docx", "application/octet-stream", mode, route)


class VkImageRegressionTests(unittest.TestCase):
    def test_adapter_to_gateway_filters_images_and_keeps_docx(self):
        for doc in (
            {"title": "screen.jpg"}, {"title": "screen.png", "mime_type": "application/octet-stream"},
            {"title": "unknown.blob", "mime_type": "image/png"}, {"title": "screen", "ext": "jpg"},
            {"title": "misleading.docx", "ext": "png"}, {"title": "work.docx"},
        ):
            for location in ("direct", "reply_message", "fwd_messages"):
                with self.subTest(doc=doc, location=location):
                    nested = {"attachments": [{"type": "doc", "doc": doc}]}
                    message = {"id": 1, "peer_id": 42, "from_id": 42}
                    if location == "direct":
                        message.update(nested)
                    elif location == "reply_message":
                        message[location] = nested
                    else:
                        message[location] = [nested]
                    envelope = VkCallbackAdapter.build_envelope({"object": {"message": message}})
                    store, file_store = MagicMock(), MagicMock()
                    adapter = VkCallbackAdapter()
                    adapter.download_attachment = AsyncMock()
                    gateway = InboundGateway(store, file_store)
                    gateway._is_sender_allowed = AsyncMock(return_value=True)
                    gateway._fetch_vk_sender_name = MagicMock(return_value=None)
                    store.get_by_dedupe_key.return_value = None
                    jobs = asyncio.run(gateway.ingest(adapter, envelope))
                    if doc == {"title": "work.docx"}:
                        self.assertEqual(1, len(jobs))
                        adapter.download_attachment.assert_awaited_once()
                        file_store.persist_downloaded_attachment.assert_called_once()
                        store.enqueue_file.assert_called_once()
                        self.assertEqual("work.docx", store.enqueue_file.call_args.args[1].file_name)
                    else:
                        self.assertEqual((), envelope.attachments)
                        self.assertEqual([], jobs)
                        adapter.download_attachment.assert_not_awaited()
                        self.assertEqual([], file_store.mock_calls)
                        self.assertEqual([], store.mock_calls)
