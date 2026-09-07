import asyncio
import json
from pathlib import Path

from config import BASE_DIR, Config, AccountManager
from bot_handlers import BotHandlers
from main import ConsoleMenu


MAIN_CONFIG_FILE = BASE_DIR / "config.json"
COUNTER_CONFIG_FILE = BASE_DIR / "counter_config.json"

COUNTER_SETTING_KEYS = {

    "mode",
    "counter_type",
    "counter_author",
    "counter_vk_author",
    "counter_tg_author",
    "counter_editor_nick",
    "counter_start_file",
    "counter_start_time",
    "counter_date",
    "counter_end_file",
    "counter_end_date",
    "counter_end_time",
    "counter_pattern",
    "counter_vk_peer_id",
    "counter_vk_scope",
    "counter_vk_source",
}


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"❌ Ошибка загрузки конфигурации: {exc}")
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_counter_config() -> None:
    """Load main accounts/tokens plus separate mode3 settings."""
    main_data = _read_json(MAIN_CONFIG_FILE)
    counter_data = _read_json(COUNTER_CONFIG_FILE)

    Config._ACCOUNTS = {}
    Config._SETTINGS = {}

    accounts_data = main_data.get("accounts", Config.DEFAULT_ACCOUNTS)
    for nickname, account_info in accounts_data.items():
        if nickname not in Config.DEFAULT_ACCOUNTS:
            continue
        account_info = dict(account_info or {})
        if account_info.get("api_id"):
            try:
                account_info["api_id"] = int(account_info["api_id"])
            except (ValueError, TypeError):
                account_info["api_id"] = None
        Config._ACCOUNTS[nickname] = {
            "api_id": account_info.get("api_id"),
            "api_hash": account_info.get("api_hash"),
            "phone": account_info.get("phone"),
        }

    for nickname, default_info in Config.DEFAULT_ACCOUNTS.items():
        if nickname not in Config._ACCOUNTS:
            Config._ACCOUNTS[nickname] = default_info.copy()

    main_settings = dict(main_data.get("settings") or {})
    counter_settings = dict(counter_data.get("settings") or {})

    settings = Config.DEFAULT_SETTINGS.copy()
    settings.update(main_settings)
    settings.update(counter_settings)
    settings["mode"] = "mode3"
    Config._SETTINGS = settings
    Config._normalize_settings(
        has_forced_authors_destination="forced_authors_destination" in main_settings,
        legacy_author_routes=main_settings.get("plagiscan_user_routes"),
    )

    print(f"✅ Конфигурация загружена: {len(Config._ACCOUNTS)} аккаунтов")


def _save_counter_config() -> None:
    """Save only mode3 settings, leaving main config.json untouched."""
    settings = {
        key: Config.get_setting(key)
        for key in sorted(COUNTER_SETTING_KEYS)
        if Config.get_setting(key) is not None
    }
    settings["mode"] = "mode3"
    _write_json(COUNTER_CONFIG_FILE, {"settings": settings})
    print("✅ Конфигурация сохранена")


async def _run_counter_mode() -> None:
    manager = AccountManager()
    handlers = BotHandlers(manager)
    await handlers.run_counter_mode()


async def main() -> None:
    original_load_config = Config.load_config
    original_save_config = Config.save_config
    Config.load_config = classmethod(lambda cls: _load_counter_config())
    Config.save_config = classmethod(lambda cls: _save_counter_config())

    try:
        while True:
            if not ConsoleMenu.setup_before_launch(
                force_mode="mode3",
                header_title="📊 СЧЕТЧИК ФАЙЛОВ - РЕЖИМ 3",
                launch_prompt="Нажмите Enter для запуска счетчика...",
                allow_exit=True,
            ):
                return

            try:
                await _run_counter_mode()
            except KeyboardInterrupt:
                print("\n\n🔄 Возвращаюсь в меню счетчика...")
            except Exception as exc:
                print(f"\n❌ Ошибка в режиме 3: {exc}")
                import traceback
                traceback.print_exc()
    finally:
        Config.load_config = original_load_config
        Config.save_config = original_save_config


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n🔄 Счетчик остановлен")
