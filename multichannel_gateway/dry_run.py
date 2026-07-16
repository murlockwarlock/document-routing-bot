from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from multichannel_gateway.adapters.max_adapter import MaxBotAdapter
from multichannel_gateway.adapters.vk import VkCallbackAdapter
from multichannel_gateway.bootstrap import BASE_DIR, DATA_DIR, build_gateway, build_store
from multichannel_gateway.core.files import AtomicFileStore
from multichannel_gateway.core.outbox import LocalOutboxSpool
from multichannel_gateway.core.router import InboundGateway
from multichannel_gateway.core.storage import SqliteJobStore
from multichannel_gateway.outbound import OutboundDispatcher


SAMPLES_DIR = BASE_DIR / "samples"
DEFAULT_INPUT_FILE = SAMPLES_DIR / "input" / "sample_input.docx"


def _replace_placeholders(value: Any, input_file: Path) -> Any:
    if isinstance(value, dict):
        return {key: _replace_placeholders(item, input_file) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_placeholders(item, input_file) for item in value]
    if isinstance(value, str):
        return (
            value
            .replace("__LOCAL_FILE_URL__", input_file.resolve().as_uri())
            .replace("__LOCAL_FILE_NAME__", input_file.name)
            .replace("__LOCAL_FILE_SIZE__", str(input_file.stat().st_size))
        )
    return value


def load_payload_template(json_path: Path, input_file: Path) -> dict[str, Any]:
    raw_text = json_path.read_text(encoding="utf-8")
    raw_text = (
        raw_text
        .replace("__LOCAL_FILE_URL__", input_file.resolve().as_uri())
        .replace("__LOCAL_FILE_NAME__", input_file.name)
        .replace("__LOCAL_FILE_SIZE__", str(input_file.stat().st_size))
    )
    payload = json.loads(raw_text)
    return _replace_placeholders(payload, input_file)


def build_adapter(source: str):
    if source == "vk":
        return VkCallbackAdapter()
    if source == "max":
        return MaxBotAdapter()
    raise ValueError(f"Unsupported source: {source}")


def build_envelope(source: str, payload: dict[str, Any]):
    adapter = build_adapter(source)
    return adapter, adapter.build_envelope(payload)


def create_fake_pdf(target_dir: Path, original_name: str) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    final_name = f"{Path(original_name).stem}.pdf"
    result_path = target_dir / final_name
    result_path.write_bytes(
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
        b"trailer<</Root 1 0 R>>\n%%EOF\n"
    )
    return result_path


def prepare_variant_input(input_file: Path, variant: str, work_dir: Path) -> Path:
    work_dir.mkdir(parents=True, exist_ok=True)
    original_name = input_file.name
    if variant == "anti" and "анти" not in original_name.lower():
        target_name = f"анти_{original_name}"
    elif variant == "editor-fallback":
        target_name = f"editor_{original_name}"
    else:
        target_name = original_name

    target_path = work_dir / target_name
    target_path.write_bytes(input_file.read_bytes())
    return target_path


async def execute_dry_run(
    source: str,
    payload: dict[str, Any],
    *,
    gateway: InboundGateway,
    store: SqliteJobStore,
    dispatcher: OutboundDispatcher,
    result_dir: Path,
    variant: str = "normal",
) -> dict[str, Any]:
    adapter, envelope = build_envelope(source, payload)
    jobs = await gateway.ingest(adapter, envelope)
    claimed = await asyncio.to_thread(store.claim_next_bridgeable, "dry-run", (source,))
    if not claimed:
        raise RuntimeError("No bridgeable job was claimed after ingest")

    result_path = create_fake_pdf(result_dir, claimed.original_file_name)
    caption = {
        "normal": "Dry run result",
        "anti": "Dry run anti result",
        "editor-fallback": "Dry run editor fallback result",
    }.get(variant, "Dry run result")
    delivery = await asyncio.to_thread(
        dispatcher.send_result,
        claimed.source,
        {"chat_id": claimed.chat_id, "sender_id": claimed.sender_id},
        result_path,
        caption,
        result_path.name,
    )
    await asyncio.to_thread(store.mark_done, claimed.job_id, str(result_path), "dry_run_complete")

    return {
        "source": source,
        "variant": variant,
        "ingested_jobs": [job.job_id for job in jobs],
        "claimed_job_id": claimed.job_id,
        "result_path": str(result_path),
        "delivery": delivery,
    }


async def run_dry_run(
    source: str,
    json_path: Path,
    input_file: Path | None = None,
    variant: str = "normal",
) -> dict[str, Any]:
    input_path = (input_file or DEFAULT_INPUT_FILE).resolve()
    prepared_input = prepare_variant_input(input_path, variant, DATA_DIR / "dry_run_inputs")
    payload = load_payload_template(json_path, prepared_input)
    gateway = build_gateway()
    store = build_store()
    dispatcher = OutboundDispatcher()
    result_dir = DATA_DIR / "dry_run_results"
    return await execute_dry_run(
        source,
        payload,
        gateway=gateway,
        store=store,
        dispatcher=dispatcher,
        result_dir=result_dir,
        variant=variant,
    )
