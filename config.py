import os
import json
import asyncio
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
        "НИК-3": {"api_id": None, "api_hash": None, "phone": None},
        "НИК-4": {"api_id": None, "api_hash": None, "phone": None},
        "НИК-5": {"api_id": None, "api_hash": None, "phone": None},
        "НИК-6": {"api_id": None, "api_hash": None, "phone": None},
        "НИК-7": {"api_id": None, "api_hash": None, "phone": None},
    }

    DEFAULT_SETTINGS = {
        "mode": "mode1",
        "allowed_authors": [],  # Список авторов, от которых принимаем файлы
        "normal_destination": "бот",
        "anti_destination": "бот",
        "anti_bot": "@plagaiscan_bot",
        "plagiscan_users": [],
        "plagiscan_user_routes": {},
        "ai_bot": "@AAA_Report_AIBot",
        "editor_nickname": None,
        "anti_editor_nickname": None,
        "normal_editor_nickname": None,
        "editor_24_7": None,
        "time_start": "00:00",
        "time_end": "23:59",
        "max_files_per_day": 999,
        "max_concurrent": 7,
        "normal_accounts": ["НИК-3", "НИК-4", "НИК-5", "НИК-6", "НИК-7"],
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
                    accounts_data = data.get("accounts", cls.DEFAULT_ACCOUNTS)
                    for nickname, account_info in accounts_data.items():
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
                    settings_data = data.get("settings", cls.DEFAULT_SETTINGS)
                    cls._SETTINGS = settings_data.copy()

                    # Обновляем DEFAULT значениями если что-то отсутствует
                    for key, value in cls.DEFAULT_SETTINGS.items():
                        if key not in cls._SETTINGS:
                            cls._SETTINGS[key] = value

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
            "accounts": cls.DEFAULT_ACCOUNTS,
            "settings": cls.DEFAULT_SETTINGS
        }
        with open(config_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        # Загружаем созданные данные
        cls._ACCOUNTS = cls.DEFAULT_ACCOUNTS.copy()
        cls._SETTINGS = cls.DEFAULT_SETTINGS.copy()

        print("✅ Создана новая конфигурация по умолчанию")

    @classmethod
    def save_config(cls):
        """Сохранение конфигурации в файл"""
        config_file = cls.CONFIG_FILE
        data = {
            "accounts": cls._ACCOUNTS,
            "settings": cls._SETTINGS
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
    def setup_accounts_interactive():
        """Интерактивная настройка аккаунтов через консоль"""
        print("\n" + "=" * 50)
        print("⚙️  НАСТРОЙКА АККАУНТОВ TELEGRAM")
        print("=" * 50)

        # Загружаем текущие данные
        Config.load_config()

        for nickname in Config._ACCOUNTS.keys():
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

        current_normal_dest = settings.get("normal_destination", "бот")
        print(f"\nКуда отправлять обычные файлы?")
        print("1. В AI бота")
        print("2. Напрямую редактору")
        print("3. В бота @plagaiscan_bot")
        print(f"Текущая настройка: {current_normal_dest}")

        while True:
            normal_choice = input("Выберите (1-3) [Enter чтобы оставить текущий]: ").strip()
            if normal_choice == '1':
                Config.update_setting("normal_destination", "бот")
                break
            elif normal_choice == '2':
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
            elif normal_choice == '3':
                Config.update_setting("normal_destination", "плагискан")
                current_bot = settings.get("anti_bot", "@plagaiscan_bot")
                custom_bot = input(f"Имя Plagiscan бота [Enter: {current_bot}]: ").strip()
                Config.update_setting("anti_bot", custom_bot if custom_bot else current_bot)
                break
            elif not normal_choice and current_normal_dest:
                break
            else:
                print("❌ Неверный выбор")

        # Настройка AI бота
        print("\n" + "-" * 30)
        print("🤖 НАСТРОЙКА AI БОТА")
        print("-" * 30)

        current_ai_bot = settings.get("ai_bot", "@AAA_Report_AIBot")
        ai_bot = input(f"Имя AI бота [Enter: {current_ai_bot}]: ").strip()
        Config.update_setting("ai_bot", ai_bot if ai_bot else current_ai_bot)

        print("\n" + "-" * 30)
        print("🛡️  ПРИНУДИТЕЛЬНАЯ ПРОВЕРКА В ПЛАГИСКАНЕ")
        print("-" * 30)
        print("Задайте принудительных авторов для Плагискан бота с отчетом ИИ")
        print("(для этих пользователей любой файл идет в Плагискан, независимо от слова 'анти')")
        current_plagiscan_users = settings.get("plagiscan_users", [])

        # Разделяем текущих на VK (vk:... / vk.com/...) и TG
        current_vk = []
        current_tg = []
        for u in current_plagiscan_users:
            low = u.lower()
            if (low.startswith("vk:") or low.startswith("vk.com/")
                    or low.startswith("https://vk.com/") or low.startswith("http://vk.com/")):
                current_vk.append(u)
            else:
                current_tg.append(u)

        vk_display = ', '.join(current_vk) if current_vk else 'не задан'
        tg_display = ', '.join(current_tg) if current_tg else 'не задан'

        print(f"\nАвторы из ВК:        {vk_display}")
        vk_input = input(
            "Авторы из ВК (username, @username, 123456789) [Enter оставить, 0 очистить]: "
        ).strip()

        print(f"\nАвторы из телеграмм: {tg_display}")
        tg_input = input(
            "Авторы из телеграмм (@username, username, числовой ID) [Enter оставить, 0 очистить]: "
        ).strip()

        # Обрабатываем VK
        if vk_input == "0":
            new_vk = []
        elif vk_input:
            new_vk = []
            for item in vk_input.split(","):
                item = item.strip().lstrip('@')
                if not item:
                    continue
                low = item.lower()
                # Добавляем vk: если нет явного VK-префикса
                if (low.startswith("vk:") or low.startswith("vk.com/")
                        or low.startswith("https://vk.com/")):
                    new_vk.append(item)
                else:
                    new_vk.append(f"vk:{item}")
        else:
            new_vk = current_vk

        # Обрабатываем TG
        if tg_input == "0":
            new_tg = []
        elif tg_input:
            new_tg = [item.strip() for item in tg_input.split(",") if item.strip()]
        else:
            new_tg = current_tg

        combined = new_vk + new_tg
        Config.update_setting("plagiscan_users", combined)
        if combined:
            print(f"✅ Пользователи Плагискана: {', '.join(combined)}")
            Config.setup_plagiscan_user_routes_interactive(combined)
        else:
            print("✅ Список пользователей Плагискана очищен")

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
    def setup_plagiscan_user_routes_interactive(authors):
        if not authors:
            return

        current_routes = Config.get_setting("plagiscan_user_routes", {}) or {}
        if not isinstance(current_routes, dict):
            current_routes = {}
        routes = dict(current_routes)

        print("\n📌 ИНДИВИДУАЛЬНЫЕ МАРШРУТЫ ПРИНУДИТЕЛЬНЫХ АВТОРОВ")
        print("1. Plagiscan")
        print("2. Конкретный редактор")

        for author in authors:
            current = routes.get(author) or {}
            current_destination = str(current.get("destination", "plagiscan")).lower()
            current_editor = current.get("editor_nickname") or ""
            current_label = "Plagiscan"
            if current_destination == "editor" and current_editor:
                current_label = f"редактор {current_editor}"

            while True:
                choice = input(
                    f"Маршрут для {author} (1 — Plagiscan, 2 — редактор) [Enter: {current_label}]: "
                ).strip()
                if not choice:
                    if current_destination == "editor" and current_editor:
                        routes[author] = {
                            "destination": "editor",
                            "editor_nickname": current_editor,
                        }
                    else:
                        routes[author] = {"destination": "plagiscan"}
                    break
                if choice == "1":
                    routes[author] = {"destination": "plagiscan"}
                    break
                if choice == "2":
                    editor = input(
                        f"Никнейм редактора для {author} [Enter: {current_editor or 'указать'}]: "
                    ).strip()
                    editor = editor or current_editor
                    if editor and not editor.startswith("@"):
                        editor = "@" + editor
                    if editor:
                        routes[author] = {
                            "destination": "editor",
                            "editor_nickname": editor,
                        }
                        break
                    print("❌ Для маршрута к редактору нужен никнейм")
                    continue
                print("❌ Неверный выбор")

        Config.update_setting("plagiscan_user_routes", routes)

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

        print(f"Обычные файлы: → {settings.get('normal_destination', 'бот')}")
        if settings.get('normal_destination') == 'бот':
            print(f"  AI бот: {settings.get('ai_bot', '@AAA_Report_AIBot')}")
        elif settings.get('normal_destination') == 'плагискан':
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

        plagiscan_users = settings.get("plagiscan_users", [])
        if plagiscan_users:
            print(f"Пользователи всегда в Плагискан: {', '.join(plagiscan_users)}")
        else:
            print("Пользователи всегда в Плагискан: не заданы")

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
        print(
            f"Аккаунты для обычных файлов: {', '.join(settings.get('normal_accounts', ['НИК-3', 'НИК-4', 'НИК-5', 'НИК-6', 'НИК-7']))}")

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
        for nickname, acc in accounts.items():
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
        required = {"НИК-1", "НИК-2"}
        normal_destination = Config.get_setting("normal_destination", "бот") or "бот"
        if normal_destination == "бот":
            normal_accounts = Config.get_setting("normal_accounts") or [
                "НИК-3", "НИК-4", "НИК-5", "НИК-6", "НИК-7"
            ]
            if isinstance(normal_accounts, str):
                normal_accounts = [item.strip() for item in normal_accounts.split(",") if item.strip()]
            required.update(str(account).strip() for account in normal_accounts if str(account).strip())
        return required

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
        normal_accounts = Config.get_setting("normal_accounts")
        if not normal_accounts:
            normal_accounts = ["НИК-3", "НИК-4", "НИК-5", "НИК-6", "НИК-7"]

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
