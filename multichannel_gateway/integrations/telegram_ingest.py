from __future__ import annotations

from typing import Any

from multichannel_gateway.adapters.telegram import TelegramPyrogramAdapter
from multichannel_gateway.bootstrap import build_gateway
from multichannel_gateway.core.models import JobRecord


async def ingest_pyrogram_message(message: Any) -> list[JobRecord]:
    """
    Thin integration point for the current Telegram codebase.

    Usage:
        jobs = await ingest_pyrogram_message(message)

    After this step the file is already persisted locally and has a stable job id.
    """
    adapter = TelegramPyrogramAdapter()
    envelope = adapter.build_envelope(message)
    gateway = build_gateway()
    return await gateway.ingest(adapter, envelope)
