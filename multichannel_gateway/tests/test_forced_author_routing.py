from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

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
from multichannel_gateway.core.storage import SqliteJobStore


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
    handlers = BotHandlers(manager or make_manager())
    handlers._safety_test_dir = tempfile.TemporaryDirectory()
    handlers._editor_safety_store = SqliteJobStore(Path(handlers._safety_test_dir.name) / "jobs.sqlite3")
    return handlers


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
        "plagiscan_user_routes": {},
        "forced_authors_destination": "plagiscan",
        "forced_authors_editor_nickname": None,
        "telegram_group_routes": {},
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
    def test_active_defaults_contain_only_first_two_accounts(self):
        self.assertEqual(["НИК-1", "НИК-2"], list(Config.DEFAULT_ACCOUNTS))
        self.assertEqual("плагискан", Config.DEFAULT_SETTINGS["normal_destination"])

    def test_legacy_accounts_are_loaded_but_not_kept_active_or_saved(self):
        old_accounts = Config._ACCOUNTS
        old_settings = Config._SETTINGS
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "config.json"
                config_path.write_text(
                    json.dumps(
                        {
                            "accounts": {
                                nickname: {"api_id": None, "api_hash": None, "phone": None}
                                for nickname in (
                                    "НИК-1",
                                    "НИК-2",
                                    "НИК-3",
                                    "НИК-4",
                                    "НИК-5",
                                    "НИК-6",
                                    "НИК-7",
                                )
                            },
                            "settings": {"plagiscan_users": ["@legacy"]},
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                with patch.object(Config, "CONFIG_FILE", config_path):
                    Config.load_config()
                    self.assertEqual({"НИК-1", "НИК-2"}, set(Config.get_all_accounts()))
                    Config.save_config()
                    saved = json.loads(config_path.read_text(encoding="utf-8"))
                    self.assertEqual({"НИК-1", "НИК-2"}, set(saved["accounts"]))
        finally:
            Config._ACCOUNTS = old_accounts
            Config._SETTINGS = old_settings

    def test_legacy_aaa_normal_destination_migrates_to_existing_editor(self):
        old_accounts = Config._ACCOUNTS
        old_settings = Config._SETTINGS
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "config.json"
                config_path.write_text(
                    json.dumps(
                        {
                            "accounts": {},
                            "settings": {
                                "normal_destination": "бот",
                                "normal_editor_nickname": "legacy-editor",
                                "normal_accounts": ["НИК-3"],
                            },
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                with patch.object(Config, "CONFIG_FILE", config_path):
                    Config.load_config()
                    Config.save_config()

                self.assertEqual("редактор", Config.get_setting("normal_destination"))
                self.assertEqual("@legacy-editor", Config.get_setting("normal_editor_nickname"))
                self.assertEqual([], Config.get_setting("normal_accounts"))
                saved = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertEqual("редактор", saved["settings"]["normal_destination"])
                self.assertEqual([], saved["settings"]["normal_accounts"])
        finally:
            Config._ACCOUNTS = old_accounts
            Config._SETTINGS = old_settings

    def test_legacy_aaa_normal_destination_migrates_to_plagiscan_without_editor(self):
        old_accounts = Config._ACCOUNTS
        old_settings = Config._SETTINGS
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "config.json"
                config_path.write_text(
                    json.dumps(
                        {
                            "accounts": {},
                            "settings": {"normal_destination": "бот"},
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                with patch.object(Config, "CONFIG_FILE", config_path):
                    Config.load_config()
                    Config.save_config()

                self.assertEqual("плагискан", Config.get_setting("normal_destination"))
                saved = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertEqual("плагискан", saved["settings"]["normal_destination"])
        finally:
            Config._ACCOUNTS = old_accounts
            Config._SETTINGS = old_settings

    def test_common_settings_menu_does_not_offer_aaa_route(self):
        old_settings = Config._SETTINGS
        try:
            Config._SETTINGS = {
                **Config.DEFAULT_SETTINGS,
                "normal_destination": "плагискан",
                "anti_destination": "бот",
                "max_concurrent": 7,
            }
            with patch.object(Config, "load_config"), patch.object(Config, "save_config"), patch.object(
                Config, "show_current_settings"
            ), patch.object(Config, "setup_forced_authors_interactive"), patch(
                "builtins.input", side_effect=["", "", "", ""]
            ), patch("builtins.print") as print_mock:
                Config.setup_editors_interactive()

            output = " ".join(str(item) for item in print_mock.call_args_list)
            self.assertNotIn("AAA", output)
            self.assertNotIn("AI бота", output)
            self.assertIn("Напрямую редактору", output)
            self.assertIn("В бота @plagaiscan_bot", output)
        finally:
            Config._SETTINGS = old_settings

    def test_launch_menu_does_not_offer_aaa_route(self):
        from main import ConsoleMenu

        old_settings = Config._SETTINGS
        try:
            Config._SETTINGS = {
                **Config.DEFAULT_SETTINGS,
                "mode": "mode2",
                "normal_destination": "плагискан",
                "anti_destination": "бот",
                "editor_nickname": "@editor",
                "editor_24_7": "@editor-247",
            }
            with patch.object(Config, "load_config"), patch.object(Config, "save_config"), patch.object(
                ConsoleMenu, "setup_editor_24_7"
            ), patch(
                "builtins.input", side_effect=["", "", "", "", "", "", "", "", ""]
            ), patch("builtins.print") as print_mock:
                self.assertTrue(ConsoleMenu.setup_before_launch(force_mode="mode2", launch_prompt="continue"))

            output = " ".join(str(item) for item in print_mock.call_args_list)
            self.assertNotIn("AAA", output)
            self.assertIn("Редактор", output)
            self.assertIn("Плагискан", output)
        finally:
            Config._SETTINGS = old_settings

    def test_account_menu_prompts_only_for_the_two_active_accounts(self):
        old_accounts = Config._ACCOUNTS
        old_settings = Config._SETTINGS
        try:
            Config._ACCOUNTS = {
                "НИК-1": {"api_id": 1, "api_hash": "hash1", "phone": "+1001"},
                "НИК-2": {"api_id": 2, "api_hash": "hash2", "phone": "+1002"},
                "legacy-account": {"api_id": 3, "api_hash": "hash3", "phone": "+1003"},
            }
            Config._SETTINGS = {}
            with patch.object(Config, "load_config"), patch.object(Config, "save_config"), patch(
                "builtins.input", side_effect=["n", "n"]
            ) as input_mock, patch("builtins.print") as print_mock:
                Config.setup_accounts_interactive()

            output = " ".join(str(item) for item in print_mock.call_args_list)
            self.assertIn("НИК-1", output)
            self.assertIn("НИК-2", output)
            self.assertNotIn("legacy-account", output)
            self.assertEqual(2, input_mock.call_count)
        finally:
            Config._ACCOUNTS = old_accounts
            Config._SETTINGS = old_settings

    def test_current_settings_show_only_active_accounts_and_human_forced_route(self):
        old_accounts = Config._ACCOUNTS
        old_settings = Config._SETTINGS
        try:
            Config._ACCOUNTS = {
                "НИК-1": {"api_id": None, "api_hash": None, "phone": None},
                "НИК-2": {"api_id": None, "api_hash": None, "phone": None},
                "legacy-account": {"api_id": 3, "api_hash": "hash3", "phone": "+1003"},
            }
            Config._SETTINGS = {
                "plagiscan_users": ["@ivan"],
                "forced_authors_destination": "editor",
                "forced_authors_editor_nickname": "@editor",
                "telegram_group_routes": {
                    "-100123": {"title": "Компания", "destination": "plagiscan"}
                },
            }
            with patch.object(Config, "load_config"), patch("builtins.print") as print_mock:
                Config.show_current_settings()

            output = " ".join(str(item) for item in print_mock.call_args_list)
            self.assertIn("Принудительные авторы", output)
            self.assertIn("редактор @editor", output)
            self.assertIn("Компания → Plagiscan", output)
            self.assertNotIn("AAA", output)
            self.assertNotIn("legacy-account", output)
            for stale_name in ("НИК-3", "НИК-4", "НИК-5", "НИК-6", "НИК-7"):
                self.assertNotIn(stale_name, output)
        finally:
            Config._ACCOUNTS = old_accounts
            Config._SETTINGS = old_settings

    def test_shared_forced_route_is_used_for_every_forced_author(self):
        handlers = make_handlers()
        settings = base_settings(
            ["@ivan", "@masha", "vk:700"],
            forced_authors_destination="editor",
            forced_authors_editor_nickname="editor.one",
        )

        with patch("bot_handlers.Config.get_setting", side_effect=settings_lookup(settings)):
            for file_info in (
                {"author": "ivan", "author_id": 1},
                {"author": "masha", "author_id": 2},
                {"author": "VK Chat", "route_sender_id": "700", "route_sender_name": "vk-name"},
            ):
                self.assertEqual(
                    {"destination": "editor", "editor_nickname": "@editor.one"},
                    handlers.get_force_author_route(file_info),
                )

    def test_telegram_group_route_has_priority_over_shared_author_route(self):
        handlers = make_handlers()
        settings = base_settings(
            ["author"],
            forced_authors_destination="editor",
            forced_authors_editor_nickname="@author-editor",
            telegram_group_routes={
                "-100123": {
                    "title": "Компания",
                    "destination": "editor",
                    "editor_nickname": "@group-editor",
                }
            },
        )

        with patch("bot_handlers.Config.get_setting", side_effect=settings_lookup(settings)):
            self.assertEqual(
                {"destination": "editor", "editor_nickname": "@group-editor", "scope": "telegram_group"},
                handlers.get_force_author_route(
                    {
                        "author": "ordinary",
                        "author_id": 99,
                        "source_platform": "telegram",
                        "route_chat_id": -100123,
                        "route_is_group": True,
                    }
                ),
            )
            self.assertEqual(
                {"destination": "editor", "editor_nickname": "@author-editor"},
                handlers.get_force_author_route({"author": "author", "author_id": 42}),
            )

    def test_explicit_shared_plagiscan_route_applies_to_every_forced_author(self):
        handlers = make_handlers()
        settings = base_settings(
            ["@ivan", "@masha", "vk:700"],
            forced_authors_destination="plagiscan",
            forced_authors_editor_nickname=None,
        )

        with patch("bot_handlers.Config.get_setting", side_effect=settings_lookup(settings)):
            self.assertEqual(
                {"destination": "plagiscan"},
                handlers.get_force_author_route({"author": "ivan", "author_id": 1}),
            )
            self.assertTrue(
                handlers.is_force_plagiscan_file_info(
                    {"author": "VK", "route_sender_id": "700", "source_platform": "vk"}
                )
            )

    def test_legacy_conflicting_per_author_routes_migrate_to_safe_shared_plagiscan(self):
        old_accounts = Config._ACCOUNTS
        old_settings = Config._SETTINGS
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "config.json"
                config_path.write_text(
                    json.dumps(
                        {
                            "accounts": {},
                            "settings": {
                                "plagiscan_users": ["@ivan", "@masha"],
                                "plagiscan_user_routes": {
                                    "@ivan": {"destination": "editor", "editor_nickname": "@one"},
                                    "@masha": {"destination": "editor", "editor_nickname": "@two"},
                                },
                            },
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                with patch.object(Config, "CONFIG_FILE", config_path):
                    Config.load_config()

            self.assertEqual("plagiscan", Config.get_setting("forced_authors_destination"))
            self.assertIsNone(Config.get_setting("forced_authors_editor_nickname"))
            self.assertEqual({}, Config.get_setting("plagiscan_user_routes"))
        finally:
            Config._ACCOUNTS = old_accounts
            Config._SETTINGS = old_settings

    def test_legacy_same_editor_routes_migrate_to_one_shared_editor(self):
        old_accounts = Config._ACCOUNTS
        old_settings = Config._SETTINGS
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "config.json"
                config_path.write_text(
                    json.dumps(
                        {
                            "accounts": {},
                            "settings": {
                                "plagiscan_users": ["@ivan", "@masha"],
                                "plagiscan_user_routes": {
                                    "@ivan": {"destination": "editor", "editor_nickname": "@one"},
                                    "@masha": {"destination": "editor", "editor_nickname": "one"},
                                },
                            },
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                with patch.object(Config, "CONFIG_FILE", config_path):
                    Config.load_config()

            self.assertEqual("editor", Config.get_setting("forced_authors_destination"))
            self.assertEqual("@one", Config.get_setting("forced_authors_editor_nickname"))
            self.assertEqual({}, Config.get_setting("plagiscan_user_routes"))
        finally:
            Config._ACCOUNTS = old_accounts
            Config._SETTINGS = old_settings

    def test_legacy_partial_routes_migrate_to_safe_shared_plagiscan(self):
        old_accounts = Config._ACCOUNTS
        old_settings = Config._SETTINGS
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "config.json"
                config_path.write_text(
                    json.dumps(
                        {
                            "accounts": {},
                            "settings": {
                                "plagiscan_users": ["@ivan", "@masha"],
                                "plagiscan_user_routes": {
                                    "@ivan": {"destination": "editor", "editor_nickname": "@one"},
                                },
                            },
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                with patch.object(Config, "CONFIG_FILE", config_path):
                    Config.load_config()

            self.assertEqual("plagiscan", Config.get_setting("forced_authors_destination"))
            self.assertIsNone(Config.get_setting("forced_authors_editor_nickname"))
        finally:
            Config._ACCOUNTS = old_accounts
            Config._SETTINGS = old_settings

    def test_vk_external_job_uses_shared_editor_route_through_legacy_import_path(self):
        from main import FileDistributionBot

        manager = make_manager()
        editor = MagicMock()
        editor.send_document = AsyncMock(
            return_value=SimpleNamespace(id=901, chat=SimpleNamespace(id=300))
        )
        manager.get_client.side_effect = lambda name: editor if name == "НИК-2" else None
        handlers = make_handlers(manager)
        handlers.process_anti_file = AsyncMock()
        handlers.send_to_24_7_editor = AsyncMock()
        handlers._mark_gateway_job_waiting_editor = AsyncMock()
        bot = FileDistributionBot.__new__(FileDistributionBot)
        bot.manager = manager
        bot.handlers = handlers
        bot.gateway_store = MagicMock()

        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "work.docx"
            source_path.write_bytes(b"source")
            job = SimpleNamespace(
                job_id=77,
                source="vk",
                dedupe_key="vk:700:800:0",
                chat_id="2000000001",
                sender_id="700",
                sender_name="vk-author",
                message_id="800",
                original_file_name="work.docx",
                file_path=str(source_path),
            )
            settings = base_settings(
                ["vk:700"],
                forced_authors_destination="editor",
                forced_authors_editor_nickname="@shared-editor",
            )
            with patch("main.claim_external_job_for_legacy", new=AsyncMock(return_value=job)), patch(
                "bot_handlers.Config.get_setting", side_effect=settings_lookup(settings)
            ):
                asyncio.run(bot.import_external_jobs(limit=1))
                self.assertEqual(1, len(manager.file_queue))
                self.assertEqual(
                    {"destination": "editor", "editor_nickname": "@shared-editor"},
                    manager.file_queue[0]["forced_route"],
                )
                asyncio.run(handlers.process_queue())

        self.assertEqual(1, editor.send_document.await_count)
        self.assertEqual("@shared-editor", editor.send_document.await_args.kwargs["chat_id"])
        self.assertEqual("work.docx", editor.send_document.await_args.kwargs["file_name"])
        handlers.process_anti_file.assert_not_awaited()
        handlers.send_to_24_7_editor.assert_not_awaited()

    def test_configured_group_member_bypasses_author_allowlist_and_keeps_origin_context(self):
        manager = make_manager()
        handlers = make_handlers(manager)
        handlers.process_queue = AsyncMock()
        handlers.editor_tracking["existing-editor-task"] = {"destination": "@ordinary"}
        message = SimpleNamespace(
            id=777,
            chat=SimpleNamespace(id=-100123, type="supergroup"),
            from_user=SimpleNamespace(id=42, username="ordinary"),
            document=SimpleNamespace(file_name="paper.docx"),
            text=None,
            reply_to_message_id=None,
        )
        ingest_job = SimpleNamespace(
            job_id=77,
            file_path="/tmp/paper.docx",
            dedupe_key="telegram:-100123:777:0",
        )
        settings = base_settings(
            [],
            allowed_authors=["someone_else"],
            telegram_group_routes={
                "-100123": {
                    "title": "Компания",
                    "destination": "editor",
                    "editor_nickname": "@group-editor",
                }
            },
        )

        with patch("bot_handlers.Config.get_setting", side_effect=settings_lookup(settings)), patch(
            "bot_handlers.ingest_pyrogram_message", new=AsyncMock(return_value=[ingest_job])
        ):
            asyncio.run(handlers.handle_main_account(MagicMock(), message))

        queued = manager.file_queue[0]
        self.assertEqual("ordinary", queued["author"])
        self.assertEqual(-100123, queued["route_chat_id"])
        self.assertEqual(777, queued["route_message_id"])
        self.assertTrue(queued["route_is_group"])
        self.assertEqual(
            {"destination": "editor", "editor_nickname": "@group-editor", "scope": "telegram_group"},
            queued["forced_route"],
        )
        self.assertTrue(queued["fixed_author_editor_route"])
        self.assertFalse(queued["force_plagiscan"])
        self.assertEqual(1, handlers.files_today["count"])

    def test_configured_group_plagiscan_route_ignores_anti_marker_but_keeps_filter_path(self):
        manager = make_manager()
        handlers = make_handlers(manager)
        handlers.process_anti_file = AsyncMock()
        task = telegram_file_info("ordinary", 42, "ordinary.docx", chat_id=-100123, message_id=777)
        task["is_anti"] = False
        task["force_plagiscan"] = False
        manager.file_queue = [task]
        settings = base_settings(
            [],
            telegram_group_routes={
                "-100123": {"title": "Компания", "destination": "plagiscan"}
            },
        )

        with patch("bot_handlers.Config.get_setting", side_effect=settings_lookup(settings)):
            asyncio.run(handlers.process_queue())

        handlers.process_anti_file.assert_awaited_once_with(task)
        self.assertTrue(task["force_plagiscan"])
        self.assertTrue(task["is_anti"])

    def test_dialog_discovery_returns_only_unique_groups_and_supergroups(self):
        async def dialog_stream():
            for chat in (
                SimpleNamespace(id=-1001, title="Группа", type="group"),
                SimpleNamespace(id=-1002, title="Супергруппа", type="supergroup"),
                SimpleNamespace(id=55, title="Личный чат", type="private"),
                SimpleNamespace(id=-1003, title="Канал", type="channel"),
                SimpleNamespace(id=-1001, title="Дубликат", type="group"),
            ):
                yield SimpleNamespace(chat=chat)

        client = SimpleNamespace(get_dialogs=lambda: dialog_stream())
        dialogs = asyncio.run(Config.get_telegram_group_dialogs(client))

        self.assertEqual(
            [
                {"chat_id": "-1001", "title": "Группа", "chat_type": "group"},
                {"chat_id": "-1002", "title": "Супергруппа", "chat_type": "supergroup"},
            ],
            dialogs,
        )

    def test_forced_author_menu_asks_for_one_shared_route_and_enter_preserves_it(self):
        old_settings = Config._SETTINGS
        try:
            Config._SETTINGS = {
                "plagiscan_users": ["vk:700", "@ivan", "@masha"],
                "forced_authors_destination": "editor",
                "forced_authors_editor_nickname": "@old-editor",
            }
            with patch("builtins.input", side_effect=["", "", ""]), patch.object(Config, "save_config"):
                Config.setup_forced_authors_interactive()

            self.assertEqual(["vk:700", "@ivan", "@masha"], Config.get_setting("plagiscan_users"))
            self.assertEqual("editor", Config.get_setting("forced_authors_destination"))
            self.assertEqual("@old-editor", Config.get_setting("forced_authors_editor_nickname"))
        finally:
            Config._SETTINGS = old_settings

    def test_group_menu_adds_by_title_without_manual_chat_id(self):
        old_settings = Config._SETTINGS
        try:
            Config._SETTINGS = {"telegram_group_routes": {}}
            dialogs = [
                {"chat_id": "-100123", "title": "Компания Бета", "chat_type": "group"},
                {"chat_id": "-100124", "title": "Компания Альфа", "chat_type": "supergroup"},
                {"chat_id": "55", "title": "Личный чат", "chat_type": "private"},
            ]
            with patch.object(Config, "get_telegram_group_dialogs", new=AsyncMock(return_value=dialogs)), patch.object(
                Config, "save_config"
            ) as save_config, patch(
                "builtins.input", side_effect=["1", "2", "2", "group.editor", "4"]
            ):
                asyncio.run(Config.setup_telegram_group_routes_interactive())

            self.assertEqual(
                {
                    "-100124": {
                        "title": "Компания Альфа",
                        "destination": "editor",
                        "editor_nickname": "@group.editor",
                    }
                },
                Config.get_setting("telegram_group_routes"),
            )
            save_config.assert_called_once()
        finally:
            Config._SETTINGS = old_settings

    def test_group_menu_saves_addition_before_next_menu_prompt(self):
        old_settings = Config._SETTINGS
        try:
            Config._SETTINGS = {"telegram_group_routes": {}}
            dialogs = [{"chat_id": "-100123", "title": "Компания Бета", "chat_type": "group"}]
            save_calls = []
            with patch.object(Config, "get_telegram_group_dialogs", new=AsyncMock(return_value=dialogs)), patch(
                "builtins.input", side_effect=["1", "1", "1", "4"]
            ) as input_mock, patch.object(
                Config,
                "save_config",
                side_effect=lambda: (save_calls.append(input_mock.call_count), self.assertEqual(
                    "plagiscan", Config.get_setting("telegram_group_routes")["-100123"]["destination"]
                )),
            ) as save_config:
                asyncio.run(Config.setup_telegram_group_routes_interactive())

            self.assertEqual([3], save_calls)
            save_config.assert_called_once()
        finally:
            Config._SETTINGS = old_settings

    def test_group_menu_enter_during_add_does_not_change_or_save(self):
        old_settings = Config._SETTINGS
        try:
            Config._SETTINGS = {"telegram_group_routes": {}}
            dialogs = [{"chat_id": "-100123", "title": "Компания Бета", "chat_type": "group"}]
            with patch.object(Config, "get_telegram_group_dialogs", new=AsyncMock(return_value=dialogs)), patch.object(
                Config, "save_config"
            ) as save_config, patch("builtins.input", side_effect=["1", "1", "", "4"]):
                asyncio.run(Config.setup_telegram_group_routes_interactive())

            self.assertEqual({}, Config.get_setting("telegram_group_routes"))
            save_config.assert_not_called()
        finally:
            Config._SETTINGS = old_settings

    def test_group_menu_addition_survives_config_reload(self):
        old_accounts = Config._ACCOUNTS
        old_settings = Config._SETTINGS
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "config.json"
                Config._SETTINGS = {"telegram_group_routes": {}}
                dialogs = [{"chat_id": "-100123", "title": "Компания Бета", "chat_type": "group"}]
                with patch.object(Config, "CONFIG_FILE", config_path), patch.object(
                    Config, "get_telegram_group_dialogs", new=AsyncMock(return_value=dialogs)
                ), patch("builtins.input", side_effect=["1", "1", "1", "4"]):
                    asyncio.run(Config.setup_telegram_group_routes_interactive())
                    Config._SETTINGS = {}
                    Config.load_config()

                self.assertEqual(
                    {"title": "Компания Бета", "destination": "plagiscan"},
                    Config.get_setting("telegram_group_routes")["-100123"],
                )
        finally:
            Config._ACCOUNTS = old_accounts
            Config._SETTINGS = old_settings

    def test_group_menu_does_not_duplicate_an_existing_chat(self):
        old_settings = Config._SETTINGS
        try:
            Config._SETTINGS = {
                "telegram_group_routes": {
                    "-100123": {
                        "title": "Компания",
                        "destination": "editor",
                        "editor_nickname": "@old-editor",
                    }
                }
            }
            dialogs = [{"chat_id": "-100123", "title": "Компания", "chat_type": "group"}]
            with patch.object(Config, "get_telegram_group_dialogs", new=AsyncMock(return_value=dialogs)), patch.object(
                Config, "save_config"
            ) as save_config, patch(
                "builtins.input", side_effect=["1", "1", "2", "", "4"]
            ):
                asyncio.run(Config.setup_telegram_group_routes_interactive())

            self.assertEqual(1, len(Config.get_setting("telegram_group_routes")))
            self.assertEqual("@old-editor", Config.get_setting("telegram_group_routes")["-100123"]["editor_nickname"])
            save_config.assert_called_once()
        finally:
            Config._SETTINGS = old_settings

    def test_group_menu_change_lists_saved_routes_and_uses_selected_number(self):
        old_settings = Config._SETTINGS
        try:
            Config._SETTINGS = {
                "telegram_group_routes": {
                    "-100123": {
                        "title": "Первая компания",
                        "destination": "editor",
                        "editor_nickname": "@first-editor",
                    },
                    "-100124": {
                        "title": "Вторая компания",
                        "destination": "editor",
                        "editor_nickname": "@second-editor",
                    },
                }
            }
            dialogs = [
                {"chat_id": "-100123", "title": "Первая компания", "chat_type": "group"},
                {"chat_id": "-100124", "title": "Вторая компания", "chat_type": "supergroup"},
            ]
            with patch.object(Config, "get_telegram_group_dialogs", new=AsyncMock(return_value=dialogs)), patch.object(
                Config, "save_config"
            ) as save_config, patch(
                "builtins.input", side_effect=["2", "2", "1", "4"]
            ), patch("builtins.print") as print_mock:
                asyncio.run(Config.setup_telegram_group_routes_interactive())

            output = " ".join(str(call_item) for call_item in print_mock.call_args_list)
            self.assertIn("Выберите беседу для изменения:", output)
            self.assertIn("1. Первая компания → редактор @first-editor", output)
            self.assertIn("2. Вторая компания → редактор @second-editor", output)
            self.assertEqual("editor", Config.get_setting("telegram_group_routes")["-100123"]["destination"])
            self.assertEqual("plagiscan", Config.get_setting("telegram_group_routes")["-100124"]["destination"])
            save_config.assert_called_once()
        finally:
            Config._SETTINGS = old_settings

    def test_group_menu_searches_case_insensitively_after_ten_dialogs(self):
        old_settings = Config._SETTINGS
        try:
            Config._SETTINGS = {"telegram_group_routes": {}}
            dialogs = [
                {"chat_id": str(index), "title": f"Команда {index}", "chat_type": "group"}
                for index in range(11)
            ] + [
                {"chat_id": "99", "title": "Ромашка сотрудники", "chat_type": "supergroup"}
            ]
            with patch.object(Config, "get_telegram_group_dialogs", new=AsyncMock(return_value=dialogs)), patch.object(
                Config, "save_config"
            ), patch(
                "builtins.input", side_effect=["1", "11", "РОМАШ", "1", "1", "4"]
            ) as input_mock, patch("builtins.print") as print_mock:
                asyncio.run(Config.setup_telegram_group_routes_interactive())

            self.assertEqual("99", next(iter(Config.get_setting("telegram_group_routes"))))
            prompt_text = " ".join(str(call_item) for call_item in print_mock.call_args_list)
            self.assertIn("Ромашка сотрудники", prompt_text)
            self.assertNotIn("Введите chat_id", prompt_text)
            self.assertEqual(6, input_mock.call_count)
        finally:
            Config._SETTINGS = old_settings

    def test_group_menu_enter_does_not_change_saved_route_or_delete_it(self):
        old_settings = Config._SETTINGS
        try:
            saved_route = {
                "-100123": {
                    "title": "Компания Альфа",
                    "destination": "editor",
                    "editor_nickname": "@old-editor",
                }
            }
            Config._SETTINGS = {"telegram_group_routes": saved_route}
            with patch.object(
                Config,
                "get_telegram_group_dialogs",
                new=AsyncMock(return_value=[{"chat_id": "-100123", "title": "Компания Альфа", "chat_type": "group"}]),
            ), patch.object(Config, "save_config") as save_config, patch(
                "builtins.input", side_effect=["2", "", "3", "", "4"]
            ), patch("builtins.print") as print_mock:
                asyncio.run(Config.setup_telegram_group_routes_interactive())

            self.assertEqual(saved_route, Config.get_setting("telegram_group_routes"))
            save_config.assert_not_called()
            output = " ".join(str(call_item) for call_item in print_mock.call_args_list)
            self.assertIn("Выберите беседу для изменения:", output)
            self.assertIn("Выберите беседу для удаления:", output)
        finally:
            Config._SETTINGS = old_settings

    def test_group_menu_successful_change_refreshes_title_without_changing_route(self):
        old_settings = Config._SETTINGS
        try:
            Config._SETTINGS = {
                "telegram_group_routes": {
                    "-100123": {
                        "title": "Старое название",
                        "destination": "editor",
                        "editor_nickname": "@group-editor",
                    }
                }
            }
            with patch.object(
                Config,
                "get_telegram_group_dialogs",
                new=AsyncMock(return_value=[{"chat_id": "-100123", "title": "Новое название", "chat_type": "supergroup"}]),
            ), patch.object(Config, "save_config") as save_config, patch(
                "builtins.input", side_effect=["2", "1", "2", "", "4"]
            ):
                asyncio.run(Config.setup_telegram_group_routes_interactive())

            self.assertEqual("Новое название", Config.get_setting("telegram_group_routes")["-100123"]["title"])
            self.assertEqual("editor", Config.get_setting("telegram_group_routes")["-100123"]["destination"])
            self.assertEqual("@group-editor", Config.get_setting("telegram_group_routes")["-100123"]["editor_nickname"])
            save_config.assert_called_once()
        finally:
            Config._SETTINGS = old_settings

    def test_group_menu_change_survives_config_reload(self):
        old_accounts = Config._ACCOUNTS
        old_settings = Config._SETTINGS
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "config.json"
                Config._SETTINGS = {
                    "telegram_group_routes": {
                        "-100123": {"title": "Компания", "destination": "editor", "editor_nickname": "@editor"}
                    }
                }
                dialogs = [{"chat_id": "-100123", "title": "Компания", "chat_type": "group"}]
                with patch.object(Config, "CONFIG_FILE", config_path), patch.object(
                    Config, "get_telegram_group_dialogs", new=AsyncMock(return_value=dialogs)
                ), patch("builtins.input", side_effect=["2", "1", "1", "4"]):
                    asyncio.run(Config.setup_telegram_group_routes_interactive())
                    Config._SETTINGS = {}
                    Config.load_config()

                self.assertEqual("plagiscan", Config.get_setting("telegram_group_routes")["-100123"]["destination"])
        finally:
            Config._ACCOUNTS = old_accounts
            Config._SETTINGS = old_settings

    def test_group_menu_saves_change_before_next_menu_prompt(self):
        old_settings = Config._SETTINGS
        try:
            Config._SETTINGS = {
                "telegram_group_routes": {
                    "-100123": {"title": "Компания", "destination": "editor", "editor_nickname": "@editor"}
                }
            }
            dialogs = [{"chat_id": "-100123", "title": "Компания", "chat_type": "group"}]
            save_calls = []
            with patch.object(Config, "get_telegram_group_dialogs", new=AsyncMock(return_value=dialogs)), patch(
                "builtins.input", side_effect=["2", "1", "1", "4"]
            ) as input_mock, patch.object(
                Config,
                "save_config",
                side_effect=lambda: (save_calls.append(input_mock.call_count), self.assertEqual(
                    "plagiscan", Config.get_setting("telegram_group_routes")["-100123"]["destination"]
                )),
            ) as save_config:
                asyncio.run(Config.setup_telegram_group_routes_interactive())

            self.assertEqual([3], save_calls)
            save_config.assert_called_once()
        finally:
            Config._SETTINGS = old_settings

    def test_group_menu_enter_during_change_does_not_refresh_title_or_save(self):
        old_settings = Config._SETTINGS
        try:
            Config._SETTINGS = {
                "telegram_group_routes": {
                    "-100123": {"title": "Старое название", "destination": "editor", "editor_nickname": "@editor"}
                }
            }
            refresh_dialogs = AsyncMock(
                return_value=[{"chat_id": "-100123", "title": "Новое название", "chat_type": "group"}]
            )
            with patch.object(Config, "get_telegram_group_dialogs", new=refresh_dialogs), patch.object(
                Config, "save_config"
            ) as save_config, patch("builtins.input", side_effect=["2", "1", "", "4"]):
                asyncio.run(Config.setup_telegram_group_routes_interactive())

            self.assertEqual("Старое название", Config.get_setting("telegram_group_routes")["-100123"]["title"])
            refresh_dialogs.assert_not_awaited()
            save_config.assert_not_called()
        finally:
            Config._SETTINGS = old_settings

    def test_group_menu_delete_is_the_only_explicit_clear_action(self):
        old_settings = Config._SETTINGS
        try:
            Config._SETTINGS = {
                "telegram_group_routes": {
                    "-100123": {"title": "Первая компания", "destination": "plagiscan"},
                    "-100124": {"title": "Вторая компания", "destination": "plagiscan"},
                }
            }
            with patch.object(Config, "save_config") as save_config, patch(
                "builtins.input", side_effect=["3", "2", "y", "4"]
            ), patch("builtins.print") as print_mock:
                asyncio.run(Config.setup_telegram_group_routes_interactive())

            self.assertEqual(
                {"-100123": {"title": "Первая компания", "destination": "plagiscan"}},
                Config.get_setting("telegram_group_routes"),
            )
            save_config.assert_called_once()
            output = " ".join(str(call_item) for call_item in print_mock.call_args_list)
            self.assertIn("Выберите беседу для удаления:", output)
            self.assertIn("1. Первая компания → Plagiscan", output)
            self.assertIn("2. Вторая компания → Plagiscan", output)
        finally:
            Config._SETTINGS = old_settings

    def test_group_menu_saves_deletion_before_next_menu_prompt(self):
        old_settings = Config._SETTINGS
        try:
            Config._SETTINGS = {
                "telegram_group_routes": {
                    "-100123": {"title": "Компания", "destination": "plagiscan"}
                }
            }
            save_calls = []
            with patch(
                "builtins.input", side_effect=["3", "1", "y", "4"]
            ) as input_mock, patch.object(
                Config,
                "save_config",
                side_effect=lambda: (save_calls.append(input_mock.call_count), self.assertEqual(
                    {}, Config.get_setting("telegram_group_routes")
                )),
            ) as save_config:
                asyncio.run(Config.setup_telegram_group_routes_interactive())

            self.assertEqual([3], save_calls)
            save_config.assert_called_once()
        finally:
            Config._SETTINGS = old_settings

    def test_group_menu_deletion_survives_config_reload(self):
        old_accounts = Config._ACCOUNTS
        old_settings = Config._SETTINGS
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "config.json"
                Config._SETTINGS = {
                    "telegram_group_routes": {
                        "-100123": {"title": "Компания", "destination": "plagiscan"}
                    }
                }
                with patch.object(Config, "CONFIG_FILE", config_path), patch(
                    "builtins.input", side_effect=["3", "1", "y", "4"]
                ):
                    asyncio.run(Config.setup_telegram_group_routes_interactive())
                    Config._SETTINGS = {}
                    Config.load_config()

                self.assertEqual({}, Config.get_setting("telegram_group_routes"))
        finally:
            Config._ACCOUNTS = old_accounts
            Config._SETTINGS = old_settings

    def test_group_menu_delete_enter_does_not_remove_saved_route(self):
        old_settings = Config._SETTINGS
        try:
            saved_routes = {
                "-100123": {"title": "Компания", "destination": "plagiscan"}
            }
            Config._SETTINGS = {"telegram_group_routes": saved_routes}
            with patch.object(Config, "save_config") as save_config, patch(
                "builtins.input", side_effect=["3", "1", "", "4"]
            ):
                asyncio.run(Config.setup_telegram_group_routes_interactive())

            self.assertEqual(saved_routes, Config.get_setting("telegram_group_routes"))
            save_config.assert_not_called()
        finally:
            Config._SETTINGS = old_settings

    def test_group_menu_delete_n_does_not_remove_saved_route(self):
        old_settings = Config._SETTINGS
        try:
            saved_routes = {
                "-100123": {"title": "Компания", "destination": "plagiscan"}
            }
            Config._SETTINGS = {"telegram_group_routes": saved_routes}
            with patch.object(Config, "save_config") as save_config, patch(
                "builtins.input", side_effect=["3", "1", "n", "4"]
            ):
                asyncio.run(Config.setup_telegram_group_routes_interactive())

            self.assertEqual(saved_routes, Config.get_setting("telegram_group_routes"))
            save_config.assert_not_called()
        finally:
            Config._SETTINGS = old_settings

    def test_main_menu_places_group_settings_at_seven_and_exit_at_eight(self):
        from main import ConsoleMenu

        with patch("builtins.print") as print_mock:
            ConsoleMenu.show_main_menu()

        output = " ".join(str(call_item) for call_item in print_mock.call_args_list)
        self.assertIn("7. 💬 Настроить Telegram-беседы", output)
        self.assertIn("8. ❌ Выйти", output)

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
                    self.assertEqual("plagiscan", Config.get_setting("forced_authors_destination"))
                    self.assertEqual({}, Config.get_setting("telegram_group_routes"))
                    Config.save_config()
                    saved = json.loads(config_path.read_text(encoding="utf-8"))
                    self.assertEqual({}, saved["settings"]["plagiscan_user_routes"])
        finally:
            Config._ACCOUNTS = old_accounts
            Config._SETTINGS = old_settings

    def test_console_route_menu_saves_one_shared_editor_choice(self):
        old_settings = Config._SETTINGS
        try:
            Config._SETTINGS = {
                "plagiscan_users": ["@one", "vk:2"],
                "plagiscan_user_routes": {},
                "forced_authors_destination": "plagiscan",
                "forced_authors_editor_nickname": None,
            }
            with patch("builtins.input", side_effect=["2", "editor.one"]), patch.object(
                Config, "update_setting", wraps=Config.update_setting
            ) as update_setting:
                Config.setup_forced_authors_route_interactive()

            self.assertEqual("editor", Config.get_setting("forced_authors_destination"))
            self.assertEqual("@editor.one", Config.get_setting("forced_authors_editor_nickname"))
            self.assertEqual({}, Config.get_setting("plagiscan_user_routes"))
            update_setting.assert_any_call("forced_authors_destination", "editor")
            update_setting.assert_any_call("forced_authors_editor_nickname", "@editor.one")
        finally:
            Config._SETTINGS = old_settings

    def test_route_resolution_reuses_force_user_matching_for_telegram_and_vk(self):
        handlers = make_handlers()
        settings = base_settings(
            ["@tg-author", "vk:700", "@plagiscan-only"],
            forced_authors_destination="editor",
            forced_authors_editor_nickname="@shared-editor",
        )
        with patch("bot_handlers.Config.get_setting", side_effect=settings_lookup(settings)):
            self.assertEqual(
                {"destination": "editor", "editor_nickname": "@shared-editor"},
                handlers.get_force_author_route({"author": "tg-author", "author_id": 42}),
            )
            self.assertEqual(
                {"destination": "editor", "editor_nickname": "@shared-editor"},
                handlers.get_force_author_route(
                    {"author": "VK Chat", "route_sender_id": "700", "route_sender_name": "vk-name"}
                ),
            )
            self.assertEqual(
                {"destination": "editor", "editor_nickname": "@shared-editor"},
                handlers.get_force_author_route({"author": "plagiscan-only"}),
            )
            self.assertIsNone(handlers.get_force_author_route({"author": "ordinary"}))

    def test_force_author_numeric_telegram_id_is_allowed_with_username(self):
        handlers = make_handlers()
        settings = base_settings(
            ["987654321"],
            forced_authors_destination="editor",
            forced_authors_editor_nickname="@editor",
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
            forced_authors_destination="editor",
            forced_authors_editor_nickname="@fixed-editor",
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
            forced_authors_destination="editor",
            forced_authors_editor_nickname="@shared-editor",
        )

        with patch("bot_handlers.Config.get_setting", side_effect=settings_lookup(settings)):
            asyncio.run(handlers.process_queue())
            asyncio.run(handlers.process_queue())

        destinations = [call.kwargs["chat_id"] for call in editor.send_document.await_args_list]
        self.assertEqual(["@shared-editor", "@shared-editor"], destinations)

        plagiscan_handlers = make_handlers()
        plagiscan_handlers.process_anti_file = AsyncMock()
        plagiscan_handlers.manager.file_queue = [telegram_file_info("plagiscan-only", 11, "legacy.docx")]
        plagiscan_settings = base_settings(
            ["plagiscan-only"],
            forced_authors_destination="plagiscan",
            forced_authors_editor_nickname=None,
        )
        with patch("bot_handlers.Config.get_setting", side_effect=settings_lookup(plagiscan_settings)):
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

    async def test_group_editor_route_delivers_both_reports_to_original_group_reply(self):
        task = telegram_file_info(
            "ordinary",
            42,
            "анти курсовая.docx",
            chat_id=-100123,
            message_id=777,
        )
        task["is_anti"] = True
        task["force_plagiscan"] = False
        settings = base_settings(
            [],
            telegram_group_routes={
                "-100123": {
                    "title": "Компания",
                    "destination": "editor",
                    "editor_nickname": "@group-editor",
                }
            },
        )
        sent = [SimpleNamespace(id=501, chat=SimpleNamespace(id=301))]
        handlers, nik1, nik2 = await self._make_handlers_with_tasks([task], sent, settings)
        tracking_key, tracking = next(iter(handlers.editor_tracking.items()))
        self.assertEqual("@group-editor", tracking["destination"])
        self.assertEqual(-100123, tracking["route_chat_id"])
        self.assertEqual(777, tracking["route_message_id"])
        self.assertTrue(tracking["route_is_group"])
        self.assertEqual("@group-editor", nik2.send_document.await_args.kwargs["chat_id"])

        normal_path = Path(tempfile.gettempdir()) / "group-normal.pdf"
        ai_path = Path(tempfile.gettempdir()) / "group-ai.pdf"
        normal_path.write_bytes(b"normal")
        ai_path.write_bytes(b"ai")
        client = SimpleNamespace(name="НИК-2")
        with patch("bot_handlers.Config.get_setting", side_effect=settings_lookup(settings)):
            await handlers.handle_editor_response(
                client,
                await self.make_response("анти курсовая.pdf", 501, 301, normal_path),
            )
            self.assertIn(tracking_key, handlers.editor_tracking)
            await handlers.handle_editor_response(
                client,
                await self.make_response("ИИ анти курсовая.pdf", 501, 301, ai_path),
            )

        self.assertEqual(2, nik1.send_document.await_count)
        self.assertEqual([-100123, -100123], [item.kwargs["chat_id"] for item in nik1.send_document.await_args_list])
        self.assertEqual([777, 777], [item.kwargs["reply_to_message_id"] for item in nik1.send_document.await_args_list])
        self.assertEqual(
            {"анти курсовая.pdf", "ии анти курсовая.pdf"},
            tracking["delivered_reports"],
        )

    async def test_full_fixed_editor_flow_accepts_two_unreplied_reports_without_mutating_message(self):
        task = telegram_file_info(
            "ordinary",
            42,
            "курсовая работа.docx",
            chat_id=-100123,
            message_id=777,
        )
        settings = base_settings(
            [],
            telegram_group_routes={
                "-100123": {
                    "title": "Компания",
                    "destination": "editor",
                    "editor_nickname": "@fixed-editor",
                }
            },
        )
        sent = [SimpleNamespace(id=300330, chat=SimpleNamespace(id=301))]
        handlers, nik1, _nik2 = await self._make_handlers_with_tasks([task], sent, settings)
        tracking_key, tracking = next(iter(handlers.editor_tracking.items()))
        handlers._increment_files_today("telegram")
        source_count = handlers.files_today["count"]

        class PyrogramLikeMessage(SimpleNamespace):
            def __copy__(self):
                return type(self)(**{key: value for key, value in vars(self).items() if key != "_client"})

            async def download(self, path):
                if self is not self.original or not hasattr(self, "_client"):
                    raise AssertionError("download must use the original Pyrogram message")
                Path(path).write_bytes(self.payload)
                return path

        normal = PyrogramLikeMessage(
            id=7001,
            _client=object(),
            reply_to_message_id=None,
            document=SimpleNamespace(file_name="курсовая работа.pdf"),
            chat=SimpleNamespace(id=301),
            from_user=SimpleNamespace(username="fixed-editor", id=9000),
            payload=b"normal",
        )
        ai = PyrogramLikeMessage(
            id=7002,
            _client=object(),
            reply_to_message_id=None,
            document=SimpleNamespace(file_name="ИИ курсовая работа.pdf"),
            chat=SimpleNamespace(id=301),
            from_user=SimpleNamespace(username="fixed-editor", id=9000),
            payload=b"ai",
        )
        normal.original = normal
        ai.original = ai
        from copy import copy
        with self.assertRaises(AssertionError):
            await copy(normal).download("must-not-be-written.pdf")
        client = SimpleNamespace(name="НИК-2")

        with patch("bot_handlers.Config.get_setting", side_effect=settings_lookup(settings)), patch(
            "os.remove"
        ), patch("os.path.exists", return_value=True), patch("builtins.print") as print_mock:
            await handlers.handle_editor_response(client, normal)
            await handlers.handle_editor_response(client, ai)
            await handlers.handle_editor_response(client, normal)

        self.assertIsNone(normal.reply_to_message_id)
        self.assertIsNone(ai.reply_to_message_id)
        self.assertEqual(2, nik1.send_document.await_count)
        self.assertEqual(
            ["курсовая работа.pdf", "ИИ курсовая работа.pdf"],
            [call.kwargs["file_name"] for call in nik1.send_document.await_args_list],
        )
        self.assertEqual(
            [-100123, -100123],
            [call.kwargs["chat_id"] for call in nik1.send_document.await_args_list],
        )
        self.assertEqual(
            [777, 777],
            [call.kwargs["reply_to_message_id"] for call in nik1.send_document.await_args_list],
        )
        self.assertEqual(
            {"курсовая работа.pdf", "ии курсовая работа.pdf"},
            tracking["delivered_reports"],
        )
        self.assertIn(tracking_key, handlers.editor_tracking)
        self.assertEqual(source_count, handlers.files_today["count"])
        self.assertEqual(
            [],
            [
                call_args
                for call_args in print_mock.call_args_list
                if "Не найдена информация об отправке" in str(call_args)
            ],
        )

    async def test_editor_response_download_failure_releases_claim_for_retry(self):
        task = telegram_file_info(
            "ordinary",
            42,
            "курсовая работа.docx",
            chat_id=-100123,
            message_id=777,
        )
        settings = base_settings(
            [],
            telegram_group_routes={
                "-100123": {
                    "title": "Компания",
                    "destination": "editor",
                    "editor_nickname": "@fixed-editor",
                }
            },
        )
        sent = [SimpleNamespace(id=300330, chat=SimpleNamespace(id=301))]
        handlers, nik1, _nik2 = await self._make_handlers_with_tasks([task], sent, settings)
        result_path = Path(tempfile.gettempdir()) / "retry-editor-response.pdf"
        result_path.write_bytes(b"normal")
        message = SimpleNamespace(
            id=7003,
            reply_to_message_id=None,
            document=SimpleNamespace(file_name="курсовая работа.pdf"),
            chat=SimpleNamespace(id=301),
            from_user=SimpleNamespace(username="fixed-editor", id=9000),
        )
        attempts = 0

        async def download(path):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary download failure")
            Path(path).write_bytes(result_path.read_bytes())
            return path

        message.download = download
        client = SimpleNamespace(name="НИК-2")

        with patch("bot_handlers.Config.get_setting", side_effect=settings_lookup(settings)), patch(
            "os.remove"
        ), patch("os.path.exists", return_value=True):
            await handlers.handle_editor_response(client, message)
            self.assertEqual(0, nik1.send_document.await_count)
            await handlers.handle_editor_response(client, message)

        self.assertEqual(2, attempts)
        self.assertEqual(1, nik1.send_document.await_count)

    async def test_telegram_spool_is_not_accepted_and_same_editor_message_can_retry(self):
        task = telegram_file_info(
            "ordinary",
            42,
            "курсовая работа.docx",
            chat_id=-100123,
            message_id=777,
        )
        settings = base_settings(
            [],
            telegram_group_routes={
                "-100123": {
                    "title": "Компания",
                    "destination": "editor",
                    "editor_nickname": "@fixed-editor",
                }
            },
        )
        sent = [SimpleNamespace(id=300330, chat=SimpleNamespace(id=301))]
        handlers, _nik1, _nik2 = await self._make_handlers_with_tasks([task], sent, settings)
        message = SimpleNamespace(
            id=7006,
            reply_to_message_id=300330,
            document=SimpleNamespace(file_name="курсовая работа.pdf"),
            chat=SimpleNamespace(id=301),
            from_user=SimpleNamespace(username="fixed-editor", id=9000),
        )

        async def download(path):
            Path(path).write_bytes(b"normal")
            return path

        message.download = download
        handlers._deliver_document_to_origin = AsyncMock(
            side_effect=[
                {"status": "spooled", "outbox_id": "editor-result-1"},
                {"status": "sent"},
            ]
        )
        handlers._finish_processing = MagicMock()
        client = SimpleNamespace(name="НИК-2")

        with patch("bot_handlers.Config.get_setting", side_effect=settings_lookup(settings)), patch(
            "os.remove"
        ), patch("os.path.exists", return_value=True):
            await handlers.handle_editor_response(client, message)
            tracking = next(iter(handlers.editor_tracking.values()))
            self.assertEqual(set(), tracking["delivered_reports"])
            self.assertNotIn(("301", "7006"), handlers._editor_response_claims)
            handlers._finish_processing.assert_called_once_with(tracking["file_uid"], False)
            await handlers.handle_editor_response(client, message)

        self.assertEqual(2, handlers._deliver_document_to_origin.await_count)
        self.assertEqual({"курсовая работа.pdf"}, tracking["delivered_reports"])
        self.assertIn(("301", "7006"), handlers._editor_response_claims)
        self.assertEqual(2, handlers._finish_processing.call_count)
        handlers._finish_processing.assert_any_call(tracking["file_uid"], False)
        handlers._finish_processing.assert_called_with(tracking["file_uid"], True)

    async def test_duplicate_incoming_editor_message_is_delivered_once(self):
        task = telegram_file_info(
            "ordinary",
            42,
            "курсовая работа.docx",
            chat_id=-100123,
            message_id=777,
        )
        settings = base_settings(
            [],
            telegram_group_routes={
                "-100123": {
                    "title": "Компания",
                    "destination": "editor",
                    "editor_nickname": "@fixed-editor",
                }
            },
        )
        sent = [SimpleNamespace(id=300330, chat=SimpleNamespace(id=301))]
        handlers, nik1, _nik2 = await self._make_handlers_with_tasks([task], sent, settings)
        message = SimpleNamespace(
            id=7004,
            reply_to_message_id=300330,
            document=SimpleNamespace(file_name="курсовая работа.pdf"),
            chat=SimpleNamespace(id=301),
            from_user=SimpleNamespace(username="fixed-editor", id=9000),
        )

        async def download(path):
            await asyncio.sleep(0)
            Path(path).write_bytes(b"normal")
            return path

        async def deliver(*args, **kwargs):
            await asyncio.sleep(0)
            return {"status": "sent"}

        message.download = download
        handlers._deliver_document_to_origin = AsyncMock(side_effect=deliver)
        client = SimpleNamespace(name="НИК-2")

        with patch("bot_handlers.Config.get_setting", side_effect=settings_lookup(settings)), patch(
            "os.remove"
        ), patch("os.path.exists", return_value=True):
            await asyncio.gather(
                handlers.handle_editor_response(client, message),
                handlers.handle_editor_response(client, message),
            )

        self.assertEqual(1, handlers._deliver_document_to_origin.await_count)

    async def test_unknown_editor_reply_is_logged_once_without_filename_fallback(self):
        handlers = make_handlers()
        handlers.editor_tracking["fixed_editor_301_300330"] = {
            "author": "ordinary",
            "original_name": "курсовая работа.docx",
            "expected_pdf_name": "курсовая работа.pdf",
            "expected_ai_pdf_name": "ИИ курсовая работа.pdf",
            "destination": "@fixed-editor",
            "sent_from_account": "НИК-2",
            "reply_to_message_id": 300330,
            "chat_id": 301,
            "fixed_author_editor_route": True,
            "delivered_reports": set(),
        }
        message = SimpleNamespace(
            id=7005,
            reply_to_message_id=300329,
            document=SimpleNamespace(file_name="курсовая работа.pdf"),
            chat=SimpleNamespace(id=301),
            from_user=SimpleNamespace(username="fixed-editor", id=9000),
        )
        client = SimpleNamespace(name="НИК-2")

        with patch("builtins.print") as print_mock:
            await handlers.handle_editor_response(client, message)
            await handlers.handle_editor_response(client, message)

        warnings = [
            call_args
            for call_args in print_mock.call_args_list
            if "Не найдена информация об отправке" in str(call_args)
        ]
        self.assertEqual(1, len(warnings))

    @staticmethod
    def _same_named_fixed_tracking(old_delivered_reports):
        return {
            "fixed_editor_301_300329": {
                "original_name": "ДИПЛОМ.docx",
                "expected_pdf_name": "ДИПЛОМ.pdf",
                "expected_ai_pdf_name": "ИИ ДИПЛОМ.pdf",
                "destination": "@fixed-editor",
                "sent_from_account": "НИК-2",
                "reply_to_message_id": 300329,
                "chat_id": 301,
                "fixed_author_editor_route": True,
                "delivered_reports": set(old_delivered_reports),
                "sent_at": datetime.now(),
            },
            "fixed_editor_301_300330": {
                "original_name": "ДИПЛОМ.docx",
                "expected_pdf_name": "ДИПЛОМ.pdf",
                "expected_ai_pdf_name": "ИИ ДИПЛОМ.pdf",
                "destination": "@fixed-editor",
                "sent_from_account": "НИК-2",
                "reply_to_message_id": 300330,
                "chat_id": 301,
                "fixed_author_editor_route": True,
                "delivered_reports": set(),
                "sent_at": datetime.now(),
            },
        }

    async def _assert_single_report_history_blocks_filename_fallback(self, old_report, incoming_name):
        handlers = make_handlers()
        handlers.editor_tracking = self._same_named_fixed_tracking({old_report})

        key, info, reason = handlers.find_editor_tracking_for_unreplied_pdf(
            "fixed-editor",
            incoming_name,
            reply_chat_id=301,
        )

        self.assertIsNone(key)
        self.assertIsNone(info)
        self.assertIn("неоднозначное", reason)

        handlers._deliver_document_to_origin = AsyncMock()
        message = SimpleNamespace(
            id=7008,
            reply_to_message_id=None,
            document=SimpleNamespace(file_name=incoming_name),
            chat=SimpleNamespace(id=301),
            from_user=SimpleNamespace(username="fixed-editor", id=9000),
        )
        with patch("builtins.print"):
            await handlers.handle_editor_response(SimpleNamespace(name="НИК-2"), message)

        handlers._deliver_document_to_origin.assert_not_awaited()
        self.assertIn("fixed_editor_301_300330", handlers.editor_tracking)

    async def test_recent_normal_only_report_blocks_same_name_filename_fallback(self):
        await self._assert_single_report_history_blocks_filename_fallback("диплом.pdf", "ДИПЛОМ.pdf")

    async def test_recent_ai_only_report_blocks_same_name_filename_fallback(self):
        await self._assert_single_report_history_blocks_filename_fallback("ии диплом.pdf", "ИИ ДИПЛОМ.pdf")

    async def test_explicit_reply_selects_active_task_after_single_old_report(self):
        handlers = make_handlers()
        handlers.editor_tracking = self._same_named_fixed_tracking({"диплом.pdf"})

        key, info = handlers._find_editor_tracking_by_reply(300330, chat_id=301)

        self.assertEqual("fixed_editor_301_300330", key)
        self.assertEqual(300330, info["reply_to_message_id"])

    async def test_completed_fixed_task_is_not_a_filename_candidate_for_next_same_named_task(self):
        handlers = make_handlers()
        handlers.editor_tracking = {
            "fixed_editor_301_300329": {
                "original_name": "курсовая работа.docx",
                "expected_pdf_name": "курсовая работа.pdf",
                "expected_ai_pdf_name": "ИИ курсовая работа.pdf",
                "destination": "@fixed-editor",
                "sent_from_account": "НИК-2",
                "reply_to_message_id": 300329,
                "chat_id": 301,
                "fixed_author_editor_route": True,
                "delivered_reports": {"курсовая работа.pdf", "ии курсовая работа.pdf"},
                "sent_at": datetime.now(),
            },
            "fixed_editor_301_300330": {
                "original_name": "курсовая работа.docx",
                "expected_pdf_name": "курсовая работа.pdf",
                "expected_ai_pdf_name": "ИИ курсовая работа.pdf",
                "destination": "@fixed-editor",
                "sent_from_account": "НИК-2",
                "reply_to_message_id": 300330,
                "chat_id": 301,
                "fixed_author_editor_route": True,
                "delivered_reports": set(),
                "sent_at": datetime.now(),
            },
        }

        key, info, reason = handlers.find_editor_tracking_for_unreplied_pdf(
            "fixed-editor",
            "курсовая работа.pdf",
            reply_chat_id=301,
        )

        self.assertIsNone(key)
        self.assertIsNone(info)
        self.assertIn("неоднозначное", reason)

        handlers._deliver_document_to_origin = AsyncMock()
        late_message = SimpleNamespace(
            id=7007,
            reply_to_message_id=None,
            document=SimpleNamespace(file_name="курсовая работа.pdf"),
            chat=SimpleNamespace(id=301),
            from_user=SimpleNamespace(username="fixed-editor", id=9000),
        )
        with patch("builtins.print"):
            await handlers.handle_editor_response(SimpleNamespace(name="НИК-2"), late_message)

        handlers._deliver_document_to_origin.assert_not_awaited()
        self.assertIn("fixed_editor_301_300330", handlers.editor_tracking)

    async def test_explicit_reply_selects_active_same_named_task_after_completed_task(self):
        handlers = make_handlers()
        handlers.editor_tracking = {
            "fixed_editor_301_300329": {
                "original_name": "курсовая работа.docx",
                "expected_pdf_name": "курсовая работа.pdf",
                "expected_ai_pdf_name": "ИИ курсовая работа.pdf",
                "destination": "@fixed-editor",
                "sent_from_account": "НИК-2",
                "reply_to_message_id": 300329,
                "chat_id": 301,
                "fixed_author_editor_route": True,
                "delivered_reports": {"курсовая работа.pdf", "ии курсовая работа.pdf"},
                "sent_at": datetime.now(),
            },
            "fixed_editor_301_300330": {
                "original_name": "курсовая работа.docx",
                "expected_pdf_name": "курсовая работа.pdf",
                "expected_ai_pdf_name": "ИИ курсовая работа.pdf",
                "destination": "@fixed-editor",
                "sent_from_account": "НИК-2",
                "reply_to_message_id": 300330,
                "chat_id": 301,
                "fixed_author_editor_route": True,
                "delivered_reports": set(),
                "sent_at": datetime.now(),
            },
        }

        key, info = handlers._find_editor_tracking_by_reply(300330, chat_id=301)

        self.assertEqual("fixed_editor_301_300330", key)
        self.assertEqual(300330, info["reply_to_message_id"])

    async def test_editor_response_claims_expire_without_unbounded_growth(self):
        handlers = make_handlers()
        now = datetime.now()
        handlers._editor_response_claims = {
            ("301", "old"): now - timedelta(hours=25),
            ("301", "fresh"): now,
        }
        handlers._editor_response_unknown_warnings = {
            ("301", "old-warning"): now - timedelta(hours=25),
            ("301", "fresh-warning"): now,
        }

        handlers._cleanup_editor_response_state(now)

        self.assertNotIn(("301", "old"), handlers._editor_response_claims)
        self.assertIn(("301", "fresh"), handlers._editor_response_claims)
        self.assertNotIn(("301", "old-warning"), handlers._editor_response_unknown_warnings)
        self.assertIn(("301", "fresh-warning"), handlers._editor_response_unknown_warnings)

    async def test_two_group_routes_use_different_editors_for_same_named_files(self):
        tasks = [
            telegram_file_info("first", 1, "работа.docx", chat_id=-1001, message_id=10),
            telegram_file_info("second", 2, "работа.docx", chat_id=-1002, message_id=20),
        ]
        for task in tasks:
            task["is_anti"] = False
            task["force_plagiscan"] = False
        settings = base_settings(
            [],
            telegram_group_routes={
                "-1001": {"title": "Первая", "destination": "editor", "editor_nickname": "@editor-one"},
                "-1002": {"title": "Вторая", "destination": "editor", "editor_nickname": "@editor-two"},
            },
        )
        sent = [
            SimpleNamespace(id=601, chat=SimpleNamespace(id=401)),
            SimpleNamespace(id=602, chat=SimpleNamespace(id=402)),
        ]
        handlers, _nik1, nik2 = await self._make_handlers_with_tasks(tasks, sent, settings)

        self.assertEqual(
            ["@editor-one", "@editor-two"],
            [item.kwargs["chat_id"] for item in nik2.send_document.await_args_list],
        )
        self.assertEqual(2, len(handlers.editor_tracking))

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
            forced_authors_destination="editor",
            forced_authors_editor_nickname="@fixed-editor",
        )
        sent = [SimpleNamespace(id=501, chat=SimpleNamespace(id=301))]
        handlers, nik1, _nik2 = await self._make_handlers_with_tasks(tasks, sent, settings)
        tracking_key, tracking = next(iter(handlers.editor_tracking.items()))
        self.assertEqual("@fixed-editor", tracking["destination"])
        handlers._increment_files_today("telegram")
        initial_count = handlers.files_today["count"]

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
        self.assertEqual(initial_count, handlers.files_today["count"])
        self.assertEqual(1, handlers.files_today["by_source"]["telegram"])

    async def test_same_message_id_in_different_editor_chats_keeps_tasks_separate(self):
        tasks = [
            telegram_file_info("author-a", 101, "работа.docx", message_id=1),
            telegram_file_info("author-b", 102, "работа.docx", message_id=2),
        ]
        settings = base_settings(
            ["author-a", "author-b"],
            forced_authors_destination="editor",
            forced_authors_editor_nickname="@shared-editor",
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
            forced_authors_destination="editor",
            forced_authors_editor_nickname="@editor",
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
            forced_authors_destination="editor",
            forced_authors_editor_nickname="@fixed-editor",
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
            forced_authors_destination="editor",
            forced_authors_editor_nickname="@editor",
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
            forced_authors_destination="editor",
            forced_authors_editor_nickname="@fixed-editor",
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
            forced_authors_destination="editor",
            forced_authors_editor_nickname="@editor",
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
        client.name = "legacy-1"
        client.send_message = AsyncMock()
        client.send_document = AsyncMock(side_effect=ValueError("invalid document"))
        manager.clients = {"legacy-1": client}
        manager.get_available_account.return_value = "legacy-1"
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
    def test_normal_account_setting_keeps_only_active_accounts(self):
        old_settings = Config._SETTINGS
        try:
            Config._SETTINGS = {
                "normal_accounts": ["НИК-2", "НИК-3", "legacy-account"],
            }
            Config._normalize_settings()
            self.assertEqual(["НИК-2"], Config.get_setting("normal_accounts"))
        finally:
            Config._SETTINGS = old_settings

    def test_only_first_two_accounts_are_required_in_every_mode(self):
        with patch(
            "config.Config.get_setting",
            side_effect=settings_lookup(
                {"mode": "mode1", "normal_destination": "редактор", "normal_accounts": ["legacy-1"]}
            ),
        ):
            self.assertEqual({"НИК-1", "НИК-2"}, AccountManager.required_account_names())

        with patch(
            "config.Config.get_setting",
            side_effect=settings_lookup(
                {"mode": "mode1", "normal_destination": "бот", "normal_accounts": ["legacy-1", "legacy-2"]}
            ),
        ):
            self.assertEqual({"НИК-1", "НИК-2"}, AccountManager.required_account_names())

    def test_client_initialization_skips_unused_aaa_accounts(self):
        manager = AccountManager()
        accounts = {
            name: {"api_id": 1, "api_hash": "hash", "phone": None}
            for name in ("НИК-1", "НИК-2", "legacy-1", "legacy-2")
        }
        settings = {
            "mode": "mode1",
            "normal_destination": "редактор",
            "normal_accounts": ["legacy-1", "legacy-2"],
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

    def test_authorization_uses_only_the_two_active_accounts(self):
        manager = AccountManager()
        accounts = {
            "НИК-1": {"api_id": 1, "api_hash": "hash1", "phone": "+1001"},
            "НИК-2": {"api_id": 2, "api_hash": "hash2", "phone": "+1002"},
            "legacy-account": {"api_id": 3, "api_hash": "hash3", "phone": "+1003"},
        }

        class FakeClient:
            def __init__(self, name):
                self.name = name
                self.started = False
                self.stopped = False

            async def start(self):
                self.started = True

            async def get_me(self):
                return SimpleNamespace(first_name=self.name, username=self.name.lower(), phone_number=None)

            async def stop(self):
                self.stopped = True

        clients = {}

        def make_client(**kwargs):
            client = FakeClient(kwargs["name"])
            clients[kwargs["name"]] = client
            return client

        with tempfile.TemporaryDirectory() as temp_dir:
            sessions_dir = Path(temp_dir) / "sessions"
            with patch.object(Config, "load_config"), patch.object(
                Config, "get_all_accounts", return_value=accounts
            ), patch.object(Config, "SESSIONS_DIR", sessions_dir), patch(
                "config.Client", side_effect=make_client
            ), patch.object(Config, "save_config"), patch("builtins.input") as input_mock:
                asyncio.run(manager.authorize_all_accounts())

        self.assertEqual({"НИК-1", "НИК-2"}, set(clients))
        self.assertTrue(all(client.started and client.stopped for client in clients.values()))
        input_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
