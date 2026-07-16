from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from multichannel_gateway.core.files import AtomicFileStore


class CoreFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.store = AtomicFileStore(self.base / "inbox")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @patch("multichannel_gateway.core.files.requests")
    def test_persist_download_uses_requests_stream(self, mock_requests: Mock) -> None:
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        response.raise_for_status.return_value = None
        response.raw = tempfile.TemporaryFile()
        response.raw.write(b"vk-payload")
        response.raw.seek(0)
        mock_requests.get.return_value = response

        saved = self.store.persist_download("vk:1:2:0", "report.docx", "https://example.invalid/report.docx")

        self.assertTrue(saved.path.exists())
        self.assertEqual(b"vk-payload", saved.path.read_bytes())
        mock_requests.get.assert_called_once()


class SanitizeNameTests(unittest.TestCase):
    """Edge cases for AtomicFileStore.sanitize_name()."""

    def test_empty_string_returns_fallback(self) -> None:
        self.assertEqual("file.bin", AtomicFileStore.sanitize_name(""))

    def test_none_returns_fallback(self) -> None:
        self.assertEqual("file.bin", AtomicFileStore.sanitize_name(None))

    def test_whitespace_only_returns_fallback(self) -> None:
        self.assertEqual("file.bin", AtomicFileStore.sanitize_name("   "))

    def test_custom_fallback(self) -> None:
        self.assertEqual("default.docx", AtomicFileStore.sanitize_name("", fallback="default.docx"))

    def test_slashes_replaced(self) -> None:
        result = AtomicFileStore.sanitize_name("path/to\\file.docx")
        self.assertNotIn("/", result)
        self.assertNotIn("\\", result)
        self.assertIn("file.docx", result)

    def test_cyrillic_preserved(self) -> None:
        result = AtomicFileStore.sanitize_name("Заказ 12345.docx")
        self.assertIn("Заказ", result)
        self.assertIn("12345", result)
        self.assertTrue(result.endswith(".docx"))

    def test_special_characters_sanitized(self) -> None:
        result = AtomicFileStore.sanitize_name("file<>|?.docx")
        self.assertNotIn("<", result)
        self.assertNotIn(">", result)
        self.assertNotIn("|", result)


class PersistBytesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = AtomicFileStore(Path(self.temp_dir.name) / "inbox")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_sha256_is_correct(self) -> None:
        import hashlib
        payload = b"test content for sha256"
        expected_sha = hashlib.sha256(payload).hexdigest()

        saved = self.store.persist_bytes("key:1", "test.txt", payload)
        self.assertEqual(expected_sha, saved.sha256)
        self.assertEqual(len(payload), saved.size_bytes)

    def test_file_written_correctly(self) -> None:
        payload = b"hello world"
        saved = self.store.persist_bytes("key:2", "hello.txt", payload)
        self.assertEqual(payload, saved.path.read_bytes())


class PersistFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = AtomicFileStore(Path(self.temp_dir.name) / "inbox")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_source_not_found_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            self.store.persist_file("key:3", "missing.docx", "/nonexistent/path/missing.docx")

    def test_persist_file_copies_content(self) -> None:
        source = Path(self.temp_dir.name) / "source.docx"
        source.write_bytes(b"source content")

        saved = self.store.persist_file("key:4", "copy.docx", source)
        self.assertTrue(saved.path.exists())
        self.assertEqual(b"source content", saved.path.read_bytes())
        self.assertEqual("copy.docx", saved.original_file_name)


class PersistDownloadedAttachmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = AtomicFileStore(Path(self.temp_dir.name) / "inbox")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_content_bytes_branch(self) -> None:
        from multichannel_gateway.core.models import DownloadedAttachment

        download = DownloadedAttachment(
            original_file_name="bytes.docx",
            content_bytes=b"byte payload",
        )
        saved = self.store.persist_downloaded_attachment("key:5", download)
        self.assertEqual(b"byte payload", saved.path.read_bytes())

    def test_file_path_branch(self) -> None:
        from multichannel_gateway.core.models import DownloadedAttachment

        source = Path(self.temp_dir.name) / "local.docx"
        source.write_bytes(b"local file")
        download = DownloadedAttachment(
            original_file_name="local.docx",
            file_path=str(source),
        )
        saved = self.store.persist_downloaded_attachment("key:6", download)
        self.assertEqual(b"local file", saved.path.read_bytes())

    def test_no_data_raises_value_error(self) -> None:
        from multichannel_gateway.core.models import DownloadedAttachment

        download = DownloadedAttachment(original_file_name="empty.docx")
        with self.assertRaises(ValueError):
            self.store.persist_downloaded_attachment("key:7", download)


if __name__ == "__main__":
    unittest.main()
