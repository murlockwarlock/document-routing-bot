"""
Tests for BotHandlers key methods:
- _build_work_file_path (Fix #5 & #6: absolute paths + uuid uniqueness)
- _ensure_work_file (Fix #4: VK download_url fallback)
- _deliver_document_to_origin (Fix #1: Telegram spool fallback)
- _finish_processing / _try_start_processing (state machine)
- file_info routing (VK → Telegram → VK)
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

# Patch heavy imports before importing bot_handlers
import sys

# Create stub modules so bot_handlers can be imported without real pyrogram/fitz/requests
_stubs = {}
for mod_name in (
    "pyrogram", "pyrogram.types", "pyrogram.errors", "pyrogram.enums",
    "fitz", "requests",
):
    if mod_name not in sys.modules:
        stub = MagicMock()
        sys.modules[mod_name] = stub
        _stubs[mod_name] = stub

# Provide FloodWait as a real exception class so "except FloodWait" works
class _FakeFloodWait(Exception):
    def __init__(self, value=30):
        self.value = value
        super().__init__(f"FloodWait: {value}")

sys.modules["pyrogram.errors"].FloodWait = _FakeFloodWait

# Now we can import the module under test
from bot_handlers import BotHandlers, CounterModeProcessor, FILES_DIR, FileProcessor, VkApiError  # noqa: E402


def _make_account_manager():
    """Create a minimal mock AccountManager."""
    mgr = MagicMock()
    mgr.get_client.return_value = None
    mgr.set_file_status = AsyncMock()
    mgr.file_queue = []
    mgr.processing_files = {}
    mgr.blacklisted_accounts = []
    return mgr


def _make_handlers(manager=None):
    """Instantiate BotHandlers with mocked dependencies."""
    with patch("bot_handlers.Config") as mock_config:
        mock_config.get_setting.return_value = None
        mgr = manager or _make_account_manager()
        handlers = BotHandlers(mgr)
        from multichannel_gateway.core.storage import SqliteJobStore
        handlers._safety_test_dir = tempfile.TemporaryDirectory()
        handlers._editor_safety_store = SqliteJobStore(Path(handlers._safety_test_dir.name) / "jobs.sqlite3")
    return handlers


class TestBuildWorkFilePath(unittest.TestCase):
    """Fix #5 (absolute path) and Fix #6 (uuid uniqueness)."""

    def test_anti_file_detection_supports_russian_and_latin_names(self):
        self.assertTrue(FileProcessor.is_anti_file("375762 анти.docx"))
        self.assertTrue(FileProcessor.is_anti_file("375762 anti.docx"))
        self.assertFalse(FileProcessor.is_anti_file("375762.docx"))

    def test_returns_absolute_path(self):
        file_info = {"original_file_name": "test.docx", "file_uid": "abc123"}
        path = BotHandlers._build_work_file_path(file_info, prefix="normal")
        self.assertTrue(os.path.isabs(path), f"Path should be absolute: {path}")
        self.assertTrue(path.startswith(FILES_DIR))

    def test_contains_uuid_suffix(self):
        file_info = {"original_file_name": "test.docx"}
        path1 = BotHandlers._build_work_file_path(file_info, prefix="a")
        path2 = BotHandlers._build_work_file_path(file_info, prefix="a")
        # Two calls should produce different paths (uuid uniqueness)
        self.assertNotEqual(path1, path2)

    def test_preserves_original_filename(self):
        file_info = {"original_file_name": "мой_файл.docx"}
        path = BotHandlers._build_work_file_path(file_info)
        self.assertTrue(path.endswith("мой_файл.docx"))

    def test_uses_file_name_fallback(self):
        file_info = {"file_name": "fallback.pdf"}
        path = BotHandlers._build_work_file_path(file_info)
        self.assertTrue(path.endswith("fallback.pdf"))

    def test_uses_default_when_no_name(self):
        file_info = {}
        path = BotHandlers._build_work_file_path(file_info)
        self.assertTrue(path.endswith("document.bin"))


class TestForcePlagiscanRouting(unittest.TestCase):
    def setUp(self):
        self.handlers = _make_handlers()

    def test_matches_telegram_username(self):
        with patch("bot_handlers.Config.get_setting", return_value=["@manager"]):
            self.assertTrue(self.handlers.is_force_plagiscan_author(author="manager"))

    def test_matches_vk_numeric_variants(self):
        with patch("bot_handlers.Config.get_setting", return_value=["vk:1106569752"]):
            self.assertTrue(self.handlers.is_force_plagiscan_author(route_sender_id="1106569752"))

        with patch("bot_handlers.Config.get_setting", return_value=["id1106569752"]):
            self.assertTrue(self.handlers.is_force_plagiscan_author(author_id="1106569752"))

        with patch("bot_handlers.Config.get_setting", return_value=["234351996"]):
            self.assertTrue(self.handlers.is_force_plagiscan_author(route_sender_id="-234351996"))

        with patch("bot_handlers.Config.get_setting", return_value=["vk:-234351996"]):
            self.assertTrue(self.handlers.is_force_plagiscan_author(route_sender_id="234351996"))

    def test_matches_vk_screen_name_variants(self):
        with patch("bot_handlers.Config.get_setting", return_value=["vk.com/editor_user"]):
            self.assertTrue(self.handlers.is_force_plagiscan_author(route_sender_name="editor_user"))

    def test_no_match_when_list_empty(self):
        with patch("bot_handlers.Config.get_setting", return_value=[]):
            self.assertFalse(self.handlers.is_force_plagiscan_author(author="manager"))

    def test_file_info_match_uses_route_sender(self):
        file_info = {"author": "Беседа", "route_sender_id": "1106569752"}
        with patch("bot_handlers.Config.get_setting", return_value=["1106569752"]):
            self.assertTrue(self.handlers.is_force_plagiscan_file_info(file_info))

    def test_force_plagiscan_telegram_user_is_allowed_even_not_in_allowed_authors(self):
        settings = {
            "ai_bot": "@AAA_Report_AIBot",
            "anti_bot": "@plagaiscan_bot",
            "allowed_authors": ["someone_else"],
            "plagiscan_users": ["@zakazrabotu"],
        }
        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None: settings.get(key, default)):
            self.assertTrue(self.handlers.is_author_allowed("zakazrabotu"))

    def test_explicit_vk_plagiscan_user_does_not_allow_same_telegram_numeric_author(self):
        settings = {
            "ai_bot": "@AAA_Report_AIBot",
            "anti_bot": "@plagaiscan_bot",
            "allowed_authors": ["someone_else"],
            "plagiscan_users": ["vk:25829046"],
        }
        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None: settings.get(key, default)):
            self.assertFalse(self.handlers.is_author_allowed("25829046"))

    def test_allowed_author_matches_bot_username_without_underscore_case_insensitive(self):
        settings = {
            "ai_bot": "@AAA_Report_AIBot",
            "anti_bot": "@plagaiscan_bot",
            "allowed_authors": ["@ANTIPLAGIADBOT"],
            "plagiscan_users": [],
        }
        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None: settings.get(key, default)):
            self.assertTrue(self.handlers.is_author_allowed("antiplagiadbot"))

    def test_mode2_editor_send_uses_transport_and_preserves_original_file_name(self):
        temp_file = Path(tempfile.gettempdir()) / "editor247_work_copy.docx"
        temp_file.write_bytes(b"data")
        client = MagicMock()
        client.send_document = AsyncMock(
            return_value=SimpleNamespace(id=555, chat=SimpleNamespace(id=777))
        )
        manager = _make_account_manager()
        manager.get_client.side_effect = lambda name: client if name == "НИК-2" else None
        handlers = _make_handlers(manager)
        handlers._ensure_work_file = AsyncMock(return_value=str(temp_file))

        file_info = {
            "author": "manager",
            "author_id": 123,
            "file_name": "357517.docx",
            "original_file_name": "357517.docx",
            "is_anti": False,
            "reason": "вне рабочего времени",
            "source_platform": "telegram",
        }

        with patch("bot_handlers.Config.get_setting", return_value="@editor247"):
            asyncio.run(handlers.send_to_24_7_editor(None, file_info))

        client.send_document.assert_awaited_once()
        _, kwargs = client.send_document.await_args
        self.assertRegex(kwargs["file_name"], r"^357517__job[1-9][0-9]*\.docx$")
        self.assertEqual("357517.docx", file_info["original_file_name"])

    def test_force_plagiscan_overrides_anti_destination_editor(self):
        handlers = _make_handlers()
        handlers.manager.file_queue = [
            {
                "file_name": "обычный.docx",
                "original_file_name": "обычный.docx",
                "is_anti": True,
                "force_plagiscan": True,
            }
        ]
        handlers.process_anti_file = AsyncMock()
        handlers.process_normal_file = AsyncMock()

        settings = {
            "max_concurrent": 7,
            "anti_destination": "редактор",
            "mode": "mode1",
            "normal_destination": "бот",
        }
        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None: settings.get(key, default)):
            asyncio.run(handlers.process_queue())

        handlers.process_anti_file.assert_awaited_once()
        routed = handlers.process_anti_file.await_args.args[0]
        self.assertTrue(routed["force_plagiscan"])
        handlers.process_normal_file.assert_not_awaited()

    def test_process_anti_file_editor_destination_uses_editor_nickname(self):
        manager = _make_account_manager()
        client_nik2 = AsyncMock()
        client_nik2.send_document = AsyncMock(return_value=SimpleNamespace(id=901, chat=SimpleNamespace(id=777)))
        manager.get_client.return_value = client_nik2
        handlers = _make_handlers(manager)
        handlers._ensure_work_file = AsyncMock(return_value="/tmp/anti.docx")
        file_info = {
            "author": "author",
            "author_id": "42",
            "file_name": "анти.docx",
            "original_file_name": "анти.docx",
            "is_anti": True,
            "force_plagiscan": False,
            "chat_id": "100",
            "message_id": "1",
            "file_uid": "telegram:100:1:0",
        }
        settings = {
            "anti_destination": "редактор",
            "editor_nickname": "@work_editor",
        }

        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None: settings.get(key, default)):
            asyncio.run(handlers.process_anti_file(file_info))

        client_nik2.send_document.assert_awaited_once()
        _, kwargs = client_nik2.send_document.await_args
        self.assertEqual("@work_editor", kwargs["chat_id"])

    def test_process_anti_file_editor_destination_prefers_anti_editor_nickname(self):
        manager = _make_account_manager()
        client_nik2 = AsyncMock()
        client_nik2.send_document = AsyncMock(return_value=SimpleNamespace(id=902, chat=SimpleNamespace(id=777)))
        manager.get_client.return_value = client_nik2
        handlers = _make_handlers(manager)
        handlers._ensure_work_file = AsyncMock(return_value="/tmp/anti.docx")
        file_info = {
            "author": "author",
            "author_id": "42",
            "file_name": "анти.docx",
            "original_file_name": "анти.docx",
            "is_anti": True,
            "force_plagiscan": False,
            "chat_id": "100",
            "message_id": "1",
            "file_uid": "telegram:100:1:0",
        }
        settings = {
            "anti_destination": "редактор",
            "editor_nickname": "@legacy_editor",
            "anti_editor_nickname": "@anti_editor",
            "normal_editor_nickname": "@normal_editor",
        }

        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None: settings.get(key, default)):
            asyncio.run(handlers.process_anti_file(file_info))

        client_nik2.send_document.assert_awaited_once()
        _, kwargs = client_nik2.send_document.await_args
        self.assertEqual("@anti_editor", kwargs["chat_id"])

    def test_process_anti_file_mode1_routes_to_247_when_anti_editor_unavailable(self):
        manager = _make_account_manager()
        client_nik2 = AsyncMock()
        client_nik2.send_document = AsyncMock(return_value=SimpleNamespace(id=904, chat=SimpleNamespace(id=777)))
        manager.get_client.return_value = client_nik2
        handlers = _make_handlers(manager)
        handlers._ensure_work_file = AsyncMock(return_value="/tmp/anti.docx")
        handlers._mark_editor_unavailable("@anti_editor")
        file_info = {
            "author": "author",
            "author_id": "42",
            "file_name": "анти.docx",
            "original_file_name": "анти.docx",
            "is_anti": True,
            "force_plagiscan": False,
            "chat_id": "100",
            "message_id": "1",
            "file_uid": "telegram:100:1:0",
        }
        settings = {
            "mode": "mode1",
            "anti_destination": "редактор",
            "editor_24_7": "@editor247",
            "editor_nickname": "@legacy_editor",
            "anti_editor_nickname": "@anti_editor",
        }

        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None: settings.get(key, default)):
            asyncio.run(handlers.process_anti_file(file_info))

        client_nik2.send_document.assert_awaited_once()
        _, kwargs = client_nik2.send_document.await_args
        self.assertEqual("@editor247", kwargs["chat_id"])
        self.assertEqual("@editor247", handlers.editor_tracking["anti_editor_904"]["destination"])

    def test_process_anti_file_mode2_uses_work_editor_not_mode1_anti_editor(self):
        manager = _make_account_manager()
        client_nik2 = AsyncMock()
        client_nik2.send_document = AsyncMock(return_value=SimpleNamespace(id=903, chat=SimpleNamespace(id=777)))
        manager.get_client.return_value = client_nik2
        handlers = _make_handlers(manager)
        handlers._ensure_work_file = AsyncMock(return_value="/tmp/anti.docx")
        file_info = {
            "author": "author",
            "author_id": "42",
            "file_name": "анти.docx",
            "original_file_name": "анти.docx",
            "is_anti": True,
            "force_plagiscan": False,
            "chat_id": "100",
            "message_id": "1",
            "file_uid": "telegram:100:1:0",
        }
        settings = {
            "mode": "mode2",
            "anti_destination": "редактор",
            "editor_nickname": "@work_editor",
            "anti_editor_nickname": "@mode1_anti_editor",
            "normal_editor_nickname": "@mode1_normal_editor",
        }

        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None: settings.get(key, default)):
            asyncio.run(handlers.process_anti_file(file_info))

        client_nik2.send_document.assert_awaited_once()
        _, kwargs = client_nik2.send_document.await_args
        self.assertEqual("@work_editor", kwargs["chat_id"])

    def test_process_anti_file_mode2_routes_to_247_when_work_editor_unavailable(self):
        manager = _make_account_manager()
        client_nik2 = AsyncMock()
        client_nik2.send_document = AsyncMock(return_value=SimpleNamespace(id=905, chat=SimpleNamespace(id=777)))
        manager.get_client.return_value = client_nik2
        handlers = _make_handlers(manager)
        handlers._ensure_work_file = AsyncMock(return_value="/tmp/anti.docx")
        handlers._mark_editor_unavailable("@work_editor")
        file_info = {
            "author": "author",
            "author_id": "42",
            "file_name": "анти.docx",
            "original_file_name": "анти.docx",
            "is_anti": True,
            "force_plagiscan": False,
            "chat_id": "100",
            "message_id": "1",
            "file_uid": "telegram:100:1:0",
        }
        settings = {
            "mode": "mode2",
            "anti_destination": "редактор",
            "editor_24_7": "@editor247",
            "editor_nickname": "@work_editor",
            "anti_editor_nickname": "@mode1_anti_editor",
        }

        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None: settings.get(key, default)):
            asyncio.run(handlers.process_anti_file(file_info))

        client_nik2.send_document.assert_awaited_once()
        _, kwargs = client_nik2.send_document.await_args
        self.assertEqual("@editor247", kwargs["chat_id"])
        self.assertEqual("@editor247", handlers.editor_tracking["anti_editor_905"]["destination"])

    def test_normal_destination_plagiscan_routes_plain_file_to_anti_bot(self):
        handlers = _make_handlers()
        handlers.manager.file_queue = [
            {
                "file_name": "обычный.docx",
                "original_file_name": "обычный.docx",
                "is_anti": False,
                "force_plagiscan": False,
            }
        ]
        handlers.process_anti_file = AsyncMock()
        handlers.process_normal_file = AsyncMock()

        settings = {
            "max_concurrent": 7,
            "anti_destination": "редактор",
            "mode": "mode1",
            "normal_destination": "плагискан",
        }
        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None: settings.get(key, default)):
            asyncio.run(handlers.process_queue())

        handlers.process_anti_file.assert_awaited_once()
        routed = handlers.process_anti_file.await_args.args[0]
        self.assertTrue(routed["is_anti"])
        self.assertFalse(routed["force_plagiscan"])
        self.assertTrue(routed["via_normal_destination"])
        handlers.process_normal_file.assert_not_awaited()

    def test_plagiscan_user_filter_unlisted_normal_file_still_goes_to_plagiscan_when_destination_set(self):
        """Unlisted user + normal_destination=плагискан → still goes to plagiscan (not process_normal_file)."""
        handlers = _make_handlers()
        handlers.manager.file_queue = [
            {
                "file_name": "обычный.docx",
                "original_file_name": "обычный.docx",
                "is_anti": False,
                "force_plagiscan": False,
            }
        ]
        handlers.process_anti_file = AsyncMock()
        handlers.process_normal_file = AsyncMock()

        settings = {
            "max_concurrent": 7,
            "anti_destination": "бот",
            "mode": "mode1",
            "normal_destination": "плагискан",
            "plagiscan_users": ["1106569752"],
        }
        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None: settings.get(key, default)):
            asyncio.run(handlers.process_queue())

        # normal_destination=плагискан → process_anti_file (plagiscan path), not process_normal_file
        handlers.process_anti_file.assert_awaited_once()
        routed = handlers.process_anti_file.await_args.args[0]
        self.assertFalse(routed["force_plagiscan"])
        self.assertTrue(routed["via_normal_destination"])
        handlers.process_normal_file.assert_not_awaited()

    def test_force_plagiscan_user_goes_to_plagiscan_even_when_destination_is_bot(self):
        """User in plagiscan_users → file goes to plagiscan even when normal_destination=бот (Victoria's bug)."""
        handlers = _make_handlers()
        handlers.manager.file_queue = [
            {
                "file_name": "виктория.docx",
                "original_file_name": "виктория.docx",
                "is_anti": False,
                "force_plagiscan": True,  # set by is_force_plagiscan_author
            }
        ]
        handlers.process_anti_file = AsyncMock()
        handlers.process_normal_file = AsyncMock()

        settings = {
            "max_concurrent": 7,
            "mode": "mode1",
            "normal_destination": "бот",   # бот, not плагискан
            "plagiscan_users": ["1106569752"],
        }
        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None: settings.get(key, default)):
            asyncio.run(handlers.process_queue())

        # force_plagiscan=True must override normal_destination=бот
        handlers.process_anti_file.assert_awaited_once()
        handlers.process_normal_file.assert_not_awaited()

    def test_plagiscan_user_filter_does_not_override_unlisted_anti_destination(self):
        handlers = _make_handlers()
        file_info = {
            "file_name": "анти.docx",
            "original_file_name": "анти.docx",
            "is_anti": True,
            "force_plagiscan": False,
        }
        settings = {
            "anti_destination": "бот",
            "plagiscan_users": ["1106569752"],
        }
        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None: settings.get(key, default)):
            self.assertEqual("бот", handlers._anti_destination_for(file_info))

    def test_plagiscan_allows_parallel_anti_bot_files(self):
        handlers = _make_handlers()
        handlers.current_processing_files["anti-1"] = {
            "account": "НИК-2",
            "bot_type": "anti",
        }
        handlers.manager.file_queue = [
            {
                "file_name": "анти2.docx",
                "original_file_name": "анти2.docx",
                "is_anti": True,
                "force_plagiscan": True,
            }
        ]
        handlers.process_anti_file = AsyncMock()

        settings = {
            "max_concurrent": 7,
            "anti_destination": "бот",
            "plagiscan_users": [],
        }
        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None: settings.get(key, default)):
            asyncio.run(handlers.process_queue())

        handlers.process_anti_file.assert_awaited_once()
        self.assertEqual(0, len(handlers.manager.file_queue))


class TestMachineGenerationAndAIReport(unittest.TestCase):
    """Tests for machine-generation detection and dual-report (main + ИИ) delivery."""

    def setUp(self):
        self.handlers = _make_handlers()

    # ── _extract_machine_generation_percent ─────────────────────────────────

    def test_extract_machine_gen_present(self):
        text = "✅ Ваш файл успешно проверен!\nОригинальность: 59.06%\nМашинная генерация: 21.2%"
        self.assertAlmostEqual(21.2, self.handlers._extract_machine_generation_percent(text))

    def test_extract_machine_gen_zero(self):
        text = "✅ Ваш файл успешно проверен!\nОригинальность: 85%\nМашинная генерация: 0%"
        self.assertEqual(0.0, self.handlers._extract_machine_generation_percent(text))

    def test_extract_machine_gen_absent(self):
        text = "✅ Ваш файл успешно проверен!\nОригинальность: 85%"
        self.assertIsNone(self.handlers._extract_machine_generation_percent(text))

    def test_extract_machine_gen_comma_decimal(self):
        text = "Машинная генерация: 15,7%"
        self.assertAlmostEqual(15.7, self.handlers._extract_machine_generation_percent(text))

    def test_extract_machine_gen_none_text(self):
        self.assertIsNone(self.handlers._extract_machine_generation_percent(None))

    # ── _is_ai_report_button ─────────────────────────────────────────────────

    def test_is_ai_report_button_ии(self):
        btn = SimpleNamespace(text="ИИ отчет", callback_data="ai_report")
        self.assertTrue(self.handlers._is_ai_report_button(btn))

    def test_is_ai_report_button_ai(self):
        btn = SimpleNamespace(text="AI Report", callback_data="ai")
        self.assertTrue(self.handlers._is_ai_report_button(btn))

    def test_is_ai_report_button_normal_report(self):
        btn = SimpleNamespace(text="Скачать отчет", callback_data="report")
        self.assertFalse(self.handlers._is_ai_report_button(btn))

    # ── _find_plagiscan_report_buttons ───────────────────────────────────────

    def test_find_buttons_returns_both(self):
        btn_main = SimpleNamespace(text="Скачать отчет", callback_data="get_report", url=None)
        btn_ai = SimpleNamespace(text="ИИ отчет", callback_data="get_ai_report", url=None)
        markup = SimpleNamespace(inline_keyboard=[[btn_main, btn_ai]])
        msg = SimpleNamespace(reply_markup=markup)
        main, ai = self.handlers._find_plagiscan_report_buttons(msg)
        self.assertIsNotNone(main)
        self.assertIsNotNone(ai)
        self.assertFalse(self.handlers._is_ai_report_button(main))
        self.assertTrue(self.handlers._is_ai_report_button(ai))

    def test_find_buttons_no_markup(self):
        msg = SimpleNamespace(reply_markup=None)
        main, ai = self.handlers._find_plagiscan_report_buttons(msg)
        self.assertIsNone(main)
        self.assertIsNone(ai)

    def test_find_buttons_only_main(self):
        btn = SimpleNamespace(text="Отчет", callback_data="report", url=None)
        markup = SimpleNamespace(inline_keyboard=[[btn]])
        msg = SimpleNamespace(reply_markup=markup)
        main, ai = self.handlers._find_plagiscan_report_buttons(msg)
        self.assertIsNotNone(main)
        self.assertIsNone(ai)

    # ── _anti_destination_for with force_plagiscan ───────────────────────────

    def test_force_plagiscan_always_routes_to_bot(self):
        """force_plagiscan=True must override anti_destination and send to bot."""
        file_info = {"force_plagiscan": True}
        with patch("bot_handlers.Config.get_setting", return_value=["some_user"]):
            dest = self.handlers._anti_destination_for(file_info)
        self.assertEqual("бот", dest)

    def test_unlisted_anti_file_uses_anti_destination_when_filter_active(self):
        """plagiscan_users only forces listed authors; other anti files still use anti_destination."""
        file_info = {"force_plagiscan": False}
        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None: {
            "anti_destination": "бот",
            "plagiscan_users": ["specific_user"],
        }.get(key, default)):
            dest = self.handlers._anti_destination_for(file_info)
        self.assertEqual("бот", dest)

    def test_normal_destination_plagiscan_routes_to_bot_without_force_author_flag(self):
        """normal_destination=плагискан sends to bot, but does not make the author forced."""
        file_info = {"force_plagiscan": False, "via_normal_destination": True}
        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None: {
            "anti_destination": "редактор",
            "plagiscan_users": ["specific_user"],
        }.get(key, default)):
            dest = self.handlers._anti_destination_for(file_info)
        self.assertEqual("бот", dest)

    # ── is_force_plagiscan_author: VK user formats ───────────────────────────

    def test_vk_numeric_id_matches(self):
        with patch("bot_handlers.Config.get_setting", return_value=["42"]):
            self.assertTrue(self.handlers.is_force_plagiscan_author(route_sender_id="42"))

    def test_vk_with_prefix_matches(self):
        with patch("bot_handlers.Config.get_setting", return_value=["vk:42"]):
            self.assertTrue(self.handlers.is_force_plagiscan_author(route_sender_id="42"))

    def test_vk_screen_name_matches(self):
        with patch("bot_handlers.Config.get_setting", return_value=["vk.com/victoria"]):
            self.assertTrue(self.handlers.is_force_plagiscan_author(route_sender_name="victoria"))

    def test_tg_username_at_prefix_stripped(self):
        with patch("bot_handlers.Config.get_setting", return_value=["@student99"]):
            self.assertTrue(self.handlers.is_force_plagiscan_author(author="student99"))

    def test_tg_numeric_id_matches(self):
        with patch("bot_handlers.Config.get_setting", return_value=["9988776"]):
            self.assertTrue(self.handlers.is_force_plagiscan_author(author_id=9988776))

    def test_no_match_returns_false(self):
        with patch("bot_handlers.Config.get_setting", return_value=["@other_user"]):
            self.assertFalse(self.handlers.is_force_plagiscan_author(author="student99"))

    def test_empty_list_returns_false(self):
        with patch("bot_handlers.Config.get_setting", return_value=[]):
            self.assertFalse(self.handlers.is_force_plagiscan_author(author="anyone"))

    # ── process_queue: normal file from plagiscan_users → is_anti=True ───────

    def test_force_plagiscan_normal_file_queued_as_anti(self):
        """Normal file from a user in plagiscan_users must be treated as anti in the queue."""
        handlers = _make_handlers()
        handlers.manager.file_queue = [
            {
                "file_name": "курсовая.docx",
                "original_file_name": "курсовая.docx",
                "is_anti": True,          # set at intake when force_plagiscan=True
                "force_plagiscan": True,
            }
        ]
        handlers.process_anti_file = AsyncMock()
        handlers.process_normal_file = AsyncMock()

        settings = {"max_concurrent": 5, "mode": "mode1", "normal_destination": "бот",
                    "anti_destination": "редактор", "plagiscan_users": ["@student"]}
        with patch("bot_handlers.Config.get_setting",
                   side_effect=lambda k, default=None: settings.get(k, default)):
            asyncio.run(handlers.process_queue())

        handlers.process_anti_file.assert_awaited_once()
        handlers.process_normal_file.assert_not_awaited()
        routed = handlers.process_anti_file.await_args.args[0]
        self.assertTrue(routed["force_plagiscan"])
        self.assertTrue(routed["is_anti"])

    # ── _has_plagiscan_user_filter ────────────────────────────────────────────

    def test_filter_active_when_list_nonempty(self):
        with patch("bot_handlers.Config.get_setting", return_value=["@user1", "123"]):
            self.assertTrue(self.handlers._has_plagiscan_user_filter())

    def test_filter_inactive_when_list_empty(self):
        with patch("bot_handlers.Config.get_setting", return_value=[]):
            self.assertFalse(self.handlers._has_plagiscan_user_filter())

    def test_filter_inactive_when_none(self):
        with patch("bot_handlers.Config.get_setting", return_value=None):
            self.assertFalse(self.handlers._has_plagiscan_user_filter())


class TestEditorPdfMatching(unittest.TestCase):
    def test_no_reply_pdf_matches_exact_original_duplicate_suffix(self):
        handlers = _make_handlers()
        handlers.editor_tracking = {
            "first": {
                "sent_from_account": "НИК-2",
                "destination": "@editor",
                "chat_id": 1000,
                "original_name": "work.docx",
                "original_name_without_ext": "work",
                "expected_pdf_name": "work.pdf",
                "sent_at": datetime(2026, 5, 6, 10, 0),
                "reply_to_message_id": 100,
            },
            "second": {
                "sent_from_account": "НИК-2",
                "destination": "@editor",
                "chat_id": 1000,
                "original_name": "work (1).docx",
                "original_name_without_ext": "work (1)",
                "expected_pdf_name": "work (1).pdf",
                "sent_at": datetime(2026, 5, 6, 10, 1),
                "reply_to_message_id": 101,
            },
        }

        key, info, _reason = handlers.find_editor_tracking_for_unreplied_pdf("editor", "work (1).pdf")

        self.assertEqual("second", key)
        self.assertEqual(101, info["reply_to_message_id"])

    def test_no_reply_pdf_does_not_guess_when_name_does_not_match(self):
        handlers = _make_handlers()
        handlers.editor_tracking = {
            "anti": {
                "sent_from_account": "НИК-2",
                "destination": "@zakazrabotu",
                "original_name": "361664 анти (1).doc",
                "original_name_without_ext": "361664 анти (1)",
                "expected_pdf_name": "361664 анти (1).pdf",
                "sent_at": datetime(2026, 5, 6, 14, 43),
                "reply_to_message_id": 62107,
            },
            "other": {
                "sent_from_account": "НИК-2",
                "destination": "@zakazrabotu",
                "original_name": "8397_Анти.pdf",
                "original_name_without_ext": "8397_Анти",
                "expected_pdf_name": "8397_Анти.pdf",
                "sent_at": datetime(2026, 5, 6, 14, 44),
                "reply_to_message_id": 62108,
            },
        }

        key, info, reason = handlers.find_editor_tracking_for_unreplied_pdf(
            "zakazrabotu",
            "362092 анти (1).pdf",
        )

        self.assertIsNone(key)
        self.assertIsNone(info)
        self.assertIn("нет точного совпадения", reason)

    def test_no_reply_pdf_ambiguous_same_name_is_not_delivered(self):
        handlers = _make_handlers()
        handlers.editor_tracking = {
            "first": {
                "sent_from_account": "НИК-2",
                "destination": "@editor",
                "original_name": "370111.docx",
                "expected_pdf_name": "370111.pdf",
                "reply_to_message_id": 100,
            },
            "second": {
                "sent_from_account": "НИК-2",
                "destination": "@editor",
                "original_name": "370111.rtf",
                "expected_pdf_name": "370111.pdf",
                "reply_to_message_id": 101,
            },
        }

        key, info, reason = handlers.find_editor_tracking_for_unreplied_pdf("editor", "370111.pdf")

        self.assertIsNone(key)
        self.assertIsNone(info)
        self.assertIn("неоднозначное", reason)


class TestPlagiscanParallelResponseMatching(unittest.TestCase):
    def test_reply_maps_response_to_matching_parallel_file_even_with_same_name(self):
        handlers = _make_handlers()
        handlers.current_processing_files = {
            "anti-001": {
                "account": "НИК-2",
                "bot_type": "anti",
                "original_file_name": "same.docx",
            },
            "anti-002": {
                "account": "НИК-2",
                "bot_type": "anti",
                "original_file_name": "same.docx",
            },
        }
        handlers.message_to_file_map = {"999_502": "anti-002"}
        message = SimpleNamespace(
            reply_to_message_id=502,
            chat=SimpleNamespace(id=999),
            document=SimpleNamespace(file_name="result.pdf"),
            text=None,
        )

        key, info, reason = handlers.find_anti_processing_for_response("НИК-2", message)

        self.assertEqual("anti-002", key)
        self.assertEqual("same.docx", info["original_file_name"])
        self.assertIn("reply_to", reason)

    def test_no_reply_pdf_suffix_does_not_guess_parallel_duplicate(self):
        handlers = _make_handlers()
        handlers.current_processing_files = {
            "anti-001": {
                "account": "НИК-2",
                "bot_type": "anti",
                "original_file_name": "work.docx",
            },
            "anti-002": {
                "account": "НИК-2",
                "bot_type": "anti",
                "original_file_name": "work.docx",
            },
        }
        message = SimpleNamespace(
            reply_to_message_id=None,
            chat=SimpleNamespace(id=999),
            document=SimpleNamespace(file_name="work (1).pdf"),
            text=None,
        )

        key, info, reason = handlers.find_anti_processing_for_response("НИК-2", message)

        self.assertIsNone(key)
        self.assertIsNone(info)
        self.assertIn("unknown", reason)

    def test_no_reply_result_without_name_does_not_choose_oldest(self):
        handlers = _make_handlers()
        handlers.current_processing_files = {
            "anti-010": {
                "account": "НИК-2",
                "bot_type": "anti",
                "original_file_name": "first.docx",
            },
            "anti-020": {
                "account": "НИК-2",
                "bot_type": "anti",
                "original_file_name": "second.docx",
            },
        }
        message = SimpleNamespace(
            reply_to_message_id=None,
            chat=SimpleNamespace(id=999),
            document=None,
            text="✅ Ваш файл успешно проверен! Оригинальность: 90%",
        )

        key, info, reason = handlers.find_anti_processing_for_response("НИК-2", message)

        self.assertIsNone(key)
        self.assertIsNone(info)
        self.assertIn("correlation", reason)


class TestPlagiscanReportButtons(unittest.TestCase):
    def test_extracts_machine_generation_percent(self):
        text = "✅ Ваш файл успешно проверен!\nОригинальность: 59.06%\nМашинная генерация: 21.2%"
        self.assertEqual(21.2, BotHandlers._extract_machine_generation_percent(text))

    def test_zero_machine_generation_does_not_require_ai_report(self):
        text = "Машинная генерация: 0%"
        self.assertEqual(0.0, BotHandlers._extract_machine_generation_percent(text))

    def test_finds_main_and_ai_report_buttons(self):
        main_button = SimpleNamespace(text="Посмотреть отчет", url="https://example.com/main.pdf")
        help_button = SimpleNamespace(text="Посмотреть справку", url="https://example.com/help")
        ai_button = SimpleNamespace(text="Посмотреть отчет по ИИ", url="https://example.com/ai.pdf")
        message = SimpleNamespace(
            reply_markup=SimpleNamespace(
                inline_keyboard=[[main_button, help_button, ai_button]]
            )
        )

        normal, ai = BotHandlers._find_plagiscan_report_buttons(message)
        self.assertIs(normal, main_button)
        self.assertIs(ai, ai_button)


class TestPlagiscanScenarioHandling(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.handlers = _make_handlers()
        self.handlers.file_tracking = {"vk-user": {"sent": [], "received": [], "pending": [], "sent_to_editor": []}}
        self.handlers.manager.mark_account_free = Mock()
        self.handlers.process_queue = AsyncMock()
        self.handlers.message_to_file_map = {"999_23": "anti-key"}
        self.handlers._finish_processing = Mock()
        self.handlers._mark_gateway_job_done = AsyncMock()
        self.handlers._deliver_document_to_origin = AsyncMock(return_value={"platform": "vk", "status": "sent"})
        self.handlers.processor.cleanup_temp_files = Mock()

        def fake_download(url, path):
            Path(path).write_bytes(f"pdf:{url}".encode())
            return True

        def fake_crop(input_path, output_path):
            Path(output_path).write_bytes(Path(input_path).read_bytes() + b":cropped")
            return True

        self.handlers.processor.download_pdf = fake_download
        self.handlers.processor.crop_pdf = fake_crop

    def tearDown(self):
        self.temp_dir.cleanup()

    def _processing_info(self, file_name="курсовая работа (5).docx"):
        return {
            "author": "vk-user",
            "author_id": "1106569752",
            "original_file_name": file_name,
            "temp_path": str(Path(self.temp_dir.name) / file_name),
            "account": "НИК-2",
            "bot_type": "anti",
            "chat_id": "2000000001",
            "message_id": "23",
            "file_uid": "vk:2000000001:23:0",
            "source_platform": "vk",
            "route_sender_id": "1106569752",
            "route_chat_id": "2000000001",
            "gateway_job_id": 44,
        }

    def test_plagiscan_success_with_ai_percent_non_force_sends_only_main_report(self):
        info = self._processing_info()
        self.handlers.current_processing_files["anti-key"] = info
        main_button = SimpleNamespace(text="Посмотреть отчет", url="https://example.com/main.pdf")
        ai_button = SimpleNamespace(text="Посмотреть отчет по ИИ", url="https://example.com/ai.pdf")
        message = SimpleNamespace(
            text="✅ Ваш файл успешно проверен!\nОригинальность: 59.06%\nМашинная генерация: 21.2%",
            reply_to_message_id=23,
            chat=SimpleNamespace(id=999),
            id=1000,
            reply_markup=SimpleNamespace(inline_keyboard=[[main_button, ai_button]]),
            document=None,
        )
        client = SimpleNamespace(name="НИК-2")
        self.handlers.manager.get_client.return_value = SimpleNamespace(name="НИК-1")

        asyncio.run(self.handlers.handle_anti_bot_response(client, message))

        sent_names = [call.kwargs["file_name"] for call in self.handlers._deliver_document_to_origin.await_args_list]
        self.assertEqual(["курсовая работа (5).pdf"], sent_names)
        self.assertNotIn("anti-key", self.handlers.current_processing_files)
        self.handlers._finish_processing.assert_called_once_with("vk:2000000001:23:0", True)

    def test_force_plagiscan_success_with_ai_percent_sends_two_reports(self):
        info = self._processing_info()
        info["force_plagiscan"] = True
        self.handlers.current_processing_files["anti-key"] = info
        main_button = SimpleNamespace(text="Посмотреть отчет", url="https://example.com/main.pdf")
        ai_button = SimpleNamespace(text="Посмотреть отчет по ИИ", url="https://example.com/ai.pdf")
        message = SimpleNamespace(
            text="✅ Ваш файл успешно проверен!\nОригинальность: 59.06%\nМашинная генерация: 21.2%",
            reply_to_message_id=23,
            chat=SimpleNamespace(id=999),
            id=1000,
            reply_markup=SimpleNamespace(inline_keyboard=[[main_button, ai_button]]),
            document=None,
        )
        client = SimpleNamespace(name="НИК-2")
        self.handlers.manager.get_client.return_value = SimpleNamespace(name="НИК-1")

        asyncio.run(self.handlers.handle_anti_bot_response(client, message))

        sent_names = [call.kwargs["file_name"] for call in self.handlers._deliver_document_to_origin.await_args_list]
        self.assertEqual(["курсовая работа (5).pdf", "ИИ курсовая работа (5).pdf"], sent_names)
        self.assertNotIn("anti-key", self.handlers.current_processing_files)
        self.handlers._finish_processing.assert_called_once_with("vk:2000000001:23:0", True)

    def test_plagiscan_zero_ai_percent_sends_only_main_report(self):
        info = self._processing_info("работа.docx")
        self.handlers.current_processing_files["anti-key"] = info
        main_button = SimpleNamespace(text="Посмотреть отчет", url="https://example.com/main.pdf")
        ai_button = SimpleNamespace(text="Посмотреть отчет по ИИ", url="https://example.com/ai.pdf")
        message = SimpleNamespace(
            text="✅ Ваш файл успешно проверен!\nОригинальность: 90%\nМашинная генерация: 0%",
            reply_to_message_id=23,
            chat=SimpleNamespace(id=999),
            id=1000,
            reply_markup=SimpleNamespace(inline_keyboard=[[main_button, ai_button]]),
            document=None,
        )
        client = SimpleNamespace(name="НИК-2")
        self.handlers.manager.get_client.return_value = SimpleNamespace(name="НИК-1")

        asyncio.run(self.handlers.handle_anti_bot_response(client, message))

        sent_names = [call.kwargs["file_name"] for call in self.handlers._deliver_document_to_origin.await_args_list]
        self.assertEqual(["работа.pdf"], sent_names)

    def test_plagiscan_success_and_no_checks_still_sends_report_button(self):
        info = self._processing_info("анти_ВКР_1338295_Фин.2.docx")
        self.handlers.current_processing_files["anti-key"] = info
        main_button = SimpleNamespace(text="Посмотреть отчет", url="https://example.com/main.pdf")
        message = SimpleNamespace(
            text=(
                "✅ Ваш файл успешно проверен!\n"
                "Оригинальность: 74,89%\n"
                "Машинная генерация: 0.0%\n"
                "У вас закончились проверки 😉\n"
                "Перейдите в раздел оплата /payment, чтобы пополнить баланс проверок."
            ),
            reply_to_message_id=23,
            chat=SimpleNamespace(id=999),
            id=1000,
            reply_markup=SimpleNamespace(inline_keyboard=[[main_button]]),
            document=None,
        )
        client = SimpleNamespace(name="НИК-2")
        self.handlers.manager.get_client.return_value = SimpleNamespace(name="НИК-1")
        self.handlers.send_to_nik2 = AsyncMock()

        asyncio.run(self.handlers.handle_anti_bot_response(client, message))

        self.handlers.send_to_nik2.assert_not_awaited()
        sent_names = [call.kwargs["file_name"] for call in self.handlers._deliver_document_to_origin.await_args_list]
        self.assertEqual(["анти_ВКР_1338295_Фин.2.pdf"], sent_names)
        self.assertNotIn("anti-key", self.handlers.current_processing_files)

    def test_two_same_original_names_keep_distinct_processing_keys(self):
        first = self._processing_info("357517.docx")
        second = self._processing_info("357517.docx")
        first["file_uid"] = "vk:2000000001:23:0"
        second["file_uid"] = "vk:2000000001:24:0"
        self.handlers.current_processing_files["anti-001"] = first
        self.handlers.current_processing_files["anti-002"] = second

        self.assertEqual("vk:2000000001:23:0", self.handlers.current_processing_files["anti-001"]["file_uid"])
        self.assertEqual("vk:2000000001:24:0", self.handlers.current_processing_files["anti-002"]["file_uid"])

    def test_no_reply_plagiscan_pdf_name_maps_to_matching_parallel_file(self):
        first = self._processing_info("first_work.docx")
        second = self._processing_info("second_work.docx")
        self.handlers.current_processing_files["anti-001"] = first
        self.handlers.current_processing_files["anti-002"] = second

        message = SimpleNamespace(
            reply_to_message_id=None,
            chat=SimpleNamespace(id=999),
            document=SimpleNamespace(file_name="second_work.pdf"),
            text=None,
        )

        key, info, _reason = self.handlers.find_anti_processing_for_response("НИК-2", message)

        self.assertEqual("anti-002", key)
        self.assertEqual("second_work.docx", info["original_file_name"])


class TestEnsureWorkFile(unittest.TestCase):
    """Fix #4: VK download_url fallback."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.handlers = _make_handlers()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_uses_local_path_when_exists(self):
        """If local_path exists, it should be copied."""
        local_file = Path(self.temp_dir.name) / "source.docx"
        local_file.write_bytes(b"test content")

        file_info = {
            "original_file_name": "source.docx",
            "file_uid": "uid1",
            "local_path": str(local_file),
        }

        result = asyncio.run(self.handlers._ensure_work_file(file_info))
        self.assertTrue(os.path.exists(result))
        self.assertEqual(Path(result).read_bytes(), b"test content")

    def test_uses_message_download_fallback(self):
        """If no local_path, should use message.download()."""
        async def fake_download(target_path):
            Path(target_path).write_bytes(b"downloaded")
            return target_path

        mock_message = MagicMock()
        mock_message.document = MagicMock()
        mock_message.download = fake_download

        file_info = {
            "original_file_name": "doc.docx",
            "file_uid": "uid2",
            "message": mock_message,
        }

        result = asyncio.run(self.handlers._ensure_work_file(file_info))
        self.assertTrue(os.path.exists(result))
        self.assertEqual(Path(result).read_bytes(), b"downloaded")

    @patch("bot_handlers.requests")
    def test_vk_url_fallback(self, mock_requests):
        """Fix #4: If no local_path and no message, should try download_url."""
        mock_response = MagicMock()
        mock_response.content = b"vk file content"
        mock_response.raise_for_status.return_value = None
        mock_requests.get.return_value = mock_response

        file_info = {
            "original_file_name": "vk_doc.docx",
            "file_uid": "uid3",
            "download_url": "https://vk.com/download/test.docx",
        }

        result = asyncio.run(self.handlers._ensure_work_file(file_info))
        self.assertTrue(os.path.exists(result))
        self.assertEqual(Path(result).read_bytes(), b"vk file content")

    def test_raises_when_no_source(self):
        """Should raise FileNotFoundError when no source available."""
        file_info = {
            "original_file_name": "missing.docx",
            "file_uid": "uid4",
        }

        with self.assertRaises(FileNotFoundError):
            asyncio.run(self.handlers._ensure_work_file(file_info))


class TestVkCounterMode(unittest.IsolatedAsyncioTestCase):
    """VK history counter via messages.getHistory."""

    def setUp(self):
        self.processor = CounterModeProcessor(_make_account_manager())
        self.processor.generate_counter_report_simple = AsyncMock()
        self.print_patch = patch("builtins.print")
        self.print_patch.start()

    def tearDown(self):
        self.print_patch.stop()

    @staticmethod
    def _msg(ts, from_id=101, cmid=1, docs=None, text=""):
        attachments = []
        for index, doc in enumerate(docs or []):
            attachments.append(
                {
                    "type": "doc",
                    "doc": {
                        "id": 9000 + index,
                        "title": doc,
                        "ext": "docx" if "." not in doc else None,
                        "size": 100 + index,
                    },
                }
            )
        return {
            "date": ts,
            "from_id": from_id,
            "conversation_message_id": cmid,
            "text": text,
            "attachments": attachments,
        }

    async def test_vk_counter_counts_only_selected_authors_and_time_window(self):
        start_ts = int(datetime(2026, 4, 27, 10, 0).timestamp())
        page = {
            "items": [
                self._msg(start_ts + 60, from_id=101, cmid=10, docs=["work1.docx"]),
                self._msg(start_ts + 120, from_id=202, cmid=11, docs=["other.docx"]),
                self._msg(start_ts + 180, from_id=101, cmid=12, docs=[]),
                self._msg(start_ts - 60, from_id=101, cmid=13, docs=["old.docx"]),
            ]
        }
        self.processor._vk_api_call = Mock(return_value=page)

        with patch.object(self.processor, "_resolve_vk_counter_author_ids", return_value={101}):
            await self.processor.analyze_vk_files_custom(
                "101",
                "2000000001",
                date_str="2026-04-27",
                start_time_str="10:00",
            )

        sent_files = self.processor.generate_counter_report_simple.await_args.args[0]
        self.assertEqual(["work1.docx"], [item["file_name"] for item in sent_files])

    async def test_vk_counter_start_file_skips_until_match_inclusive(self):
        start_ts = int(datetime(2026, 4, 27, 0, 0).timestamp())
        page = {
            "items": [
                self._msg(start_ts + 60, cmid=1, docs=["before.docx"]),
                self._msg(start_ts + 120, cmid=2, docs=["start.docx"]),
                self._msg(start_ts + 180, cmid=3, docs=["after.docx"]),
            ]
        }
        self.processor._vk_api_call = Mock(return_value=page)

        with patch.object(self.processor, "_resolve_vk_counter_author_ids", return_value={101}):
            await self.processor.analyze_vk_files_custom(
                "101",
                "2000000001",
                start_file="start.docx",
                date_str="2026-04-27",
            )

        sent_files = self.processor.generate_counter_report_simple.await_args.args[0]
        self.assertEqual(["start.docx", "after.docx"], [item["file_name"] for item in sent_files])

    async def test_vk_counter_paginates_until_short_page(self):
        start_ts = int(datetime(2026, 4, 27, 0, 0).timestamp())
        first_page = {"items": [self._msg(start_ts + 1000 + i, cmid=i, docs=[f"f{i}.docx"]) for i in range(200)]}
        second_page = {"items": [self._msg(start_ts + 10, cmid=201, docs=["last.docx"])]}
        self.processor._vk_api_call = Mock(side_effect=[first_page, second_page])

        with patch.object(self.processor, "_resolve_vk_counter_author_ids", return_value={101}):
            await self.processor.analyze_vk_files_custom(
                "101",
                "2000000001",
                date_str="2026-04-27",
            )

        sent_files = self.processor.generate_counter_report_simple.await_args.args[0]
        self.assertEqual(201, len(sent_files))
        calls = self.processor._vk_api_call.call_args_list
        self.assertEqual(0, calls[0].args[1]["offset"])
        self.assertEqual(200, calls[1].args[1]["offset"])

    async def test_vk_counter_skips_payment_documents(self):
        start_ts = int(datetime(2026, 4, 27, 0, 0).timestamp())
        page = {
            "items": [
                self._msg(start_ts + 60, docs=["Квитанция.pdf"]),
                self._msg(start_ts + 120, docs=["normal.docx"]),
            ]
        }
        self.processor._vk_api_call = Mock(return_value=page)

        with patch.object(self.processor, "_resolve_vk_counter_author_ids", return_value={101}):
            await self.processor.analyze_vk_files_custom(
                "101",
                "2000000001",
                date_str="2026-04-27",
            )

        sent_files = self.processor.generate_counter_report_simple.await_args.args[0]
        self.assertEqual(["normal.docx"], [item["file_name"] for item in sent_files])

    def test_resolve_vk_counter_author_ids_mixes_ids_and_screen_names(self):
        self.processor._vk_api_call = Mock(return_value=[{"id": 303}])

        result = self.processor._resolve_vk_counter_author_ids("101, vk:202, https://vk.com/editor")

        self.assertEqual({101, 202, 303}, result)
        self.processor._vk_api_call.assert_called_once_with("users.get", {"user_ids": "editor"})

    async def test_vk_counter_logs_start_and_finish(self):
        start_ts = int(datetime(2026, 4, 27, 0, 0).timestamp())
        self.processor._vk_api_call = Mock(return_value={"items": [self._msg(start_ts + 60, docs=["a.docx"])]})

        with (
            patch.object(self.processor, "_resolve_vk_counter_author_ids", return_value={101}),
            patch("bot_handlers.append_channel_log") as log_mock,
        ):
            await self.processor.analyze_vk_files_custom(
                "101",
                "2000000001",
                date_str="2026-04-27",
            )

        log_messages = [call.args[1] for call in log_mock.call_args_list if call.args[0] == "vk_counter"]
        self.assertTrue(any(message.startswith("start ") for message in log_messages))
        self.assertTrue(any(message.startswith("finish ") for message in log_messages))

    @staticmethod
    def _job(
        file_name,
        ts,
        sender_id="101",
        chat_id="2000000001",
        message_id="1",
        dedupe_key=None,
        created_at="2026-04-27T00:00:00",
    ):
        return SimpleNamespace(
            dedupe_key=dedupe_key or f"vk:{chat_id}:{message_id}:0",
            source="vk",
            chat_id=chat_id,
            sender_id=sender_id,
            sender_name=None,
            message_id=message_id,
            reply_to_message_id=None,
            original_file_name=file_name,
            file_size_bytes=123,
            created_at=created_at,
            transport_meta={"vk_message_date": ts},
        )

    async def test_vk_longpoll_counter_counts_local_jobs_by_time_start_file_and_payment_filter(self):
        start_ts = int(datetime(2026, 4, 30, 10, 0).timestamp())
        jobs = [
            self._job("old.docx", start_ts - 60, message_id="old"),
            self._job("before.docx", start_ts + 60, message_id="before"),
            self._job("start.docx", start_ts + 120, message_id="start"),
            self._job("Квитанция.pdf", start_ts + 180, message_id="pay"),
            self._job("after.docx", start_ts + 240, message_id="after"),
        ]
        store = Mock()
        store.list_vk_counter_jobs.return_value = jobs

        with (
            patch("bot_handlers.build_store", return_value=store),
            patch.object(self.processor, "_resolve_vk_counter_author_ids", return_value={101}),
        ):
            await self.processor.analyze_vk_files_from_longpoll_custom(
                "101",
                "2000000001",
                start_file="start.docx",
                date_str="2026-04-30",
                start_time_str="10:00",
            )

        store.list_vk_counter_jobs.assert_called_once_with("2000000001", ("101",))
        sent_files = self.processor.generate_counter_report_simple.await_args.args[0]
        self.assertEqual(["start.docx", "after.docx"], [item["file_name"] for item in sent_files])

    async def test_vk_longpoll_counter_empty_local_database_still_generates_empty_report(self):
        store = Mock()
        store.list_vk_counter_jobs.return_value = []

        with (
            patch("bot_handlers.build_store", return_value=store),
            patch.object(self.processor, "_resolve_vk_counter_author_ids", return_value={101}),
        ):
            await self.processor.analyze_vk_files_from_longpoll_custom(
                "101",
                "2000000001",
                date_str="2026-04-30",
            )

        sent_files = self.processor.generate_counter_report_simple.await_args.args[0]
        self.assertEqual([], sent_files)

    async def test_vk_longpoll_counter_missing_peer_does_not_generate_report(self):
        await self.processor.analyze_vk_files_from_longpoll_custom(
            "101",
            "",
            date_str="2026-04-30",
        )

        self.processor.generate_counter_report_simple.assert_not_awaited()

    async def test_vk_route_uses_history_auto_counter(self):
        with (
            patch.object(self.processor, "analyze_vk_files_local", new=AsyncMock()) as local_counter,
            patch.object(self.processor, "analyze_vk_files_custom", new=AsyncMock()) as history_counter,
            patch.object(self.processor, "analyze_vk_files_history_auto", new=AsyncMock()) as auto_counter,
        ):
            await self.processor.analyze_author_files_custom(
                "101",
                date_str="2026-04-30",
            )

        auto_counter.assert_awaited_once()
        local_counter.assert_not_awaited()
        history_counter.assert_not_awaited()

    async def test_vk_history_auto_uses_ls_and_known_conversation_peers(self):
        start_ts = int(datetime(2026, 4, 30, 0, 0).timestamp())
        store = Mock()
        store.list_vk_peer_ids_by_sender.return_value = ["2000000001"]
        store.list_vk_jobs_by_sender.return_value = []
        self.processor._vk_api_call = Mock(
            side_effect=[
                {"items": [self._msg(start_ts + 60, from_id=101, cmid=1, docs=["ls.docx"])]},
                {"items": [self._msg(start_ts + 120, from_id=101, cmid=2, docs=["chat.docx"])]},
            ]
        )

        with (
            patch("bot_handlers.build_store", return_value=store),
            patch("bot_handlers.Config.get_setting", return_value=""),
            patch.object(self.processor, "_resolve_vk_counter_author_ids", return_value={101}),
        ):
            await self.processor.analyze_vk_files_history_auto("101", date_str="2026-04-30")

        store.list_vk_peer_ids_by_sender.assert_called_once_with(("101",))
        calls = self.processor._vk_api_call.call_args_list
        self.assertEqual("bot", calls[0].args[1]["_token_kind"])
        self.assertEqual(101, calls[0].args[1]["peer_id"])
        self.assertEqual("counter", calls[1].args[1]["_token_kind"])
        self.assertEqual(2000000001, calls[1].args[1]["peer_id"])
        sent_files = self.processor.generate_counter_report_simple.await_args.args[0]
        self.assertEqual(["ls.docx", "chat.docx"], [item["file_name"] for item in sent_files])

    async def test_vk_history_auto_uses_only_explicit_counter_peer_id_when_set(self):
        start_ts = int(datetime(2026, 5, 12, 0, 0).timestamp())
        store = Mock()
        store.list_vk_peer_ids_by_sender.return_value = ["2000000001"]
        store.list_vk_jobs_by_sender.return_value = []
        self.processor._vk_api_call = Mock(
            return_value={"items": [self._msg(start_ts + 60, from_id=1065504879, cmid=5, docs=["only_here.docx"])]}
        )

        def config_get(key, default=None):
            if key == "counter_vk_peer_id":
                return "2000000002"
            return default

        with (
            patch("bot_handlers.build_store", return_value=store),
            patch("bot_handlers.Config.get_setting", side_effect=config_get),
            patch.object(self.processor, "_resolve_vk_counter_author_ids", return_value={1065504879}),
        ):
            await self.processor.analyze_vk_files_history_auto("1065504879", date_str="2026-05-12")

        calls = self.processor._vk_api_call.call_args_list
        self.assertEqual(1, len(calls))
        self.assertEqual("messages.getHistory", calls[0].args[0])
        self.assertEqual(2000000002, calls[0].args[1]["peer_id"])
        sent_files = self.processor.generate_counter_report_simple.await_args.args[0]
        self.assertEqual(["only_here.docx"], [item["file_name"] for item in sent_files])

    async def test_vk_history_auto_collects_received_pdf_reports_from_same_peer(self):
        start_ts = int(datetime(2026, 5, 13, 0, 0).timestamp())
        page = {
            "items": [
                self._msg(start_ts + 60, from_id=1065504879, cmid=10, docs=["368333.pdf"]),
                self._msg(start_ts + 120, from_id=-224047547, cmid=11, docs=["368333.pdf"]),
                self._msg(start_ts + 180, from_id=-224047547, cmid=12, docs=["ИИ 368333.pdf"]),
                self._msg(start_ts + 240, from_id=500008234, cmid=13, docs=["other.docx"]),
            ]
        }
        self.processor._vk_api_call = Mock(return_value=page)

        with (
            patch.object(self.processor, "_vk_counter_peer_ids_for_authors", return_value=["2000000002"]),
            patch.object(self.processor, "_vk_local_counter_status_map", return_value={}),
            patch.object(self.processor, "_resolve_vk_counter_author_ids", return_value={1065504879}),
        ):
            await self.processor.analyze_vk_files_history_auto("1065504879", date_str="2026-05-13")

        sent_files = self.processor.generate_counter_report_simple.await_args.args[0]
        received_files = self.processor.generate_counter_report_simple.await_args.args[1]
        self.assertEqual(["368333.pdf"], [item["file_name"] for item in sent_files])
        self.assertEqual(["368333.pdf"], [item["file_name"] for item in received_files])

    async def test_vk_history_auto_counts_negative_group_author_id(self):
        start_ts = int(datetime(2026, 5, 15, 0, 0).timestamp())
        page = {
            "items": [
                self._msg(start_ts + 60, from_id=-224047547, cmid=10, docs=["bot_file.docx"]),
                self._msg(start_ts + 120, from_id=1065504879, cmid=11, docs=["user_file.docx"]),
            ]
        }
        self.processor._vk_api_call = Mock(return_value=page)

        with (
            patch.object(self.processor, "_vk_counter_peer_ids_for_authors", return_value=["2000000002"]),
            patch.object(self.processor, "_vk_local_counter_status_map", return_value={}),
        ):
            await self.processor.analyze_vk_files_history_auto("vk:-224047547", date_str="2026-05-15")

        sent_files = self.processor.generate_counter_report_simple.await_args.args[0]
        self.assertEqual(["bot_file.docx"], [item["file_name"] for item in sent_files])

    async def test_vk_history_auto_matches_same_negative_author_pdf_report(self):
        start_ts = int(datetime(2026, 5, 15, 0, 0).timestamp())
        page = {
            "items": [
                self._msg(start_ts + 60, from_id=-224047547, cmid=10, docs=["370831.pdf"]),
                self._msg(start_ts + 120, from_id=-224047547, cmid=11, docs=["370831.pdf"]),
                self._msg(start_ts + 180, from_id=-224047547, cmid=12, docs=["370831 анти.pdf"]),
                self._msg(start_ts + 240, from_id=-224047547, cmid=13, docs=["370831 анти.pdf"]),
            ]
        }
        self.processor._vk_api_call = Mock(return_value=page)

        with (
            patch.object(self.processor, "_vk_counter_peer_ids_for_authors", return_value=["2000000002"]),
            patch.object(self.processor, "_vk_local_counter_status_map", return_value={}),
        ):
            await self.processor.analyze_vk_files_history_auto("vk:-224047547", date_str="2026-05-15")

        sent_files = self.processor.generate_counter_report_simple.await_args.args[0]
        received_files = self.processor.generate_counter_report_simple.await_args.args[1]
        self.assertEqual(["370831.pdf", "370831 анти.pdf"], [item["file_name"] for item in sent_files])
        self.assertEqual(["370831.pdf", "370831 анти.pdf"], [item["file_name"] for item in received_files])

    async def test_vk_history_auto_matches_same_negative_author_docx_to_pdf_report(self):
        start_ts = int(datetime(2026, 5, 15, 0, 0).timestamp())
        page = {
            "items": [
                self._msg(start_ts + 60, from_id=-224047547, cmid=10, docs=["370831.docx"]),
                self._msg(start_ts + 120, from_id=-224047547, cmid=11, docs=["370831.pdf"]),
                self._msg(start_ts + 180, from_id=-224047547, cmid=12, docs=["370831 анти.docx"]),
                self._msg(start_ts + 240, from_id=-224047547, cmid=13, docs=["370831 анти.pdf"]),
            ]
        }
        self.processor._vk_api_call = Mock(return_value=page)

        with (
            patch.object(self.processor, "_vk_counter_peer_ids_for_authors", return_value=["2000000002"]),
            patch.object(self.processor, "_vk_local_counter_status_map", return_value={}),
        ):
            await self.processor.analyze_vk_files_history_auto("vk:-224047547", date_str="2026-05-15")

        sent_files = self.processor.generate_counter_report_simple.await_args.args[0]
        received_files = self.processor.generate_counter_report_simple.await_args.args[1]
        self.assertEqual(["370831.docx", "370831 анти.docx"], [item["file_name"] for item in sent_files])
        self.assertEqual(["370831.pdf", "370831 анти.pdf"], [item["file_name"] for item in received_files])

    async def test_vk_history_auto_matches_report_from_our_bot_by_caption_alias(self):
        start_ts = int(datetime(2026, 5, 15, 0, 0).timestamp())
        page = {
            "items": [
                self._msg(start_ts + 60, from_id=-224047547, cmid=10, docs=["370831.docx"]),
                self._msg(start_ts + 120, from_id=-234351996, cmid=11, docs=["vk_doc_999.pdf"], text="370831"),
                self._msg(start_ts + 180, from_id=-224047547, cmid=12, docs=["370831 анти.docx"]),
                self._msg(
                    start_ts + 240,
                    from_id=-234351996,
                    cmid=13,
                    docs=["random_report.pdf"],
                    text="Отчет: 370831 анти.pdf\nИсходный файл: 370831 анти.docx",
                ),
            ]
        }
        self.processor._vk_api_call = Mock(return_value=page)

        with (
            patch.object(self.processor, "_vk_counter_peer_ids_for_authors", return_value=["2000000002"]),
            patch.object(self.processor, "_vk_local_counter_status_map", return_value={}),
        ):
            await self.processor.analyze_vk_files_history_auto("vk:-224047547", date_str="2026-05-15")

        sent_files = self.processor.generate_counter_report_simple.await_args.args[0]
        received_files = self.processor.generate_counter_report_simple.await_args.args[1]
        self.assertEqual(["370831.docx", "370831 анти.docx"], [item["file_name"] for item in sent_files])
        self.assertEqual(["vk_doc_999.pdf", "random_report.pdf"], [item["file_name"] for item in received_files])
        self.assertIn("370831", received_files[0]["file_name_normalized_aliases"])
        self.assertIn("370831 анти", received_files[1]["file_name_normalized_aliases"])

    async def test_vk_history_auto_counts_multiple_docs_in_one_author_and_bot_message(self):
        start_ts = int(datetime(2026, 5, 22, 0, 0).timestamp())
        page = {
            "items": [
                self._msg(
                    start_ts + 60,
                    from_id=1106569752,
                    cmid=10,
                    docs=["375762.docx", "375762 анти.docx"],
                    text="375762",
                ),
                self._msg(
                    start_ts + 120,
                    from_id=-234351996,
                    cmid=11,
                    docs=["375762.pdf", "375762 анти.pdf"],
                    text="375762",
                ),
            ]
        }
        self.processor._vk_api_call = Mock(return_value=page)

        with (
            patch.object(self.processor, "_vk_counter_peer_ids_for_authors", return_value=["2000000002"]),
            patch.object(self.processor, "_vk_local_counter_status_map", return_value={}),
            patch.object(self.processor, "_resolve_vk_counter_author_ids", return_value={1106569752}),
        ):
            await self.processor.analyze_vk_files_history_auto("1106569752", date_str="2026-05-22")

        sent_files = self.processor.generate_counter_report_simple.await_args.args[0]
        received_files = self.processor.generate_counter_report_simple.await_args.args[1]
        self.assertEqual(["375762.docx", "375762 анти.docx"], [item["file_name"] for item in sent_files])
        self.assertEqual(["375762.pdf", "375762 анти.pdf"], [item["file_name"] for item in received_files])

    def test_vk_extract_docs_accepts_dict_attachments_from_vk_api(self):
        message = {
            "text": "375762",
            "attachments": {
                "0": {"type": "doc", "doc": {"owner_id": -1, "id": 1, "title": "375762.docx", "size": 100}},
                "1": {"type": "doc", "doc": {"owner_id": -1, "id": 2, "title": "375762 анти.docx", "size": 100}},
            },
        }

        docs = self.processor._extract_vk_docs(message)

        self.assertEqual(["375762.docx", "375762 анти.docx"], [item["file_name"] for item in docs])

    async def test_vk_history_auto_reads_pdf_reports_from_forwarded_messages(self):
        start_ts = int(datetime(2026, 5, 15, 0, 0).timestamp())
        forwarded_batch = self._msg(start_ts + 180, from_id=-224047547, cmid=12, docs=[], text="7 пересланных сообщений")
        forwarded_batch["fwd_messages"] = [
            self._msg(start_ts + 120, from_id=-224047547, cmid=101, docs=["370900.pdf"], text="370900"),
            self._msg(start_ts + 121, from_id=-224047547, cmid=102, docs=["370902.pdf"], text="370902"),
            {
                "date": start_ts + 122,
                "from_id": -224047547,
                "text": "",
                "attachments": [],
                "fwd_messages": [
                    self._msg(start_ts + 123, from_id=-224047547, cmid=103, docs=["370908.pdf"], text="370908")
                ],
            },
        ]
        page = {
            "items": [
                self._msg(start_ts + 60, from_id=-237224429, cmid=10, docs=["370900.docx"]),
                self._msg(start_ts + 61, from_id=-237224429, cmid=11, docs=["370902.docx"]),
                self._msg(start_ts + 62, from_id=-237224429, cmid=12, docs=["370908.docx"]),
                forwarded_batch,
            ]
        }
        self.processor._vk_api_call = Mock(return_value=page)

        with (
            patch.object(self.processor, "_vk_counter_peer_ids_for_authors", return_value=["2000000002"]),
            patch.object(self.processor, "_vk_local_counter_status_map", return_value={}),
        ):
            await self.processor.analyze_vk_files_history_auto("vk:-237224429", date_str="2026-05-15")

        sent_files = self.processor.generate_counter_report_simple.await_args.args[0]
        received_files = self.processor.generate_counter_report_simple.await_args.args[1]
        self.assertEqual(["370900.docx", "370902.docx", "370908.docx"], [item["file_name"] for item in sent_files])
        self.assertEqual(["370900.pdf", "370902.pdf", "370908.pdf"], [item["file_name"] for item in received_files])

    async def test_vk_history_auto_direct_scope_does_not_scan_known_chat_peers(self):
        start_ts = int(datetime(2026, 5, 12, 0, 0).timestamp())
        store = Mock()
        store.list_vk_peer_ids_by_sender.return_value = ["2000000001"]
        store.list_vk_jobs_by_sender.return_value = []
        self.processor._vk_api_call = Mock(
            return_value={"items": [self._msg(start_ts + 60, from_id=1065504879, cmid=5, docs=["direct.docx"])]}
        )

        def config_get(key, default=None):
            if key == "counter_vk_peer_id":
                return "2000000001"
            if key == "counter_vk_scope":
                return "direct"
            return default

        with (
            patch("bot_handlers.build_store", return_value=store),
            patch("bot_handlers.Config.get_setting", side_effect=config_get),
            patch.object(self.processor, "_resolve_vk_counter_author_ids", return_value={1065504879}),
        ):
            await self.processor.analyze_vk_files_history_auto("1065504879", date_str="2026-05-12")

        calls = self.processor._vk_api_call.call_args_list
        self.assertEqual(1, len(calls))
        self.assertEqual(1065504879, calls[0].args[1]["peer_id"])
        sent_files = self.processor.generate_counter_report_simple.await_args.args[0]
        self.assertEqual(["direct.docx"], [item["file_name"] for item in sent_files])

    async def test_vk_history_auto_retries_flood_control_and_continues(self):
        start_ts = int(datetime(2026, 5, 20, 0, 0).timestamp())
        self.processor._vk_api_call = Mock(
            side_effect=[
                VkApiError("messages.getHistory", {"error_code": 9, "error_msg": "Flood control"}),
                {"items": [self._msg(start_ts + 60, docs=["after-flood.docx"])]},
            ]
        )

        with (
            patch.object(self.processor, "_resolve_vk_counter_author_ids", return_value={101}),
            patch.object(self.processor, "_vk_counter_peer_ids_for_authors", return_value=["2000000002"]),
            patch.object(self.processor, "_vk_local_counter_status_map", return_value={}),
            patch("bot_handlers.asyncio.sleep", new=AsyncMock()) as sleep_mock,
            patch("builtins.print") as print_mock,
        ):
            await self.processor.analyze_vk_files_history_auto("101", date_str="2026-05-20")

        sleep_mock.assert_awaited_once_with(2)
        self.assertEqual(2, self.processor._vk_api_call.call_count)
        self.assertEqual(["after-flood.docx"], [item["file_name"] for item in self.processor.generate_counter_report_simple.await_args.args[0]])
        printed = " ".join(str(call.args[0]) for call in print_mock.call_args_list if call.args)
        self.assertIn("error_code=9", printed)
        self.assertIn("Доступ к истории VK восстановлен", printed)

    async def test_vk_history_auto_retries_too_many_requests_and_continues(self):
        start_ts = int(datetime(2026, 5, 20, 0, 0).timestamp())
        self.processor._vk_api_call = Mock(
            side_effect=[
                VkApiError("messages.getHistory", {"error_code": 6, "error_msg": "Too many requests per second"}),
                {"items": [self._msg(start_ts + 60, docs=["after-rate-limit.docx"])]},
            ]
        )

        with (
            patch.object(self.processor, "_resolve_vk_counter_author_ids", return_value={101}),
            patch.object(self.processor, "_vk_counter_peer_ids_for_authors", return_value=["2000000002"]),
            patch.object(self.processor, "_vk_local_counter_status_map", return_value={}),
            patch("bot_handlers.asyncio.sleep", new=AsyncMock()) as sleep_mock,
        ):
            await self.processor.analyze_vk_files_history_auto("101", date_str="2026-05-20")

        sleep_mock.assert_awaited_once_with(2)
        self.assertEqual(["after-rate-limit.docx"], [item["file_name"] for item in self.processor.generate_counter_report_simple.await_args.args[0]])

    async def test_vk_history_auto_flood_control_after_retries_is_error_without_report(self):
        error = VkApiError("messages.getHistory", {"error_code": 9, "error_msg": "Flood control"})
        self.processor._vk_api_call = Mock(side_effect=[error, error, error, error])

        with (
            patch.object(self.processor, "_resolve_vk_counter_author_ids", return_value={101}),
            patch.object(self.processor, "_vk_counter_peer_ids_for_authors", return_value=["2000000002"]),
            patch.object(self.processor, "_vk_local_counter_status_map", return_value={}),
            patch("bot_handlers.asyncio.sleep", new=AsyncMock()) as sleep_mock,
            patch("builtins.print") as print_mock,
        ):
            await self.processor.analyze_vk_files_history_auto("101", date_str="2026-05-20")

        self.assertEqual([call.args[0] for call in sleep_mock.await_args_list], [2, 5, 10])
        self.assertEqual(4, self.processor._vk_api_call.call_count)
        self.processor.generate_counter_report_simple.assert_not_awaited()
        printed = " ".join(str(call.args[0]) for call in print_mock.call_args_list if call.args)
        self.assertIn("Не удалось получить историю VK-беседы 2000000002", printed)
        self.assertIn("error_code=9", printed)
        self.assertNotIn("Просмотрено сообщений в периоде: 0", printed)

    async def test_vk_history_auto_access_denied_is_not_retried(self):
        self.processor._vk_api_call = Mock(
            side_effect=VkApiError("messages.getHistory", {"error_code": 15, "error_msg": "Access denied"})
        )

        with (
            patch.object(self.processor, "_resolve_vk_counter_author_ids", return_value={101}),
            patch.object(self.processor, "_vk_counter_peer_ids_for_authors", return_value=["2000000002"]),
            patch.object(self.processor, "_vk_local_counter_status_map", return_value={}),
            patch("bot_handlers.asyncio.sleep", new=AsyncMock()) as sleep_mock,
            patch("builtins.print") as print_mock,
        ):
            await self.processor.analyze_vk_files_history_auto("101", date_str="2026-05-20")

        sleep_mock.assert_not_awaited()
        self.processor._vk_api_call.assert_called_once()
        self.processor.generate_counter_report_simple.assert_not_awaited()
        printed = " ".join(str(call.args[0]) for call in print_mock.call_args_list if call.args)
        self.assertIn("Нет доступа к истории VK-беседы 2000000002", printed)

    async def test_vk_history_auto_missing_counter_token_is_explicit_error(self):
        self.processor._get_vk_api_token = Mock(side_effect=RuntimeError("VK Counter/User Token не задан"))
        self.processor._vk_api_call = CounterModeProcessor._vk_api_call.__get__(self.processor)

        with (
            patch.object(self.processor, "_resolve_vk_counter_author_ids", return_value={101}),
            patch.object(self.processor, "_vk_counter_peer_ids_for_authors", return_value=["2000000002"]),
            patch.object(self.processor, "_vk_local_counter_status_map", return_value={}),
            patch("builtins.print") as print_mock,
        ):
            await self.processor.analyze_vk_files_history_auto("101", date_str="2026-05-20")

        self.processor.generate_counter_report_simple.assert_not_awaited()
        printed = " ".join(str(call.args[0]) for call in print_mock.call_args_list if call.args)
        self.assertIn("Для VK-беседы 2000000002 нужен VK Counter/User Token", printed)

    async def test_vk_history_auto_successful_empty_history_is_zero_report(self):
        self.processor._vk_api_call = Mock(return_value={"items": []})

        with (
            patch.object(self.processor, "_resolve_vk_counter_author_ids", return_value={101}),
            patch.object(self.processor, "_vk_counter_peer_ids_for_authors", return_value=["2000000002"]),
            patch.object(self.processor, "_vk_local_counter_status_map", return_value={}),
            patch("builtins.print") as print_mock,
        ):
            await self.processor.analyze_vk_files_history_auto("101", date_str="2026-05-20")

        self.processor.generate_counter_report_simple.assert_awaited_once()
        self.assertEqual([], self.processor.generate_counter_report_simple.await_args.args[0])
        printed = " ".join(str(call.args[0]) for call in print_mock.call_args_list if call.args)
        self.assertIn("Просмотрено сообщений в периоде: 0", printed)

    async def test_vk_history_auto_retries_paginated_page_without_duplicates(self):
        start_ts = int(datetime(2026, 5, 20, 0, 0).timestamp())
        first_page = {
            "items": [self._msg(start_ts + 1000 + index, cmid=index) for index in range(199)]
            + [self._msg(start_ts + 1200, cmid=199, docs=["first-page.docx"])],
        }
        second_page = {"items": [self._msg(start_ts + 60, cmid=200, docs=["second-page.docx"])]}
        self.processor._vk_api_call = Mock(
            side_effect=[
                first_page,
                VkApiError("messages.getHistory", {"error_code": 9, "error_msg": "Flood control"}),
                second_page,
            ]
        )

        with (
            patch.object(self.processor, "_resolve_vk_counter_author_ids", return_value={101}),
            patch.object(self.processor, "_vk_counter_peer_ids_for_authors", return_value=["2000000002"]),
            patch.object(self.processor, "_vk_local_counter_status_map", return_value={}),
            patch("bot_handlers.asyncio.sleep", new=AsyncMock()) as sleep_mock,
        ):
            await self.processor.analyze_vk_files_history_auto("101", date_str="2026-05-20")

        sleep_mock.assert_awaited_once_with(2)
        calls = self.processor._vk_api_call.call_args_list
        self.assertEqual([0, 200, 200], [call.args[1]["offset"] for call in calls])
        sent_files = self.processor.generate_counter_report_simple.await_args.args[0]
        self.assertEqual(["second-page.docx", "first-page.docx"], [item["file_name"] for item in sent_files])
        self.assertEqual(2, len({item["file_key"] for item in sent_files}))

    def test_vk_longpoll_counter_datetime_falls_back_to_created_at(self):
        job = self._job(
            "fallback.docx",
            ts=None,
            created_at="2026-04-30T12:34:56",
        )
        job.transport_meta = {}

        result = self.processor._job_counter_datetime(job)

        self.assertEqual(datetime(2026, 4, 30, 12, 34, 56), result)


class TestDeliverDocumentToOrigin(unittest.TestCase):
    """Fix #1: Telegram spool fallback."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.file_path = Path(self.temp_dir.name) / "result.pdf"
        self.file_path.write_bytes(b"pdf content")
        self.handlers = _make_handlers()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_telegram_success(self):
        """Normal Telegram delivery should succeed."""
        mock_client = AsyncMock()
        mock_client.is_connected = True
        mock_client.send_document = AsyncMock(return_value=MagicMock())
        self.handlers.manager.get_client.return_value = mock_client

        context = {
            "source_platform": "telegram",
            "author_id": "12345",
            "author": "testuser",
        }

        result = asyncio.run(
            self.handlers._deliver_document_to_origin(
                context, str(self.file_path), file_name="result.pdf",
                telegram_client=mock_client,
            )
        )
        self.assertEqual(result["platform"], "telegram")
        self.assertNotIn("status", result)  # no "spooled" status
        mock_client.send_document.assert_called_once()

    def test_telegram_spool_on_persistent_failure(self):
        """Fix #1: After 2 Telegram failures, should spool to outbox."""
        mock_client = AsyncMock()
        mock_client.is_connected = True
        mock_client.send_document = AsyncMock(side_effect=asyncio.TimeoutError())
        self.handlers.manager.get_client.return_value = mock_client

        mock_spool = MagicMock(return_value={"id": "spool-001"})
        self.handlers.outbound_dispatcher.outbox.spool_delivery = mock_spool

        context = {
            "source_platform": "telegram",
            "author_id": "12345",
            "route_chat_id": "67890",
        }

        result = asyncio.run(
            self.handlers._deliver_document_to_origin(
                context, str(self.file_path), file_name="result.pdf",
                telegram_client=mock_client,
            )
        )
        self.assertEqual(result["status"], "spooled")
        self.assertEqual(result["outbox_id"], "spool-001")
        mock_spool.assert_called_once()

    def test_telegram_floodwait_short_retries(self):
        """FloodWait with short wait should retry and succeed."""
        mock_client = AsyncMock()
        mock_client.is_connected = True
        # First call raises FloodWait(1), second succeeds
        mock_client.send_document = AsyncMock(
            side_effect=[_FakeFloodWait(1), MagicMock()]
        )
        self.handlers.manager.get_client.return_value = mock_client

        context = {"source_platform": "telegram", "author_id": "123"}

        result = asyncio.run(
            self.handlers._deliver_document_to_origin(
                context, str(self.file_path), file_name="result.pdf",
                telegram_client=mock_client,
            )
        )
        self.assertEqual(result["platform"], "telegram")
        self.assertEqual(mock_client.send_document.call_count, 2)

    def test_vk_routing(self):
        """VK platform should go through outbound_dispatcher with caption in document."""
        self.handlers.outbound_dispatcher.send_result = MagicMock(
            return_value={"platform": "vk", "status": "sent"}
        )

        context = {
            "source_platform": "vk",
            "route_chat_id": "100",
            "route_sender_id": "200",
        }

        result = asyncio.run(
            self.handlers._deliver_document_to_origin(
                context, str(self.file_path), file_name="result.pdf"
            )
        )
        self.assertEqual(result["platform"], "vk")
        self.handlers.outbound_dispatcher.send_result.assert_called_once()
        self.assertEqual(self.handlers.outbound_dispatcher.send_result.call_args.args[3], "result")

    def test_vk_routing_sends_ai_name_text_before_document(self):
        self.handlers.outbound_dispatcher.send_result = MagicMock(
            return_value={"platform": "vk", "status": "sent"}
        )

        context = {
            "source_platform": "vk",
            "route_chat_id": "100",
            "route_sender_id": "200",
        }

        asyncio.run(
            self.handlers._deliver_document_to_origin(
                context, str(self.file_path), file_name="ИИ курсовая работа (5).pdf"
            )
        )

        self.assertEqual(
            self.handlers.outbound_dispatcher.send_result.call_args.args[3],
            "ИИ курсовая работа (5)"
        )

    def test_vk_routing_accepts_integer_response(self):
        """VK messages.send may return a plain integer message id."""
        self.handlers.outbound_dispatcher.send_result = MagicMock(return_value=123456)

        context = {
            "source_platform": "vk",
            "route_chat_id": "100",
            "route_sender_id": "200",
        }

        result = asyncio.run(
            self.handlers._deliver_document_to_origin(
                context, str(self.file_path), file_name="result.pdf"
            )
        )
        self.assertEqual("vk", result["platform"])
        self.assertEqual("sent", result["status"])
        self.assertEqual(123456, result["response"])

    def test_vk_delivery_retries_before_success(self):
        self.handlers.outbound_dispatcher.send_result = MagicMock(
            side_effect=[RuntimeError("timeout"), RuntimeError("temporary"), {"platform": "vk", "status": "sent"}]
        )

        context = {
            "source_platform": "vk",
            "route_chat_id": "100",
            "route_sender_id": "200",
        }

        with patch("asyncio.sleep", new=AsyncMock()):
            result = asyncio.run(
                self.handlers._deliver_document_to_origin(
                    context, str(self.file_path), file_name="result.pdf"
                )
            )

        self.assertEqual("sent", result["status"])
        self.assertEqual(3, self.handlers.outbound_dispatcher.send_result.call_count)
        self.assertEqual(self.handlers.outbound_dispatcher.send_result.call_args.args[3], "result")


class TestProcessingStateMachine(unittest.TestCase):
    """Tests for _try_start_processing and _finish_processing."""

    def setUp(self):
        self.handlers = _make_handlers()

    def test_try_start_processing_new_file(self):
        """New file should be accepted for processing."""
        result = asyncio.run(self.handlers._try_start_processing("file_uid_1"))
        self.assertTrue(result)
        self.assertEqual(self.handlers.processed_files["file_uid_1"], "IN_PROGRESS")

    def test_try_start_processing_duplicate(self):
        """Already processing file should be rejected."""
        asyncio.run(self.handlers._try_start_processing("file_uid_1"))
        result = asyncio.run(self.handlers._try_start_processing("file_uid_1"))
        self.assertFalse(result)

    def test_finish_processing_success(self):
        """Finishing with success should set DONE."""
        asyncio.run(self.handlers._try_start_processing("file_uid_1"))
        # _finish_processing uses asyncio.create_task, so must be run inside a loop
        async def _do():
            self.handlers._finish_processing("file_uid_1", success=True)
        asyncio.run(_do())
        self.assertEqual(self.handlers.processed_files["file_uid_1"], "DONE")

    def test_finish_processing_failure(self):
        """Finishing with failure should set FAILED."""
        asyncio.run(self.handlers._try_start_processing("file_uid_1"))
        async def _do():
            self.handlers._finish_processing("file_uid_1", success=False)
        asyncio.run(_do())
        self.assertEqual(self.handlers.processed_files["file_uid_1"], "FAILED")

    def test_try_start_after_done_rejected(self):
        """Re-processing an already done file should be rejected."""
        asyncio.run(self.handlers._try_start_processing("file_uid_1"))
        async def _do():
            self.handlers._finish_processing("file_uid_1", success=True)
        asyncio.run(_do())
        result = asyncio.run(self.handlers._try_start_processing("file_uid_1"))
        self.assertFalse(result)


class TestAccountManagerProcessingCounts(unittest.TestCase):
    def test_duplicate_file_names_count_as_separate_processing_items(self):
        from config import AccountManager

        manager = AccountManager()
        manager.mark_account_busy("НИК-2", "same.docx")
        manager.mark_account_busy("НИК-2", "same.docx")

        self.assertEqual(["same.docx", "same.docx"], manager.processing_files["НИК-2"])

        manager.mark_account_free("НИК-2", "same.docx")
        self.assertEqual(["same.docx"], manager.processing_files["НИК-2"])


class TestFileInfoRouting(unittest.TestCase):
    """Test that file_info correctly carries routing information for VK↔Telegram."""

    def test_vk_file_info_has_routing_fields(self):
        """VK file_info from legacy bridge should have all required routing fields."""
        from multichannel_gateway.integrations.legacy_bridge import job_to_legacy_file_info
        from multichannel_gateway.core.models import JobRecord

        job = JobRecord(
            job_id=1,
            source="vk",
            dedupe_key="vk:100:200:0",
            chat_id="100",
            sender_id="42",
            sender_name="VK User",
            message_id="200",
            reply_to_message_id=None,
            text=None,
            attachment_index=0,
            original_file_name="test.docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            file_path="/tmp/test.docx",
            file_sha256="abc123",
            file_size_bytes=1024,
            status="queued",
            attempts=0,
            last_error=None,
            locked_by=None,
            created_at="2026-03-24T00:00:00",
            updated_at="2026-03-24T00:00:00",
            result_path=None,
            transport_meta={},
        )

        file_info = job_to_legacy_file_info(job, is_anti=False)

        # Verify all routing fields exist
        self.assertEqual(file_info["source_platform"], "vk")
        self.assertEqual(file_info["route_chat_id"], "100")
        self.assertEqual(file_info["route_sender_id"], "42")
        self.assertEqual(file_info["gateway_job_id"], 1)
        self.assertEqual(file_info["local_path"], "/tmp/test.docx")
        self.assertFalse(file_info["is_anti"])
        self.assertIsNone(file_info["message"])  # VK has no pyrogram message

    def test_vk_file_info_anti_flag(self):
        """Anti file info should have is_anti=True."""
        from multichannel_gateway.integrations.legacy_bridge import job_to_legacy_file_info
        from multichannel_gateway.core.models import JobRecord

        job = JobRecord(
            job_id=2,
            source="vk",
            dedupe_key="vk:100:201:0",
            chat_id="100",
            sender_id="42",
            sender_name="VK User",
            message_id="201",
            reply_to_message_id=None,
            text=None,
            attachment_index=0,
            original_file_name="анти_test.docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            file_path="/tmp/анти_test.docx",
            file_sha256="def456",
            file_size_bytes=2048,
            status="queued",
            attempts=0,
            last_error=None,
            locked_by=None,
            created_at="2026-03-24T00:00:00",
            updated_at="2026-03-24T00:00:00",
            result_path=None,
            transport_meta={},
        )

        file_info = job_to_legacy_file_info(job, is_anti=True)
        self.assertTrue(file_info["is_anti"])
        self.assertEqual(file_info["original_file_name"], "анти_test.docx")


class TestMode2WorkingHours(unittest.TestCase):
    @staticmethod
    def _fixed_datetime(hour: int, minute: int = 0, second: int = 0):
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 5, 6, hour, minute, second)

        return FixedDateTime

    def _check_at(self, hour: int, minute: int, expected: bool, second: int = 0):
        handlers = _make_handlers()
        settings = {
            "mode": "mode2",
            "time_start": "09:00",
            "time_end": "21:00",
        }
        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None: settings.get(key, default)), \
             patch("bot_handlers.datetime", self._fixed_datetime(hour, minute, second)):
            self.assertIs(handlers.is_within_working_hours(), expected)

    def test_mode2_time_start_is_inside_working_hours(self):
        self._check_at(9, 0, True)

    def test_mode2_time_end_is_inside_working_hours(self):
        self._check_at(21, 0, True)

    def test_mode2_time_end_minute_is_inside_working_hours(self):
        self._check_at(21, 0, True, second=59)

    def test_mode2_before_start_is_outside_working_hours(self):
        self._check_at(8, 59, False)

    def test_mode2_after_end_is_outside_working_hours(self):
        self._check_at(21, 1, False)

    def test_mode2_bad_time_config_fails_open(self):
        handlers = _make_handlers()
        settings = {
            "mode": "mode2",
            "time_start": "bad",
            "time_end": "21:00",
        }
        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None: settings.get(key, default)):
            self.assertTrue(handlers.is_within_working_hours())

    def test_mode2_limit_resets_for_current_work_interval(self):
        handlers = _make_handlers()
        handlers.daily_counter = {"count": 10, "date": datetime(2026, 5, 6).date()}
        settings = {
            "mode": "mode2",
            "time_start": "17:00",
            "time_end": "17:20",
            "max_files_per_day": 5,
        }
        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None: settings.get(key, default)), \
             patch("bot_handlers.datetime", self._fixed_datetime(17, 0)):
            self.assertTrue(handlers.check_daily_limit())
            self.assertEqual(0, handlers.daily_counter["count"])
            handlers._increment_mode2_limit_counter()
            self.assertEqual(1, handlers.daily_counter["count"])

    def test_mode2_limit_blocks_only_after_interval_count_reaches_max(self):
        handlers = _make_handlers()
        settings = {
            "mode": "mode2",
            "time_start": "17:00",
            "time_end": "17:20",
            "max_files_per_day": 5,
        }
        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None: settings.get(key, default)), \
             patch("bot_handlers.datetime", self._fixed_datetime(17, 10)):
            for _ in range(5):
                self.assertTrue(handlers.check_daily_limit())
                handlers._increment_mode2_limit_counter()
            self.assertFalse(handlers.check_daily_limit())


class TestFilesTodaySourceCounters(unittest.TestCase):
    def test_counts_telegram_vk_and_max_without_mixing_sources(self):
        handlers = _make_handlers()

        handlers._increment_files_today("telegram")
        handlers._increment_files_today("vk")
        handlers._increment_files_today("telegram")
        handlers._increment_files_today("max")

        self.assertEqual(4, handlers.files_today["count"])
        self.assertEqual(2, handlers.files_today["by_source"]["telegram"])
        self.assertEqual(1, handlers.files_today["by_source"]["vk"])
        self.assertEqual(1, handlers.files_today["by_source"]["max"])

    def test_unknown_source_gets_own_bucket(self):
        handlers = _make_handlers()

        handlers._increment_files_today("custom")

        self.assertEqual(1, handlers.files_today["count"])
        self.assertEqual(1, handlers.files_today["by_source"]["custom"])


class TestCleanupOldEditorTracking(unittest.TestCase):
    """Fix #2: Verify only the safe version of cleanup_old_editor_tracking exists."""

    def test_cleanup_removes_old_entries(self):
        """Entries older than 24h should be removed."""
        from datetime import timedelta
        handlers = _make_handlers()
        handlers.editor_tracking = {
            "old_msg": {
                "sent_at": datetime.now() - timedelta(hours=25),
                "author": "test",
            },
            "new_msg": {
                "sent_at": datetime.now() - timedelta(hours=1),
                "author": "test2",
            },
        }
        handlers.cleanup_old_editor_tracking()

        self.assertNotIn("old_msg", handlers.editor_tracking)
        self.assertIn("new_msg", handlers.editor_tracking)

    def test_cleanup_handles_non_datetime_sent_at(self):
        """The safe version should handle non-datetime sent_at gracefully."""
        handlers = _make_handlers()
        handlers.editor_tracking = {
            "bad_entry": {
                "sent_at": "2024-01-01",
                "author": "test",
            },
        }
        # Should not crash
        handlers.cleanup_old_editor_tracking()
        # Non-datetime entry should be kept (not crash)
        self.assertIn("bad_entry", handlers.editor_tracking)


class TestMessageClassifiers(unittest.TestCase):
    """Test static classifiers for AAA bot message types."""

    def test_no_checks_messages(self):
        self.assertTrue(BotHandlers._is_no_checks_message(
            "❌ У вас пока нет проверок. Для покупки нажмите команду /pay 💳"
        ))
        self.assertTrue(BotHandlers._is_no_checks_message(
            "Проверка завершена. У вас не осталось больше проверок"
        ))
        self.assertFalse(BotHandlers._is_no_checks_message(
            "✅ Проверка завершена! Оригинальность: 85%"
        ))
        self.assertFalse(BotHandlers._is_no_checks_message(None))
        self.assertFalse(BotHandlers._is_no_checks_message(""))

    def test_global_aaa_unavailable(self):
        self.assertTrue(BotHandlers._is_global_aaa_unavailable_message(
            "В настоящее время нет доступных проверок"
        ))
        self.assertTrue(BotHandlers._is_global_aaa_unavailable_message(
            "❗ В настоящее время нет доступных проверок.\n"
            "Следующая проверка будет доступна после: неизвестно."
        ))
        self.assertFalse(BotHandlers._is_global_aaa_unavailable_message(
            "У вас осталось 5 проверок"
        ))

    def test_ai_upload_prompt(self):
        self.assertTrue(BotHandlers._is_ai_upload_prompt(
            "Пожалуйста, загрузите файл для проверки"
        ))
        self.assertTrue(BotHandlers._is_ai_upload_prompt(
            "У вас осталось 3 проверки. Допустимые форматы: .doc .docx. Максимальный размер: 20МБ"
        ))
        self.assertFalse(BotHandlers._is_ai_upload_prompt(
            "Ваш файл загружен на проверку"
        ))


class TestAaaUploadFilename(unittest.TestCase):
    def test_process_normal_file_preserves_original_filename_for_aaa_upload(self):
        temp_dir = tempfile.TemporaryDirectory()
        work_file = Path(temp_dir.name) / "gateway-copy.docx"
        work_file.write_bytes(b"test content")

        manager = _make_account_manager()
        manager.get_available_account.return_value = "AAA-1"
        manager.mark_account_busy = Mock()
        manager.mark_account_free = Mock()

        mock_client = AsyncMock()
        mock_client.name = "AAA-1"
        mock_client.send_message = AsyncMock()
        mock_client.send_document = AsyncMock(
            return_value=SimpleNamespace(
                id=777,
                chat=SimpleNamespace(id=555),
            )
        )
        manager.clients = {"AAA-1": mock_client}

        config_values = {
            "ai_bot": "@AAA_Report_AIBot",
            "ai_upload_prompt_attempts": 2,
        }

        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None: config_values.get(key, default)):
            handlers = BotHandlers(manager)

        handlers._ensure_work_file = AsyncMock(return_value=str(work_file))
        handlers._wait_for_ai_upload_prompt = AsyncMock(return_value=True)
        handlers.processor.cleanup_temp_files = Mock()

        file_info = {
            "author": "vk-user",
            "author_id": "42",
            "file_name": "gateway-copy.docx",
            "original_file_name": "Заказ 12345.docx",
            "chat_id": "100",
            "message_id": "200",
            "message": None,
            "local_path": str(work_file),
            "source_platform": "vk",
            "route_chat_id": "100",
            "route_sender_id": "42",
        }

        try:
            with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None: config_values.get(key, default)):
                asyncio.run(handlers.process_normal_file(file_info))
        finally:
            temp_dir.cleanup()

        mock_client.send_document.assert_awaited_once()
        _, kwargs = mock_client.send_document.await_args
        self.assertEqual("@AAA_Report_AIBot", kwargs["chat_id"])
        self.assertEqual(str(work_file), kwargs["document"])
        self.assertEqual("Заказ 12345.docx", kwargs["file_name"])

    def test_four_normal_accounts_available_when_config_has_four(self):
        manager = _make_account_manager()
        with patch("bot_handlers.Config.get_setting", return_value=["AAA-1", "AAA-2", "AAA-3", "AAA-4"]):
            handlers = BotHandlers(manager)
            self.assertEqual(["AAA-1", "AAA-2", "AAA-3", "AAA-4"], handlers._get_normal_accounts())


class TestRecoveryAndEditorRouting(unittest.TestCase):
    def setUp(self):
        self.handlers = _make_handlers()

    def test_requeue_after_missing_ai_prompt_preserves_vk_route(self):
        temp_dir = tempfile.TemporaryDirectory()
        temp_path = Path(temp_dir.name) / "work.docx"
        temp_path.write_bytes(b"payload")

        self.handlers.current_processing_files["file-key"] = {"temp_path": str(temp_path)}
        self.handlers.manager.mark_account_free = Mock()
        self.handlers._delayed_process_queue = AsyncMock()

        file_info = {
            "author": "vk-user",
            "author_id": "42",
            "file_name": "work.docx",
            "original_file_name": "Заказ.docx",
            "message_id": "200",
            "chat_id": "100",
            "message": None,
            "file_uid": "vk:100:200:0",
            "local_path": "/tmp/source.docx",
            "gateway_job_id": 77,
            "source_platform": "vk",
            "route_sender_id": "42",
            "route_chat_id": "100",
        }

        with patch("bot_handlers.Config.get_setting", return_value=30):
            try:
                asyncio.run(self.handlers._requeue_after_missing_ai_prompt("AAA-1", "file-key", file_info, str(temp_path)))
            finally:
                temp_dir.cleanup()

        self.assertEqual(1, len(self.handlers.manager.file_queue))
        queued = self.handlers.manager.file_queue[0]
        self.assertEqual("vk", queued["source_platform"])
        self.assertEqual("42", queued["route_sender_id"])
        self.assertEqual("100", queued["route_chat_id"])
        self.assertEqual(77, queued["gateway_job_id"])
        self.assertEqual(1, queued["ai_prompt_retry_count"])
        self.assertNotIn("file-key", self.handlers.current_processing_files)
        self.assertFalse(temp_path.exists())

    def test_handle_global_aaa_unavailable_forwards_vk_routing_to_editor(self):
        self.handlers._activate_global_aaa_unavailable = Mock()
        self.handlers.send_to_nik2 = AsyncMock()
        self.handlers.cleanup_account_mappings = Mock()
        self.handlers.process_queue = AsyncMock()

        processing_info = {
            "author": "vk-user",
            "author_id": "42",
            "chat_id": "100",
            "original_file_name": "Заказ.docx",
            "message": None,
            "file_uid": "vk:100:200:0",
            "local_path": "/tmp/source.docx",
            "gateway_job_id": 77,
            "source_platform": "vk",
            "route_sender_id": "42",
            "route_chat_id": "100",
        }
        self.handlers.current_processing_files["file-key"] = processing_info.copy()

        client = SimpleNamespace(name="AAA-1")
        message = SimpleNamespace(id=10)

        asyncio.run(self.handlers._handle_global_aaa_unavailable(client, message, processing_info, "file-key"))

        self.handlers.send_to_nik2.assert_awaited_once()
        forwarded = self.handlers.send_to_nik2.await_args.args[0]
        self.assertEqual("vk", forwarded["source_platform"])
        self.assertEqual("42", forwarded["route_sender_id"])
        self.assertEqual("100", forwarded["route_chat_id"])
        self.assertNotIn("file-key", self.handlers.current_processing_files)

    def test_send_to_nik2_mode2_uses_editor_24_7_for_247_reasons_and_preserves_route(self):
        manager = _make_account_manager()
        client_nik2 = AsyncMock()
        client_nik2.send_document = AsyncMock(return_value=SimpleNamespace(id=555, chat=SimpleNamespace(id=777)))
        manager.get_client.return_value = client_nik2
        handlers = _make_handlers(manager)
        handlers._ensure_work_file = AsyncMock(return_value="/tmp/editor.docx")

        settings = {
            "mode": "mode2",
            "editor_24_7": "@editor247",
            "editor_nickname": "@work_editor",
        }
        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None: settings.get(key, default)):
            asyncio.run(
                handlers.send_to_nik2(
                    {
                        "author": "vk-user",
                        "author_id": "42",
                        "file_name": "doc.docx",
                        "original_file_name": "doc.docx",
                        "file_uid": "vk:100:1:0",
                        "source_platform": "vk",
                        "route_sender_id": "42",
                        "route_chat_id": "100",
                    },
                    reason="вне рабочего времени",
                )
            )

        client_nik2.send_document.assert_awaited_once()
        _, kwargs = client_nik2.send_document.await_args
        self.assertEqual("@editor247", kwargs["chat_id"])
        tracking = handlers.editor_tracking["no_checks_555"]
        self.assertEqual("vk", tracking["source_platform"])
        self.assertEqual("42", tracking["route_sender_id"])
        self.assertEqual("100", tracking["route_chat_id"])

    def test_send_to_nik2_mode2_uses_work_editor_for_working_hours_editor_route(self):
        manager = _make_account_manager()
        client_nik2 = AsyncMock()
        client_nik2.send_document = AsyncMock(return_value=SimpleNamespace(id=556, chat=SimpleNamespace(id=778)))
        manager.get_client.return_value = client_nik2
        handlers = _make_handlers(manager)
        handlers._ensure_work_file = AsyncMock(return_value="/tmp/work-editor.docx")

        settings = {
            "mode": "mode2",
            "editor_24_7": "@editor247",
            "editor_nickname": "@work_editor",
        }
        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None: settings.get(key, default)):
            asyncio.run(
                handlers.send_to_nik2(
                    {
                        "author": "tg-user",
                        "author_id": "42",
                        "file_name": "doc.docx",
                        "original_file_name": "doc.docx",
                        "file_uid": "telegram:100:1:0",
                        "source_platform": "telegram",
                        "route_sender_id": "42",
                        "route_chat_id": "100",
                    },
                    reason="обычные файлы направляются редактору",
                )
            )

        client_nik2.send_document.assert_awaited_once()
        _, kwargs = client_nik2.send_document.await_args
        self.assertEqual("@work_editor", kwargs["chat_id"])
        tracking = handlers.editor_tracking["no_checks_556"]
        self.assertEqual("@work_editor", tracking["destination"])

    def test_send_to_nik2_mode2_routes_to_247_when_work_editor_unavailable(self):
        manager = _make_account_manager()
        client_nik2 = AsyncMock()
        client_nik2.send_document = AsyncMock(return_value=SimpleNamespace(id=558, chat=SimpleNamespace(id=779)))
        manager.get_client.return_value = client_nik2
        handlers = _make_handlers(manager)
        handlers._ensure_work_file = AsyncMock(return_value="/tmp/work-editor.docx")
        handlers._mark_editor_unavailable("@work_editor")

        settings = {
            "mode": "mode2",
            "editor_24_7": "@editor247",
            "editor_nickname": "@work_editor",
        }
        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None: settings.get(key, default)):
            asyncio.run(
                handlers.send_to_nik2(
                    {
                        "author": "tg-user",
                        "author_id": "42",
                        "file_name": "doc.docx",
                        "original_file_name": "doc.docx",
                        "file_uid": "telegram:100:1:0",
                        "source_platform": "telegram",
                    },
                    reason="обычные файлы направляются редактору",
                )
            )

        client_nik2.send_document.assert_awaited_once()
        _, kwargs = client_nik2.send_document.await_args
        self.assertEqual("@editor247", kwargs["chat_id"])
        tracking = handlers.editor_tracking["no_checks_558"]
        self.assertEqual("@editor247", tracking["destination"])

    def test_send_to_nik2_mode1_uses_normal_editor_nickname_for_plain_files(self):
        manager = _make_account_manager()
        client_nik2 = AsyncMock()
        client_nik2.send_document = AsyncMock(return_value=SimpleNamespace(id=557, chat=SimpleNamespace(id=778)))
        manager.get_client.return_value = client_nik2
        handlers = _make_handlers(manager)
        handlers._ensure_work_file = AsyncMock(return_value="/tmp/normal-editor.docx")

        settings = {
            "mode": "mode1",
            "editor_nickname": "@legacy_editor",
            "anti_editor_nickname": "@anti_editor",
            "normal_editor_nickname": "@normal_editor",
        }
        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None: settings.get(key, default)):
            asyncio.run(
                handlers.send_to_nik2(
                    {
                        "author": "tg-user",
                        "author_id": "42",
                        "file_name": "doc.docx",
                        "original_file_name": "doc.docx",
                        "is_anti": False,
                        "file_uid": "telegram:100:1:0",
                        "source_platform": "telegram",
                    },
                    reason="обычные файлы направляются редактору",
                )
            )

        client_nik2.send_document.assert_awaited_once()
        _, kwargs = client_nik2.send_document.await_args
        self.assertEqual("@normal_editor", kwargs["chat_id"])
        tracking = handlers.editor_tracking["no_checks_557"]
        self.assertEqual("@normal_editor", tracking["destination"])

    def test_send_to_nik2_mode1_routes_to_247_when_normal_editor_unavailable(self):
        manager = _make_account_manager()
        client_nik2 = AsyncMock()
        client_nik2.send_document = AsyncMock(return_value=SimpleNamespace(id=559, chat=SimpleNamespace(id=780)))
        manager.get_client.return_value = client_nik2
        handlers = _make_handlers(manager)
        handlers._ensure_work_file = AsyncMock(return_value="/tmp/normal-editor.docx")
        handlers._mark_editor_unavailable("@normal_editor")

        settings = {
            "mode": "mode1",
            "editor_24_7": "@editor247",
            "editor_nickname": "@legacy_editor",
            "anti_editor_nickname": "@anti_editor",
            "normal_editor_nickname": "@normal_editor",
        }
        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None: settings.get(key, default)):
            asyncio.run(
                handlers.send_to_nik2(
                    {
                        "author": "tg-user",
                        "author_id": "42",
                        "file_name": "doc.docx",
                        "original_file_name": "doc.docx",
                        "is_anti": False,
                        "file_uid": "telegram:100:1:0",
                        "source_platform": "telegram",
                    },
                    reason="обычные файлы направляются редактору",
                )
            )

        client_nik2.send_document.assert_awaited_once()
        _, kwargs = client_nik2.send_document.await_args
        self.assertEqual("@editor247", kwargs["chat_id"])
        tracking = handlers.editor_tracking["no_checks_559"]
        self.assertEqual("@editor247", tracking["destination"])

    def test_handle_no_more_checks_requeues_vk_file_when_other_accounts_alive(self):
        self.handlers.manager.blacklisted_accounts = ["AAA-2"]
        self.handlers.manager.blacklist_account = Mock()
        self.handlers.manager.mark_account_free = Mock()
        self.handlers.cleanup_account_mappings = Mock()
        self.handlers.process_queue = AsyncMock()

        processing_info = {
            "author": "vk-user",
            "author_id": "42",
            "original_file_name": "doc.docx",
            "message_id": "200",
            "chat_id": "100",
            "bot_type": "ai",
            "message": None,
            "file_uid": "vk:100:200:0",
            "local_path": "/tmp/source.docx",
            "gateway_job_id": 88,
            "source_platform": "vk",
            "route_sender_id": "42",
            "route_chat_id": "100",
            "temp_path": None,
            "account": "AAA-1",
        }
        self.handlers.current_processing_files["file-key"] = {"account": "AAA-1"}
        message = SimpleNamespace(id=321, chat=SimpleNamespace(id=654))
        client = SimpleNamespace(name="AAA-1")

        with patch("bot_handlers.Config.get_setting", return_value=["AAA-1", "AAA-2", "AAA-3"]):
            asyncio.run(self.handlers.handle_no_more_checks(client, message, processing_info, "file-key"))

        self.assertEqual(1, len(self.handlers.manager.file_queue))
        requeued = self.handlers.manager.file_queue[0]
        self.assertEqual("vk", requeued["source_platform"])
        self.assertEqual("42", requeued["route_sender_id"])
        self.assertEqual("100", requeued["route_chat_id"])
        self.assertEqual(88, requeued["gateway_job_id"])

    def test_handle_no_more_checks_sends_to_editor_when_all_accounts_exhausted(self):
        self.handlers.manager.blacklisted_accounts = ["AAA-1", "AAA-2", "AAA-3"]
        self.handlers.manager.blacklist_account = Mock()
        self.handlers.manager.mark_account_free = Mock()
        self.handlers.cleanup_account_mappings = Mock()
        self.handlers.process_queue = AsyncMock()
        self.handlers.send_to_nik2 = AsyncMock()

        processing_info = {
            "author": "vk-user",
            "author_id": "42",
            "original_file_name": "doc.docx",
            "message_id": "200",
            "chat_id": "100",
            "bot_type": "ai",
            "message": None,
            "file_uid": "vk:100:200:0",
            "local_path": "/tmp/source.docx",
            "gateway_job_id": 88,
            "source_platform": "vk",
            "route_sender_id": "42",
            "route_chat_id": "100",
            "temp_path": None,
            "account": "AAA-1",
        }
        self.handlers.current_processing_files["file-key"] = {"account": "AAA-1"}
        message = SimpleNamespace(id=321, chat=SimpleNamespace(id=654))
        client = SimpleNamespace(name="AAA-1")

        with patch("bot_handlers.Config.get_setting", return_value=["AAA-1", "AAA-2", "AAA-3"]):
            asyncio.run(self.handlers.handle_no_more_checks(client, message, processing_info, "file-key"))

        self.handlers.send_to_nik2.assert_awaited_once()
        forwarded = self.handlers.send_to_nik2.await_args.args[0]
        self.assertEqual("vk", forwarded["source_platform"])
        self.assertEqual("42", forwarded["route_sender_id"])
        self.assertEqual("100", forwarded["route_chat_id"])


class TestCounterEndDatetime(unittest.TestCase):
    """Unit tests for CounterModeProcessor._counter_end_datetime static method."""

    def test_both_empty_returns_none(self):
        result = CounterModeProcessor._counter_end_datetime("", "")
        self.assertIsNone(result)

    def test_none_none_returns_none(self):
        result = CounterModeProcessor._counter_end_datetime(None, None)
        self.assertIsNone(result)

    def test_date_only_returns_end_of_day(self):
        result = CounterModeProcessor._counter_end_datetime("2026-05-07", "")
        self.assertIsNotNone(result)
        self.assertEqual(result.year, 2026)
        self.assertEqual(result.month, 5)
        self.assertEqual(result.day, 7)

    def test_date_and_time_returns_combined(self):
        result = CounterModeProcessor._counter_end_datetime("2026-05-07", "14:30")
        self.assertIsNotNone(result)
        self.assertEqual(result.year, 2026)
        self.assertEqual(result.month, 5)
        self.assertEqual(result.day, 7)
        self.assertEqual(result.hour, 14)
        self.assertEqual(result.minute, 30)

    def test_time_only_without_date_returns_today_with_time(self):
        result = CounterModeProcessor._counter_end_datetime("", "09:15")
        self.assertIsNotNone(result)
        today = datetime.now().date()
        self.assertEqual(result.date(), today)
        self.assertEqual(result.hour, 9)
        self.assertEqual(result.minute, 15)


class TestVkLocalCounterRange(unittest.IsolatedAsyncioTestCase):
    """Tests for analyze_vk_files_local with end_date / end_time filtering."""

    def setUp(self):
        self.processor = CounterModeProcessor(_make_account_manager())
        self.processor.generate_counter_report_simple = AsyncMock()
        self.print_patch = patch("builtins.print")
        self.print_patch.start()

    def tearDown(self):
        self.print_patch.stop()

    @staticmethod
    def _make_job(ts, sender_id=101, filename="file.docx", cmid=1, status="done"):
        """Create a mock gateway job."""
        job = SimpleNamespace()
        job.sender_id = str(sender_id)
        job.dedupe_key = filename
        job.file_size_bytes = 1024
        job.original_file_name = filename
        job.message_id = cmid
        job.reply_to_message_id = None
        job.transport_meta = {"vk_message_date": str(ts)}
        job.chat_id = "2000000001"
        job.status = status
        return job

    async def test_end_dt_filters_out_messages_after_cutoff(self):
        base = int(datetime(2026, 5, 7, 12, 0).timestamp())
        jobs = [
            self._make_job(base + 3600, filename="late.docx"),   # 13:00 — after cutoff
            self._make_job(base, filename="ontime.docx"),        # 12:00 — within range
            self._make_job(base - 3600, filename="early.docx"),  # 11:00 — before start
        ]

        mock_store = MagicMock()
        mock_store.list_vk_jobs_by_sender.return_value = jobs

        with patch("builtins.print"), \
             patch("bot_handlers.build_store", return_value=mock_store), \
             patch.object(self.processor, "_resolve_vk_counter_author_ids", return_value={101}):
            await self.processor.analyze_vk_files_local(
                authors="101",
                date_str="2026-05-07",
                start_time_str="11:30",
                end_date_str="2026-05-07",
                end_time_str="12:30",
            )

        sent_files = self.processor.generate_counter_report_simple.await_args.args[0]
        file_names = [f["file_name"] for f in sent_files]
        self.assertIn("ontime.docx", file_names)
        self.assertNotIn("late.docx", file_names)


class TestAuthorCounter(unittest.IsolatedAsyncioTestCase):
    """Tests for analyze_author_files_custom."""

    @staticmethod
    def _make_message(from_id, file_name, ts_offset=0, msg_id=1):
        from datetime import timedelta
        base_dt = datetime(2024, 3, 15, 10, 0) + timedelta(seconds=ts_offset)
        return SimpleNamespace(
            id=msg_id,
            document=SimpleNamespace(file_name=file_name, file_size=1024),
            from_user=SimpleNamespace(id=from_id),
            date=base_dt,
            reply_to_message_id=None,
        )

    async def test_author_late_reports_after_end_file_are_counted(self):
        """End file limits author's sent batch only; returned PDFs are checked up to now."""
        author_id = 2002
        nik1_id = 1001

        msgs = [
            self._make_message(author_id, "A.docx", ts_offset=0, msg_id=1),
            self._make_message(author_id, "B.docx", ts_offset=60, msg_id=2),
            self._make_message(author_id, "C.docx", ts_offset=120, msg_id=3),
            self._make_message(nik1_id, "B.pdf", ts_offset=7200, msg_id=4),
        ]

        mgr = _make_account_manager()
        mgr.clients = {}
        processor = CounterModeProcessor(mgr)

        actual_sent = []
        actual_received = []
        report_kwargs = {}

        async def capture_report(sent, received, *args, **kwargs):
            actual_sent.extend(sent)
            actual_received.extend(received)
            report_kwargs.update(kwargs)

        processor.generate_counter_report_simple = capture_report

        mock_client = AsyncMock()
        mock_client.me = SimpleNamespace(id=nik1_id)
        mock_client.get_users = AsyncMock(return_value=SimpleNamespace(
            id=author_id, first_name="Author", last_name=None
        ))
        mock_client.get_chat_history = lambda chat_id, limit=10000: _async_iter(msgs)
        mgr.get_client.return_value = mock_client

        with patch("builtins.print"), \
             patch("bot_handlers.Config.get_account", return_value=None):
            await processor.analyze_author_files_custom(
                author="@author",
                date_str="2024-03-15",
                end_file="B",
                end_time_str="10:01",
            )

        sent_names = [f["file_name_normalized"] for f in actual_sent]
        received_names = [f["file_name_normalized"] for f in actual_received]
        self.assertEqual(["a", "b"], sent_names)
        self.assertIn("b", received_names)
        self.assertIsNotNone(report_kwargs.get("end_dt"))


class TestEditorCounter(unittest.IsolatedAsyncioTestCase):
    """Tests for analyze_editor_files_custom."""

    def _make_message(self, from_id, file_name, ts_offset=0, msg_id=1):
        from datetime import timedelta
        base_dt = datetime(2024, 3, 15, 10, 0) + timedelta(seconds=ts_offset)
        msg = SimpleNamespace(
            id=msg_id,
            document=SimpleNamespace(file_name=file_name, file_size=1024),
            from_user=SimpleNamespace(id=from_id),
            date=base_dt,
        )
        return msg

    async def test_editor_report_separates_sent_and_received(self):
        nik2_id = 1001
        editor_id = 2002

        msgs = [
            self._make_message(nik2_id, "work1.docx", ts_offset=0, msg_id=1),
            self._make_message(nik2_id, "work2.docx", ts_offset=60, msg_id=2),
            self._make_message(editor_id, "work1.pdf", ts_offset=120, msg_id=3),
        ]

        mgr = _make_account_manager()
        processor = CounterModeProcessor(mgr)
        processor.generate_editor_report = AsyncMock()

        mock_client = AsyncMock()
        mock_client.me = SimpleNamespace(id=nik2_id)
        mock_client.get_users = AsyncMock(return_value=SimpleNamespace(
            id=editor_id, first_name="Editor", last_name=None
        ))
        mock_client.get_chat_history = AsyncMock()

        async def fake_history(chat_id, limit=10000):
            for m in msgs:
                yield m

        mock_client.get_chat_history.return_value = fake_history(None)
        # patch get_chat_history to be async iterable
        mock_client.get_chat_history = lambda chat_id, limit=10000: _async_iter(msgs)

        mgr.get_client.return_value = mock_client

        with patch("builtins.print"), \
             patch("bot_handlers.Config.get_account", return_value=None):
            await processor.analyze_editor_files_custom(
                editor_nick="@editor",
                date_str="2024-03-15",
            )

        processor.generate_editor_report.assert_awaited_once()
        sent, received, *_ = processor.generate_editor_report.await_args.args
        self.assertEqual(2, len(sent), "should count 2 files sent to editor")
        self.assertEqual(1, len(received), "should count 1 file received from editor")

    async def test_editor_not_returned_is_correct(self):
        nik2_id = 1001
        editor_id = 2002

        msgs = [
            self._make_message(nik2_id, "A.docx", ts_offset=0, msg_id=1),
            self._make_message(nik2_id, "B.docx", ts_offset=60, msg_id=2),
            # editor only returned A
            self._make_message(editor_id, "A.pdf", ts_offset=120, msg_id=3),
        ]

        mgr = _make_account_manager()
        processor = CounterModeProcessor(mgr)

        actual_sent = []
        actual_received = []

        async def capture_report(sent, received, *args, **kwargs):
            actual_sent.extend(sent)
            actual_received.extend(received)

        processor.generate_editor_report = capture_report

        mock_client = AsyncMock()
        mock_client.me = SimpleNamespace(id=nik2_id)
        mock_client.get_users = AsyncMock(return_value=SimpleNamespace(
            id=editor_id, first_name="Ed", last_name=None
        ))
        mock_client.get_chat_history = lambda chat_id, limit=10000: _async_iter(msgs)

        mgr.get_client.return_value = mock_client

        with patch("builtins.print"), \
             patch("bot_handlers.Config.get_account", return_value=None):
            await processor.analyze_editor_files_custom(
                editor_nick="@editor",
                date_str="2024-03-15",
            )

        sent_names = {f["file_name_normalized"] for f in actual_sent}
        recv_names = {f["file_name_normalized"] for f in actual_received}
        not_returned = sent_names - recv_names
        self.assertIn("b", not_returned)   # B was not returned (normalized "b" from "B.docx")
        self.assertNotIn("a", not_returned) # A was returned (normalized "a" from "A.docx"/"A.pdf")

    async def test_editor_not_returned_counts_duplicates(self):
        """Sent same file 3 times, received only 2 — should report 1 not returned."""
        nik2_id = 1001
        editor_id = 2002

        msgs = [
            self._make_message(nik2_id, "work.docx", ts_offset=0, msg_id=1),
            self._make_message(nik2_id, "work.docx", ts_offset=60, msg_id=2),
            self._make_message(nik2_id, "work.docx", ts_offset=120, msg_id=3),
            self._make_message(editor_id, "work.pdf", ts_offset=180, msg_id=4),
            self._make_message(editor_id, "work.pdf", ts_offset=240, msg_id=5),
        ]

        mgr = _make_account_manager()
        processor = CounterModeProcessor(mgr)

        actual_sent = []
        actual_received = []

        async def capture_report(sent, received, *args, **kwargs):
            actual_sent.extend(sent)
            actual_received.extend(received)

        processor.generate_editor_report = capture_report

        mock_client = AsyncMock()
        mock_client.me = SimpleNamespace(id=nik2_id)
        mock_client.get_users = AsyncMock(return_value=SimpleNamespace(
            id=editor_id, first_name="Ed", last_name=None
        ))
        mock_client.get_chat_history = lambda chat_id, limit=10000: _async_iter(msgs)
        mgr.get_client.return_value = mock_client

        with patch("builtins.print"), \
             patch("bot_handlers.Config.get_account", return_value=None):
            await processor.analyze_editor_files_custom(
                editor_nick="@editor",
                date_str="2024-03-15",
            )

        # Count-based: sent 3 "work", received 2 "work" → 1 missing
        sent_counts: dict[str, int] = {}
        for f in actual_sent:
            sent_counts[f["file_name_normalized"]] = sent_counts.get(f["file_name_normalized"], 0) + 1
        recv_counts: dict[str, int] = {}
        for f in actual_received:
            recv_counts[f["file_name_normalized"]] = recv_counts.get(f["file_name_normalized"], 0) + 1
        missing = sum(max(0, s - recv_counts.get(n, 0)) for n, s in sent_counts.items())
        self.assertEqual(missing, 1)

    async def test_editor_late_reports_after_end_file_are_counted(self):
        """End file limits sent batch only; editor replies are checked up to now."""
        nik2_id = 1001
        editor_id = 2002

        msgs = [
            self._make_message(nik2_id, "A.docx", ts_offset=0, msg_id=1),
            self._make_message(nik2_id, "B.docx", ts_offset=60, msg_id=2),
            self._make_message(nik2_id, "C.docx", ts_offset=120, msg_id=3),
            self._make_message(editor_id, "B.pdf", ts_offset=7200, msg_id=4),
        ]

        mgr = _make_account_manager()
        processor = CounterModeProcessor(mgr)

        actual_sent = []
        actual_received = []

        async def capture_report(sent, received, *args, **kwargs):
            actual_sent.extend(sent)
            actual_received.extend(received)

        processor.generate_editor_report = capture_report

        mock_client = AsyncMock()
        mock_client.me = SimpleNamespace(id=nik2_id)
        mock_client.get_users = AsyncMock(return_value=SimpleNamespace(
            id=editor_id, first_name="Ed", last_name=None
        ))
        mock_client.get_chat_history = lambda chat_id, limit=10000: _async_iter(msgs)
        mgr.get_client.return_value = mock_client

        with patch("builtins.print"), \
             patch("bot_handlers.Config.get_account", return_value=None):
            await processor.analyze_editor_files_custom(
                editor_nick="@editor",
                date_str="2024-03-15",
                end_file="B",
                end_time_str="10:01",
            )

        sent_names = [f["file_name_normalized"] for f in actual_sent]
        received_names = [f["file_name_normalized"] for f in actual_received]
        self.assertEqual(["a", "b"], sent_names)
        self.assertIn("b", received_names)


def _async_iter(items):
    """Helper: create an async iterable from a list for mocking get_chat_history."""
    class _AsyncIter:
        def __init__(self, it):
            self._it = iter(it)
        def __aiter__(self):
            return self
        async def __anext__(self):
            try:
                return next(self._it)
            except StopIteration:
                raise StopAsyncIteration
    return _AsyncIter(items)


# ══════════════════════════════════════════════════════════════════════════════
# VK filename delivery — no UUID prefix
# ══════════════════════════════════════════════════════════════════════════════

class TestVkOutboundFilename(unittest.TestCase):
    """VkOutboundClient.send_document должен использовать file_name как имя при загрузке (без UUID-пути)."""

    def _make_vk_client(self):
        from multichannel_gateway.outbound import VkOutboundClient
        # Use constructor with _token_override; token is a read-only property backed by _token_override
        client = VkOutboundClient(token="fake_token")
        return client

    def _http_post_mock(self, upload_name_holder):
        def _post(url, files=None, timeout=None):
            if files:
                fname = files["file"][0] if isinstance(files["file"], tuple) else None
                upload_name_holder.append(fname)
            resp = MagicMock()
            resp.json.return_value = {"file": "fake_file_token"}
            resp.raise_for_status = lambda: None
            return resp
        return _post

    def _mock_api_calls(self, mock_api):
        mock_api.side_effect = [
            {"upload_url": "https://fake-upload.vk.com/"},    # getMessagesUploadServer
            {"doc": {"id": 1, "owner_id": 2}},                # docs.save
            {"message_id": 100},                               # messages.send
        ]

    def test_send_document_uses_file_name_param_not_os_basename(self):
        """file_name param должен быть использован в multipart, а не os.path.basename(file_path)."""
        client = self._make_vk_client()
        upload_names = []

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF test")
            tmp = f.name

        try:
            with patch("multichannel_gateway.outbound._require_requests") as mock_req, \
                 patch.object(client, "_api_call") as mock_api:
                mock_http = MagicMock()
                mock_http.post.side_effect = self._http_post_mock(upload_names)
                mock_req.return_value = mock_http
                self._mock_api_calls(mock_api)
                client.send_document(peer_id=12345, file_path=tmp, file_name="364257.pdf")
        finally:
            os.unlink(tmp)

        self.assertEqual(len(upload_names), 1, "Ожидался один POST при загрузке")
        self.assertEqual(upload_names[0], "364257.pdf",
                         f"Ожидалось '364257.pdf', получено '{upload_names[0]}'")

    def test_send_document_falls_back_to_basename_when_no_file_name(self):
        """Если file_name не передан — использовать os.path.basename(file_path)."""
        client = self._make_vk_client()
        upload_names = []

        with tempfile.NamedTemporaryFile(suffix=".pdf", prefix="abc123_", delete=False) as f:
            f.write(b"%PDF test")
            tmp = f.name

        try:
            with patch("multichannel_gateway.outbound._require_requests") as mock_req, \
                 patch.object(client, "_api_call") as mock_api:
                mock_http = MagicMock()
                mock_http.post.side_effect = self._http_post_mock(upload_names)
                mock_req.return_value = mock_http
                self._mock_api_calls(mock_api)
                client.send_document(peer_id=12345, file_path=tmp)  # no file_name
        finally:
            os.unlink(tmp)

        self.assertEqual(upload_names[0], os.path.basename(tmp))

    def test_send_document_uuid_prefix_NOT_in_upload_name(self):
        """Имя загружаемого файла НЕ должно содержать UUID-префикс если передан file_name."""
        client = self._make_vk_client()
        upload_names = []

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF test")
            tmp = f.name

        try:
            with patch("multichannel_gateway.outbound._require_requests") as mock_req, \
                 patch.object(client, "_api_call") as mock_api:
                mock_http = MagicMock()
                mock_http.post.side_effect = self._http_post_mock(upload_names)
                mock_req.return_value = mock_http
                self._mock_api_calls(mock_api)
                client.send_document(peer_id=12345, file_path=tmp, file_name="361664 анти (1).doc")
        finally:
            os.unlink(tmp)

        name = upload_names[0]
        import re
        has_uuid_prefix = bool(re.match(r'^[0-9a-f]{8}_', name))
        self.assertFalse(has_uuid_prefix, f"UUID-префикс в имени файла: '{name}'")
        self.assertEqual(name, "361664 анти (1).doc")


# ══════════════════════════════════════════════════════════════════════════════
# Editor response — original filename preserved
# ══════════════════════════════════════════════════════════════════════════════

class TestEditorResponseOriginalFilename(unittest.IsolatedAsyncioTestCase):
    """handle_editor_response должен доставлять редакторский PDF только при точном совпадении имени."""

    def _setup_handlers_with_tracking(self, original_name, editor_returned_name):
        """Создать handlers с tracking и вернуть (handlers, message_mock)."""
        handlers = _make_handlers()
        handlers.manager.get_client = MagicMock(return_value=MagicMock(name="НИК-1"))

        # tracking_info simulates a file sent to НИК-2 editor
        tracking_key = "anti_editor_777"
        original_name_without_ext = os.path.splitext(original_name)[0]
        handlers.editor_tracking[tracking_key] = {
            "expected_pdf_name": f"{original_name_without_ext}.pdf",
            "original_name": original_name,
            "original_name_without_ext": original_name_without_ext,
            "is_anti_file": True,
            "author_id": 111,
            "author": 111,
            "route_sender_id": None,
            "source_platform": "telegram",
            "sent_from_account": "НИК-2",
            "reply_to_message_id": 777,
            "file_uid": "test:0:0:0",
        }

        doc = MagicMock()
        doc.file_name = editor_returned_name
        msg = MagicMock()
        msg.reply_to_message_id = 777
        msg.document = doc
        msg.download = AsyncMock(side_effect=lambda path: path)
        msg.from_user = SimpleNamespace(username="editor", id=777000)

        return handlers, msg

    async def test_deliver_uses_editor_pdf_name_when_name_matches(self):
        """Если редактор вернул готовый PDF с тем же базовым именем — доставить его имя."""
        handlers, msg = self._setup_handlers_with_tracking(
            original_name="361664 анти (1).doc",
            editor_returned_name="361664 анти (1).pdf",
        )
        delivered_names = []

        async def fake_deliver(tracking_info, path, file_name=None, telegram_client=None):
            delivered_names.append(file_name)
            return {"status": "ok"}

        handlers._deliver_document_to_origin = fake_deliver
        handlers._build_result_file_path = MagicMock(return_value="/tmp/fake_361664_anti.pdf")
        handlers._finish_processing = MagicMock()
        handlers._mark_gateway_job_done = AsyncMock()

        with patch("os.path.exists", return_value=True):
            nik2 = MagicMock()
            nik2.name = "НИК-2"
            await handlers.handle_editor_response(nik2, msg)

        self.assertEqual(len(delivered_names), 1)
        self.assertEqual(delivered_names[0], "361664 анти (1).pdf",
                         f"Доставлено с именем '{delivered_names[0]}' вместо '361664 анти (1).pdf'")

    async def test_editor_renamed_pdf_is_not_delivered(self):
        """Если имя PDF не совпало с исходным файлом — не угадывать и не доставлять."""
        handlers, msg = self._setup_handlers_with_tracking(
            original_name="8397_Анти.docx",
            editor_returned_name="6f88ab1a_8397_Анти.pdf",
        )
        delivered_names = []

        async def fake_deliver(tracking_info, path, file_name=None, telegram_client=None):
            delivered_names.append(file_name)
            return {"status": "ok"}

        handlers._deliver_document_to_origin = fake_deliver
        handlers._build_result_file_path = MagicMock(return_value="/tmp/fake.pdf")
        handlers._finish_processing = MagicMock()
        handlers._mark_gateway_job_done = AsyncMock()

        with patch("os.path.exists", return_value=True):
            nik2 = MagicMock()
            nik2.name = "НИК-2"
            await handlers.handle_editor_response(nik2, msg)

        self.assertEqual([], delivered_names)
        self.assertIn("anti_editor_777", handlers.editor_tracking)

    async def test_24_7_editor_pdf_is_delivered_back_to_origin(self):
        handlers = _make_handlers()
        handlers.manager.get_client = MagicMock(return_value=MagicMock(name="НИК-1"))
        tracking_key = "24_7_777_120000"
        handlers.editor_tracking[tracking_key] = {
            "expected_pdf_name": "324891.pdf",
            "original_name": "324891.docx",
            "is_anti_file": False,
            "author_id": 111,
            "author": "author",
            "source_platform": "telegram",
            "sent_from_account": "НИК-2",
            "reply_to_message_id": 777,
            "destination": "@editor247",
            "file_uid": "telegram:111:222:0",
        }

        doc = MagicMock()
        doc.file_name = "324891.pdf"
        msg = MagicMock()
        msg.reply_to_message_id = 777
        msg.document = doc
        msg.download = AsyncMock(side_effect=lambda path: path)

        delivered_names = []

        async def fake_deliver(tracking_info, path, file_name=None, telegram_client=None):
            delivered_names.append((tracking_info["destination"], file_name))
            return {"status": "ok"}

        handlers._deliver_document_to_origin = fake_deliver
        handlers._build_result_file_path = MagicMock(return_value="/tmp/fake_324891.pdf")
        handlers._finish_processing = MagicMock()
        handlers._mark_gateway_job_done = AsyncMock()

        with patch("os.path.exists", return_value=True), patch("os.remove"):
            nik2 = MagicMock()
            nik2.name = "НИК-2"
            await handlers.handle_editor_response(nik2, msg)

        self.assertEqual([("@editor247", "324891.pdf")], delivered_names)
        self.assertNotIn(tracking_key, handlers.editor_tracking)

    async def test_editor_no_checks_keeps_old_tracking_and_late_pdf_is_delivered(self):
        handlers = _make_handlers()
        handlers.manager.get_client = MagicMock(return_value=MagicMock(name="НИК-1"))
        handlers.send_to_24_7_editor = AsyncMock()
        tracking_key = "no_checks_713"
        handlers.editor_tracking[tracking_key] = {
            "expected_pdf_name": "324891.pdf",
            "original_name": "324891.docx",
            "original_name_without_ext": "324891",
            "author": "author",
            "source_platform": "telegram",
            "sent_from_account": "НИК-2",
            "reply_to_message_id": 713,
            "destination": "@Xlistyara",
            "file_uid": "telegram:author:713:0",
        }

        no_checks_msg = MagicMock()
        no_checks_msg.reply_to_message_id = 713
        no_checks_msg.document = None
        no_checks_msg.text = "⏳ В данный момент проверки закончились.\nНачнем проверять после: 12:00 по мск времени."
        no_checks_msg.from_user = SimpleNamespace(username="Xlistyara", id=1000)

        nik2 = MagicMock()
        nik2.name = "НИК-2"
        await handlers.handle_editor_response(nik2, no_checks_msg)

        self.assertIn(tracking_key, handlers.editor_tracking)
        self.assertTrue(handlers._is_editor_unavailable("@Xlistyara"))
        handlers.send_to_24_7_editor.assert_not_awaited()

        doc = MagicMock()
        doc.file_name = "324891.pdf"
        pdf_msg = MagicMock()
        pdf_msg.reply_to_message_id = 713
        pdf_msg.document = doc
        pdf_msg.from_user = SimpleNamespace(username="Xlistyara", id=1000)
        pdf_msg.download = AsyncMock(side_effect=lambda path: path)

        delivered = []

        async def fake_deliver(tracking_info, path, file_name=None, telegram_client=None):
            delivered.append((tracking_info["author"], file_name))
            return {"status": "ok"}

        handlers._deliver_document_to_origin = fake_deliver
        handlers._build_result_file_path = MagicMock(return_value="/tmp/324891.pdf")
        handlers._finish_processing = MagicMock()
        handlers._mark_gateway_job_done = AsyncMock()

        with patch("os.path.exists", return_value=True), patch("os.remove"):
            await handlers.handle_editor_response(nik2, pdf_msg)

        self.assertEqual([("author", "324891.pdf")], delivered)
        self.assertNotIn(tracking_key, handlers.editor_tracking)

    async def test_no_reply_normal_editor_pdf_uses_filename_not_fifo(self):
        handlers = _make_handlers()
        handlers.manager.get_client = MagicMock(return_value=MagicMock(name="НИК-1"))
        handlers.editor_tracking = {
            "no_checks_100": {
                "expected_pdf_name": "373588.pdf",
                "original_name": "373588.docx",
                "original_name_without_ext": "373588",
                "author": "author_a",
                "source_platform": "telegram",
                "sent_from_account": "НИК-2",
                "reply_to_message_id": 100,
                "destination": "@editor",
                "chat_id": 1000,
                "file_uid": "telegram:a:1:0",
                "sent_at": datetime(2026, 5, 20, 10, 0),
            },
            "no_checks_101": {
                "expected_pdf_name": "373662.pdf",
                "original_name": "373662.docx",
                "original_name_without_ext": "373662",
                "author": "author_b",
                "source_platform": "telegram",
                "sent_from_account": "НИК-2",
                "reply_to_message_id": 101,
                "destination": "@editor",
                "chat_id": 1000,
                "file_uid": "telegram:b:1:0",
                "sent_at": datetime(2026, 5, 20, 10, 1),
            },
        }

        key, info, reason = handlers.find_editor_tracking_for_unreplied_pdf("editor", "373662.pdf")
        self.assertEqual("no_checks_101", key)
        self.assertIn("точное совпадение", reason)

        doc = MagicMock()
        doc.file_name = "373662.pdf"
        msg = MagicMock()
        msg.reply_to_message_id = info["reply_to_message_id"]
        msg.document = doc
        msg.chat = SimpleNamespace(id=1000)
        msg.from_user = SimpleNamespace(username="editor", id=1000)
        msg.download = AsyncMock(side_effect=lambda path: path)

        delivered = []

        async def fake_deliver(tracking_info, path, file_name=None, telegram_client=None):
            delivered.append((tracking_info["author"], file_name))
            return {"status": "ok"}

        handlers._deliver_document_to_origin = fake_deliver
        handlers._build_result_file_path = MagicMock(return_value="/tmp/373662.pdf")
        handlers._finish_processing = MagicMock()
        handlers._mark_gateway_job_done = AsyncMock()

        with patch("os.path.exists", return_value=True), patch("os.remove"):
            nik2 = MagicMock()
            nik2.name = "НИК-2"
            await handlers.handle_editor_response(nik2, msg)

        self.assertEqual([("author_b", "373662.pdf")], delivered)
        self.assertIn("no_checks_100", handlers.editor_tracking)
        self.assertNotIn("no_checks_101", handlers.editor_tracking)

    async def test_editor_pdf_source_pdf_does_not_send_original_local_file(self):
        handlers = _make_handlers()
        handlers.manager.get_client = MagicMock(return_value=MagicMock(name="НИК-1"))
        source_pdf = tempfile.NamedTemporaryFile(delete=False, suffix="_source.pdf")
        source_pdf.write(b"SOURCE PDF CONTENT")
        source_pdf.close()
        result_pdf = tempfile.NamedTemporaryFile(delete=False, suffix="_result.pdf")
        result_pdf.write(b"EDITOR REPORT CONTENT")
        result_pdf.close()

        tracking_key = "no_checks_373580"
        handlers.editor_tracking[tracking_key] = {
            "expected_pdf_name": "373580.pdf",
            "original_name": "373580.pdf",
            "original_name_without_ext": "373580",
            "author": "author_pdf",
            "source_platform": "telegram",
            "sent_from_account": "НИК-2",
            "reply_to_message_id": 373580,
            "destination": "@editor",
            "file_uid": "telegram:pdf:373580:0",
            "local_path": source_pdf.name,
        }

        doc = MagicMock()
        doc.file_name = "373580.pdf"
        msg = MagicMock()
        msg.reply_to_message_id = 373580
        msg.document = doc
        msg.from_user = SimpleNamespace(username="editor", id=1000)

        async def fake_download(path):
            shutil.copy2(result_pdf.name, path)
            return path

        msg.download = AsyncMock(side_effect=fake_download)
        delivered = []

        async def fake_deliver(tracking_info, path, file_name=None, telegram_client=None):
            with open(path, "rb") as f:
                delivered.append((file_name, f.read()))
            return {"status": "ok"}

        handlers._deliver_document_to_origin = fake_deliver
        handlers._finish_processing = MagicMock()
        handlers._mark_gateway_job_done = AsyncMock()

        try:
            with patch("os.remove"):
                nik2 = MagicMock()
                nik2.name = "НИК-2"
                await handlers.handle_editor_response(nik2, msg)
        finally:
            os.unlink(source_pdf.name)
            os.unlink(result_pdf.name)

        self.assertEqual([("373580.pdf", b"EDITOR REPORT CONTENT")], delivered)

    async def test_no_reply_24_7_pdf_without_exact_name_is_not_delivered(self):
        handlers = _make_handlers()
        handlers.manager.get_client = MagicMock(return_value=MagicMock(name="НИК-1"))
        handlers.editor_tracking = {
            "24_7_100": {
                "expected_pdf_name": "373588.pdf",
                "original_name": "373588.docx",
                "original_name_without_ext": "373588",
                "author": "author_a",
                "source_platform": "telegram",
                "sent_from_account": "НИК-2",
                "reply_to_message_id": 100,
                "destination": "@editor247",
                "file_uid": "telegram:a:1:0",
            },
            "24_7_101": {
                "expected_pdf_name": "373662.pdf",
                "original_name": "373662.docx",
                "original_name_without_ext": "373662",
                "author": "author_b",
                "source_platform": "telegram",
                "sent_from_account": "НИК-2",
                "reply_to_message_id": 101,
                "destination": "@editor247",
                "file_uid": "telegram:b:1:0",
            },
        }

        key, info, reason = handlers.find_editor_tracking_for_unreplied_pdf("editor247", "wrong_373662.pdf")

        self.assertIsNone(key)
        self.assertIsNone(info)
        self.assertIn("нет точного совпадения", reason)

    async def test_editor_damaged_file_text_closes_tracking_without_delivery(self):
        handlers = _make_handlers()
        handlers.editor_tracking["no_checks_911"] = {
            "expected_pdf_name": "374911.pdf",
            "original_name": "374911.rtf",
            "original_name_without_ext": "374911",
            "author": "author",
            "source_platform": "telegram",
            "sent_from_account": "НИК-2",
            "reply_to_message_id": 911,
            "destination": "@editor",
            "file_uid": "telegram:author:911:0",
        }
        delivered = []

        async def fake_deliver(*args, **kwargs):
            delivered.append((args, kwargs))

        handlers._deliver_document_to_origin = fake_deliver
        handlers._finish_processing = MagicMock()
        handlers._mark_gateway_job_failed = AsyncMock()

        msg = MagicMock()
        msg.reply_to_message_id = 911
        msg.document = None
        msg.text = "Проверено: 52\nОшибок: 1\nФайл: 374911.rtf - поврежденный файл"
        msg.from_user = SimpleNamespace(username="editor", id=1000)

        nik2 = MagicMock()
        nik2.name = "НИК-2"
        await handlers.handle_editor_response(nik2, msg)

        self.assertEqual([], delivered)
        self.assertNotIn("no_checks_911", handlers.editor_tracking)
        handlers._finish_processing.assert_called_once_with("telegram:author:911:0", False)

    async def test_24_7_editor_damaged_file_text_can_match_without_reply_by_filename(self):
        handlers = _make_handlers()
        handlers.editor_tracking["24_7_900"] = {
            "expected_pdf_name": "374900.pdf",
            "original_name": "374900.docx",
            "original_name_without_ext": "374900",
            "sent_from_account": "НИК-2",
            "reply_to_message_id": 900,
            "destination": "@editor247",
            "chat_id": 1000,
        }
        handlers.editor_tracking["24_7_911"] = {
            "expected_pdf_name": "374911.pdf",
            "original_name": "374911.rtf",
            "original_name_without_ext": "374911",
            "sent_from_account": "НИК-2",
            "reply_to_message_id": 911,
            "destination": "@editor247",
            "chat_id": 1000,
        }

        key, info, reason = handlers.find_editor_tracking_for_text_response(
            "editor247",
            None,
            sent_from_account="НИК-2",
            message_text="Проверено: 52\nОшибок: 1\nФайл: 374911.rtf - поврежденный файл",
        )

        self.assertEqual("24_7_911", key)
        self.assertEqual(911, info["reply_to_message_id"])
        self.assertIn("точное совпадение", reason)

    async def test_non_pdf_response_is_rejected(self):
        """Редактор вернул .docx — должны игнорировать (не доставлять)."""
        handlers = _make_handlers()
        tracking_key = "anti_editor_888"
        handlers.editor_tracking[tracking_key] = {
            "expected_pdf_name": "работа.pdf",
            "sent_from_account": "НИК-2",
            "reply_to_message_id": 888,
            "file_uid": "test:0:0:0",
        }
        delivered = []

        async def fake_deliver(tracking_info, path, **kw):
            delivered.append(path)

        handlers._deliver_document_to_origin = fake_deliver

        doc = MagicMock()
        doc.file_name = "работа.docx"  # not PDF
        msg = MagicMock()
        msg.reply_to_message_id = 888
        msg.document = doc

        nik2 = MagicMock()
        nik2.name = "НИК-2"
        await handlers.handle_editor_response(nik2, msg)

        self.assertEqual(delivered, [], "Non-PDF от редактора не должен доставляться")

    async def test_no_document_in_editor_response_is_ignored(self):
        """Редактор прислал текст без файла — должны игнорировать."""
        handlers = _make_handlers()
        tracking_key = "anti_editor_999"
        handlers.editor_tracking[tracking_key] = {
            "expected_pdf_name": "работа.pdf",
            "sent_from_account": "НИК-2",
            "reply_to_message_id": 999,
            "file_uid": "test:0:0:0",
        }
        delivered = []

        async def fake_deliver(tracking_info, path, **kw):
            delivered.append(path)

        handlers._deliver_document_to_origin = fake_deliver

        msg = MagicMock()
        msg.reply_to_message_id = 999
        msg.document = None  # no document

        nik2 = MagicMock()
        nik2.name = "НИК-2"
        await handlers.handle_editor_response(nik2, msg)

        self.assertEqual(delivered, [])

    async def test_main_account_text_reply_from_editor_routes_to_editor_response(self):
        handlers = _make_handlers()
        handlers.handle_editor_response = AsyncMock()
        handlers.editor_tracking["no_checks_705"] = {
            "sent_from_account": "НИК-2",
            "reply_to_message_id": 705,
            "destination": "@Xlistyara",
            "original_name": "325451 анти.docx",
            "file_uid": "telegram:806750628:13023:0",
        }

        msg = MagicMock()
        msg.document = None
        msg.text = "⏳ В данный момент проверки закончились.\nНачнем проверять после: 12:00 по мск времени."
        msg.reply_to_message_id = 705
        msg.from_user = SimpleNamespace(username="Xlistyara", id=806750628)

        client = MagicMock()
        client.name = "НИК-2"
        await handlers.handle_main_account(client, msg)

        handlers.handle_editor_response.assert_awaited_once_with(client, msg)

    async def test_main_account_known_editor_document_reply_stops_inbound_flow(self):
        handlers = _make_handlers()
        handlers.handle_editor_response = AsyncMock()
        handlers.is_bot_message = MagicMock(return_value=False)
        handlers.is_author_allowed = MagicMock(return_value=True)
        handlers.manager.get_file_lock = AsyncMock(return_value=asyncio.Lock())
        handlers.manager.get_file_status = AsyncMock(return_value="NEW")
        handlers.manager.set_file_status = AsyncMock()
        handlers.manager.reset_waiting_status = MagicMock()
        handlers.process_queue = AsyncMock()
        handlers.editor_tracking["no_checks_705"] = {
            "sent_from_account": "НИК-2",
            "reply_to_message_id": 705,
            "chat_id": 301,
            "destination": "@Xlistyara",
            "original_name": "325451 анти.docx",
            "expected_pdf_name": "325451 анти.pdf",
            "file_uid": "telegram:806750628:13023:0",
        }
        msg = SimpleNamespace(
            id=706,
            document=SimpleNamespace(file_name="325451 анти.pdf", file_size=256),
            text=None,
            reply_to_message_id=705,
            chat=SimpleNamespace(id=301, type="private"),
            from_user=SimpleNamespace(username="Xlistyara", id=806750628),
        )
        ingest_job = SimpleNamespace(
            job_id=80,
            file_path=__file__,
            dedupe_key="telegram:301:706:0",
        )

        with patch("bot_handlers.ingest_pyrogram_message", new=AsyncMock(return_value=[ingest_job])) as ingest_mock:
            await handlers.handle_main_account(SimpleNamespace(name="НИК-1"), msg)

        handlers.handle_editor_response.assert_awaited_once_with(SimpleNamespace(name="НИК-1"), msg)
        ingest_mock.assert_not_awaited()
        self.assertEqual([], handlers.manager.file_queue)

    async def test_main_account_text_from_editor_without_reply_uses_single_pending_tracking(self):
        handlers = _make_handlers()
        handlers.handle_editor_response = AsyncMock()
        handlers.editor_tracking["no_checks_705"] = {
            "sent_from_account": "НИК-2",
            "reply_to_message_id": 705,
            "destination": "@Xlistyara",
            "original_name": "325451 анти.docx",
            "file_uid": "telegram:806750628:13023:0",
        }

        msg = MagicMock()
        msg.document = None
        msg.text = "⏳ В данный момент проверки закончились.\nНачнем проверять после: 12:00 по мск времени."
        msg.reply_to_message_id = None
        msg.from_user = SimpleNamespace(username="Xlistyara", id=806750628)

        client = MagicMock()
        client.name = "НИК-2"
        await handlers.handle_main_account(client, msg)

        self.assertIsNone(msg.reply_to_message_id)
        handlers.handle_editor_response.assert_awaited_once_with(
            client,
            msg,
            resolved_reply_to_message_id=705,
        )

    async def test_main_account_pdf_without_reply_passes_resolved_id_without_mutation(self):
        handlers = _make_handlers()
        handlers.handle_editor_response = AsyncMock()
        handlers.editor_tracking["no_checks_706"] = {
            "sent_from_account": "НИК-2",
            "reply_to_message_id": 706,
            "chat_id": 301,
            "destination": "@Xlistyara",
            "original_name": "325452 анти.docx",
            "expected_pdf_name": "325452 анти.pdf",
            "file_uid": "telegram:806750628:13024:0",
        }

        msg = MagicMock()
        msg.document = SimpleNamespace(file_name="325452 анти.pdf")
        msg.text = None
        msg.reply_to_message_id = None
        msg.chat = SimpleNamespace(id=301)
        msg.from_user = SimpleNamespace(username="Xlistyara", id=806750628)

        client = MagicMock()
        client.name = "НИК-2"
        await handlers.handle_main_account(client, msg)

        self.assertIsNone(msg.reply_to_message_id)
        handlers.handle_editor_response.assert_awaited_once_with(
            client,
            msg,
            resolved_reply_to_message_id=706,
        )

    async def test_main_account_unknown_reply_does_not_use_filename_fallback(self):
        handlers = _make_handlers()
        handlers.handle_editor_response = AsyncMock()
        handlers.editor_tracking["no_checks_707"] = {
            "sent_from_account": "НИК-2",
            "reply_to_message_id": 707,
            "chat_id": 301,
            "destination": "@Xlistyara",
            "original_name": "325453 анти.docx",
            "expected_pdf_name": "325453 анти.pdf",
            "file_uid": "telegram:806750628:13025:0",
        }

        msg = MagicMock()
        msg.document = SimpleNamespace(file_name="325453 анти.pdf")
        msg.text = None
        msg.reply_to_message_id = 706
        msg.chat = SimpleNamespace(id=301)
        msg.from_user = SimpleNamespace(username="Xlistyara", id=806750628)

        client = MagicMock()
        client.name = "НИК-2"
        with patch("builtins.print"):
            await handlers.handle_main_account(client, msg)

        handlers.handle_editor_response.assert_not_awaited()

    async def test_client_docx_with_unknown_reply_from_nik1_continues_group_routing(self):
        handlers = _make_handlers()
        handlers.process_queue = AsyncMock()
        handlers.manager.get_file_lock = AsyncMock(return_value=asyncio.Lock())
        handlers.manager.get_file_status = AsyncMock(return_value="NEW")
        handlers.manager.set_file_status = AsyncMock()
        handlers.manager.reset_waiting_status = MagicMock()
        message = SimpleNamespace(
            id=778,
            reply_to_message_id=777,
            document=SimpleNamespace(file_name="ДИПЛОМ.docx", file_size=256),
            text=None,
            chat=SimpleNamespace(id=-100123, type="group"),
            from_user=SimpleNamespace(username="client", id=42),
        )
        ingest_job = SimpleNamespace(
            job_id=78,
            file_path=__file__,
            dedupe_key="telegram:-100123:778:0",
        )
        settings = {
            "mode": "mode1",
            "ai_bot": "@AAA_Report_AIBot",
            "anti_bot": "@plagiscan_bot",
            "allowed_authors": [],
            "telegram_group_routes": {
                "-100123": {
                    "title": "Компания",
                    "destination": "plagiscan",
                }
            },
        }

        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None: settings.get(key, default)), patch(
            "bot_handlers.ingest_pyrogram_message",
            new=AsyncMock(return_value=[ingest_job]),
        ) as ingest_mock:
            await handlers.handle_main_account(SimpleNamespace(name="НИК-1"), message)

        ingest_mock.assert_awaited_once_with(message)
        self.assertEqual(1, len(handlers.manager.file_queue))
        self.assertEqual(-100123, handlers.manager.file_queue[0]["route_chat_id"])
        self.assertEqual(778, handlers.manager.file_queue[0]["route_message_id"])
        self.assertEqual(
            {"destination": "plagiscan", "scope": "telegram_group"},
            handlers.manager.file_queue[0]["forced_route"],
        )

    async def test_client_docx_with_unknown_reply_from_nik1_continues_private_routing(self):
        handlers = _make_handlers()
        handlers.process_queue = AsyncMock()
        handlers.manager.get_file_lock = AsyncMock(return_value=asyncio.Lock())
        handlers.manager.get_file_status = AsyncMock(return_value="NEW")
        handlers.manager.set_file_status = AsyncMock()
        handlers.manager.reset_waiting_status = MagicMock()
        message = SimpleNamespace(
            id=779,
            reply_to_message_id=778,
            document=SimpleNamespace(file_name="ДИПЛОМ.docx", file_size=256),
            text=None,
            chat=SimpleNamespace(id=42, type="private"),
            from_user=SimpleNamespace(username="client", id=42),
        )
        ingest_job = SimpleNamespace(
            job_id=79,
            file_path=__file__,
            dedupe_key="telegram:42:779:0",
        )
        settings = {
            "mode": "mode1",
            "ai_bot": "@AAA_Report_AIBot",
            "anti_bot": "@plagaiscan_bot",
            "allowed_authors": ["client"],
            "telegram_group_routes": {},
        }

        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None: settings.get(key, default)), patch(
            "bot_handlers.ingest_pyrogram_message",
            new=AsyncMock(return_value=[ingest_job]),
        ) as ingest_mock:
            await handlers.handle_main_account(SimpleNamespace(name="НИК-1"), message)

        ingest_mock.assert_awaited_once_with(message)
        self.assertEqual(1, len(handlers.manager.file_queue))
        self.assertEqual(42, handlers.manager.file_queue[0]["route_chat_id"])
        self.assertIsNone(handlers.manager.file_queue[0]["forced_route"])


# ══════════════════════════════════════════════════════════════════════════════
# Counter report format — период, анти/обычные, НЕ ПРИСЛАННЫЕ
# ══════════════════════════════════════════════════════════════════════════════

class TestCounterReportFormat(unittest.IsolatedAsyncioTestCase):
    """generate_counter_report_simple должен содержать период, разбивку анти/обычные, НЕ ПРИСЛАННЫЕ."""

    def setUp(self):
        with patch("bot_handlers.Config") as mock_config:
            mock_config.get_setting.return_value = None
            self.processor = CounterModeProcessor(_make_account_manager())
        self.processor.save_simple_report_to_file = AsyncMock()

    def _make_file(self, name, is_anti=False, dt=None):
        normalized = name.lower().replace(" ", "_")
        return {
            "file_name": name,
            "file_name_normalized": normalized,
            "file_key": normalized,
            "is_anti": is_anti,
            "date": dt or datetime(2026, 5, 6, 10, 0),
            "message_id": 1,
        }

    async def test_period_header_with_start_dt(self):
        """start_dt задан → заголовок содержит 'СТАТИСТИКА ЗА' и время начала."""
        from io import StringIO
        import contextlib
        start = datetime(2026, 5, 6, 9, 0)
        sent = [self._make_file("диплом.docx")]
        output = StringIO()
        with contextlib.redirect_stdout(output):
            await self.processor.generate_counter_report_simple(sent, [], datetime(2026, 5, 6), start_dt=start)
        text = output.getvalue()
        self.assertIn("СТАТИСТИКА ЗА", text)
        self.assertIn("09:00", text)

    async def test_period_header_without_start_dt_uses_date(self):
        """start_dt не задан → заголовок содержит дату в формате dd.mm.yyyy."""
        from io import StringIO
        import contextlib
        sent = [self._make_file("реферат.docx")]
        output = StringIO()
        with contextlib.redirect_stdout(output):
            await self.processor.generate_counter_report_simple(sent, [], datetime(2026, 5, 6).date())
        text = output.getvalue()
        self.assertIn("06.05.2026", text)

    async def test_anti_and_normal_split_in_output(self):
        """Отчёт должен раздельно показывать обычные и анти файлы."""
        from io import StringIO
        import contextlib
        sent = [
            self._make_file("курсовая.docx", is_anti=False),
            self._make_file("АНТИ_курсовая.docx", is_anti=True),
            self._make_file("диплом.docx", is_anti=False),
        ]
        output = StringIO()
        with contextlib.redirect_stdout(output):
            await self.processor.generate_counter_report_simple(sent, [], datetime(2026, 5, 6).date())
        text = output.getvalue()
        self.assertIn("обычных файлов: 2", text)
        self.assertIn("АНТИ файлов: 1", text)

    async def test_not_received_section_shown(self):
        """Секция 'НЕ ПРИСЛАННЫЕ ОТЧЕТЫ' должна присутствовать в выводе."""
        from io import StringIO
        import contextlib
        sent = [self._make_file("курсовая.docx")]
        output = StringIO()
        with contextlib.redirect_stdout(output):
            await self.processor.generate_counter_report_simple(sent, [], datetime(2026, 5, 6).date())
        text = output.getvalue()
        self.assertIn("НЕ ПРИСЛАННЫЕ", text)

    async def test_not_received_lists_missing_file_names(self):
        """Файлы не вернувшиеся от редактора перечислены в секции НЕ ПРИСЛАННЫЕ."""
        from io import StringIO
        import contextlib
        sent = [
            self._make_file("курсовая.docx", is_anti=False),
            self._make_file("диплом.docx", is_anti=False),
        ]
        received = [
            {
                "file_name": "курсовая.pdf",
                "file_name_normalized": "курсовая.docx",
                "is_anti": False,
                "date": datetime(2026, 5, 6, 12, 0),
            }
        ]
        output = StringIO()
        with contextlib.redirect_stdout(output):
            await self.processor.generate_counter_report_simple(sent, received, datetime(2026, 5, 6).date())
        text = output.getvalue()
        self.assertIn("диплом", text)  # missing one

    async def test_zero_files_no_crash(self):
        """Пустой список не вызывает исключений."""
        await self.processor.generate_counter_report_simple([], [], datetime(2026, 5, 6).date())

    async def test_all_normal_zero_anti(self):
        """Только обычные файлы → анти = 0."""
        from io import StringIO
        import contextlib
        sent = [self._make_file("doc1.docx"), self._make_file("doc2.docx")]
        output = StringIO()
        with contextlib.redirect_stdout(output):
            await self.processor.generate_counter_report_simple(sent, [], datetime(2026, 5, 6).date())
        text = output.getvalue()
        self.assertIn("АНТИ файлов: 0", text)

    async def test_end_dt_in_period_header(self):
        """end_dt задан → заголовок содержит время окончания."""
        from io import StringIO
        import contextlib
        start = datetime(2026, 5, 6, 8, 0)
        end = datetime(2026, 5, 6, 18, 0)
        sent = [self._make_file("файл.docx")]
        output = StringIO()
        with contextlib.redirect_stdout(output):
            await self.processor.generate_counter_report_simple(
                sent, [], datetime(2026, 5, 6).date(), start_dt=start, end_dt=end
            )
        text = output.getvalue()
        self.assertIn("18:00", text)

    async def test_vk_direct_report_status_marks_missing_specific_anti_file(self):
        """VK local counter uses each gateway job status instead of only name-count matching."""
        from io import StringIO
        import contextlib
        sent = [
            self._make_file("анти_ВКР_1338295_Фин.2.docx", is_anti=True),
            self._make_file("анти_ВКР_1338295_Фин.3.docx", is_anti=True),
        ]
        sent[0]["report_delivered"] = False
        sent[1]["report_delivered"] = True

        output = StringIO()
        with contextlib.redirect_stdout(output):
            await self.processor.generate_counter_report_simple(sent, [], datetime(2026, 5, 8).date())

        text = output.getvalue()
        self.assertIn("Не прислал анти файлов: 1", text)
        self.assertIn("Фин.2", text)

    async def test_vk_history_missing_gateway_status_uses_received_files_matching(self):
        """History API entries without local gateway status still match returned PDFs by name."""
        from io import StringIO
        import contextlib
        sent = [self._make_file("368333.pdf")]
        sent[0]["gateway_status"] = "missing_from_gateway"
        sent[0]["report_delivered"] = False
        received = [self._make_file("368333.pdf")]

        output = StringIO()
        with contextlib.redirect_stdout(output):
            await self.processor.generate_counter_report_simple(sent, received, datetime(2026, 5, 13).date())

        text = output.getvalue()
        self.assertIn("Не прислал обычных файлов: 0", text)


# ══════════════════════════════════════════════════════════════════════════════
# process_queue routing — все сценарии (mode1 / mode2 / VK / TG / анти / обычные)
# ══════════════════════════════════════════════════════════════════════════════

class TestProcessQueueAllScenarios(unittest.TestCase):
    """Комплексные тесты маршрутизации process_queue для mode1/mode2, VK/TG, анти/обычные."""

    BASE_SETTINGS = {
        "max_concurrent": 7,
        "mode": "mode1",
        "normal_destination": "бот",
        "anti_destination": "бот",
        "plagiscan_users": [],
    }

    def _run(self, handlers, settings):
        merged = {**self.BASE_SETTINGS, **settings}
        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None: merged.get(key, default)):
            asyncio.run(handlers.process_queue())

    # ── normal_destination = "бот" ──────────────────────────────────────────

    def test_mode1_normal_destination_bot_calls_process_normal_file(self):
        h = _make_handlers()
        h.manager.file_queue = [{"file_name": "f.docx", "original_file_name": "f.docx",
                                  "is_anti": False, "force_plagiscan": False}]
        h.process_normal_file = AsyncMock()
        h.process_anti_file = AsyncMock()
        self._run(h, {"normal_destination": "бот"})
        h.process_normal_file.assert_awaited_once()
        h.process_anti_file.assert_not_awaited()

    def test_mode1_normal_destination_plagiscan_calls_process_anti_file(self):
        h = _make_handlers()
        h.manager.file_queue = [{"file_name": "f.docx", "original_file_name": "f.docx",
                                  "is_anti": False, "force_plagiscan": False}]
        h.process_normal_file = AsyncMock()
        h.process_anti_file = AsyncMock()
        self._run(h, {"normal_destination": "плагискан"})
        h.process_anti_file.assert_awaited_once()
        h.process_normal_file.assert_not_awaited()

    def test_force_plagiscan_overrides_normal_destination_bot(self):
        """force_plagiscan=True → плагискан, даже если normal_destination=бот."""
        h = _make_handlers()
        h.manager.file_queue = [{"file_name": "victoria.docx", "original_file_name": "victoria.docx",
                                  "is_anti": False, "force_plagiscan": True}]
        h.process_normal_file = AsyncMock()
        h.process_anti_file = AsyncMock()
        self._run(h, {"normal_destination": "бот", "plagiscan_users": ["@victoria"]})
        h.process_anti_file.assert_awaited_once()
        h.process_normal_file.assert_not_awaited()

    def test_force_plagiscan_overrides_normal_destination_editor(self):
        """force_plagiscan=True → плагискан, даже если normal_destination=редактор."""
        h = _make_handlers()
        h.manager.file_queue = [{"file_name": "victoria.docx", "original_file_name": "victoria.docx",
                                  "is_anti": False, "force_plagiscan": True}]
        h.process_normal_file = AsyncMock()
        h.process_anti_file = AsyncMock()
        h.send_to_nik2 = AsyncMock()
        self._run(h, {"normal_destination": "редактор", "plagiscan_users": ["@victoria"]})
        h.process_anti_file.assert_awaited_once()
        h.process_normal_file.assert_not_awaited()
        h.send_to_nik2.assert_not_awaited()

    def test_mode1_normal_destination_editor_calls_send_to_nik2(self):
        h = _make_handlers()
        h.manager.file_queue = [{"file_name": "f.docx", "original_file_name": "f.docx",
                                  "is_anti": False, "force_plagiscan": False}]
        h.process_normal_file = AsyncMock()
        h.process_anti_file = AsyncMock()
        h.send_to_nik2 = AsyncMock()
        self._run(h, {"normal_destination": "редактор"})
        h.send_to_nik2.assert_awaited_once()
        h.process_normal_file.assert_not_awaited()

    def test_mode1_anti_file_calls_process_anti_file(self):
        h = _make_handlers()
        h.manager.file_queue = [{"file_name": "АНТИ.docx", "original_file_name": "АНТИ.docx",
                                  "is_anti": True, "force_plagiscan": False}]
        h.process_normal_file = AsyncMock()
        h.process_anti_file = AsyncMock()
        self._run(h, {})
        h.process_anti_file.assert_awaited_once()
        h.process_normal_file.assert_not_awaited()

    def test_empty_queue_no_crash(self):
        h = _make_handlers()
        h.manager.file_queue = []
        h.process_normal_file = AsyncMock()
        h.process_anti_file = AsyncMock()
        self._run(h, {})  # should not raise
        h.process_normal_file.assert_not_awaited()

    # ── mode2 ──────────────────────────────────────────────────────────────

    def test_mode2_outside_hours_sends_to_editor(self):
        h = _make_handlers()
        h.manager.file_queue = [{"file_name": "f.docx", "original_file_name": "f.docx",
                                  "is_anti": False, "force_plagiscan": False,
                                  "message": MagicMock()}]
        h.process_normal_file = AsyncMock()
        h.send_to_24_7_editor = AsyncMock()
        h.is_within_working_hours = MagicMock(return_value=False)
        h.check_daily_limit = MagicMock(return_value=True)
        h.aaa_globally_unavailable = False
        self._run(h, {"mode": "mode2"})
        h.send_to_24_7_editor.assert_awaited_once()
        h.process_normal_file.assert_not_awaited()

    def test_mode2_within_hours_and_limit_ok_calls_process_normal_file(self):
        h = _make_handlers()
        h.manager.file_queue = [{"file_name": "f.docx", "original_file_name": "f.docx",
                                  "is_anti": False, "force_plagiscan": False}]
        h.process_normal_file = AsyncMock()
        h.send_to_24_7_editor = AsyncMock()
        h.is_within_working_hours = MagicMock(return_value=True)
        h.check_daily_limit = MagicMock(return_value=True)
        h.aaa_globally_unavailable = False
        self._run(h, {"mode": "mode2"})
        h.process_normal_file.assert_awaited_once()
        h.send_to_24_7_editor.assert_not_awaited()

    def test_mode2_within_hours_normal_destination_editor_calls_work_editor_route(self):
        h = _make_handlers()
        h.manager.file_queue = [{"file_name": "f.docx", "original_file_name": "f.docx",
                                  "is_anti": False, "force_plagiscan": False}]
        h.process_normal_file = AsyncMock()
        h.process_anti_file = AsyncMock()
        h.send_to_nik2 = AsyncMock()
        h.send_to_24_7_editor = AsyncMock()
        h.is_within_working_hours = MagicMock(return_value=True)
        h.check_daily_limit = MagicMock(return_value=True)
        h.aaa_globally_unavailable = False

        self._run(h, {"mode": "mode2", "normal_destination": "редактор"})

        h.send_to_nik2.assert_awaited_once()
        forwarded, = h.send_to_nik2.await_args.args
        self.assertEqual("обычные файлы направляются редактору", forwarded["reason"])
        h.send_to_24_7_editor.assert_not_awaited()
        h.process_normal_file.assert_not_awaited()
        h.process_anti_file.assert_not_awaited()

    def test_mode2_within_hours_normal_destination_plagiscan_calls_plagiscan_without_force(self):
        h = _make_handlers()
        h.manager.file_queue = [{"file_name": "f.docx", "original_file_name": "f.docx",
                                  "is_anti": False, "force_plagiscan": False}]
        h.process_normal_file = AsyncMock()
        h.process_anti_file = AsyncMock()
        h.send_to_24_7_editor = AsyncMock()
        h.is_within_working_hours = MagicMock(return_value=True)
        h.check_daily_limit = MagicMock(return_value=True)
        h.aaa_globally_unavailable = False

        self._run(h, {"mode": "mode2", "normal_destination": "плагискан"})

        h.process_anti_file.assert_awaited_once()
        routed = h.process_anti_file.await_args.args[0]
        self.assertTrue(routed["is_anti"])
        self.assertFalse(routed["force_plagiscan"])
        self.assertTrue(routed["via_normal_destination"])
        h.process_normal_file.assert_not_awaited()
        h.send_to_24_7_editor.assert_not_awaited()

    def test_mode2_within_hours_anti_destination_editor_calls_process_anti_file(self):
        h = _make_handlers()
        h.manager.file_queue = [{"file_name": "АНТИ.docx", "original_file_name": "АНТИ.docx",
                                  "is_anti": True, "force_plagiscan": False}]
        h.process_anti_file = AsyncMock()
        h.send_to_24_7_editor = AsyncMock()
        h.is_within_working_hours = MagicMock(return_value=True)
        h.check_daily_limit = MagicMock(return_value=True)
        h.aaa_globally_unavailable = False

        self._run(h, {"mode": "mode2", "anti_destination": "редактор"})

        h.process_anti_file.assert_awaited_once()
        h.send_to_24_7_editor.assert_not_awaited()

    def test_mode2_force_plagiscan_bypasses_outside_hours_and_destinations(self):
        h = _make_handlers()
        h.manager.file_queue = [{"file_name": "force.docx", "original_file_name": "force.docx",
                                  "is_anti": False, "force_plagiscan": True}]
        h.process_anti_file = AsyncMock()
        h.send_to_nik2 = AsyncMock()
        h.send_to_24_7_editor = AsyncMock()
        h.is_within_working_hours = MagicMock(return_value=False)
        h.check_daily_limit = MagicMock(return_value=False)
        h.aaa_globally_unavailable = True

        self._run(h, {"mode": "mode2", "normal_destination": "редактор", "anti_destination": "редактор"})

        h.process_anti_file.assert_awaited_once()
        routed = h.process_anti_file.await_args.args[0]
        self.assertTrue(routed["force_plagiscan"])
        h.send_to_24_7_editor.assert_not_awaited()
        h.send_to_nik2.assert_not_awaited()

    def test_mode2_limit_exceeded_sends_to_editor(self):
        h = _make_handlers()
        h.manager.file_queue = [{"file_name": "f.docx", "original_file_name": "f.docx",
                                  "is_anti": False, "force_plagiscan": False,
                                  "message": MagicMock()}]
        h.process_normal_file = AsyncMock()
        h.send_to_24_7_editor = AsyncMock()
        h.is_within_working_hours = MagicMock(return_value=True)
        h.check_daily_limit = MagicMock(return_value=False)  # exceeded
        h.aaa_globally_unavailable = False
        self._run(h, {"mode": "mode2"})
        h.send_to_24_7_editor.assert_awaited_once()

    def test_mode2_aaa_globally_unavailable_sends_to_editor(self):
        h = _make_handlers()
        h.manager.file_queue = [{"file_name": "f.docx", "original_file_name": "f.docx",
                                  "is_anti": False, "force_plagiscan": False,
                                  "message": MagicMock()}]
        h.process_normal_file = AsyncMock()
        h.send_to_24_7_editor = AsyncMock()
        h.is_within_working_hours = MagicMock(return_value=True)
        h.check_daily_limit = MagicMock(return_value=True)
        h.aaa_globally_unavailable = True
        self._run(h, {"mode": "mode2"})
        h.send_to_24_7_editor.assert_awaited_once()

    def test_mode2_anti_file_outside_hours_sends_to_editor(self):
        """mode2: вне рабочих часов анти-файл тоже идёт круглосуточному редактору."""
        h = _make_handlers()
        h.manager.file_queue = [{"file_name": "АНТИ.docx", "original_file_name": "АНТИ.docx",
                                  "is_anti": True, "force_plagiscan": False,
                                  "message": MagicMock()}]
        h.process_anti_file = AsyncMock()
        h.send_to_24_7_editor = AsyncMock()
        h.is_within_working_hours = MagicMock(return_value=False)
        h.check_daily_limit = MagicMock(return_value=True)
        h.aaa_globally_unavailable = False
        print("\n========== СЦЕНАРИЙ: MODE2 АНТИ-ФАЙЛ ВНЕ РАБОЧЕГО ВРЕМЕНИ ==========")
        print("⚙️  Настройки:")
        print("   mode: mode2")
        print("   time: вне рабочего времени")
        print("   editor_24_7: @editor247")
        print("   anti_destination: бот")
        print("   normal_destination: бот")
        print("📥 Очередь:")
        print("   1. АНТИ.docx | тип=АНТИ | force_plagiscan=False | source=telegram")
        self._run(h, {"mode": "mode2"})
        print("📤 Ожидаемый маршрут:")
        print("   АНТИ.docx -> круглосуточный редактор")
        print("✅ Проверка:")
        print("   send_to_24_7_editor вызван 1 раз")
        print("   process_anti_file не вызван")
        h.send_to_24_7_editor.assert_awaited_once()
        h.process_anti_file.assert_not_awaited()

    # ── plagiscan_users routing ────────────────────────────────────────────

    def test_plagiscan_users_empty_no_force(self):
        """plagiscan_users=[] → force_plagiscan=False → normal_destination=бот → process_normal_file."""
        h = _make_handlers()
        h.manager.file_queue = [{"file_name": "f.docx", "original_file_name": "f.docx",
                                  "is_anti": False, "force_plagiscan": False}]
        h.process_normal_file = AsyncMock()
        h.process_anti_file = AsyncMock()
        self._run(h, {"plagiscan_users": [], "normal_destination": "бот"})
        h.process_normal_file.assert_awaited_once()

    def test_is_force_plagiscan_author_by_tg_at_username(self):
        """@username в plagiscan_users совпадает с author из TG."""
        h = _make_handlers()
        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None:
                   {"plagiscan_users": ["@vika_user"]}.get(key, default)):
            result = h.is_force_plagiscan_author(author="vika_user")
        self.assertTrue(result)

    def test_is_force_plagiscan_author_by_vk_prefix(self):
        """vk:123456 в plagiscan_users совпадает с route_sender_id='123456'."""
        h = _make_handlers()
        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None:
                   {"plagiscan_users": ["vk:123456"]}.get(key, default)):
            result = h.is_force_plagiscan_author(author="Беседа", route_sender_id="123456")
        self.assertTrue(result)

    def test_is_force_plagiscan_author_unlisted_returns_false(self):
        h = _make_handlers()
        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None:
                   {"plagiscan_users": ["@other_user"]}.get(key, default)):
            result = h.is_force_plagiscan_author(author="regular_user", author_id=999)
        self.assertFalse(result)

    def test_is_force_plagiscan_author_by_tg_numeric_id(self):
        h = _make_handlers()
        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None:
                   {"plagiscan_users": ["987654321"]}.get(key, default)):
            result = h.is_force_plagiscan_author(author=987654321, author_id=987654321)
        self.assertTrue(result)


# ══════════════════════════════════════════════════════════════════════════════
# _job_counter_datetime — VK Long Poll / local DB datetime extraction
# ══════════════════════════════════════════════════════════════════════════════

class TestJobCounterDatetime(unittest.TestCase):
    """_job_counter_datetime должен извлекать datetime из transport_meta или created_at."""

    def setUp(self):
        with patch("bot_handlers.Config") as cfg:
            cfg.get_setting.return_value = None
            self.processor = CounterModeProcessor(_make_account_manager())

    def _make_job(self, transport_meta=None, created_at=None):
        j = MagicMock()
        j.transport_meta = transport_meta
        j.created_at = created_at
        return j

    def test_vk_message_date_from_transport_meta(self):
        """transport_meta["vk_message_date"] → datetime.fromtimestamp."""
        ts = 1746540000  # известная дата
        job = self._make_job(transport_meta={"vk_message_date": str(ts)})
        result = self.processor._job_counter_datetime(job)
        from datetime import datetime
        expected = datetime.fromtimestamp(ts)
        self.assertEqual(result, expected)

    def test_falls_back_to_created_at_when_no_transport_meta(self):
        """Нет vk_message_date → берём created_at."""
        job = self._make_job(transport_meta={}, created_at="2026-05-06T10:30:00")
        result = self.processor._job_counter_datetime(job)
        self.assertEqual(result.hour, 10)
        self.assertEqual(result.minute, 30)

    def test_none_transport_meta_uses_created_at(self):
        """transport_meta=None → берём created_at."""
        job = self._make_job(transport_meta=None, created_at="2026-05-06T15:00:00")
        result = self.processor._job_counter_datetime(job)
        self.assertEqual(result.hour, 15)

    def test_invalid_created_at_returns_datetime_now(self):
        """Если created_at не парсится → возвращаем datetime.now() (не падаем)."""
        job = self._make_job(transport_meta=None, created_at="not-a-date")
        result = self.processor._job_counter_datetime(job)
        self.assertIsInstance(result, datetime)

    def test_invalid_vk_ts_falls_back_to_created_at(self):
        """Невалидный vk_message_date → fallback на created_at."""
        job = self._make_job(
            transport_meta={"vk_message_date": "not-a-timestamp"},
            created_at="2026-05-06T12:00:00"
        )
        result = self.processor._job_counter_datetime(job)
        self.assertEqual(result.hour, 12)

    def test_empty_vk_message_date_falls_back_to_created_at(self):
        """vk_message_date="" → fallback на created_at."""
        job = self._make_job(
            transport_meta={"vk_message_date": ""},
            created_at="2026-05-06T09:45:00"
        )
        result = self.processor._job_counter_datetime(job)
        self.assertEqual(result.hour, 9)
        self.assertEqual(result.minute, 45)


# ══════════════════════════════════════════════════════════════════════════════
# VK Long Poll ReadTimeout — должен молча продолжать без traceback
# ══════════════════════════════════════════════════════════════════════════════

class TestVkLongPollReadTimeout(unittest.TestCase):
    """_is_longpoll_timeout определяет штатный таймаут VK long poll без traceback."""

    def _import_fn(self):
        from multichannel_gateway.vk_longpoll import _is_longpoll_timeout
        return _is_longpoll_timeout

    def test_requests_read_timeout_is_detected(self):
        try:
            from requests.exceptions import ReadTimeout
            fn = self._import_fn()
            self.assertTrue(fn(ReadTimeout()))
        except ImportError:
            self.skipTest("requests not installed")

    def test_urllib3_read_timeout_is_detected(self):
        try:
            from urllib3.exceptions import ReadTimeoutError
            fn = self._import_fn()
            self.assertTrue(fn(ReadTimeoutError(None, None, "read timed out")))
        except ImportError:
            self.skipTest("urllib3 not installed")

    def test_generic_exception_is_not_timeout(self):
        fn = self._import_fn()
        self.assertFalse(fn(ValueError("something else")))
        self.assertFalse(fn(RuntimeError("network error")))

    def test_class_named_read_timeout_is_detected_without_import(self):
        """Fallback: проверяем по имени класса если import не доступен."""
        fn = self._import_fn()
        class ReadTimeout(Exception):
            pass
        self.assertTrue(fn(ReadTimeout()))

    def test_run_forever_does_not_print_on_read_timeout(self):
        """run_forever не должен печатать ошибку при ReadTimeout."""
        import asyncio
        import io

        call_count = [0]

        # Create a real exception class that _is_longpoll_timeout will recognize
        class ReadTimeout(Exception):
            pass

        async def fake_run_once():
            call_count[0] += 1
            if call_count[0] == 1:
                raise ReadTimeout()
            raise KeyboardInterrupt()

        captured = io.StringIO()
        try:
            from multichannel_gateway.vk_longpoll import VkLongPollRunner
            runner = VkLongPollRunner.__new__(VkLongPollRunner)
            runner.client = MagicMock()
            runner.log = MagicMock()
            runner.run_once = fake_run_once

            with patch("multichannel_gateway.vk_longpoll.append_channel_log"), \
                 patch("sys.stdout", captured):
                try:
                    asyncio.run(runner.run_forever(retry_delay_seconds=0))
                except KeyboardInterrupt:
                    pass
        except ImportError:
            self.skipTest("vk_longpoll not importable in test env")

        output = captured.getvalue()
        self.assertNotIn("❌", output, f"Не должно быть ошибок при ReadTimeout: {output!r}")
        self.assertNotIn("Traceback", output)


# ══════════════════════════════════════════════════════════════════════════════
# anti_destination routing — через _anti_destination_for
# ══════════════════════════════════════════════════════════════════════════════

class TestAntiDestinationRouting(unittest.TestCase):
    """_anti_destination_for должен правильно определять куда идёт анти-файл."""

    def _dest(self, handlers, file_info, settings):
        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None:
                   settings.get(key, default)):
            return handlers._anti_destination_for(file_info)

    def test_force_plagiscan_always_returns_bot(self):
        """force_plagiscan=True → всегда 'бот' (плагискан), независимо от anti_destination."""
        h = _make_handlers()
        for anti_dest in ("бот", "редактор"):
            result = self._dest(h, {"force_plagiscan": True}, {"anti_destination": anti_dest})
            self.assertEqual(result, "бот", f"force_plagiscan с anti_destination={anti_dest} должен давать 'бот'")

    def test_no_filter_returns_config_anti_destination(self):
        """Без фильтра → используем anti_destination из конфига."""
        h = _make_handlers()
        for dest in ("бот", "редактор"):
            result = self._dest(
                h,
                {"force_plagiscan": False},
                {"anti_destination": dest, "plagiscan_users": []}
            )
            self.assertEqual(result, dest)

    def test_with_filter_active_still_returns_config_anti_destination(self):
        """plagiscan_users не меняет анти-маршрут обычных авторов: используем anti_destination."""
        h = _make_handlers()
        result = self._dest(
            h,
            {"force_plagiscan": False},
            {"anti_destination": "бот", "plagiscan_users": ["@vika"]}
        )
        self.assertEqual(result, "бот")

    def test_via_normal_destination_always_returns_bot(self):
        """normal_destination=плагискан идёт в Плагискан, даже если anti_destination=редактор."""
        h = _make_handlers()
        result = self._dest(
            h,
            {"force_plagiscan": False, "via_normal_destination": True},
            {"anti_destination": "редактор", "plagiscan_users": ["@vika"]}
        )
        self.assertEqual(result, "бот")

    def test_anti_destination_editor_config(self):
        h = _make_handlers()
        result = self._dest(
            h,
            {"force_plagiscan": False},
            {"anti_destination": "редактор", "plagiscan_users": []}
        )
        self.assertEqual(result, "редактор")


# ══════════════════════════════════════════════════════════════════════════════
# Parallel processing — несколько файлов одновременно
# ══════════════════════════════════════════════════════════════════════════════

class TestParallelProcessing(unittest.TestCase):
    """Несколько файлов в очереди — должны обрабатываться с учётом max_concurrent."""

    def test_second_file_not_started_when_max_concurrent_reached(self):
        """Если max_concurrent=1 и уже 1 файл в обработке — второй не стартует."""
        h = _make_handlers()
        # manager.processing_files is what process_queue checks (not current_processing_files)
        h.manager.processing_files = {"AAA-1": {"first.docx": {}}}
        h.manager.file_queue = [
            {"file_name": "second.docx", "original_file_name": "second.docx",
             "is_anti": False, "force_plagiscan": False}
        ]
        h.process_normal_file = AsyncMock()

        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None:
                   {"max_concurrent": 1, "mode": "mode1", "normal_destination": "бот",
                    "plagiscan_users": []}.get(key, default)):
            asyncio.run(h.process_queue())

        h.process_normal_file.assert_not_awaited()

    def test_second_file_starts_when_slots_available(self):
        """max_concurrent=2, 1 файл уже обрабатывается → второй стартует."""
        h = _make_handlers()
        h.manager.processing_files = {"AAA-1": {"first.docx": {}}}
        h.manager.file_queue = [
            {"file_name": "second.docx", "original_file_name": "second.docx",
             "is_anti": False, "force_plagiscan": False}
        ]
        h.process_normal_file = AsyncMock()

        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None:
                   {"max_concurrent": 2, "mode": "mode1", "normal_destination": "бот",
                    "plagiscan_users": []}.get(key, default)):
            asyncio.run(h.process_queue())

        h.process_normal_file.assert_awaited_once()

    def test_anti_and_normal_can_be_processed_in_parallel(self):
        """Анти и обычный файл могут обрабатываться параллельно (НИК-1/2 не занимают обычные слоты)."""
        h = _make_handlers()
        # НИК-2 processing anti — does NOT count toward normal_slots
        h.manager.processing_files = {"НИК-2": {"anti.docx": {}}}
        h.manager.file_queue = [
            {"file_name": "normal.docx", "original_file_name": "normal.docx",
             "is_anti": False, "force_plagiscan": False}
        ]
        h.process_normal_file = AsyncMock()

        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None:
                   {"max_concurrent": 1, "mode": "mode1", "normal_destination": "бот",
                    "plagiscan_users": []}.get(key, default)):
            asyncio.run(h.process_queue())

        h.process_normal_file.assert_awaited_once()


class TestMixedVkTgEndToEndRouting(unittest.TestCase):
    """Смешанный вход VK+TG в общей очереди без реальных сетевых вызовов."""

    BASE_SETTINGS = {
        "max_concurrent": 7,
        "mode": "mode2",
        "normal_destination": "бот",
        "anti_destination": "бот",
        "plagiscan_users": ["vk:700", "tg_plagi"],
    }

    @staticmethod
    def _file_info(source, file_name, *, author, author_id=None, is_anti=False,
                   force_plagiscan=False, route_sender_id=None, route_sender_name=None):
        return {
            "author": author,
            "author_id": author_id,
            "file_name": file_name,
            "original_file_name": file_name,
            "message_id": f"{source}-msg-{file_name}",
            "chat_id": f"{source}-chat",
            "message": MagicMock() if source == "telegram" else None,
            "is_anti": is_anti,
            "force_plagiscan": force_plagiscan,
            "source_platform": source,
            "route_sender_id": route_sender_id,
            "route_sender_name": route_sender_name,
            "route_chat_id": f"{source}-route-chat",
            "file_uid": f"{source}:{file_name}:0",
            "local_path": __file__,
            "gateway_job_id": 100 if source == "vk" else 200,
        }

    def _run_all(self, handlers, settings=None):
        merged = {**self.BASE_SETTINGS, **(settings or {})}
        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None: merged.get(key, default)):
            while handlers.manager.file_queue:
                asyncio.run(handlers.process_queue())

    def test_mode2_outside_hours_routes_mixed_vk_tg_queue_by_file_rules(self):
        h = _make_handlers()
        routed = []

        async def fake_anti(file_info):
            routed.append(("plagiscan", file_info["source_platform"], file_info["file_name"]))
            print(f"🛡️  {file_info['file_name']} [{file_info['source_platform']}] -> Плагискан")

        async def fake_editor(message, file_info):
            routed.append(("editor247", file_info["source_platform"], file_info["file_name"]))
            print(f"📤 {file_info['file_name']} [{file_info['source_platform']}] -> круглосуточный редактор")

        h.process_anti_file = AsyncMock(side_effect=fake_anti)
        h.process_normal_file = AsyncMock()
        h.send_to_24_7_editor = AsyncMock(side_effect=fake_editor)
        h.is_within_working_hours = MagicMock(return_value=False)
        h.check_daily_limit = MagicMock(return_value=True)

        h.manager.file_queue = [
            self._file_info("vk", "vk_regular.docx", author="VK Chat", route_sender_id="701"),
            self._file_info("telegram", "tg_regular.docx", author="tg_regular", author_id=11),
            self._file_info("vk", "vk_anti анти.docx", author="VK Chat", is_anti=True, route_sender_id="701"),
            self._file_info("telegram", "tg_plagi.docx", author="tg_plagi", author_id=12, force_plagiscan=True),
        ]

        print("\n========== СЦЕНАРИЙ: СМЕШАННАЯ ОЧЕРЕДЬ VK+TG В MODE2 ВНЕ ВРЕМЕНИ ==========")
        print("⚙️  Настройки:")
        print("   mode: mode2")
        print("   time: вне рабочего времени")
        print("   editor_24_7: @editor247")
        print("   anti_destination: бот")
        print("   normal_destination: бот")
        print("   plagiscan_users: vk:700, tg_plagi")
        print("📥 Очередь:")
        for idx, item in enumerate(h.manager.file_queue, 1):
            file_type = "АНТИ" if item.get("is_anti") else "обычный"
            print(
                f"   {idx}. {item['file_name']} | source={item['source_platform']} "
                f"| тип={file_type} | force_plagiscan={item.get('force_plagiscan')}"
            )
        print("📤 Маршрутизация:")
        self._run_all(h)
        print("✅ Проверка маршрутов:")
        for route, source, file_name in routed:
            print(f"   {file_name} [{source}] => {route}")

        self.assertIn(("editor247", "vk", "vk_regular.docx"), routed)
        self.assertIn(("editor247", "telegram", "tg_regular.docx"), routed)
        self.assertIn(("editor247", "vk", "vk_anti анти.docx"), routed)
        self.assertIn(("plagiscan", "telegram", "tg_plagi.docx"), routed)
        h.process_normal_file.assert_not_awaited()

    def test_mode2_within_hours_mixed_sources_keep_plagiscan_filter_and_normal_flow(self):
        h = _make_handlers()
        routed = []

        async def fake_anti(file_info):
            routed.append((
                "anti_flow",
                h._anti_destination_for(file_info),
                file_info["source_platform"],
                file_info["file_name"],
            ))

        async def fake_normal(file_info):
            routed.append(("normal_flow", "бот", file_info["source_platform"], file_info["file_name"]))

        h.process_anti_file = AsyncMock(side_effect=fake_anti)
        h.process_normal_file = AsyncMock(side_effect=fake_normal)
        h.send_to_24_7_editor = AsyncMock()
        h.is_within_working_hours = MagicMock(return_value=True)
        h.check_daily_limit = MagicMock(return_value=True)

        h.manager.file_queue = [
            self._file_info("vk", "vk_listed анти.docx", author="VK Chat", is_anti=True, route_sender_id="700"),
            self._file_info("vk", "vk_unlisted анти.docx", author="VK Chat", is_anti=True, route_sender_id="701"),
            self._file_info("telegram", "tg_plagi.docx", author="tg_plagi", author_id=12, force_plagiscan=True),
            self._file_info("telegram", "tg_regular.docx", author="tg_regular", author_id=13),
            self._file_info("vk", "vk_regular.docx", author="VK Chat", route_sender_id="702"),
        ]

        self._run_all(h)

        self.assertIn(("anti_flow", "бот", "vk", "vk_listed анти.docx"), routed)
        self.assertIn(("anti_flow", "бот", "vk", "vk_unlisted анти.docx"), routed)
        self.assertIn(("anti_flow", "бот", "telegram", "tg_plagi.docx"), routed)
        self.assertIn(("normal_flow", "бот", "telegram", "tg_regular.docx"), routed)
        self.assertIn(("normal_flow", "бот", "vk", "vk_regular.docx"), routed)
        h.send_to_24_7_editor.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()


class TestVkSenderNameResolution(unittest.TestCase):
    """VK screen_name должен подтягиваться в JobRecord через ingest() и доходить до is_force_plagiscan_author."""

    def _make_gateway(self, fetch_result):
        from multichannel_gateway.core.router import InboundGateway
        from multichannel_gateway.core.storage import SqliteJobStore
        from multichannel_gateway.core.files import AtomicFileStore
        import tempfile, os

        tmp = tempfile.mkdtemp()
        store = SqliteJobStore(os.path.join(tmp, "jobs.db"))
        file_store = AtomicFileStore(os.path.join(tmp, "files"))
        gw = InboundGateway(store=store, file_store=file_store)
        # Подменяем _fetch_vk_sender_name так, чтобы не делать реальный API-запрос
        gw._fetch_vk_sender_name = lambda sender_id: fetch_result
        return gw

    def _make_vk_event(self, from_id: int, file_title: str = "test.docx"):
        return {
            "type": "message_new",
            "object": {
                "message": {
                    "id": 1,
                    "peer_id": 2000000001,
                    "from_id": from_id,
                    "date": 9999999999,
                    "text": "",
                    "attachments": [
                        {
                            "type": "doc",
                            "doc": {
                                "id": 1,
                                "title": file_title,
                                "ext": "docx",
                                "url": "https://example.com/test.docx",
                                "size": 100,
                            },
                        }
                    ],
                }
            },
        }

    def test_vk_sender_name_stored_in_job(self):
        """ingest() должен записать screen_name в job.sender_name."""
        import asyncio
        from multichannel_gateway.adapters.vk import VkCallbackAdapter
        from multichannel_gateway.core.models import DownloadedAttachment

        gw = self._make_gateway(fetch_result="gosdohnem")
        adapter = VkCallbackAdapter()
        event = self._make_vk_event(from_id=1106569752)
        envelope = adapter.build_envelope(event)
        self.assertIsNone(envelope.sender_name)  # адаптер ставит None

        fake_dl = DownloadedAttachment(original_file_name="test.docx", content_bytes=b"test", size_bytes=4)
        with patch.object(adapter, "download_attachment", new=AsyncMock(return_value=fake_dl)):
            jobs = asyncio.run(gw.ingest(adapter, envelope))
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].sender_name, "gosdohnem")

    def test_vk_sender_name_none_when_lookup_fails(self):
        """Если lookup упал — sender_name остаётся None, job всё равно создаётся."""
        import asyncio
        from multichannel_gateway.adapters.vk import VkCallbackAdapter
        from multichannel_gateway.core.models import DownloadedAttachment

        gw = self._make_gateway(fetch_result=None)
        adapter = VkCallbackAdapter()
        event = self._make_vk_event(from_id=9999999)
        envelope = adapter.build_envelope(event)

        fake_dl = DownloadedAttachment(original_file_name="test.docx", content_bytes=b"test", size_bytes=4)
        with patch.object(adapter, "download_attachment", new=AsyncMock(return_value=fake_dl)):
            jobs = asyncio.run(gw.ingest(adapter, envelope))
        self.assertEqual(len(jobs), 1)
        self.assertIsNone(jobs[0].sender_name)

    def test_is_force_plagiscan_matches_via_route_sender_name(self):
        """is_force_plagiscan_author: bare username матчит и TG и VK."""
        h = _make_handlers()
        # С vk: префиксом — совпадает только по VK route_sender_name
        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None:
                   {"plagiscan_users": ["vk:gosdohnem"]}.get(key, default)):
            result = h.is_force_plagiscan_author(
                author="Беседа",
                author_id=None,
                route_sender_id="1106569752",
                route_sender_name="gosdohnem",
            )
        self.assertTrue(result)
        # Без префикса — bare строка матчит ОБА: TG и VK
        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None:
                   {"plagiscan_users": ["gosdohnem"]}.get(key, default)):
            result_bare = h.is_force_plagiscan_author(
                author="Беседа",
                author_id=None,
                route_sender_id="1106569752",
                route_sender_name="gosdohnem",
            )
        self.assertTrue(result_bare)
        # Bare TG username тоже матчит TG author
        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None:
                   {"plagiscan_users": ["gosdohnem"]}.get(key, default)):
            result_tg = h.is_force_plagiscan_author(
                author="gosdohnem",
                author_id=None,
                route_sender_id=None,
                route_sender_name=None,
            )
        self.assertTrue(result_tg)

    def test_is_force_plagiscan_no_match_without_sender_name(self):
        """Без route_sender_name numeric ID не сопоставится со screen_name."""
        h = _make_handlers()
        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None:
                   {"plagiscan_users": ["gosdohnem"]}.get(key, default)):
            result = h.is_force_plagiscan_author(
                author="Беседа",
                author_id=None,
                route_sender_id="1106569752",
                route_sender_name=None,
            )
        self.assertFalse(result)

    def test_plagiscan_user_allowed_even_without_allowed_authors(self):
        """Пользователь из plagiscan_users принимается даже если его нет в allowed_authors."""
        import asyncio
        from multichannel_gateway.adapters.vk import VkCallbackAdapter
        from multichannel_gateway.core.models import DownloadedAttachment
        from multichannel_gateway.core.router import InboundGateway
        from multichannel_gateway.core.storage import SqliteJobStore
        from multichannel_gateway.core.files import AtomicFileStore
        import tempfile, os

        tmp = tempfile.mkdtemp()
        store = SqliteJobStore(os.path.join(tmp, "jobs.db"))
        file_store = AtomicFileStore(os.path.join(tmp, "files"))
        gw = InboundGateway(store=store, file_store=file_store)
        gw._fetch_vk_sender_name = lambda sender_id: "gosdohnem"

        adapter = VkCallbackAdapter()
        event = self._make_vk_event(from_id=9876543)
        envelope = adapter.build_envelope(event)

        fake_dl = DownloadedAttachment(original_file_name="test.docx", content_bytes=b"test", size_bytes=4)

        def mock_config(key, default=None):
            return {"allowed_authors": ["1106569752"], "plagiscan_users": ["gosdohnem"]}.get(key, default)

        with patch("multichannel_gateway.core.router.InboundGateway._is_sender_allowed",
                   wraps=gw._is_sender_allowed):
            with patch("config.Config.get_setting", side_effect=mock_config):
                with patch.object(adapter, "download_attachment", new=AsyncMock(return_value=fake_dl)):
                    jobs = asyncio.run(gw.ingest(adapter, envelope))

        self.assertEqual(len(jobs), 1, "файл от plagiscan_user должен быть принят")


class TestPlagiscanNoChecksHandling(unittest.TestCase):
    """Плагискан отвечает 'У вас закончились проверки' — файл должен уйти редактору."""

    def test_is_no_checks_message_plagiscan_variant(self):
        h = _make_handlers()
        self.assertTrue(h._is_no_checks_message("У вас закончились проверки 😉\nПерейдите в раздел оплата /payment, чтобы пополнить баланс проверок."))

    def test_is_no_checks_message_plagiscan_reversed_phrase_variant(self):
        h = _make_handlers()
        self.assertTrue(h._is_no_checks_message("⏳ В данный момент проверки закончились.\nНачнем проверять после: 12:00 по мск времени."))

    def test_is_no_checks_message_aaa_variant(self):
        h = _make_handlers()
        self.assertTrue(h._is_no_checks_message("❌ У вас пока нет проверок. Для покупки нажмите команду /pay 💳"))

    def test_is_no_checks_message_false_for_success(self):
        h = _make_handlers()
        self.assertFalse(h._is_no_checks_message("✅ Ваш файл успешно проверен!\nОригинальность: 56.58%\nМашинная генерация: 0.0%"))

    def test_is_no_checks_message_false_for_service(self):
        h = _make_handlers()
        self.assertFalse(h._is_no_checks_message("🔄 Проверяем файл..."))

    def test_handle_anti_bot_response_no_checks_sends_to_editor(self):
        """handle_anti_bot_response при 'закончились проверки' перенаправляет файл редактору."""
        h = _make_handlers()
        file_key = "anti_test_key"
        processing_info = {
            "author": "testuser",
            "author_id": 123,
            "original_file_name": "325451 анти.docx",
            "file_name": "325451 анти.docx",
            "is_anti": True,
            "source_platform": "vk",
            "route_sender_id": "101064713",
            "chat_id": "vk:2000000001",
            "file_uid": "vk:x:1:0",
            "local_path": None,
            "message": None,
            "gateway_job_id": None,
            "route_chat_id": None,
        }
        h.current_processing_files[file_key] = processing_info

        mock_client = MagicMock()
        mock_client.name = "НИК-2"

        mock_message = MagicMock()
        mock_message.text = "У вас закончились проверки 😉\nПерейдите в раздел оплата /payment, чтобы пополнить баланс проверок."
        mock_message.document = None
        mock_message.reply_to_message = None

        h.find_anti_processing_for_response = MagicMock(return_value=(file_key, processing_info, "fallback"))
        h.send_to_nik2 = AsyncMock()
        h.manager.mark_account_free = MagicMock()

        asyncio.run(h.handle_anti_bot_response(mock_client, mock_message))

        h.send_to_nik2.assert_awaited_once()
        call_kwargs = h.send_to_nik2.call_args
        reason = call_kwargs[1].get("reason") or (call_kwargs[0][1] if len(call_kwargs[0]) > 1 else "")
        self.assertIn("проверк", reason.lower())
        self.assertNotIn(file_key, h.current_processing_files)
        h.manager.mark_account_free.assert_called_once_with("НИК-2", "325451 анти.docx")


def _normalize_console_author(platform, raw_value):
    value = (raw_value or "").strip()
    if not value:
        return ""
    if platform == "vk":
        value = value.lstrip("@").strip()
        lowered = value.lower()
        if lowered.startswith("vk:") or lowered.startswith("vk.com/") or lowered.startswith("https://vk.com/"):
            return value
        return f"vk:{value}"
    return value[1:] if value.startswith("@") else value


def _parse_allowed_authors_inputs(vk_input, tg_input, current_authors=None):
    current_authors = current_authors or []
    current_vk = [u for u in current_authors if str(u).lower().startswith("vk:") or str(u).lower().startswith("vk.com/")]
    current_tg = [u for u in current_authors if u not in current_vk]

    if vk_input == "0":
        new_vk = []
    elif vk_input:
        new_vk = [_normalize_console_author("vk", item) for item in vk_input.split(",") if item.strip()]
    else:
        new_vk = current_vk

    if tg_input == "0":
        new_tg = []
    elif tg_input:
        new_tg = [_normalize_console_author("tg", item.strip()) for item in tg_input.split(",") if item.strip()]
    else:
        new_tg = current_tg

    return new_vk + new_tg



def _display_or_not_set(items):
    return ", ".join(items) if items else "не задан"


class TestAdditionalPlatformNormalization(unittest.TestCase):
    def test_vk_username_with_at_is_stored_without_at(self):
        self.assertEqual("vk:gosdohnem", _normalize_console_author("vk", "@gosdohnem"))

    def test_vk_numeric_with_at_is_stored_without_at(self):
        self.assertEqual("vk:75057", _normalize_console_author("vk", "@75057"))

    def test_vk_digits_only_are_prefixed(self):
        self.assertEqual("vk:75057", _normalize_console_author("vk", "75057"))

    def test_vk_prefixed_value_is_kept_as_is(self):
        self.assertEqual("vk:gosdohnem", _normalize_console_author("vk", "vk:gosdohnem"))

    def test_tg_plain_username_has_no_prefix(self):
        self.assertEqual("Xlistyara", _normalize_console_author("tg", "Xlistyara"))

    def test_tg_at_prefix_is_stripped(self):
        self.assertEqual("Xlistyara", _normalize_console_author("tg", "@Xlistyara"))

    def test_router_vk_name_normalization_strips_at(self):
        from multichannel_gateway.core.router import InboundGateway

        self.assertEqual("gosdohnem", InboundGateway._normalize_vk_name("@gosdohnem"))


class TestAdditionalForcePlagiscanMatching(unittest.TestCase):
    def setUp(self):
        self.handlers = _make_handlers()

    def test_vk_prefixed_user_matches_prefixed_list(self):
        with patch("bot_handlers.Config.get_setting", return_value=["vk:gosdohnem"]):
            self.assertTrue(self.handlers.is_force_plagiscan_author(route_sender_name="gosdohnem"))

    def test_vk_user_does_not_match_tg_only_list(self):
        with patch("bot_handlers.Config.get_setting", return_value=["Xlistyara"]):
            self.assertFalse(self.handlers.is_force_plagiscan_author(route_sender_name="gosdohnem"))

    def test_tg_user_matches_tg_list(self):
        with patch("bot_handlers.Config.get_setting", return_value=["Xlistyara"]):
            self.assertTrue(self.handlers.is_force_plagiscan_author(author="Xlistyara"))

    def test_tg_user_matches_at_prefixed_entry(self):
        with patch("bot_handlers.Config.get_setting", return_value=["@Xlistyara"]):
            self.assertTrue(self.handlers.is_force_plagiscan_author(author="Xlistyara"))

    def test_numeric_vk_id_matches_prefixed_numeric_list(self):
        with patch("bot_handlers.Config.get_setting", return_value=["vk:123456"]):
            self.assertTrue(self.handlers.is_force_plagiscan_author(route_sender_id="123456"))

    def test_empty_force_plagiscan_list_returns_false(self):
        with patch("bot_handlers.Config.get_setting", return_value=[]):
            self.assertFalse(self.handlers.is_force_plagiscan_author(author="any-user"))

    def test_file_info_with_force_plagiscan_false_uses_normal_routing(self):
        handlers = _make_handlers()
        handlers.manager.file_queue = [{
            "file_name": "plain.docx",
            "original_file_name": "plain.docx",
            "is_anti": False,
            "force_plagiscan": False,
        }]
        handlers.process_normal_file = AsyncMock()
        handlers.process_anti_file = AsyncMock()
        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None: {
            "mode": "mode1",
            "normal_destination": "бот",
            "max_concurrent": 3,
        }.get(key, default)):
            asyncio.run(handlers.process_queue())
        handlers.process_normal_file.assert_awaited_once()
        handlers.process_anti_file.assert_not_awaited()


class TestAdditionalMode2ForcePlagiscanBypass(unittest.TestCase):
    def _make_file_info(self, force_plagiscan=False):
        return {
            "author": "student",
            "author_id": 42,
            "file_name": "essay.docx",
            "original_file_name": "essay.docx",
            "message": None,
            "chat_id": 100,
            "message_id": 200,
            "is_anti": False,
            "force_plagiscan": force_plagiscan,
        }

    def _run_queue(self, handlers, file_info, **settings):
        handlers.manager.file_queue = [file_info]
        handlers.process_anti_file = AsyncMock()
        handlers.process_normal_file = AsyncMock()
        handlers.send_to_24_7_editor = AsyncMock()
        config = {"mode": "mode2", "normal_destination": "бот", "max_concurrent": 5, **settings}
        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None: config.get(key, default)):
            asyncio.run(handlers.process_queue())
        return handlers

    def test_force_plagiscan_bypasses_outside_hours_check(self):
        h = _make_handlers()
        h.is_within_working_hours = lambda: False
        h.check_daily_limit = lambda: True
        self._run_queue(h, self._make_file_info(force_plagiscan=True))
        h.process_anti_file.assert_awaited_once()
        h.send_to_24_7_editor.assert_not_awaited()

    def test_force_plagiscan_bypasses_daily_limit_check(self):
        h = _make_handlers()
        h.is_within_working_hours = lambda: True
        h.check_daily_limit = lambda: False
        self._run_queue(h, self._make_file_info(force_plagiscan=True))
        h.process_anti_file.assert_awaited_once()
        h.send_to_24_7_editor.assert_not_awaited()

    def test_force_plagiscan_bypasses_aaa_unavailable_redirect(self):
        h = _make_handlers()
        h.is_within_working_hours = lambda: True
        h.check_daily_limit = lambda: True
        h.aaa_globally_unavailable = True
        self._run_queue(h, self._make_file_info(force_plagiscan=True))
        h.process_anti_file.assert_awaited_once()
        h.send_to_24_7_editor.assert_not_awaited()

    def test_regular_file_outside_hours_goes_to_editor(self):
        h = _make_handlers()
        h.is_within_working_hours = lambda: False
        h.check_daily_limit = lambda: True
        self._run_queue(h, self._make_file_info(force_plagiscan=False))
        h.send_to_24_7_editor.assert_awaited_once()
        h.process_anti_file.assert_not_awaited()
        h.process_normal_file.assert_not_awaited()

    def test_regular_file_over_limit_goes_to_editor(self):
        h = _make_handlers()
        h.is_within_working_hours = lambda: True
        h.check_daily_limit = lambda: False
        self._run_queue(h, self._make_file_info(force_plagiscan=False))
        h.send_to_24_7_editor.assert_awaited_once()
        h.process_anti_file.assert_not_awaited()

    def test_force_plagiscan_file_outside_hours_goes_to_plagiscan_not_editor(self):
        h = _make_handlers()
        h.is_within_working_hours = lambda: False
        h.check_daily_limit = lambda: False
        self._run_queue(h, self._make_file_info(force_plagiscan=True))
        h.process_anti_file.assert_awaited_once()
        h.send_to_24_7_editor.assert_not_awaited()


class TestAdditionalNoChecksHandling(unittest.TestCase):
    def _anti_processing_info(self, force_plagiscan=False):
        return {
            "author": "student",
            "author_id": 123,
            "original_file_name": "325451 анти.docx",
            "file_name": "325451 анти.docx",
            "is_anti": True,
            "force_plagiscan": force_plagiscan,
            "source_platform": "vk",
            "route_sender_id": "101064713",
            "chat_id": "vk:2000000001",
            "file_uid": "vk:x:1:0",
            "local_path": None,
            "message": None,
            "gateway_job_id": None,
            "route_chat_id": None,
        }

    def test_force_plagiscan_plagiscan_no_checks_drops_file(self):
        h = _make_handlers()
        file_key = "anti_force_key"
        processing_info = self._anti_processing_info(force_plagiscan=True)
        h.current_processing_files[file_key] = processing_info
        mock_client = SimpleNamespace(name="НИК-2")
        mock_message = SimpleNamespace(
            text="У вас закончились проверки 😉",
            document=None,
            reply_to_message=None,
            chat=SimpleNamespace(id=1),
            id=2,
        )
        h.find_anti_processing_for_response = MagicMock(return_value=(file_key, processing_info, "fallback"))
        h.send_to_nik2 = AsyncMock()
        h.manager.mark_account_free = MagicMock()

        asyncio.run(h.handle_anti_bot_response(mock_client, mock_message))

        h.send_to_nik2.assert_not_awaited()
        self.assertNotIn(file_key, h.current_processing_files)
        h.manager.mark_account_free.assert_called_once_with("НИК-2", "325451 анти.docx")

    def test_regular_plagiscan_no_checks_redirects_to_editor(self):
        h = _make_handlers()
        file_key = "anti_regular_key"
        processing_info = self._anti_processing_info(force_plagiscan=False)
        h.current_processing_files[file_key] = processing_info
        mock_client = SimpleNamespace(name="НИК-2")
        mock_message = SimpleNamespace(
            text="У вас закончились проверки 😉",
            document=None,
            reply_to_message=None,
            chat=SimpleNamespace(id=1),
            id=2,
        )
        h.find_anti_processing_for_response = MagicMock(return_value=(file_key, processing_info, "fallback"))
        h.send_to_nik2 = AsyncMock()
        h.manager.mark_account_free = MagicMock()

        asyncio.run(h.handle_anti_bot_response(mock_client, mock_message))

        h.send_to_nik2.assert_awaited_once()
        self.assertNotIn(file_key, h.current_processing_files)

    def test_handle_no_more_checks_force_plagiscan_drops_without_redirect(self):
        h = _make_handlers()
        file_key = "force_drop_key"
        processing_info = {
            "author": "student",
            "author_id": 1,
            "original_file_name": "essay.docx",
            "message_id": 11,
            "chat_id": 22,
            "bot_type": "normal",
            "message": None,
            "force_plagiscan": True,
            "file_uid": "uid-1",
            "local_path": None,
        }
        h.current_processing_files[file_key] = {"account": "AAA-1"}
        h.send_to_nik2 = AsyncMock()
        h.manager.blacklist_account = MagicMock()
        h.manager.mark_account_free = MagicMock()
        message = SimpleNamespace(chat=SimpleNamespace(id=55), id=66)
        client = SimpleNamespace(name="AAA-1")

        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None: {
            "normal_accounts": ["AAA-1", "AAA-2"],
        }.get(key, default)):
            asyncio.run(h.handle_no_more_checks(client, message, processing_info, file_key))

        h.send_to_nik2.assert_not_awaited()
        self.assertEqual([], h.manager.file_queue)
        self.assertNotIn(file_key, h.current_processing_files)


class TestAdditionalDeliveryCaptions(unittest.TestCase):
    def test_deliver_document_to_telegram_uses_no_caption(self):
        manager = _make_account_manager()
        client = MagicMock()
        client.is_connected = True
        client.send_document = AsyncMock(return_value=SimpleNamespace(id=1, chat=SimpleNamespace(id=2)))
        manager.get_client.side_effect = lambda name: client if name == "НИК-1" else None
        handlers = _make_handlers(manager)

        context = {"source_platform": "telegram", "author_id": 123}
        asyncio.run(handlers._deliver_document_to_origin(context, __file__, file_name="report.pdf"))

        _, kwargs = client.send_document.await_args
        self.assertIsNone(kwargs["caption"])

    def test_deliver_document_to_vk_builds_caption_from_filenames(self):
        handlers = _make_handlers()
        handlers.outbound_dispatcher.send_result = MagicMock(return_value={"status": "sent"})
        with patch.object(BotHandlers, "_vk_result_message_text", wraps=BotHandlers._vk_result_message_text) as vk_text:
            context = {
                "source_platform": "vk",
                "route_chat_id": 2000000001,
                "route_sender_id": "75057",
                "original_file_name": "essay.docx",
            }
            asyncio.run(handlers._deliver_document_to_origin(context, __file__, file_name="essay.pdf"))

        self.assertIn("Отчет: essay.pdf", vk_text.call_args.kwargs["caption"])
        self.assertIn("Исходный файл: essay.docx", vk_text.call_args.kwargs["caption"])


class TestAdditionalSenderSafety(unittest.TestCase):
    def test_handle_main_account_with_none_from_user_returns_early(self):
        h = _make_handlers()
        message = SimpleNamespace(
            from_user=None,
            document=SimpleNamespace(file_name="anon.docx", file_size=128),
            text=None,
            reply_to_message_id=None,
            chat=SimpleNamespace(id=1),
            id=2,
        )
        with patch("bot_handlers.ingest_pyrogram_message", new=AsyncMock()) as ingest_mock:
            asyncio.run(h.handle_main_account(MagicMock(), message))
        ingest_mock.assert_not_awaited()
        self.assertEqual([], h.manager.file_queue)

    def test_handle_bot_response_with_none_from_user_does_not_crash(self):
        h = _make_handlers()
        h.handle_ai_bot_response = AsyncMock()
        h.handle_anti_bot_response = AsyncMock()
        message = SimpleNamespace(from_user=None, text="service", document=None)
        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None: {
            "ai_bot": "@AAA_Report_AIBot",
            "anti_bot": "@plagaiscan_bot",
        }.get(key, default)):
            asyncio.run(h.handle_bot_response(SimpleNamespace(name="НИК-1"), message))
        h.handle_ai_bot_response.assert_not_awaited()
        h.handle_anti_bot_response.assert_not_awaited()


class TestAdditionalMode3FilenameNormalization(unittest.TestCase):
    def test_numeric_docx_normalizes_to_number(self):
        self.assertEqual("272727", CounterModeProcessor.normalize_filename("272727.docx"))

    def test_numeric_without_extension_stays_number(self):
        self.assertEqual("272727", CounterModeProcessor.normalize_filename("272727"))

    def test_numeric_pdf_normalizes_to_number(self):
        self.assertEqual("272727", CounterModeProcessor.normalize_filename("272727.pdf"))

    def test_anti_suffix_is_preserved(self):
        self.assertEqual("325451 анти", CounterModeProcessor.normalize_filename("325451 анти.docx"))

    def test_vk_encoded_special_chars_are_stripped(self):
        self.assertEqual("temastest", CounterModeProcessor.normalize_filename("tema_39_s_34_test.docx"))


class TestAdditionalAllowedAuthorsParsing(unittest.TestCase):
    def test_vk_plain_username_gets_vk_prefix(self):
        self.assertEqual(["vk:gosdohnem"], _parse_allowed_authors_inputs("gosdohnem", ""))

    def test_vk_username_with_at_gets_vk_prefix(self):
        self.assertEqual(["vk:gosdohnem"], _parse_allowed_authors_inputs("@gosdohnem", ""))

    def test_vk_numeric_id_gets_vk_prefix(self):
        self.assertEqual(["vk:123456789"], _parse_allowed_authors_inputs("123456789", ""))

    def test_vk_prefixed_value_is_not_double_prefixed(self):
        self.assertEqual(["vk:gosdohnem"], _parse_allowed_authors_inputs("vk:gosdohnem", ""))

    def test_tg_input_with_at_strips_at(self):
        self.assertEqual(["Xlistyara"], _parse_allowed_authors_inputs("", "@Xlistyara"))

    def test_tg_input_without_at_stays_plain(self):
        self.assertEqual(["Xlistyara"], _parse_allowed_authors_inputs("", "Xlistyara"))

    def test_mixed_vk_and_tg_inputs_are_combined(self):
        self.assertEqual(
            ["vk:gosdohnem", "vk:123456", "Xlistyara"],
            _parse_allowed_authors_inputs("gosdohnem,123456", "Xlistyara"),
        )


class TestAdditionalNik2BotSenderMatching(unittest.TestCase):
    def test_detects_configured_plagiscan_bot_sender(self):
        from main import FileDistributionBot

        self.assertTrue(
            FileDistributionBot._telegram_sender_matches_configured_bot(
                "plagaiscan_bot",
                "@plagaiscan_bot",
            )
        )
        self.assertTrue(
            FileDistributionBot._telegram_sender_matches_configured_bot(
                "@Plagaiscan_Bot",
                "plagaiscan_bot",
            )
        )

    def test_editor_sender_is_not_treated_as_plagiscan(self):
        from main import FileDistributionBot

        self.assertFalse(
            FileDistributionBot._telegram_sender_matches_configured_bot(
                "Xlistyara",
                "@plagaiscan_bot",
            )
        )

    def test_no_checks_notice_marks_editor_unavailable_without_tracking_match(self):
        from main import FileDistributionBot

        bot = FileDistributionBot.__new__(FileDistributionBot)
        bot.handlers = MagicMock()
        bot.handlers._is_no_checks_message.return_value = True

        handled = bot._handle_editor_no_checks_notice(
            "Xlistyara",
            "⏳ В данный момент проверки закончились.\nНачнем проверять после: 12:00 по мск времени.",
        )

        self.assertTrue(handled)
        bot.handlers._mark_editor_unavailable.assert_called_once_with("Xlistyara")

    def test_regular_editor_text_is_not_no_checks_notice(self):
        from main import FileDistributionBot

        bot = FileDistributionBot.__new__(FileDistributionBot)
        bot.handlers = MagicMock()
        bot.handlers._is_no_checks_message.return_value = False

        handled = bot._handle_editor_no_checks_notice("Xlistyara", "Проверено: 52")

        self.assertFalse(handled)
        bot.handlers._mark_editor_unavailable.assert_not_called()


class TestAdditionalEditorTrackingSafety(unittest.TestCase):
    def _ingest_result(self):
        return [SimpleNamespace(file_path=__file__, job_id=1, dedupe_key="telegram:1:1:0")]

    @staticmethod
    def _message(author, file_name):
        return SimpleNamespace(
            from_user=SimpleNamespace(username=author, id=123),
            document=SimpleNamespace(file_name=file_name, file_size=256),
            text=None,
            reply_to_message_id=None,
            chat=SimpleNamespace(id=1),
            id=2,
        )

    def test_active_editor_skips_force_plagiscan_check(self):
        h = _make_handlers()
        h.editor_tracking["track-1"] = {"destination": "@editor_user"}
        h.is_force_plagiscan_author = MagicMock(return_value=True)
        h.process_queue = AsyncMock()
        h.manager.get_file_lock = AsyncMock(return_value=asyncio.Lock())
        h.manager.get_file_status = AsyncMock(return_value="NEW")
        h.manager.set_file_status = AsyncMock()
        h.manager.reset_waiting_status = MagicMock()
        message = self._message("editor_user", "reply.docx")
        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None: {
            "ai_bot": "@AAA_Report_AIBot",
            "anti_bot": "@plagaiscan_bot",
        }.get(key, default)):
            with patch("bot_handlers.ingest_pyrogram_message", new=AsyncMock(return_value=self._ingest_result())):
                asyncio.run(h.handle_main_account(MagicMock(), message))
        h.is_force_plagiscan_author.assert_not_called()
        self.assertFalse(h.manager.file_queue[0]["force_plagiscan"])

    def test_non_editor_applies_force_plagiscan_check(self):
        h = _make_handlers()
        h.is_force_plagiscan_author = MagicMock(return_value=True)
        h.process_queue = AsyncMock()
        h.manager.get_file_lock = AsyncMock(return_value=asyncio.Lock())
        h.manager.get_file_status = AsyncMock(return_value="NEW")
        h.manager.set_file_status = AsyncMock()
        h.manager.reset_waiting_status = MagicMock()
        message = self._message("student_user", "work.docx")
        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None: {
            "ai_bot": "@AAA_Report_AIBot",
            "anti_bot": "@plagaiscan_bot",
        }.get(key, default)):
            with patch("bot_handlers.ingest_pyrogram_message", new=AsyncMock(return_value=self._ingest_result())):
                asyncio.run(h.handle_main_account(MagicMock(), message))
        h.is_force_plagiscan_author.assert_called_once()
        self.assertTrue(h.manager.file_queue[0]["force_plagiscan"])

    def test_allowed_telegram_bot_document_is_accepted_as_author_file(self):
        h = _make_handlers()
        h.process_queue = AsyncMock()
        h.handle_bot_response = AsyncMock()
        h.manager.get_file_lock = AsyncMock(return_value=asyncio.Lock())
        h.manager.get_file_status = AsyncMock(return_value="NEW")
        h.manager.set_file_status = AsyncMock()
        h.manager.reset_waiting_status = MagicMock()
        message = self._message("ANTIPLAGIAD_bot", "accepted.docx")

        def config_get(key, default=None):
            return {
                "allowed_authors": ["ANTIPLAGIAD_bot"],
                "ai_bot": "@AAA_Report_AIBot",
                "anti_bot": "@plagaiscan_bot",
                "mode": "mode1",
            }.get(key, default)

        with patch("bot_handlers.Config.get_setting", side_effect=config_get):
            with patch("bot_handlers.ingest_pyrogram_message", new=AsyncMock(return_value=self._ingest_result())):
                asyncio.run(h.handle_main_account(MagicMock(), message))

        h.handle_bot_response.assert_not_awaited()
        self.assertEqual("ANTIPLAGIAD_bot", h.manager.file_queue[0]["author"])
        self.assertEqual("accepted.docx", h.manager.file_queue[0]["file_name"])

    def test_unlisted_telegram_bot_document_still_goes_to_bot_response(self):
        h = _make_handlers()
        h.process_queue = AsyncMock()
        h.handle_bot_response = AsyncMock()
        message = self._message("ANTIPLAGIAD_bot", "ignored.docx")

        def config_get(key, default=None):
            return {
                "allowed_authors": ["student_user"],
                "ai_bot": "@AAA_Report_AIBot",
                "anti_bot": "@plagaiscan_bot",
                "mode": "mode1",
            }.get(key, default)

        with patch("bot_handlers.Config.get_setting", side_effect=config_get):
            with patch("bot_handlers.ingest_pyrogram_message", new=AsyncMock()) as ingest_mock:
                asyncio.run(h.handle_main_account(MagicMock(), message))

        h.handle_bot_response.assert_awaited_once()
        ingest_mock.assert_not_awaited()
        self.assertEqual([], h.manager.file_queue)


class TestAdditionalDisplayFormatting(unittest.TestCase):
    def test_empty_plagiscan_users_display_shows_not_set(self):
        self.assertEqual("не задан", _display_or_not_set([]))

    def test_empty_allowed_authors_display_shows_not_set(self):
        self.assertEqual("не задан", _display_or_not_set([]))

    def test_clearing_with_zero_stores_empty_list_and_displays_not_set(self):
        parsed = _parse_allowed_authors_inputs("0", "0", current_authors=["vk:gosdohnem", "Xlistyara"])
        self.assertEqual([], parsed)
        self.assertEqual("не задан", _display_or_not_set(parsed))


class TestAdditionalRoutingMatrix(unittest.TestCase):
    BASE_SETTINGS = {
        "max_concurrent": 7,
        "mode": "mode1",
        "normal_destination": "бот",
        "anti_destination": "бот",
        "plagiscan_users": [],
        "editor_24_7": "@editor247",
        "editor_nickname": "@work_editor",
        "normal_editor_nickname": "@normal_editor",
        "anti_editor_nickname": "@anti_editor",
    }

    def _fresh_handlers(self, file_info):
        h = _make_handlers()
        h.manager.file_queue = [dict(file_info)]
        h.process_normal_file = AsyncMock()
        h.process_anti_file = AsyncMock()
        h.send_to_nik2 = AsyncMock()
        h.send_to_24_7_editor = AsyncMock()
        h._increment_mode2_limit_counter = MagicMock()
        return h

    def _run(self, h, settings):
        merged = {**self.BASE_SETTINGS, **settings}
        with patch("bot_handlers.Config.get_setting", side_effect=lambda key, default=None: merged.get(key, default)):
            asyncio.run(h.process_queue())

    def test_mode1_normal_file_all_destinations_and_force_override(self):
        base_file = {
            "file_name": "plain.docx",
            "original_file_name": "plain.docx",
            "is_anti": False,
            "force_plagiscan": False,
            "message": MagicMock(),
        }
        cases = [
            ("бот", False, "normal"),
            ("редактор", False, "editor"),
            ("плагискан", False, "plagiscan"),
            ("бот", True, "plagiscan"),
            ("редактор", True, "plagiscan"),
            ("плагискан", True, "plagiscan"),
        ]
        for normal_destination, force_plagiscan, expected in cases:
            with self.subTest(normal_destination=normal_destination, force_plagiscan=force_plagiscan):
                file_info = dict(base_file, force_plagiscan=force_plagiscan)
                h = self._fresh_handlers(file_info)
                self._run(h, {"mode": "mode1", "normal_destination": normal_destination})

                if expected == "normal":
                    h.process_normal_file.assert_awaited_once()
                    h.process_anti_file.assert_not_awaited()
                    h.send_to_nik2.assert_not_awaited()
                elif expected == "editor":
                    h.send_to_nik2.assert_awaited_once()
                    h.process_normal_file.assert_not_awaited()
                    h.process_anti_file.assert_not_awaited()
                else:
                    h.process_anti_file.assert_awaited_once()
                    routed = h.process_anti_file.await_args.args[0]
                    self.assertTrue(routed["is_anti"])
                    self.assertEqual(force_plagiscan, routed["force_plagiscan"])
                    if normal_destination == "плагискан" and not force_plagiscan:
                        self.assertTrue(routed["via_normal_destination"])
                    h.process_normal_file.assert_not_awaited()
                    h.send_to_nik2.assert_not_awaited()

    def test_mode1_anti_file_ignores_normal_destination_matrix(self):
        for normal_destination in ("бот", "редактор", "плагискан"):
            for anti_destination in ("бот", "редактор"):
                with self.subTest(normal_destination=normal_destination, anti_destination=anti_destination):
                    h = self._fresh_handlers({
                        "file_name": "anti анти.docx",
                        "original_file_name": "anti анти.docx",
                        "is_anti": True,
                        "force_plagiscan": False,
                        "message": MagicMock(),
                    })
                    self._run(h, {
                        "mode": "mode1",
                        "normal_destination": normal_destination,
                        "anti_destination": anti_destination,
                    })
                    h.process_anti_file.assert_awaited_once()
                    h.process_normal_file.assert_not_awaited()
                    h.send_to_nik2.assert_not_awaited()

    def test_mode2_within_work_hours_matrix(self):
        cases = [
            ({"is_anti": False, "force_plagiscan": False}, {"normal_destination": "бот"}, "normal"),
            ({"is_anti": False, "force_plagiscan": False}, {"normal_destination": "редактор"}, "editor"),
            ({"is_anti": False, "force_plagiscan": False}, {"normal_destination": "плагискан"}, "plagiscan"),
            ({"is_anti": True, "force_plagiscan": False}, {"anti_destination": "бот"}, "plagiscan"),
            ({"is_anti": True, "force_plagiscan": False}, {"anti_destination": "редактор"}, "plagiscan"),
            ({"is_anti": False, "force_plagiscan": True}, {"normal_destination": "редактор"}, "plagiscan"),
        ]
        for file_flags, settings, expected in cases:
            with self.subTest(file_flags=file_flags, settings=settings):
                h = self._fresh_handlers({
                    "file_name": "mode2.docx",
                    "original_file_name": "mode2.docx",
                    "message": MagicMock(),
                    **file_flags,
                })
                h.is_within_working_hours = MagicMock(return_value=True)
                h.check_daily_limit = MagicMock(return_value=True)
                h.aaa_globally_unavailable = False
                self._run(h, {"mode": "mode2", **settings})

                if expected == "normal":
                    h.process_normal_file.assert_awaited_once()
                    h.process_anti_file.assert_not_awaited()
                    h.send_to_nik2.assert_not_awaited()
                    h.send_to_24_7_editor.assert_not_awaited()
                elif expected == "editor":
                    h.send_to_nik2.assert_awaited_once()
                    h.process_normal_file.assert_not_awaited()
                    h.process_anti_file.assert_not_awaited()
                    h.send_to_24_7_editor.assert_not_awaited()
                else:
                    h.process_anti_file.assert_awaited_once()
                    h.process_normal_file.assert_not_awaited()
                    h.send_to_nik2.assert_not_awaited()
                    h.send_to_24_7_editor.assert_not_awaited()

    def test_mode2_outside_hours_matrix_non_force_to_247_force_to_plagiscan(self):
        cases = [
            (False, False, "editor247"),
            (True, False, "editor247"),
            (False, True, "plagiscan"),
            (True, True, "plagiscan"),
        ]
        for is_anti, force_plagiscan, expected in cases:
            with self.subTest(is_anti=is_anti, force_plagiscan=force_plagiscan):
                h = self._fresh_handlers({
                    "file_name": "outside анти.docx" if is_anti else "outside.docx",
                    "original_file_name": "outside анти.docx" if is_anti else "outside.docx",
                    "is_anti": is_anti,
                    "force_plagiscan": force_plagiscan,
                    "message": MagicMock(),
                })
                h.is_within_working_hours = MagicMock(return_value=False)
                h.check_daily_limit = MagicMock(return_value=False)
                h.aaa_globally_unavailable = True
                self._run(h, {"mode": "mode2", "normal_destination": "редактор", "anti_destination": "редактор"})

                if expected == "editor247":
                    h.send_to_24_7_editor.assert_awaited_once()
                    h.process_anti_file.assert_not_awaited()
                    h.send_to_nik2.assert_not_awaited()
                else:
                    h.process_anti_file.assert_awaited_once()
                    h.send_to_24_7_editor.assert_not_awaited()
                    h.send_to_nik2.assert_not_awaited()


class TestAdditionalCounterStandaloneConfig(unittest.TestCase):
    def test_counter_client_starts_without_listening_for_updates(self):
        processor = CounterModeProcessor(_make_account_manager())
        created_clients = []

        class FakeClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                created_clients.append(self)

            async def start(self):
                return None

        with tempfile.TemporaryDirectory() as tmp_dir:
            live_session = Path(tmp_dir) / "НИК-2.session"
            live_session.write_bytes(b"session-data")

            with patch("pyrogram.Client", FakeClient), \
                 patch("bot_handlers.Config.get_account", return_value={"api_id": 123, "api_hash": "hash"}), \
                 patch("bot_handlers.Config.get_session_name", return_value=str(live_session)):
                client, temp_session = asyncio.run(processor._start_counter_client("НИК-2"))

            try:
                self.assertIs(client, created_clients[0])
                self.assertTrue(created_clients[0].kwargs["no_updates"])
            finally:
                if temp_session:
                    temp_session.cleanup()

    def test_counter_session_copy_uses_temp_file_instead_of_live_session(self):
        processor = CounterModeProcessor(_make_account_manager())
        with tempfile.TemporaryDirectory() as tmp_dir:
            live_session = Path(tmp_dir) / "НИК-2.session"
            live_session.write_bytes(b"session-data")

            with patch("bot_handlers.Config.get_session_name", return_value=str(live_session)):
                temp_session, session_name = processor._copy_session_for_counter("НИК-2")

            try:
                copied_session = Path(temp_session.name) / f"{session_name}.session"
                self.assertTrue(copied_session.exists())
                self.assertEqual(b"session-data", copied_session.read_bytes())
                self.assertNotEqual(live_session.parent, copied_session.parent)
                self.assertTrue(session_name.startswith("counter_"))
            finally:
                temp_session.cleanup()

    def test_counter_py_loads_main_tokens_and_saves_only_counter_settings(self):
        import json
        import counter
        from config import Config

        old_accounts = Config._ACCOUNTS.copy()
        old_settings = Config._SETTINGS.copy()

        with tempfile.TemporaryDirectory() as tmp_dir:
            main_config = Path(tmp_dir) / "config.json"
            counter_config = Path(tmp_dir) / "counter_config.json"
            main_config.write_text(
                json.dumps(
                    {
                        "accounts": {
                            "НИК-1": {"api_id": "123", "api_hash": "hash1", "phone": "+100"},
                            "НИК-2": {"api_id": "456", "api_hash": "hash2", "phone": "+200"},
                        },
                        "settings": {
                            "mode": "mode2",
                            "vk_bot_token": "main-vk-token",
                            "vk_counter_token": "main-counter-token",
                            "normal_destination": "редактор",
                            "counter_author": "vk:old",
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            counter_config.write_text(
                json.dumps(
                    {
                        "settings": {
                            "mode": "mode3",
                            "counter_type": "editor",
                            "counter_editor_nick": "@editor",
                            "counter_author": "vk:counter",
                            "counter_vk_scope": "chat",
                            "counter_vk_peer_id": "2000000002",
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            try:
                with patch.object(counter, "MAIN_CONFIG_FILE", main_config), \
                     patch.object(counter, "COUNTER_CONFIG_FILE", counter_config):
                    counter._load_counter_config()
                    self.assertEqual("mode3", Config.get_setting("mode"))
                    self.assertEqual("main-vk-token", Config.get_setting("vk_bot_token"))
                    self.assertEqual("main-counter-token", Config.get_setting("vk_counter_token"))
                    self.assertEqual("editor", Config.get_setting("counter_type"))
                    self.assertEqual("@editor", Config.get_setting("counter_editor_nick"))
                    self.assertEqual("2000000002", Config.get_setting("counter_vk_peer_id"))
                    self.assertEqual(123, Config.get_account("НИК-1")["api_id"])

                    Config.update_setting("counter_author", "vk:saved")
                    Config.update_setting("normal_destination", "бот")
                    Config.update_setting("vk_bot_token", "should-not-be-saved-here")
                    counter._save_counter_config()

                saved = json.loads(counter_config.read_text(encoding="utf-8"))
                self.assertEqual("mode3", saved["settings"]["mode"])
                self.assertEqual("vk:saved", saved["settings"]["counter_author"])
                self.assertNotIn("normal_destination", saved["settings"])
                self.assertNotIn("vk_bot_token", saved["settings"])
                self.assertNotIn("vk_counter_token", saved["settings"])
            finally:
                Config._ACCOUNTS = old_accounts
                Config._SETTINGS = old_settings

    def test_counter_py_returns_to_counter_menu_after_one_report(self):
        import counter

        async_run_counter = AsyncMock()
        with patch.object(counter.ConsoleMenu, "setup_before_launch", side_effect=[True, False]) as setup_mock, \
             patch.object(counter, "_run_counter_mode", async_run_counter):
            asyncio.run(counter.main())

        async_run_counter.assert_awaited_once()
        self.assertEqual(2, setup_mock.call_count)
        _, first_kwargs = setup_mock.call_args_list[0]
        self.assertEqual("mode3", first_kwargs["force_mode"])
        self.assertEqual("Нажмите Enter для запуска счетчика...", first_kwargs["launch_prompt"])
        self.assertTrue(first_kwargs["allow_exit"])

    def test_vk_counter_tokens_are_read_from_environment_each_call(self):
        processor = CounterModeProcessor(_make_account_manager())
        with patch.dict(os.environ, {"VK_BOT_TOKEN": "env-bot-1", "VK_COUNTER_TOKEN": "env-counter-1"}):
            self.assertEqual(("env-bot-1", "VK Bot Token"), processor._get_vk_api_token(token_kind="bot"))
            self.assertEqual(("env-counter-1", "VK Counter/User Token"), processor._get_vk_api_token(token_kind="counter"))
            os.environ["VK_BOT_TOKEN"] = "env-bot-2"
            os.environ["VK_COUNTER_TOKEN"] = "env-counter-2"
            self.assertEqual(("env-bot-2", "VK Bot Token"), processor._get_vk_api_token(token_kind="bot"))
            self.assertEqual(("env-counter-2", "VK Counter/User Token"), processor._get_vk_api_token(token_kind="counter"))


class TestAdditionalConsoleInputNormalization(unittest.TestCase):
    def test_vk_author_link_and_id_are_normalized_for_counter(self):
        from main import ConsoleMenu

        self.assertEqual("1106569752", ConsoleMenu.normalize_vk_author("https://vk.com/id1106569752?from=search"))
        self.assertEqual("-224047547", ConsoleMenu.normalize_vk_author("vk:-224047547"))
        self.assertEqual("gosdohnem", ConsoleMenu.normalize_vk_author("@gosdohnem"))

    def test_vk_peer_links_are_normalized_for_non_technical_users(self):
        from main import ConsoleMenu

        self.assertEqual("2000000002", ConsoleMenu.normalize_vk_peer_id("https://vk.com/im/convo/2000000002?entrypoint=list_all"))
        self.assertEqual("2000000007", ConsoleMenu.normalize_vk_peer_id("https://vk.com/im?sel=c7"))
        self.assertEqual("1065504879", ConsoleMenu.normalize_vk_peer_id("https://vk.com/im?sel=1065504879"))

    def test_counter_remembers_vk_author_and_peer_after_tg_author_was_current(self):
        from config import Config
        from main import ConsoleMenu

        old_settings = Config._SETTINGS
        Config._SETTINGS = Config.DEFAULT_SETTINGS.copy()
        Config._SETTINGS.update({
            "mode": "mode3",
            "counter_type": "author",
            "counter_author": "Xlistyara",
            "counter_tg_author": "Xlistyara",
            "counter_vk_author": "vk:1106569752",
            "counter_vk_peer_id": "2000000001",
            "counter_vk_scope": "chat",
        })
        inputs = iter([
            "1",  # анализ по автору
            "1",  # ВКонтакте, хотя текущим был TG
            "",   # оставить сохраненного VK автора
            "",   # оставить место отправки: беседа ВК
            "",   # оставить peer_id
            "", "", "", "", "", "", "",  # даты/файлы/время/запуск
        ])

        try:
            with patch.object(Config, "load_config", return_value=None), \
                 patch.object(Config, "save_config", return_value=None), \
                 patch("builtins.input", side_effect=lambda _="": next(inputs)):
                self.assertTrue(ConsoleMenu.setup_before_launch(force_mode="mode3", allow_exit=True))

            self.assertEqual("vk:1106569752", Config.get_setting("counter_author"))
            self.assertEqual("vk:1106569752", Config.get_setting("counter_vk_author"))
            self.assertEqual("Xlistyara", Config.get_setting("counter_tg_author"))
            self.assertEqual("2000000001", Config.get_setting("counter_vk_peer_id"))
            self.assertEqual("chat", Config.get_setting("counter_vk_scope"))
        finally:
            Config._SETTINGS = old_settings
