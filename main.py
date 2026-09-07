import asyncio
import signal
import sys
import os
import re
from datetime import datetime, time
from pyrogram_asyncio_compat import ensure_main_event_loop

ensure_main_event_loop()

from pyrogram import filters

from config import Config, AccountManager
from bot_handlers import BotHandlers, FileProcessor
from log_utils import append_channel_log, setup_runtime_logging
from multichannel_gateway.bootstrap import build_store
from multichannel_gateway.integrations.legacy_bridge import (
    claim_external_job_for_legacy,
    job_to_legacy_file_info,
    recover_stale_external_jobs,
    skip_external_jobs_before_start,
)
from multichannel_gateway.vk_longpoll import VkLongPollConfigurationError, VkLongPollSettings, run_vk_longpoll


class ConsoleMenu:
    """Консольное меню управления ботом"""

    @staticmethod
    def normalize_vk_peer_id(raw_value):
        """Extract VK peer_id from a plain id or common VK chat URL."""
        value = (raw_value or "").strip()
        if not value:
            return ""

        convo_match = re.search(r"/im/convo/(-?\d+)", value)
        if convo_match:
            return convo_match.group(1)

        old_chat_match = re.search(r"(?:[?&]sel=|/im\?sel=)c(\d+)", value)
        if old_chat_match:
            return str(2000000000 + int(old_chat_match.group(1)))

        direct_peer_match = re.search(r"(?:[?&]sel=|/im\?sel=)(-?\d+)", value)
        if direct_peer_match:
            return direct_peer_match.group(1)

        return value

    @staticmethod
    def normalize_vk_author(raw_value):
        """Extract a VK username or numeric id from common user inputs."""
        value = (raw_value or "").strip().lstrip('@')
        if not value:
            return ""
        lowered = value.lower()
        if lowered.startswith("vk:"):
            value = value[3:]
            lowered = value.lower()
        for prefix in ("https://vk.com/", "http://vk.com/", "vk.com/"):
            if lowered.startswith(prefix):
                value = value[len(prefix):]
                lowered = value.lower()
                break
        value = value.split("?", 1)[0].split("#", 1)[0].strip("/")
        if value.lower().startswith("id") and value[2:].isdigit():
            value = value[2:]
        return value

    @staticmethod
    def extract_vk_access_token(raw_value):
        """Accept a raw VK token or an OAuth redirect URL and return access_token."""
        value = (raw_value or "").strip()
        if not value:
            return ""

        if "access_token=" not in value:
            return value

        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(value)
        for source in (parsed.fragment, parsed.query):
            token = (parse_qs(source).get("access_token") or [""])[0].strip()
            if token:
                return token

        match = re.search(r"access_token=([^&#]+)", value)
        return match.group(1).strip() if match else value

    @staticmethod
    def setup_editor_24_7(settings):
        """Настройка резервного круглосуточного редактора."""
        raw_24_7 = settings.get("editor_24_7")
        current_24_7 = raw_24_7 if isinstance(raw_24_7, str) and raw_24_7.startswith('@') else ""
        if not current_24_7 and raw_24_7 not in (None, "", False):
            Config.update_setting("editor_24_7", None)

        print(f"\nТекущий круглосуточный редактор: {current_24_7 or 'не задан'}")
        editor_24_7 = input(f"Никнейм круглосуточного редактора [Enter: {current_24_7 or 'не задан'}]: ").strip()
        if editor_24_7:
            if not editor_24_7.startswith('@'):
                editor_24_7 = '@' + editor_24_7
            Config.update_setting("editor_24_7", editor_24_7)
            print(f"✅ Круглосуточный редактор: {editor_24_7}")
        elif current_24_7:
            print(f"✅ Круглосуточный редактор оставлен: {current_24_7}")

    @staticmethod
    def show_main_menu():
        """Показать главное меню"""
        print("\n" + "=" * 60)
        print("🤖 БОТ РАСПРЕДЕЛЕНИЯ ФАЙЛОВ - ГЛАВНОЕ МЕНЮ")
        print("=" * 60)
        print("1. 🔧 Настроить аккаунты Telegram")
        print("2. ⚙️  Настроить редакторов и ботов (общие настройки)")
        print("3. 🔐 Авторизовать аккаунты (создать сессии)")
        print("4. 📋 Показать текущие настройки")
        print("5. 🚀 ЗАПУСТИТЬ БОТА")
        print("6. 🌐 Настроить VK / MAX токены")
        print("7. ❌ Выйти")
        print("8. 💬 Настроить Telegram-беседы")
        print("")
        print("Версия: 1.8")
        print("-" * 60)

    @staticmethod
    def setup_before_launch(
        force_mode=None,
        header_title="⚙️  НАСТРОЙКА ПЕРЕД ЗАПУСКОМ БОТА",
        launch_prompt="Нажмите Enter для запуска бота...",
        allow_exit=False,
    ):
        """Настройка перед запуском бота"""
        print("\n" + "=" * 60)
        print(header_title)
        print("=" * 60)

        # Загружаем текущие настройки
        Config.load_config()
        settings = Config.get_all_settings()

        if force_mode:
            Config.update_setting("mode", force_mode)
        else:
            # Выбор режима работы
            print("\n📊 ВЫБЕРИТЕ РЕЖИМ РАБОТЫ:")
            print("1. Режим 1 - Разделение по типу файлов")
            print("2. Режим 2 - Разделение по времени и количеству")
            print("3. Режим 3 - Счетчик файлов (анализ)")
            print(f"Текущий режим: {settings.get('mode', 'mode1')}")

            mode_choice = input("\nВыберите режим (1-3): ").strip()

            if mode_choice == '1':
                Config.update_setting("mode", "mode1")
                print("✅ Выбран Режим 1: Разделение по типу файлов")
            elif mode_choice == '2':
                Config.update_setting("mode", "mode2")
                print("✅ Выбран Режим 2: Разделение по времени и количеству")
            elif mode_choice == '3':
                Config.update_setting("mode", "mode3")
                print("✅ Выбран Режим 3: Счетчик файлов")
            else:
                print("⚠️  Используется текущий режим")

        current_mode = Config.get_setting("mode")

        # Настройка авторов (для режимов 1 и 2)
        if current_mode in ["mode1", "mode2"]:
            print("\n" + "-" * 40)
            print("👤 НАСТРОЙКА АВТОРОВ")
            print("-" * 40)

            current_authors = settings.get("allowed_authors", [])

            # Разделяем текущих на VK и TG
            current_vk_a = [u for u in current_authors if u.lower().startswith("vk:") or u.lower().startswith("vk.com/")]
            current_tg_a = [u for u in current_authors if not (u.lower().startswith("vk:") or u.lower().startswith("vk.com/"))]

            print("\n⚠️  ВНИМАНИЕ: Если оставить оба поля пустыми, бот будет принимать файлы от ЛЮБЫХ пользователей!")

            vk_display_a = ', '.join(current_vk_a) if current_vk_a else 'не задан'
            tg_display_a = ', '.join(current_tg_a) if current_tg_a else 'не задан'

            print(f"\nАвторы из ВК:        {vk_display_a}")
            vk_input_a = input("Авторы из ВК (username, @username, 123456789) [Enter оставить, 0 очистить]: ").strip()

            print(f"\nАвторы из телеграмм: {tg_display_a}")
            tg_input_a = input("Авторы из телеграмм (@username, username, числовой ID) [Enter оставить, 0 очистить]: ").strip()

            # Обрабатываем VK
            if vk_input_a == "0":
                new_vk_a = []
            elif vk_input_a:
                new_vk_a = []
                for item in vk_input_a.split(","):
                    item = item.strip().lstrip('@')
                    if not item:
                        continue
                    low = item.lower()
                    if low.startswith("vk:") or low.startswith("vk.com/") or low.startswith("https://vk.com/"):
                        new_vk_a.append(item)
                    else:
                        new_vk_a.append(f"vk:{item}")
            else:
                new_vk_a = current_vk_a

            # Обрабатываем TG (убираем @ в начале)
            if tg_input_a == "0":
                new_tg_a = []
            elif tg_input_a:
                new_tg_a = []
                for item in tg_input_a.split(","):
                    item = item.strip()
                    if not item:
                        continue
                    new_tg_a.append(item[1:] if item.startswith('@') else item)
            else:
                new_tg_a = current_tg_a

            combined_authors = new_vk_a + new_tg_a
            Config.update_setting("allowed_authors", combined_authors)
            if combined_authors:
                print(f"✅ Установлены авторы: {', '.join(combined_authors)}")
            else:
                print("⚠️  Авторы не заданы - бот будет принимать файлы от всех пользователей!")

        if current_mode == "mode1":
            print("\n" + "-" * 40)
            print("🌙 НАСТРОЙКА КРУГЛОСУТОЧНОГО РЕДАКТОРА")
            print("-" * 40)
            print("Используется как резерв, если основной редактор сообщил, что проверки закончились.")
            ConsoleMenu.setup_editor_24_7(settings)

        # Дополнительные настройки для режима 2
        if current_mode == "mode2":
            print("\n" + "-" * 40)
            print("⏰ НАСТРОЙКА РЕЖИМА 2")
            print("-" * 40)

            # Время работы
            current_start = settings.get("time_start", "09:00")
            current_end = settings.get("time_end", "21:00")

            print(f"\nТекущее время работы: {current_start} - {current_end}")

            time_start = input(f"Время начала (ЧЧ:ММ) [Enter: {current_start}]: ").strip()
            time_end = input(f"Время окончания (ЧЧ:ММ) [Enter: {current_end}]: ").strip()

            # Проверка формата времени
            def is_valid_time(time_str):
                try:
                    time.fromisoformat(time_str)
                    return True
                except ValueError:
                    return False

            if time_start and is_valid_time(time_start):
                Config.update_setting("time_start", time_start)
            elif not time_start:
                Config.update_setting("time_start", current_start)

            if time_end and is_valid_time(time_end):
                Config.update_setting("time_end", time_end)
            elif not time_end:
                Config.update_setting("time_end", current_end)

            # Максимальное количество файлов
            try:
                current_max = settings.get("max_files_per_day", 50)
                print(f"\nТекущий заданный лимит проверок в рабочем интервале: {current_max}")

                max_files = input(f"Максимум проверок в рабочем интервале [Enter: {current_max}]: ").strip()
                if max_files:
                    Config.update_setting("max_files_per_day", int(max_files))
                    print(f"✅ Лимит установлен: {max_files} проверок в рабочем интервале")
            except ValueError:
                print("⚠️  Используется текущее значение")

            # Круглосуточный редактор
            ConsoleMenu.setup_editor_24_7(settings)

            # Редактор для рабочего времени
            raw_work_editor = Config.get_setting("editor_nickname")
            current_work_editor = raw_work_editor if isinstance(raw_work_editor, str) and raw_work_editor.startswith('@') else ""
            print(f"\nТекущий редактор для рабочего времени: {current_work_editor or 'не задан'}")

            work_editor = input(
                f"Никнейм редактора для рабочего времени [Enter: {current_work_editor or 'не задан'}, 0 очистить]: "
            ).strip()
            if work_editor == "0":
                Config.update_setting("editor_nickname", None)
                current_work_editor = ""
                print("✅ Редактор для рабочего времени очищен")
            elif work_editor:
                if not work_editor.startswith('@'):
                    work_editor = '@' + work_editor
                Config.update_setting("editor_nickname", work_editor)
                current_work_editor = work_editor
                print(f"✅ Редактор для рабочего времени: {work_editor}")
            elif current_work_editor:
                print(f"✅ Редактор для рабочего времени оставлен: {current_work_editor}")

            # Маршрут обычных файлов в рабочее время
            print("\n📄 КУДА ОТПРАВЛЯТЬ ОБЫЧНЫЕ ФАЙЛЫ В РАБОЧЕЕ ВРЕМЯ?")
            print("1. Бот AAA")
            print("2. Редактор")
            print("3. Плагискан")
            current_normal_dest = Config.get_setting("normal_destination", "бот")
            print(f"Текущая настройка: {current_normal_dest}")

            while True:
                normal_choice = input("Выберите (1-3) [Enter оставить текущую]: ").strip()
                if normal_choice == '1':
                    Config.update_setting("normal_destination", "бот")
                    current_ai_bot = Config.get_setting("ai_bot", "@AAA_Report_AIBot")
                    ai_bot = input(f"Имя AAA бота [Enter: {current_ai_bot}]: ").strip()
                    Config.update_setting("ai_bot", ai_bot if ai_bot else current_ai_bot)
                    break
                elif normal_choice == '2':
                    Config.update_setting("normal_destination", "редактор")
                    if not Config.get_setting("editor_nickname"):
                        editor = input("Никнейм редактора для рабочего времени: ").strip()
                        if editor and not editor.startswith('@'):
                            editor = '@' + editor
                        Config.update_setting("editor_nickname", editor or None)
                    break
                elif normal_choice == '3':
                    Config.update_setting("normal_destination", "плагискан")
                    current_bot = Config.get_setting("anti_bot", "@plagaiscan_bot")
                    custom_bot = input(f"Имя Plagiscan бота [Enter: {current_bot}]: ").strip()
                    Config.update_setting("anti_bot", custom_bot if custom_bot else current_bot)
                    break
                elif not normal_choice and current_normal_dest:
                    break
                else:
                    print("❌ Неверный выбор")

            # Маршрут анти-файлов в рабочее время
            print("\n🛡️  КУДА ОТПРАВЛЯТЬ АНТИ-ФАЙЛЫ В РАБОЧЕЕ ВРЕМЯ?")
            print("1. Плагискан")
            print("2. Редактор")
            current_anti_dest = Config.get_setting("anti_destination", "бот")
            print(f"Текущая настройка: {current_anti_dest}")

            while True:
                anti_choice = input("Выберите (1-2) [Enter оставить текущую]: ").strip()
                if anti_choice == '1':
                    Config.update_setting("anti_destination", "бот")
                    current_bot = Config.get_setting("anti_bot", "@plagaiscan_bot")
                    custom_bot = input(f"Имя Plagiscan бота [Enter: {current_bot}]: ").strip()
                    Config.update_setting("anti_bot", custom_bot if custom_bot else current_bot)
                    break
                elif anti_choice == '2':
                    Config.update_setting("anti_destination", "редактор")
                    if not Config.get_setting("editor_nickname"):
                        editor = input("Никнейм редактора для рабочего времени: ").strip()
                        if editor and not editor.startswith('@'):
                            editor = '@' + editor
                        Config.update_setting("editor_nickname", editor or None)
                    break
                elif not anti_choice and current_anti_dest:
                    break
                else:
                    print("❌ Неверный выбор")

            print("\n📌 В режиме 2 принудительные авторы всегда идут в Плагискан.")
            print("   Второй ИИ-отчет выдается только принудительным авторам, если ИИ > 0.")

        # Настройки для режима 3
        elif current_mode == "mode3":
            print("\n" + "-" * 40)
            print("📊 НАСТРОЙКА РЕЖИМА 3 - СЧЕТЧИК ФАЙЛОВ (ИНТЕРАКТИВНЫЙ)")
            print("-" * 40)

            # Выбор типа анализа
            current_type = Config.get_setting("counter_type") or "author"
            cur_type_label = "автор" if current_type == "author" else "редактор"
            print(f"\n📌 Тип анализа:")
            if allow_exit:
                print("   0 - Выйти из счетчика")
            print("   1 - Анализ по АВТОРУ (сколько файлов пришло и ушло)")
            print("   2 - Анализ по РЕДАКТОРУ (сколько отправили и сколько вернул)")
            choices_label = "0/1/2" if allow_exit else "1/2"
            type_choice = input(f"Выберите тип [{choices_label}, Enter = оставить текущий ({cur_type_label})]: ").strip()
            if allow_exit and type_choice == "0":
                print("✅ Счетчик закрыт")
                return False
            elif type_choice == "1":
                counter_type = "author"
            elif type_choice == "2":
                counter_type = "editor"
            else:
                counter_type = current_type

            if counter_type == "editor":
                current_editor_nick = Config.get_setting("counter_editor_nick") or ""
                print(f"\nТекущий ник редактора: {current_editor_nick or 'не задан'}")
                editor_nick = input("👤 Введите ник редактора (TG @username): ").strip()
                if not editor_nick:
                    if current_editor_nick:
                        editor_nick = current_editor_nick
                    else:
                        print("❌ Ник редактора не может быть пустым")
                        return False
                Config.update_setting("counter_editor_nick", editor_nick)
                Config.update_setting("counter_type", "editor")
            else:
                # Шаг 1: выбор платформы (показываем только платформу, без ника)
                current_author = Config.get_setting("counter_author") or ""
                saved_vk_author = Config.get_setting("counter_vk_author") or (
                    current_author if current_author.lower().startswith("vk:") else ""
                )
                saved_tg_author = Config.get_setting("counter_tg_author") or (
                    current_author if current_author and not current_author.lower().startswith("vk:") else ""
                )
                if current_author.lower().startswith("vk:"):
                    cur_platform_hint = "Enter = оставить текущую (ВКонтакте)"
                    cur_platform = "vk"
                elif current_author:
                    cur_platform_hint = "Enter = оставить текущую (Телеграмм)"
                    cur_platform = "tg"
                else:
                    cur_platform_hint = "не задан"
                    cur_platform = None

                platform_choice = input(f"\nПлатформа автора [1 - ВКонтакте / 2 - Телеграмм, {cur_platform_hint}]: ").strip()

                if platform_choice == "1" or (platform_choice == "" and cur_platform == "vk"):
                    cur_vk_nick = saved_vk_author.replace("vk:", "") if saved_vk_author.lower().startswith("vk:") else saved_vk_author
                    hint = f"[Enter = оставить ({cur_vk_nick})]" if cur_vk_nick else ""
                    raw = input(f"Введите ВК (username или числовой ID) {hint}: ").strip().lstrip('@')
                    if not raw:
                        if cur_vk_nick:
                            raw = cur_vk_nick
                        else:
                            print("❌ Автор не может быть пустым")
                            return False
                    raw = ConsoleMenu.normalize_vk_author(raw)
                    if not raw:
                        print("❌ Автор не может быть пустым")
                        return False
                    author = f"vk:{raw}"
                    Config.update_setting("counter_vk_author", author)
                    current_peer_id = Config.get_setting("counter_vk_peer_id") or ""
                    current_scope = (Config.get_setting("counter_vk_scope") or "").strip().lower()
                    current_place = (
                        "личные сообщения/сообщения группы"
                        if current_scope == "direct"
                        else "беседа ВК" if current_peer_id else "личные сообщения/сообщения группы"
                    )
                    print("\nГде автор отправлял файлы?")
                    print("   1 - В беседе ВК")
                    print("   2 - В личные сообщения группы / сообщения сообщества")
                    place_choice = input(f"Выберите [1/2, Enter = оставить текущее ({current_place})]: ").strip()
                    selected_place = place_choice or ("2" if current_scope == "direct" else "1" if current_peer_id else "2")

                    if selected_place == "1":
                        print("\nОткройте нужную беседу ВК и скопируйте ссылку из адресной строки.")
                        print("Пример ссылки:")
                        print("https://vk.com/im/convo/2000000002?entrypoint=list_all")
                        print("Можно вставить всю ссылку целиком или только число 2000000002.")
                        peer_prompt = (
                            f"Ссылка на беседу или peer_id [Enter = оставить {current_peer_id}]: "
                            if current_peer_id else
                            "Ссылка на беседу или peer_id: "
                        )
                        while True:
                            raw_peer_id = input(peer_prompt).strip()
                            if allow_exit and raw_peer_id == "0":
                                print("✅ Счетчик закрыт")
                                return False
                            if not raw_peer_id and current_peer_id:
                                peer_id = current_peer_id
                            else:
                                peer_id = ConsoleMenu.normalize_vk_peer_id(raw_peer_id)
                            if peer_id and re.fullmatch(r"-?\d+", peer_id):
                                break
                            print(f"❌ Не удалось понять peer_id: {raw_peer_id or 'пусто'}")
                            print("Введите ссылку на беседу или peer_id еще раз.")
                            if allow_exit:
                                print("Для выхода из счетчика введите 0.")
                        Config.update_setting("counter_vk_peer_id", peer_id)
                        Config.update_setting("counter_vk_scope", "chat")
                    elif selected_place == "2":
                        Config.update_setting("counter_vk_scope", "direct")
                    else:
                        print("❌ Неверный выбор места отправки файлов")
                        return False
                elif platform_choice == "2" or (platform_choice == "" and cur_platform == "tg"):
                    cur_tg_nick = saved_tg_author or (current_author if cur_platform == "tg" else "")
                    hint = f"[Enter = оставить ({cur_tg_nick})]" if cur_tg_nick else ""
                    raw = input(f"Введите Telegram username (можно с @) {hint}: ").strip().lstrip('@')
                    if not raw:
                        if cur_tg_nick:
                            raw = cur_tg_nick
                        else:
                            print("❌ Автор не может быть пустым")
                            return False
                    author = raw
                    Config.update_setting("counter_tg_author", author)
                elif platform_choice == "":
                    print("❌ Автор не задан. Выберите платформу.")
                    return False
                else:
                    print("❌ Неверный выбор платформы")
                    return False
                Config.update_setting("counter_author", author)
                Config.update_setting("counter_type", "author")

            # Ввод даты для анализа (начало диапазона)
            today = datetime.now().strftime("%Y-%m-%d")
            current_date = Config.get_setting("counter_date")
            if current_date:
                print(f"\n📅 Текущая начальная дата: {current_date}")

            date_str = input(
                f"📅 Начальная дата (ГГГГ-ММ-ДД, например {today}, или Enter для сегодня): ").strip()
            if date_str:
                try:
                    datetime.strptime(date_str, "%Y-%m-%d")
                    counter_date = date_str
                except ValueError:
                    print(f"❌ Неверный формат даты. Используется сегодняшняя дата: {today}")
                    counter_date = today
            else:
                counter_date = ""  # пустая строка = всегда использовать текущую дату при запуске

            print("\n--- НАЧАЛО ДИАПАЗОНА ---")
            start_file = input(
                "📄 Начальный файл (включительно, или Enter чтобы начать с начала): ").strip()
            start_time_str = input(
                "⏰ Начальное время (ЧЧ:ММ, или Enter для 00:00): ").strip()

            print("\n--- КОНЕЦ ДИАПАЗОНА ---")
            current_end_date = Config.get_setting("counter_end_date") or ""
            if current_end_date:
                print(f"📅 Текущая конечная дата: {current_end_date}")
            end_date_str = input(
                f"📅 Конечная дата (ГГГГ-ММ-ДД, или Enter для текущего момента): ").strip()

            current_end_file = Config.get_setting("counter_end_file") or ""
            if current_end_file:
                print(f"📄 Текущий конечный файл: {current_end_file}")
            end_file = input(
                "📄 Конечный файл (включительно, или Enter чтобы считать до конца): ").strip()

            end_time_str = input(
                "⏰ Конечное время (ЧЧ:ММ, или Enter для текущего момента): ").strip()

            # Сохраняем общие параметры диапазона
            Config.update_setting("counter_date", counter_date)
            Config.update_setting("counter_start_file", start_file)
            Config.update_setting("counter_start_time", start_time_str if start_time_str else "")
            Config.update_setting("counter_end_date", end_date_str)
            Config.update_setting("counter_end_file", end_file)
            Config.update_setting("counter_end_time", end_time_str if end_time_str else "")

            print(f"\n✅ Параметры режима 3 установлены:")
            if counter_type == "editor":
                print(f"   👤 Редактор: {editor_nick}")
            else:
                print(f"   👤 Автор: {author}")
                if author.lower().startswith("vk:"):
                    peer_id = Config.get_setting("counter_vk_peer_id") or ""
                    scope = (Config.get_setting("counter_vk_scope") or "").strip().lower()
                    if scope == "direct":
                        print("   🌐 VK беседа: не используется, считаем сообщения группы/личку")
                    else:
                        print(f"   🌐 VK беседа: {peer_id or 'не задана, считаем сообщения группы/личку'}")
            print(f"   📅 Начало: {counter_date or 'сегодня'} {start_time_str or '00:00'} | Файл: {start_file or 'с первого'}")
            print(f"   📅 Конец:  {end_date_str or 'текущий момент'} {end_time_str or ''} | Файл: {end_file or 'до последнего'}")
            print(f"   🔍 Фильтрация: все файлы (кроме платежных документов)")

        # Сохранение настроек
        Config.save_config()

        print("\n" + "=" * 60)
        print("✅ НАСТРОКИ СОХРАНЕНЫ!")
        print("=" * 60)

        # Показываем итоговые настройки
        print("\n📋 ИТОГОВЫЕ НАСТРОКИ:")
        print("-" * 40)

        final_settings = Config.get_all_settings()
        print(f"Режим: {final_settings.get('mode')}")

        if final_settings.get('mode') in ["mode1", "mode2"]:
            authors = final_settings.get('allowed_authors', [])
            if authors:
                print(f"Авторы: {', '.join(authors)}")
            else:
                print(f"Авторы: все пользователи")

        if final_settings.get('mode') == "mode1":
            editor = final_settings.get('editor_24_7')
            editor_display = editor if isinstance(editor, str) and editor.startswith('@') else None
            print(f"Круглосуточный редактор: {editor_display or 'не задан'}")

        if final_settings.get('mode') == "mode2":
            print(f"Время работы: {final_settings.get('time_start')} - {final_settings.get('time_end')}")
            print(f"Лимит проверок: {final_settings.get('max_files_per_day')} в рабочем интервале")
            editor = final_settings.get('editor_24_7')
            editor_display = editor if isinstance(editor, str) and editor.startswith('@') else None
            print(f"Круглосуточный редактор: {editor_display or 'не задан'}")
            work_editor = final_settings.get('editor_nickname')
            work_editor_display = work_editor if isinstance(work_editor, str) and work_editor.startswith('@') else None
            print(f"Редактор рабочего времени: {work_editor_display or 'не задан'}")
            print(f"Обычные файлы в рабочее время: {final_settings.get('normal_destination', 'бот')}")
            if final_settings.get('normal_destination') == "бот":
                print(f"  AAA бот: {final_settings.get('ai_bot', '@AAA_Report_AIBot')}")
            elif final_settings.get('normal_destination') == "плагискан":
                print(f"  Plagiscan бот: {final_settings.get('anti_bot', '@plagaiscan_bot')}")
            print(f"Анти-файлы в рабочее время: {final_settings.get('anti_destination', 'бот')}")
            if final_settings.get('anti_destination') == "бот":
                print(f"  Plagiscan бот: {final_settings.get('anti_bot', '@plagaiscan_bot')}")

        elif final_settings.get('mode') == "mode3":
            counter_type = final_settings.get("counter_type", "author")
            if counter_type == "editor":
                print(f"Редактор для анализа: {final_settings.get('counter_editor_nick')}")
            else:
                print(f"Автор для анализа: {final_settings.get('counter_author')}")
            pattern = final_settings.get('counter_pattern', '')
            print(f"Паттерн файлов: {pattern if pattern else 'все файлы'}")

        print("-" * 40)

        input(f"\n{launch_prompt}")
        return True


class FileDistributionBot:
    """Главный класс бота распределения файлов"""

    def __init__(self):
        self.manager = AccountManager()
        self.handlers = BotHandlers(self.manager)
        self.running = False
        self.task = None
        self.gateway_store = build_store()
        self.vk_longpoll_task = None
        self.external_start_iso = None

    @staticmethod
    def _telegram_sender_matches_configured_bot(sender, configured_bot):
        sender_clean = str(sender or "").replace("@", "").lower()
        bot_clean = str(configured_bot or "").replace("@", "").lower()
        return bool(bot_clean and sender_clean == bot_clean)

    def _handle_editor_no_checks_notice(self, sender, text):
        if not self.handlers._is_no_checks_message(text):
            return False

        self.handlers._mark_editor_unavailable(sender)
        print(f"⛔ Редактор @{sender} сообщил, что проверки закончились")
        print("⛔ Новые файлы этому редактору больше не отправляем")
        print("ℹ️ Уже отправленные файлы остаются в ожидании: если редактор пришлет PDF, бот доставит его автору")
        return True

    async def initialize(self):
        """Инициализация бота"""
        print("\n" + "=" * 60)
        print("🤖 ИНИЦИАЛИЗАЦИЯ БОТА РАСПРЕДЕЛЕНИЯ ФАЙЛОВ")
        print("=" * 60)
        append_channel_log("main", "🤖 ИНИЦИАЛИЗАЦИЯ БОТА РАСПРЕДЕЛЕНИЯ ФАЙЛОВ")

        # Загрузка конфигурации
        Config.load_config()

        # Создаем необходимые папки
        Config.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        Config.FILES_DIR.mkdir(parents=True, exist_ok=True)

        # Проверяем режим работы
        current_mode = Config.get_setting("mode")

        if current_mode == "mode3":
            # Для режима 3 не нужны все аккаунты
            print("✅ Режим 3 (Счетчик файлов) активирован")
            append_channel_log("main", "✅ Режим 3 (Счетчик файлов) активирован")
            return True

        # Для режимов 1 и 2 проверяем настройки аккаунтов
        accounts = Config.get_all_accounts()

        # Проверяем настройки аккаунтов
        accounts_configured = False
        configured_accounts = []

        required_accounts = self.manager.required_account_names()
        for nickname, account in accounts.items():
            if nickname not in required_accounts:
                continue
            if account.get("api_id") and account.get("api_hash"):
                configured_accounts.append(nickname)

        accounts_configured = len(configured_accounts) > 0

        if not accounts_configured:
            print("⚠️  Аккаунты не настроены!")
            print("Сначала настройте аккаунты (пункт 1 меню)")
            append_channel_log("main", "⚠️  Аккаунты не настроены!")
            return False

        # Инициализация клиентов
        await self.manager.init_all_clients()

        # Проверяем существование сессий
        session_count = 0
        for nickname in configured_accounts:
            session_file = Config.get_session_name(nickname)
            if os.path.exists(session_file):
                session_count += 1

        print(f"\n📁 Найдено сессий: {session_count}/{len(configured_accounts)}")

        if session_count < len(configured_accounts):
            print("⚠️  Не все аккаунты авторизованы!")
            print("Выполните авторизацию (пункт 3 меню)")
            return False

        print("\n✅ ИНИЦИАЛИЗАЦИЯ ЗАВЕРШЕНА")
        append_channel_log("main", "✅ ИНИЦИАЛИЗАЦИЯ ЗАВЕРШЕНА")

        if current_mode != "mode3":
            print("📥 Ожидаю файлы от клиентов в НИК-1...")
            append_channel_log("main", "📥 Ожидаю файлы от клиентов в НИК-1...")

        return True

    async def run_bot(self):
        """Запуск бота в рабочем режиме"""
        # Настройка перед запуском
        if not ConsoleMenu.setup_before_launch():
            return

        # Инициализируем бота
        if not await self.initialize():
            print("\n❌ Не удалось инициализировать бота")
            input("\nНажмите Enter для возврата в меню...")
            return

        # Проверяем режим работы
        current_mode = Config.get_setting("mode")

        if current_mode == "mode3":
            # Запускаем режим 3 напрямую через handlers
            try:
                await self.handlers.run_counter_mode()
            except KeyboardInterrupt:
                print("\n\n🔄 Возвращаюсь в главное меню...")
            except Exception as e:
                print(f"\n❌ Ошибка в режиме 3: {e}")
                import traceback
                traceback.print_exc()
            return  # Режим 3 завершён, возвращаемся в меню

        self.running = True

        print("\n" + "=" * 60)
        print("🚀 ЗАПУСК БОТА В РАБОЧЕМ РЕЖИМЕ")
        print("=" * 60)
        print(f"Режим работы: {current_mode}")
        print("Бот теперь обрабатывает файлы автоматически.")
        print("Для возврата в меню нажмите Ctrl+C")
        print("-" * 60)
        append_channel_log("main", f"🚀 ЗАПУСК БОТА В РАБОЧЕМ РЕЖИМЕ: {current_mode}")

        # Получаем настройки
        settings = Config.get_all_settings()
        self.external_start_iso = datetime.now().astimezone().isoformat()

        # Запускаем все клиенты
        clients_started = []

        for nickname, client in self.manager.clients.items():
            try:
                await client.start()
                clients_started.append(nickname)
                print(f"✅ Аккаунт запущен: {nickname}")
                append_channel_log("main", f"✅ Аккаунт запущен: {nickname}")

                # Настраиваем обработчики для каждого клиента
                # 1. Основной обработчик для НИК-1 (прием файлов от клиентов)
                if nickname == "НИК-1":
                    @client.on_message(filters.document | filters.text)
                    async def main_handler(client, message):
                        await self.handlers.handle_main_account(client, message)

                # 2. Обработчик ответов от редактора для НИК-2 
                # (Теперь ВСЕГДА нужен, так как ошибки AAA уходят редактору через НИК-2)
                if nickname == "НИК-2":
                    @client.on_message(filters.document | filters.text)
                    async def editor_response_handler(client, message):
                        sender = message.from_user.username or str(message.from_user.id) if message.from_user else ""
                        anti_bot = settings.get("anti_bot", "@plagaiscan_bot")
                        if self._telegram_sender_matches_configured_bot(sender, anti_bot):
                            await self.handlers.handle_anti_bot_response(client, message)
                            return

                        if message.text and not message.document:
                            if self._handle_editor_no_checks_notice(sender, message.text):
                                return

                            tracking_key, matched_info, reason = self.handlers.find_editor_tracking_for_text_response(
                                sender,
                                message.reply_to_message_id,
                                sent_from_account="НИК-2",
                                message_text=message.text,
                                reply_chat_id=getattr(getattr(message, "chat", None), "id", None),
                            )
                            if matched_info is None:
                                return
                            print(
                                f"ℹ️ Текст от редактора {sender} привязан: "
                                f"{tracking_key} ({reason})"
                            )
                            if not message.reply_to_message_id:
                                message.reply_to_message_id = matched_info["reply_to_message_id"]
                            await self.handlers.handle_editor_response(client, message)
                            return

                        if message.document and message.document.file_name.lower().endswith('.pdf'):
                            if message.reply_to_message_id:
                                await self.handlers.handle_editor_response(client, message)
                            else:
                                doc_name = message.document.file_name
                                tracking_key, matched_info, reason = self.handlers.find_editor_tracking_for_unreplied_pdf(
                                    sender,
                                    doc_name,
                                    sent_from_account="НИК-2",
                                )
                                if matched_info is None:
                                    print(
                                        f"⚠️ PDF от редактора {sender} не привязан: "
                                        f"{doc_name}, {reason}"
                                    )
                                else:
                                    print(
                                        f"ℹ️ PDF от редактора {sender} привязан без reply: "
                                        f"{doc_name} -> {tracking_key} ({reason})"
                                    )
                                    message.reply_to_message_id = matched_info["reply_to_message_id"]
                                    await self.handlers.handle_editor_response(client, message)

                normal_accounts = settings.get("normal_accounts") or []
                if nickname in normal_accounts:
                    ai_bot = settings.get("ai_bot", "@AAA_Report_AIBot")
                    @client.on_message(filters.chat(ai_bot))
                    async def ai_bot_handler(client, message):
                        await self.handlers.handle_ai_bot_response(client, message)

                # 4. Обработчик ответов от антиплагиат бота для НИК-2
                # Регистрируем всегда когда задан anti_bot, т.к. anti_destination может быть "бот"
                # динамически (force_plagiscan / anti-файлы) даже если в конфиге не выставлено.
                if nickname == "НИК-2":
                    anti_bot = settings.get("anti_bot", "@plagaiscan_bot")
                    if anti_bot:
                        @client.on_message(filters.chat(anti_bot))
                        async def anti_bot_handler(client, message):
                            await self.handlers.handle_anti_bot_response(client, message)

            except Exception as e:
                print(f"❌ Ошибка запуска {nickname}: {e}")
                append_channel_log("main", f"❌ Ошибка запуска {nickname}: {e}")

        if not clients_started:
            print("❌ Не удалось запустить ни одного аккаунта")
            await self.stop_all_clients()
            return

        await self.start_external_ingress()

        # Настройка обработчика Ctrl+C
        loop = asyncio.get_event_loop()

        def signal_handler():
            print("\n\n🛑 Получен сигнал Ctrl+C")
            self.running = False
            if self.task and not self.task.done():
                self.task.cancel()

        # Добавляем обработчик для Windows
        if sys.platform == 'win32':
            try:
                import win32api
                def win_signal_handler(sig, func=None):
                    signal_handler()

                win32api.SetConsoleCtrlHandler(win_signal_handler, True)
            except ImportError:
                # Если win32api не установлен, используем стандартный обработчик
                try:
                    for sig in (signal.SIGINT, signal.SIGTERM):
                        loop.add_signal_handler(sig, signal_handler)
                except NotImplementedError:
                    pass

        # Для Unix-систем
        else:
            try:
                for sig in (signal.SIGINT, signal.SIGTERM):
                    loop.add_signal_handler(sig, signal_handler)
            except NotImplementedError:
                pass

        # Основной цикл обработки
        try:
            while self.running:
                # Создаем задачу для обработки очереди
                self.task = asyncio.create_task(self.process_queue_loop())

                try:
                    await self.task
                except asyncio.CancelledError:
                    print("🔄 Задача обработки отменена")
                    append_channel_log("main", "🔄 Задача обработки отменена")
                    break
                except Exception as e:
                    print(f"❌ Ошибка в задаче: {e}")
                    append_channel_log("main", f"❌ Ошибка в задаче: {e}")
                    break

                # Небольшая пауза перед следующей итерацией
                await asyncio.sleep(1)

        except KeyboardInterrupt:
            print("\n\n⚠️  Получено прерывание клавиатуры")
        except Exception as e:
            print(f"\n❌ Ошибка в основном цикле: {e}")
        finally:
            await self.stop_all_clients()
            print("\n✅ Возвращаюсь в главное меню...")

    async def process_queue_loop(self):
        """Цикл обработки очереди"""
        _last_status_ts = 0.0
        _last_recovery_ts = 0.0
        while self.running:
            now_ts = datetime.now().timestamp()
            if now_ts - _last_recovery_ts >= 60.0:
                recovered = await recover_stale_external_jobs(older_than_seconds=900)
                if recovered:
                    print(f"🔄 Возвращено из stale processing во внешней очереди: {recovered}")
                    append_channel_log("gateway", f"🔄 Возвращено из stale processing во внешней очереди: {recovered}")
                _last_recovery_ts = now_ts

            await self.import_external_jobs()

            # Проверяем очередь каждые 3 секунды
            if self.manager.file_queue:
                await self.handlers.process_queue()

            # Показываем статус каждые 10 секунд (надёжный таймер)
            now = datetime.now().timestamp()
            if now - _last_status_ts >= 10.0:
                self.show_status()
                _last_status_ts = now

            await asyncio.sleep(3)

    async def import_external_jobs(self, limit=3):
        imported = 0
        while imported < limit:
            job = await claim_external_job_for_legacy("legacy-bot")
            if not job:
                return

            if not os.path.exists(job.file_path):
                await asyncio.to_thread(
                    self.gateway_store.mark_failed,
                    job.job_id,
                    f"bridge file missing: {job.file_path}",
                    300,
                )
                continue

            file_uid = job.dedupe_key
            file_lock = await self.manager.get_file_lock(file_uid)
            async with file_lock:
                status = await self.manager.get_file_status(file_uid)
                if status != "NEW":
                    print(f"⚠️ External job уже в статусе {status}: {job.dedupe_key}")
                    append_channel_log("gateway", f"⚠️ External job уже в статусе {status}: {job.dedupe_key}")
                    continue
                await self.manager.set_file_status(file_uid, "IN_PROGRESS")

            file_info = job_to_legacy_file_info(
                job,
                is_anti=self.handlers.processor.is_anti_file(job.original_file_name),
            )
            forced_route = self.handlers.get_force_author_route(file_info)
            if forced_route:
                file_info["forced_route"] = forced_route
                if forced_route["destination"] == "plagiscan":
                    file_info["is_anti"] = True
                    file_info["force_plagiscan"] = True
                    append_channel_log(
                        "plagiscan",
                        f"🛡️ force_plagiscan external source={job.source} sender={job.sender_id} file={job.original_file_name}",
                    )
                else:
                    file_info["fixed_author_editor_route"] = True
                    file_info["forced_editor_nickname"] = forced_route["editor_nickname"]
                    append_channel_log(
                        "editor",
                        f"👤 fixed editor route external source={job.source} sender={job.sender_id} file={job.original_file_name}",
                    )
            if self.handlers.processor.is_payment_document(job.original_file_name):
                print(f"💰 External payment document skipped: source={job.source} job_id={job.job_id} file={job.original_file_name}")
                append_channel_log("gateway", f"💰 External payment document skipped: source={job.source} job_id={job.job_id} file={job.original_file_name}")
                self.handlers._finish_processing(file_uid, True)
                await asyncio.to_thread(self.gateway_store.mark_done, job.job_id, None, "skipped_payment_document")
                continue
            self.manager.reset_waiting_status()
            self.handlers._increment_files_today(job.source)
            self.manager.file_queue.append(file_info)
            imported += 1
            print(f"📥 External job добавлен в очередь: source={job.source} job_id={job.job_id} file={job.original_file_name}")
            append_channel_log("gateway", f"📥 External job добавлен в очередь: source={job.source} job_id={job.job_id} file={job.original_file_name}")

    async def start_external_ingress(self):
        try:
            if self.external_start_iso:
                skipped = await skip_external_jobs_before_start(self.external_start_iso)
                if skipped:
                    print(f"⏭️ Старые VK/MAX jobs до запуска пропущены: {skipped}")
                    append_channel_log("gateway", f"⏭️ Старые VK/MAX jobs до запуска пропущены: {skipped}")

            vk_settings = VkLongPollSettings.from_sources()
            missing = vk_settings.missing_fields()
            if missing:
                print(f"ℹ️  VK Long Poll не запущен: не заданы {', '.join(missing)}")
                append_channel_log("gateway", f"ℹ️  VK Long Poll не запущен: не заданы {', '.join(missing)}")
                return

            if self.vk_longpoll_task and not self.vk_longpoll_task.done():
                return

            self.vk_longpoll_task = asyncio.create_task(run_vk_longpoll(), name="vk-longpoll")
            print(
                f"🌐 VK Long Poll запущен: group_id={vk_settings.group_id}, "
                f"wait={vk_settings.wait_seconds}, api={vk_settings.api_version}"
            )
            append_channel_log(
                "gateway",
                f"🌐 VK Long Poll запущен: group_id={vk_settings.group_id}, "
                f"wait={vk_settings.wait_seconds}, api={vk_settings.api_version}"
            )
        except VkLongPollConfigurationError as e:
            print(f"⚠️  VK Long Poll не запущен: {e}")
            append_channel_log("gateway", f"⚠️  VK Long Poll не запущен: {e}")
        except Exception as e:
            print(f"⚠️  Ошибка запуска VK Long Poll: {e}")
            append_channel_log("gateway", f"⚠️  Ошибка запуска VK Long Poll: {e}")

    def show_status(self):
        """Показать текущий статус"""
        settings = Config.get_all_settings()
        vk_enabled = bool(settings.get("vk_group_id") and (settings.get("vk_longpoll_token") or settings.get("vk_bot_token")))
        max_enabled = bool(settings.get("max_bot_token"))

        # Считаем обработку из единого источника: current_processing_files + editor_tracking
        processing_by_source = {"telegram": 0, "vk": 0, "max": 0}
        for info in self.handlers.current_processing_files.values():
            source = (info.get("source_platform") or "telegram").lower()
            if source in processing_by_source:
                processing_by_source[source] += 1
        # Файлы у редактора тоже считаем как «в обработке»
        for info in self.handlers.editor_tracking.values():
            source = (info.get("source_platform") or "telegram").lower()
            if source in processing_by_source:
                processing_by_source[source] += 1
        # Итог всегда равен сумме по источникам — расхождений быть не может
        total_processing = sum(processing_by_source.values())

        if not vk_enabled and not max_enabled:
            print(f"\n📊 Статус: {len(self.manager.file_queue)} в очереди | "
                  f"{total_processing} в обработке | "
                  f"{self.handlers.files_today['count']} файлов сегодня")
            return

        queue_by_source = {"telegram": 0, "vk": 0, "max": 0}
        for file_info in self.manager.file_queue:
            source = (file_info.get("source_platform") or "telegram").lower()
            if source in queue_by_source:
                queue_by_source[source] += 1

        today_by_source = self.handlers.files_today.get("by_source", {})

        source_order = [("telegram", "TG")]
        if vk_enabled:
            source_order.append(("vk", "VK"))
        if max_enabled:
            source_order.append(("max", "MAX"))

        queue_text = ", ".join(f"{label}:{queue_by_source.get(source, 0)}" for source, label in source_order)
        processing_text = ", ".join(f"{label}:{processing_by_source.get(source, 0)}" for source, label in source_order)
        today_text = ", ".join(f"{label}:{today_by_source.get(source, 0)}" for source, label in source_order)

        print(f"\n📊 Статус: {len(self.manager.file_queue)} в очереди [{queue_text}] | "
              f"{total_processing} в обработке [{processing_text}] | "
              f"{self.handlers.files_today['count']} файлов сегодня [{today_text}]")

    async def stop_all_clients(self):
        """Остановка всех клиентов"""
        print("\n🛑 Остановка аккаунтов...")
        append_channel_log("main", "🛑 Остановка аккаунтов...")

        if self.vk_longpoll_task and not self.vk_longpoll_task.done():
            self.vk_longpoll_task.cancel()
            try:
                await self.vk_longpoll_task
            except asyncio.CancelledError:
                print("✅ VK Long Poll остановлен")
                append_channel_log("gateway", "✅ VK Long Poll остановлен")
            except Exception as e:
                print(f"⚠️  Ошибка остановки VK Long Poll: {e}")
                append_channel_log("gateway", f"⚠️  Ошибка остановки VK Long Poll: {e}")
            finally:
                self.vk_longpoll_task = None

        for nickname, client in self.manager.clients.items():
            try:
                await client.stop()
                print(f"✅ Остановлен: {nickname}")
                append_channel_log("main", f"✅ Остановлен: {nickname}")
            except:
                print(f"⚠️  Не удалось остановить: {nickname}")
                append_channel_log("main", f"⚠️  Не удалось остановить: {nickname}")

        self.running = False
        print("\n✅ Все аккаунты остановлены")
        append_channel_log("main", "✅ Все аккаунты остановлены")


async def main():
    """Основная функция с консольным меню"""
    setup_runtime_logging()
    append_channel_log("main", "Старт main()")

    # Проверка зависимостей
    try:
        import pyrogram
        import requests
        import fitz
    except ImportError as e:
        print(f"❌ Не установлены зависимости: {e}")
        print("\n📦 Установите зависимости командой:")
        print("pip install pyrogram requests pymupdf")
        sys.exit(1)

    bot = FileDistributionBot()
    menu = ConsoleMenu()

    while True:
        menu.show_main_menu()

        try:
            choice = input("\nВыберите действие (1-8): ").strip()

            if choice == '1':
                # Настройка аккаунтов
                Config.setup_accounts_interactive()

            elif choice == '2':
                # Настройка редакторов и ботов (общие настройки)
                Config.setup_editors_interactive()

            elif choice == '3':
                # Авторизация аккаунтов
                await bot.manager.authorize_all_accounts()
                input("\nНажмите Enter для продолжения...")

            elif choice == '4':
                # Показать настройки
                Config.show_current_settings()
                input("\nНажмите Enter для продолжения...")

            elif choice == '5':
                # Запуск бота (с настройкой перед запуском)
                try:
                    await bot.run_bot()
                except KeyboardInterrupt:
                    print("\n\n🔄 Возвращаюсь в главное меню...")
                except Exception as e:
                    print(f"\n❌ Ошибка при работе бота: {e}")
                    import traceback
                    traceback.print_exc()
                    input("\nНажмите Enter для продолжения...")

            elif choice == '6':
                # Настройка VK / MAX токенов
                print("\n" + "=" * 60)
                print("🌐 НАСТРОЙКА VK / MAX И LONG POLL")
                print("=" * 60)
                print("Токены можно задать здесь или через переменные окружения.")
                print("Переменные окружения (VK_BOT_TOKEN, VK_COUNTER_TOKEN, VK_LONGPOLL_TOKEN, VK_GROUP_ID, VK_LONGPOLL_WAIT, MAX_BOT_TOKEN) имеют приоритет.\n")

                Config.load_config()

                # VK
                current_vk = Config.get_setting("vk_bot_token")
                env_vk = os.getenv("VK_BOT_TOKEN")
                if env_vk:
                    print(f"  VK токен (env):    {env_vk[:8]}...{env_vk[-4:]}")
                elif current_vk:
                    print(f"  VK токен (config): {current_vk[:8]}...{current_vk[-4:]}")
                else:
                    print("  VK токен:          не задан")

                vk_input = input("\nВведите VK Bot Token [Enter — оставить текущий, 0 — очистить]: ").strip()
                if vk_input == '0':
                    Config.update_setting("vk_bot_token", None)
                    print("✅ VK токен очищен")
                elif vk_input:
                    Config.update_setting("vk_bot_token", vk_input)
                    print(f"✅ VK токен сохранён: {vk_input[:8]}...")

                # VK Counter/User token for reading conversation history.
                current_vk_counter = Config.get_setting("vk_counter_token")
                env_vk_counter = os.getenv("VK_COUNTER_TOKEN")
                if env_vk_counter:
                    print(f"\n  VK Counter/User Token (env):    {env_vk_counter[:8]}...{env_vk_counter[-4:]}")
                elif current_vk_counter:
                    print(f"\n  VK Counter/User Token (config): {current_vk_counter[:8]}...{current_vk_counter[-4:]}")
                else:
                    print("\n  VK Counter/User Token:          не задан")

                print("  Для бесед нужен токен VK-аккаунта, который состоит в этих беседах.")
                print("  Откройте ссылку, подтвердите вход и доступ, затем скопируйте сюда ВЕСЬ URL из адресной строки:")
                print("  https://oauth.vk.com/authorize?client_id=2685278&display=page&redirect_uri=https://oauth.vk.com/blank.html&scope=messages,docs,offline&response_type=token&revoke=1&v=5.199")
                vk_counter_input = input("Введите VK Counter/User Token или OAuth URL [Enter — оставить текущий, 0 — очистить]: ").strip()
                if vk_counter_input == '0':
                    Config.update_setting("vk_counter_token", None)
                    print("✅ VK Counter/User Token очищен")
                elif vk_counter_input:
                    vk_counter_token = ConsoleMenu.extract_vk_access_token(vk_counter_input)
                    Config.update_setting("vk_counter_token", vk_counter_token)
                    print(f"✅ VK Counter/User Token сохранён: {vk_counter_token[:8]}...{vk_counter_token[-4:]}")

                # VK API version
                current_ver = Config.get_setting("vk_api_version", "5.199")
                ver_input = input(f"VK API версия [{current_ver}]: ").strip()
                if ver_input:
                    Config.update_setting("vk_api_version", ver_input)

                # VK Long Poll group id
                current_vk_group_id = Config.get_setting("vk_group_id")
                env_vk_group_id = os.getenv("VK_GROUP_ID")
                if env_vk_group_id:
                    print(f"  VK Long Poll group_id (env):    {env_vk_group_id}")
                elif current_vk_group_id:
                    print(f"  VK Long Poll group_id (config): {current_vk_group_id}")
                else:
                    print("  VK Long Poll group_id:          не задан")

                vk_group_id_input = input("Введите VK Group ID для Long Poll [Enter — оставить текущий, 0 — очистить]: ").strip()
                if vk_group_id_input == '0':
                    Config.update_setting("vk_group_id", None)
                    print("✅ VK Long Poll group_id очищен")
                elif vk_group_id_input:
                    try:
                        Config.update_setting("vk_group_id", int(vk_group_id_input))
                        print(f"✅ VK Long Poll group_id сохранён: {vk_group_id_input}")
                    except ValueError:
                        print("⚠️  VK Group ID должен быть числом, текущее значение оставлено без изменений")

                # VK Long Poll wait
                current_vk_wait = Config.get_setting("vk_longpoll_wait", 25)
                env_vk_wait = os.getenv("VK_LONGPOLL_WAIT")
                if env_vk_wait:
                    print(f"  VK Long Poll wait (env):    {env_vk_wait} сек")
                else:
                    print(f"  VK Long Poll wait (config): {current_vk_wait} сек")

                print("  Что это: сколько секунд VK держит одно Long Poll-подключение открытым,")
                print("  пока ждет новые события. Это не задержка обработки: новые сообщения обычно")
                print("  приходят сразу, как только VK их отдаст.")
                print("  25 сек — нормальное значение. Повышать можно до 30-60 сек, если много пустых")
                print("  циклов/логов или хочется реже дергать VK. Снижать до 5-10 сек имеет смысл")
                print("  только для отладки или если сеть/прокси часто рвет долгие запросы.")
                vk_wait_input = input("VK Long Poll wait в секундах [Enter — оставить текущий]: ").strip()
                if vk_wait_input:
                    try:
                        wait_value = int(vk_wait_input)
                        if wait_value < 1:
                            raise ValueError
                        Config.update_setting("vk_longpoll_wait", wait_value)
                    except ValueError:
                        print("⚠️  VK Long Poll wait должен быть положительным числом, текущее значение оставлено без изменений")

                # MAX
                current_max = Config.get_setting("max_bot_token")
                env_max = os.getenv("MAX_BOT_TOKEN")
                if env_max:
                    print(f"\n  MAX токен (env):    {env_max[:8]}...{env_max[-4:]}")
                elif current_max:
                    print(f"\n  MAX токен (config): {current_max[:8]}...{current_max[-4:]}")
                else:
                    print("\n  MAX токен:          не задан")

                max_input = input("Введите MAX Bot Token [Enter — оставить текущий, 0 — очистить]: ").strip()
                if max_input == '0':
                    Config.update_setting("max_bot_token", None)
                    print("✅ MAX токен очищен")
                elif max_input:
                    Config.update_setting("max_bot_token", max_input)
                    print(f"✅ MAX токен сохранён: {max_input[:8]}...")

                Config.save_config()
                print("\n✅ Настройки VK/MAX/Long Poll сохранены в config.json")
                input("\nНажмите Enter для продолжения...")

            elif choice == '8':
                Config.load_config()
                await Config.setup_telegram_group_routes_interactive(
                    bot.manager.get_client("НИК-1")
                )

            elif choice == '7':
                # Выход
                print("\n👋 До свидания!")
                if bot.running:
                    await bot.stop_all_clients()
                sys.exit(0)

            else:
                print("❌ Неверный выбор. Попробуйте снова.")

        except KeyboardInterrupt:
            print("\n\n🔄 Возвращаюсь в главное меню...")
            continue  # Просто продолжаем цикл, а не выходим
        except Exception as e:
            print(f"\n❌ Ошибка: {e}")
            import traceback
            traceback.print_exc()
            input("\nНажмите Enter для продолжения...")


if __name__ == "__main__":
    # Настройка асинхронного event loop
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    # Запуск
    asyncio.run(main())
