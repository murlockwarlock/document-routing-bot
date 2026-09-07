from __future__ import annotations

import asyncio
from datetime import datetime

from multichannel_gateway.bootstrap import build_store
from multichannel_gateway.core.models import JobRecord


async def recover_stale_external_jobs(older_than_seconds: int = 900) -> int:
    store = build_store()
    return await asyncio.to_thread(store.release_stale_processing, older_than_seconds, ("vk", "max"))


async def claim_external_job_for_legacy(worker_name: str = "legacy-bridge") -> JobRecord | None:
    store = build_store()
    return await asyncio.to_thread(store.claim_next_bridgeable, worker_name, ("vk", "max"))


async def skip_external_jobs_before_start(cutoff_iso: str) -> int:
    store = build_store()
    return await asyncio.to_thread(
        store.mark_pending_before_done,
        cutoff_iso,
        ("vk", "max"),
        "skipped_pre_start",
    )


def job_to_legacy_file_info(job: JobRecord, is_anti: bool) -> dict:
    return {
        "author": job.sender_name or job.sender_id,
        "author_id": job.sender_id,
        "file_name": job.original_file_name,
        "message_id": job.message_id,
        "chat_id": job.chat_id,
        "is_anti": is_anti,
        "received_at": datetime.now(),
        "status": "в очереди (external)",
        "message": None,
        "original_file_name": job.original_file_name,
        "sent_to_editor": False,
        "file_uid": job.dedupe_key,
        "local_path": job.file_path,
        "gateway_job_id": job.job_id,
        "source_platform": job.source,
        "route_chat_id": job.chat_id,
        "route_sender_id": job.sender_id,
        "route_sender_name": job.sender_name,
    }
