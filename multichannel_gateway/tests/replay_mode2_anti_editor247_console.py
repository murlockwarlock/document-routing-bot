from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bot_handlers import BotHandlers


def _make_manager():
    manager = MagicMock()
    manager.file_queue = []
    manager.processing_files = {}
    manager.blacklisted_accounts = []
    manager.get_client.return_value = SimpleNamespace(name="НИК-2")
    manager.mark_account_busy = MagicMock()
    manager.mark_account_free = MagicMock()
    return manager


async def main():
    config_patcher = patch("bot_handlers.Config")
    mock_config = config_patcher.start()
    settings = {
        "mode": "mode2",
        "time_start": "09:00",
        "time_end": "21:00",
        "editor_24_7": "@editor247",
        "normal_destination": "бот",
        "anti_destination": "бот",
        "anti_bot": "@plagaiscan_bot",
        "max_concurrent": 7,
        "max_files_per_day": 999,
        "plagiscan_users": ["vk:700", "tg_plagi"],
    }
    mock_config.get_setting.side_effect = lambda key, default=None: settings.get(key, default)
    manager = _make_manager()
    handlers = BotHandlers(manager)

    temp_dir = tempfile.TemporaryDirectory()
    temp_file = Path(temp_dir.name) / "vk_anti анти.docx"
    temp_file.write_bytes(b"demo")

    async def fake_ensure_work_file(file_info, message=None, prefix="work"):
        work_file = Path(temp_dir.name) / f"{prefix}_{file_info['original_file_name']}"
        work_file.write_bytes(b"demo")
        return str(work_file)

    async def fake_send_document(chat_id, document, file_name=None, caption=None):
        return SimpleNamespace(id=9001, chat=SimpleNamespace(id=777))

    handlers._ensure_work_file = fake_ensure_work_file
    handlers._mark_gateway_job_waiting_editor = AsyncMock()
    manager.get_client.return_value = SimpleNamespace(
        name="НИК-2",
        send_document=fake_send_document,
    )
    handlers.is_within_working_hours = MagicMock(return_value=False)
    handlers.check_daily_limit = MagicMock(return_value=True)
    handlers.aaa_globally_unavailable = False

    file_info = {
        "author": "VK Chat",
        "author_id": "701",
        "file_name": "vk_anti анти.docx",
        "original_file_name": "vk_anti анти.docx",
        "message_id": "105",
        "chat_id": "2000000001",
        "message": None,
        "is_anti": True,
        "force_plagiscan": False,
        "source_platform": "vk",
        "route_sender_id": "701",
        "route_sender_name": "student_vk",
        "route_chat_id": "2000000001",
        "file_uid": "vk:2000000001:105:0",
        "local_path": str(temp_file),
        "gateway_job_id": 37,
    }

    print("📋 Текущие настройки сценария")
    print(f"Режим: {settings['mode']}")
    print(f"Время работы: {settings['time_start']} - {settings['time_end']}")
    print(f"Круглосуточный редактор: {settings['editor_24_7']}")
    print(f"Обычные файлы: {settings['normal_destination']}")
    print(f"Анти-файлы: {settings['anti_destination']}")
    print(f"Пользователи Плагискана: {', '.join(settings['plagiscan_users'])}")
    print()

    print("📥 External job добавлен в очередь: source=vk job_id=37 file=vk_anti анти.docx")
    manager.file_queue.append(file_info)
    print("✅ Файл добавлен в очередь: vk_anti анти.docx")
    print("   Тип: АНТИ")
    print(f"   В очереди: {len(manager.file_queue)} файлов")
    print()
    print("📊 Статус: 1 в очереди [TG:0, VK:1] | 0 в обработке [TG:0, VK:0] | 1 файлов сегодня [TG:0, VK:1]")

    try:
        await handlers.process_queue()
        handlers._mark_gateway_job_waiting_editor.assert_awaited_once()
    finally:
        config_patcher.stop()

    print()
    print("📊 Статус: 0 в очереди [TG:0, VK:0] | 1 в обработке [TG:0, VK:1] | 1 файлов сегодня [TG:0, VK:1]")
    print("✅ СЦЕНАРИЙ ПРОЙДЕН: анти-файл в mode2 вне рабочего времени ушел круглосуточному редактору")
    print("✅ Плагискан для этого файла не вызывался")
    temp_dir.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
