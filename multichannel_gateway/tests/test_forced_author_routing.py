from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

for module_name in (
    "pyrogram",
    "pyrogram.types",
    "pyrogram.errors",
    "pyrogram.enums",
    "fitz",
    "requests",
):
    if module_name not in sys.modules:
        sys.modules[module_name] = MagicMock()


class _FakeFloodWait(Exception):
    def __init__(self, value=30):
        self.value = value
        super().__init__(f"FloodWait: {value}")


sys.modules["pyrogram.errors"].FloodWait = _FakeFloodWait

from bot_handlers import BotHandlers
from config import AccountManager, Config


def make_manager():
    manager = MagicMock()
    manager.get_client.return_value = None
    manager.set_file_status = AsyncMock()
    manager.get_file_lock = AsyncMock(return_value=asyncio.Lock())
    manager.get_file_status = AsyncMock(return_value="NEW")
    manager.file_queue = []
    manager.processing_files = {}
    manager.blacklisted_accounts = []
    return manager


def make_handlers(manager=None):
    return BotHandlers(manager or make_manager())


def settings_lookup(settings):
    return lambda key, default=None: settings.get(key, default)


def base_settings(users, routes=None, **overrides):
    settings = {
        "mode": "mode1",
        "max_concurrent": 7,
        "normal_destination": "бот",
        "anti_destination": "бот",
        "anti_bot": "@plagiscan_bot",
        "ai_bot": "@AAA_Report_AIBot",
        "editor_nickname": "@legacy-editor",
        "editor_24_7": "@editor-247",
        "plagiscan_users": users,
        "plagiscan_user_routes": routes or {},
    }
    settings.update(overrides)
    return settings


def telegram_file_info(author, author_id, file_name, *, chat_id=None, message_id=None):
    chat_id = author_id if chat_id is None else chat_id
    message_id = 1 if message_id is None else message_id
    return {
        "author": author,
        "author_id": author_id,
        "file_name": file_name,
        "original_file_name": file_name,
        "message_id": message_id,
        "chat_id": chat_id,
        "route_chat_id": chat_id,
        "route_message_id": message_id,
        "route_is_group": chat_id != author_id,
        "message": None,
        "is_anti": False,
        "force_plagiscan": True,
        "source_platform": "telegram",
        "file_uid": f"telegram:{chat_id}:{message_id}:0",
        "local_path": None,
        "gateway_job_id": None,
    }


class TestForcedAuthorRouting(unittest.TestCase):
    def test_old_config_loads_and_round_trips_without_manual_new_section(self):
        old_accounts = Config._ACCOUNTS
        old_settings = Config._SETTINGS
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "config.json"
                config_path.write_text(
                    json.dumps(
                        {
                            "accounts": {
                                "НИК-1": {"api_id": None, "api_hash": None, "phone": None}
                            },
                            "settings": {"plagiscan_users": ["@legacy"]},
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                with patch.object(Config, "CONFIG_FILE", config_path):
                    Config.load_config()
                    self.assertEqual(["@legacy"], Config.get_setting("plagiscan_users"))
                    self.assertEqual({}, Config.get_setting("plagiscan_user_routes"))
                    Config.save_config()
                    saved = json.loads(config_path.read_text(encoding="utf-8"))
                    self.assertEqual({}, saved["settings"]["plagiscan_user_routes"])
        finally:
            Config._ACCOUNTS = old_accounts
            Config._SETTINGS = old_settings

    def test_console_route_menu_saves_plagiscan_and_editor_choices(self):
        old_settings = Config._SETTINGS
        try:
            Config._SETTINGS = {"plagiscan_user_routes": {}}
            with patch("builtins.input", side_effect=["1", "2", "editor.one"]), patch.object(
                Config, "update_setting", wraps=Config.update_setting
            ) as update_setting:
                Config.setup_plagiscan_user_routes_interactive(["@one", "vk:2"])

            self.assertEqual(
                {
                    "@one": {"destination": "plagiscan"},
                    "vk:2": {"destination": "editor", "editor_nickname": "@editor.one"},
                },
                Config.get_setting("plagiscan_user_routes"),
            )
            update_setting.assert_called_once_with(
                "plagiscan_user_routes",
                {
                    "@one": {"destination": "plagiscan"},
                    "vk:2": {"destination": "editor", "editor_nickname": "@editor.one"},
                },
            )
        finally:
            Config._SETTINGS = old_settings

    def test_route_resolution_reuses_force_user_matching_for_telegram_and_vk(self):
        handlers = make_handlers()
        settings = base_settings(
            ["@tg-author", "vk:700", "@plagiscan-only"],
            {
                "@tg-author": {"destination": "editor", "editor_nickname": "@editor-tg"},
                "vk:700": {"destination": "editor", "editor_nickname": "@editor-vk"},
            },
        )
        with patch("bot_handlers.Config.get_setting", side_effect=settings_lookup(settings)):
            self.assertEqual(
                {"destination": "editor", "editor_nickname": "@editor-tg"},
                handlers.get_force_author_route({"author": "tg-author", "author_id": 42}),
            )
            self.assertEqual(
                {"destination": "editor", "editor_nickname": "@editor-vk"},
                handlers.get_force_author_route(
                    {"author": "VK Chat", "route_sender_id": "700", "route_sender_name": "vk-name"}
                ),
            )
            self.assertEqual(
                {"destination": "plagiscan"},
                handlers.get_force_author_route({"author": "plagiscan-only"}),
            )
            self.assertIsNone(handlers.get_force_author_route({"author": "ordinary"}))

    def test_force_author_numeric_telegram_id_is_allowed_with_username(self):
        handlers = make_handlers()
        settings = base_settings(
            ["987654321"],
            {"987654321": {"destination": "editor", "editor_nickname": "@editor"}},
            allowed_authors=["someone_else"],
        )
        with patch("bot_handlers.Config.get_setting", side_effect=settings_lookup(settings)):
            self.assertTrue(handlers.is_author_allowed("named-user", 987654321))

    def test_fixed_editor_route_is_used_in_mode2_without_247_or_plagiscan_fallback(self):
        manager = make_manager()
        editor = MagicMock()
        editor.send_document = AsyncMock(
            return_value=SimpleNamespace(id=901, chat=SimpleNamespace(id=300))
        )
        manager.get_client.side_effect = lambda name: editor if name == "НИК-2" else None
        handlers = make_handlers(manager)
        input_path = Path(tempfile.gettempdir()) / "forced-route-input.docx"
        input_path.write_bytes(b"source")
        handlers._ensure_work_file = AsyncMock(return_value=str(input_path))
        handlers.process_anti_file = AsyncMock()
        handlers.send_to_24_7_editor = AsyncMock()
        handlers.manager.file_queue = [telegram_file_info("forced", 42, "work.docx")]
        settings = base_settings(
            ["forced"],
            {"forced": {"destination": "editor", "editor_nickname": "@fixed-editor"}},
            mode="mode2",
        )

        with patch("bot_handlers.Config.get_setting", side_effect=settings_lookup(settings)):
            asyncio.run(handlers.process_queue())

        kwargs = editor.send_document.await_args.kwargs
        self.assertEqual("@fixed-editor", kwargs["chat_id"])
        self.assertEqual("work.docx", kwargs["file_name"])
        handlers.process_anti_file.assert_not_awaited()
        handlers.send_to_24_7_editor.assert_not_awaited()
        self.assertEqual("@fixed-editor", next(iter(handlers.editor_tracking.values()))["destination"])

    def test_fixed_route_is_same_for_vk_and_telegram_and_explicit_plagiscan_keeps_old_flow(self):
        manager = make_manager()
        editor = MagicMock()
        editor.send_document = AsyncMock(
            side_effect=[
                SimpleNamespace(id=910, chat=SimpleNamespace(id=301)),
                SimpleNamespace(id=911, chat=SimpleNamespace(id=302)),
            ]
        )
        manager.get_client.side_effect = lambda name: editor if name == "НИК-2" else None
        handlers = make_handlers(manager)
        input_path = Path(tempfile.gettempdir()) / "mixed-route-input.docx"

        async def ensure_input(*_args, **_kwargs):
            input_path.write_bytes(b"source")
            return str(input_path)

        handlers._ensure_work_file = AsyncMock(side_effect=ensure_input)
        handlers.manager.file_queue = [
            telegram_file_info("tg-author", 10, "tg.docx"),
            {
                **telegram_file_info("VK Chat", 700, "vk.docx"),
                "source_platform": "vk",
                "route_sender_id": "700",
                "route_sender_name": "vk-name",
                "route_chat_id": "2000000001",
                "route_is_group": False,
            },
        ]
        settings = base_settings(
            ["tg-author", "vk:700", "plagiscan-only"],
            {
                "tg-author": {"destination": "editor", "editor_nickname": "@editor-tg"},
                "vk:700": {"destination": "editor", "editor_nickname": "@editor-vk"},
                "plagiscan-only": {"destination": "plagiscan"},
            },
        )

        with patch("bot_handlers.Config.get_setting", side_effect=settings_lookup(settings)):
            asyncio.run(handlers.process_queue())
            asyncio.run(handlers.process_queue())

        destinations = [call.kwargs["chat_id"] for call in editor.send_document.await_args_list]
        self.assertEqual(["@editor-tg", "@editor-vk"], destinations)

        plagiscan_handlers = make_handlers()
        plagiscan_handlers.process_anti_file = AsyncMock()
        plagiscan_handlers.manager.file_queue = [telegram_file_info("plagiscan-only", 11, "legacy.docx")]
        with patch("bot_handlers.Config.get_setting", side_effect=settings_lookup(settings)):
            asyncio.run(plagiscan_handlers.process_queue())
        plagiscan_handlers.process_anti_file.assert_awaited_once()

    def test_telegram_group_input_keeps_origin_chat_and_message_context(self):
        manager = make_manager()
        handlers = make_handlers(manager)
        handlers.process_queue = AsyncMock()
        message = SimpleNamespace(
            id=777,
            chat=SimpleNamespace(id=-100123, type="supergroup"),
            from_user=SimpleNamespace(id=42, username="author"),
            document=SimpleNamespace(file_name="paper.docx"),
            text=None,
            reply_to_message_id=None,
        )
        ingest_job = SimpleNamespace(
            job_id=77,
            file_path="/tmp/paper.docx",
            dedupe_key="telegram:-100123:777:0",
        )
        settings = base_settings([], {}, allowed_authors=["author"])

        with patch("bot_handlers.Config.get_setting", side_effect=settings_lookup(settings)), patch(
            "bot_handlers.ingest_pyrogram_message", new=AsyncMock(return_value=[ingest_job])
        ):
            asyncio.run(handlers.handle_main_account(MagicMock(), message))

        queued = manager.file_queue[0]
        self.assertEqual(-100123, queued["route_chat_id"])
        self.assertEqual(777, queued["route_message_id"])
        self.assertTrue(queued["route_is_group"])
        self.assertEqual(42, queued["author_id"])
        self.assertEqual(1, handlers.files_today["count"])

    def test_payment_filter_is_identical_in_private_and_group_messages(self):
        settings = base_settings([], {}, allowed_authors=["author"])
        for chat_id, chat_type in ((42, "private"), (-100123, "group")):
            manager = make_manager()
            handlers = make_handlers(manager)
            message = SimpleNamespace(
                id=chat_id + 1,
                chat=SimpleNamespace(id=chat_id, type=chat_type),
                from_user=SimpleNamespace(id=42, username="author"),
                document=SimpleNamespace(file_name="payment_receipt.docx"),
                text=None,
                reply_to_message_id=None,
            )
            with patch("bot_handlers.Config.get_setting", side_effect=settings_lookup(settings)), patch(
                "bot_handlers.ingest_pyrogram_message", new=AsyncMock()
            ) as ingest:
                asyncio.run(handlers.handle_main_account(MagicMock(), message))
            self.assertEqual([], manager.file_queue)
            ingest.assert_not_awaited()


class TestEditorReportsForFixedRoute(unittest.IsolatedAsyncioTestCase):
    async def _make_handlers_with_tasks(self, tasks, sent_messages, settings):
        manager = make_manager()
        nik2 = MagicMock()
        nik2.send_document = AsyncMock(side_effect=sent_messages)
        nik1 = MagicMock()
        nik1.name = "НИК-1"
        nik1.is_connected = True
        nik1.sent_contents = []

        async def capture_document(**kwargs):
            nik1.sent_contents.append(Path(kwargs["document"]).read_bytes())
            return SimpleNamespace(id=7000)

        nik1.send_document = AsyncMock(side_effect=capture_document)
        manager.get_client.side_effect = lambda name: {"НИК-1": nik1, "НИК-2": nik2}.get(name)
        handlers = make_handlers(manager)
        input_path = Path(tempfile.gettempdir()) / "fixed-editor-input.docx"

        async def ensure_input(*_args, **_kwargs):
            input_path.write_bytes(b"source")
            return str(input_path)

        handlers._ensure_work_file = AsyncMock(side_effect=ensure_input)
        handlers.processor.crop_pdf = MagicMock(side_effect=AssertionError("editor result was cropped"))
        manager.file_queue = tasks
        task_count = len(tasks)
        with patch("bot_handlers.Config.get_setting", side_effect=settings_lookup(settings)):
            for _ in range(task_count):
                await handlers.process_queue()
        return handlers, nik1, nik2

    @staticmethod
    async def make_response(file_name, reply_to, editor_chat_id, result_path):
        document = SimpleNamespace(file_name=file_name)
        message = SimpleNamespace(
            reply_to_message_id=reply_to,
            document=document,
            chat=SimpleNamespace(id=editor_chat_id),
            from_user=SimpleNamespace(username="editor", id=9000),
        )

        async def download(path):
            Path(path).write_bytes(Path(result_path).read_bytes())
            return path

        message.download = download
        return message

    async def test_normal_and_ai_reports_are_delivered_immediately_once_and_replied_to_group(self):
        tasks = [
            telegram_file_info(
                "forced",
                42,
                "курсовая работа.docx",
                chat_id=-100123,
                message_id=777,
            )
        ]
        settings = base_settings(
            ["forced"],
            {"forced": {"destination": "editor", "editor_nickname": "@fixed-editor"}},
        )
        sent = [SimpleNamespace(id=501, chat=SimpleNamespace(id=301))]
        handlers, nik1, _nik2 = await self._make_handlers_with_tasks(tasks, sent, settings)
        tracking_key, tracking = next(iter(handlers.editor_tracking.items()))
        self.assertEqual("@fixed-editor", tracking["destination"])

        normal_path = Path(tempfile.gettempdir()) / "normal-result.pdf"
        ai_path = Path(tempfile.gettempdir()) / "ai-result.pdf"
        normal_path.write_bytes(b"normal")
        ai_path.write_bytes(b"ai")
        client = SimpleNamespace(name="НИК-2")

        with patch("bot_handlers.Config.get_setting", side_effect=settings_lookup(settings)):
            await handlers.handle_editor_response(
                client,
                await self.make_response("курсовая работа.pdf", 501, 301, normal_path),
            )
            self.assertEqual(1, nik1.send_document.await_count)
            self.assertEqual("курсовая работа.pdf", nik1.send_document.await_args.kwargs["file_name"])
            self.assertIn(tracking_key, handlers.editor_tracking)
            await handlers.handle_editor_response(
                client,
                await self.make_response("ИИ курсовая работа.pdf", 501, 301, ai_path),
            )
            await handlers.handle_editor_response(
                client,
                await self.make_response("курсовая работа.pdf", 501, 301, normal_path),
            )

        self.assertEqual(2, nik1.send_document.await_count)
        calls = nik1.send_document.await_args_list
        self.assertEqual(
            ["курсовая работа.pdf", "ИИ курсовая работа.pdf"],
            [call.kwargs["file_name"] for call in calls],
        )
        self.assertEqual([-100123, -100123], [call.kwargs["chat_id"] for call in calls])
        self.assertEqual([777, 777], [call.kwargs["reply_to_message_id"] for call in calls])
        self.assertEqual(
            {"курсовая работа.pdf", "ии курсовая работа.pdf"},
            tracking["delivered_reports"],
        )
        self.assertEqual([b"normal", b"ai"], nik1.sent_contents)
        self.assertEqual(0, handlers.processor.crop_pdf.call_count)

    async def test_same_message_id_in_different_editor_chats_keeps_tasks_separate(self):
        tasks = [
            telegram_file_info("author-a", 101, "работа.docx", message_id=1),
            telegram_file_info("author-b", 102, "работа.docx", message_id=2),
        ]
        settings = base_settings(
            ["author-a", "author-b"],
            {
                "author-a": {"destination": "editor", "editor_nickname": "@editor-a"},
                "author-b": {"destination": "editor", "editor_nickname": "@editor-b"},
            },
        )
        sent = [
            SimpleNamespace(id=600, chat=SimpleNamespace(id=401)),
            SimpleNamespace(id=600, chat=SimpleNamespace(id=402)),
        ]
        handlers, nik1, _nik2 = await self._make_handlers_with_tasks(tasks, sent, settings)
        first_path = Path(tempfile.gettempdir()) / "same-first.pdf"
        second_path = Path(tempfile.gettempdir()) / "same-second.pdf"
        first_path.write_bytes(b"first")
        second_path.write_bytes(b"second")
        client = SimpleNamespace(name="НИК-2")

        with patch("bot_handlers.Config.get_setting", side_effect=settings_lookup(settings)):
            await handlers.handle_editor_response(
                client,
                await self.make_response("работа.pdf", 600, 401, first_path),
            )
            await handlers.handle_editor_response(
                client,
                await self.make_response("работа.pdf", 600, 402, second_path),
            )

        self.assertEqual([101, 102], [call.kwargs["chat_id"] for call in nik1.send_document.await_args_list])

    async def test_reply_from_other_editor_chat_cannot_match_single_task(self):
        tasks = [telegram_file_info("author", 101, "работа.docx", message_id=1)]
        settings = base_settings(
            ["author"],
            {"author": {"destination": "editor", "editor_nickname": "@editor"}},
        )
        sent = [SimpleNamespace(id=600, chat=SimpleNamespace(id=401))]
        handlers, nik1, _nik2 = await self._make_handlers_with_tasks(tasks, sent, settings)
        result_path = Path(tempfile.gettempdir()) / "wrong-editor-chat.pdf"
        result_path.write_bytes(b"wrong-chat")

        with patch("bot_handlers.Config.get_setting", side_effect=settings_lookup(settings)):
            await handlers.handle_editor_response(
                SimpleNamespace(name="НИК-2"),
                await self.make_response("работа.pdf", 600, 402, result_path),
            )

        self.assertEqual(0, nik1.send_document.await_count)
        self.assertEqual(1, len(handlers.editor_tracking))

    async def test_ai_report_can_arrive_before_normal_report(self):
        tasks = [
            telegram_file_info("forced", 42, "курсовая работа.docx", chat_id=-100123, message_id=777)
        ]
        settings = base_settings(
            ["forced"],
            {"forced": {"destination": "editor", "editor_nickname": "@fixed-editor"}},
        )
        sent = [SimpleNamespace(id=502, chat=SimpleNamespace(id=301))]
        handlers, nik1, _nik2 = await self._make_handlers_with_tasks(tasks, sent, settings)
        ai_path = Path(tempfile.gettempdir()) / "first-ai-result.pdf"
        normal_path = Path(tempfile.gettempdir()) / "second-normal-result.pdf"
        ai_path.write_bytes(b"ai")
        normal_path.write_bytes(b"normal")
        client = SimpleNamespace(name="НИК-2")

        with patch("bot_handlers.Config.get_setting", side_effect=settings_lookup(settings)):
            await handlers.handle_editor_response(
                client,
                await self.make_response("ИИ курсовая работа.pdf", 502, 301, ai_path),
            )
            self.assertEqual(1, nik1.send_document.await_count)
            self.assertEqual("ИИ курсовая работа.pdf", nik1.send_document.await_args.kwargs["file_name"])
            await handlers.handle_editor_response(
                client,
                await self.make_response("курсовая работа.pdf", 502, 301, normal_path),
            )

        self.assertEqual(
            ["ИИ курсовая работа.pdf", "курсовая работа.pdf"],
            [call.kwargs["file_name"] for call in nik1.send_document.await_args_list],
        )
        self.assertEqual([b"ai", b"normal"], nik1.sent_contents)

    async def test_same_author_can_have_two_same_named_tasks_with_reply_correlation(self):
        tasks = [
            telegram_file_info("author", 101, "работа.docx", message_id=11),
            telegram_file_info("author", 101, "работа.docx", message_id=12),
        ]
        settings = base_settings(
            ["author"],
            {"author": {"destination": "editor", "editor_nickname": "@editor"}},
        )
        sent = [
            SimpleNamespace(id=610, chat=SimpleNamespace(id=401)),
            SimpleNamespace(id=611, chat=SimpleNamespace(id=401)),
        ]
        handlers, nik1, _nik2 = await self._make_handlers_with_tasks(tasks, sent, settings)
        first_path = Path(tempfile.gettempdir()) / "same-author-first.pdf"
        second_path = Path(tempfile.gettempdir()) / "same-author-second.pdf"
        first_path.write_bytes(b"first")
        second_path.write_bytes(b"second")
        client = SimpleNamespace(name="НИК-2")

        with patch("bot_handlers.Config.get_setting", side_effect=settings_lookup(settings)):
            await handlers.handle_editor_response(
                client,
                await self.make_response("работа.pdf", 610, 401, first_path),
            )
            await handlers.handle_editor_response(
                client,
                await self.make_response("работа.pdf", 611, 401, second_path),
            )

        self.assertEqual([101, 101], [call.kwargs["chat_id"] for call in nik1.send_document.await_args_list])
        self.assertEqual(2, len(handlers.editor_tracking))


    async def test_fixed_editor_no_checks_does_not_change_route_for_next_task(self):
        settings = base_settings(
            ["forced"],
            {"forced": {"destination": "editor", "editor_nickname": "@fixed-editor"}},
            mode="mode2",
        )
        sent = [
            SimpleNamespace(id=620, chat=SimpleNamespace(id=401)),
            SimpleNamespace(id=621, chat=SimpleNamespace(id=401)),
        ]
        handlers, _nik1, nik2 = await self._make_handlers_with_tasks([], sent, settings)
        first_task = telegram_file_info("forced", 42, "first.docx", message_id=1)
        second_task = telegram_file_info("forced", 42, "second.docx", message_id=2)
        client = SimpleNamespace(name="НИК-2")

        with patch("bot_handlers.Config.get_setting", side_effect=settings_lookup(settings)):
            handlers.manager.file_queue = [first_task]
            await handlers.process_queue()
            no_checks = SimpleNamespace(
                reply_to_message_id=620,
                document=None,
                text="⏳ В данный момент проверки закончились. Начнем проверять после: 12:00 по мск времени.",
                chat=SimpleNamespace(id=401),
                from_user=SimpleNamespace(username="fixed-editor", id=9000),
            )
            await handlers.handle_editor_response(client, no_checks)
            handlers.manager.file_queue = [second_task]
            await handlers.process_queue()

        self.assertTrue(handlers._is_editor_unavailable("@fixed-editor"))
        self.assertEqual(["@fixed-editor", "@fixed-editor"], [call.kwargs["chat_id"] for call in nik2.send_document.await_args_list])
        self.assertEqual(2, len(handlers.editor_tracking))

    async def test_pdf_reply_to_one_task_cannot_fallback_to_another_task_by_name(self):
        tasks = [
            telegram_file_info("author-a", 101, "a.docx", message_id=1),
            telegram_file_info("author-b", 102, "b.docx", message_id=2),
        ]
        settings = base_settings(
            ["author-a", "author-b"],
            {
                "author-a": {"destination": "editor", "editor_nickname": "@editor"},
                "author-b": {"destination": "editor", "editor_nickname": "@editor"},
            },
        )
        sent = [
            SimpleNamespace(id=601, chat=SimpleNamespace(id=401)),
            SimpleNamespace(id=602, chat=SimpleNamespace(id=401)),
        ]
        handlers, nik1, _nik2 = await self._make_handlers_with_tasks(tasks, sent, settings)
        result_path = Path(tempfile.gettempdir()) / "wrong-result.pdf"
        result_path.write_bytes(b"wrong")

        with patch("bot_handlers.Config.get_setting", side_effect=settings_lookup(settings)):
            await handlers.handle_editor_response(
                SimpleNamespace(name="НИК-2"),
                await self.make_response("b.pdf", 601, 401, result_path),
            )

        self.assertEqual(0, nik1.send_document.await_count)
        self.assertEqual(2, len(handlers.editor_tracking))


class TestTelegramRouteContext(unittest.IsolatedAsyncioTestCase):
    async def test_normal_processing_error_requeues_group_context(self):
        manager = make_manager()
        client = MagicMock()
        client.name = "НИК-3"
        client.send_message = AsyncMock()
        client.send_document = AsyncMock(side_effect=ValueError("invalid document"))
        manager.clients = {"НИК-3": client}
        manager.get_available_account.return_value = "НИК-3"
        manager.mark_account_busy = MagicMock()
        manager.mark_account_free = MagicMock()
        handlers = make_handlers(manager)
        source_path = Path(tempfile.gettempdir()) / "normal-requeue-source.docx"
        source_path.write_bytes(b"source")
        handlers._ensure_work_file = AsyncMock(return_value=str(source_path))
        handlers._wait_for_ai_upload_prompt = AsyncMock(return_value=True)
        handlers.process_queue = AsyncMock()
        message = SimpleNamespace(id=777, chat=SimpleNamespace(id=-100123))
        file_info = {
            **telegram_file_info("author", 42, "paper.docx", chat_id=-100123, message_id=777),
            "message": message,
        }
        settings = base_settings([], {}, ai_upload_prompt_attempts=1)

        with patch("bot_handlers.Config.get_setting", side_effect=settings_lookup(settings)):
            await handlers.process_normal_file(file_info)

        queued = manager.file_queue[0]
        self.assertEqual(-100123, queued["route_chat_id"])
        self.assertEqual(777, queued["route_message_id"])
        self.assertTrue(queued["route_is_group"])


class TestRequiredTelegramAccounts(unittest.TestCase):
    def test_aaa_accounts_are_required_only_when_normal_route_uses_aaa(self):
        with patch(
            "config.Config.get_setting",
            side_effect=settings_lookup(
                {"mode": "mode1", "normal_destination": "редактор", "normal_accounts": ["НИК-3"]}
            ),
        ):
            self.assertEqual({"НИК-1", "НИК-2"}, AccountManager.required_account_names())

        with patch(
            "config.Config.get_setting",
            side_effect=settings_lookup(
                {"mode": "mode1", "normal_destination": "бот", "normal_accounts": ["НИК-3", "НИК-4"]}
            ),
        ):
            self.assertEqual(
                {"НИК-1", "НИК-2", "НИК-3", "НИК-4"},
                AccountManager.required_account_names(),
            )

    def test_client_initialization_skips_unused_aaa_accounts(self):
        manager = AccountManager()
        accounts = {
            name: {"api_id": 1, "api_hash": "hash", "phone": None}
            for name in ("НИК-1", "НИК-2", "НИК-3", "НИК-4")
        }
        settings = {
            "mode": "mode1",
            "normal_destination": "редактор",
            "normal_accounts": ["НИК-3", "НИК-4"],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            sessions_dir = Path(temp_dir) / "sessions"
            files_dir = Path(temp_dir) / "files"
            with patch.object(Config, "load_config"), patch.object(
                Config, "get_all_accounts", return_value=accounts
            ), patch.object(Config, "SESSIONS_DIR", sessions_dir), patch.object(
                Config, "FILES_DIR", files_dir
            ), patch("config.Config.get_setting", side_effect=settings_lookup(settings)):
                result = asyncio.run(manager.init_all_clients())

        self.assertTrue(result)
        self.assertEqual({"НИК-1", "НИК-2"}, set(manager.clients))


if __name__ == "__main__":
    unittest.main()
