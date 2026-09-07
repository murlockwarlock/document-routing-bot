from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .models import DownloadedAttachment, SavedFile

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


class AtomicFileStore:
    def __init__(self, root_dir: str | Path):
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def sanitize_name(file_name: str, fallback: str = "file.bin") -> str:
        candidate = (file_name or "").strip()
        if not candidate:
            candidate = fallback
        candidate = candidate.replace("\\", "_").replace("/", "_")
        candidate = re.sub(r"[^\w.\-() @\u0400-\u04ff]+", "_", candidate)
        candidate = candidate.strip("._ ")
        return candidate or fallback

    def _target_path(self, dedupe_key: str, file_name: str) -> Path:
        safe_name = self.sanitize_name(file_name)
        prefix = hashlib.sha256(dedupe_key.encode("utf-8")).hexdigest()[:16]
        return self.root_dir / f"{prefix}_{safe_name}"

    def persist_bytes(self, dedupe_key: str, file_name: str, payload: bytes, mime_type: str | None = None) -> SavedFile:
        target = self._target_path(dedupe_key, file_name)
        tmp_fd, tmp_name = tempfile.mkstemp(prefix="ingest_", dir=self.root_dir)
        sha = hashlib.sha256()
        try:
            with os.fdopen(tmp_fd, "wb") as handle:
                handle.write(payload)
                sha.update(payload)
            os.replace(tmp_name, target)
        except Exception:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise

        return SavedFile(
            path=target,
            sha256=sha.hexdigest(),
            size_bytes=len(payload),
            original_file_name=file_name,
            mime_type=mime_type,
        )

    def persist_file(self, dedupe_key: str, file_name: str, source_path: str | Path, mime_type: str | None = None) -> SavedFile:
        source = Path(source_path)
        if not source.exists():
            raise FileNotFoundError(source)

        target = self._target_path(dedupe_key, file_name)
        tmp_fd, tmp_name = tempfile.mkstemp(prefix="ingest_", dir=self.root_dir)
        sha = hashlib.sha256()
        size = 0
        try:
            with source.open("rb") as src, os.fdopen(tmp_fd, "wb") as dst:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
                    sha.update(chunk)
                    size += len(chunk)
            os.replace(tmp_name, target)
        except Exception:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise

        return SavedFile(
            path=target,
            sha256=sha.hexdigest(),
            size_bytes=size,
            original_file_name=file_name,
            mime_type=mime_type,
        )

    def persist_stream(
        self,
        dedupe_key: str,
        file_name: str,
        stream: BinaryIO,
        mime_type: str | None = None,
    ) -> SavedFile:
        target = self._target_path(dedupe_key, file_name)
        tmp_fd, tmp_name = tempfile.mkstemp(prefix="ingest_", dir=self.root_dir)
        sha = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(tmp_fd, "wb") as dst:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
                    sha.update(chunk)
                    size += len(chunk)
            os.replace(tmp_name, target)
        except Exception:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise

        return SavedFile(
            path=target,
            sha256=sha.hexdigest(),
            size_bytes=size,
            original_file_name=file_name,
            mime_type=mime_type,
        )

    def persist_download(
        self,
        dedupe_key: str,
        file_name: str,
        remote_url: str,
        mime_type: str | None = None,
        headers: dict[str, str] | None = None,
        timeout: int = 120,
    ) -> SavedFile:
        scheme = urlparse(remote_url).scheme.lower()
        if requests is not None and scheme in {"http", "https"}:
            with requests.get(remote_url, headers=headers or {}, stream=True, timeout=timeout) as response:
                response.raise_for_status()
                return self.persist_stream(dedupe_key, file_name, response.raw, mime_type=mime_type)

        request = Request(remote_url, headers=headers or {})
        with urlopen(request, timeout=timeout) as response:
            return self.persist_stream(dedupe_key, file_name, response, mime_type=mime_type)

    def persist_downloaded_attachment(self, dedupe_key: str, download: DownloadedAttachment) -> SavedFile:
        if download.file_path is not None:
            saved = self.persist_file(dedupe_key, download.original_file_name, download.file_path, mime_type=download.mime_type)
            if download.cleanup_after_persist:
                try:
                    Path(download.file_path).unlink(missing_ok=True)
                except Exception:
                    pass
            return saved
        if download.content_bytes is not None:
            return self.persist_bytes(dedupe_key, download.original_file_name, download.content_bytes, mime_type=download.mime_type)
        if download.remote_url is not None:
            return self.persist_download(dedupe_key, download.original_file_name, download.remote_url, mime_type=download.mime_type)
        raise ValueError("DownloadedAttachment does not contain file_path, content_bytes or remote_url")
