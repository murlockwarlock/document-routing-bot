from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pymupdf

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

    def _plagiscan_success_with_url(self, info, *, ai=False):
        message = self.response("unused")
        message.document = None
        message.text = "✅ Ваш файл успешно проверен!\nОригинальность: 75%"
        main = SimpleNamespace(text="Посмотреть отчет", url="https://example.com/main.pdf")
        buttons = [main]
        if ai:
            message.text += "\nМашинная генерация: 20%"
            buttons.append(SimpleNamespace(text="Посмотреть отчет по ИИ", url="https://example.com/ai.pdf"))
        message.reply_markup = SimpleNamespace(inline_keyboard=[buttons])
        self.h.current_processing_files = {"B": info}
        self.h.message_to_file_map = {"900_11": "B"}
        return message

    async def test_ai_url_report_is_delivered_without_crop_and_with_original_bytes(self):
        info = self.anti_info()
        info.update(main_report_delivered=True, awaiting_ai_report=True, force_plagiscan=True)
        message = self._plagiscan_success_with_url(info, ai=True)
        source = b"AI report bytes that must not be changed"
        downloaded_paths = []

        def download(_url, path):
            downloaded_paths.append(path)
            Path(path).write_bytes(source)
            return True

        async def deliver(_info, path, **_kwargs):
            self.assertTrue(Path(path).exists())
            self.assertEqual(source, Path(path).read_bytes())
            return {"status": "sent"}

        self.h.processor.download_pdf = download
        self.h.processor.crop_pdf = Mock(side_effect=AssertionError("crop_pdf must not be called for AI report"))
        self.h.processor.cleanup_temp_files = Mock()
        self.h._deliver_document_to_origin = AsyncMock(side_effect=deliver)

        await self.h.handle_anti_bot_response(self.client, message)

        self.h.processor.crop_pdf.assert_not_called()
        self.assertTrue(info["ai_report_delivered"])
        self.assertTrue(info["main_report_delivered"])
        delivery = self.h._deliver_document_to_origin.await_args
        self.assertEqual("ИИ ДИПЛОМ.pdf", delivery.kwargs["file_name"])
        self.assertEqual(source, Path(delivery.args[1]).read_bytes())
        self.assertFalse(Path(downloaded_paths[0]).exists())

    async def test_main_url_report_still_uses_crop(self):
        info = self.anti_info()
        message = self._plagiscan_success_with_url(info)
        source = b"main source"
        cropped = b"main cropped"

        def download(_url, path):
            Path(path).write_bytes(source)
            return True

        def crop(input_path, output_path):
            Path(output_path).write_bytes(Path(input_path).read_bytes() + b" + " + cropped)
            return True

        self.h.processor.download_pdf = download
        self.h.processor.crop_pdf = Mock(side_effect=crop)
        self.h.processor.cleanup_temp_files = Mock()

        await self.h.handle_anti_bot_response(self.client, message)

        self.h.processor.crop_pdf.assert_called_once()
        delivery = self.h._deliver_document_to_origin.await_args
        self.assertEqual("ДИПЛОМ.pdf", delivery.kwargs["file_name"])
        self.assertEqual(source + b" + " + cropped, Path(delivery.args[1]).read_bytes())
        self.assertTrue(info["main_report_delivered"])
        self.assertFalse(info.get("ai_report_delivered", False))

    async def test_ai_direct_pdf_is_delivered_without_crop(self):
        info = self.anti_info()
        info.update(main_report_delivered=True, awaiting_ai_report=True, force_plagiscan=True)
        message = self.response("ИИ ДИПЛОМ.pdf")
        source = b"direct AI bytes"
        downloaded_paths = []

        async def download(path):
            downloaded_paths.append(path)
            Path(path).write_bytes(source)
            return path

        async def deliver(_info, path, **_kwargs):
            self.assertTrue(Path(path).exists())
            self.assertEqual(source, Path(path).read_bytes())
            return {"status": "sent"}

        message.download = download
        self.h.current_processing_files = {"B": info}
        self.h.message_to_file_map = {"900_11": "B"}
        self.h.processor.crop_pdf = Mock(side_effect=AssertionError("crop_pdf must not be called for AI report"))
        self.h.processor.cleanup_temp_files = Mock()
        self.h._deliver_document_to_origin = AsyncMock(side_effect=deliver)

        await self.h.handle_anti_bot_response(self.client, message)

        self.h.processor.crop_pdf.assert_not_called()
        self.assertTrue(info["ai_report_delivered"])
        delivery = self.h._deliver_document_to_origin.await_args
        self.assertEqual("ИИ ДИПЛОМ.pdf", delivery.kwargs["file_name"])
        self.assertEqual(source, Path(delivery.args[1]).read_bytes())
        self.assertFalse(Path(downloaded_paths[0]).exists())

    async def test_ai_direct_pdf_keeps_valid_pdf_bytes_and_geometry(self):
        info = self.anti_info()
        info.update(main_report_delivered=True, awaiting_ai_report=True, force_plagiscan=True)
        source_path = self.root / "ai-source.pdf"
        document = pymupdf.open()
        page = document.new_page(width=400, height=600)
        page.insert_text((20, 70), "AI REPORT HEADER")
        page.insert_text((20, 120), "IMPORTANT AI TEXT AT TOP")
        page.insert_text((20, 300), "SECOND LINE")
        document.save(source_path)
        document.close()
        source_bytes = source_path.read_bytes()
        message = self.response("ИИ ДИПЛОМ.pdf")

        async def download(path):
            Path(path).write_bytes(source_bytes)
            return path

        async def deliver(_info, path, **_kwargs):
            return {"status": "sent"}

        message.download = download
        self.h.current_processing_files = {"B": info}
        self.h.message_to_file_map = {"900_11": "B"}
        self.h.processor.crop_pdf = Mock(side_effect=AssertionError("crop_pdf must not be called for AI report"))
        self.h.processor.cleanup_temp_files = Mock()
        self.h._deliver_document_to_origin = AsyncMock(side_effect=deliver)

        await self.h.handle_anti_bot_response(self.client, message)

        delivered_path = self.h._deliver_document_to_origin.await_args.args[1]
        self.assertEqual(source_bytes, Path(delivered_path).read_bytes())
        delivered = pymupdf.open(delivered_path)
        original = pymupdf.open(source_path)
        self.assertEqual(len(original), len(delivered))
        self.assertEqual(original[0].rect.width, delivered[0].rect.width)
        self.assertEqual(original[0].rect.height, delivered[0].rect.height)
        self.assertIn("IMPORTANT AI TEXT AT TOP", delivered[0].get_text())
        delivered.close()
        original.close()

    async def test_main_direct_pdf_still_uses_crop(self):
        info = self.anti_info()
        message = self.response("ДИПЛОМ.pdf")
        source = b"direct main source"
        cropped = b"direct main cropped"

        async def download(path):
            Path(path).write_bytes(source)
            return path

        def crop(input_path, output_path):
            Path(output_path).write_bytes(Path(input_path).read_bytes() + cropped)
            return True

        message.download = download
        self.h.current_processing_files = {"B": info}
        self.h.message_to_file_map = {"900_11": "B"}
        self.h.processor.crop_pdf = Mock(side_effect=crop)
        self.h.processor.cleanup_temp_files = Mock()

        await self.h.handle_anti_bot_response(self.client, message)

        self.h.processor.crop_pdf.assert_called_once()
        delivery = self.h._deliver_document_to_origin.await_args
        self.assertEqual("ДИПЛОМ.pdf", delivery.kwargs["file_name"])
        self.assertEqual(source + cropped, Path(delivery.args[1]).read_bytes())
        self.assertTrue(info["main_report_delivered"])

    def test_main_crop_pdf_reduces_first_page(self):
        import bot_handlers

        source_path = self.root / "main-source.pdf"
        cropped_path = self.root / "main-cropped.pdf"
        document = pymupdf.open()
        page = document.new_page(width=400, height=600)
        page.insert_text((20, 70), "HEADER")
        page.insert_text((20, 250), "RESULTS")
        page.insert_text((20, 350), "BODY")
        document.save(source_path)
        document.close()

        with patch.object(bot_handlers, "fitz", pymupdf):
            self.assertTrue(self.h.processor.crop_pdf(str(source_path), str(cropped_path)))

        original = pymupdf.open(source_path)
        cropped = pymupdf.open(cropped_path)
        self.assertLess(cropped[0].rect.height, original[0].rect.height)
        self.assertIn("BODY", cropped[0].get_text())
        original.close()
        cropped.close()

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
