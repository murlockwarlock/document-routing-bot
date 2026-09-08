from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from multichannel_gateway.tests import test_forced_author_routing as fixtures
from multichannel_gateway.core.outbox import LocalOutboxSpool
from multichannel_gateway.core.storage import SqliteJobStore


class ResultSafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.h = fixtures.make_handlers()
        self.h._editor_safety_store = SqliteJobStore(self.root / "jobs.sqlite3")
        self.h.process_queue = AsyncMock()
        self.h._deliver_document_to_origin = AsyncMock(return_value={"status": "sent"})
        self.h._mark_gateway_job_done = AsyncMock()
        self.h._mark_gateway_job_delivery_pending = AsyncMock()
        self.h._finish_processing = Mock()
        self.h._log_plagiscan = Mock()
        self.h._build_result_file_path = lambda name: str(self.root / Path(name).name)
        self.client = SimpleNamespace(name="НИК-2")
        self.h.manager.get_client.return_value = SimpleNamespace(name="НИК-1")

    def tracking(self, reply_id):
        return dict(
            author="client", author_id=reply_id, original_name="ДИПЛОМ.docx",
            expected_pdf_name="ДИПЛОМ.pdf", expected_ai_pdf_name="ИИ ДИПЛОМ.pdf",
            destination="@editor", chat_id=900, reply_to_message_id=reply_id,
            sent_from_account="НИК-2", fixed_author_editor_route=True,
            delivered_reports=set(), sent_at=datetime.now(), file_uid=f"source:{reply_id}",
            source_platform="telegram", route_chat_id=reply_id, route_message_id=1,
        )

    def response(self, name, reply=11):
        async def download(path):
            Path(path).write_bytes(b"PDF")
            return path
        return SimpleNamespace(
            id=700, reply_to_message_id=reply, chat=SimpleNamespace(id=900),
            from_user=SimpleNamespace(username="editor", id=900), text=None,
            document=SimpleNamespace(file_name=name, mime_type="application/pdf"),
            download=download,
        )

    async def test_legacy_human_reports_ignore_persisted_history_without_writing_it(self):
        store = self.h._editor_safety_store
        for report in ("ДИПЛОМ.pdf", "ИИ ДИПЛОМ.pdf"):
            store.remember_editor_report("editor", "900", self.h._editor_report_key(report), "old")
            store.remember_editor_report("*", "*", self.h._editor_report_key(report), "restart")
        reloaded = SqliteJobStore(store.db_path)
        self.h._editor_safety_store = reloaded
        self.h.editor_tracking = {"B": self.tracking(12)}
        with patch.object(reloaded, "editor_report_conflicts", side_effect=AssertionError("human history read")), patch.object(
            reloaded, "remember_editor_report", side_effect=AssertionError("human history write"),
        ):
            for index, report in enumerate(("ДИПЛОМ.pdf", "ИИ ДИПЛОМ.pdf")):
                message = self.response(report, None)
                message.id += index
                await self.h.handle_editor_response(self.client, message)
                self.assertEqual(index + 1, self.h._deliver_document_to_origin.await_count)
                self.assertEqual(12, self.h._deliver_document_to_origin.await_args.args[0]["route_chat_id"])

    def test_history_retention_is_independent_and_bounded(self):
        store = self.h._editor_safety_store
        now = time.time()
        store.remember_editor_report("editor", "900", "диплом.pdf", "A", now - 25 * 3600)
        self.assertTrue(store.editor_report_conflicts("editor", "900", "диплом.pdf", "B", now))
        self.assertFalse(store.editor_report_conflicts("editor", "900", "диплом.pdf", "B", now + store.EDITOR_SAFETY_RETENTION))
        with store._connect() as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM editor_report_safety").fetchone()[0])
        store.EDITOR_SAFETY_MAX_RECORDS = 1
        store.remember_editor_report("editor", "900", "one.pdf", "A", now)
        store.remember_editor_report("editor", "900", "two.pdf", "B", now)
        reloaded = SqliteJobStore(store.db_path)
        self.assertTrue(reloaded.editor_report_conflicts("other", "901", "three.pdf", "C", now))
        with store._connect() as connection:
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM editor_report_safety").fetchone()[0])

    def test_history_separates_editor_chat_filename_and_same_task(self):
        store = self.h._editor_safety_store
        store.remember_editor_report("editor", "900", "диплом.pdf", "A")
        for editor, chat, report, task in (
            ("editor", "901", "диплом.pdf", "B"),
            ("other", "900", "диплом.pdf", "B"),
            ("editor", "900", "другая.pdf", "B"),
            ("editor", "900", "диплом.pdf", "A"),
        ):
            with self.subTest(editor=editor, chat=chat, report=report, task=task):
                self.assertFalse(store.editor_report_conflicts(editor, chat, report, task))

    def test_unavailable_history_does_not_block_legacy_human_correlation(self):
        self.h.editor_tracking = {"B": self.tracking(12)}
        corrupt = self.root / "corrupt.sqlite3"
        corrupt.write_bytes(b"invalid database")
        self.h._editor_safety_store = None
        with patch("bot_handlers.build_store", side_effect=lambda: SqliteJobStore(corrupt)):
            self.assertEqual("B", self.h.find_editor_tracking_for_unreplied_pdf("editor", "ДИПЛОМ.pdf", reply_chat_id=900)[0])

    def anti_info(self):
        return dict(
            author="client", original_file_name="ДИПЛОМ.docx", account="НИК-2", bot_type="anti",
            file_uid="telegram:42:1:0", source_platform="telegram", route_chat_id=42,
            gateway_job_id=1,
        )

    async def test_plagiscan_unknown_reply_never_delivers_to_active_task(self):
        self.h.current_processing_files = {"B": self.anti_info()}
        self.h.message_to_file_map = {"900_11": "removed-A"}
        await self.h.handle_anti_bot_response(self.client, self.response("ДИПЛОМ.pdf"))
        self.h._deliver_document_to_origin.assert_not_awaited()
        self.assertIn("B", self.h.current_processing_files)
        self.h._log_plagiscan.assert_any_call("Plagiscan correlation stopped: unknown or stale Plagiscan reply; correlation stopped")

    def test_plagiscan_safe_correlation_matrix(self):
        self.h.current_processing_files = {"B": self.anti_info()}
        self.h.message_to_file_map = {"900_12": "B"}
        self.assertEqual("B", self.h.find_anti_processing_for_response("НИК-2", self.response("result.pdf", 12))[0])
        self.assertEqual("B", self.h.find_anti_processing_for_response("НИК-2", self.response("ДИПЛОМ.pdf", None))[0])
        self.assertIsNone(self.h.find_anti_processing_for_response("НИК-2", self.response("unknown.pdf", None))[0])
        self.h.current_processing_files["A"] = self.anti_info()
        self.assertIsNone(self.h.find_anti_processing_for_response("НИК-2", self.response("ДИПЛОМ.pdf", None))[0])
        status = SimpleNamespace(document=None, text="У вас закончились проверки", reply_to_message_id=None)
        self.assertEqual("A", self.h.find_anti_processing_for_response("НИК-2", status)[0])
        status.text = "Ваш файл успешно проверен!"
        self.assertIsNone(self.h.find_anti_processing_for_response("НИК-2", status)[0])

    async def test_plagiscan_delivery_paths_and_retry(self):
        for path in ("url", "uncropped", "pdf", "ai_url", "ai_uncropped", "ai_pdf"):
            for outcome in ("sent", "spooled"):
                with self.subTest(path=path, outcome=outcome):
                    info = self.anti_info()
                    self.h.current_processing_files = {"B": info}
                    self.h.message_to_file_map = {"900_11": "B"}
                    self.h._deliver_document_to_origin.reset_mock()
                    self.h._deliver_document_to_origin.return_value = {"status": outcome}
                    self.h._mark_gateway_job_done.reset_mock()
                    self.h._mark_gateway_job_delivery_pending.reset_mock()
                    self.h._finish_processing.reset_mock()
                    self.h.manager.mark_account_free.reset_mock()
                    self.h._log_plagiscan.reset_mock()
                    message = self.response("ДИПЛОМ.pdf")
                    if path.startswith("ai"):
                        info.update(main_report_delivered=True, awaiting_ai_report=True, force_plagiscan=True)
                    if path not in ("pdf", "ai_pdf"):
                        message.document = None
                        message.text = "Ваш файл успешно проверен! Оригинальность: 75% Машинная генерация: 20%"
                        main = SimpleNamespace(text="Посмотреть отчет", url="https://example.com/main.pdf")
                        ai = SimpleNamespace(text="Посмотреть отчет по ИИ", url="https://example.com/ai.pdf")
                        message.reply_markup = SimpleNamespace(inline_keyboard=[[main, ai]])
                    def crop(source, target):
                        if path.endswith("uncropped"):
                            return False
                        Path(target).write_bytes(Path(source).read_bytes())
                        return True
                    def download(url, target):
                        Path(target).write_bytes(b"pdf")
                        return True
                    self.h.processor.crop_pdf = crop
                    self.h.processor.download_pdf = download
                    self.h.processor.cleanup_temp_files = Mock()
                    await self.h.handle_anti_bot_response(self.client, message)
                    flag = "ai_report_delivered" if path.startswith("ai") else "main_report_delivered"
                    if outcome == "spooled":
                        self.assertFalse(info.get(flag, False))
                        self.assertTrue(info["outbound_pending"])
                        self.h._mark_gateway_job_done.assert_not_awaited()
                        self.h._mark_gateway_job_delivery_pending.assert_awaited_once()
                        self.h._finish_processing.assert_not_called()
                        self.assertIn("B", self.h.current_processing_files)
                        self.assertFalse(any("доставлен" in str(c) or "отправлен автору" in str(c) for c in self.h._log_plagiscan.call_args_list))
                        self.h._deliver_document_to_origin.return_value = {"status": "sent"}
                        await self.h.handle_anti_bot_response(self.client, message)
                    self.assertTrue(info[flag])
                    self.h._mark_gateway_job_done.assert_awaited_once()
                    self.assertNotIn("B", self.h.current_processing_files)
                    self.h.manager.mark_account_free.assert_called_once()

    async def test_main_sent_ai_spooled_retries_only_ai(self):
        info = self.anti_info()
        info["force_plagiscan"] = True
        self.h.current_processing_files = {"B": info}
        self.h.message_to_file_map = {"900_11": "B"}
        self.h._deliver_document_to_origin.side_effect = [
            {"status": "sent"}, {"status": "spooled"}, {"status": "sent"},
        ]
        message = self.response("unused")
        message.document = None
        message.text = "Ваш файл успешно проверен! Оригинальность: 75% Машинная генерация: 20%"
        message.reply_markup = SimpleNamespace(inline_keyboard=[[
            SimpleNamespace(text="Посмотреть отчет", url="https://example.com/main.pdf"),
            SimpleNamespace(text="Посмотреть отчет по ИИ", url="https://example.com/ai.pdf"),
        ]])
        def write_pdf(source, path):
            Path(path).write_bytes(b"pdf")
            return True
        self.h.processor.download_pdf = write_pdf
        self.h.processor.crop_pdf = write_pdf
        self.h.processor.cleanup_temp_files = Mock()
        await self.h.handle_anti_bot_response(self.client, message)
        self.assertTrue(info["main_report_delivered"])
        self.assertFalse(info.get("ai_report_delivered", False))
        self.h._mark_gateway_job_done.assert_not_awaited()
        await self.h.handle_anti_bot_response(self.client, message)
        self.assertEqual(
            ["ДИПЛОМ.pdf", "ИИ ДИПЛОМ.pdf", "ИИ ДИПЛОМ.pdf"],
            [item.kwargs["file_name"] for item in self.h._deliver_document_to_origin.await_args_list],
        )
        self.h._mark_gateway_job_done.assert_awaited_once()
        self.h.manager.mark_account_free.assert_called_once()

    async def test_vk_spool_acceptance_is_preserved(self):
        info = self.anti_info()
        info["source_platform"] = "vk"
        self.h._deliver_document_to_origin.return_value = {"status": "spooled"}
        self.assertTrue(await self.h._deliver_anti_result(self.client, info, "file.pdf", "file.pdf", None))
        self.assertNotIn("outbound_pending", info)

    async def test_telegram_spool_dedupes_and_retry_acknowledges_entry(self):
        h = fixtures.make_handlers()
        outbox = LocalOutboxSpool(self.root / "outbox")
        h.outbound_dispatcher.outbox = outbox
        client = SimpleNamespace(is_connected=True, send_document=AsyncMock(side_effect=RuntimeError("offline")))
        document = self.root / "report.pdf"
        document.write_bytes(b"pdf")
        context = self.anti_info()
        with patch("bot_handlers.asyncio.sleep", new=AsyncMock()):
            for _ in range(3):
                result = await h._deliver_document_to_origin(context, str(document), "report.pdf", telegram_client=client)
                self.assertEqual("spooled", result["status"])
        self.assertEqual(1, len(outbox.list_entries()))
        self.assertEqual(1, len(list(outbox.files_dir.iterdir())))
        client.send_document.side_effect = None
        await h._deliver_document_to_origin(context, str(document), "report.pdf", telegram_client=client)
        self.assertEqual([], outbox.list_entries(pending_only=True))
