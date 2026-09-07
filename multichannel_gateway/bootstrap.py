from __future__ import annotations

from pathlib import Path

from log_utils import build_file_logger
from multichannel_gateway.core.files import AtomicFileStore
from multichannel_gateway.core.outbox import LocalOutboxSpool
from multichannel_gateway.core.router import InboundGateway, PassthroughProcessor, QueueWorker
from multichannel_gateway.core.storage import SqliteJobStore


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "jobs.sqlite3"
INBOX_DIR = DATA_DIR / "inbox"
OUTBOX_DIR = DATA_DIR / "outbox"


def build_gateway() -> InboundGateway:
    store = build_store()
    file_store = AtomicFileStore(INBOX_DIR)
    logger = build_file_logger("multichannel.gateway", "gateway")
    return InboundGateway(store=store, file_store=file_store, logger=logger)


def build_worker(worker_name: str = "bootstrap-worker") -> QueueWorker:
    store = build_store()
    processor = PassthroughProcessor(INBOX_DIR)
    return QueueWorker(store=store, processor=processor, worker_name=worker_name)


def build_store() -> SqliteJobStore:
    return SqliteJobStore(DB_PATH)


def build_outbox() -> LocalOutboxSpool:
    return LocalOutboxSpool(OUTBOX_DIR)
