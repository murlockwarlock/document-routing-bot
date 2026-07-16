from __future__ import annotations

import io
import logging
import os
import sys
import threading
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
LOGS_DIR = BASE_DIR / "logs"
LOG_MAX_BYTES = int(os.getenv("BOT_LOG_MAX_BYTES", str(10 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.getenv("BOT_LOG_BACKUP_COUNT", "5"))
FILE_LOGS_ENABLED = os.getenv("BOT_FILE_LOGS_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}

CHANNEL_FILES = {
    "runtime": "runtime.log",
    "errors": "errors.log",
    "main": "main.log",
    "aaa": "aaa_bot.log",
    "plagiscan": "plagiscan_bot.log",
    "editor": "editor.log",
    "gateway": "gateway.log",
    "vk_counter": "vk_counter.log",
}

_channel_lock = threading.Lock()
_runtime_lock = threading.Lock()
_runtime_configured = False
_logger_lock = threading.Lock()


def _rotate_file(path: Path, backup_count: int) -> None:
    if backup_count <= 0:
        if path.exists():
            path.unlink()
        return

    oldest_backup = path.with_name(f"{path.name}.{backup_count}")
    if oldest_backup.exists():
        oldest_backup.unlink()

    for index in range(backup_count - 1, 0, -1):
        src = path.with_name(f"{path.name}.{index}")
        dst = path.with_name(f"{path.name}.{index + 1}")
        if src.exists():
            src.replace(dst)

    if path.exists():
        path.replace(path.with_name(f"{path.name}.1"))


class ManagedRotatingFileStream(io.TextIOBase):
    def __init__(self, path: Path, max_bytes: int = LOG_MAX_BYTES, backup_count: int = LOG_BACKUP_COUNT):
        self.path = path
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self._lock = threading.Lock()
        self._handle = None
        self._open_handle()

    def _open_handle(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8", buffering=1)

    def _should_rotate(self, text: str) -> bool:
        if self.max_bytes <= 0:
            return False
        if not self.path.exists():
            return False
        current_size = self.path.stat().st_size
        incoming_size = len(text.encode("utf-8"))
        return current_size + incoming_size > self.max_bytes

    def write(self, text):
        if not text:
            return 0
        with self._lock:
            if self._should_rotate(text):
                self._handle.close()
                _rotate_file(self.path, self.backup_count)
                self._open_handle()
            self._handle.write(text)
            self._handle.flush()
        return len(text)

    def flush(self):
        with self._lock:
            if self._handle:
                self._handle.flush()

    def close(self):
        with self._lock:
            if self._handle:
                self._handle.close()
                self._handle = None
        super().close()

    def isatty(self):
        return False


class TeeStream(io.TextIOBase):
    def __init__(self, *streams):
        self.streams = streams

    def write(self, text):
        for stream in self.streams:
            stream.write(text)
            stream.flush()
        return len(text)

    def flush(self):
        for stream in self.streams:
            stream.flush()

    def isatty(self):
        for stream in self.streams:
            if hasattr(stream, "isatty") and stream.isatty():
                return True
        return False


def ensure_logs_dir() -> Path:
    if not FILE_LOGS_ENABLED:
        return LOGS_DIR
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return LOGS_DIR


def get_log_path(channel: str) -> Path:
    if FILE_LOGS_ENABLED:
        ensure_logs_dir()
    file_name = CHANNEL_FILES.get(channel, f"{channel}.log")
    return LOGS_DIR / file_name


def get_log_channels() -> list[str]:
    return list(CHANNEL_FILES.keys())


def read_log_tail(channel: str, lines: int = 50) -> str:
    log_path = get_log_path(channel)
    if not log_path.exists():
        return ""

    payload = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    if lines <= 0:
        return ""
    return "\n".join(payload[-lines:])


def append_channel_log(channel: str, message: str) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {message.rstrip()}\n"
    if not FILE_LOGS_ENABLED:
        return

    log_path = get_log_path(channel)
    with _channel_lock:
        stream = ManagedRotatingFileStream(log_path)
        try:
            stream.write(line)
        finally:
            stream.close()


def setup_runtime_logging() -> None:
    global _runtime_configured
    if _runtime_configured:
        return

    with _runtime_lock:
        if _runtime_configured:
            return

        if not FILE_LOGS_ENABLED:
            _runtime_configured = True
            return

        runtime_handle = ManagedRotatingFileStream(get_log_path("runtime"))
        error_handle = ManagedRotatingFileStream(get_log_path("errors"))

        if not isinstance(sys.stdout, TeeStream):
            sys.stdout = TeeStream(sys.__stdout__, runtime_handle)
        if not isinstance(sys.stderr, TeeStream):
            sys.stderr = TeeStream(sys.__stderr__, runtime_handle, error_handle)

        _runtime_configured = True


def build_file_logger(logger_name: str, channel: str) -> logging.Logger:
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    with _logger_lock:
        if not FILE_LOGS_ENABLED:
            return logger

        log_path = get_log_path(channel)
        existing_paths = {
            Path(getattr(handler, "baseFilename", "")).resolve()
            for handler in logger.handlers
            if getattr(handler, "baseFilename", None)
        }
        if log_path.resolve() not in existing_paths:
            handler = RotatingFileHandler(
                log_path,
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
            logger.addHandler(handler)

    return logger
