from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from log_utils import get_log_channels, get_log_path, read_log_tail, setup_runtime_logging
from multichannel_gateway.adapters.max_adapter import MaxBotAdapter
from multichannel_gateway.adapters.vk import VkCallbackAdapter
from multichannel_gateway.bootstrap import build_gateway, build_store, build_worker
from multichannel_gateway.dry_run import DEFAULT_INPUT_FILE, run_dry_run
from multichannel_gateway.outbound import OutboundDispatcher
from multichannel_gateway.vk_longpoll import VkLongPollConfigurationError, run_vk_longpoll


async def ingest_json(source: str, json_path: Path) -> None:
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    gateway = build_gateway()

    if source == "vk":
        adapter = VkCallbackAdapter()
        envelope = adapter.build_envelope(payload)
    elif source == "max":
        adapter = MaxBotAdapter()
        envelope = adapter.build_envelope(payload)
    else:
        raise ValueError(f"Unsupported source for JSON ingest: {source}")

    jobs = await gateway.ingest(adapter, envelope)
    for job in jobs:
        print(f"job_id={job.job_id} status={job.status} file={job.original_file_name}")


async def run_worker() -> None:
    worker = build_worker()
    await worker.run_forever()


async def run_vk_longpoll_command(once: bool) -> None:
    jobs = await run_vk_longpoll(once=once)
    if once:
        print(json.dumps({"status": "ok", "jobs_processed": len(jobs)}, ensure_ascii=False, indent=2))


def show_stats() -> None:
    stats = build_store().get_stats()
    print(json.dumps(stats, ensure_ascii=False, indent=2))


def recover_stale(older_than_seconds: int) -> None:
    recovered = build_store().release_stale_processing(older_than_seconds=older_than_seconds)
    print(json.dumps({"recovered": recovered, "older_than_seconds": older_than_seconds}, ensure_ascii=False))


def list_jobs(limit: int) -> None:
    jobs = build_store().list_jobs(limit=limit)
    payload = [
        {
            "job_id": job.job_id,
            "source": job.source,
            "status": job.status,
            "file": job.original_file_name,
            "attempts": job.attempts,
            "locked_by": job.locked_by,
            "last_error": job.last_error,
        }
        for job in jobs
    ]
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def list_outbox(limit: int, pending_only: bool) -> None:
    payload = OutboundDispatcher().list_outbox(limit=limit, pending_only=pending_only)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def replay_outbox(limit: int, source_platform: str | None) -> None:
    payload = OutboundDispatcher().replay_outbox(limit=limit, source_platform=source_platform)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def archive_outbox(limit: int) -> None:
    archived = OutboundDispatcher().outbox.archive_sent(limit=limit)
    print(json.dumps({"archived": archived, "count": len(archived)}, ensure_ascii=False, indent=2))


def delete_outbox_entry(entry_id: str, keep_file: bool) -> None:
    deleted = OutboundDispatcher().outbox.delete_entry(entry_id, include_file=not keep_file)
    print(json.dumps({"id": entry_id, "deleted": deleted, "kept_file": keep_file}, ensure_ascii=False, indent=2))


def dry_run(source: str, json_path: Path, input_file: Path | None, variant: str) -> None:
    payload = asyncio.run(run_dry_run(source, json_path, input_file, variant))
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def list_logs() -> None:
    payload = [
        {
            "channel": channel,
            "path": str(get_log_path(channel)),
            "exists": get_log_path(channel).exists(),
        }
        for channel in get_log_channels()
    ]
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def show_log(channel: str, lines: int) -> None:
    payload = read_log_tail(channel, lines=lines)
    if payload:
        print(payload)


def tail_log(channel: str, lines: int, interval_seconds: float) -> None:
    log_path = get_log_path(channel)
    print(f"# tail {log_path}")
    initial = read_log_tail(channel, lines=lines)
    if initial:
        print(initial)

    last_size = log_path.stat().st_size if log_path.exists() else 0
    while True:
        try:
            time.sleep(interval_seconds)
            if not log_path.exists():
                continue

            current_size = log_path.stat().st_size
            if current_size < last_size:
                last_size = 0
            if current_size == last_size:
                continue

            with log_path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(last_size)
                chunk = handle.read()
            if chunk:
                print(chunk, end="" if chunk.endswith("\n") else "\n")
            last_size = current_size
        except KeyboardInterrupt:
            return


def main() -> None:
    setup_runtime_logging()
    parser = argparse.ArgumentParser(description="Multi-platform file intake gateway")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest_parser = subparsers.add_parser("ingest-json", help="Ingest a saved MAX/VK webhook payload")
    ingest_parser.add_argument("--source", choices=["max", "vk"], required=True)
    ingest_parser.add_argument("--json", dest="json_path", type=Path, required=True)

    subparsers.add_parser("worker", help="Run the local queue worker")
    subparsers.add_parser("stats", help="Show queue stats")

    recover_parser = subparsers.add_parser("recover", help="Release stale processing jobs back to queued")
    recover_parser.add_argument("--older-than", dest="older_than_seconds", type=int, default=900)

    jobs_parser = subparsers.add_parser("jobs", help="List recent jobs")
    jobs_parser.add_argument("--limit", type=int, default=20)

    outbox_parser = subparsers.add_parser("outbox", help="List local outbox entries")
    outbox_parser.add_argument("--limit", type=int, default=20)
    outbox_parser.add_argument("--pending-only", action="store_true")

    replay_parser = subparsers.add_parser("replay-outbox", help="Replay pending outbox entries when tokens become available")
    replay_parser.add_argument("--limit", type=int, default=10)
    replay_parser.add_argument("--source", choices=["vk", "max"], default=None)

    archive_parser = subparsers.add_parser("archive-outbox", help="Move sent outbox entries into archive")
    archive_parser.add_argument("--limit", type=int, default=50)

    delete_parser = subparsers.add_parser("delete-outbox-entry", help="Delete a local outbox manifest and optionally its stored file")
    delete_parser.add_argument("--id", dest="entry_id", required=True)
    delete_parser.add_argument("--keep-file", action="store_true")

    dry_run_parser = subparsers.add_parser("dry-run", help="Offline end-to-end dry-run for VK/MAX sample payloads")
    dry_run_parser.add_argument("--source", choices=["vk", "max"], required=True)
    dry_run_parser.add_argument("--json", dest="json_path", type=Path, required=True)
    dry_run_parser.add_argument("--input-file", dest="input_file", type=Path, default=DEFAULT_INPUT_FILE)
    dry_run_parser.add_argument("--variant", choices=["normal", "anti", "editor-fallback"], default="normal")

    vk_longpoll_parser = subparsers.add_parser("vk-longpoll", help="Run VK Bots Long Poll ingestion without a public webhook server")
    vk_longpoll_parser.add_argument("--once", action="store_true", help="Poll one long poll cycle and exit")

    subparsers.add_parser("list-logs", help="List available log channels and files")

    show_log_parser = subparsers.add_parser("show-log", help="Show the last lines of a log")
    show_log_parser.add_argument("--channel", choices=get_log_channels(), required=True)
    show_log_parser.add_argument("--lines", type=int, default=50)

    tail_log_parser = subparsers.add_parser("tail-log", help="Follow a log file")
    tail_log_parser.add_argument("--channel", choices=get_log_channels(), required=True)
    tail_log_parser.add_argument("--lines", type=int, default=50)
    tail_log_parser.add_argument("--interval", dest="interval_seconds", type=float, default=1.0)

    args = parser.parse_args()

    if args.command == "ingest-json":
        asyncio.run(ingest_json(args.source, args.json_path))
    elif args.command == "worker":
        asyncio.run(run_worker())
    elif args.command == "stats":
        show_stats()
    elif args.command == "recover":
        recover_stale(args.older_than_seconds)
    elif args.command == "jobs":
        list_jobs(args.limit)
    elif args.command == "outbox":
        list_outbox(args.limit, args.pending_only)
    elif args.command == "replay-outbox":
        replay_outbox(args.limit, args.source)
    elif args.command == "archive-outbox":
        archive_outbox(args.limit)
    elif args.command == "delete-outbox-entry":
        delete_outbox_entry(args.entry_id, args.keep_file)
    elif args.command == "dry-run":
        dry_run(args.source, args.json_path, args.input_file, args.variant)
    elif args.command == "vk-longpoll":
        try:
            asyncio.run(run_vk_longpoll_command(args.once))
        except VkLongPollConfigurationError as exc:
            raise SystemExit(str(exc)) from exc
    elif args.command == "list-logs":
        list_logs()
    elif args.command == "show-log":
        show_log(args.channel, args.lines)
    elif args.command == "tail-log":
        tail_log(args.channel, args.lines, args.interval_seconds)


if __name__ == "__main__":
    main()
