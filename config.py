import os
import json
import asyncio
from copy import deepcopy
from pathlib import Path
from pyrogram_asyncio_compat import ensure_main_event_loop

ensure_main_event_loop()

from pyrogram import Client
from pyrogram.enums import ParseMode

BASE_DIR = Path(__file__).resolve().parent


class Config:
    """Класс для управления конфигурацией и сессиями"""

    CONFIG_FILE = BASE_DIR / "config.json"
    SESSIONS_DIR = BASE_DIR / "sessions"
    FILES_DIR = BASE_DIR / "files"

    # Хранилища для данных
    _ACCOUNTS = {}
    _SETTINGS = {}

    # Настройки по умолчанию
    DEFAULT_ACCOUNTS = {
        "НИК-1": {"api_id": None, "api_hash": None, "phone": None},
        "НИК-2": {"api_id": None, "api_hash": None, "phone": None},
    }

    DEFAULT_SETTINGS = {
        "mode": "mode1",
        "allowed_authors": [],  # Список авторов, от которых принимаем файлы
        "normal_destination": "плагискан",
        "anti_destination": "бот",
        "anti_bot": "@plagaiscan_bot",
        "plagiscan_users": [],
        "plagiscan_user_routes": {},
        "forced_authors_destination": "plagiscan",
        "forced_authors_editor_nickname": None,
        "telegram_group_routes": {},
        "ai_bot": "@AAA_Report_AIBot",
        "editor_nickname": None,
        "anti_editor_nickname": None,
        "normal_editor_nickname": None,
        "editor_24_7": None,
        "time_start": "00:00",
        "time_end": "23:59",
        "max_files_per_day": 999,
        "max_concurrent": 7,
        "normal_accounts": [],
        "ai_upload_prompt_timeout": 120,
        "ai_upload_prompt_attempts": 2,
        "ai_upload_retry_delay": 30,
        "pdf_status_poll_interval_seconds": 20,
        "pdf_status_attempts": 30,
        "pdf_download_retry_interval_seconds": 20,
        "pdf_download_attempts": 10,
        "pdf_generation_max_wait_seconds": 660,
        # Настройки для режима 3
        "counter_author": None,
        "counter_vk_author": "",
        "counter_tg_author": "",
        "counter_start_file": "",
        "counter_start_time": "",
        "counter_date": None,
        "counter_pattern": "",
        "counter_vk_peer_id": "",
        "counter_vk_scope": "",
        "counter_vk_source": "longpoll",
        # VK / MAX токены (env-переменные VK_BOT_TOKEN / MAX_BOT_TOKEN имеют приоритет)
        "vk_bot_token": None,
        "vk_counter_token": None,
        "vk_api_version": "5.199",
        "vk_longpoll_token": None,
        "vk_group_id": None,
        "vk_longpoll_wait": 25,
        "max_bot_token": None,
    }

    @classmethod
    def load_config(cls):
        """Загрузка конфигурации из файла"""
        config_file = cls.CONFIG_FILE
        if config_file.exists():
            try:
                with open(config_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)

                    # Очищаем перед загрузкой
                    cls._ACCOUNTS = {}
                    cls._SETTINGS = {}

                    # Загружаем аккаунты с обработкой типов данных
                    accounts_data = data.get("accounts", {}) or {}
                    if not isinstance(accounts_data, dict):
                        accounts_data = {}
                    for nickname, account_info in accounts_data.items():
                        if nickname not in cls.DEFAULT_ACCOUNTS:
                            continue
                        account_info = dict(account_info or {})
                        # Преобразуем api_id в int если он строка
                        if 'api_id' in account_info and account_info['api_id']:
                            try:
                                account_info['api_id'] = int(account_info['api_id'])
                            except (ValueError, TypeError):
                                account_info['api_id'] = None

                        cls._ACCOUNTS[nickname] = {
                            "api_id": account_info.get("api_id"),
                            "api_hash": account_info.get("api_hash"),
                            "phone": account_info.get("phone")
                        }

                    # Загружаем настройки
                    settings_data = data.get("settings", {}) or {}
                    if not isinstance(settings_data, dict):
                        settings_data = {}
                    has_forced_authors_destination = "forced_authors_destination" in settings_data
                    legacy_author_routes = settings_data.get("plagiscan_user_routes")
                    cls._SETTINGS = deepcopy(cls.DEFAULT_SETTINGS)
                    cls._SETTINGS.update(settings_data)

                    # Обновляем DEFAULT значениями если что-то отсутствует
                    for key, value in cls.DEFAULT_SETTINGS.items():
                        if key not in cls._SETTINGS:
                            cls._SETTINGS[key] = deepcopy(value)

                    cls._normalize_settings(
                        has_forced_authors_destination=has_forced_authors_destination,
                        legacy_author_routes=legacy_author_routes,
                    )

                    # Добавляем недостающие аккаунты из DEFAULT_ACCOUNTS
                    for nickname, default_info in cls.DEFAULT_ACCOUNTS.items():
                        if nickname not in cls._ACCOUNTS:
                            cls._ACCOUNTS[nickname] = default_info.copy()

                    print(f"✅ Конфигурация загружена: {len(cls._ACCOUNTS)} аккаунтов")

            except Exception as e:
                print(f"❌ Ошибка загрузки конфигурации: {e}")
                import traceback
                traceback.print_exc()
                cls.create_default_config()
        else:
            print("⚠️  Конфигурационный файл не найден")
            cls.create_default_config()

    @classmethod
    def create_default_config(cls):
        """Создание конфигурации по умолчанию"""
        config_file = cls.CONFIG_FILE
        data = {
            "accounts": deepcopy(cls.DEFAULT_ACCOUNTS),
            "settings": deepcopy(cls.DEFAULT_SETTINGS),
        }
        with open(config_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        # Загружаем созданные данные
        cls._ACCOUNTS = deepcopy(cls.DEFAULT_ACCOUNTS)
        cls._SETTINGS = deepcopy(cls.DEFAULT_SETTINGS)

        print("✅ Создана новая конфигурация по умолчанию")

    @classmethod
    def save_config(cls):
        """Сохранение конфигурации в файл"""
        cls._ACCOUNTS = {
            nickname: deepcopy(cls._ACCOUNTS.get(nickname, default_info))
            for nickname, default_info in cls.DEFAULT_ACCOUNTS.items()
        }
        cls._normalize_settings(has_forced_authors_destination=True)
        config_file = cls.CONFIG_FILE
        data = {
            "accounts": deepcopy(cls._ACCOUNTS),
            "settings": deepcopy(cls._SETTINGS),
        }
        with open(config_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print("✅ Конфигурация сохранена")

    @classmethod
    def get_account(cls, nickname):
        """Получение данных аккаунта по никнейму"""
        return cls._ACCOUNTS.get(nickname)

    @classmethod
    def get_setting(cls, key, default=None):
        """Получение настройки"""
        return cls._SETTINGS.get(key, default)

    @classmethod
    def get_all_accounts(cls):
        """Получение всех аккаунтов"""
        return cls._ACCOUNTS

    @classmethod
    def get_all_settings(cls):
        """Получение всех настроек"""
        return cls._SETTINGS

    @classmethod
    def update_account(cls, nickname, api_id=None, api_hash=None, phone=None):
        """Обновление данных аккаунта"""
        if nickname not in cls.DEFAULT_ACCOUNTS:
            return
        if nickname not in cls._ACCOUNTS:
            cls._ACCOUNTS[nickname] = {"api_id": None, "api_hash": None, "phone": None}

        if api_id is not None:
            cls._ACCOUNTS[nickname]["api_id"] = int(api_id) if api_id else None
        if api_hash is not None:
            cls._ACCOUNTS[nickname]["api_hash"] = api_hash
        if phone is not None:
            cls._ACCOUNTS[nickname]["phone"] = phone

    @classmethod
    def update_setting(cls, key, value):
        """Обновление настройки"""
        cls._SETTINGS[key] = value

    @staticmethod
    def _normalize_nickname(value):
        nickname = str(value or "").strip()
        if nickname and not nickname.startswith("@"):
            nickname = "@" + nickname
        return nickname or None

    @classmethod
    def _normalize_normal_destination(cls, value, settings=None):
        destination = str(value or "плагискан").strip().lower()
        if destination in {"редактор", "editor"}:
            return "редактор"
        if destination in {"плагискан", "plagiscan"}:
            return "плагискан"
        if destination in {"бот", "bot"}:
            settings = settings if isinstance(settings, dict) else cls._SETTINGS
            editor = cls._normalize_nickname(
                settings.get("normal_editor_nickname") or settings.get("editor_nickname")
            )
            return "редактор" if editor else "плагискан"
        return "плагискан"

    @classmethod
    def _legacy_shared_route(cls, legacy_routes, forced_authors=None):
        if not isinstance(legacy_routes, dict) or not legacy_routes:
            return "plagiscan", None

        if forced_authors:
            author_keys = {
                str(author).strip().lstrip("@").casefold()
                for author in cls._configured_list(forced_authors)
            }
            route_keys = {
                str(author).strip().lstrip("@").casefold()
                for author in legacy_routes
            }
            if author_keys != route_keys:
                print("⚠️ Старые индивидуальные маршруты неполные; выбран Plagiscan")
                return "plagiscan", None

        normalized = []
        for route in legacy_routes.values():
            if isinstance(route, str):
                route = {"destination": route}
            if not isinstance(route, dict):
                normalized.append(("invalid", None))
                continue
            destination = str(route.get("destination") or route.get("route") or "plagiscan").strip().lower()
            if destination in {"plagiscan", "плагискан", ""}:
                normalized.append(("plagiscan", None))
            elif destination == "editor":
                editor = cls._normalize_nickname(route.get("editor_nickname"))
                normalized.append(("editor", editor) if editor else ("invalid", None))
            else:
                normalized.append(("invalid", None))

        unique = set(normalized)
        if len(unique) == 1 and normalized[0][0] in {"plagiscan", "editor"}:
            return normalized[0]

        print("⚠️ Старые индивидуальные маршруты нельзя однозначно объединить; выбран Plagiscan")
        return "plagiscan", None

    @classmethod
    def _normalize_group_routes(cls, configured):
        if not isinstance(configured, dict):
            return {}

        routes = {}
        for chat_id, route in configured.items():
            chat_key = str(chat_id).strip()
            if not chat_key:
                continue
            route = route if isinstance(route, dict) else {}
            title = str(route.get("title") or "Без названия").strip()
            destination = str(route.get("destination") or "plagiscan").strip().lower()
            if destination == "editor":
                editor = cls._normalize_nickname(route.get("editor_nickname"))
                if editor:
                    routes[chat_key] = {
                        "title": title,
                        "destination": "editor",
                        "editor_nickname": editor,
                    }
                    continue
            routes[chat_key] = {"title": title, "destination": "plagiscan"}
        return routes

    @classmethod
    def _normalize_settings(cls, *, has_forced_authors_destination=True, legacy_author_routes=None):
        settings = cls._SETTINGS
        raw_normal_destination = settings.get("normal_destination")
        normal_editor = cls._normalize_nickname(settings.get("normal_editor_nickname"))
        if normal_editor:
            settings["normal_editor_nickname"] = normal_editor
        if str(raw_normal_destination or "").strip().lower() in {"бот", "bot"} and not normal_editor:
            legacy_editor = cls._normalize_nickname(settings.get("editor_nickname"))
            if legacy_editor:
                settings["normal_editor_nickname"] = legacy_editor
        settings["normal_destination"] = cls._normalize_normal_destination(raw_normal_destination, settings)
        forced_destination = str(settings.get("forced_authors_destination") or "plagiscan").strip().lower()
        if not has_forced_authors_destination:
            forced_destination, legacy_editor = cls._legacy_shared_route(
                legacy_author_routes if legacy_author_routes is not None else settings.get("plagiscan_user_routes"),
                settings.get("plagiscan_users"),
            )
            settings["forced_authors_editor_nickname"] = legacy_editor

        if forced_destination == "editor":
            editor = cls._normalize_nickname(settings.get("forced_authors_editor_nickname"))
            if editor:
                settings["forced_authors_editor_nickname"] = editor
            else:
                forced_destination = "plagiscan"
                settings["forced_authors_editor_nickname"] = None
        else:
            forced_destination = "plagiscan"
            settings["forced_authors_editor_nickname"] = None

        settings["forced_authors_destination"] = forced_destination
        settings["plagiscan_users"] = cls._configured_list(settings.get("plagiscan_users"))
        settings["normal_accounts"] = [
            account
            for account in cls._configured_list(settings.get("normal_accounts"))
            if account in cls.DEFAULT_ACCOUNTS
        ]
        settings["telegram_group_routes"] = cls._normalize_group_routes(settings.get("telegram_group_routes"))
        settings["plagiscan_user_routes"] = {}

    @staticmethod
    def _configured_list(value):
        if isinstance(value, str):
            value = value.split(",")
        if not isinstance(value, (list, tuple, set)):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    @staticmethod
    def setup_accounts_interactive():
        """Интерактивная настройка аккаунтов через консоль"""
        print("\n" + "=" * 50)
        print("⚙️  НАСТРОЙКА АККАУНТОВ TELEGRAM")
        print("=" * 50)

        # Загружаем текущие данные
        Config.load_config()

        for nickname in Config.DEFAULT_ACCOUNTS:
            print(f"\n📱 Настройка аккаунта: {nickname}")
            print("-" * 30)

            # Получаем текущие данные аккаунта
            current_account = Config.get_account(nickname)
            if not current_account:
                current_account = {"api_id": None, "api_hash": None, "phone": None}
                Config._ACCOUNTS[nickname] = current_account

            # Показываем текущие настройки
            if current_account["api_id"] and current_account["api_hash"]:
                print(f"Текущие настройки:")
                print(f"  API ID: {current_account['api_id']}")
                print(f"  API Hash: {current_account['api_hash'][:10]}...")
                print(f"  Phone: {current_account['phone'] or 'не указан'}")

                change = input("Изменить? (y/N): ").lower()
                if change != 'y':
                    continue

            while True:
                try:
                    # Ввод API ID
                    api_id_input = input(
                        "API ID (получите на my.telegram.org) [Enter чтобы оставить текущий]: ").strip()
                    if api_id_input:
                        api_id = int(api_id_input)
                    elif current_account["api_id"]:
                        api_id = current_account["api_id"]
                    else:
                        print("❌ API ID обязателен!")
                        continue

                    # Ввод API Hash
                    api_hash_input = input("API Hash [Enter чтобы оставить текущий]: ").strip()
                    if api_hash_input:
                        api_hash = api_hash_input
                    elif current_account["api_hash"]:
                        api_hash = current_account["api_hash"]
                    else:
                        print("❌ API Hash обязателен!")
                        continue

                    # Ввод номера телефона
                    phone_input = input(
                        "Номер телефона (например, +79123456789) [Enter чтобы оставить текущий]: ").strip()
                    if phone_input:
                        phone = phone_input
                        if phone and not phone.startswith('+'):
                            print("⚠️  Номер должен начинаться с +")
                            phone = '+' + phone
                    else:
                        phone = current_account["phone"]

                    # Обновляем аккаунт
                    Config.update_account(nickname, api_id, api_hash, phone)
                    print(f"✅ {nickname} настроен")
                    break

                except ValueError:
                    print("❌ API ID должен быть числом!")
                except Exception as e:
                    print(f"❌ Ошибка: {e}")

        # Сохраняем изменения
        Config.save_config()
        print("\n✅ Все настройки аккаунтов сохранены!")

    @staticmethod
    def setup_editors_interactive():
        """Интерактивная настройка редакторов и ботов (только общие настройки)"""
        print("\n" + "=" * 50)
        print("⚙️  ОБЩИЕ НАСТРОЙКИ РЕДАКТОРОВ И БОТОВ")
        print("=" * 50)

        # Загружаем текущие настройки
        Config.load_config()
        settings = Config.get_all_settings()

        # Настройка обработки анти-файлов
        print("\n" + "-" * 30)
        print("🛡️  НАСТРОЙКА ОБРАБОТКИ АНТИ-ФАЙЛОВ")
        print("-" * 30)

        current_dest = settings.get("anti_destination", "бот")
        print(f"\nКуда отправлять анти-файлы из НИК-2?")
        print("1. В бота @plagaiscan_bot")
        print("2. Напрямую редактору")
        print(f"Текущая настройка: {current_dest}")

        while True:
            anti_choice = input("Выберите (1-2) [Enter чтобы оставить текущий]: ").strip()
            if anti_choice == '1':
                Config.update_setting("anti_destination", "бот")

                # Настройка бота антиплагиата
                print("\n🤖 Настройка бота антиплагиата:")
                current_bot = settings.get("anti_bot", "@plagaiscan_bot")
                custom_bot = input(f"Имя бота (по умолчанию @plagaiscan_bot) [Enter: {current_bot}]: ").strip()
                Config.update_setting("anti_bot", custom_bot if custom_bot else current_bot)
                break

            elif anti_choice == '2':
                Config.update_setting("anti_destination", "редактор")

                # Настройка редактора
                print("\n👤 Настройка редактора:")
                current_editor = settings.get("anti_editor_nickname") or settings.get("editor_nickname", "")
                editor = input(f"Никнейм редактора для анти-файлов (например, @username) [Enter: {current_editor}]: ").strip()
                if editor:
                    if not editor.startswith('@'):
                        editor = '@' + editor
                else:
                    editor = current_editor
                Config.update_setting("anti_editor_nickname", editor)
                break

            elif not anti_choice and current_dest:
                break
            else:
                print("❌ Неверный выбор")

        # Настройка обработки обычных файлов
        print("\n" + "-" * 30)
        print("📄 НАСТРОЙКА ОБЫЧНЫХ ФАЙЛОВ")
        print("-" * 30)

        current_normal_dest = settings.get("normal_destination", "плагискан")
        print(f"\nКуда отправлять обычные файлы?")
        print("1. Напрямую редактору")
        print("2. В бота @plagaiscan_bot")
        print(f"Текущая настройка: {current_normal_dest}")

        while True:
            normal_choice = input("Выберите (1-2) [Enter чтобы оставить текущий]: ").strip()
            if normal_choice == '1':
                Config.update_setting("normal_destination", "редактор")
                current_editor = settings.get("normal_editor_nickname") or settings.get("editor_nickname", "")
                editor = input(f"Никнейм редактора для обычных файлов (например, @username) [Enter: {current_editor}]: ").strip()
                if editor:
                    if not editor.startswith('@'):
                        editor = '@' + editor
                else:
                    editor = current_editor
                Config.update_setting("normal_editor_nickname", editor)
                break
            elif normal_choice == '2':
                Config.update_setting("normal_destination", "плагискан")
                current_bot = settings.get("anti_bot", "@plagaiscan_bot")
                custom_bot = input(f"Имя Plagiscan бота [Enter: {current_bot}]: ").strip()
                Config.update_setting("anti_bot", custom_bot if custom_bot else current_bot)
                break
            elif not normal_choice and current_normal_dest:
                break
            else:
                print("❌ Неверный выбор")

        Config.setup_forced_authors_interactive()

        # Настройка параллельной обработки
        print("\n" + "-" * 30)
        print("⚡ НАСТРОЙКА ПАРАЛЛЕЛЬНОЙ ОБРАБОТКИ")
        print("-" * 30)

        try:
            current_conc = settings.get("max_concurrent", 7)
            max_conc = input(f"Максимум одновременных проверок [Enter: {current_conc}]: ").strip()
            if max_conc:
                Config.update_setting("max_concurrent", int(max_conc))
        except ValueError:
            print("⚠️  Используется текущее значение")

        # Сохранение настроек
        Config.save_config()
        print("\n" + "=" * 50)
        print("✅ ОБЩИЕ НАСТРОКИ СОХРАНЕНЫ!")
        print("=" * 50)
        Config.show_current_settings()

    @staticmethod
    def setup_forced_authors_route_interactive():
        settings = Config.get_all_settings()
        current_destination = str(settings.get("forced_authors_destination", "plagiscan") or "plagiscan").lower()
        current_editor = Config._normalize_nickname(settings.get("forced_authors_editor_nickname")) or ""
        if current_destination != "editor" or not current_editor:
            current_destination = "plagiscan"
            current_editor = ""
        current_label = "Plagiscan" if current_destination == "plagiscan" else f"редактор {current_editor}"

        print("\nКуда отправлять файлы принудительных авторов?")
        print("1. Plagiscan")
        print("2. Редактор")
        print(f"Текущая настройка: {current_label}")

        while True:
            choice = input("Выберите [Enter — оставить текущую настройку]: ").strip()
            if not choice:
                return
            if choice == "1":
                Config.update_setting("forced_authors_destination", "plagiscan")
                Config.update_setting("forced_authors_editor_nickname", None)
                return
            if choice == "2":
                editor = input(f"Ник редактора [Enter: {current_editor or 'указать'}]: ").strip()
                editor = Config._normalize_nickname(editor) or current_editor
                if editor:
                    Config.update_setting("forced_authors_destination", "editor")
                    Config.update_setting("forced_authors_editor_nickname", editor)
                    return
                print("❌ Для маршрута к редактору нужен никнейм")
                continue
            print("❌ Неверный выбор")

    @staticmethod
    def setup_forced_authors_interactive():
        settings = Config.get_all_settings()
        current_users = Config._configured_list(settings.get("plagiscan_users"))
        current_vk = []
        current_tg = []
        for user in current_users:
            lower_user = user.lower()
            if (lower_user.startswith("vk:") or lower_user.startswith("vk.com/")
                    or lower_user.startswith("https://vk.com/") or lower_user.startswith("http://vk.com/")):
                current_vk.append(user)
            else:
                current_tg.append(user)

        print("\n" + "-" * 30)
        print("👤 ПРИНУДИТЕЛЬНЫЕ АВТОРЫ")
        print("-" * 30)
        print(f"Авторы VK: {', '.join(current_vk) if current_vk else 'не заданы'}")
        vk_input = input("Авторы VK [Enter — оставить, 0 — очистить]: ").strip()
        print(f"Авторы Telegram: {', '.join(current_tg) if current_tg else 'не заданы'}")
        tg_input = input("Авторы Telegram [Enter — оставить, 0 — очистить]: ").strip()

        if vk_input == "0":
            new_vk = []
        elif vk_input:
            new_vk = []
            for item in vk_input.split(","):
                item = item.strip().lstrip("@")
                if not item:
                    continue
                lower_item = item.lower()
                if (lower_item.startswith("vk:") or lower_item.startswith("vk.com/")
                        or lower_item.startswith("https://vk.com/") or lower_item.startswith("http://vk.com/")):
                    new_vk.append(item)
                else:
                    new_vk.append(f"vk:{item}")
        else:
            new_vk = current_vk

        if tg_input == "0":
            new_tg = []
        elif tg_input:
            new_tg = [item.strip() for item in tg_input.split(",") if item.strip()]
        else:
            new_tg = current_tg

        combined = new_vk + new_tg
        Config.update_setting("plagiscan_users", combined)
        print(f"✅ Принудительные авторы: {', '.join(combined) if combined else 'не заданы'}")
        Config.setup_forced_authors_route_interactive()

    @staticmethod
    def setup_plagiscan_user_routes_interactive(authors=None):
        Config.setup_forced_authors_route_interactive()

    @classmethod
    async def get_telegram_group_dialogs(cls, telegram_client=None):
        client = telegram_client
        own_client = False
        if client is not None and getattr(client, "is_connected", True) is False:
            client = None
        if client is None:
            account = cls.get_account("НИК-1") or {}
            session_path = cls.get_session_name("НИК-1")
            if not account.get("api_id") or not account.get("api_hash") or not os.path.exists(session_path):
                print("⚠️ Не удалось получить список бесед. Сначала настройте и авторизуйте НИК-1.")
                return []
            client = Client(
                name="НИК-1",
                api_id=account["api_id"],
                api_hash=account["api_hash"],
                parse_mode=ParseMode.HTML,
                workdir=str(cls.SESSIONS_DIR),
                sleep_threshold=0,
            )
            own_client = True

        try:
            if own_client:
                await client.start()
            dialogs_source = client.get_dialogs()
            if hasattr(dialogs_source, "__await__"):
                dialogs_source = await dialogs_source

            dialogs = []
            if hasattr(dialogs_source, "__aiter__"):
                async for dialog in dialogs_source:
                    dialogs.append(dialog)
            else:
                dialogs.extend(dialogs_source or [])

            result = []
            seen_ids = set()
            for dialog in dialogs:
                chat = getattr(dialog, "chat", dialog)
                chat_type = getattr(chat, "type", None)
                chat_type = getattr(chat_type, "value", chat_type)
                if str(chat_type or "").lower() not in {"group", "supergroup"}:
                    continue
                chat_id = getattr(chat, "id", None)
                if chat_id is None or str(chat_id) in seen_ids:
                    continue
                seen_ids.add(str(chat_id))
                result.append(
                    {
                        "chat_id": str(chat_id),
                        "title": str(getattr(chat, "title", None) or "Без названия"),
                        "chat_type": str(chat_type).lower(),
                    }
                )
            return result
        except Exception as exc:
            print(f"⚠️ Не удалось получить список бесед: {exc}")
            return []
        finally:
            if own_client:
                try:
                    await client.stop()
                except Exception:
                    pass

    @staticmethod
    def _group_route_label(route):
        if str(route.get("destination") or "plagiscan").lower() == "editor" and route.get("editor_nickname"):
            return f"редактор {route['editor_nickname']}"
        return "Plagiscan"

    @classmethod
    async def _select_telegram_group(cls, telegram_client, routes):
        dialogs = await cls.get_telegram_group_dialogs(telegram_client)
        unique_dialogs = []
        seen_ids = set()
        for dialog in dialogs:
            chat_type = str(dialog.get("chat_type") or "").lower()
            if chat_type and chat_type not in {"group", "supergroup"}:
                continue
            chat_id = str(dialog.get("chat_id") or "").strip()
            title = str(dialog.get("title") or "Без названия").strip()
            if not chat_id or chat_id in seen_ids:
                continue
            seen_ids.add(chat_id)
            unique_dialogs.append({"chat_id": chat_id, "title": title})

        if not unique_dialogs:
            print("⚠️ Доступных Telegram-бесед не найдено.")
            return None

        def show_dialogs(items):
            for index, dialog in enumerate(items, 1):
                marker = ""
                if dialog["chat_id"] in routes:
                    marker = f" — уже настроена: {cls._group_route_label(routes[dialog['chat_id']])}"
                print(f"{index}. {dialog['title']}{marker}")

        if len(unique_dialogs) <= 10:
            visible = unique_dialogs
            show_dialogs(visible)
        else:
            visible = unique_dialogs[:10]
            show_dialogs(visible)
            print("11. 🔎 Найти беседу по названию")

        choice = input("Выберите беседу [Enter — назад]: ").strip()
        if not choice:
            return None

        if len(unique_dialogs) > 10 and choice == "11":
            query = input("Введите часть названия: ").strip().lower()
            matches = [dialog for dialog in unique_dialogs if query in dialog["title"].lower()]
            if not matches:
                print("⚠️ Беседы с таким названием не найдены.")
                return None
            show_dialogs(matches)
            choice = input("Выберите беседу [Enter — назад]: ").strip()
            visible = matches

        try:
            index = int(choice) - 1
            return visible[index] if 0 <= index < len(visible) else None
        except (TypeError, ValueError):
            print("❌ Неверный выбор")
            return None

    @classmethod
    async def _refresh_telegram_group_title(cls, telegram_client, routes, chat_id):
        dialogs = await cls.get_telegram_group_dialogs(telegram_client)
        for dialog in dialogs:
            if str(dialog.get("chat_id")) == str(chat_id):
                routes[str(chat_id)]["title"] = str(dialog.get("title") or "Без названия")
                return

    @classmethod
    def _configure_telegram_group_route(cls, routes, dialog, *, is_new):
        chat_id = str(dialog["chat_id"])
        current = routes.get(chat_id) or {}
        current_destination = str(current.get("destination") or "plagiscan").lower()
        current_editor = cls._normalize_nickname(current.get("editor_nickname")) or ""
        current_label = cls._group_route_label(
            {"destination": current_destination, "editor_nickname": current_editor}
        )
        prompt_suffix = "Enter — отменить" if is_new else f"Enter — оставить: {current_label}"
        choice = input(f"Куда отправлять все файлы из беседы «{dialog['title']}»? [1 — Plagiscan, 2 — редактор; {prompt_suffix}]: ").strip()
        if not choice:
            return False
        if choice == "1":
            routes[chat_id] = {"title": dialog["title"], "destination": "plagiscan"}
            return True
        if choice == "2":
            editor = input(f"Ник редактора [Enter: {current_editor or 'указать'}]: ").strip()
            editor = cls._normalize_nickname(editor) or current_editor
            if not editor:
                print("❌ Для маршрута к редактору нужен никнейм")
                return False
            routes[chat_id] = {
                "title": dialog["title"],
                "destination": "editor",
                "editor_nickname": editor,
            }
            return True
        print("❌ Неверный выбор")
        return False

    @classmethod
    async def setup_telegram_group_routes_interactive(cls, telegram_client=None):
        routes = deepcopy(cls.get_setting("telegram_group_routes", {}) or {})
        if not isinstance(routes, dict):
            routes = {}

        def show_saved_routes(prompt, saved_routes):
            print(prompt)
            for index, (_, route) in enumerate(saved_routes, 1):
                print(f"{index}. {route.get('title', 'Без названия')} → {cls._group_route_label(route)}")

        def persist_routes():
            cls.update_setting("telegram_group_routes", cls._normalize_group_routes(routes))
            cls.save_config()

        while True:
            print("\n" + "-" * 30)
            print("💬 TELEGRAM-БЕСЕДЫ")
            print("-" * 30)
            print("Настроенные беседы:")
            if routes:
                for index, route in enumerate(routes.values(), 1):
                    print(f"{index}. {route.get('title', 'Без названия')} → {cls._group_route_label(route)}")
            else:
                print("не заданы")
            print("\n1. Добавить беседу")
            print("2. Изменить беседу")
            print("3. Удалить беседу")
            print("4. Назад")

            choice = input("Выберите [Enter — назад]: ").strip()
            if choice in {"", "4"}:
                break
            if choice == "1":
                dialog = await cls._select_telegram_group(telegram_client, routes)
                if dialog and cls._configure_telegram_group_route(routes, dialog, is_new=dialog["chat_id"] not in routes):
                    persist_routes()
                continue
            if choice == "2":
                if not routes:
                    print("⚠️ Нет настроенных бесед для изменения.")
                    continue
                saved = list(routes.items())
                show_saved_routes("\nВыберите беседу для изменения:", saved)
                selected = input("Номер беседы [Enter — назад]: ").strip()
                if not selected:
                    continue
                try:
                    chat_id, route = saved[int(selected) - 1]
                except (TypeError, ValueError, IndexError):
                    print("❌ Неверный выбор")
                    continue
                dialog = {"chat_id": chat_id, "title": routes[chat_id].get("title", route.get("title", "Без названия"))}
                if cls._configure_telegram_group_route(routes, dialog, is_new=False):
                    await cls._refresh_telegram_group_title(telegram_client, routes, chat_id)
                    persist_routes()
                continue
            if choice == "3":
                if not routes:
                    print("⚠️ Нет настроенных бесед для удаления.")
                    continue
                saved = list(routes.items())
                show_saved_routes("\nВыберите беседу для удаления:", saved)
                selected = input("Номер беседы для удаления [Enter — назад]: ").strip()
                if not selected:
                    continue
                try:
                    chat_id, route = saved[int(selected) - 1]
                except (TypeError, ValueError, IndexError):
                    print("❌ Неверный выбор")
                    continue
                title = route.get("title", "Без названия")
                confirmation = input(f"Удалить беседу «{title}»? [y/N]: ").strip().lower()
                if confirmation != "y":
                    continue
                del routes[chat_id]
                persist_routes()
                print("✅ Беседа удалена")
                continue
            print("❌ Неверный выбор")

    @staticmethod
    def show_current_settings():
        """Показать текущие настройки"""
        # Загружаем актуальные данные
        Config.load_config()

        print("\n📋 ТЕКУЩИЕ НАСТРОКИ:")
        print("-" * 40)

        settings = Config.get_all_settings()
        accounts = Config.get_all_accounts()

        print(f"Режим: {settings.get('mode', 'mode1')}")

        # Авторы
        allowed_authors = settings.get('allowed_authors', [])
        if allowed_authors:
            print(f"Разрешенные авторы: {', '.join(allowed_authors)}")
        else:
            print("Разрешенные авторы: все авторы")

        normal_destination = Config._normalize_normal_destination(settings.get("normal_destination"), settings)
        print(f"Обычные файлы: → {normal_destination}")
        if normal_destination == 'плагискан':
            print(f"  Plagiscan бот: {settings.get('anti_bot', '@plagaiscan_bot')}")
        else:
            normal_editor = settings.get('normal_editor_nickname') or settings.get('editor_nickname', 'не задан')
            print(f"  Редактор: {normal_editor}")

        print(f"Анти-файлы: → {settings.get('anti_destination', 'бот')}")

        if settings.get('anti_destination') == 'бот':
            print(f"  Бот: {settings.get('anti_bot', '@plagaiscan_bot')}")
        else:
            anti_editor = settings.get('anti_editor_nickname') or settings.get('editor_nickname', 'не задан')
            print(f"  Редактор: {anti_editor}")

        forced_authors = settings.get("plagiscan_users", [])
        if forced_authors:
            print(f"Принудительные авторы: {', '.join(forced_authors)}")
        else:
            print("Принудительные авторы: не заданы")
        forced_destination = settings.get("forced_authors_destination", "plagiscan")
        if forced_destination == "editor" and settings.get("forced_authors_editor_nickname"):
            print(f"  Маршрут принудительных авторов: редактор {settings['forced_authors_editor_nickname']}")
        else:
            print("  Маршрут принудительных авторов: Plagiscan")

        group_routes = settings.get("telegram_group_routes", {}) or {}
        print("Telegram-беседы:")
        if group_routes:
            for route in group_routes.values():
                print(f"  {route.get('title', 'Без названия')} → {Config._group_route_label(route)}")
        else:
            print("  не заданы")

        if settings.get('mode') == 'mode2':
            print(f"Круглосуточный редактор: {settings.get('editor_24_7', 'не задан')}")
            print(f"Время работы: {settings.get('time_start', '09:00')} - {settings.get('time_end', '21:00')}")
            print(f"Максимум проверок в рабочем интервале: {settings.get('max_files_per_day', 50)}")

        elif settings.get('mode') == 'mode3':
            print(f"Автор для анализа: {settings.get('counter_author', 'не задан')}")
            pattern = settings.get('counter_pattern', '')
            print(f"Паттерн файлов: {pattern if pattern else 'все файлы'}")
            print(f"Период анализа: {settings.get('counter_days', 7)} дней")

        print(f"Макс. одновременных проверок: {settings.get('max_concurrent', 5)}")
        print("\n🌐 VK / MAX:")
        print(f"VK API версия: {settings.get('vk_api_version', '5.199')}")
        print(f"VK Long Poll group_id: {settings.get('vk_group_id') or 'не задан'}")
        print(f"VK Long Poll wait: {settings.get('vk_longpoll_wait', 25)} сек")
        vk_longpoll_token = settings.get('vk_longpoll_token')
        print(f"VK Long Poll token: {'задан' if vk_longpoll_token else 'использует VK Bot Token / env'}")
        print(f"VK Bot token: {'задан' if settings.get('vk_bot_token') else 'не задан'}")
        print(f"VK Counter token: {'задан' if settings.get('vk_counter_token') else 'не задан'}")
        print(f"MAX Bot token: {'задан' if settings.get('max_bot_token') else 'не задан'}")

        print("\n👥 СТАТУС АККАУНТОВ:")
        for nickname in Config.DEFAULT_ACCOUNTS:
            acc = accounts.get(nickname, {})
            status = "✅ настроен" if acc.get('api_id') and acc.get('api_hash') else "❌ не настроен"
            print(f"  {nickname}: {status}")

        print("-" * 40)

    @staticmethod
    def get_session_name(nickname):
        """Получение имени файла сессии"""
        return str(Config.SESSIONS_DIR / f"{nickname}.session")


class AccountManager:
    """Менеджер для работы с аккаунтами"""

    def __init__(self):
        self.clients = {}
        self.active_mode = "mode1"
        self.file_queue = []
        self.processing_files = {}  # account_name -> file_name
        self.locks = {}  # file_uid -> asyncio.Lock
        self.file_statuses = {}  # file_uid -> status (NEW, IN_PROGRESS, DONE, FAILED)
        self.global_lock = asyncio.Lock()  # Для атомарных операций над статусами
        self.completed_files = {}
        self.initialized = False
        self.waiting_for_new_files = True
        self.blacklisted_accounts = []  # Аккаунты без проверок

    @staticmethod
    def required_account_names():
        return set(Config.DEFAULT_ACCOUNTS)

    async def init_all_clients(self):
        """Инициализация всех клиентов"""
        Config.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        Config.FILES_DIR.mkdir(parents=True, exist_ok=True)

        print("\n🔧 ИНИЦИАЛИЗАЦИЯ АККАУНТОВ:")
        print("-" * 40)

        # Загружаем конфигурацию
        Config.load_config()
        accounts = Config.get_all_accounts()
        required_accounts = self.required_account_names()

        for nickname, creds in accounts.items():
            if nickname not in required_accounts:
                continue
            if creds.get("api_id") and creds.get("api_hash"):
                session_file = Config.get_session_name(nickname)

                client = Client(
                    name=nickname,
                    api_id=creds["api_id"],
                    api_hash=creds["api_hash"],
                    parse_mode=ParseMode.HTML,
                    workdir=str(Config.SESSIONS_DIR),
                    sleep_threshold=0
                )
                self.clients[nickname] = client

                if os.path.exists(session_file):
                    print(f"✅ {nickname}: сессия существует")
                else:
                    print(f"⚠️  {nickname}: сессия будет создана при авторизации")

        self.initialized = True
        print(f"\n✅ Инициализировано {len(self.clients)} аккаунтов")
        return len(self.clients) > 0

    async def authorize_all_accounts(self):
        """Авторизация всех аккаунтов (создание сессий)"""
        print("\n🔐 АВТОРИЗАЦИЯ АККАУНТОВ:")
        print("=" * 50)

        # Сначала загружаем конфиг
        Config.load_config()

        # Убедимся, что папка sessions существует
        try:
            Config.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
            print(f"📁 Папка sessions проверена: {Config.SESSIONS_DIR}")
        except Exception as e:
            print(f"❌ Не удалось создать папку sessions: {e}")
            return

        # Получаем все аккаунты
        accounts = Config.get_all_accounts()
        required_accounts = self.required_account_names()

        if not accounts or not required_accounts:
            print("❌ Нет аккаунтов в конфигурации!")
            return

        for nickname, creds in accounts.items():
            if nickname not in required_accounts:
                continue
            print(f"\n📱 Авторизация аккаунта: {nickname}")
            print("-" * 30)

            try:
                # Проверяем есть ли API данные для аккаунта
                if not creds.get("api_id") or not creds.get("api_hash"):
                    print(f"❌ API данные не настроены для {nickname}")
                    print(f"   Настройте аккаунт в меню (пункт 1)")
                    continue

                # Получаем путь к сессионному файлу
                session_path = Config.get_session_name(nickname)
                print(f"📂 Сессионный файл: {session_path}")

                # Запрашиваем номер телефона если не указан
                phone = creds.get("phone")
                if not phone:
                    phone = input(f"Введите номер телефона для {nickname} (+79123456789): ").strip()
                    if not phone.startswith('+'):
                        phone = '+' + phone

                    # Обновляем в конфиге
                    Config.update_account(nickname, phone=phone)
                    Config.save_config()

                    print(f"📞 Номер телефона сохранен: {phone}")

                # Если сессия уже есть — спрашиваем, нужно ли переавторизовывать
                if os.path.exists(session_path):
                    reauth = input(f"✅ Сессия уже существует. Переавторизовать заново? (y/N): ").strip().lower()
                    if reauth != 'y':
                        print(f"⏭️  Пропускаем {nickname} — сессия уже есть")
                        continue
                    try:
                        os.remove(session_path)
                        print(f"🗑️  Удалена старая сессия")
                    except Exception as e:
                        print(f"⚠️  Не удалось удалить старую сессию: {e}")

                # Создаем новую сессию
                print(f"🔄 Создаю новую сессию для {nickname}...")

                # Создаем клиент
                client = Client(
                    name=nickname,
                    api_id=creds["api_id"],
                    api_hash=creds["api_hash"],
                    phone_number=phone,
                    workdir=str(Config.SESSIONS_DIR)
                )

                try:
                    # Запускаем клиент
                    await client.start()
                    print(f"✅ {nickname}: успешная авторизация!")

                    # Получаем информацию о пользователе
                    me = await client.get_me()
                    print(f"   👤 Имя: {me.first_name}")
                    print(f"   📱 Username: @{me.username}" if me.username else "   📱 Username: не установлен")
                    print(f"   📞 Phone: {me.phone_number}")

                    # Останавливаем клиент
                    await client.stop()

                    # Сохраняем клиент в менеджер
                    self.clients[nickname] = client
                    print(f"💾 Сессия создана для {nickname}")

                except Exception as e:
                    print(f"❌ Ошибка при работе с клиентом {nickname}: {e}")
                    import traceback
                    traceback.print_exc()

            except Exception as e:
                print(f"❌ Общая ошибка авторизации {nickname}: {e}")

        print("\n" + "=" * 50)
        print("✅ АВТОРИЗАЦИЯ ЗАВЕРШЕНА")
        print("=" * 50)

    def get_client(self, nickname):
        """Получение клиента по никнейму"""
        return self.clients.get(nickname)

    def get_available_account(self):
        """Получение доступного аккаунта для обычных файлов"""
        # В режиме 3 не обрабатываем обычные файлы
        if Config.get_setting("mode") == "mode3":
            return None

        # Получаем список аккаунтов для обычных файлов
        normal_accounts = Config.get_setting("normal_accounts") or []
        if isinstance(normal_accounts, str):
            normal_accounts = [item.strip() for item in normal_accounts.split(",") if item.strip()]
        normal_accounts = [
            str(account).strip()
            for account in normal_accounts
            if str(account).strip() in Config.DEFAULT_ACCOUNTS
        ]

        max_concurrent = Config.get_setting("max_concurrent")
        if not max_concurrent:
            max_concurrent = 5

        # Проверяем, сколько обычных аккаунтов уже занято (НИК-1 и НИК-2 не считаются)
        current_processing = len({k: v for k, v in self.processing_files.items() if k not in ("НИК-1", "НИК-2")})
        if current_processing >= max_concurrent:
            print(f"⚠️  Достигнут лимит параллельных проверок: {current_processing}/{max_concurrent}")
            return None

        for account in normal_accounts:
            # Пропускаем заблокированные аккаунты
            if account in self.blacklisted_accounts:
                print(f"⚠️  Аккаунт {account} заблокирован (нет проверок)")
                continue

            if account in self.clients:
                # Проверяем, не обрабатывается ли уже файл в этом аккаунте
                if account not in self.processing_files or not self.processing_files[account]:
                    print(f"✅ Найден свободный аккаунт для обычных файлов: {account}")
                    return account
            else:
                print(f"⚠️  Аккаунт {account} настроен в конфиге, но не инициализирован в clients")

        print(f"❌ Нет свободных аккаунтов из списка: {normal_accounts}")
        print(f"   Доступные клиенты: {list(self.clients.keys())}")
        print(f"   Занятые аккаунты: {list(self.processing_files.keys())}")
        print(f"   Заблокированные: {self.blacklisted_accounts}")
        return None

    def mark_account_busy(self, account, file_name):
        """Пометить аккаунт как занятый обработкой файла"""
        if account not in self.processing_files:
            self.processing_files[account] = []

        # Один аккаунт может ждать несколько результатов с одинаковым именем
        # файла, поэтому храним каждую задачу отдельной записью.
        self.processing_files[account].append(file_name)
        print(f"🔒 Аккаунт {account} занят файлом: {file_name}")

    def mark_account_free(self, account, file_name):
        """Освободить аккаунт после обработки файла"""
        if account in self.processing_files:
            if file_name and file_name in self.processing_files[account]:
                self.processing_files[account].remove(file_name)
                print(f"🔓 Аккаунт {account} освобожден от файла: {file_name}")

            # Если список пустой или file_name пустой, удаляем аккаунт
            if not self.processing_files[account] or not file_name:
                del self.processing_files[account]
                print(f"✅ Аккаунт {account} полностью свободен")

    def blacklist_account(self, account):
        """Добавить аккаунт в черный список (нет проверок)"""
        if account not in self.blacklisted_accounts:
            self.blacklisted_accounts.append(account)
            print(f"⛔ Аккаунт {account} добавлен в черный список (нет проверок)")

            # Освобождаем аккаунт если он в обработке
            self.mark_account_free(account, "")

    def check_all_files_processed(self):
        """Проверить, все ли файлы обработаны"""
        if (not self.file_queue and
                not self.processing_files and
                self.waiting_for_new_files == False):
            self.waiting_for_new_files = True
            print("\n" + "=" * 50)
            print("📭 ОЖИДАЮ НОВЫХ ФАЙЛОВ ОТ КЛИЕНТА...")
            print("=" * 50)
            return True
        return False

    def reset_waiting_status(self):
        """Сбросить статус ожидания при поступлении новых файлов"""
        if self.waiting_for_new_files:
            print("\n" + "=" * 50)
            print("📨 ПОЛУЧЕНЫ НОВЫЕ ФАЙЛЫ!")
            print("=" * 50)
            self.waiting_for_new_files = False

    async def get_file_lock(self, file_uid):
        """Получить или создать Lock для конкретного файла"""
        async with self.global_lock:
            if file_uid not in self.locks:
                self.locks[file_uid] = asyncio.Lock()
            return self.locks[file_uid]

    async def set_file_status(self, file_uid, status):
        """Атомарно установить статус файла"""
        async with self.global_lock:
            self.file_statuses[file_uid] = status
            print(f"📊 Статус файла {file_uid}: {status}")

    async def get_file_status(self, file_uid):
        """Получить текущий статус файла"""
        async with self.global_lock:
            return self.file_statuses.get(file_uid, "NEW")
