from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from multichannel_gateway.core.files import AtomicFileStore
from multichannel_gateway.core.models import AttachmentRef, InboundEnvelope
from multichannel_gateway.core.storage import SqliteJobStore


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.store = SqliteJobStore(self.base / "jobs.sqlite3")
        self.files = AtomicFileStore(self.base / "inbox")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _enqueue(self, source: str = "vk", file_name: str = "test.docx"):
        envelope = InboundEnvelope(
            source=source,
            event_id=f"{source}:1:1",
            chat_id="1",
            sender_id="42",
            sender_name="tester",
            message_id="1",
            attachments=(AttachmentRef(index=0, file_name=file_name),),
        )
        saved = self.files.persist_bytes(f"{source}:1:1:0", file_name, b"payload")
        return self.store.enqueue_file(envelope, envelope.attachments[0], saved)

    def test_enqueue_and_claim_bridgeable(self) -> None:
        job = self._enqueue(source="vk")
        claimed = self.store.claim_next_bridgeable("worker", ("vk", "max"))
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(job.job_id, claimed.job_id)
        self.assertEqual("processing", claimed.status)

    def test_release_stale_processing(self) -> None:
        self._enqueue(source="vk")
        claimed = self.store.claim_next_bridgeable("worker", ("vk", "max"))
        self.assertIsNotNone(claimed)
        recovered = self.store.release_stale_processing(older_than_seconds=0, sources=("vk", "max"))
        self.assertEqual(1, recovered)
        stats = self.store.get_stats()
        self.assertEqual(1, stats["queued"])
        self.assertEqual(0, stats["processing"])

    def test_stats_include_total(self) -> None:
        self._enqueue(source="vk")
        self._enqueue(source="max", file_name="second.docx")
        stats = self.store.get_stats()
        self.assertEqual(2, stats["queued"])
        self.assertEqual(2, stats["total"])

    def test_mark_pending_before_done_skips_only_old_vk_max_jobs(self) -> None:
        old_vk = self._enqueue(source="vk", file_name="old_vk.docx")
        old_max = self._enqueue(source="max", file_name="old_max.docx")
        old_tg = self._enqueue(source="telegram", file_name="old_tg.docx")

        skipped = self.store.mark_pending_before_done("2999-01-01T00:00:00", sources=("vk", "max"))

        self.assertEqual(2, skipped)
        self.assertEqual("done", self.store.get_by_dedupe_key(old_vk.dedupe_key).status)
        self.assertEqual("done", self.store.get_by_dedupe_key(old_max.dedupe_key).status)
        self.assertEqual("queued", self.store.get_by_dedupe_key(old_tg.dedupe_key).status)

    def test_mark_done_sets_status_and_result_path(self) -> None:
        job = self._enqueue(source="vk")
        claimed = self.store.claim_next_bridgeable("worker", ("vk",))
        self.assertIsNotNone(claimed)

        self.store.mark_done(job.job_id, result_path="/tmp/result.pdf", note="ok")

        updated = self.store.get_by_dedupe_key(job.dedupe_key)
        self.assertEqual("done", updated.status)
        self.assertEqual("/tmp/result.pdf", updated.result_path)
        self.assertIsNone(updated.locked_by)

    def test_mark_failed_without_retry_sets_status_failed(self) -> None:
        job = self._enqueue(source="vk")
        claimed = self.store.claim_next_bridgeable("worker", ("vk",))
        self.assertIsNotNone(claimed)

        self.store.mark_failed(job.job_id, "test error")

        updated = self.store.get_by_dedupe_key(job.dedupe_key)
        self.assertEqual("failed", updated.status)
        self.assertEqual("test error", updated.last_error)
        self.assertIsNone(updated.locked_by)

    def test_mark_failed_with_retry_requeues_as_queued(self) -> None:
        job = self._enqueue(source="vk")
        claimed = self.store.claim_next_bridgeable("worker", ("vk",))
        self.assertIsNotNone(claimed)

        self.store.mark_failed(job.job_id, "retriable error", retry_delay_seconds=60)

        updated = self.store.get_by_dedupe_key(job.dedupe_key)
        self.assertEqual("queued", updated.status)
        self.assertEqual("retriable error", updated.last_error)
        self.assertIsNone(updated.locked_by)

    def test_mark_waiting_editor_sets_status(self) -> None:
        job = self._enqueue(source="vk")
        claimed = self.store.claim_next_bridgeable("worker", ("vk",))
        self.assertIsNotNone(claimed)

        self.store.mark_waiting_editor(job.job_id, note="sent to editor")

        updated = self.store.get_by_dedupe_key(job.dedupe_key)
        self.assertEqual("awaiting_editor", updated.status)
        self.assertEqual("sent to editor", updated.last_error)
        self.assertIsNone(updated.locked_by)

    def test_dedupe_prevents_duplicate_enqueue(self) -> None:
        envelope = InboundEnvelope(
            source="vk",
            event_id="vk:1:1",
            chat_id="1",
            sender_id="42",
            sender_name="tester",
            message_id="1",
            attachments=(AttachmentRef(index=0, file_name="dup.docx"),),
        )
        saved = self.files.persist_bytes("vk:1:1:0", "dup.docx", b"payload")
        job1 = self.store.enqueue_file(envelope, envelope.attachments[0], saved)
        job2 = self.store.enqueue_file(envelope, envelope.attachments[0], saved)

        # INSERT OR IGNORE returns the existing row — same job_id
        self.assertEqual(job1.job_id, job2.job_id)
        # Total jobs should be 1, not 2
        stats = self.store.get_stats()
        self.assertEqual(1, stats["total"])

    def test_list_vk_counter_jobs_filters_peer_and_sender(self) -> None:
        def enqueue(chat_id: str, sender_id: str, file_name: str):
            envelope = InboundEnvelope(
                source="vk",
                event_id=f"vk:{chat_id}:{sender_id}:{file_name}",
                chat_id=chat_id,
                sender_id=sender_id,
                sender_name="tester",
                message_id=file_name,
                attachments=(AttachmentRef(index=0, file_name=file_name),),
            )
            saved = self.files.persist_bytes(envelope.attachment_dedupe_key(envelope.attachments[0]), file_name, b"payload")
            return self.store.enqueue_file(envelope, envelope.attachments[0], saved)

        matching = enqueue("2000000001", "101", "match.docx")
        enqueue("2000000001", "202", "wrong_author.docx")
        enqueue("2000000002", "101", "wrong_peer.docx")
        enqueue("1", "101", "wrong_source_peer.docx")

        result = self.store.list_vk_counter_jobs("2000000001", ("101",))

        self.assertEqual([matching.job_id], [job.job_id for job in result])

    def test_list_vk_counter_jobs_returns_all_senders_when_sender_filter_empty(self) -> None:
        first = self._enqueue(source="vk", file_name="first.docx")
        envelope = InboundEnvelope(
            source="vk",
            event_id="vk:1:2",
            chat_id="1",
            sender_id="99",
            sender_name="tester",
            message_id="2",
            attachments=(AttachmentRef(index=0, file_name="second.docx"),),
        )
        saved = self.files.persist_bytes("vk:1:2:0", "second.docx", b"payload")
        second = self.store.enqueue_file(envelope, envelope.attachments[0], saved)

        result = self.store.list_vk_counter_jobs("1")

        self.assertEqual([first.job_id, second.job_id], [job.job_id for job in result])


if __name__ == "__main__":
    unittest.main()
