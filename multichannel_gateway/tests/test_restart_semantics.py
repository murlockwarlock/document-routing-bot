from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from multichannel_gateway.tests import test_forced_author_routing as fixtures
from config import Config
from main import FileDistributionBot
from multichannel_gateway.core.files import AtomicFileStore
from multichannel_gateway.core.models import AttachmentRef, InboundEnvelope
from multichannel_gateway.core.outbox import LocalOutboxSpool
from multichannel_gateway.core.router import InboundGateway
from multichannel_gateway.core.storage import SqliteJobStore


class RestartSemanticsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = SqliteJobStore(self.root / "jobs.sqlite3")
        self.files = AtomicFileStore(self.root / "inbox")
        self.outbox = LocalOutboxSpool(self.root / "outbox")
        self.settings = fixtures.base_settings(
            ["author"], forced_authors_destination="editor", forced_authors_editor_nickname="@editor",
            allowed_authors=[], telegram_group_routes={
                "-100123": {"title": "Group", "destination": "editor", "editor_nickname": "@editor"},
            },
        )
        for patcher in (
            patch.object(Config, "_SETTINGS", self.settings),
            patch("main.build_store", return_value=self.store),
            patch("bot_handlers.build_store", return_value=self.store),
            patch("bot_handlers.BotHandlers._setup_periodic_cleanup"),
            patch("multichannel_gateway.outbound.build_outbox", return_value=self.outbox),
            patch("multichannel_gateway.integrations.telegram_ingest.build_gateway", return_value=InboundGateway(self.store, self.files)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.next_reply_id = 300

    def bot(self):
        bot = FileDistributionBot()
        nik1 = SimpleNamespace(name="НИК-1", is_connected=True, send_document=AsyncMock())
        async def send(**kwargs):
            self.next_reply_id += 1
            return SimpleNamespace(id=self.next_reply_id, chat=SimpleNamespace(id=900))
        nik2 = SimpleNamespace(name="НИК-2", is_connected=True, send_document=AsyncMock(side_effect=send))
        bot.manager.clients = {"НИК-1": nik1, "НИК-2": nik2}
        bot.handlers._build_result_file_path = lambda name: str(self.root / name)
        return bot

    async def restart(self):
        bot = self.bot()
        await bot.prepare_clean_start(datetime.now(timezone.utc).isoformat())
        return bot

    def source(self, message_id=1, name="ДИПЛОМ.docx"):
        async def download(path):
            Path(path).write_bytes(b"source")
            return path
        return SimpleNamespace(
            id=message_id, chat=SimpleNamespace(id=-100123, type="supergroup"),
            from_user=SimpleNamespace(id=42, username="author"), text=None, reply_to_message_id=None,
            document=SimpleNamespace(file_name=name, file_id=f"file-{message_id}", file_size=6, mime_type="application/docx"),
            download=download,
        )

    def report(self, reply=None, name="ДИПЛОМ.pdf"):
        async def download(path):
            Path(path).write_bytes(b"pdf")
            return path
        return SimpleNamespace(
            id=800, chat=SimpleNamespace(id=900), from_user=SimpleNamespace(id=900, username="editor"),
            text=None, reply_to_message_id=reply, document=SimpleNamespace(file_name=name, mime_type="application/pdf"),
            download=download,
        )

    def enqueue(self, source="telegram", message_id=1, name="ДИПЛОМ.docx"):
        attachment = AttachmentRef(index=0, file_name=name)
        envelope = InboundEnvelope(source, f"{source}:1:{message_id}", "1", "42", "author", str(message_id), attachments=(attachment,))
        saved = self.files.persist_bytes(envelope.attachment_dedupe_key(attachment), name, b"source")
        return self.store.enqueue_file(envelope, attachment, saved)

    async def test_fresh_runtime_accepts_new_source_and_delivers_group_reply(self):
        old = self.bot()
        old.manager.mark_account_busy("НИК-2", "old.docx")
        for state in (old.handlers.current_processing_files, old.handlers.editor_tracking, old.handlers.message_to_file_map,
                      old.handlers._editor_response_claims, old.handlers._editor_response_unknown_warnings):
            state["old"] = {}
        bot = await self.restart()
        for state in (bot.handlers.current_processing_files, bot.handlers.editor_tracking, bot.handlers.message_to_file_map,
                      bot.handlers._editor_response_claims, bot.handlers._editor_response_unknown_warnings,
                      bot.manager.processing_files, bot.manager.file_statuses):
            self.assertEqual({}, state)
        await bot.handlers.handle_main_account(bot.manager.clients["НИК-1"], self.source(name="Новая.docx"))
        self.assertEqual(1, bot.manager.clients["НИК-2"].send_document.await_count)
        await bot.handlers.handle_editor_response(bot.manager.clients["НИК-2"], self.report(name="Новая.pdf"))
        sent = bot.manager.clients["НИК-1"].send_document.await_args.kwargs
        self.assertEqual((-100123, 1), (sent["chat_id"], sent["reply_to_message_id"]))
        self.assertEqual(1, bot.handlers.files_today["count"])

    async def test_old_editor_waiting_response_cannot_select_new_same_name(self):
        old = self.bot()
        await old.handlers.handle_main_account(old.manager.clients["НИК-1"], self.source())
        old_info = next(iter(old.handlers.editor_tracking.values()))
        bot = await self.restart()
        self.assertEqual("abandoned", self.store.get_job(old_info["gateway_job_id"]).status)
        await bot.handlers.handle_editor_response(bot.manager.clients["НИК-2"], self.report())
        await bot.handlers.handle_main_account(bot.manager.clients["НИК-1"], self.source(2))
        for name in ("ДИПЛОМ.pdf", "ИИ ДИПЛОМ.pdf"):
            await bot.handlers.handle_editor_response(bot.manager.clients["НИК-2"], self.report(name=name))
            await bot.handlers.handle_editor_response(bot.manager.clients["НИК-2"], self.report(old_info["reply_to_message_id"], name))
        bot.manager.clients["НИК-1"].send_document.assert_not_awaited()
        new_info = next(iter(bot.handlers.editor_tracking.values()))
        await bot.handlers.handle_editor_response(bot.manager.clients["НИК-2"], self.report(new_info["reply_to_message_id"]))
        bot.manager.clients["НИК-1"].send_document.assert_awaited_once()

    async def test_old_plagiscan_response_cannot_select_new_same_name(self):
        old = self.bot()
        self.settings["telegram_group_routes"]["-100123"]["destination"] = "plagiscan"
        await old.handlers.handle_main_account(old.manager.clients["НИК-1"], self.source())
        self.assertEqual(1, len(old.handlers.current_processing_files))
        old_reply = self.next_reply_id
        bot = await self.restart()
        await bot.handlers.handle_anti_bot_response(bot.manager.clients["НИК-2"], self.report(old_reply))
        await bot.handlers.handle_anti_bot_response(bot.manager.clients["НИК-2"], self.report())
        await bot.handlers.handle_main_account(bot.manager.clients["НИК-1"], self.source(2))
        for reply in (None, old_reply):
            await bot.handlers.handle_anti_bot_response(bot.manager.clients["НИК-2"], self.report(reply))
        bot.manager.clients["НИК-1"].send_document.assert_not_awaited()
        mapped = bot.handlers.find_anti_processing_for_response("НИК-2", self.report(self.next_reply_id))
        self.assertIsNotNone(mapped[1])

    async def test_delivered_editor_safety_survives_restart(self):
        old = self.bot()
        await old.handlers.handle_main_account(old.manager.clients["НИК-1"], self.source())
        await old.handlers.handle_editor_response(old.manager.clients["НИК-2"], self.report())
        with self.store._connect() as connection:
            before = [tuple(row) for row in connection.execute("SELECT * FROM editor_report_safety WHERE editor!='*'")]
        self.assertTrue(before)
        bot = await self.restart()
        with self.store._connect() as connection:
            after = [tuple(row) for row in connection.execute("SELECT * FROM editor_report_safety WHERE editor!='*'")]
        self.assertEqual(before, after)
        await bot.handlers.handle_main_account(bot.manager.clients["НИК-1"], self.source(2))
        await bot.handlers.handle_editor_response(bot.manager.clients["НИК-2"], self.report())
        bot.manager.clients["НИК-1"].send_document.assert_not_awaited()

    async def test_pending_jobs_and_outbox_abandoned_new_source_works(self):
        job = self.enqueue()
        self.store.mark_delivery_pending(job.job_id, "offline")
        pdf = self.root / "old.pdf"
        pdf.write_bytes(b"old report")
        entry = self.outbox.spool_delivery("telegram", {"chat_id": "1"}, pdf)
        bot = await self.restart()
        stored = SqliteJobStore(self.store.db_path).get_job(job.job_id)
        self.assertEqual("abandoned", stored.status)
        self.assertEqual("abandoned_on_restart", stored.last_error)
        self.assertIsNone(self.store.claim_next_bridgeable("worker", ("telegram",)))
        self.assertEqual("abandoned", self.outbox.get_entry(entry["id"])["status"])
        self.assertEqual(b"old report", Path(entry["stored_file_path"]).read_bytes())
        bot.handlers.outbound_dispatcher.send_result = Mock()
        self.assertEqual([], bot.handlers.outbound_dispatcher.replay_outbox())
        bot.handlers.outbound_dispatcher.send_result.assert_not_called()
        self.assertEqual({}, bot.manager.processing_files)
        await bot.handlers.handle_main_account(bot.manager.clients["НИК-1"], self.source(2, "Новая.docx"))
        await bot.handlers.handle_editor_response(bot.manager.clients["НИК-2"], self.report(name="Новая.pdf"))
        bot.manager.clients["НИК-1"].send_document.assert_awaited_once()
        self.assertEqual("abandoned", self.store.get_job(job.job_id).status)

    async def test_old_inbound_update_does_not_requeue_abandoned_source(self):
        old = self.bot()
        await old.handlers.handle_main_account(old.manager.clients["НИК-1"], self.source())
        bot = await self.restart()
        await bot.handlers.handle_main_account(bot.manager.clients["НИК-1"], self.source())
        bot.manager.clients["НИК-2"].send_document.assert_not_awaited()
        self.assertEqual(0, bot.handlers.files_today["count"])
        self.assertEqual({}, bot.handlers.editor_tracking)

    async def test_vk_max_outbox_replay_and_jobs_unchanged(self):
        pdf = self.root / "report.pdf"
        pdf.write_bytes(b"pdf")
        entries = [self.outbox.spool_delivery(source, {"chat_id": "1"}, pdf) for source in ("vk", "max")]
        jobs = [self.enqueue(source) for source in ("vk", "max")]
        bot = await self.restart()
        for job in jobs:
            self.assertEqual("queued", self.store.get_job(job.job_id).status)
        for entry in entries:
            self.assertEqual("pending", self.outbox.get_entry(entry["id"])["status"])
        bot.handlers.outbound_dispatcher.send_result = Mock(return_value={"status": "sent"})
        results = bot.handlers.outbound_dispatcher.replay_outbox()
        self.assertEqual(2, len(results))
        self.assertTrue(all(result["status"] == "sent" for result in results))

    async def test_cleanup_preserves_config_and_newer_state_is_idempotent(self):
        before = copy.deepcopy(self.settings)
        config = self.root / "config.json"
        config.write_text(json.dumps({"accounts": Config.DEFAULT_ACCOUNTS, "settings": before}))
        cutoff = datetime.now(timezone.utc).isoformat()
        new_job = self.enqueue()
        pdf = self.root / "report.pdf"
        pdf.write_bytes(b"pdf")
        new_entry = self.outbox.spool_delivery("telegram", {"chat_id": "1"}, pdf)
        bot = self.bot()
        for _ in range(2):
            await bot.prepare_clean_start(cutoff)
        self.assertEqual("queued", self.store.get_job(new_job.job_id).status)
        self.assertEqual("pending", self.outbox.get_entry(new_entry["id"])["status"])
        self.assertEqual(before, self.settings)
        self.assertEqual({"accounts": Config.DEFAULT_ACCOUNTS, "settings": before}, json.loads(config.read_text()))
        with patch.object(Config, "CONFIG_FILE", config), patch.object(Config, "_ACCOUNTS", {}):
            Config.load_config()
            self.assertEqual(before["telegram_group_routes"], Config.get_setting("telegram_group_routes"))
            self.assertEqual(before["forced_authors_destination"], Config.get_setting("forced_authors_destination"))
            self.assertEqual(before["forced_authors_editor_nickname"], Config.get_setting("forced_authors_editor_nickname"))

    async def test_startup_cleanup_precedes_client_start(self):
        job = self.enqueue()
        self.store.mark_delivery_pending(job.job_id, "offline")
        bot = self.bot()
        observed = []
        async def start():
            observed.append(self.store.get_job(job.job_id).status)
            raise RuntimeError("No network in unit test")
        bot.manager.clients = {"НИК-1": SimpleNamespace(start=start)}
        bot.initialize = AsyncMock(return_value=True)
        bot.stop_all_clients = AsyncMock()
        with patch("main.ConsoleMenu.setup_before_launch", return_value=True):
            await bot.run_bot()
        self.assertEqual(["abandoned"], observed)

    async def test_old_unfinished_source_older_than_retention_is_still_quarantined(self):
        job = self.enqueue()
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        with self.store._connect() as connection:
            connection.execute("UPDATE jobs SET created_at=? WHERE id=?", (old_date, job.job_id))
        await self.restart()
        self.assertEqual("abandoned", self.store.get_job(job.job_id).status)
        self.assertTrue(self.store.editor_report_conflicts("editor", "900", "диплом.pdf", "new"))

    async def test_restart_tombstone_retention_and_capacity_are_bounded(self):
        self.enqueue()
        self.store.EDITOR_SAFETY_MAX_RECORDS = 1
        await self.restart()
        self.assertTrue(self.store.editor_report_conflicts("other", "1", "other.pdf", "new"))
        future = datetime.now(timezone.utc).timestamp() + self.store.EDITOR_SAFETY_RETENTION + 1
        self.assertFalse(self.store.editor_report_conflicts("other", "1", "other.pdf", "new", future))
        with self.store._connect() as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM editor_report_safety").fetchone()[0])
