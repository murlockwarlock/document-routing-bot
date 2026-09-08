from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from multichannel_gateway.tests import test_forced_author_routing as fixtures


class EditorJobTokenTests(unittest.IsolatedAsyncioTestCase):
    async def assignments(self, ids=(101, 102), **settings):
        tasks = [fixtures.telegram_file_info(f"client-{job}", job, "ДИПЛОМ (копия).docx", message_id=job) for job in ids]
        for job, task in zip(ids, tasks):
            task.update(gateway_job_id=job, force_plagiscan=False)
        config = fixtures.base_settings(
            [task["author"] for task in tasks], forced_authors_destination="editor",
            forced_authors_editor_nickname="@editor", **settings,
        )
        sent = [SimpleNamespace(id=job + 1000, chat=SimpleNamespace(id=900)) for job in ids]
        handlers, nik1, nik2 = await fixtures.TestEditorReportsForFixedRoute._make_handlers_with_tasks(self, tasks, sent, config)
        nik2.name = "НИК-2"
        handlers._mark_gateway_job_done = AsyncMock()
        handlers._get_editor_safety_store = lambda: (_ for _ in ()).throw(AssertionError("token path accessed basename history"))
        return handlers, nik1, nik2

    def response(self, job=101, ai=False, reply=None, sender="editor", chat=900, incoming_id=None, name=None):
        message = SimpleNamespace(
            id=incoming_id or job * 10 + int(ai), text=None, reply_to_message_id=reply,
            from_user=SimpleNamespace(username=sender, id=900), chat=SimpleNamespace(id=chat),
            document=SimpleNamespace(file_name=name or f"{'ИИ ' if ai else ''}ДИПЛОМ (копия)__job{job}.pdf"),
        )
        async def download(path):
            Path(path).write_bytes(f"{job}:{ai}".encode())
            return path
        message.download = AsyncMock(side_effect=download)
        return message

    def test_pdf_token_parser(self):
        parse = fixtures.BotHandlers._editor_job_token
        for name in ("ДИПЛОМ__job82.pdf", "ИИ ДИПЛОМ (копия)__job82.pdf", "my job notes__job82.PDF"):
            self.assertEqual(82, parse(name))
        for name in ("job82.pdf", "ДИПЛОМ__job0.pdf", "ДИПЛОМ__job-82.pdf", "ДИПЛОМ__job82 copy.pdf",
                     "ДИПЛОМ__job82.pdf.bak", "ДИПЛОМ__job82.docx", "ДИПЛОМ__jobx.pdf", "ДИПЛОМ__job01.pdf"):
            self.assertIsNone(parse(name), name)

    async def test_parallel_same_names_normal_and_ai_no_reply_exact_origins(self):
        handlers, nik1, nik2 = await self.assignments()
        self.assertEqual(["ДИПЛОМ (копия)__job101.docx", "ДИПЛОМ (копия)__job102.docx"],
                         [call.kwargs["file_name"] for call in nik2.send_document.await_args_list])
        self.assertEqual(2, len(handlers.editor_tracking))
        initial_count = handlers.files_today["count"]
        for index, (job, ai) in enumerate(((102, False), (101, False), (101, True), (102, True)), 1):
            message = self.response(job, ai)
            await handlers.handle_editor_response(nik2, message)
            self.assertEqual(index, nik1.send_document.await_count)
            kwargs = nik1.send_document.await_args.kwargs
            self.assertEqual(job, kwargs["chat_id"])
            self.assertEqual(("ИИ " if ai else "") + "ДИПЛОМ (копия).pdf", kwargs["file_name"])
            self.assertNotIn("__job", kwargs["file_name"])
            self.assertIsNone(message.reply_to_message_id)
        self.assertEqual({}, handlers.editor_tracking)
        self.assertEqual(initial_count, handlers.files_today["count"])
        handlers.processor.crop_pdf.assert_not_called()

    async def test_reply_conflicts_unknown_malformed_and_wrong_sender_fail_closed(self):
        handlers, nik1, nik2 = await self.assignments()
        for message in (
            self.response(reply=1102), self.response(999, reply=1101), self.response(sender="intruder"),
            self.response(chat=901), self.response(reply=999), self.response(name="other__job101.pdf"),
            self.response(name="ДИПЛОМ (копия)__jobx.pdf", reply=1101),
            self.response(name="ДИПЛОМ (копия)__JOB101.pdf", reply=1101),
            self.response(name="ДИПЛОМ (копия).pdf"),
        ):
            await handlers.handle_editor_response(nik2, message)
            message.download.assert_not_awaited()
        nik1.send_document.assert_not_awaited()
        await handlers.handle_editor_response(nik2, self.response(reply=1101))
        nik1.send_document.assert_awaited_once()
        self.assertEqual(101, nik1.send_document.await_args.kwargs["chat_id"])

    async def test_first_ai_immediate_then_normal_at_twenty_minutes(self):
        handlers, nik1, nik2 = await self.assignments((101,))
        info = next(iter(handlers.editor_tracking.values()))
        await handlers.handle_editor_response(nik2, self.response(ai=True))
        nik1.send_document.assert_awaited_once()
        self.assertTrue(info["ai_report_delivered"])
        self.assertFalse(info["normal_report_delivered"])
        first = info["first_report_at"]
        with patch("bot_handlers.datetime", wraps=datetime) as clock:
            clock.now.return_value = first + timedelta(minutes=20)
            await handlers.handle_editor_response(nik2, self.response())
        self.assertEqual(2, nik1.send_document.await_count)
        self.assertEqual("closed", info["lifecycle"])
        self.assertEqual({}, handlers.editor_tracking)
        self.assertEqual(first, info["first_report_at"])

    async def test_first_normal_expires_at_thirty_minutes_without_blocking_other_job(self):
        handlers, nik1, nik2 = await self.assignments()
        info = next(info for info in handlers.editor_tracking.values() if info["editor_job_id"] == 101)
        await handlers.handle_editor_response(nik2, self.response())
        self.assertEqual(1, nik1.send_document.await_count)
        first = info["first_report_at"]
        handlers._expire_editor_assignments(first + timedelta(minutes=29, seconds=59))
        self.assertEqual(2, len(handlers.editor_tracking))
        with patch("bot_handlers.datetime", wraps=datetime) as clock:
            clock.now.return_value = first + timedelta(minutes=30)
            await handlers.handle_editor_response(nik2, self.response(ai=True))
            await handlers.handle_editor_response(nik2, self.response(102))
        self.assertEqual(2, nik1.send_document.await_count)
        self.assertEqual(102, nik1.send_document.await_args.kwargs["chat_id"])
        self.assertEqual("closed", info["lifecycle"])
        self.assertFalse(info["ai_report_delivered"])
        self.assertTrue(info["account_released"])

    async def test_same_incoming_concurrent_delivery_once_other_report_allowed(self):
        handlers, nik1, nik2 = await self.assignments((101,))
        message = self.response()
        await asyncio.gather(*(handlers.handle_editor_response(nik2, message) for _ in range(3)))
        nik1.send_document.assert_awaited_once()
        await handlers.handle_editor_response(nik2, self.response(ai=True))
        self.assertEqual(2, nik1.send_document.await_count)

    async def test_spooled_delivery_releases_claim_and_does_not_start_window_or_done(self):
        handlers, nik1, nik2 = await self.assignments((101,))
        handlers._deliver_document_to_origin = AsyncMock(side_effect=[{"status": "spooled"}, {"status": "sent"}])
        info = next(iter(handlers.editor_tracking.values()))
        message = self.response()
        await handlers.handle_editor_response(nik2, message)
        self.assertFalse(info["normal_report_delivered"])
        self.assertIsNone(info["first_report_at"])
        self.assertEqual("waiting_first_report", info["lifecycle"])
        handlers._mark_gateway_job_done.assert_not_awaited()
        self.assertFalse(handlers._editor_response_was_claimed(message))
        await handlers.handle_editor_response(nik2, message)
        self.assertTrue(info["normal_report_delivered"])
        handlers._mark_gateway_job_done.assert_awaited_once()

    async def test_legacy_plain_explicit_reply_allowed_but_plain_no_reply_not_guessed(self):
        handlers, nik1, nik2 = await self.assignments((101,))
        await handlers.handle_editor_response(nik2, self.response(name="ДИПЛОМ (копия).pdf"))
        nik1.send_document.assert_not_awaited()
        await handlers.handle_editor_response(nik2, self.response(name="ДИПЛОМ (копия).pdf", reply=1101))
        nik1.send_document.assert_awaited_once()

    async def test_original_user_job_word_restored_from_metadata(self):
        handlers = fixtures.make_handlers()
        client = SimpleNamespace(send_document=AsyncMock())
        source = {"original_file_name": "my job__job82 (копия).docx", "gateway_job_id": 105}
        await handlers._send_human_editor_document(client, "editor", "unused", source)
        self.assertEqual("my job__job82 (копия)__job105.docx", client.send_document.await_args.kwargs["file_name"])
        info = {}
        handlers._attach_editor_assignment(info, source)
        self.assertEqual("my job__job82 (копия).pdf", handlers._editor_client_report_name(info, info["expected_pdf_name"]))

    async def test_all_human_editor_send_paths_use_transport_names(self):
        import tempfile
        cases = ("ordinary", "mode2_work", "mode2_247", "anti_editor", "direct247", "aaa_error")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                manager = fixtures.make_manager()
                nik2 = SimpleNamespace(name="НИК-2", send_document=AsyncMock(
                    return_value=SimpleNamespace(id=1101, chat=SimpleNamespace(id=900)),
                ))
                manager.get_client.side_effect = lambda name: nik2 if name == "НИК-2" else None
                handlers = fixtures.make_handlers(manager)
                source = fixtures.telegram_file_info("client", 101, "ДИПЛОМ (копия).docx")
                source.update(gateway_job_id=101, force_plagiscan=False)
                path = Path(directory) / "original.docx"
                path.write_bytes(b"source")
                source["temp_path"] = str(path)
                handlers._ensure_work_file = AsyncMock(return_value=str(path))
                handlers._mark_gateway_job_waiting_editor = AsyncMock()
                settings = fixtures.base_settings([], anti_destination="редактор", anti_editor_nickname="@editor")
                if case.startswith("mode2"):
                    settings["mode"] = "mode2"
                with patch("bot_handlers.Config.get_setting", side_effect=fixtures.settings_lookup(settings)):
                    if case == "anti_editor":
                        source["is_anti"] = True
                        await handlers.process_anti_file(source)
                    elif case == "direct247":
                        await handlers.send_to_24_7_editor(None, source)
                    elif case == "aaa_error":
                        await handlers.handle_aaa_error_message(SimpleNamespace(name="НИК-1"), None, source, "old")
                    else:
                        await handlers.send_to_nik2(source, reason="вне рабочего времени" if case == "mode2_247" else "")
                nik2.send_document.assert_awaited_once()
                self.assertEqual("ДИПЛОМ (копия)__job101.docx", nik2.send_document.await_args.kwargs["file_name"])
                info = next(iter(handlers.editor_tracking.values()))
                self.assertEqual("ДИПЛОМ (копия).docx", info["original_file_name"])
                self.assertEqual(101, info["editor_job_id"])
                self.assertTrue(info["account_released"])
                manager.mark_account_busy.assert_not_called()

    async def test_nik1_editor247_token_result_uses_main_handler_and_original_name(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            manager = fixtures.make_manager()
            nik1 = SimpleNamespace(name="НИК-1", send_document=AsyncMock(
                return_value=SimpleNamespace(id=1101, chat=SimpleNamespace(id=900)),
            ))
            manager.get_client.side_effect = lambda name: nik1 if name == "НИК-1" else None
            handlers = fixtures.make_handlers(manager)
            source = fixtures.telegram_file_info("client", 101, "ДИПЛОМ (копия).docx")
            source["gateway_job_id"] = 101
            path = Path(directory) / "source.docx"
            path.write_bytes(b"source")
            handlers._ensure_work_file = AsyncMock(return_value=str(path))
            handlers._mark_gateway_job_waiting_editor = AsyncMock()
            handlers._mark_gateway_job_done = AsyncMock()
            handlers._deliver_document_to_origin = AsyncMock(return_value={"status": "sent"})
            with patch("bot_handlers.Config.get_setting", side_effect=fixtures.settings_lookup(fixtures.base_settings([], editor_24_7="@editor"))):
                await handlers.send_to_24_7_editor(None, source)
                await handlers.handle_main_account(nik1, self.response())
            handlers._deliver_document_to_origin.assert_awaited_once()
            self.assertEqual("ДИПЛОМ (копия).pdf", handlers._deliver_document_to_origin.await_args.kwargs["file_name"])
            self.assertEqual(1, nik1.send_document.await_count)

    async def test_two_different_report_ids_can_be_processed_concurrently(self):
        handlers, nik1, nik2 = await self.assignments((101,))
        await asyncio.gather(handlers.handle_editor_response(nik2, self.response()),
                             handlers.handle_editor_response(nik2, self.response(ai=True)))
        self.assertEqual(2, nik1.send_document.await_count)
        self.assertEqual({}, handlers.editor_tracking)

    async def test_delayed_ai_at_twenty_minutes_delivered_on_arrival(self):
        handlers, nik1, nik2 = await self.assignments((101,))
        info = next(iter(handlers.editor_tracking.values()))
        await handlers.handle_editor_response(nik2, self.response())
        nik1.send_document.assert_awaited_once()
        with patch("bot_handlers.datetime", wraps=datetime) as clock:
            clock.now.return_value = info["first_report_at"] + timedelta(minutes=20)
            await handlers.handle_editor_response(nik2, self.response(ai=True))
        self.assertEqual(2, nik1.send_document.await_count)
        self.assertEqual("ИИ ДИПЛОМ (копия).pdf", nik1.send_document.await_args.kwargs["file_name"])
        self.assertEqual({}, handlers.editor_tracking)
