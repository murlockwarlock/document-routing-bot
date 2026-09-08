from __future__ import annotations

import json
import hashlib
import os
import tempfile
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class LocalOutboxSpool:
    def __init__(self, root_dir: str | Path):
        self.root_dir = Path(root_dir)
        self.files_dir = self.root_dir / "files"
        self.meta_dir = self.root_dir / "manifests"
        self.archive_files_dir = self.root_dir / "archive" / "files"
        self.archive_meta_dir = self.root_dir / "archive" / "manifests"
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        self.archive_files_dir.mkdir(parents=True, exist_ok=True)
        self.archive_meta_dir.mkdir(parents=True, exist_ok=True)

    def spool_delivery(
        self,
        source_platform: str,
        route: dict[str, Any],
        file_path: str | Path,
        caption: str | None = None,
        file_name: str | None = None,
        reason: str | None = None,
        dedupe_key: str | None = None,
    ) -> dict[str, Any]:
        source = (source_platform or "unknown").lower()
        source_path = Path(file_path)
        if not source_path.exists():
            raise FileNotFoundError(source_path)

        entry_id = self.delivery_id(dedupe_key) if dedupe_key else uuid.uuid4().hex
        if dedupe_key:
            existing = self.get_entry(entry_id)
            if existing is not None:
                return existing
        stored_name = f"{entry_id}_{file_name or source_path.name}"
        stored_file_path = self.files_dir / stored_name
        shutil.copy2(source_path, stored_file_path)

        manifest = {
            "id": entry_id,
            "source_platform": source,
            "route": route,
            "caption": caption,
            "file_name": file_name or source_path.name,
            "stored_file_path": str(stored_file_path),
            "original_file_path": str(source_path),
            "status": "pending",
            "attempts": 0,
            "last_error": None,
            "delivery_response": None,
            "reason": reason,
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
            "sent_at": None,
        }
        self._write_manifest(entry_id, manifest)
        return manifest

    @staticmethod
    def delivery_id(dedupe_key):
        return hashlib.sha256(dedupe_key.encode()).hexdigest()

    def list_entries(self, limit: int = 50, pending_only: bool = False) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for manifest_path in sorted(self.meta_dir.glob("*.json"), reverse=True):
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            if pending_only and payload.get("status") != "pending":
                continue
            entries.append(payload)
            if len(entries) >= limit:
                break
        return entries

    def get_entry(self, entry_id: str) -> dict[str, Any] | None:
        manifest_path = self.meta_dir / f"{entry_id}.json"
        if not manifest_path.exists():
            return None
        return json.loads(manifest_path.read_text(encoding="utf-8"))

    def mark_sent(self, entry_id: str, delivery_response: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = self._require_entry(entry_id)
        payload["status"] = "sent"
        payload["attempts"] = int(payload.get("attempts") or 0) + 1
        payload["delivery_response"] = delivery_response
        payload["last_error"] = None
        payload["sent_at"] = utc_now_iso()
        payload["updated_at"] = utc_now_iso()
        self._write_manifest(entry_id, payload)
        return payload

    def mark_failed(self, entry_id: str, error_text: str) -> dict[str, Any]:
        payload = self._require_entry(entry_id)
        payload["status"] = "pending"
        payload["attempts"] = int(payload.get("attempts") or 0) + 1
        payload["last_error"] = error_text[:2000]
        payload["updated_at"] = utc_now_iso()
        self._write_manifest(entry_id, payload)
        return payload

    def abandon_telegram_before(self, cutoff_iso: str) -> int:
        cutoff = datetime.fromisoformat(cutoff_iso)
        abandoned = 0
        for manifest_path in self.meta_dir.glob("*.json"):
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            if payload.get("source_platform") != "telegram" or payload.get("status") != "pending":
                continue
            if datetime.fromisoformat(payload["created_at"]) >= cutoff:
                continue
            payload["status"] = "abandoned"
            payload["reason"] = "abandoned_on_restart"
            payload["updated_at"] = utc_now_iso()
            self._write_manifest(payload["id"], payload)
            abandoned += 1
        return abandoned

    def archive_sent(self, limit: int = 100) -> list[str]:
        archived: list[str] = []
        for payload in self.list_entries(limit=limit * 5, pending_only=False):
            if payload.get("status") != "sent":
                continue
            entry_id = payload["id"]
            manifest_path = self.meta_dir / f"{entry_id}.json"
            stored_file_path = Path(payload["stored_file_path"])
            archive_manifest_path = self.archive_meta_dir / f"{entry_id}.json"
            archive_file_path = self.archive_files_dir / stored_file_path.name

            if stored_file_path.exists():
                shutil.move(str(stored_file_path), str(archive_file_path))
                payload["stored_file_path"] = str(archive_file_path)
            if manifest_path.exists():
                archive_manifest_path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                    encoding="utf-8",
                )
                manifest_path.unlink()
            archived.append(entry_id)
            if len(archived) >= limit:
                break
        return archived

    def delete_entry(self, entry_id: str, include_file: bool = True) -> bool:
        payload = self.get_entry(entry_id)
        if payload is None:
            return False
        manifest_path = self.meta_dir / f"{entry_id}.json"
        if manifest_path.exists():
            manifest_path.unlink()
        if include_file:
            stored_file_path = Path(payload["stored_file_path"])
            if stored_file_path.exists():
                stored_file_path.unlink()
        return True

    def _require_entry(self, entry_id: str) -> dict[str, Any]:
        payload = self.get_entry(entry_id)
        if payload is None:
            raise FileNotFoundError(f"Outbox entry not found: {entry_id}")
        return payload

    def _write_manifest(self, entry_id: str, payload: dict[str, Any]) -> None:
        manifest_path = self.meta_dir / f"{entry_id}.json"
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.meta_dir, delete=False) as handle:
            temp_path = Path(handle.name)
            try:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
                os.replace(temp_path, manifest_path)
            finally:
                if temp_path.exists():
                    temp_path.unlink()
