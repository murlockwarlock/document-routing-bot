import os
import re
import asyncio
import shutil
import tempfile
import uuid
import time as time_module  # Переименовываем стандартный модуль time
from datetime import datetime, time, timedelta
from pyrogram_asyncio_compat import ensure_main_event_loop

ensure_main_event_loop()

from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.errors import FloodWait
import requests
import fitz
from urllib.parse import urlparse, urlunparse
import fnmatch

from config import Config, AccountManager
from log_utils import append_channel_log
from multichannel_gateway.bootstrap import build_store
from multichannel_gateway.integrations.telegram_ingest import ingest_pyrogram_message
from multichannel_gateway.outbound import OutboundDispatcher

# Абсолютный путь к директории временных файлов (Fix #5: не зависит от CWD)
FILES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "files")


class VkApiError(RuntimeError):
    def __init__(self, method: str, error: dict):
        self.method = method
        self.error = error
        self.code = error.get("error_code")
        self.message = error.get("error_msg")
        super().__init__(f"VK API {method} error: {error}")


def _normalize_vk_id(value: str) -> str:
    """Strip optional VK prefixes, preserving group/bot negative IDs."""
    v = value.strip()
    lowered = v.lower()
    for prefix in ("https://vk.com/", "http://vk.com/", "vk.com/"):
        if lowered.startswith(prefix):
            v = v[len(prefix):]
            lowered = v.lower()
            break
    if v.lower().startswith("vk:"):
        v = v[3:]
    if v.lower().startswith("id") and v[2:].isdigit():
        v = v[2:]
    return v


def _is_vk_id(value: str) -> bool:
    """Check if value looks like a VK numeric ID (with or without vk: prefix)."""
    return _normalize_vk_id(value).lstrip("-").isdigit()



def _normalize_vk_id(value: str) -> str:
    """Strip optional VK prefixes, preserving group/bot negative IDs."""
    v = value.strip()
    lowered = v.lower()
    for prefix in ("https://vk.com/", "http://vk.com/", "vk.com/"):
        if lowered.startswith(prefix):
            v = v[len(prefix):]
            lowered = v.lower()
            break
    if v.lower().startswith("vk:"):
        v = v[3:]
    if v.lower().startswith("id") and v[2:].isdigit():
        v = v[2:]
    return v


def _is_vk_id(value: str) -> bool:
    """Check if value looks like a VK numeric ID (with or without vk: prefix)."""
    return _normalize_vk_id(value).lstrip("-").isdigit()
os.makedirs(FILES_DIR, exist_ok=True)


def _normalize_user_token(value) -> str:
    if value is None:
        return ""
    token = str(value).strip()
    if not token:
        return ""
    token = token.strip("@").strip()
    lowered = token.lower()
    for prefix in ("https://vk.com/", "http://vk.com/", "vk.com/"):
        if lowered.startswith(prefix):
            token = token[len(prefix):]
            lowered = token.lower()
            break
    if lowered.startswith("vk:"):
        token = token[3:]
    elif lowered.startswith("id") and token[2:].isdigit():
        token = token[2:]
    if token.startswith("-") and token[1:].isdigit():
        token = token[1:]
    return token.lower()


def _user_tokens_match(left, right) -> bool:
    left_norm = _normalize_user_token(left)
    right_norm = _normalize_user_token(right)
    if not left_norm or not right_norm:
        return False
    return left_norm == right_norm


class FileProcessor:
    """Обработчик файлов"""

    BROWSER_USER_AGENT = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    )

    @staticmethod
    def is_payment_document(file_name):
        """Проверка, является ли файл платежным документом"""
        if not file_name:
            return False
        payment_keywords = [
            "payment", "receipt", "пэймент", "документ", "чек",
            "оплата", "счет", "счёт", "квитанция", "invoice", "bill",
            "dokument", "document",
        ]
        file_lower = file_name.lower()
        return any(keyword in file_lower for keyword in payment_keywords)

    @staticmethod
    def is_anti_file(file_name):
        """Проверка, является ли файл анти-файлом"""
        if not file_name:
            return False
        file_lower = file_name.lower()
        return 'анти' in file_lower or 'anti' in file_lower

    @staticmethod
    def is_ai_report_filename(file_name):
        """True если имя файла — ИИ-отчет (начинается с 'ИИ ').
        Пара ИИ + обычный отчет считается как 1 исходный файл."""
        if not file_name:
            return False
        return file_name.startswith("ИИ ")

    @staticmethod
    def _create_http_session():
        session_factory = getattr(requests, "Session", None)
        if not session_factory:
            return None
        try:
            return session_factory()
        except Exception as e:
            print(f"⚠️  Не удалось создать HTTP session: {e}")
            return None

    @staticmethod
    def _request(session, method, url, **kwargs):
        requester = session if session is not None else requests
        return getattr(requester, method.lower())(url, **kwargs)

    @staticmethod
    def _build_browser_headers(referer=None, accept=None, x_requested_with=False, navigate=False):
        headers = {
            'User-Agent': FileProcessor.BROWSER_USER_AGENT,
            'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
        }

        if accept:
            headers['Accept'] = accept
        if referer:
            headers['Referer'] = referer
        if x_requested_with:
            headers['X-Requested-With'] = 'XMLHttpRequest'
        if navigate:
            headers['Upgrade-Insecure-Requests'] = '1'
        return headers

    @staticmethod
    def _absolute_url(base_domain, url_or_path):
        if not url_or_path:
            return None
        if url_or_path.startswith('http://') or url_or_path.startswith('https://'):
            return url_or_path
        if not url_or_path.startswith('/'):
            url_or_path = f"/{url_or_path}"
        return f"https://{base_domain}{url_or_path}"

    @staticmethod
    def _prime_report_session(session, urls_info):
        if session is None or not urls_info.get('summary_url'):
            return
        try:
            headers = FileProcessor._build_browser_headers(
                referer=f"https://{urls_info['domain']}/",
                accept='text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                navigate=True
            )
            FileProcessor._request(session, 'get', urls_info['summary_url'], headers=headers, timeout=30)
            print("✅ Открыта страница summary для прогрева browser-session")
        except Exception as e:
            print(f"⚠️  Не удалось прогреть summary-страницу: {e}")

    @staticmethod
    def _resolve_export_page_url(session, urls_info):
        if session is None or not urls_info.get('is_closed_url'):
            return None

        try:
            headers = FileProcessor._build_browser_headers(
                referer=urls_info.get('summary_url'),
                accept='application/json, text/javascript, */*; q=0.01',
                x_requested_with=True
            )
            response = FileProcessor._request(session, 'get', urls_info['is_closed_url'], headers=headers, timeout=30)
            response.raise_for_status()
            payload = response.json()
            print(f"📊 Ответ report/api/isClosed: {payload}")

            if isinstance(payload, dict) and payload.get('success') and payload.get('url'):
                export_url = FileProcessor._absolute_url(urls_info['domain'], payload.get('url'))
                print(f"✅ Получен browser export URL: {export_url[:100]}...")
                return export_url
        except Exception as e:
            print(f"⚠️  Не удалось получить browser export URL: {e}")

        return None

    @staticmethod
    def _open_export_page(session, urls_info, export_url):
        if session is None or not export_url:
            return
        try:
            headers = FileProcessor._build_browser_headers(
                referer=urls_info.get('summary_url'),
                accept='text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                navigate=True
            )
            FileProcessor._request(session, 'get', export_url, headers=headers, timeout=30)
            print("✅ Открыта export-страница перед скачиванием PDF")
        except Exception as e:
            print(f"⚠️  Не удалось открыть export-страницу: {e}")

    @staticmethod
    def download_pdf_detailed(url, filename, session=None, headers=None):
        """Скачивание PDF файла с возвратом HTTP статуса."""
        response = None
        try:
            request_headers = headers or FileProcessor._build_browser_headers(
                accept='application/octet-stream,*/*;q=0.8'
            )
            response = FileProcessor._request(session, 'get', url, headers=request_headers, stream=True, timeout=30)
            response.raise_for_status()

            with open(filename, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)

            print(f"✅ PDF скачан: {filename} ({os.path.getsize(filename)} байт)")
            return True, getattr(response, 'status_code', 200)
        except Exception as e:
            print(f"❌ Ошибка скачивания: {e}")
            status_code = getattr(getattr(e, 'response', None), 'status_code', None)
            if status_code is None and response is not None:
                status_code = getattr(response, 'status_code', None)
            return False, status_code

    @staticmethod
    def download_pdf(url, filename, session=None, headers=None):
        """Скачивание PDF файла"""
        success, _ = FileProcessor.download_pdf_detailed(url, filename, session=session, headers=headers)
        return success

    @staticmethod
    def _normalize_export_statuses(status_payload):
        """Нормализует ответ export/status к списку словарей."""
        if isinstance(status_payload, list):
            raw_items = status_payload
        elif isinstance(status_payload, dict):
            if isinstance(status_payload.get('Result'), list):
                raw_items = status_payload.get('Result', [])
            elif isinstance(status_payload.get('Data'), list):
                raw_items = status_payload.get('Data', [])
            else:
                raw_items = [status_payload]
        else:
            raw_items = []

        return [item for item in raw_items if isinstance(item, dict)]

    @staticmethod
    def _extract_pdf_export_status(status_payload):
        """Возвращает статус PDF из ответа export/status."""
        statuses = FileProcessor._normalize_export_statuses(status_payload)
        if not statuses:
            return "", None, None

        pdf_status = None
        for item in statuses:
            if str(item.get('Type', '')).lower() == 'pdf':
                pdf_status = item
                break

        payload = pdf_status or statuses[0]
        status = str(payload.get('Status') or payload.get('status') or '').strip()
        remaining_ms = payload.get('RemainingMs')
        if remaining_ms is None:
            remaining_ms = payload.get('remainingMs')
        return status, remaining_ms, payload

    @staticmethod
    def _is_pdf_ready_status(status):
        return status.lower() in {
            'ready', 'complete', 'completed', 'finished', 'done', 'iscreated'
        }

    @staticmethod
    def _is_pdf_pending_status(status):
        return status.lower() in {
            'inprogress', 'processing', 'creating', 'queued', 'isnotcreated'
        }

    @staticmethod
    def _is_pdf_error_status(status):
        return status.lower() in {
            'waserror', 'error', 'failed', 'iserror'
        }

    @staticmethod
    def _numeric_setting(name, default, minimum):
        value = Config.get_setting(name, default)
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = default
        return max(value, minimum)

    @staticmethod
    def _attempt_setting(name, default):
        value = Config.get_setting(name, default)
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = default
        return max(value, 1)

    @staticmethod
    def _status_wait_seconds(remaining_ms=None, default_seconds=None, minimum_seconds=None):
        if default_seconds is None:
            default_seconds = FileProcessor._numeric_setting("pdf_status_poll_interval_seconds", 20, 1)
        if minimum_seconds is None:
            minimum_seconds = default_seconds
        if isinstance(remaining_ms, (int, float)) and remaining_ms > 0:
            return max(remaining_ms / 1000, minimum_seconds)
        return default_seconds

    @staticmethod
    def build_api_urls(original_url):
        """Генерация URL для работы с API антиплагиата для ЛЮБОГО домена"""
        try:
            # Разбираем оригинальный URL
            parsed_url = urlparse(original_url)
            query_params = {}

            # Извлекаем параметры из query строки
            if parsed_url.query:
                for pair in parsed_url.query.split('&'):
                    if '=' in pair:
                        key, value = pair.split('=', 1)
                        query_params[key] = value

            # Извлекаем ID документа из пути
            path_parts = parsed_url.path.split('/')
            doc_id = None
            for part in reversed(path_parts):
                if part.isdigit():
                    doc_id = part
                    break

            if not doc_id:
                # Пробуем найти ID в другом месте (например, в bylink/summary/430)
                for i, part in enumerate(path_parts):
                    if part.isdigit() and i > 0:
                        doc_id = part
                        break

            if not doc_id:
                raise ValueError("Не найден ID документа")

            # Формируем параметры в правильном порядке
            report_params = {
                'userId': query_params.get('userId'),
                'v': query_params.get('v'),
                'c': query_params.get('c'),
                'short': 'false',
                'viewMode': 'Anonym',
                'validationHash': query_params.get('validationHash'),
            }

            # Фильтруем None значения
            report_params = {k: v for k, v in report_params.items() if v is not None}
            download_params = report_params.copy()
            download_params['type'] = 'Pdf'

            # Формируем query строку в правильном порядке
            report_query_string = '&'.join([f"{k}={v}" for k, v in report_params.items()])
            download_query_string = '&'.join([f"{k}={v}" for k, v in download_params.items()])

            # Получаем домен из оригинального URL
            domain = parsed_url.netloc

            # Формируем базовый путь для API
            base_path = "/export/api"

            # Формируем URL для всех операций
            make_url = f"https://{domain}{base_path}/make/{doc_id}?{download_query_string}"
            status_url = f"https://{domain}{base_path}/status/{doc_id}?{download_query_string}"
            download_url = f"https://{domain}{base_path}/download/{doc_id}?{download_query_string}"
            is_closed_url = f"https://{domain}/report/api/isClosed/{doc_id}?{report_query_string}"

            print(f"📄 Домен: {domain}")
            print(f"📄 ID документа: {doc_id}")
            print(f"📄 Сгенерирован URL для создания PDF: {make_url[:100]}...")
            print(f"📄 Сгенерирован URL для проверки статуса: {status_url[:100]}...")
            print(f"📄 Сгенерирован URL для скачивания: {download_url[:100]}...")
            print(f"📄 Сгенерирован URL для browser isClosed: {is_closed_url[:100]}...")

            return {
                'summary_url': original_url,
                'make_url': make_url,
                'status_url': status_url,
                'download_url': download_url,
                'is_closed_url': is_closed_url,
                'doc_id': doc_id,
                'domain': domain
            }

        except Exception as e:
            print(f"❌ Ошибка генерации URL: {e}")
            # Возвращаем оригинальный URL или базовый
            try:
                # Пытаемся извлечь ID документа
                path_parts = original_url.split('/')
                doc_id = None
                for part in reversed(path_parts):
                    if part.isdigit():
                        doc_id = part
                        break

                if doc_id:
                    # Получаем домен из оригинального URL
                    parsed = urlparse(original_url)
                    domain = parsed.netloc if parsed.netloc else original_url.split('/')[2]

                    # Простой fallback URL
                    fallback_url = f"https://{domain}/export/api/download/{doc_id}?short=false&viewMode=Anonym&type=Pdf"
                    print(f"⚠️  Используется fallback URL: {fallback_url[:100]}...")
                    return {
                        'make_url': None,
                        'status_url': None,
                        'download_url': fallback_url,
                        'summary_url': original_url,
                        'is_closed_url': None,
                        'doc_id': doc_id,
                        'domain': domain
                    }
            except:
                pass

            return {
                'make_url': None,
                'status_url': None,
                'download_url': original_url,
                'summary_url': original_url,
                'is_closed_url': None,
                'doc_id': None,
                'domain': None
            }

    @staticmethod
    def create_and_download_pdf(report_url, temp_pdf):
        """Создание и скачивание PDF с новой логикой для ЛЮБОГО домена"""
        try:
            # Получаем URL для работы с API
            urls_info = FileProcessor.build_api_urls(report_url)
            max_wait_seconds = FileProcessor._numeric_setting("pdf_generation_max_wait_seconds", 660, 60)
            max_status_attempts = FileProcessor._attempt_setting("pdf_status_attempts", 30)
            max_download_attempts = FileProcessor._attempt_setting("pdf_download_attempts", 10)
            download_wait_seconds = FileProcessor._numeric_setting("pdf_download_retry_interval_seconds", 20, 1)
            deadline = time_module.monotonic() + max_wait_seconds
            session = FileProcessor._create_http_session()
            FileProcessor._prime_report_session(session, urls_info)
            browser_export_url = None

            # Если не удалось получить make_url, используем старый метод
            if not urls_info.get('make_url'):
                print("⚠️  Не удалось сгенерировать URL для создания PDF, используем старый метод")
                if urls_info.get('download_url'):
                    return FileProcessor.download_pdf(urls_info['download_url'], temp_pdf)
                return False

            print(f"🔄 Отправляем POST запрос на создание PDF для домена: {urls_info['domain']}")

            # Шаг 1: Отправляем POST запрос для создания PDF
            headers = FileProcessor._build_browser_headers(
                referer=urls_info.get('summary_url'),
                accept='application/json, text/javascript, */*; q=0.01',
                x_requested_with=True
            )

            try:
                make_response = FileProcessor._request(session, 'post', urls_info['make_url'], headers=headers, timeout=30)
                make_response.raise_for_status()
                print("✅ POST запрос на создание PDF отправлен успешно")

                # Проверяем ответ
                try:
                    response_json = make_response.json()
                    print(f"📊 Ответ от сервера: {response_json}")

                    status, remaining_ms, _ = FileProcessor._extract_pdf_export_status(response_json)
                    if FileProcessor._is_pdf_pending_status(status):
                        print("⚠️  PDF все еще создается, ждем...")
                        wait_time = FileProcessor._status_wait_seconds(remaining_ms)
                        print(f"⏳ Ждем {wait_time:.1f} секунд...")
                        time_module.sleep(wait_time)
                    elif FileProcessor._is_pdf_ready_status(status):
                        print("✅ PDF уже готов!")
                    elif FileProcessor._is_pdf_error_status(status):
                        print(f"❌ PDF вернул ошибочный статус генерации сразу после make: {status.lower()}")
                        return False
                    else:
                        print(f"📊 Неизвестный статус: {status}")
                        time_module.sleep(FileProcessor._status_wait_seconds())

                except:
                    # Если не JSON, просто выводим текст
                    print(f"📊 Ответ от сервера (текст): {make_response.text[:200]}")
                    # Ждем стандартное время
                    time_module.sleep(FileProcessor._status_wait_seconds())

            except requests.exceptions.RequestException as e:
                print(f"⚠️  Ошибка POST запроса: {e}")
                # Пробуем GET как fallback
                try:
                    print("🔄 Пробуем GET запрос как fallback...")
                    make_response = FileProcessor._request(session, 'get', urls_info['make_url'], headers=headers, timeout=30)
                    make_response.raise_for_status()
                    print("✅ GET запрос отправлен успешно")
                    time_module.sleep(FileProcessor._status_wait_seconds())
                except Exception as e2:
                    print(f"❌ Ошибка GET запроса: {e2}")
                    time_module.sleep(FileProcessor._status_wait_seconds())  # Все равно ждем

            # Шаг 2: Проверяем статус создания
            pdf_ready = False
            if urls_info.get('status_url'):
                print("🔄 Проверяем статус создания PDF...")
                attempt = 0
                while time_module.monotonic() < deadline and attempt < max_status_attempts:
                    attempt += 1
                    try:
                        candidate_export_url = FileProcessor._resolve_export_page_url(session, urls_info)
                        if candidate_export_url:
                            browser_export_url = candidate_export_url
                            FileProcessor._open_export_page(session, urls_info, browser_export_url)
                            print("✅ PDF подтвержден через browser-flow, пробуем скачивание")
                            pdf_ready = True
                            break

                        status_response = FileProcessor._request(session, 'get', urls_info['status_url'], headers=headers, timeout=30)
                        status_response.raise_for_status()

                        # Парсим ответ
                        try:
                            status_data = status_response.json()
                            print(f"📊 Статус создания (попытка {attempt}): {status_data}")

                            status, remaining_ms, status_item = FileProcessor._extract_pdf_export_status(status_data)
                            status_lower = status.lower()

                            if FileProcessor._is_pdf_ready_status(status):
                                print("✅ PDF готов к скачиванию")
                                pdf_ready = True
                                break
                            elif FileProcessor._is_pdf_error_status(status):
                                wait = FileProcessor._status_wait_seconds(remaining_ms)
                                print(f"⚠️  PDF вернул статус генерации {status_lower}, ждем {wait:.1f} секунд и проверяем снова...")
                                time_left = deadline - time_module.monotonic()
                                if time_left <= 0:
                                    break
                                time_module.sleep(min(wait, time_left))
                                continue
                            elif FileProcessor._is_pdf_pending_status(status):
                                wait = FileProcessor._status_wait_seconds(remaining_ms)
                                print(f"⏳ PDF все еще создается, ждем {wait:.1f} секунд...")
                                time_left = deadline - time_module.monotonic()
                                if time_left <= 0:
                                    break
                                time_module.sleep(min(wait, time_left))
                                continue
                            elif status_item and status_item.get('error'):
                                print(f"⚠️  Ошибка создания PDF: {status_item['error']}")
                                break
                            else:
                                print(f"📊 Неизвестный статус: {status_lower or status_data}")
                                time_left = deadline - time_module.monotonic()
                                if time_left <= 0:
                                    break
                                time_module.sleep(min(FileProcessor._status_wait_seconds(), time_left))
                                continue

                        except:
                            # Если не JSON, просто выводим текст
                            status_text = status_response.text[:200]
                            status_text_lower = status_text.lower()
                            print(f"📊 Статус создания (попытка {attempt}): {status_text}")
                            if 'ready' in status_text_lower or 'готов' in status_text_lower or 'iscreated' in status_text_lower:
                                print("✅ PDF готов к скачиванию")
                                pdf_ready = True
                                break
                            elif 'waserror' in status_text_lower or 'failed' in status_text_lower or 'error' in status_text_lower:
                                wait = FileProcessor._status_wait_seconds()
                                print(f"⚠️  PDF вернул текстовый статус генерации, ждем {wait:.1f} секунд и проверяем снова...")
                                time_left = deadline - time_module.monotonic()
                                if time_left <= 0:
                                    break
                                time_module.sleep(min(wait, time_left))
                                continue
                            elif 'inprogress' in status_text_lower or 'создается' in status_text_lower:
                                time_left = deadline - time_module.monotonic()
                                if time_left <= 0:
                                    break
                                time_module.sleep(min(FileProcessor._status_wait_seconds(), time_left))
                                continue

                        time_left = deadline - time_module.monotonic()
                        if time_left <= 0:
                            break
                        print(f"⏳ Ждем {min(FileProcessor._status_wait_seconds(), time_left):.1f} секунд перед следующей проверкой...")
                        time_module.sleep(min(FileProcessor._status_wait_seconds(), time_left))

                    except Exception as e:
                        print(f"⚠️  Ошибка проверки статуса (попытка {attempt}): {e}")
                        time_left = deadline - time_module.monotonic()
                        if time_left <= 0:
                            break
                        time_module.sleep(min(FileProcessor._status_wait_seconds(), time_left))

                if not pdf_ready:
                    print("⚠️  PDF не подтвердил готовность через status API, пробуем скачать напрямую")

            # Шаг 3: Скачиваем PDF
            print("📥 Начинаем скачивание PDF...")
            if urls_info.get('download_url'):
                download_attempt = 0
                last_status_code = None
                while time_module.monotonic() < deadline and download_attempt < max_download_attempts:
                    download_attempt += 1
                    print(f"📥 Попытка скачивания {download_attempt}...")

                    if browser_export_url:
                        FileProcessor._open_export_page(session, urls_info, browser_export_url)
                    else:
                        candidate_export_url = FileProcessor._resolve_export_page_url(session, urls_info)
                        if candidate_export_url:
                            browser_export_url = candidate_export_url
                            FileProcessor._open_export_page(session, urls_info, browser_export_url)

                    download_headers = FileProcessor._build_browser_headers(
                        referer=browser_export_url or urls_info.get('summary_url'),
                        accept='text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                        navigate=True
                    )
                    success, status_code = FileProcessor.download_pdf_detailed(
                        urls_info['download_url'],
                        temp_pdf,
                        session=session,
                        headers=download_headers
                    )
                    last_status_code = status_code

                    if success:
                        if os.path.exists(temp_pdf) and os.path.getsize(temp_pdf) > 1000:  # Минимум 1KB
                            print(f"✅ PDF успешно скачан: {os.path.getsize(temp_pdf)} байт")
                            return True

                        file_size = os.path.getsize(temp_pdf) if os.path.exists(temp_pdf) else 0
                        print(f"⚠️  Файл скачан, но слишком мал или поврежден: {file_size} байт")
                        if os.path.exists(temp_pdf):
                            os.remove(temp_pdf)
                    elif status_code == 404:
                        print("ℹ️  PDF пока ещё не доступен по download URL (404)")

                    time_left = deadline - time_module.monotonic()
                    if time_left <= 0:
                        break

                    wait_seconds = download_wait_seconds
                    print(f"⏳ Ждем {min(wait_seconds, time_left):.1f} секунд перед повтором скачивания...")
                    time_module.sleep(min(wait_seconds, time_left))

                print(f"❌ Не удалось скачать PDF за отведенное время. Последний HTTP статус: {last_status_code}")
                return False
            else:
                print("❌ Нет URL для скачивания")
                return False

        except Exception as e:
            print(f"❌ Ошибка создания/скачивания PDF: {e}")
            import traceback
            traceback.print_exc()

            # Fallback: пробуем скачать напрямую через старый метод
            try:
                print("🔄 Пробуем fallback: скачать напрямую через старый метод...")
                # Просто используем оригинальный URL как есть
                return FileProcessor.download_pdf(report_url, temp_pdf)
            except Exception as e2:
                print(f"❌ Ошибка fallback скачивания: {e2}")

            return False

    @staticmethod
    def process_antiplagiat_url(report_url, temp_pdf):
        """Универсальная обработка URL антиплагиата для ЛЮБОГО домена"""
        try:
            print(f"🔍 Обработка URL: {report_url[:80]}...")

            # Всегда используем новую логику для ЛЮБОГО домена
            print("🔄 Использую новую логику обработки для любого домена...")
            return FileProcessor.create_and_download_pdf(report_url, temp_pdf)

        except Exception as e:
            print(f"❌ Ошибка обработки URL антиплагиата: {e}")

            # Крайний fallback: пробуем скачать как есть
            try:
                print("🔄 Крайний fallback: скачиваю как есть...")
                return FileProcessor.download_pdf(report_url, temp_pdf)
            except Exception as e2:
                print(f"❌ Ошибка крайнего fallback: {e2}")
                return False

    @staticmethod
    def crop_pdf(input_path, output_path):
        """Обрезка верхней части PDF"""
        try:
            print(f"✂️  Обрезка PDF: {input_path}")

            doc = fitz.open(input_path)
            if len(doc) == 0:
                doc.close()
                return False

            page = doc[0]
            crop_y = 150  # Стандартная высота обрезки

            # Ищем "РЕЗУЛЬТАТЫ ПРОВЕРКИ"
            for phrase in ["РЕЗУЛЬТАТЫ ПРОВЕРКИ", "Результаты проверки", "RESULTS"]:
                results = page.search_for(phrase)
                if results:
                    crop_y = results[0].y0 - 15
                    print(f"📏 Найдена фраза '{phrase}', обрезаем на высоте {crop_y}px")
                    break

            # Метод 1: Создаем новый документ с обрезанной страницей
            new_doc = fitz.open()
            new_page = new_doc.new_page(
                width=page.rect.width,
                height=page.rect.height - crop_y
            )

            # Копируем область
            clip_rect = fitz.Rect(0, crop_y, page.rect.width, page.rect.height)
            new_page.show_pdf_page(new_page.rect, doc, 0, clip=clip_rect)

            # Копируем остальные страницы
            if len(doc) > 1:
                for i in range(1, len(doc)):
                    new_doc.insert_pdf(doc, from_page=i, to_page=i)

            # Сохраняем со сжатием
            new_doc.save(output_path, garbage=4, deflate=True, clean=True)
            new_doc.close()
            doc.close()

            print(f"✅ PDF обрезан и сохранен: {output_path}")
            return True

        except Exception as e:
            print(f"❌ Ошибка обрезки PDF: {e}")

            # Метод 2: Просто копируем файл
            try:
                import shutil
                shutil.copy2(input_path, output_path)
                print(f"⚠️  PDF не обрезан, просто скопирован: {output_path}")
                return True
            except Exception as e2:
                print(f"❌ Ошибка копирования: {e2}")
                return False

    @staticmethod
    def cleanup_temp_files(file_paths):
        """Очистка временных файлов"""
        for file_path in file_paths:
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                    print(f"🗑️  Удален временный файл: {file_path}")
            except Exception as e:
                print(f"⚠️  Не удалось удалить файл {file_path}: {e}")


class CounterModeProcessor:
    """Обработчик для режима 3 - Счетчик файлов"""

    def __init__(self, account_manager):
        self.manager = account_manager
        self.processor = FileProcessor()

    @staticmethod
    def _counter_session_name(nickname: str) -> str:
        suffix = re.sub(r"[^A-Za-z0-9_]+", "_", str(nickname or "account"))
        return f"counter_{suffix}_{os.getpid()}"

    def _copy_session_for_counter(self, nickname: str):
        source_session = Config.get_session_name(nickname)
        if not os.path.exists(source_session):
            return None, None

        temp_dir = tempfile.TemporaryDirectory(prefix=f"counter_{nickname}_")
        session_name = self._counter_session_name(nickname)
        target_session = os.path.join(temp_dir.name, f"{session_name}.session")
        shutil.copy2(source_session, target_session)
        for suffix in ("-journal", "-wal", "-shm"):
            extra_source = f"{source_session}{suffix}"
            if os.path.exists(extra_source):
                shutil.copy2(extra_source, f"{target_session}{suffix}")
        return temp_dir, session_name

    async def _start_counter_client(self, nickname: str):
        account_data = Config.get_account(nickname)
        if not account_data or not account_data.get("api_id") or not account_data.get("api_hash"):
            print(f"❌ Аккаунт {nickname} не настроен")
            return None, None

        session_file = Config.get_session_name(nickname)
        if not os.path.exists(session_file):
            print(f"❌ Сессия {nickname} не найдена. Авторизуйте аккаунты в меню (пункт 3)")
            return None, None

        from pyrogram import Client
        from pyrogram.enums import ParseMode

        temp_dir, session_name = self._copy_session_for_counter(nickname)
        if not temp_dir or not session_name:
            return None, None

        client = Client(
            name=session_name,
            api_id=account_data["api_id"],
            api_hash=account_data["api_hash"],
            parse_mode=ParseMode.HTML,
            workdir=temp_dir.name,
            no_updates=True,
        )
        try:
            await client.start()
            return client, temp_dir
        except Exception:
            temp_dir.cleanup()
            raise

    async def analyze_author_files_interactive(self):
        """Интерактивный анализ файлов конкретного автора с выбором начальной точки"""
        print("\n" + "=" * 60)
        print("📊 РЕЖИМ 3 - СЧЕТЧИК ФАЙЛОВ (ИНТЕРАКТИВНЫЙ)")
        print("=" * 60)

        today = datetime.now().strftime("%Y-%m-%d")

        # Ввод автора — сначала выбор платформы
        platform_choice_m3 = input("Платформа автора [1 - ВКонтакте / 2 - Телеграмм]: ").strip()
        if platform_choice_m3 == "1":
            raw_m3 = input("Введите ВК (username или числовой ID): ").strip().lstrip('@')
            if not raw_m3:
                print("❌ Ник автора не может быть пустым")
                return
            author = f"vk:{raw_m3}" if not raw_m3.lower().startswith("vk:") else raw_m3
        elif platform_choice_m3 == "2":
            raw_m3 = input("Введите Telegram username (можно с @): ").strip().lstrip('@')
            if not raw_m3:
                print("❌ Ник автора не может быть пустым")
                return
            author = raw_m3
        else:
            print("❌ Неверный выбор платформы")
            return

        # --- НАЧАЛО ---
        start_file = input(
            "📄 Начальный файл (включительно, или Enter чтобы начать с начала): ").strip()
        date_str = input(
            f"📅 Начальная дата (ГГГГ-ММ-ДД, или Enter для сегодня [{today}]): ").strip() or today
        start_time_str = input(
            "⏰ Начальное время (ЧЧ:ММ, или Enter для 00:00): ").strip()

        # --- КОНЕЦ ---
        end_file = input(
            "📄 Конечный файл (включительно, или Enter чтобы до конца): ").strip()
        end_date_str = input(
            f"📅 Конечная дата (ГГГГ-ММ-ДД, или Enter для сегодня [{today}]): ").strip() or today
        end_time_str = input(
            "⏰ Конечное время (ЧЧ:ММ, или Enter для текущего момента): ").strip()

        # Сохраняем настройки
        Config.update_setting("counter_author", author)
        Config.update_setting("counter_start_file", start_file if start_file else "")
        Config.update_setting("counter_end_file", end_file if end_file else "")
        Config.update_setting("counter_date", date_str)
        Config.update_setting("counter_start_time", start_time_str if start_time_str else "")
        Config.update_setting("counter_end_date", end_date_str)
        Config.update_setting("counter_end_time", end_time_str if end_time_str else "")
        Config.update_setting("counter_pattern", "")

        await self.analyze_author_files_custom(
            author, start_file, start_time_str, date_str,
            end_file=end_file, end_date_str=end_date_str, end_time_str=end_time_str,
        )

    @staticmethod
    def normalize_filename(filename):
        """Нормализация имени файла для сравнения.
        Учитывает VK-кодировку спецсимволов: '_39_' → апостроф, '_34_' → кавычка и т.д.
        """
        if not filename:
            return ""
        # Убираем расширение
        name = os.path.splitext(filename)[0]
        # Приводим к нижнему регистру
        name = name.lower().strip()
        # VK кодирует спецсимволы как _<decimal>_ (например ' → _39_, " → _34_)
        # Убираем такие вставки, чтобы сравнение шло по смыслу
        import re
        name = re.sub(r"_\d{2,3}_", "", name)
        # Убираем оставшиеся апострофы и кавычки
        name = re.sub(r"['\"]", "", name)
        return name

    @classmethod
    def _counter_name_aliases(cls, file_name: str, message_text: str | None = None) -> list[str]:
        aliases: list[str] = []

        def add(value: str | None) -> None:
            normalized = cls.normalize_filename(value or "")
            if normalized and normalized not in aliases:
                aliases.append(normalized)

        add(file_name)

        text = str(message_text or "").strip()
        if text:
            for line in text.splitlines():
                candidate = line.strip()
                if not candidate:
                    continue
                candidate = re.sub(
                    r"^(отчет|отчёт|исходный файл|файл|документ)\s*:\s*",
                    "",
                    candidate,
                    flags=re.IGNORECASE,
                ).strip()
                if candidate and len(candidate) <= 120:
                    add(candidate)

            for match in re.finditer(r"[\wА-Яа-яЁё .()\-]+?\.(?:docx?|pdf|rtf|txt|pptx?)", text, flags=re.IGNORECASE):
                add(match.group(0).strip())

        return aliases

    def _log_vk_counter(self, message: str):
        append_channel_log("vk_counter", message)

    def _get_vk_api_token(self, prefer_counter_token: bool = False, token_kind: str | None = None) -> tuple[str, str]:
        if token_kind == "bot":
            bot_token = os.getenv("VK_BOT_TOKEN") or Config.get_setting("vk_bot_token")
            if bot_token:
                return bot_token, "VK Bot Token"
            raise RuntimeError("VK Bot Token не задан")

        if token_kind == "counter":
            counter_token = os.getenv("VK_COUNTER_TOKEN") or Config.get_setting("vk_counter_token")
            if counter_token:
                return counter_token, "VK Counter/User Token"
            raise RuntimeError("VK Counter/User Token не задан")

        if prefer_counter_token:
            counter_token = os.getenv("VK_COUNTER_TOKEN") or Config.get_setting("vk_counter_token")
            if counter_token:
                return counter_token, "VK Counter Token"

        bot_token = os.getenv("VK_BOT_TOKEN") or Config.get_setting("vk_bot_token")
        if bot_token:
            return bot_token, "VK Bot Token"

        raise RuntimeError("VK token не задан: настройте VK Bot Token или VK Counter Token")

    def _vk_api_call(self, method: str, params: dict) -> dict:
        prefer_counter_token = bool(params.pop("_prefer_counter_token", False))
        token_kind = params.pop("_token_kind", None)
        token, _token_source = self._get_vk_api_token(
            prefer_counter_token=prefer_counter_token,
            token_kind=token_kind,
        )
        api_version = os.getenv("VK_API_VERSION") or Config.get_setting("vk_api_version", "5.199")

        payload = {
            **params,
            "access_token": token,
            "v": api_version,
        }
        response = requests.post(
            f"https://api.vk.com/method/{method}",
            data=payload,
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()
        if "error" in data:
            raise VkApiError(method, data["error"])
        return data.get("response") or {}

    @staticmethod
    def _split_counter_authors(authors: str) -> list[str]:
        return [item.strip() for item in (authors or "").split(",") if item.strip()]

    def _resolve_vk_counter_author_ids(self, authors: str) -> set[int]:
        result: set[int] = set()
        unresolved: list[str] = []

        for item in self._split_counter_authors(authors):
            normalized = _normalize_vk_id(item)
            if normalized.lstrip("-").isdigit():
                result.add(int(normalized))
            else:
                screen_name = normalized
                if screen_name.startswith("@"):
                    screen_name = screen_name[1:]
                if screen_name.startswith("https://vk.com/"):
                    screen_name = screen_name[len("https://vk.com/"):]
                elif screen_name.startswith("http://vk.com/"):
                    screen_name = screen_name[len("http://vk.com/"):]
                unresolved.append(screen_name)

        if unresolved:
            users = self._vk_api_call("users.get", {"user_ids": ",".join(unresolved)})
            for user in users if isinstance(users, list) else []:
                user_id = user.get("id")
                if user_id is not None:
                    result.add(int(user_id))

        return result

    @staticmethod
    def _counter_start_datetime(date_str: str | None, start_time_str: str | None) -> datetime:
        date_part = date_str or datetime.now().strftime("%Y-%m-%d")
        time_part = start_time_str or "00:00"
        try:
            return datetime.strptime(f"{date_part} {time_part}", "%Y-%m-%d %H:%M")
        except ValueError:
            return datetime.combine(datetime.now().date(), datetime.min.time())

    @staticmethod
    def _counter_end_datetime(end_date_str: str | None, end_time_str: str | None) -> datetime | None:
        """Возвращает конечную точку диапазона, или None если не задана (= текущий момент)."""
        if not end_date_str and not end_time_str:
            return None
        date_part = end_date_str or datetime.now().strftime("%Y-%m-%d")
        time_part = end_time_str or datetime.now().strftime("%H:%M")
        try:
            return datetime.strptime(f"{date_part} {time_part}", "%Y-%m-%d %H:%M")
        except ValueError:
            return None

    def _extract_vk_docs(self, message: dict) -> list[dict]:
        docs: list[dict] = []

        def iter_attachments(raw_attachments):
            if isinstance(raw_attachments, dict):
                if raw_attachments.get("type"):
                    return [raw_attachments]
                return [item for item in raw_attachments.values() if isinstance(item, dict)]
            if isinstance(raw_attachments, list):
                return [item for item in raw_attachments if isinstance(item, dict)]
            return []

        def collect(current_message: dict, depth: int = 0) -> None:
            message_text = current_message.get("text")
            for item in iter_attachments(current_message.get("attachments")):
                if item.get("type") != "doc":
                    continue
                doc = item.get("doc") or item.get("document") or {}
                title = doc.get("title") or f"vk_doc_{doc.get('id', len(docs))}"
                ext = doc.get("ext")
                if ext and "." not in title:
                    title = f"{title}.{ext}"
                doc_uid_parts = [
                    str(doc.get("owner_id") or ""),
                    str(doc.get("id") or ""),
                    str(doc.get("access_key") or ""),
                    str(len(docs)),
                ]
                docs.append(
                    {
                        "file_name": title,
                        "size": doc.get("size") or 0,
                        "doc_id": ":".join(doc_uid_parts),
                        "message_text": message_text,
                        "fwd_depth": depth,
                    }
                )

            for forwarded in current_message.get("fwd_messages") or []:
                if isinstance(forwarded, dict):
                    collect(forwarded, depth + 1)

        collect(message)
        return docs

    @staticmethod
    def _vk_peer_token_kind(peer_id: str | int) -> str:
        try:
            return "counter" if int(peer_id) >= 2000000000 else "bot"
        except (TypeError, ValueError):
            return "bot"

    def _vk_counter_peer_ids_for_authors(self, author_ids: set[int]) -> list[str]:
        store = build_store()
        known_peers = store.list_vk_peer_ids_by_sender(tuple(str(item) for item in sorted(author_ids)))
        peers: list[str] = []
        seen: set[str] = set()

        def add(peer: str | int) -> None:
            value = str(peer).strip()
            if value and value not in seen:
                peers.append(value)
                seen.add(value)

        # ЛС сообщества: peer_id совпадает с user_id. Добавляем всегда, чтобы
        # менеджеру не нужно было знать, писал ли автор в ЛС раньше.
        for author_id in sorted(author_ids):
            add(author_id)

        counter_vk_scope = str(Config.get_setting("counter_vk_scope") or "").strip().lower()
        if counter_vk_scope == "direct":
            return peers

        explicit_peer_id = str(Config.get_setting("counter_vk_peer_id") or "").strip()
        if explicit_peer_id:
            peers.clear()
            seen.clear()
            add(explicit_peer_id)
            return peers

        for peer_id in known_peers:
            add(peer_id)
        return peers

    def _vk_local_counter_status_map(self, author_ids: set[int]) -> dict[tuple[str, str, str], dict]:
        store = build_store()
        jobs = store.list_vk_jobs_by_sender(tuple(str(item) for item in sorted(author_ids)))
        result: dict[tuple[str, str, str], dict] = {}
        for job in jobs:
            normalized = self.normalize_filename(job.original_file_name)
            message_ids = {str(job.message_id or "")}
            meta = job.transport_meta or {}
            if meta.get("vk_message_id") not in (None, ""):
                message_ids.add(str(meta.get("vk_message_id")))
            for message_id in message_ids:
                if not message_id:
                    continue
                result[(str(job.chat_id), message_id, normalized)] = {
                    "status": job.status,
                    "result_path": getattr(job, "result_path", None),
                    "dedupe_key": job.dedupe_key,
                }
        return result

    async def _fetch_vk_history_messages_for_peer(
        self,
        peer_id: str,
        start_ts: int,
        end_ts: int | None = None,
    ) -> list[dict]:
        page_size = 200
        offset = 0
        messages: list[dict] = []
        token_kind = self._vk_peer_token_kind(peer_id)

        while True:
            response = await asyncio.to_thread(
                self._vk_api_call,
                "messages.getHistory",
                {
                    "peer_id": int(peer_id),
                    "count": page_size,
                    "offset": offset,
                    "_token_kind": token_kind,
                },
            )
            items = response.get("items") or []
            if not items:
                break

            oldest_ts = None
            for message in items:
                try:
                    msg_ts = int(message.get("date") or 0)
                except (TypeError, ValueError):
                    msg_ts = 0
                oldest_ts = msg_ts if oldest_ts is None else min(oldest_ts, msg_ts)
                if msg_ts < start_ts:
                    continue
                if end_ts is not None and msg_ts > end_ts:
                    continue
                message["_counter_peer_id"] = str(peer_id)
                messages.append(message)

            self._log_vk_counter(
                f"history_page peer_id={peer_id} token={token_kind} offset={offset} "
                f"items={len(items)} collected={len(messages)}"
            )
            if len(items) < page_size or (oldest_ts is not None and oldest_ts < start_ts):
                break
            offset += page_size

        return messages

    async def analyze_vk_files_history_auto(
        self,
        authors: str,
        start_file: str | None = None,
        start_time_str: str | None = None,
        date_str: str | None = None,
        end_file: str | None = None,
        end_date_str: str | None = None,
        end_time_str: str | None = None,
    ):
        start_dt = self._counter_start_datetime(date_str, start_time_str)
        end_dt = self._counter_end_datetime(end_date_str, end_time_str)
        start_ts = int(start_dt.timestamp())
        end_ts = int(end_dt.timestamp()) if end_dt else None
        author_ids = self._resolve_vk_counter_author_ids(authors)
        if not author_ids:
            print(f"❌ Не удалось определить VK пользователей: {authors}")
            return

        peer_ids = self._vk_counter_peer_ids_for_authors(author_ids)
        if not peer_ids:
            print("❌ Не удалось определить VK диалоги для подсчета")
            return

        end_label = end_dt.strftime('%Y-%m-%d %H:%M') if end_dt else "текущий момент"
        print("\n" + "=" * 60)
        print("📊 VK СЧЕТЧИК ФАЙЛОВ")
        print("=" * 60)
        print(f"👤 VK пользователи: {', '.join(str(item) for item in sorted(author_ids))}")
        print(f"⏰ Период: с {start_dt.strftime('%Y-%m-%d %H:%M')} по {end_label}")
        print("📥 Источник: история VK API")
        local_status = self._vk_local_counter_status_map(author_ids)
        self._log_vk_counter(
            f"history_auto_start authors={sorted(author_ids)} peers={peer_ids} "
            f"start_dt={start_dt.isoformat()} end_dt={end_dt.isoformat() if end_dt else ''} "
            f"start_file={start_file or ''} end_file={end_file or ''}"
        )

        all_messages: list[dict] = []
        skipped_access = 0
        for peer_id in peer_ids:
            token_kind = self._vk_peer_token_kind(peer_id)
            try:
                peer_messages = await self._fetch_vk_history_messages_for_peer(peer_id, start_ts, end_ts)
            except RuntimeError as exc:
                skipped_access += 1
                self._log_vk_counter(f"history_skip_token peer_id={peer_id} token={token_kind} error={exc}")
                continue
            except VkApiError as exc:
                skipped_access += 1
                self._log_vk_counter(
                    f"history_skip_access peer_id={peer_id} token={token_kind} "
                    f"code={exc.code} error={exc.error}"
                )
                continue
            all_messages.extend(peer_messages)

        all_messages.sort(
            key=lambda item: (
                int(item.get("date") or 0),
                str(item.get("_counter_peer_id") or ""),
                int(item.get("conversation_message_id") or item.get("id") or 0),
            )
        )

        start_file_normalized = self.normalize_filename(start_file) if start_file else ""
        end_file_normalized = self.normalize_filename(end_file) if end_file else ""
        counting_started = not bool(start_file_normalized)
        sent_files: list[dict] = []
        received_files: list[dict] = []
        skipped_author = 0
        skipped_non_doc = 0
        skipped_payment = 0
        skipped_before_start_file = 0
        sent_counts_seen: dict[str, int] = {}
        same_author_received_counts: dict[str, int] = {}
        same_author_sent_message_ids: dict[str, set[str]] = {}

        for message in all_messages:
            try:
                from_id = int(message.get("from_id") or 0)
            except (TypeError, ValueError):
                from_id = 0

            docs = self._extract_vk_docs(message)
            if not docs:
                if from_id in author_ids:
                    skipped_non_doc += 1
                continue

            msg_ts = int(message.get("date") or 0)
            msg_dt = datetime.fromtimestamp(msg_ts)
            peer_id = str(message.get("_counter_peer_id") or "")
            message_id = message.get("id") or message.get("conversation_message_id")

            if from_id not in author_ids:
                skipped_author += 1
                for doc in docs:
                    file_name = doc["file_name"]
                    if not file_name.lower().endswith(".pdf"):
                        continue
                    if self.processor.is_payment_document(file_name):
                        continue
                    if self.processor.is_ai_report_filename(file_name):
                        continue
                    normalized = self.normalize_filename(file_name)
                    aliases = self._counter_name_aliases(file_name, doc.get("message_text") or message.get("text"))
                    received_files.append(
                        {
                            "file_name": file_name,
                            "file_name_normalized": normalized,
                            "file_name_normalized_aliases": aliases,
                            "file_key": f"vk_received:{peer_id}:{message_id}:{doc.get('doc_id') or len(received_files)}",
                            "date": msg_dt,
                            "message_id": message_id,
                            "file_size": doc.get("size") or 0,
                            "reply_to": None,
                        }
                    )
                continue

            for doc in docs:
                file_name = doc["file_name"]
                if self.processor.is_payment_document(file_name):
                    skipped_payment += 1
                    self._log_vk_counter(f"history_skip_payment peer_id={peer_id} file={file_name} msg={message_id}")
                    continue

                normalized = self.normalize_filename(file_name)
                message_id_text = str(message_id or "")
                # Для VK-сообществ/ботов from_id отрицательный. В беседе и исходные
                # файлы, и возвращенные PDF-отчеты могут выглядеть как документы от
                # того же from_id. Если такой PDF повторяет уже учтенное имя из
                # предыдущего сообщения, считаем его возвращенным отчетом.
                if (
                    from_id < 0
                    and file_name.lower().endswith(".pdf")
                    and sent_counts_seen.get(normalized, 0) > same_author_received_counts.get(normalized, 0)
                    and message_id_text not in same_author_sent_message_ids.get(normalized, set())
                    and not self.processor.is_ai_report_filename(file_name)
                ):
                    same_author_received_counts[normalized] = same_author_received_counts.get(normalized, 0) + 1
                    aliases = self._counter_name_aliases(file_name, doc.get("message_text") or message.get("text"))
                    received_files.append(
                        {
                            "file_name": file_name,
                            "file_name_normalized": normalized,
                            "file_name_normalized_aliases": aliases,
                            "file_key": f"vk_received_same_author:{peer_id}:{message_id}:{doc.get('doc_id') or len(received_files)}",
                            "date": msg_dt,
                            "message_id": message_id,
                            "file_size": doc.get("size") or 0,
                            "reply_to": None,
                        }
                    )
                    self._log_vk_counter(
                        f"history_same_author_received peer_id={peer_id} file={file_name} "
                        f"from_id={from_id} date={msg_dt.isoformat()} msg={message_id}"
                    )
                    continue

                if start_file_normalized and not counting_started:
                    if normalized == start_file_normalized:
                        counting_started = True
                        print(f"✅ Начало подсчета с файла: {file_name}")
                    else:
                        skipped_before_start_file += 1
                        continue

                entry = {
                    "file_name": file_name,
                    "file_name_normalized": normalized,
                    "file_key": f"vk:{peer_id}:{message_id}:{doc.get('doc_id') or len(sent_files)}",
                    "date": msg_dt,
                    "is_anti": self.processor.is_anti_file(file_name),
                    "message_id": message_id,
                    "file_size": doc.get("size") or 0,
                    "reply_to": None,
                }
                status_info = local_status.get((peer_id, str(message_id or ""), normalized))
                if status_info:
                    entry.update(
                        {
                            "file_key": status_info.get("dedupe_key") or entry["file_key"],
                            "status": status_info.get("status"),
                            "gateway_status": status_info.get("status"),
                            "gateway_result_path": status_info.get("result_path"),
                            "report_delivered": status_info.get("status") == "done" and bool(status_info.get("result_path")),
                        }
                    )
                else:
                    entry.update(
                        {
                            "status": "missing_from_gateway",
                            "gateway_status": "missing_from_gateway",
                            "gateway_result_path": None,
                            "report_delivered": False,
                        }
                    )
                sent_files.append(entry)
                sent_counts_seen[normalized] = sent_counts_seen.get(normalized, 0) + 1
                same_author_sent_message_ids.setdefault(normalized, set()).add(message_id_text)
                print(
                    f"✅ Файл учтен: {file_name} | from_id={from_id} | "
                    f"time={msg_dt.strftime('%Y-%m-%d %H:%M:%S')}"
                )
                self._log_vk_counter(
                    f"history_count peer_id={peer_id} file={file_name} from_id={from_id} "
                    f"date={msg_dt.isoformat()} msg={message_id}"
                )

                if end_file_normalized and normalized == end_file_normalized:
                    print(f"🏁 Конец подсчета на файле: {file_name}")
                    await self.generate_counter_report_simple(
                        sent_files,
                        received_files,
                        start_dt.date(),
                        start_dt=start_dt,
                        end_dt=end_dt,
                        source_label="история VK API",
                    )
                    return

        print("\n📊 VK счетчик завершен")
        print(f"   Просмотрено сообщений в периоде: {len(all_messages)}")
        print(f"   Учтено файлов: {len(sent_files)}")
        print(f"   Найдено присланных PDF отчетов: {len(received_files)}")
        print(f"   Пропущено по автору: {skipped_author}")
        print(f"   Сообщений без документов от выбранных авторов: {skipped_non_doc}")
        print(f"   Пропущено платежных документов: {skipped_payment}")
        print(f"   Пропущено до начального файла: {skipped_before_start_file}")
        self._log_vk_counter(
            f"history_auto_finish peers={len(peer_ids)} scanned={len(all_messages)} counted={len(sent_files)} "
            f"received={len(received_files)} "
            f"skipped_access={skipped_access} skipped_author={skipped_author} skipped_non_doc={skipped_non_doc} "
            f"skipped_payment={skipped_payment} skipped_before_start_file={skipped_before_start_file}"
        )

        await self.generate_counter_report_simple(
            sent_files,
            received_files,
            start_dt.date(),
            start_dt=start_dt,
            end_dt=end_dt,
            source_label="история VK API",
        )

    async def analyze_vk_files_custom(
        self,
        authors: str,
        peer_id: str,
        start_file: str | None = None,
        start_time_str: str | None = None,
        date_str: str | None = None,
    ):
        if not peer_id:
            print("❌ Для VK счетчика нужен peer_id беседы/ЛС")
            print("   Беседа VK: 2000000000 + номер беседы, ЛС: id пользователя")
            return

        start_dt = self._counter_start_datetime(date_str, start_time_str)
        start_ts = int(start_dt.timestamp())
        author_ids = self._resolve_vk_counter_author_ids(authors)
        if not author_ids:
            print(f"❌ Не удалось определить VK пользователей: {authors}")
            return

        start_file_normalized = self.normalize_filename(start_file) if start_file else ""
        counting_started = not bool(start_file_normalized)
        sent_files: list[dict] = []
        scanned_messages = 0
        skipped_author = 0
        skipped_time = 0
        skipped_non_doc = 0
        offset = 0
        page_size = 200
        peer_id_int = int(peer_id)

        print("\n" + "=" * 60)
        print("📊 VK СЧЕТЧИК ФАЙЛОВ")
        print("=" * 60)
        print(f"🌐 peer_id: {peer_id_int}")
        print(f"👤 VK пользователи: {', '.join(str(item) for item in sorted(author_ids))}")
        print(f"⏰ Период: с {start_dt.strftime('%Y-%m-%d %H:%M')} до текущего момента")
        print(f"📄 Начальный файл: {start_file or 'не задан'}")
        try:
            _token, token_source = self._get_vk_api_token(prefer_counter_token=True)
            print(f"🔑 Токен для чтения истории: {token_source}")
        except RuntimeError:
            token_source = "не задан"
        self._log_vk_counter(
            f"start peer_id={peer_id_int} authors={sorted(author_ids)} "
            f"start_dt={start_dt.isoformat()} start_file={start_file or ''} token_source={token_source}"
        )

        while True:
            try:
                response = await asyncio.to_thread(
                    self._vk_api_call,
                    "messages.getHistory",
                    {
                        "peer_id": peer_id_int,
                        "count": page_size,
                        "offset": offset,
                        "_prefer_counter_token": True,
                    },
                )
            except VkApiError as exc:
                if exc.code == 15:
                    print("\n❌ VK не дал доступ к истории этой беседы/ЛС (error_code=15 Access denied).")
                    print(f"   peer_id выбран правильно: {peer_id_int}")
                    print("   Что проверить:")
                    print("   Сейчас VK не разрешает читать историю этим токеном.")
                    print("   Для режима 3 укажите отдельный VK Counter/User Token от аккаунта,")
                    print("   который состоит в этой беседе и видит нужные файлы в обычном VK.")
                    print("   Настройка: главное меню -> 6 -> VK Counter/User Token.")
                    print("   Переменная окружения: VK_COUNTER_TOKEN.")
                    print("   Получение токена: https://dev.vk.com/ru/admin/apps-list -> Standalone-приложение,")
                    print("   затем OAuth ссылка из подсказки меню 6 с правами messages,offline,docs.")
                    self._log_vk_counter(
                        f"access_denied peer_id={peer_id_int} method={exc.method} error={exc.error}"
                    )
                    return
                raise
            items = response.get("items") or []
            if not items:
                break

            oldest_ts = None
            for message in items:
                scanned_messages += 1
                msg_ts = int(message.get("date") or 0)
                oldest_ts = msg_ts if oldest_ts is None else min(oldest_ts, msg_ts)
                if msg_ts < start_ts:
                    skipped_time += 1
                    continue

                from_id = int(message.get("from_id") or 0)
                if from_id not in author_ids:
                    skipped_author += 1
                    continue

                docs = self._extract_vk_docs(message)
                if not docs:
                    skipped_non_doc += 1
                    continue

                for doc in docs:
                    file_name = doc["file_name"]
                    if self.processor.is_payment_document(file_name):
                        self._log_vk_counter(f"skip payment file={file_name} msg={message.get('conversation_message_id')}")
                        continue

                    normalized = self.normalize_filename(file_name)
                    if start_file_normalized and not counting_started:
                        if normalized == start_file_normalized:
                            counting_started = True
                            print(f"✅ Начало подсчета с файла: {file_name}")
                            self._log_vk_counter(f"start_file matched file={file_name}")
                        else:
                            self._log_vk_counter(f"skip before_start_file file={file_name}")
                            continue

                    msg_dt = datetime.fromtimestamp(msg_ts)
                    entry = {
                        "file_name": file_name,
                        "file_name_normalized": normalized,
                        "file_key": f"{normalized}_{msg_ts}_{message.get('conversation_message_id')}_{len(sent_files)}",
                        "date": msg_dt,
                        "is_anti": self.processor.is_anti_file(file_name),
                        "message_id": message.get("id") or message.get("conversation_message_id"),
                        "file_size": doc.get("size") or 0,
                        "reply_to": None,
                    }
                    sent_files.append(entry)
                    print(
                        f"✅ VK файл учтен: {file_name} | from_id={from_id} | "
                        f"time={msg_dt.strftime('%Y-%m-%d %H:%M:%S')}"
                    )
                    self._log_vk_counter(
                        f"count file={file_name} from_id={from_id} "
                        f"date={msg_dt.isoformat()} msg={entry['message_id']}"
                    )

            self._log_vk_counter(
                f"page offset={offset} items={len(items)} scanned={scanned_messages} "
                f"counted={len(sent_files)}"
            )
            if len(items) < page_size or (oldest_ts is not None and oldest_ts < start_ts):
                break
            offset += page_size

        print("\n📊 VK счетчик завершен")
        print(f"   Просмотрено сообщений: {scanned_messages}")
        print(f"   Учтено файлов: {len(sent_files)}")
        print(f"   Пропущено по автору: {skipped_author}")
        print(f"   Пропущено по времени: {skipped_time}")
        print(f"   Сообщений без документов от выбранных авторов: {skipped_non_doc}")
        self._log_vk_counter(
            f"finish scanned={scanned_messages} counted={len(sent_files)} "
            f"skipped_author={skipped_author} skipped_time={skipped_time} skipped_non_doc={skipped_non_doc}"
        )

        await self.generate_counter_report_simple(sent_files, [], start_dt.date(), start_dt=start_dt)

    def _job_counter_datetime(self, job) -> datetime:
        """Возвращает datetime из объекта job (Long Poll / local DB)."""
        raw_ts = (job.transport_meta or {}).get("vk_message_date") if job.transport_meta else None
        if raw_ts not in (None, ""):
            try:
                return datetime.fromtimestamp(int(raw_ts))
            except (TypeError, ValueError):
                pass
        try:
            created = datetime.fromisoformat(job.created_at)
            if created.tzinfo is not None:
                return created.astimezone().replace(tzinfo=None)
            return created
        except (TypeError, ValueError):
            return datetime.now()

    async def analyze_vk_files_from_longpoll_custom(
        self,
        authors: str,
        peer_id: str,
        start_file: str | None = None,
        start_time_str: str | None = None,
        date_str: str | None = None,
    ):
        if not peer_id:
            print("❌ Для VK Long Poll счетчика нужен peer_id беседы/ЛС")
            self._log_vk_counter("longpoll_missing_peer_id")
            return

        start_dt = self._counter_start_datetime(date_str, start_time_str)
        author_ids = self._resolve_vk_counter_author_ids(authors)
        if not author_ids:
            print(f"❌ Не удалось определить VK пользователей: {authors}")
            self._log_vk_counter(f"longpoll_no_author_ids authors={authors}")
            return

        peer_id = str(peer_id)
        store = build_store()
        jobs = await asyncio.to_thread(
            store.list_vk_counter_jobs,
            peer_id,
            tuple(str(item) for item in sorted(author_ids)),
        )

        start_file_normalized = self.normalize_filename(start_file) if start_file else ""
        counting_started = not bool(start_file_normalized)
        sent_files: list[dict] = []
        skipped_time = 0
        skipped_payment = 0
        skipped_before_start_file = 0

        print("\n" + "=" * 60)
        print("📊 VK LONG POLL СЧЕТЧИК ФАЙЛОВ")
        print("=" * 60)
        print(f"🌐 peer_id: {peer_id}")
        print(f"👤 VK пользователи: {', '.join(str(item) for item in sorted(author_ids))}")
        print(f"⏰ Период: с {start_dt.strftime('%Y-%m-%d %H:%M')} до текущего момента")
        print("📥 Источник: локальная база Long Poll (только сообщения, полученные после запуска VK Long Poll)")
        if not jobs:
            print("⚠️  В локальной базе пока нет документов для этого peer_id и автора.")
            print("   Запустите бота с включенным VK Long Poll и оставьте его работать; новые файлы будут сохраняться автоматически.")
            self._log_vk_counter(f"longpoll_empty peer_id={peer_id} authors={sorted(author_ids)}")

        self._log_vk_counter(
            f"longpoll_start peer_id={peer_id} authors={sorted(author_ids)} "
            f"start_dt={start_dt.isoformat()} start_file={start_file or ''} jobs={len(jobs)}"
        )

        for job in jobs:
            msg_dt = self._job_counter_datetime(job)
            if msg_dt < start_dt:
                skipped_time += 1
                self._log_vk_counter(
                    f"longpoll_skip_time file={job.original_file_name} sender={job.sender_id} "
                    f"date={msg_dt.isoformat()} start_dt={start_dt.isoformat()} job={job.job_id if hasattr(job, 'job_id') else job.dedupe_key}"
                )
                continue

            file_name = job.original_file_name
            if self.processor.is_payment_document(file_name):
                skipped_payment += 1
                self._log_vk_counter(
                    f"longpoll_skip_payment file={file_name} sender={job.sender_id} "
                    f"date={msg_dt.isoformat()} job={job.job_id if hasattr(job, 'job_id') else job.dedupe_key}"
                )
                continue

            normalized = self.normalize_filename(file_name)
            if start_file_normalized and not counting_started:
                if normalized == start_file_normalized:
                    counting_started = True
                    print(f"✅ Начало подсчета с файла: {file_name}")
                else:
                    skipped_before_start_file += 1
                    self._log_vk_counter(
                        f"longpoll_skip_before_start_file file={file_name} sender={job.sender_id} "
                        f"date={msg_dt.isoformat()}"
                    )
                    continue

            sent_files.append(
                {
                    "file_name": file_name,
                    "file_name_normalized": normalized,
                    "file_key": job.dedupe_key,
                    "date": msg_dt,
                    "is_anti": self.processor.is_anti_file(file_name),
                    "message_id": job.message_id,
                    "file_size": job.file_size_bytes,
                    "reply_to": job.reply_to_message_id,
                }
            )
            print(
                f"✅ VK Long Poll файл учтен: {file_name} | from_id={job.sender_id} | "
                f"time={msg_dt.strftime('%Y-%m-%d %H:%M:%S')}"
            )
            self._log_vk_counter(
                f"longpoll_count file={file_name} sender={job.sender_id} "
                f"date={msg_dt.isoformat()} message_id={job.message_id} key={job.dedupe_key}"
            )

        print("\n📊 VK Long Poll счетчик завершен")
        print(f"   Документов в локальной базе по фильтру: {len(jobs)}")
        print(f"   Учтено файлов: {len(sent_files)}")
        print(f"   Пропущено по времени: {skipped_time}")
        print(f"   Пропущено платежных документов: {skipped_payment}")
        print(f"   Пропущено до начального файла: {skipped_before_start_file}")
        self._log_vk_counter(
            f"longpoll_finish scanned={len(jobs)} counted={len(sent_files)} "
            f"skipped_time={skipped_time} skipped_payment={skipped_payment} "
            f"skipped_before_start_file={skipped_before_start_file}"
        )

        await self.generate_counter_report_simple(sent_files, [], start_dt.date(), start_dt=start_dt)

    async def analyze_vk_files_local(
        self,
        authors: str,
        start_file: str | None = None,
        start_time_str: str | None = None,
        date_str: str | None = None,
        end_file: str | None = None,
        end_date_str: str | None = None,
        end_time_str: str | None = None,
    ):
        """Подсчёт VK-файлов из локальной базы gateway по sender_id, без peer_id беседы."""
        start_dt = self._counter_start_datetime(date_str, start_time_str)
        end_dt = self._counter_end_datetime(end_date_str, end_time_str)
        author_ids = self._resolve_vk_counter_author_ids(authors)
        if not author_ids:
            print(f"❌ Не удалось определить VK пользователей: {authors}")
            return

        store = build_store()
        jobs = await asyncio.to_thread(
            store.list_vk_jobs_by_sender,
            tuple(str(item) for item in sorted(author_ids)),
        )

        start_file_normalized = self.normalize_filename(start_file) if start_file else ""
        end_file_normalized = self.normalize_filename(end_file) if end_file else ""
        counting_started = not bool(start_file_normalized)
        sent_files: list[dict] = []
        skipped_time = 0
        skipped_payment = 0
        skipped_before_start_file = 0

        end_label = end_dt.strftime('%Y-%m-%d %H:%M') if end_dt else "текущий момент"
        print("\n" + "=" * 60)
        print("📊 VK СЧЕТЧИК ФАЙЛОВ (ЛОКАЛЬНАЯ БАЗА)")
        print("=" * 60)
        print(f"👤 VK пользователи: {', '.join(str(item) for item in sorted(author_ids))}")
        print(f"⏰ Период: с {start_dt.strftime('%Y-%m-%d %H:%M')} по {end_label}")
        print("📥 Источник: локальная база gateway (все обработанные VK-файлы)")
        if not jobs:
            print("⚠️  В локальной базе нет VK-файлов от этого автора.")

        for job in jobs:
            msg_dt = self._job_counter_datetime(job)
            if msg_dt < start_dt:
                skipped_time += 1
                continue
            if end_dt and msg_dt > end_dt:
                skipped_time += 1
                continue

            file_name = job.original_file_name
            if self.processor.is_payment_document(file_name):
                skipped_payment += 1
                continue

            normalized = self.normalize_filename(file_name)
            if start_file_normalized and not counting_started:
                if normalized == start_file_normalized:
                    counting_started = True
                    print(f"✅ Начало подсчета с файла: {file_name}")
                else:
                    skipped_before_start_file += 1
                    continue

            sent_files.append(
                {
                    "file_name": file_name,
                    "file_name_normalized": normalized,
                    "file_key": job.dedupe_key,
                    "date": msg_dt,
                    "is_anti": self.processor.is_anti_file(file_name),
                    "message_id": job.message_id,
                    "file_size": job.file_size_bytes,
                    "reply_to": job.reply_to_message_id,
                    "status": job.status,
                    "gateway_status": job.status,
                    "gateway_result_path": getattr(job, "result_path", None),
                    "report_delivered": job.status == "done" and bool(getattr(job, "result_path", None)),
                }
            )
            print(
                f"✅ Файл учтен: {file_name} | from_id={job.sender_id} | "
                f"time={msg_dt.strftime('%Y-%m-%d %H:%M:%S')}"
            )

            if end_file_normalized and normalized == end_file_normalized:
                print(f"🏁 Конец подсчета на файле: {file_name}")
                break

        print("\n📊 Счетчик завершен")
        print(f"   Документов в базе по фильтру: {len(jobs)}")
        print(f"   Учтено файлов: {len(sent_files)}")
        print(f"   Пропущено по времени: {skipped_time}")
        print(f"   Пропущено платежных документов: {skipped_payment}")
        print(f"   Пропущено до начального файла: {skipped_before_start_file}")

        await self.generate_counter_report_simple(sent_files, [], start_dt.date(), start_dt=start_dt, end_dt=end_dt)

    async def analyze_editor_files_custom(
        self,
        editor_nick: str,
        start_file: str | None = None,
        start_time_str: str | None = None,
        date_str: str | None = None,
        end_file: str | None = None,
        end_date_str: str | None = None,
        end_time_str: str | None = None,
    ):
        """Анализ файлов по редактору: сколько отправили редактору и сколько вернул."""
        start_dt = self._counter_start_datetime(date_str, start_time_str)
        end_dt = self._counter_end_datetime(end_date_str, end_time_str)
        end_label = end_dt.strftime('%d.%m.%Y %H:%M') if end_dt else "текущий момент"

        print(f"\n📊 АНАЛИЗ ФАЙЛОВ ПО РЕДАКТОРУ: {editor_nick}")
        print(f"📅 Период: с {start_dt.strftime('%d.%m.%Y %H:%M')} по {end_label}")
        if start_file:
            print(f"📄 Начинаем с файла: {start_file} (включительно)")
        if end_file:
            print(f"📄 Заканчиваем на файле: {end_file} (включительно)")

        client_nik2 = self.manager.get_client("НИК-2")
        _nik2_started_here = False
        _nik2_temp_session = None
        if not client_nik2 or not getattr(client_nik2, "is_connected", False):
            try:
                client_nik2, _nik2_temp_session = await self._start_counter_client("НИК-2")
                if not client_nik2:
                    return
                self.manager.clients["НИК-2"] = client_nik2
                _nik2_started_here = True
            except Exception as e:
                print(f"❌ Ошибка запуска НИК-2: {e}")
                return

        try:
            # Определяем ID редактора
            editor_clean = editor_nick.lstrip("@")
            try:
                editor_user = await client_nik2.get_users(editor_clean)
                editor_id = editor_user.id
                print(f"👤 Редактор: {editor_user.first_name} {editor_user.last_name or ''} (ID {editor_id})")
            except Exception as e:
                print(f"❌ Не удалось найти редактора {editor_nick}: {e}")
                return

            print(f"📁 Загружаем историю чата с редактором...")
            # Отправленные считаем только в заданном диапазоне партии.
            # Ответы редактора сверяем до текущего момента: отчеты могут прийти
            # позже конечного времени/файла, но все еще относиться к этой партии.
            all_messages = await self.get_messages_for_datetime_range(client_nik2, editor_id, start_dt, None)

            if not all_messages:
                print("❌ Не найдено сообщений за указанный период")
                return

            nik2_id = client_nik2.me.id
            start_file_normalized = self.normalize_filename(start_file) if start_file else ""
            end_file_normalized = self.normalize_filename(end_file) if end_file else ""
            counting_started = not bool(start_file_normalized)
            counting_finished = False

            sent_to_editor: list[dict] = []      # НИК-2 → редактор
            received_from_editor: list[dict] = [] # редактор → НИК-2

            print(f"🔍 Анализ {len(all_messages)} сообщений...")

            for message in all_messages:
                if not message.document:
                    continue
                file_name = message.document.file_name or ""
                if self.processor.is_payment_document(file_name):
                    continue
                normalized = self.normalize_filename(file_name)
                msg_dt = message.date.replace(tzinfo=None)

                if message.from_user and message.from_user.id == nik2_id:
                    # Отправлено НИК-2 → редактору
                    if counting_finished:
                        continue
                    if end_dt and msg_dt > end_dt:
                        continue
                    if start_file_normalized and not counting_started:
                        if normalized == start_file_normalized:
                            counting_started = True
                            print(f"✅ Начало подсчета с файла: {file_name}")
                        else:
                            continue
                    sent_to_editor.append({
                        "file_name": file_name,
                        "file_name_normalized": normalized,
                        "date": msg_dt,
                        "message_id": message.id,
                        "file_size": message.document.file_size or 0,
                        "is_anti": self.processor.is_anti_file(file_name),
                    })
                    print(f"📤 Отправлено редактору: {file_name} | {msg_dt.strftime('%d.%m.%Y %H:%M')}")
                    if end_file_normalized and normalized == end_file_normalized:
                        print(f"🏁 Конец подсчета на файле: {file_name}")
                        counting_finished = True
                        continue

                elif message.from_user and message.from_user.id == editor_id:
                    # Получено от редактора
                    received_from_editor.append({
                        "file_name": file_name,
                        "file_name_normalized": normalized,
                        "date": msg_dt,
                        "message_id": message.id,
                        "file_size": message.document.file_size or 0,
                        "is_anti": self.processor.is_anti_file(file_name),
                    })
                    print(f"📥 Получено от редактора: {file_name} | {msg_dt.strftime('%d.%m.%Y %H:%M')}")

            await self.generate_editor_report(sent_to_editor, received_from_editor, start_dt.date(), editor_nick, start_dt=start_dt, end_dt=end_dt)

        except Exception as e:
            print(f"❌ Ошибка анализа редактора: {e}")
            import traceback
            traceback.print_exc()
        finally:
            if _nik2_started_here:
                try:
                    await client_nik2.stop()
                    del self.manager.clients["НИК-2"]
                except Exception:
                    pass
                if _nik2_temp_session:
                    _nik2_temp_session.cleanup()

    async def generate_editor_report(self, sent_to_editor: list, received_from_editor: list, analysis_date, editor_nick: str, start_dt=None, end_dt=None):
        """Отчёт по редактору: отправлено, получено, не возвращено (с учётом количества, разделение анти/обычные)."""
        # Разделяем по типу (анти / обычные)
        normal_sent = [f for f in sent_to_editor if not f.get("is_anti")]
        anti_sent   = [f for f in sent_to_editor if f.get("is_anti")]

        # Для полученных: определяем тип по совпадению normalized имени с отправленными
        sent_is_anti_map = {f["file_name_normalized"]: f.get("is_anti", False) for f in sent_to_editor}
        normal_received = [f for f in received_from_editor if not sent_is_anti_map.get(f["file_name_normalized"], self.processor.is_anti_file(f["file_name"]))]
        anti_received   = [f for f in received_from_editor if sent_is_anti_map.get(f["file_name_normalized"], self.processor.is_anti_file(f["file_name"]))]

        def make_counts(lst):
            counts = {}
            for f in lst:
                counts[f["file_name_normalized"]] = counts.get(f["file_name_normalized"], 0) + 1
            return counts

        normal_sent_counts     = make_counts(normal_sent)
        anti_sent_counts       = make_counts(anti_sent)
        normal_received_counts = make_counts(normal_received)
        anti_received_counts   = make_counts(anti_received)

        def missing_items(sent_list, sent_counts, received_counts):
            result = []
            for name, snt in sent_counts.items():
                rec = received_counts.get(name, 0)
                if rec < snt:
                    display = next(f["file_name"] for f in sent_list if f["file_name_normalized"] == name)
                    result.append({"file_name": display, "missing": snt - rec, "sent": snt, "received": rec})
            return result

        not_returned_normal = missing_items(normal_sent, normal_sent_counts, normal_received_counts)
        not_returned_anti   = missing_items(anti_sent, anti_sent_counts, anti_received_counts)

        # Заголовок с периодом
        if start_dt:
            date_label = start_dt.strftime('%d %B').lstrip("0")
            start_label = start_dt.strftime('%H:%M')
            end_label = end_dt.strftime('%d %B %H:%M') if end_dt else "текущий момент"
            period_header = f"СТАТИСТИКА ЗА {date_label} с {start_label} по {end_label}"
        else:
            period_header = f"СТАТИСТИКА ЗА {analysis_date.strftime('%d.%m.%Y')}"

        print("\n" + "=" * 60)
        print(f"👤 Редактор: {editor_nick}")
        print("=" * 60)
        print(f"\n{period_header}")
        print("-" * 30)

        print(f"\n📄 Всего отправлено обычных файлов: {len(normal_sent)} шт.")
        print(f"\n🛡️  Всего отправлено АНТИ файлов: {len(anti_sent)} шт.")

        print("\n" + "-" * 30)
        print("НЕ ПРИСЛАННЫЕ ОТЧЕТЫ")
        print("-" * 30)

        print(f"\n📄 Не прислал обычных файлов: {sum(i['missing'] for i in not_returned_normal)} шт.")
        if not_returned_normal:
            for item in not_returned_normal:
                print(f"   ❌ {item['file_name']}: отправлено {item['sent']}, получено {item['received']}, не хватает {item['missing']}")

        print(f"\n🛡️  Не прислал анти файлов: {sum(i['missing'] for i in not_returned_anti)} шт.")
        if not_returned_anti:
            for item in not_returned_anti:
                print(f"   ❌ {item['file_name']}: отправлено {item['sent']}, получено {item['received']}, не хватает {item['missing']}")

        print("\n" + "=" * 60)

    async def get_messages_for_datetime_range(self, client, chat_id, start_dt: datetime, end_dt: datetime | None):
        """Получение сообщений в диапазоне дат/времени [start_dt, end_dt].
        end_dt=None означает до текущего момента."""
        messages = []
        effective_end = end_dt or datetime.now()

        try:
            async for message in client.get_chat_history(chat_id, limit=10000):
                msg_dt = message.date.replace(tzinfo=None)
                if msg_dt > effective_end:
                    continue  # ещё новее чем нужно, пропускаем
                if msg_dt < start_dt:
                    break  # зашли раньше начала — стоп
                messages.append(message)
        except Exception as e:
            print(f"⚠️ Ошибка при получении истории: {e}")

        print(f"📊 Найдено {len(messages)} сообщений в диапазоне {start_dt.strftime('%d.%m.%Y %H:%M')} — {effective_end.strftime('%d.%m.%Y %H:%M')}")
        messages.sort(key=lambda x: x.date)
        return messages

    async def analyze_author_files_custom(self, author, start_file=None, start_time_str=None, date_str=None, end_file=None, end_date_str=None, end_time_str=None):
        """Анализ файлов конкретного автора с пользовательскими настройками"""
        start_dt = self._counter_start_datetime(date_str, start_time_str)
        end_dt = self._counter_end_datetime(end_date_str, end_time_str)
        end_label = end_dt.strftime('%d.%m.%Y %H:%M') if end_dt else "текущий момент"

        print(f"\n📊 АНАЛИЗ ФАЙЛОВ АВТОРА: {author}")
        print(f"📅 Период: с {start_dt.strftime('%d.%m.%Y %H:%M')} по {end_label}")
        print(f"🔍 Фильтрация: все файлы (кроме платежных)")
        if start_file:
            print(f"📄 Начинаем с файла: {start_file} (включительно)")
        if end_file:
            print(f"📄 Заканчиваем на файле: {end_file} (включительно)")

        author_items = self._split_counter_authors(author)
        looks_like_vk = any(
            _is_vk_id(item)
            or item.startswith("vk.com/")
            or item.startswith("https://vk.com/")
            or item.startswith("http://vk.com/")
            or item.lower().startswith("vk:")
            for item in author_items
        )
        if looks_like_vk:
            await self.analyze_vk_files_history_auto(
                author,
                start_file,
                start_time_str,
                date_str,
                end_file=end_file,
                end_date_str=end_date_str,
                end_time_str=end_time_str,
            )
            return

        # Получаем клиент для НИК-1
        client_nik1 = self.manager.get_client("НИК-1")
        _nik1_started_here = False
        _nik1_temp_session = None
        if not client_nik1:
            # Пробуем создать клиент
            try:
                client_nik1, _nik1_temp_session = await self._start_counter_client("НИК-1")
                if not client_nik1:
                    return
                self.manager.clients["НИК-1"] = client_nik1
                _nik1_started_here = True

            except Exception as e:
                print(f"❌ Ошибка запуска НИК-1: {e}")
                import traceback
                traceback.print_exc()
                return

        try:
            # Получаем ID автора
            author_id = None

            # VK автор: используем VK ID напрямую
            if _is_vk_id(author):
                vk_id = _normalize_vk_id(author)
                author_id = int(vk_id)
                print(f"👤 VK автор ID: {author_id}")
                print("ℹ️  Для VK автора счётчик считает файлы из gateway очереди")
                # TODO: для VK пока собираем данные из gateway job store
                # а не из Telegram chat history
            else:
                try:
                    if author.startswith('@'):
                        author_clean = author[1:]
                    else:
                        author_clean = author
                    user = await client_nik1.get_users(author_clean)
                    author_id = user.id
                    print(f"👤 ID автора: {author_id}")
                    print(f"👤 Полное имя: {user.first_name} {user.last_name if user.last_name else ''}")
                except Exception as e:
                    print(f"❌ Не удалось найти автора {author}: {e}")
                    # Пробуем получить по ID если это число
                    if author.isdigit():
                        author_id = int(author)
                        print(f"�� Используем ID автора: {author_id}")
                    else:
                        return

            # Парсим время начала, если указано
            start_time = None
            if start_time_str:
                try:
                    start_time = datetime.strptime(start_time_str, "%H:%M").time()
                    print(f"⏰ Установлено время начала: {start_time_str}")
                except ValueError:
                    print(f"⚠️ Неверный формат времени: {start_time_str}. Используется полный день")
                    start_time = datetime.min.time()

            # Получаем историю до текущего момента: отправленные автором ограничим
            # диапазоном ниже, а PDF-возвраты нужны для сверки даже после конца партии.
            print(f"📁 Поиск файлов в диапазоне {start_dt.strftime('%d.%m.%Y %H:%M')} — {end_label}...")
            all_messages = await self.get_messages_for_datetime_range(client_nik1, author_id, start_dt, None)

            if not all_messages:
                print("❌ Не найдено сообщений за указанный период")
                return

            # Собираем файлы, которые автор отправил
            sent_files = []  # Файлы, которые автор отправил

            # Флаг для отслеживания, когда начать подсчет
            counting_started = not start_file  # Если start_file не указан, сразу начинаем считать
            counting_finished = False
            start_file_normalized = self.normalize_filename(start_file) if start_file else ""
            end_file_normalized = self.normalize_filename(end_file) if end_file else ""

            print(f"🔍 Анализ {len(all_messages)} сообщений от автора...")

            for message in all_messages:
                # Проверяем, что сообщение от автора
                if not message.from_user or message.from_user.id != author_id:
                    continue
                if counting_finished:
                    continue

                msg_dt = message.date.replace(tzinfo=None)
                if end_dt and msg_dt > end_dt:
                    continue

                # Проверяем, что есть документ
                if not message.document:
                    continue

                file_name = message.document.file_name

                # Пропускаем платежные документы
                if self.processor.is_payment_document(file_name):
                    continue

                # Нормализуем имя файла
                file_name_normalized = self.normalize_filename(file_name)

                # Проверяем, нужно ли начать подсчет с определенного файла
                if start_file and not counting_started:
                    if file_name_normalized == start_file_normalized:
                        counting_started = True
                        print(f"✅ Начало подсчета с файла: {file_name}")
                    else:
                        continue  # Пропускаем файлы до начального

                # ВАЖНО: Учитываем ВСЕ файлы, даже дубликаты!
                # Создаем уникальный ключ с временной меткой
                file_key = f"{file_name_normalized}_{message.date.timestamp()}"

                sent_files.append({
                    "file_name": file_name,
                    "file_name_normalized": file_name_normalized,
                    "file_key": file_key,  # Уникальный ключ с временной меткой
                    "date": message.date,
                    "is_anti": self.processor.is_anti_file(file_name),
                    "message_id": message.id,
                    "file_size": message.document.file_size if message.document.file_size else 0,
                    "reply_to": message.reply_to_message_id
                })

                if end_file_normalized and file_name_normalized == end_file_normalized:
                    print(f"🏁 Конец подсчета на файле: {file_name}")
                    counting_finished = True
                    continue

            print(f"📊 Найдено {len(sent_files)} файлов от автора (с учетом дубликатов)")

            # Собираем файлы, отправленные обратно автору (ТОЛЬКО PDF отчеты от НИК-1)
            received_files = []

            print(f"🔍 Поиск PDF файлов, отправленных обратно автору...")

            for message in all_messages:
                # Ищем сообщения от НИК-1 к автору (отправленные файлы обратно)
                if message.from_user and message.from_user.id == client_nik1.me.id and message.document:
                    file_name = message.document.file_name

                    # Проверяем, что это PDF файл (только PDF отчеты)
                    if not file_name.lower().endswith('.pdf'):
                        continue  # Пропускаем не-PDF файлы

                    # Пропускаем платежные документы
                    if self.processor.is_payment_document(file_name):
                        continue

                    # ИИ-отчет — часть того же файла, не считаем отдельно
                    if self.processor.is_ai_report_filename(file_name):
                        continue

                    # Нормализуем имя файла (убираем .pdf)
                    file_name_normalized = self.normalize_filename(file_name)

                    # ВАЖНО: Учитываем ВСЕ PDF файлы, даже дубликаты!
                    file_key = f"{file_name_normalized}_{message.date.timestamp()}"

                    received_files.append({
                        "file_name": file_name,
                        "file_name_normalized": file_name_normalized,
                        "file_key": file_key,  # Уникальный ключ с временной меткой
                        "date": message.date,
                        "file_size": message.document.file_size if message.document.file_size else 0,
                        "reply_to": message.reply_to_message_id
                    })

            print(f"📊 Найдено {len(received_files)} PDF файлов, отправленных обратно автору (с учетом дубликатов)")

            # Генерируем отчет
            await self.generate_counter_report_simple(sent_files, received_files, start_dt.date(), start_dt=start_dt, end_dt=end_dt)

        except Exception as e:
            print(f"❌ Ошибка анализа: {e}")
            import traceback
            traceback.print_exc()
        finally:
            # Останавливаем клиент НИК-1 если мы его запускали
            if _nik1_started_here and "НИК-1" in self.manager.clients:
                try:
                    await client_nik1.stop()
                    del self.manager.clients["НИК-1"]
                    print("✅ НИК-1 остановлен после анализа")
                except:
                    pass
                if _nik1_temp_session:
                    _nik1_temp_session.cleanup()

    async def generate_counter_report_simple(
        self,
        sent_files,
        received_files,
        analysis_date=None,
        start_dt=None,
        end_dt=None,
        source_label=None,
    ):
        """Генерация упрощенного отчета для режима 3 - только статистика"""
        print("\n" + "=" * 60)
        print("📊 ОТЧЕТ РЕЖИМА 3 - СЧЕТЧИК ФАЙЛОВ")
        print("=" * 60)

        # Разделяем отправленные файлы на анти и обычные
        anti_sent = [f for f in sent_files if f['is_anti']]
        normal_sent = [f for f in sent_files if not f['is_anti']]

        # Разделяем полученные файлы на анти и обычные
        anti_received = []
        normal_received = []
        uses_direct_report_status = any(
            "report_delivered" in f and f.get("gateway_status", "done") != "missing_from_gateway"
            for f in sent_files
        )

        if uses_direct_report_status:
            normal_received = [f for f in normal_sent if f.get("report_delivered")]
            anti_received = [f for f in anti_sent if f.get("report_delivered")]
        else:
            sent_name_set = {f["file_name_normalized"] for f in sent_files}
            for received_file in received_files:
                file_name = received_file['file_name']
                aliases = []
                stored_normalized = received_file.get("file_name_normalized")
                if stored_normalized:
                    aliases.append(stored_normalized)
                for alias in received_file.get("file_name_normalized_aliases") or [self.normalize_filename(file_name)]:
                    if alias and alias not in aliases:
                        aliases.append(alias)
                file_name_normalized = next(
                    (alias for alias in aliases if alias in sent_name_set),
                    aliases[0] if aliases else self.normalize_filename(file_name),
                )
                received_file["_matched_file_name_normalized"] = file_name_normalized
                is_anti = False
                for sent_file in sent_files:
                    if sent_file['file_name_normalized'] == file_name_normalized:
                        is_anti = sent_file['is_anti']
                        break
                if is_anti:
                    anti_received.append(received_file)
                else:
                    normal_received.append(received_file)

        # Подсчитываем количество уникальных имен
        normal_sent_names = {}
        for file in normal_sent:
            name = file['file_name_normalized']
            normal_sent_names[name] = normal_sent_names.get(name, 0) + 1

        anti_sent_names = {}
        for file in anti_sent:
            name = file['file_name_normalized']
            anti_sent_names[name] = anti_sent_names.get(name, 0) + 1

        normal_received_names = {}
        for file in normal_received:
            name = file.get('_matched_file_name_normalized') or file['file_name_normalized']
            normal_received_names[name] = normal_received_names.get(name, 0) + 1

        anti_received_names = {}
        for file in anti_received:
            name = file.get('_matched_file_name_normalized') or file['file_name_normalized']
            anti_received_names[name] = anti_received_names.get(name, 0) + 1

        # Находим неотправленные файлы
        not_sent_normal = []
        not_sent_anti = []

        for name, sent_count in normal_sent_names.items():
            received_count = normal_received_names.get(name, 0)
            if received_count < sent_count:
                first_file = next(
                    (
                        f for f in normal_sent
                        if f['file_name_normalized'] == name
                        and (not uses_direct_report_status or not f.get("report_delivered"))
                    ),
                    None,
                )
                if first_file is None:
                    first_file = next((f for f in normal_sent if f['file_name_normalized'] == name), None)
                if first_file:
                    not_sent_normal.append({
                        'file_name': first_file['file_name'],
                        'file_name_normalized': name,
                        'sent_count': sent_count,
                        'received_count': received_count,
                        'missing_count': sent_count - received_count
                    })

        for name, sent_count in anti_sent_names.items():
            received_count = anti_received_names.get(name, 0)
            if received_count < sent_count:
                first_file = next(
                    (
                        f for f in anti_sent
                        if f['file_name_normalized'] == name
                        and (not uses_direct_report_status or not f.get("report_delivered"))
                    ),
                    None,
                )
                if first_file is None:
                    first_file = next((f for f in anti_sent if f['file_name_normalized'] == name), None)
                if first_file:
                    not_sent_anti.append({
                        'file_name': first_file['file_name'],
                        'file_name_normalized': name,
                        'sent_count': sent_count,
                        'received_count': received_count,
                        'missing_count': sent_count - received_count
                    })

        # ========== ОТЧЕТ В ЧЕТКОЙ СТРУКТУРЕ ==========
        if start_dt:
            date_label = start_dt.strftime('%-d %B') if hasattr(start_dt, 'strftime') else str(analysis_date)
            start_label = start_dt.strftime('%H:%M')
            end_label = end_dt.strftime('%-d %B %H:%M') if end_dt else "текущий момент"
            period_header = f"СТАТИСТИКА ЗА {date_label} с {start_label} по {end_label}"
        elif analysis_date:
            period_header = f"СТАТИСТИКА ЗА {analysis_date.strftime('%d.%m.%Y')}"
        else:
            period_header = "СТАТИСТИКА ЗА ДЕНЬ"

        print("\n" + "-" * 30)
        print(period_header)
        print("-" * 30)
        if source_label:
            print(f"Источник: {source_label}")

        print(f"\n📄 Всего отправлено обычных файлов: {len(normal_sent)} шт.")
        print(f"\n🛡️  Всего отправлено АНТИ файлов: {len(anti_sent)} шт.")

        print("\n" + "-" * 30)
        print("НЕ ПРИСЛАННЫЕ ОТЧЕТЫ")
        print("-" * 30)

        print(f"\n📄 Не прислал обычных файлов: {sum(item['missing_count'] for item in not_sent_normal)} шт.")
        if not_sent_normal:
            for i, item in enumerate(not_sent_normal, 1):
                print(f"  {i:2d}. {item['file_name']}")

        print(f"\n🛡️  Не прислал анти файлов: {sum(item['missing_count'] for item in not_sent_anti)} шт.")
        if not_sent_anti:
            for i, item in enumerate(not_sent_anti, 1):
                print(f"  {i:2d}. {item['file_name']}")

        print("\n" + "=" * 60)

        # Сохраняем отчет в файл
        await self.save_simple_report_to_file(sent_files, received_files, not_sent_normal, not_sent_anti,
                                              normal_sent_names, anti_sent_names,
                                              normal_received_names, anti_received_names,
                                              period_header=period_header,
                                              source_label=source_label)

    async def save_simple_report_to_file(self, sent_files, received_files, not_sent_normal, not_sent_anti,
                                         normal_sent_names, anti_sent_names,
                                         normal_received_names, anti_received_names,
                                         period_header=None,
                                         source_label=None):
        """Сохранение упрощенного отчета в файл с четкой структурой"""
        try:
            report_dir = "reports"
            os.makedirs(report_dir, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            author = Config.get_setting("counter_author")

            # Очищаем ник от символа @ для имени файла
            author_clean = author.replace('@', '') if author else "unknown"
            author_clean = re.sub(r"[^0-9A-Za-zА-Яа-я._-]+", "_", author_clean).strip("_") or "unknown"
            report_file = f"{report_dir}/report_{author_clean}_{timestamp}.txt"

            with open(report_file, 'w', encoding='utf-8') as f:
                f.write("=" * 60 + "\n")
                f.write("ОТЧЕТ РЕЖИМА 3 - СЧЕТЧИК ФАЙЛОВ\n")
                f.write("=" * 60 + "\n")

                f.write(f"Автор: {author or 'не указан'}\n")
                f.write(f"Время создания отчета: {datetime.now().strftime('%H:%M:%S')}\n")
                f.write("-" * 60 + "\n\n")

                # СТАТИСТИКА
                f.write("-" * 30 + "\n")
                f.write(f"{period_header or 'СТАТИСТИКА ЗА ДЕНЬ'}\n")
                f.write("-" * 30 + "\n")
                if source_label:
                    f.write(f"Источник: {source_label}\n")

                f.write(f"\n📄 Всего отправлено обычных файлов: {sum(normal_sent_names.values())} шт.\n")
                f.write(f"\n🛡️  Всего отправлено АНТИ файлов: {sum(anti_sent_names.values())} шт.\n")

                # НЕ ПРИСЛАННЫЕ ОТЧЕТЫ
                f.write("\n" + "-" * 30 + "\n")
                f.write("НЕ ПРИСЛАННЫЕ ОТЧЕТЫ\n")
                f.write("-" * 30 + "\n")

                f.write(f"\n📄 Не прислал обычных файлов: {sum(item['missing_count'] for item in not_sent_normal)} шт.\n")
                if not_sent_normal:
                    for i, item in enumerate(not_sent_normal, 1):
                        f.write(f"  {i:2d}. {item['file_name']}\n")

                f.write(f"\n🛡️  Не прислал анти файлов: {sum(item['missing_count'] for item in not_sent_anti)} шт.\n")
                if not_sent_anti:
                    for i, item in enumerate(not_sent_anti, 1):
                        f.write(f"  {i:2d}. {item['file_name']}\n")

                f.write("\n" + "=" * 60 + "\n")

            print(f"✅ Отчет сохранен в файл: {report_file}")

        except Exception as e:
            print(f"⚠️ Ошибка сохранения отчета: {e}")


class BotHandlers:
    """Обработчики сообщений от ботов"""

    def __init__(self, account_manager):
        self.manager = account_manager
        self.processor = FileProcessor()
        self.counter_processor = CounterModeProcessor(account_manager)
        self.file_tracking = {}  # Отслеживание файлов по авторам
        self.daily_counter = {
            "count": 0,
            "date": datetime.now().date(),
            "interval_start": None,
            "interval_end": None,
        }  # обычные файлы, отправленные в текущем рабочем интервале MODE2
        self.files_today = {
            "count": 0,
            "date": datetime.now().date(),
            "by_source": {"telegram": 0, "vk": 0, "max": 0},
        }  # ВСЕ принятые файлы (для отображения статуса)
        self.current_mode = Config.get_setting("mode")
        self.file_storage = {}  # Хранение информации о файлах для отправки обратно автору
        self.current_processing_files = {}  # Хранение информации о файлах в обработке
        self.message_to_file_map = {}  # Сопоставление ID сообщений от ботов с файлами
        self.blacklisted_accounts = []  # Аккаунты, заблокированные из-за отсутствия проверок
        self.editor_tracking = {}  # Формат: {message_id: {"author": "...", "file_name": "...", "original_name": "...", "sent_at": datetime}}
        self.unavailable_editors = set()  # Редакторы, которые сообщили "проверки закончились"
        self.is_editor_mode = False  # Флаг для режима редактора (не бота)

        # State machine: предотвращение дублей обработки
        self.processed_files = {}  # {file_uid: "IN_PROGRESS"|"DONE"|"FAILED"}
        self._processing_lock = asyncio.Lock()
        self.ai_upload_ready_events = {}  # {account_name: asyncio.Event}
        self.aaa_globally_unavailable = False
        self.outbound_dispatcher = OutboundDispatcher()
        self._editor_response_claims = {}
        self._editor_response_unknown_warnings = {}

        self._setup_periodic_cleanup()

    @staticmethod
    def _log_aaa(message):
        append_channel_log("aaa", message)

    @staticmethod
    def _log_plagiscan(message):
        append_channel_log("plagiscan", message)

    @staticmethod
    def _log_editor(message):
        append_channel_log("editor", message)

    @staticmethod
    def _log_gateway(message):
        append_channel_log("gateway", message)

    def _reset_files_today_if_needed(self):
        today = datetime.now().date()
        if "by_source" not in self.files_today:
            self.files_today["by_source"] = {"telegram": 0, "vk": 0, "max": 0}
        if self.files_today["date"] != today:
            self.files_today = {
                "count": 0,
                "date": today,
                "by_source": {"telegram": 0, "vk": 0, "max": 0},
            }

    def _increment_files_today(self, source_platform: str):
        self._reset_files_today_if_needed()
        source = (source_platform or "telegram").lower()
        if source not in self.files_today["by_source"]:
            self.files_today["by_source"][source] = 0
        self.files_today["count"] += 1
        self.files_today["by_source"][source] += 1

    @staticmethod
    def _configured_user_list(setting_name: str) -> list[str]:
        configured = Config.get_setting(setting_name, []) or []
        if isinstance(configured, str):
            configured = [item.strip() for item in configured.split(",") if item.strip()]
        return [str(item).strip() for item in configured if str(item).strip()]

    def _has_plagiscan_user_filter(self) -> bool:
        return bool(self._configured_user_list("plagiscan_users"))

    @staticmethod
    def _is_explicit_vk_user_entry(entry: str) -> bool:
        entry_lower = str(entry or "").strip().lower()
        return (
            entry_lower.startswith("vk:")
            or entry_lower.startswith("vk.com/")
            or entry_lower.startswith("https://vk.com/")
            or entry_lower.startswith("http://vk.com/")
        )

    def _force_author_entry_matches(
        self,
        configured,
        *,
        author=None,
        author_id=None,
        route_sender_id=None,
        route_sender_name=None,
    ) -> bool:
        if self._is_explicit_vk_user_entry(configured):
            candidates = (route_sender_id, route_sender_name)
        else:
            candidates = (author, author_id, route_sender_id, route_sender_name)
        return any(_user_tokens_match(configured, candidate) for candidate in candidates)

    @staticmethod
    def _is_telegram_group_message(message) -> bool:
        chat = getattr(message, "chat", None)
        chat_type = getattr(chat, "type", None)
        chat_type = getattr(chat_type, "value", chat_type)
        return str(chat_type or "").lower() in {"group", "supergroup"}

    @staticmethod
    def _configured_group_routes():
        configured = Config.get_setting("telegram_group_routes", {}) or {}
        return configured if isinstance(configured, dict) else {}

    def _telegram_group_route_for_file_info(self, file_info: dict):
        info = file_info or {}
        if str(info.get("source_platform") or "telegram").lower() != "telegram":
            return None
        if not info.get("route_is_group"):
            return None

        chat_id = info.get("route_chat_id")
        if chat_id is None:
            return None
        route = self._configured_group_routes().get(str(chat_id))
        if not isinstance(route, dict):
            return None

        destination = str(route.get("destination") or "plagiscan").strip().lower()
        if destination == "editor":
            editor_nickname = str(route.get("editor_nickname") or "").strip()
            if editor_nickname:
                if not editor_nickname.startswith("@"):
                    editor_nickname = "@" + editor_nickname
                return {
                    "destination": "editor",
                    "editor_nickname": editor_nickname,
                    "scope": "telegram_group",
                }
        return {"destination": "plagiscan", "scope": "telegram_group"}

    def get_force_author_route(self, file_info: dict):
        info = file_info or {}
        group_route = self._telegram_group_route_for_file_info(info)
        if group_route:
            return group_route

        author = info.get("author")
        author_id = info.get("author_id")
        route_sender_id = info.get("route_sender_id")
        route_sender_name = info.get("route_sender_name")
        if not self.is_force_plagiscan_author(
            author=author,
            author_id=author_id,
            route_sender_id=route_sender_id,
            route_sender_name=route_sender_name,
        ):
            return None

        destination = str(Config.get_setting("forced_authors_destination", "plagiscan") or "plagiscan").strip().lower()
        if destination in {"editor", "редактор"}:
            editor_nickname = str(Config.get_setting("forced_authors_editor_nickname", "") or "").strip()
            if editor_nickname:
                if not editor_nickname.startswith("@"):
                    editor_nickname = "@" + editor_nickname
                return {"destination": "editor", "editor_nickname": editor_nickname}
        return {"destination": "plagiscan"}

    def _anti_destination_for(self, file_info: dict) -> str:
        if (file_info or {}).get("force_plagiscan") or (file_info or {}).get("via_normal_destination"):
            return "бот"
        return Config.get_setting("anti_destination")

    def _editor_for_file(self, file_info: dict, *, prefer_anti: bool = False) -> str:
        """Return route-specific editor with legacy editor_nickname fallback."""
        if Config.get_setting("mode") == "mode2":
            return Config.get_setting("editor_nickname")

        info = file_info or {}
        is_anti_route = prefer_anti or (info.get("is_anti") and not info.get("via_normal_destination"))
        if is_anti_route:
            return Config.get_setting("anti_editor_nickname") or Config.get_setting("editor_nickname")
        return Config.get_setting("normal_editor_nickname") or Config.get_setting("editor_nickname")

    @staticmethod
    def _normalize_editor_destination(destination) -> str:
        return str(destination or "").strip().lstrip("@").lower()

    def _mark_editor_unavailable(self, destination) -> None:
        normalized = self._normalize_editor_destination(destination)
        if normalized:
            self.unavailable_editors.add(normalized)

    def _is_editor_unavailable(self, destination) -> bool:
        normalized = self._normalize_editor_destination(destination)
        return bool(normalized and normalized in self.unavailable_editors)

    @staticmethod
    def _strip_extension_and_tg_suffix(file_name: str) -> tuple[str, int | None]:
        stem = os.path.splitext(os.path.basename(str(file_name or "")))[0].lower().strip()
        match = re.search(r"\s*\((\d+)\)\s*$", stem)
        if not match:
            return stem, None
        return stem[:match.start()].strip(), int(match.group(1))

    @staticmethod
    def _editor_file_key(file_name: str) -> str:
        stem = os.path.splitext(os.path.basename(str(file_name or "")))[0]
        return re.sub(r"\s+", " ", stem).strip().casefold()

    @staticmethod
    def _editor_report_key(file_name: str) -> str:
        name = os.path.basename(str(file_name or ""))
        return re.sub(r"\s+", " ", name).strip().casefold()

    @classmethod
    def _editor_tracking_keys(cls, tracking_info: dict) -> set[str]:
        names = {
            tracking_info.get("original_name"),
            tracking_info.get("original_file_name"),
            tracking_info.get("expected_pdf_name"),
        }
        if tracking_info.get("original_name_without_ext"):
            names.add(f"{tracking_info.get('original_name_without_ext')}.pdf")
        names.add(tracking_info.get("expected_ai_pdf_name"))
        return {key for key in (cls._editor_file_key(name) for name in names if name) if key}

    @classmethod
    def _editor_tracking_matches_file(cls, doc_name: str, tracking_info: dict) -> bool:
        doc_key = cls._editor_file_key(doc_name)
        return bool(doc_key and doc_key in cls._editor_tracking_keys(tracking_info or {}))

    @staticmethod
    def _new_editor_tracking_key(prefix: str, sent_message) -> str:
        chat_id = getattr(getattr(sent_message, "chat", None), "id", None)
        if chat_id is None:
            return f"{prefix}_{sent_message.id}"
        return f"{prefix}_{chat_id}_{sent_message.id}"

    @staticmethod
    def _editor_response_identity(message):
        message_id = getattr(message, "id", None)
        chat_id = getattr(getattr(message, "chat", None), "id", None)
        if message_id is None or chat_id is None:
            return None
        return str(chat_id), str(message_id)

    def _cleanup_editor_response_state(self, now=None):
        current_time = now or datetime.now()
        cutoff = current_time - timedelta(hours=24)
        for state in (self._editor_response_claims, self._editor_response_unknown_warnings):
            expired = [key for key, created_at in state.items() if created_at <= cutoff]
            for key in expired:
                state.pop(key, None)
            if len(state) > 4096:
                oldest = sorted(state.items(), key=lambda item: item[1])[:-4096]
                for key, _created_at in oldest:
                    state.pop(key, None)

    def _claim_editor_response(self, message):
        self._cleanup_editor_response_state()
        identity = self._editor_response_identity(message)
        if identity is None:
            return None, True
        if identity in self._editor_response_claims:
            return identity, False
        self._editor_response_claims[identity] = datetime.now()
        return identity, True

    def _editor_response_was_claimed(self, message):
        self._cleanup_editor_response_state()
        identity = self._editor_response_identity(message)
        return identity is not None and identity in self._editor_response_claims

    def _release_editor_response_claim(self, identity):
        if identity is not None:
            self._editor_response_claims.pop(identity, None)

    def _warn_unknown_editor_response(self, message, reply_to_message_id):
        self._cleanup_editor_response_state()
        identity = self._editor_response_identity(message)
        if identity is not None and identity in self._editor_response_unknown_warnings:
            return
        if identity is not None:
            self._editor_response_unknown_warnings[identity] = datetime.now()
        print(f"⚠️  Не найдена информация об отправке для сообщения {reply_to_message_id} в НИК-2")

    def _editor_report_is_pending(self, file_name: str, tracking_info: dict) -> bool:
        if not tracking_info.get("fixed_author_editor_route"):
            return True
        delivered_reports = tracking_info.get("delivered_reports") or set()
        return self._editor_report_key(file_name) not in delivered_reports

    def _editor_tracking_is_recently_completed(self, tracking_info: dict, report_name=None, now=None) -> bool:
        if not tracking_info.get("fixed_author_editor_route"):
            return False
        sent_at = tracking_info.get("sent_at")
        if not isinstance(sent_at, datetime):
            return False
        current_time = now or datetime.now()
        try:
            age = current_time - sent_at
        except TypeError:
            return False
        if age < timedelta(0) or age > timedelta(hours=24):
            return False
        expected_reports = {
            self._editor_report_key(name)
            for name in (
                tracking_info.get("expected_pdf_name"),
                tracking_info.get("expected_ai_pdf_name"),
            )
            if name
        }
        delivered_reports = {
            self._editor_report_key(name)
            for name in (tracking_info.get("delivered_reports") or set())
            if name
        }
        if report_name:
            return self._editor_report_key(report_name) in delivered_reports
        return bool(expected_reports) and expected_reports.issubset(delivered_reports)

    def _find_editor_tracking_by_reply(self, reply_to_message_id, chat_id=None, sent_from_account="НИК-2"):
        if not reply_to_message_id:
            return None, None

        candidates = [
            (key, info)
            for key, info in self.editor_tracking.items()
            if info.get("sent_from_account") == sent_from_account
            and info.get("reply_to_message_id") == reply_to_message_id
        ]
        if chat_id is not None:
            known_chat_candidates = [
                (key, info) for key, info in candidates if info.get("chat_id") is not None
            ]
            chat_matches = [
                (key, info)
                for key, info in known_chat_candidates
                if str(info.get("chat_id")) == str(chat_id)
            ]
            if len(chat_matches) == 1 and len(known_chat_candidates) == len(candidates):
                return chat_matches[0]
            if known_chat_candidates:
                return None, None
            if len(candidates) == 1:
                return candidates[0]
            return None, None
        if len(candidates) == 1:
            return candidates[0]
        return None, None

    @staticmethod
    def _extract_editor_error_file_name(text: str) -> str | None:
        if not text:
            return None
        match = re.search(
            r"файл\s*:\s*([^\r\n]+?\.(?:docx?|rtf|pdf|odt|txt))\b",
            str(text),
            re.IGNORECASE,
        )
        if not match:
            return None
        return os.path.basename(match.group(1).strip())

    @classmethod
    def _editor_name_score(cls, doc_name: str, tracking_info: dict, ordinal: int) -> tuple[int, int]:
        doc_stem = os.path.splitext(os.path.basename(str(doc_name or "")))[0].lower().strip()
        doc_base, doc_suffix = cls._strip_extension_and_tg_suffix(doc_name)
        expected_name = tracking_info.get("expected_pdf_name") or ""
        original_name = tracking_info.get("original_name") or tracking_info.get("original_file_name") or ""
        original_stem = (tracking_info.get("original_name_without_ext") or os.path.splitext(original_name)[0]).lower().strip()
        expected_stem = os.path.splitext(os.path.basename(str(expected_name)))[0].lower().strip()
        original_base, original_suffix = cls._strip_extension_and_tg_suffix(original_stem)

        score = 0
        if doc_stem and doc_stem == expected_stem:
            score += 100
        if doc_stem and doc_stem == original_stem:
            score += 90
        if doc_base and doc_base in {original_base, cls._strip_extension_and_tg_suffix(expected_stem)[0]}:
            score += 60
        if doc_suffix is not None and doc_suffix == original_suffix:
            score += 25
        if doc_suffix is not None and original_suffix is None and doc_suffix == ordinal:
            score += 20
        if "анти" in doc_stem and "анти" in original_stem:
            score += 10
        return score, -ordinal

    def find_editor_tracking_for_unreplied_pdf(
        self,
        sender: str,
        doc_name: str,
        sent_from_account: str = "НИК-2",
        reply_chat_id=None,
    ):
        sender_clean = str(sender or "").replace("@", "").lower()
        candidates = [
            (key, info)
            for key, info in self.editor_tracking.items()
            if info.get("sent_from_account") == sent_from_account
            and str(info.get("destination", "")).replace("@", "").lower() == sender_clean
            and (
                reply_chat_id is None
                or info.get("chat_id") is None
                or str(info.get("chat_id")) == str(reply_chat_id)
            )
            and self._editor_report_is_pending(doc_name, info)
        ]
        if not candidates:
            return None, None, "нет ожидающих задач"
        matches = [
            (key, info)
            for key, info in candidates
            if self._editor_tracking_matches_file(doc_name, info)
        ]
        if len(matches) == 1:
            matched_key, matched_info = matches[0]
            sender_clean = str(sender or "").replace("@", "").lower()
            completed_conflicts = [
                info
                for key, info in self.editor_tracking.items()
                if key != matched_key
                and self._editor_tracking_is_recently_completed(info, doc_name)
                and info.get("sent_from_account") == sent_from_account
                and str(info.get("destination", "")).replace("@", "").lower() == sender_clean
                and (
                    reply_chat_id is None
                    or info.get("chat_id") is None
                    or str(info.get("chat_id")) == str(reply_chat_id)
                )
                and self._editor_tracking_matches_file(doc_name, info)
            ]
            if completed_conflicts:
                return None, None, f"неоднозначное имя файла: {doc_name} (есть недавно завершенная задача)"
            return matched_key, matched_info, "точное совпадение имени файла"
        if len(matches) > 1:
            return None, None, f"неоднозначное имя файла: {doc_name}"
        return None, None, f"нет точного совпадения имени файла: {doc_name}"

    def find_editor_tracking_for_text_response(
        self,
        sender: str,
        reply_to_message_id,
        sent_from_account: str = "НИК-2",
        message_text: str | None = None,
        reply_chat_id=None,
    ):
        """Find editor tracking for a text response to a file sent through NIK-2."""
        sender_clean = str(sender or "").replace("@", "").lower()

        if reply_to_message_id:
            key, info = self._find_editor_tracking_by_reply(
                reply_to_message_id,
                chat_id=reply_chat_id,
                sent_from_account=sent_from_account,
            )
            if info:
                dest_clean = str(info.get("destination", "")).replace("@", "").lower()
                if not sender_clean or not dest_clean or dest_clean == sender_clean:
                    return key, info, "reply_to sent editor message"
            return None, None, "нет однозначного совпадения reply"

        text_file_name = self._extract_editor_error_file_name(message_text)
        if text_file_name:
            return self.find_editor_tracking_for_unreplied_pdf(
                sender,
                text_file_name,
                sent_from_account=sent_from_account,
                reply_chat_id=reply_chat_id,
            )

        candidates = [
            (key, info)
            for key, info in self.editor_tracking.items()
            if info.get("sent_from_account") == sent_from_account
            and str(info.get("destination", "")).replace("@", "").lower() == sender_clean
            and (
                reply_chat_id is None
                or info.get("chat_id") is None
                or str(info.get("chat_id")) == str(reply_chat_id)
            )
        ]
        if len(candidates) == 1:
            return candidates[0][0], candidates[0][1], "единственная ожидающая задача редактора"

        return None, None, "нет совпадения"

    def find_anti_processing_for_response(self, client_name: str, message):
        candidates = [
            (key, info)
            for key, info in self.current_processing_files.items()
            if info.get("account") == client_name and info.get("bot_type") == "anti"
        ]
        if not candidates:
            return None, None, "нет файлов Плагискана в обработке"

        if message.reply_to_message_id:
            map_key = f"{message.chat.id}_{message.reply_to_message_id}"
            mapped_key = self.message_to_file_map.get(map_key)
            if mapped_key:
                for key, info in candidates:
                    if key == mapped_key:
                        return key, info, "reply_to sent_message_id"

        candidates.sort(key=lambda item: item[0])
        response_name = ""
        if getattr(message, "document", None) and getattr(message.document, "file_name", None):
            response_name = message.document.file_name
        elif getattr(message, "text", None):
            response_name = message.text

        scored = [
            (self._editor_name_score(response_name, info, ordinal), key, info)
            for ordinal, (key, info) in enumerate(candidates)
        ]
        scored.sort(key=lambda item: item[0], reverse=True)
        best_score, best_key, best_info = scored[0]
        if best_score[0] > 0:
            return best_key, best_info, f"совпадение ответа с именем файла score={best_score[0]}"

        return candidates[0][0], candidates[0][1], "нет reply/имени, выбран самый старый"

    @staticmethod
    def _is_no_checks_message(text):
        """Проверяет, указывает ли текст сообщения на отсутствие проверок.
        Возвращает True только если проверки ИСЧЕРПАНЫ (нет денег/проверок).
        'Проверка завершена' БЕЗ указания на отсутствие проверок — НЕ считается.

        Реальные сообщения от AAA бота:
        1. '❌ У вас пока нет проверок. Для покупки нажмите команду /pay 💳'
        2. 'Проверка завершена. У вас не осталось больше проверок'

        Реальные сообщения от Плагискан бота:
        3. 'У вас закончились проверки 😉\nПерейдите в раздел оплата /payment'
        4. '⏳ В данный момент проверки закончились.\nНачнем проверять после: ...'
        """
        if not text:
            return False
        text_lower = text.lower()
        return (
            "нет проверок" in text_lower                       # "У вас пока нет проверок"
            or "не осталось проверок" in text_lower             # "У вас больше не осталось проверок"
            or "не осталось больше проверок" in text_lower      # "У вас не осталось больше проверок"
            or "закончились проверки" in text_lower             # "У вас закончились проверки 😉"
            or "проверки закончились" in text_lower             # "В данный момент проверки закончились"
            or ("payment" in text_lower and "проверк" in text_lower)  # /payment + проверк
        )

    @staticmethod
    def _is_global_aaa_unavailable_message(text):
        if not text:
            return False
        return "в настоящее время нет доступных проверок" in text.lower()

    @staticmethod
    def _is_plagiscan_success_message(text):
        if not text:
            return False
        text_lower = text.lower()
        return "ваш файл успешно проверен" in text_lower or "оригинальность:" in text_lower

    @staticmethod
    def _is_ai_upload_prompt(text):
        if not text:
            return False
        text_lower = text.lower()
        return (
            "пожалуйста, загрузите файл для проверки" in text_lower
            or (
                "у вас осталось" in text_lower
                and "допустимые форматы" in text_lower
                and "максимальный размер" in text_lower
            )
        )

    @staticmethod
    def _extract_machine_generation_percent(text):
        if not text:
            return None
        match = re.search(r'Машинная\s+генерация:\s*([\d.,]+)\s*%', text, re.IGNORECASE)
        if not match:
            return None
        try:
            return float(match.group(1).replace(",", "."))
        except ValueError:
            return None

    @staticmethod
    def _is_ai_report_button(button):
        text = (getattr(button, "text", None) or "").lower()
        return "ии" in text or "ai" in text

    @classmethod
    def _find_plagiscan_report_buttons(cls, message):
        normal_button = None
        ai_button = None
        if not (getattr(message, "reply_markup", None) and hasattr(message.reply_markup, "inline_keyboard")):
            return normal_button, ai_button

        for row in message.reply_markup.inline_keyboard:
            for button in row:
                text = (getattr(button, "text", None) or "").lower()
                if "отчет" not in text and "report" not in text and "скачать" not in text:
                    continue
                if cls._is_ai_report_button(button):
                    ai_button = ai_button or button
                elif "справк" not in text:
                    normal_button = normal_button or button
        return normal_button, ai_button

    def _get_ai_upload_ready_event(self, account_name):
        event = self.ai_upload_ready_events.get(account_name)
        if not event:
            event = asyncio.Event()
            self.ai_upload_ready_events[account_name] = event
        return event

    async def _wait_for_ai_upload_prompt(self, account_name, file_key, timeout=None):
        """Ждёт приглашение AAA к загрузке файла или раннее завершение обработки."""
        if timeout is None:
            timeout = Config.get_setting("ai_upload_prompt_timeout", 5)
        event = self._get_ai_upload_ready_event(account_name)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        while loop.time() < deadline:
            if event.is_set():
                return True
            if file_key not in self.current_processing_files:
                return False

            remaining = deadline - loop.time()
            try:
                await asyncio.wait_for(event.wait(), timeout=min(0.5, remaining))
                return True
            except asyncio.TimeoutError:
                continue

        return event.is_set()

    async def _run_blocking(self, func, *args):
        """Запускает блокирующую операцию; в тестах может работать синхронно."""
        if Config.get_setting("sync_blocking_ops", False):
            return func(*args)
        return await asyncio.to_thread(func, *args)

    async def _delayed_process_queue(self, delay_seconds):
        if delay_seconds and delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        await self.process_queue()

    def _get_normal_accounts(self):
        normal_accounts = Config.get_setting("normal_accounts") or []
        if isinstance(normal_accounts, str):
            normal_accounts = [item.strip() for item in normal_accounts.split(",") if item.strip()]
        return [str(account).strip() for account in normal_accounts if str(account).strip()]

    def is_force_plagiscan_author(self, author=None, author_id=None, route_sender_id=None, route_sender_name=None):
        """True если автор входит в отдельный список принудительных авторов.

        Правила сопоставления:
        - Явная VK-запись (vk: / vk.com/ / https://vk.com/):
            сравнивается только с route_sender_id и route_sender_name.
        - Любая другая запись (username, @username, числовой ID):
            сравнивается со ВСЕМИ полями — и TG, и VK.
        """
        configured_users = self._configured_user_list("plagiscan_users")
        if not configured_users:
            return False

        for configured in configured_users:
            if self._force_author_entry_matches(
                configured,
                author=author,
                author_id=author_id,
                route_sender_id=route_sender_id,
                route_sender_name=route_sender_name,
            ):
                return True
        return False

    def is_force_plagiscan_file_info(self, file_info):
        route = self.get_force_author_route(file_info)
        return bool(route and route.get("destination") == "plagiscan")

    def _activate_global_aaa_unavailable(self, trigger_account=None):
        if not self.aaa_globally_unavailable:
            print("⛔ AAA бот сообщает, что проверок нет глобально — переключаем обычные файлы на редактора")
        self.aaa_globally_unavailable = True

        for account in self._get_normal_accounts():
            if account not in self.blacklisted_accounts:
                self.blacklisted_accounts.append(account)
            self.manager.blacklist_account(account)

        if trigger_account:
            print(f"⛔ Триггер глобальной блокировки AAA: {trigger_account}")

    async def _handle_global_aaa_unavailable(self, client, message, processing_info=None, file_key=None):
        self._activate_global_aaa_unavailable(trigger_account=client.name)

        if processing_info:
            print("📤 Текущий файл перенаправляем редактору из-за глобального отсутствия проверок в AAA")
            file_info_to_send = {
                "author": processing_info["author"],
                "author_id": processing_info.get("author_id"),
                "file_name": processing_info["original_file_name"],
                "message": processing_info.get("message"),
                "chat_id": processing_info["chat_id"],
                "original_file_name": processing_info["original_file_name"],
                "file_uid": processing_info.get("file_uid"),
                "local_path": processing_info.get("local_path"),
                "gateway_job_id": processing_info.get("gateway_job_id"),
                "source_platform": processing_info.get("source_platform", "telegram"),
                "route_sender_id": processing_info.get("route_sender_id"),
                "route_chat_id": processing_info.get("route_chat_id"),
                "route_message_id": processing_info.get("route_message_id"),
                "route_is_group": processing_info.get("route_is_group", False),
            }

            if not file_info_to_send["message"]:
                file_info_to_send["message"] = message

            await self.send_to_nik2(file_info_to_send, reason="в AAA боте нет доступных проверок")

            self.manager.mark_account_free(client.name, "")
            print(f"🔄 Аккаунт {client.name} освобожден после глобального no_checks")

            if file_key in self.current_processing_files:
                del self.current_processing_files[file_key]
                print(f"🗑️  Удален файл {file_key} из обработки")

            original_file_path = processing_info.get("temp_path")
            if original_file_path and os.path.exists(original_file_path):
                os.remove(original_file_path)
                print(f"🗑️  Удален временный файл: {original_file_path}")

            self.cleanup_account_mappings(client.name)

        asyncio.create_task(self.process_queue())

    async def _requeue_after_missing_ai_prompt(self, account, file_key, file_info, temp_path):
        retry_delay = Config.get_setting("ai_upload_retry_delay", 30)

        requeue_info = {
            "author": file_info["author"],
            "author_id": file_info.get("author_id"),
            "file_name": file_info["original_file_name"],
            "original_file_name": file_info["original_file_name"],
            "message_id": file_info["message_id"],
            "chat_id": file_info["chat_id"],
            "is_anti": False,
            "received_at": file_info.get("received_at", datetime.now()),
            "status": "ожидание приглашения AAA",
            "message": file_info["message"],
            "sent_to_editor": False,
            "file_uid": file_info.get("file_uid"),
            "local_path": file_info.get("local_path"),
            "gateway_job_id": file_info.get("gateway_job_id"),
            "source_platform": file_info.get("source_platform", "telegram"),
            "route_sender_id": file_info.get("route_sender_id"),
            "route_chat_id": file_info.get("route_chat_id"),
            "route_message_id": file_info.get("route_message_id"),
            "route_is_group": file_info.get("route_is_group", False),
        }
        requeue_info["ai_prompt_retry_count"] = file_info.get("ai_prompt_retry_count", 0) + 1

        self.manager.mark_account_free(account, file_info["file_name"])
        print(f"🔄 Аккаунт {account} освобожден после таймаута ожидания приглашения AAA")

        if file_key in self.current_processing_files:
            del self.current_processing_files[file_key]
            print(f"🗑️  Удален файл {file_key} из обработки")

        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
            print(f"🗑️  Удален временный файл: {temp_path}")

        self.manager.file_queue.insert(0, requeue_info)
        print(
            f"⏳ Приглашение AAA не пришло вовремя. Файл {requeue_info['file_name']} возвращен в очередь, "
            f"повтор через {retry_delay} сек."
        )

        asyncio.create_task(self._delayed_process_queue(retry_delay))

    def _setup_periodic_cleanup(self):
        """Настройка периодической очистки устаревших записей"""
        import threading
        import time

        def cleanup_task():
            while True:
                time.sleep(3600)  # Проверяем каждый час
                self.cleanup_old_editor_tracking()

        # Запускаем в отдельном потоке
        cleanup_thread = threading.Thread(target=cleanup_task, daemon=True)
        cleanup_thread.start()

    @staticmethod
    def _build_work_file_path(file_info, prefix=""):
        original_name = os.path.basename(
            file_info.get("original_file_name")
            or file_info.get("file_name")
            or "document.bin"
        )
        # Fix #6: уникальный суффикс (uuid) для предотвращения коллизий при параллельной обработке
        uid_prefix = str(file_info.get("file_uid", "")).strip()[:20]
        unique_suffix = uuid.uuid4().hex[:8]
        base_name = f"{uid_prefix}_{unique_suffix}_{original_name}" if uid_prefix else f"{unique_suffix}_{original_name}"
        if prefix:
            base_name = f"{prefix}_{base_name}"
        # Fix #5: абсолютный путь через FILES_DIR
        return os.path.join(FILES_DIR, base_name)

    @staticmethod
    def _build_result_file_path(file_name: str) -> str:
        safe_name = os.path.basename(file_name or "result.pdf")
        return os.path.join(FILES_DIR, f"{uuid.uuid4().hex[:8]}_{safe_name}")

    @staticmethod
    def _preferred_document_name(file_info, message=None, default="document.bin"):
        original_name = (
            (file_info or {}).get("original_file_name")
            or (file_info or {}).get("file_name")
        )
        if not original_name and message and getattr(message, "document", None):
            original_name = getattr(message.document, "file_name", None)
        return os.path.basename(original_name or default)

    async def _ensure_work_file(self, file_info, message=None, prefix=""):
        temp_path = self._build_work_file_path(file_info, prefix=prefix)
        local_path = file_info.get("local_path")
        if local_path and os.path.exists(local_path):
            shutil.copy2(local_path, temp_path)
            print(f"📥 Использую локальную копию файла: {local_path} -> {temp_path}")
            return temp_path

        source_message = file_info.get("message") or message
        if source_message and getattr(source_message, "document", None):
            downloaded_path = await source_message.download(temp_path)
            resolved_path = downloaded_path or temp_path
            print(f"📥 Файл скачан из исходного сообщения: {resolved_path}")
            return resolved_path

        # Fix #4: VK файлы — fallback скачивание по download_url из gateway job
        download_url = file_info.get("download_url")
        if not download_url:
            gateway_job_id = file_info.get("gateway_job_id")
            if gateway_job_id:
                try:
                    store = build_store()
                    job = await asyncio.to_thread(store.get_job, int(gateway_job_id))
                    if job and hasattr(job, "download_url") and job.download_url:
                        download_url = job.download_url
                except Exception as e:
                    print(f"⚠️ Не удалось получить download_url из gateway: {e}")

        if download_url:
            try:
                resp = await asyncio.to_thread(
                    lambda: requests.get(download_url, timeout=120)
                )
                resp.raise_for_status()
                with open(temp_path, "wb") as f:
                    f.write(resp.content)
                print(f"📥 Файл скачан по URL (VK fallback): {temp_path}")
                return temp_path
            except Exception as e:
                print(f"❌ Ошибка скачивания по URL: {e}")

        raise FileNotFoundError("Нет ни local_path, ни исходного сообщения, ни download_url для скачивания файла")

    async def _mark_gateway_job_done(self, context, result_path=None, note=None):
        gateway_job_id = context.get("gateway_job_id") if context else None
        if not gateway_job_id:
            return
        store = build_store()
        await asyncio.to_thread(store.mark_done, int(gateway_job_id), result_path, note)
        self._log_gateway(f"gateway_job_done job_id={gateway_job_id} note={note} result_path={result_path}")

    async def _mark_gateway_job_failed(self, context, error_text, retry_delay=300):
        gateway_job_id = context.get("gateway_job_id") if context else None
        if not gateway_job_id:
            return
        store = build_store()
        await asyncio.to_thread(store.mark_failed, int(gateway_job_id), str(error_text), retry_delay)
        self._log_gateway(f"gateway_job_failed job_id={gateway_job_id} error={error_text}")

    async def _mark_gateway_job_waiting_editor(self, context, note="waiting_editor_response"):
        gateway_job_id = context.get("gateway_job_id") if context else None
        if not gateway_job_id:
            return
        store = build_store()
        await asyncio.to_thread(store.mark_waiting_editor, int(gateway_job_id), note)
        self._log_gateway(f"gateway_job_waiting_editor job_id={gateway_job_id} note={note}")

    @staticmethod
    def _vk_result_message_text(file_name=None, caption=None):
        if file_name:
            base_name = os.path.basename(str(file_name))
            stem, _ext = os.path.splitext(base_name)
            return stem or base_name
        if caption:
            first_line = str(caption).splitlines()[0].strip()
            if first_line.lower().startswith("отчет:"):
                first_line = first_line.split(":", 1)[1].strip()
            stem, _ext = os.path.splitext(first_line)
            return stem or first_line
        return None

    async def _deliver_document_to_origin(self, context, document_path, file_name=None, caption=None, telegram_client=None):
        source_platform = (context.get("source_platform") or "telegram").lower()
        if caption is None and source_platform == "vk":
            original_name = context.get("original_name") or context.get("original_file_name") or context.get("file_name")
            if original_name and file_name and str(original_name) != str(file_name):
                caption = f"Отчет: {file_name}\nИсходный файл: {original_name}"
            elif file_name:
                caption = f"Отчет: {file_name}"

        if source_platform == "telegram":
            client_nik1 = telegram_client or self.manager.get_client("НИК-1")
            if not client_nik1:
                raise RuntimeError("Аккаунт НИК-1 не найден для отправки результата")

            route_chat_id = context.get("route_chat_id")
            recipient = (
                route_chat_id
                if route_chat_id is not None
                else context.get("author_id") or context.get("route_sender_id") or context.get("author")
            )
            if isinstance(recipient, str) and re.fullmatch(r"-?\d+", recipient):
                recipient = int(recipient)
            elif isinstance(recipient, str) and not recipient.startswith("@"):
                recipient = f"@{recipient}"

            route_is_group = context.get("route_is_group")
            if route_is_group is None:
                route_is_group = (
                    route_chat_id is not None
                    and context.get("author_id") is not None
                    and str(route_chat_id) != str(context.get("author_id"))
                )
            send_kwargs = {
                "chat_id": recipient,
                "document": document_path,
                "file_name": file_name,
                "caption": caption[:1024] if caption else None,
            }
            if route_is_group and context.get("route_message_id") is not None:
                send_kwargs["reply_to_message_id"] = context["route_message_id"]

            if not client_nik1.is_connected:
                try:
                    await client_nik1.connect()
                except Exception as e:
                    print(f"⚠️ Не удалось предварительно подключить НИК-1: {e}")

            # Fix #1: при устойчивой ошибке Telegram — spool в outbox вместо потери файла
            last_exc = None
            for attempt in range(2):
                try:
                    await asyncio.wait_for(
                        client_nik1.send_document(**send_kwargs),
                        timeout=60,
                    )
                    self._log_gateway(f"deliver_result platform=telegram recipient={recipient} file_name={file_name}")
                    return {"platform": "telegram", "recipient": recipient}
                except FloodWait as e:
                    wait_sec = e.value
                    print(f"⚠️ FloodWait на НИК-1: ждём {wait_sec}с (попытка {attempt + 1}/2)")
                    last_exc = e
                    if attempt == 0 and wait_sec <= 120:
                        await asyncio.sleep(wait_sec)
                        continue
                    break
                except asyncio.TimeoutError as e:
                    last_exc = e
                    if attempt == 0:
                        print("⚠️ Таймаут отправки через НИК-1, повторяем")
                        await asyncio.sleep(3)
                        continue
                    break
                except Exception as e:
                    last_exc = e
                    print(f"⚠️ Ошибка отправки через Telegram (попытка {attempt + 1}/2): {e}")
                    if attempt == 0:
                        await asyncio.sleep(3)
                        continue
                    break

            # Telegram отправка провалилась — сохраняем в outbox для повторной доставки
            print(f"⚠️ Telegram отправка провалилась после 2 попыток, сохраняем в outbox: {last_exc}")
            self._log_gateway(f"telegram_delivery_failed recipient={recipient} error={last_exc} — spooling to outbox")
            route = {
                "chat_id": context.get("route_chat_id") or context.get("chat_id"),
                "sender_id": context.get("route_sender_id") or context.get("author_id") or context.get("author"),
            }
            if route_is_group and context.get("route_message_id") is not None:
                route["reply_to_message_id"] = context["route_message_id"]
            try:
                manifest = self.outbound_dispatcher.outbox.spool_delivery(
                    source_platform="telegram",
                    route=route,
                    file_path=document_path,
                    caption=caption,
                    file_name=file_name,
                    reason=str(last_exc),
                )
                self._log_gateway(f"telegram_spooled outbox_id={manifest['id']}")
                return {"platform": "telegram", "status": "spooled", "outbox_id": manifest["id"], "reason": str(last_exc)}
            except Exception as spool_exc:
                print(f"❌ Не удалось сохранить в outbox: {spool_exc}")
                raise RuntimeError(f"Telegram отправка и spool провалились: send={last_exc}, spool={spool_exc}")

        route = {
            "chat_id": context.get("route_chat_id") or context.get("chat_id"),
            "sender_id": context.get("route_sender_id") or context.get("author_id") or context.get("author"),
        }
        last_exc = None
        result = None
        vk_caption = self._vk_result_message_text(file_name=file_name, caption=caption) if source_platform == "vk" else caption
        for attempt in range(1, 11):
            try:
                result = await asyncio.to_thread(
                    self.outbound_dispatcher.send_result,
                    source_platform,
                    route,
                    document_path,
                    vk_caption,
                    file_name,
                    False,
                )
                break
            except Exception as exc:
                last_exc = exc
                self._log_gateway(
                    f"deliver_result_retry platform={source_platform} route={route} "
                    f"file_name={file_name} attempt={attempt}/10 error={exc}"
                )
                print(
                    f"⚠️ Ошибка отправки результата в {source_platform.upper()} "
                    f"(попытка {attempt}/10): {exc}"
                )
                if attempt < 10:
                    await asyncio.sleep(min(3 * attempt, 30))

        if result is None:
            result = await asyncio.to_thread(
                self.outbound_dispatcher.send_result,
                source_platform,
                route,
                document_path,
                vk_caption,
                file_name,
                True,
            )
            if isinstance(result, dict) and result.get("status") == "spooled":
                self._log_gateway(
                    f"deliver_result_failed_after_retries platform={source_platform} route={route} "
                    f"file_name={file_name} attempts=10 final_error={last_exc} outbox_id={result.get('outbox_id')}"
                )

        status = result.get("status", "sent") if isinstance(result, dict) else "sent"
        reason = result.get("reason") if isinstance(result, dict) else None
        reason_text = f" reason={reason}" if reason else ""
        self._log_gateway(
            f"deliver_result platform={source_platform} route={route} file_name={file_name} "
            f"status={status}{reason_text}"
        )
        if isinstance(result, dict):
            return result
        return {"platform": source_platform, "status": status, "response": result}

    def cleanup_old_editor_tracking(self):
        """Очистка старых записей отслеживания редактора (старше 24 часов)"""
        from datetime import datetime, timedelta

        current_time = datetime.now()
        self._cleanup_editor_response_state(current_time)
        to_remove = []

        for msg_id, tracking_info in self.editor_tracking.items():
            sent_at = tracking_info.get("sent_at")
            if isinstance(sent_at, datetime) and current_time - sent_at > timedelta(hours=24):
                to_remove.append(msg_id)

        for msg_id in to_remove:
            del self.editor_tracking[msg_id]
            print(f"🗑️  Удалена устаревшая запись отслеживания редактора: {msg_id}")

        if to_remove:
            print(f"✅ Очищено {len(to_remove)} устаревших записей отслеживания")

    async def _try_start_processing(self, file_uid: str) -> bool:
        """Атомарно проверяет и переводит файл в статус IN_PROGRESS.
        Возвращает True если можно обрабатывать, False если уже обрабатывается/обработан."""
        async with self._processing_lock:
            if file_uid in self.processed_files:
                print(f"⏭️  Файл уже обрабатывается/обработан [{self.processed_files[file_uid]}]: {file_uid[:20]}...")
                return False
            self.processed_files[file_uid] = "IN_PROGRESS"
            return True

    def _finish_processing(self, file_uid: str, success: bool = True):
        """Переводит файл в статус DONE или FAILED."""
        if file_uid:
            self.processed_files[file_uid] = "DONE" if success else "FAILED"
            # Синхронизируем с AccountManager
            asyncio.create_task(self.manager.set_file_status(file_uid, "DONE" if success else "FAILED"))

    def is_author_allowed(
        self,
        username,
        author_id=None,
        *,
        route_chat_id=None,
        route_is_group=False,
        source_platform="telegram",
    ):
        """Проверка, разрешен ли автор (только для входящих сообщений от пользователей)"""
        # Очищаем username от @
        username_clean = username[1:] if username.startswith('@') else username

        if self._telegram_group_route_for_file_info(
            {
                "source_platform": source_platform,
                "route_chat_id": route_chat_id,
                "route_is_group": route_is_group,
            }
        ):
            print("✅ Сообщение из настроенной Telegram-беседы разрешено")
            return True

        # Сначала проверяем, является ли это ожидаемым редактором
        for msg_id, tracking_info in self.editor_tracking.items():
            dest = tracking_info.get("destination", "")
            if dest:
                dest_clean = dest[1:] if dest.startswith('@') else dest
                if dest_clean == username_clean:
                    print(f"✅ Автор {username} - ожидаемый редактор (из tracking)")
                    return True

        # Для сообщений от известных ботов всегда разрешаем
        bot_usernames = [
            Config.get_setting("ai_bot", "@AAA_Report_AIBot").replace('@', ''),
            Config.get_setting("anti_bot", "@plagaiscan_bot").replace('@', ''),
            'plagaiscan_bot',
            'AAA_Report_AIBot'
        ]

        if username_clean in bot_usernames or username_clean.endswith('_bot'):
            print(f"✅ Автор {username} - известный бот")
            return True

        # Пользователи принудительного Плагискана должны проходить фильтр
        # allowed_authors, иначе файл отсекается до force_plagiscan-маршрутизации.
        # В Telegram-входе здесь нет route_sender_id/name, поэтому явные vk: записи
        # не считаем Telegram-авторами.
        for entry in self._configured_user_list("plagiscan_users"):
            if self._force_author_entry_matches(
                entry,
                author=username,
                author_id=author_id,
            ):
                return True

        # Получаем список разрешенных авторов из настроек
        allowed_authors = Config.get_setting("allowed_authors", [])

        # Если список пустой - разрешены все авторы
        if not allowed_authors:
            return True

        for entry in allowed_authors:
            if _user_tokens_match(entry, username) or _user_tokens_match(entry, author_id):
                return True

        # Для VK файлов: принимаем и "vk:123" и просто "123" в allowed_authors
        for entry in allowed_authors:
            if _is_vk_id(entry) and _normalize_vk_id(entry) == _normalize_vk_id(username_clean):
                return True

        return False

    def is_explicit_allowed_author(self, username):
        """True только если username явно указан в allowed_authors."""
        allowed_authors = Config.get_setting("allowed_authors", [])
        if not allowed_authors:
            return False

        username_clean = (username or "").lstrip("@")
        for entry in allowed_authors:
            entry_clean = str(entry).lstrip("@")
            if entry_clean.lower() == username_clean.lower():
                return True
        return False

    def is_bot_message(self, username):
        """Проверяет, является ли сообщение от бота"""
        bot_usernames = [
            Config.get_setting("ai_bot").replace('@', ''),
            Config.get_setting("anti_bot").replace('@', ''),
            'plagaiscan_bot',
            'AAA_Report_AIBot'
        ]
        username_clean = (username or "").replace("@", "")
        return username_clean in bot_usernames or username_clean.endswith("_bot")

    def is_within_working_hours(self):
        """Проверка, находимся ли мы в рабочее время (для режима 2)"""
        current_mode = Config.get_setting("mode")
        if current_mode != "mode2":
            return True

        try:
            start_str = Config.get_setting("time_start")
            end_str = Config.get_setting("time_end")

            if not start_str or not end_str:
                return True

            interval = self._get_mode2_work_interval()
            if not interval:
                print(f"⚠️  Неверный формат времени: {start_str} - {end_str}")
                return True

            now, start_dt, end_exclusive = interval
            if start_dt <= now < end_exclusive:
                print(f"✅ В рабочее время ({start_str} - {end_str})")
                return True

            print(f"⏰ Вне рабочего времени ({start_str} - {end_str})")
            print(f"   Текущее время: {now.strftime('%H:%M')}")
            print(f"   Все файлы отправляются круглосуточному редактору")
            return False

        except Exception as e:
            print(f"⚠️  Ошибка проверки рабочего времени: {e}")
            import traceback
            traceback.print_exc()
            return True

    @staticmethod
    def _parse_mode2_time(time_str):
        try:
            parts = str(time_str).split(':')
            if len(parts) == 2:
                hours = int(parts[0])
                minutes = int(parts[1])
                return time(hours, minutes)
        except (ValueError, TypeError, IndexError):
            pass
        return None

    def _get_mode2_work_interval(self):
        """Возвращает (now, start_dt, end_exclusive) для текущего рабочего окна MODE2."""
        start_str = Config.get_setting("time_start")
        end_str = Config.get_setting("time_end")
        start_time = self._parse_mode2_time(start_str)
        end_time = self._parse_mode2_time(end_str)
        if not start_time or not end_time:
            return None

        now = datetime.now()
        start_dt = datetime.combine(now.date(), start_time)
        # В настройке 17:00-17:20 вся минута 17:20 рабочая, переключение с 17:21.
        end_exclusive = datetime.combine(now.date(), end_time) + timedelta(minutes=1)

        if end_exclusive <= start_dt:
            if now >= start_dt:
                end_exclusive += timedelta(days=1)
            else:
                start_dt -= timedelta(days=1)

        return now, start_dt, end_exclusive

    def _ensure_mode2_interval_counter(self):
        interval = self._get_mode2_work_interval()
        if not interval:
            return None

        now, start_dt, end_exclusive = interval
        if not (start_dt <= now < end_exclusive):
            return interval

        if (
            self.daily_counter.get("interval_start") != start_dt
            or self.daily_counter.get("interval_end") != end_exclusive
        ):
            self.daily_counter = {
                "count": 0,
                "date": now.date(),
                "interval_start": start_dt,
                "interval_end": end_exclusive,
            }
            print(
                f"🔄 Сброшен счетчик рабочего интервала: "
                f"{start_dt.strftime('%Y-%m-%d %H:%M')} - "
                f"{(end_exclusive - timedelta(minutes=1)).strftime('%Y-%m-%d %H:%M')}"
            )

        return interval

    def check_daily_limit(self):
        """Проверка лимита файлов внутри текущего рабочего интервала (для режима 2)."""
        current_mode = Config.get_setting("mode")
        if current_mode != "mode2":
            return True

        interval = self._ensure_mode2_interval_counter()
        if interval:
            now, start_dt, end_exclusive = interval
            if not (start_dt <= now < end_exclusive):
                return False
        else:
            today = datetime.now().date()
            if self.daily_counter.get("date") != today:
                self.daily_counter = {"count": 0, "date": today, "interval_start": None, "interval_end": None}
                print(f"🔄 Сброшен счетчик: новый день {today}")

        max_files = Config.get_setting("max_files_per_day")
        if max_files is None:
            max_files = 50

        print(f"📊 Статистика рабочего интервала: {self.daily_counter['count']}/{max_files} файлов (только обычные)")

        if self.daily_counter["count"] >= max_files:
            print(f"⚠️  Достигнут лимит обычных файлов в рабочем интервале: {self.daily_counter['count']}/{max_files}")
            print(f"   Анти-файлы отправляются всегда, обычные - редактору")
            return False

        return True

    def _increment_mode2_limit_counter(self):
        """Учитывает обычный файл только в активном рабочем окне режима 2."""
        if Config.get_setting("mode") != "mode2":
            return
        interval = self._ensure_mode2_interval_counter()
        if not interval:
            return
        now, start_dt, end_exclusive = interval
        if start_dt <= now < end_exclusive:
            self.daily_counter["count"] = self.daily_counter.get("count", 0) + 1

    async def handle_main_account(self, client, message):
        """Обработка сообщений в основном аккаунте (НИК-1)"""
        if not message.document and not message.text:
            return

        if not getattr(message, "from_user", None):
            return

        if getattr(client, "name", None) == "НИК-2" and self._editor_response_was_claimed(message):
            return

        # Текстовые сообщения обрабатываем только если это ответ редактора или сообщение бота
        if not message.document:
            if not message.from_user:
                return
            author = message.from_user.username or str(message.from_user.id)
            tracking_key, tracking_info, match_reason = self.find_editor_tracking_for_text_response(
                author,
                message.reply_to_message_id,
                message_text=message.text,
                reply_chat_id=getattr(getattr(message, "chat", None), "id", None),
            )
            if tracking_key:
                if not message.reply_to_message_id and tracking_info.get("reply_to_message_id"):
                    resolved_reply_to_message_id = tracking_info.get("reply_to_message_id")
                else:
                    resolved_reply_to_message_id = None
                print(f"\n📨 Новое сообщение от {author}")
                print(f"✅ Получен ожидаемый текстовый ответ от редактора {author} ({match_reason})")
                if resolved_reply_to_message_id is None:
                    await self.handle_editor_response(client, message)
                else:
                    await self.handle_editor_response(
                        client,
                        message,
                        resolved_reply_to_message_id=resolved_reply_to_message_id,
                    )
            elif self.is_bot_message(author):
                print(f"\n📨 Новое сообщение от {author}")
                await self.handle_bot_response(client, message)
            return

        author = message.from_user.username or str(message.from_user.id) if message.from_user else "unknown"
        print(f"\n📨 Новое сообщение от {author}")

        # Сначала проверяем, является ли это ответом на ожидаемое сообщение
        if message.reply_to_message_id:
            tracking_key, tracking_info = self._find_editor_tracking_by_reply(
                message.reply_to_message_id,
                chat_id=getattr(getattr(message, "chat", None), "id", None),
                sent_from_account="НИК-2",
            )
            if tracking_info or message.reply_to_message_id in self.editor_tracking:
                print(f"✅ Получен ожидаемый ответ от редактора {author}")
                await self.handle_editor_response(client, message)
                return
            elif getattr(client, "name", None) == "НИК-2":
                if message.document and message.document.file_name.lower().endswith(".pdf"):
                    self._warn_unknown_editor_response(message, message.reply_to_message_id)
                return

        # Проверяем по отправителю и формату файла
        if not message.reply_to_message_id and message.document and message.document.file_name.lower().endswith('.pdf'):
            tracking_key, tracking_info, match_reason = self.find_editor_tracking_for_unreplied_pdf(
                author,
                message.document.file_name,
                sent_from_account="НИК-2",
                reply_chat_id=getattr(getattr(message, "chat", None), "id", None),
            )
            if tracking_info:
                print(f"✅ Обнаружен PDF файл от ожидаемого редактора {author} ({match_reason})")
                await self.handle_editor_response(
                    client,
                    message,
                    resolved_reply_to_message_id=tracking_info.get("reply_to_message_id"),
                )
                return
            if tracking_key is None and "нет ожидающих задач" not in str(match_reason):
                print(f"⚠️ PDF от редактора {author} не привязан: {message.document.file_name}, {match_reason}")

        # Telegram-бот с документом может быть явным разрешенным автором.
        # Тогда принимаем файл как входящий, а не как сервисный ответ от бота.
        if self.is_bot_message(author) and not self.is_explicit_allowed_author(author):
            await self.handle_bot_response(client, message)
            return

        route_context = {
            "author": author,
            "author_id": message.from_user.id,
            "source_platform": "telegram",
            "route_chat_id": message.chat.id,
            "route_message_id": message.id,
            "route_is_group": self._is_telegram_group_message(message),
        }

        # Проверяем разрешен ли автор
        if not self.is_author_allowed(
            author,
            message.from_user.id,
            route_chat_id=route_context["route_chat_id"],
            route_is_group=route_context["route_is_group"],
            source_platform="telegram",
        ):
            print(f"⛔ Автор {author} не в списке разрешенных. Игнорируем.")
            return

        # Игнорируем платежные документы
        if message.document:
            file_name = message.document.file_name
            file_uid = f"telegram:{message.chat.id}:{message.id}:0"
            local_path = None
            gateway_job_id = None

            # Если отправитель — активный редактор (ожидаем ответ от него),
            # не применяем force_plagiscan — чтобы не было бесконечного цикла
            author_clean_fp = author[1:] if author.startswith('@') else author
            configured_group_route = self._telegram_group_route_for_file_info(route_context)
            is_active_editor = any(
                (info.get("destination", "").lstrip("@")).lower() == author_clean_fp.lower()
                for info in self.editor_tracking.values()
            ) and not configured_group_route
            forced_route = None if is_active_editor else self.get_force_author_route(route_context)
            force_plagiscan = bool(forced_route and forced_route["destination"] == "plagiscan")
            fixed_editor_route = bool(forced_route and forced_route["destination"] == "editor")
            print(f"📄 Файл: {file_name} (UID: {file_uid[:10]}...)")
            if force_plagiscan:
                print(f"🛡️  Автор {author} в списке принудительной проверки: файл пойдет в Плагискан")
                self._log_plagiscan(f"🛡️  force_plagiscan author={author} file={file_name}")
            elif fixed_editor_route:
                print(f"👤 Файл направлен назначенному редактору: {forced_route['editor_nickname']}")
                self._log_editor(
                    f"👤 Фиксированный маршрут автора/беседы: {forced_route['editor_nickname']} | file={file_name}"
                )
            elif is_active_editor:
                print(f"ℹ️  Автор {author} — активный редактор, force_plagiscan пропущен")

            if self.processor.is_payment_document(file_name):
                print("💰 Игнорируем платежный документ")
                return

            # ТЗ 3.3: Атомарная блокировка и проверка статуса через AccountManager
            file_lock = await self.manager.get_file_lock(file_uid)
            async with file_lock:
                status = await self.manager.get_file_status(file_uid)
                if status != "NEW":
                    print(f"⚠️  Файл уже обрабатывается или обработан (статус: {status}). Пропускаем.")
                    return

                # ТЗ 3.2: Переход в IN_PROGRESS
                await self.manager.set_file_status(file_uid, "IN_PROGRESS")

            try:
                ingest_jobs = await ingest_pyrogram_message(message)
                if not ingest_jobs:
                    raise RuntimeError("Gateway не создал job для входящего документа")
                ingest_job = ingest_jobs[0]
                local_path = ingest_job.file_path
                gateway_job_id = ingest_job.job_id
                file_uid = ingest_job.dedupe_key
                print(f"✅ Gateway сохранил файл локально: job={gateway_job_id}, path={local_path}")
                self._log_gateway(f"✅ Gateway сохранил файл локально: job={gateway_job_id}, path={local_path}")
            except Exception as e:
                await self.manager.set_file_status(file_uid, "FAILED")
                print(f"❌ Ошибка gateway-ingest для файла {file_name}: {e}")
                self._log_gateway(f"❌ Ошибка gateway-ingest для файла {file_name}: {e}")
                import traceback
                traceback.print_exc()
                return

            # Сбрасываем статус ожидания при получении нового файла
            self.manager.reset_waiting_status()

            # Считаем все принятые файлы за день (включая анти и редактор)
            self._increment_files_today("telegram")

        # ВАЖНОЕ ИСПРАВЛЕНИЕ: Проверяем режим 2 для ВСЕХ файлов (анти и обычных)
        current_mode = Config.get_setting("mode")
        if current_mode == "mode2" and message.document and not force_plagiscan and not fixed_editor_route:
            should_send_to_editor = False
            reason = ""

            # Проверяем время работы
            if not self.is_within_working_hours():
                should_send_to_editor = True
                reason = "вне рабочего времени"

            # Проверяем лимит файлов (только для обычных файлов, анти всегда отправляются)
            elif not self.processor.is_anti_file(file_name) and not self.check_daily_limit():
                should_send_to_editor = True
                reason = "превышен заданный лимит проверок"

            # Если нужно отправить редактору
            if should_send_to_editor:
                print(f"⏰ Файл будет отправлен круглосуточному редактору: {reason}")

                # Создаем информацию о файле для отслеживания
                file_info = {
                    "author": author,
                    "author_id": message.from_user.id,
                    "file_name": file_name,
                    "message_id": message.id,
                    "chat_id": message.chat.id,
                    "route_chat_id": message.chat.id,
                    "route_message_id": message.id,
                    "route_is_group": self._is_telegram_group_message(message),
                    "is_anti": self.processor.is_anti_file(file_name) or force_plagiscan,
                    "force_plagiscan": force_plagiscan,
                    "forced_route": forced_route,
                    "fixed_author_editor_route": fixed_editor_route,
                    "forced_editor_nickname": forced_route.get("editor_nickname") if fixed_editor_route else None,
                    "received_at": datetime.now(),
                    "status": f"отправлен редактору ({reason})",
                    "message": message,
                    "original_file_name": file_name,
                    "sent_to_editor": True,
                    "reason": reason,
                    "local_path": local_path,
                    "gateway_job_id": gateway_job_id,
                    "file_uid": file_uid,
                    "source_platform": "telegram",
                }

                # Отправляем файл редактору
                await self.send_to_24_7_editor(message, file_info)

                # Отслеживаем файл в статистике
                if author not in self.file_tracking:
                    self.file_tracking[author] = {
                        "sent": [],
                        "received": [],
                        "pending": [],
                        "sent_to_editor": []  # Новый список для файлов отправленных редактору
                    }

                self.file_tracking[author]["sent_to_editor"].append(file_info['file_name'])
                print(f"✅ Файл отправлен круглосуточному редактору и отслеживается")
                return  # Не добавляем в обычную очередь

        # Режим 1 или режим 2 в рабочее время с доступными лимитами
        if message.document:
            file_info = {
                "author": author,
                "author_id": message.from_user.id,
                "file_name": file_name,
                "message_id": message.id,
                "chat_id": message.chat.id,
                "route_chat_id": message.chat.id,
                "route_message_id": message.id,
                "route_is_group": self._is_telegram_group_message(message),
                "is_anti": self.processor.is_anti_file(file_name) or force_plagiscan,
                "force_plagiscan": force_plagiscan,
                "forced_route": forced_route,
                "fixed_author_editor_route": fixed_editor_route,
                "forced_editor_nickname": forced_route.get("editor_nickname") if fixed_editor_route else None,
                "received_at": datetime.now(),
                "status": "в очереди",
                "message": message,
                "original_file_name": file_name,
                "sent_to_editor": False,
                "file_uid": file_uid,
                "local_path": local_path,
                "gateway_job_id": gateway_job_id,
                "source_platform": "telegram",
            }

            # Инициализируем отслеживание для автора
            if author not in self.file_tracking:
                self.file_tracking[author] = {
                    "sent": [],
                    "received": [],
                    "pending": [],
                    "sent_to_editor": []
                }

            self.file_tracking[author]["pending"].append(file_info["file_name"])
            self.manager.file_queue.append(file_info)

            print(f"✅ Файл добавлен в очередь: {file_info['file_name']}")
            print(f"   Тип: {'АНТИ' if file_info['is_anti'] else 'обычный'}")
            print(f"   В очереди: {len(self.manager.file_queue)} файлов")

            # Обрабатываем очередь
            await self.process_queue()

    async def handle_bot_response(self, client, message):
        """Обработка ответов от ботов"""
        try:
            author = message.from_user.username or str(message.from_user.id) if message.from_user else "unknown"
            print(f"\n🤖 ОТВЕТ ОТ БОТА: {author}")

            # Определяем тип бота
            ai_bot = Config.get_setting("ai_bot").replace('@', '')
            anti_bot = Config.get_setting("anti_bot").replace('@', '')

            if author == ai_bot:
                await self.handle_ai_bot_response(client, message)
            elif author == anti_bot:
                await self.handle_anti_bot_response(client, message)
            else:
                # Проверяем известных ботов
                if author in ['Art_progs', 'AAA_Report_AIBot']:
                    await self.handle_ai_bot_response(client, message)
                elif author in ['plagaiscan_bot']:
                    await self.handle_anti_bot_response(client, message)
                else:
                    print(f"⚠️  Неизвестный бот: {author}")

        except Exception as e:
            print(f"❌ Ошибка обработки ответа бота: {e}")
            import traceback
            traceback.print_exc()

    async def send_to_24_7_editor(self, message, file_info=None):
        """Отправка файла круглосуточному редактору (для режима 2) - БЕЗ ПОДПИСИ"""
        try:
            editor_24_7 = Config.get_setting("editor_24_7")
            if not editor_24_7:
                print("❌ Не задан круглосуточный редактор")

                # Отправляем обратно автору с пояснением
                if file_info:
                    await self.send_back_to_author(message, file_info,
                                                   reason="не задан круглосуточный редактор в настройках")
                return

            # Пытаемся получить НИК-2, если нет - используем НИК-1
            client = self.manager.get_client("НИК-2")
            account_name = "НИК-2"

            if not client:
                client = self.manager.get_client("НИК-1")
                account_name = "НИК-1"
                if not client:
                    print("❌ Не найден ни НИК-1, ни НИК-2")
                    if file_info:
                        await self.send_back_to_author(message, file_info,
                                                       reason="отсутствуют аккаунты для отправки")
                    return

            file_name = file_info.get("original_file_name") if file_info else None
            if not file_name and message and getattr(message, "document", None):
                file_name = message.document.file_name
            if not file_name:
                file_name = "document.bin"

            working_info = dict(file_info or {})
            working_info.setdefault("file_name", file_name)
            working_info.setdefault("original_file_name", file_name)
            temp_path = await self._ensure_work_file(working_info, message=message, prefix="editor247")

            print(f"📤 Отправляем файл круглосуточному редактору через {account_name}: {editor_24_7}")
            print(f"   Файл: {file_name}")
            print(f"   Автор: {file_info['author'] if file_info else 'неизвестен'}")
            self._log_editor(f"📤 Отправляем файл круглосуточному редактору через {account_name}: {editor_24_7} | file={file_name}")

            if file_info and file_info.get('reason'):
                print(f"   Причина: {file_info['reason']}")

            # ОТПРАВЛЯЕМ БЕЗ CAPTION
            sent_msg = await client.send_document(
                chat_id=editor_24_7,
                document=temp_path,
                file_name=file_name,
            )
            print(f"✅ Файл отправлен редактору {editor_24_7} через {account_name}")
            self._log_editor(f"✅ Файл отправлен редактору {editor_24_7} через {account_name} | file={file_name} msg_id={sent_msg.id}")

            # ДОБАВЛЯЕМ ОТСЛЕЖИВАНИЕ
            original_name = file_info.get("original_file_name", file_name) if file_info else file_name
            original_name_without_ext = os.path.splitext(original_name)[0]

            tracking_key = f"24_7_{sent_msg.id}_{datetime.now().strftime('%H%M%S')}"

            self.editor_tracking[tracking_key] = {
                "author": file_info["author"] if file_info else message.from_user.username or str(message.from_user.id),
                "author_id": file_info.get("author_id") if file_info else None,
                "original_name": original_name,
                "original_name_without_ext": original_name_without_ext,
                "expected_pdf_name": f"{original_name_without_ext}.pdf",
                "sent_at": datetime.now(),
                "chat_id": sent_msg.chat.id,
                "message_id": sent_msg.id,
                "destination": editor_24_7,
                "is_from_editor": True,
                "sent_from_account": account_name,
                "reply_to_message_id": sent_msg.id,
                "is_anti_file": file_info["is_anti"] if file_info else False,
                "reason": file_info.get("reason",
                                        "вне рабочего времени/лимита") if file_info else "вне рабочего времени/лимита",
                "source_platform": file_info.get("source_platform", "telegram") if file_info else "telegram",
                "route_sender_id": file_info.get("route_sender_id") if file_info else None,
                "route_chat_id": file_info.get("route_chat_id") if file_info else None,
                "route_message_id": file_info.get("route_message_id") if file_info else None,
                "route_is_group": file_info.get("route_is_group", False) if file_info else False,
                "gateway_job_id": file_info.get("gateway_job_id") if file_info else None,
                "local_path": file_info.get("local_path") if file_info else None,
                "message": file_info.get("message") if file_info else message,
                "download_url": file_info.get("download_url") if file_info else None,
                "force_plagiscan": file_info.get("force_plagiscan", False) if file_info else False,
                "via_normal_destination": file_info.get("via_normal_destination", False) if file_info else False,
            }
            if file_info:
                await self._mark_gateway_job_waiting_editor(file_info, "waiting_editor247_response")

            print(f"📝 Ожидаю PDF файл: {original_name_without_ext}.pdf от круглосуточного редактора")
            self._log_editor(f"📝 Ожидаю PDF файл: {original_name_without_ext}.pdf от круглосуточного редактора")

            # Удаляем временный файл
            if os.path.exists(temp_path):
                os.remove(temp_path)
                print(f"🗑️  Удален временный файл: {temp_path}")

        except Exception as e:
            print(f"❌ Ошибка отправки редактору: {e}")
            self._log_editor(f"❌ Ошибка отправки редактору: {e}")
            import traceback
            traceback.print_exc()

            # При ошибке отправки редактору, отправляем обратно автору
            if file_info:
                await self.send_back_to_author(message, file_info,
                                               reason=f"ошибка отправки редактору: {str(e)[:100]}")

    async def send_back_to_author(self, message, file_info, reason=""):
        """Отправка файла обратно автору при ошибках"""
        try:
            client_nik1 = self.manager.get_client("НИК-1")
            if not client_nik1:
                print("❌ Аккаунт НИК-1 не найден для отправки автору")
                return

            file_name = file_info.get("original_file_name") or file_info.get("file_name")
            if not file_name and message and getattr(message, "document", None):
                file_name = message.document.file_name
            if not file_name:
                file_name = "document.bin"
            temp_path = await self._ensure_work_file(
                {
                    **file_info,
                    "file_name": file_name,
                    "original_file_name": file_name,
                },
                message=message,
                prefix=f"return_{datetime.now().strftime('%H%M%S')}",
            )

            # Создаем сообщение с пояснением
            caption = f"⚠️ Файл не был отправлен на проверку.\nПричина: {reason}"

            print(f"📤 Отправляю файл обратно автору {file_info['author']}: {reason}")

            # Отправляем файл
            await self._deliver_document_to_origin(
                file_info,
                temp_path,
                file_name=file_name,
                caption=caption,
                telegram_client=client_nik1,
            )

            print(f"✅ Файл отправлен обратно автору: {file_info['author']}")

            # Обновляем статус файла
            if file_info["author"] in self.file_tracking:
                if "returned_to_author" not in self.file_tracking[file_info["author"]]:
                    self.file_tracking[file_info["author"]]["returned_to_author"] = []

                self.file_tracking[file_info["author"]]["returned_to_author"].append(
                    f"{file_info['file_name']}: {reason}"
                )

            # Удаляем временный файл
            if os.path.exists(temp_path):
                os.remove(temp_path)

        except Exception as e:
            print(f"❌ Ошибка отправки файла автору: {e}")

    async def process_queue(self):
        """Обработка очереди файлов"""
        if not self.manager.file_queue:
            total_processing = sum(len(files) for files in self.manager.processing_files.values())
            if total_processing == 0:
                self.manager.check_all_files_processed()
            return

        max_concurrent = Config.get_setting("max_concurrent") or 5
        # НИК-1 и НИК-2 не занимают слоты обычных файлов (у них своя роль)
        normal_in_processing = {k: v for k, v in self.manager.processing_files.items()
                                 if k not in ("НИК-1", "НИК-2")}
        normal_slots_free = len(normal_in_processing) < max_concurrent
        chosen_idx = None
        chosen_is_anti = False

        for i, f in enumerate(self.manager.file_queue):
            queued_route = f.get("forced_route") or self.get_force_author_route(f)
            if queued_route and queued_route.get("destination") == "editor":
                chosen_idx = i
                break
            if f.get("is_anti") and not chosen_is_anti:
                chosen_idx = i
                chosen_is_anti = True
                break
            if not f.get("is_anti") and normal_slots_free and chosen_idx is None:
                chosen_idx = i

        if chosen_idx is None:
            return

        file_info = self.manager.file_queue.pop(chosen_idx)

        forced_route = file_info.get("forced_route") or self.get_force_author_route(file_info)
        if forced_route:
            if forced_route["destination"] == "editor":
                file_info["fixed_author_editor_route"] = True
                file_info["forced_editor_nickname"] = forced_route["editor_nickname"]
                file_info["force_plagiscan"] = False
                file_info["sent_to_editor"] = True
                file_info["reason"] = "назначенный маршрут принудительного автора или беседы"
                self._log_editor(
                    f"📌 Назначенный редактор принудительного автора или беседы: "
                    f"{forced_route['editor_nickname']} | file={file_info.get('original_file_name', file_info.get('file_name'))}"
                )
                await self.send_to_nik2(
                    file_info,
                    reason="назначенный маршрут принудительного автора или беседы",
                )
                return
            file_info["force_plagiscan"] = True
            file_info["is_anti"] = True

        current_mode = Config.get_setting("mode")
        normal_destination = Config.get_setting("normal_destination", "бот")
        if current_mode == "mode2":
            should_send_to_editor = False
            reason = ""

            if not file_info.get("force_plagiscan") and not self.is_within_working_hours():
                should_send_to_editor = True
                reason = "вне рабочего времени"
            elif not file_info["is_anti"] and not file_info.get("force_plagiscan") and self.aaa_globally_unavailable:
                should_send_to_editor = True
                reason = "в AAA боте нет доступных проверок"
            elif not file_info["is_anti"] and not file_info.get("force_plagiscan") and not self.check_daily_limit():
                should_send_to_editor = True
                reason = "превышен заданный лимит проверок"

            if should_send_to_editor:
                print(f"⏰ Файл из очереди будет отправлен редактору: {reason}")
                file_info["sent_to_editor"] = True
                file_info["reason"] = reason
                await self.send_to_24_7_editor(file_info["message"], file_info)
                return

        if (
            not file_info["is_anti"]
            and (file_info.get("force_plagiscan") or normal_destination == "плагискан")
        ):
            forced_author = bool(file_info.get("force_plagiscan"))
            route_reason = "принудительный автор" if forced_author else "настройка normal_destination"
            print(f"🛡️  Обычный файл направлен в Plagiscan: {route_reason}")
            self._log_plagiscan(
                f"🛡️  Обычный файл направлен в Plagiscan: {route_reason} "
                f"| file={file_info.get('original_file_name', file_info.get('file_name'))}"
            )
            file_info["is_anti"] = True
            file_info["force_plagiscan"] = forced_author
            file_info["via_normal_destination"] = not forced_author and normal_destination == "плагискан"
            await self.process_anti_file(file_info)
            return

        if not file_info["is_anti"] and normal_destination == "редактор":
            print("👤 Обычный файл направлен редактору по настройке normal_destination")
            self._log_editor(f"👤 Обычный файл направлен редактору по настройке normal_destination | file={file_info.get('original_file_name', file_info.get('file_name'))}")
            file_info["sent_to_editor"] = True
            file_info["reason"] = "обычные файлы направляются редактору"
            await self.send_to_nik2(file_info, reason="обычные файлы направляются редактору")
            self._increment_mode2_limit_counter()
            return

        try:
            if file_info["is_anti"]:
                await self.process_anti_file(file_info)
            else:
                await self.process_normal_file(file_info)
        except Exception as e:
            print(f"❌ Ошибка обработки файла {file_info['file_name']}: {e}")
            file_info["status"] = "ошибка"
            if file_info["is_anti"]:
                # Анти-файл: игнорируем ошибку, не перенаправляем никуда
                print("ℹ️  Анти-файл с ошибкой пропущен")
                self.manager.mark_account_free("НИК-2", file_info.get("file_name", ""))
            else:
                await self.send_to_nik2(file_info, reason="ошибка обработки")
            asyncio.create_task(self.process_queue())

    async def process_anti_file(self, file_info):
        """Обработка анти-файла"""
        if file_info.get("via_normal_destination"):
            print(f"\n📤 ОТПРАВКА В ПЛАГИСКАН (обычный файл по настройке): {file_info['file_name']}")
            self._log_plagiscan(f"📤 ОТПРАВКА В ПЛАГИСКАН (обычный файл по настройке): {file_info['file_name']}")
        else:
            print(f"\n🛡️  ОБРАБОТКА АНТИ-ФАЙЛА: {file_info['file_name']}")
            self._log_plagiscan(f"🛡️  ОБРАБОТКА АНТИ-ФАЙЛА: {file_info['file_name']}")
        file_info["status"] = "обработка анти"

        temp_files_to_cleanup = []

        try:
            client_nik2 = self.manager.get_client("НИК-2")
            if not client_nik2:
                print("❌ Аккаунт НИК-2 не найден")
                file_info["status"] = "ошибка"
                return

            temp_path = await self._ensure_work_file(file_info, prefix="anti")
            print(f"🔍 [DEBUG] Скачиваю файл в: {temp_path}")
            temp_files_to_cleanup.append(temp_path)
            file_exists = os.path.exists(temp_path)
            file_size = os.path.getsize(temp_path) if file_exists else -1
            print(f"📥 Файл скачан: {temp_path}")
            print(f"🔍 [DEBUG] Файл существует: {file_exists}, размер: {file_size} байт")
            if file_size == 0:
                print(f"❌ [DEBUG] Файл пустой (0 байт)! Отправка отменена.")
                file_info["status"] = "ошибка"
                return

            destination = None
            anti_destination = self._anti_destination_for(file_info)
            if anti_destination == "бот":
                destination = Config.get_setting("anti_bot")
                print(f"🤖 Отправляем в бота: {destination}")
                self._log_plagiscan(f"🤖 Отправляем в бота: {destination} | file={file_info.get('original_file_name', file_info['file_name'])}")
            else:
                destination = self._editor_for_file(file_info, prefer_anti=True)
                if self._is_editor_unavailable(destination) and Config.get_setting("editor_24_7"):
                    old_destination = destination
                    destination = Config.get_setting("editor_24_7")
                    print(f"📤 Редактор {old_destination} недоступен, анти-файл отправляем круглосуточному редактору: {destination}")
                    self._log_editor(
                        f"📤 Редактор {old_destination} недоступен, анти-файл отправляем круглосуточному редактору: "
                        f"{destination} | file={file_info.get('original_file_name', file_info['file_name'])}"
                    )
                print(f"👤 Отправляем редактору: {destination}")
                self._log_editor(f"👤 Отправляем редактору: {destination} | anti file={file_info.get('original_file_name', file_info['file_name'])}")

            if not destination:
                print("❌ Не задан получатель для анти-файлов")
                file_info["status"] = "ошибка"
                return

            file_key = f"anti_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"

            if anti_destination != "бот":
                print(f"📤 Отправляем анти-файл редактору через НИК-2: {destination}")

                original_name = file_info.get("original_file_name", file_info["file_name"])
                sent_msg = await client_nik2.send_document(
                    chat_id=destination,
                    document=temp_path,
                    file_name=original_name
                )

                original_name_without_ext = os.path.splitext(original_name)[0]

                tracking_key = f"anti_editor_{sent_msg.id}"

                self.editor_tracking[tracking_key] = {
                    "author": file_info["author"],
                    "author_id": file_info.get("author_id"),
                    "original_name": original_name,
                    "original_name_without_ext": original_name_without_ext,
                    "expected_pdf_name": f"{original_name_without_ext}.pdf",
                    "sent_at": datetime.now(),
                    "chat_id": sent_msg.chat.id,
                    "message_id": sent_msg.id,
                    "destination": destination,
                    "is_anti_file": True,
                    "is_from_editor": True,
                    "sent_from_account": "НИК-2",
                    "reply_to_message_id": sent_msg.id,
                    "source_platform": file_info.get("source_platform", "telegram"),
                    "route_sender_id": file_info.get("route_sender_id"),
                    "route_chat_id": file_info.get("route_chat_id"),
                    "route_message_id": file_info.get("route_message_id"),
                    "route_is_group": file_info.get("route_is_group", False),
                    "gateway_job_id": file_info.get("gateway_job_id"),
                    "local_path": file_info.get("local_path"),
                    "message": file_info.get("message"),
                    "download_url": file_info.get("download_url"),
                    "force_plagiscan": file_info.get("force_plagiscan", False),
                    "via_normal_destination": file_info.get("via_normal_destination", False),
                }
                await self._mark_gateway_job_waiting_editor(file_info, "waiting_anti_editor_response")

                print(f"✅ Анти-файл отправлен редактору {destination} через НИК-2. ID сообщения: {sent_msg.id}")
                print(f"📝 Ожидаю PDF файл: {original_name_without_ext}.pdf от редактора в НИК-2")
                self._log_editor(f"✅ Анти-файл отправлен редактору {destination} через НИК-2. ID сообщения: {sent_msg.id}")
                self._log_editor(f"📝 Ожидаю PDF файл: {original_name_without_ext}.pdf от редактора в НИК-2")

                self.manager.mark_account_busy("НИК-2", file_info["file_name"])

            else:
                self.current_processing_files[file_key] = {
                    "author": file_info["author"],
                    "original_file_name": file_info["original_file_name"],
                    "temp_path": temp_path,
                    "account": "НИК-2",
                    "bot_type": "anti",
                    "chat_id": file_info["chat_id"],
                    "message_id": file_info["message_id"],
                    "file_uid": file_info.get("file_uid"),
                    "local_path": file_info.get("local_path"),
                    "gateway_job_id": file_info.get("gateway_job_id"),
                    "source_platform": file_info.get("source_platform", "telegram"),
                    "route_sender_id": file_info.get("route_sender_id"),
                    "route_chat_id": file_info.get("route_chat_id"),
                    "route_sender_name": file_info.get("route_sender_name"),
                    "route_message_id": file_info.get("route_message_id"),
                    "route_is_group": file_info.get("route_is_group", False),
                    "force_plagiscan": file_info.get("force_plagiscan", False),
                    "via_normal_destination": file_info.get("via_normal_destination", False),
                }

                sent_msg = await client_nik2.send_document(
                    chat_id=destination,
                    document=temp_path,
                    file_name=file_info.get("original_file_name", file_info["file_name"])
                )
                print(f"✅ Анти-файл отправлен в {destination}")
                self._log_plagiscan(f"✅ Анти-файл отправлен в {destination} | msg_id={sent_msg.id} file={file_info.get('original_file_name', file_info['file_name'])}")

                self.message_to_file_map[f"{sent_msg.chat.id}_{sent_msg.id}"] = file_key

                self.manager.mark_account_busy("НИК-2", file_info["file_name"])

            file_info["status"] = "отправлен на антиплагиат"
            file_info["sent_to"] = destination
            file_info["sent_message_id"] = sent_msg.id
            file_info["file_key"] = file_key
            if file_info.get("via_normal_destination"):
                self._increment_mode2_limit_counter()

            if file_info["author"] in self.file_tracking:
                self.file_tracking[file_info["author"]]["sent"].append(
                    f"{file_info['file_name']} → {destination}"
                )
                if file_info["file_name"] in self.file_tracking[file_info["author"]]["pending"]:
                    self.file_tracking[file_info["author"]]["pending"].remove(file_info["file_name"])

            self.processor.cleanup_temp_files(temp_files_to_cleanup)

            # Плагискан принимает несколько файлов одновременно — сразу грузим следующий.
            asyncio.create_task(self.process_queue())

        except Exception as e:
            print(f"❌ Ошибка обработки анти-файла: {e}")
            self._log_plagiscan(f"❌ Ошибка обработки анти-файла: {e}")
            import traceback
            traceback.print_exc()
            file_info["status"] = "ошибка"
            self.processor.cleanup_temp_files(temp_files_to_cleanup)

    async def process_normal_file(self, file_info):
        print(f"\n📄 ОБРАБОТКА ОБЫЧНОГО ФАЙЛА: {file_info['file_name']}")
        self._log_aaa(f"📄 ОБРАБОТКА ОБЫЧНОГО ФАЙЛА: {file_info['file_name']}")
        file_info["status"] = "обработка обычный"

        temp_files_to_cleanup = []
        file_key = None

        try:
            if self.aaa_globally_unavailable:
                print("⛔ AAA глобально недоступен — обычный файл сразу отправляем редактору")
                await self.send_to_nik2(file_info, reason="в AAA боте нет доступных проверок")
                return

            account = self.manager.get_available_account()
            if not account:
                alive_accounts = [acc for acc in self._get_normal_accounts() if acc not in self.manager.blacklisted_accounts]
                if not alive_accounts:
                    print("⛔ Нет доступных аккаунтов AAA: все обычные аккаунты заблокированы. Перенаправляем файл редактору.")
                    self._log_aaa("⛔ Нет доступных аккаунтов AAA: все обычные аккаунты заблокированы. Перенаправляем файл редактору.")
                    await self.send_to_nik2(file_info, reason="все аккаунты AAA заблокированы")
                    return
                print("⏳ Нет свободных аккаунтов для обычных файлов, возвращаем в очередь")
                self.manager.file_queue.insert(0, file_info)
                return

            # Проверяем, что это не НИК-1 (который только для приема файлов)
            if account == "НИК-1":
                print("❌ ОШИБКА: НИК-1 предназначен только для приема файлов, а не для отправки!")
                print("   Проверьте настройки аккаунтов Telegram")
                self.manager.file_queue.insert(0, file_info)
                return

            # Проверяем, не заблокирован ли аккаунт
            if account in self.blacklisted_accounts:
                print(f"⛔ Аккаунт {account} заблокирован (нет проверок)")
                
                # Проверяем, остались ли еще живые аккаунты
                normal_accounts = self._get_normal_accounts()
                alive_accounts = [acc for acc in normal_accounts if acc not in self.manager.blacklisted_accounts]
                
                if not alive_accounts:
                    print("❌ ВСЕ аккаунты для обычных файлов исчерпаны! Перенаправляем редактору.")
                    file_info["status"] = "все аккаунты исчерпаны"
                    await self.send_to_nik2(file_info, reason="все аккаунты без проверок")
                    return

                print("⏳ Возвращаем в очередь для другого аккаунта")
                self.manager.file_queue.insert(0, file_info)
                return

            # Помечаем аккаунт как занятый
            self.manager.mark_account_busy(account, file_info["file_name"])

            # Получаем клиент для отправки файла
            client = self.manager.clients.get(account)
            if not client:
                # Если клиент не найден в запущенных, пробуем получить через get_client
                client = self.manager.get_client(account)
                if not client:
                    print(f"❌ Клиент {account} не найден в запущенных аккаунтах")
                    self.manager.mark_account_free(account, file_info["file_name"])
                    self.manager.file_queue.insert(0, file_info)
                    return

            print(f"✅ Использую аккаунт {account} ({client.name}) для отправки файла в AI бота")
            self._log_aaa(f"✅ Использую аккаунт {account} ({client.name}) для отправки файла в AI бота")

            # Готовим рабочую копию файла из gateway/local_path, либо старым скачиванием как fallback
            temp_path = await self._ensure_work_file(file_info, prefix="normal")
            # Не добавляем temp_path в cleanup — файл нужен для handle_aaa_error_message
            # Он будет удалён там, либо после успешной доставки в handle_ai_bot_response
            print(f"📥 Файл скачан: {temp_path}")

            # Проверяем, что файл действительно скачан
            if not os.path.exists(temp_path) or os.path.getsize(temp_path) == 0:
                print(f"❌ Файл не скачан или пустой: {temp_path} — освобождаем аккаунт")
                self.manager.mark_account_free(account, file_info["file_name"])
                return

            # Сохраняем информацию о файле для последующей обработки
            file_key = f"normal_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
            self.current_processing_files[file_key] = {
                "author": file_info["author"],
                "author_id": file_info.get("author_id"),
                "original_file_name": file_info["original_file_name"],  # Сохраняем оригинальное имя
                "temp_path": temp_path,
                "account": account,
                "bot_type": "ai",
                "chat_id": file_info["chat_id"],
                "message_id": file_info["message_id"],
                "message": file_info["message"],  # Сохраняем объект сообщения для переподачи в очередь
                "file_uid": file_info.get("file_uid"),
                "local_path": file_info.get("local_path"),
                "gateway_job_id": file_info.get("gateway_job_id"),
                "source_platform": file_info.get("source_platform", "telegram"),
                "route_sender_id": file_info.get("route_sender_id"),
                "route_sender_name": file_info.get("route_sender_name"),
                "route_chat_id": file_info.get("route_chat_id"),
                "route_message_id": file_info.get("route_message_id"),
                "route_is_group": file_info.get("route_is_group", False),
            }

            # Отправляем /check перед отправкой файла
            ai_bot = Config.get_setting("ai_bot")
            print(f"🤖 Отправляем /check в AI бота: {ai_bot} через {account}")
            self._log_aaa(f"🤖 Отправляем /check в AI бота: {ai_bot} через {account}")
            prompt_ready = False
            max_prompt_attempts = Config.get_setting("ai_upload_prompt_attempts", 2)

            for prompt_attempt in range(max_prompt_attempts):
                upload_ready_event = self._get_ai_upload_ready_event(account)
                upload_ready_event.clear()

                try:
                    await client.send_message(ai_bot, "/check")
                    print(f"✅ Команда /check отправлена (попытка {prompt_attempt + 1}/{max_prompt_attempts})")
                    self._log_aaa(f"✅ Команда /check отправлена (попытка {prompt_attempt + 1}/{max_prompt_attempts})")
                    prompt_ready = await self._wait_for_ai_upload_prompt(account, file_key)
                    if prompt_ready:
                        print("✅ AAA бот прислал приглашение на загрузку файла")
                        self._log_aaa("✅ AAA бот прислал приглашение на загрузку файла")
                        break
                    if file_key not in self.current_processing_files:
                        print(f"ℹ️ Файл {file_key} уже обработан до отправки в AAA бот")
                        return
                    if prompt_attempt < max_prompt_attempts - 1:
                        print("⚠️  Не дождались приглашения AAA, повторяем /check")
                except Exception as e:
                    print(f"⚠️  Не удалось отправить /check: {e}")
                    if prompt_attempt < max_prompt_attempts - 1:
                        print("🔄 Повторяем отправку /check")

            if not prompt_ready:
                if file_key not in self.current_processing_files:
                    print(f"ℹ️ Файл {file_key} уже обработан до отправки в AAA бот")
                    return
                print("⛔ Не дождались приглашения на загрузку от AAA — файл НЕ отправляем без сообщения")
                await self._requeue_after_missing_ai_prompt(account, file_key, file_info, temp_path)
                return

            # Проверяем, не был ли файл уже обработан параллельным обработчиком
            # (handle_no_more_checks мог сработать во время asyncio.sleep выше)
            if file_key not in self.current_processing_files:
                print(f"ℹ️ Файл {file_key} уже обработан (нет проверок), отмена отправки в AI бот")
                return

            # Отправляем файл через выбранный клиент
            sent_msg = await client.send_document(
                chat_id=ai_bot,
                document=temp_path,
                file_name=self._preferred_document_name(file_info),
            )
            print(f"✅ Файл отправлен в AI бота через аккаунт {account}")
            self._log_aaa(f"✅ Файл отправлен в AI бота через аккаунт {account} | msg_id={sent_msg.id} file={file_info['original_file_name']}")

            # Сохраняем ID отправленного сообщения для сопоставления
            self.message_to_file_map[f"{sent_msg.chat.id}_{sent_msg.id}"] = file_key

            file_info["status"] = "отправлен в AI бот"
            file_info["processing_account"] = account
            file_info["sent_message_id"] = sent_msg.id
            file_info["file_key"] = file_key

            # Увеличиваем счетчик текущего рабочего интервала MODE2
            self._increment_mode2_limit_counter()

            # Обновляем отслеживание
            if file_info["author"] in self.file_tracking:
                self.file_tracking[file_info["author"]]["sent"].append(
                    f"{file_info['file_name']} → {ai_bot} через {account}"
                )
                if file_info["file_name"] in self.file_tracking[file_info["author"]]["pending"]:
                    self.file_tracking[file_info["author"]]["pending"].remove(file_info["file_name"])

            self.processor.cleanup_temp_files(temp_files_to_cleanup)

        except ValueError as e:
            print(f"❌ Ошибка файла (decode/path): {e}")
            import traceback
            traceback.print_exc()
            file_info["status"] = "ошибка"
            self.processor.cleanup_temp_files(temp_files_to_cleanup)

            if file_key and file_key in self.current_processing_files:
                info = self.current_processing_files[file_key]
                if info.get("message") or info.get("local_path"):
                    requeue_info = {
                        "author": info["author"], "author_id": info.get("author_id"),
                        "file_name": info["original_file_name"],
                        "original_file_name": info["original_file_name"],
                        "message_id": info["message_id"], "chat_id": info["chat_id"],
                        "is_anti": False, "received_at": datetime.now(),
                        "status": "повторная очередь", "message": info["message"],
                        "sent_to_editor": False, "file_uid": info.get("file_uid"),
                        "local_path": info.get("local_path"),
                        "gateway_job_id": info.get("gateway_job_id"),
                        "source_platform": info.get("source_platform", "telegram"),
                        "route_sender_id": info.get("route_sender_id"),
                        "route_sender_name": info.get("route_sender_name"),
                        "route_chat_id": info.get("route_chat_id"),
                        "route_message_id": info.get("route_message_id"),
                        "route_is_group": info.get("route_is_group", False),
                    }
                    self.manager.file_queue.insert(0, requeue_info)
                    print(f"🔄 Файл возвращён в очередь для повторной попытки")
                del self.current_processing_files[file_key]
            if "processing_account" in file_info:
                self.manager.mark_account_free(file_info["processing_account"], file_info["file_name"])
            asyncio.create_task(self.process_queue())
            # НЕ вызываем send_to_nik2

        except Exception as e:
            print(f"❌ Ошибка обработки обычного файла: {e}")
            self._log_aaa(f"❌ Ошибка обработки обычного файла: {e}")
            import traceback
            traceback.print_exc()
            file_info["status"] = "ошибка"
            self.processor.cleanup_temp_files(temp_files_to_cleanup)

            # Если handle_no_more_checks уже обработал файл (удалил из current_processing_files),
            # не пытаемся отправить повторно через send_to_nik2
            if file_key and file_key not in self.current_processing_files:
                print(f"ℹ️ Файл {file_key} уже обработан параллельным обработчиком, пропускаем")
                return

            if "processing_account" in file_info:
                self.manager.mark_account_free(file_info["processing_account"], file_info["file_name"])

            # Если файл уже успешно обработан — не отправляем редактору
            file_uid = file_info.get("file_uid")
            if file_uid and self.processed_files.get(file_uid) == "DONE":
                print(f"ℹ️ Файл {file_uid[:20]} уже обработан (DONE), не перенаправляем редактору")
                return

            # При ошибке отправляем в НИК-2
            await self.send_to_nik2(file_info, reason="ошибка отправки в AI бот")

    async def send_to_nik2(self, file_info, reason=""):
        """Отправка файла редактору через НИК-2 (при ошибках/нет проверок) и отслеживание ответа"""
        temp_files_to_cleanup = []

        try:
            print(f"⚠️  Перенаправляем файл через НИК-2: {reason}")
            self._log_editor(f"⚠️  Перенаправляем файл через НИК-2: {reason} | file={file_info.get('original_file_name', file_info.get('file_name'))}")

            # Получаем клиент НИК-2 для отправки редактору
            client_nik2 = self.manager.get_client("НИК-2")
            if not client_nik2:
                print("❌ Аккаунт НИК-2 не найден")
                return

            # Готовим рабочую копию файла из локального хранилища gateway или из исходного сообщения
            temp_path = await self._ensure_work_file(file_info, prefix="nik2")
            temp_files_to_cleanup.append(temp_path)

            # Определяем получателя
            current_mode = Config.get_setting("mode")
            destination = None
            fixed_author_editor_route = bool(file_info.get("fixed_author_editor_route"))

            if current_mode == "mode2" and not fixed_author_editor_route:
                mode2_247_reasons = (
                    "вне рабочего времени",
                    "превышен заданный лимит проверок",
                    "в aaa боте нет доступных проверок",
                    "все аккаунты без проверок",
                    "все аккаунты aaa заблокированы",
                )
                reason_lower = (reason or "").lower()
                use_24_7_editor = any(item in reason_lower for item in mode2_247_reasons)
                work_editor = Config.get_setting("editor_nickname")
                if not use_24_7_editor and self._is_editor_unavailable(work_editor):
                    use_24_7_editor = True
                    reason = "рабочий редактор сообщил, что проверки закончились"
                destination = (
                    Config.get_setting("editor_24_7")
                    if use_24_7_editor
                    else work_editor
                )
                if destination:
                    if use_24_7_editor:
                        print(f"📤 Отправляем файл круглосуточному редактору через НИК-2: {destination}")
                        self._log_editor(f"📤 Отправляем файл круглосуточному редактору через НИК-2: {destination} | file={file_info.get('original_file_name', file_info['file_name'])}")
                    else:
                        print(f"📤 Отправляем файл редактору рабочего времени через НИК-2: {destination}")
                        self._log_editor(f"📤 Отправляем файл редактору рабочего времени через НИК-2: {destination} | file={file_info.get('original_file_name', file_info['file_name'])}")
                    sent_msg = await client_nik2.send_document(
                        chat_id=destination,
                        document=temp_path,
                        file_name=file_info.get("original_file_name", file_info["file_name"])
                    )

                    original_name = file_info.get("original_file_name", file_info["file_name"])
                    original_name_without_ext = os.path.splitext(original_name)[0]
                    tracking_key = f"no_checks_{sent_msg.id}"

                    self.editor_tracking[tracking_key] = {
                        "author": file_info["author"],
                        "author_id": file_info.get("author_id"),
                        "original_name": original_name,
                        "original_name_without_ext": original_name_without_ext,
                        "expected_pdf_name": f"{original_name_without_ext}.pdf",
                        "sent_at": datetime.now(),
                        "chat_id": sent_msg.chat.id,
                        "message_id": sent_msg.id,
                        "destination": destination,
                        "is_from_editor": True,
                        "sent_from_account": "НИК-2",
                        "reply_to_message_id": sent_msg.id,
                        "file_uid": file_info.get("file_uid"),
                        "source_platform": file_info.get("source_platform", "telegram"),
                        "route_sender_id": file_info.get("route_sender_id"),
                        "route_chat_id": file_info.get("route_chat_id"),
                        "route_message_id": file_info.get("route_message_id"),
                        "route_is_group": file_info.get("route_is_group", False),
                        "gateway_job_id": file_info.get("gateway_job_id"),
                        "local_path": file_info.get("local_path"),
                        "message": file_info.get("message"),
                        "download_url": file_info.get("download_url"),
                        "force_plagiscan": file_info.get("force_plagiscan", False),
                        "via_normal_destination": file_info.get("via_normal_destination", False),
                    }
                    await self._mark_gateway_job_waiting_editor(
                        file_info,
                        "waiting_editor247_response" if use_24_7_editor else "waiting_editor_response_mode2_working_hours",
                    )

                    print(f"✅ Файл отправлен редактору {destination}. ID сообщения: {sent_msg.id}")
                    print(f"📝 Ожидаю PDF файл: {original_name_without_ext}.pdf от редактора")
                    self._log_editor(f"✅ Файл отправлен редактору {destination}. ID сообщения: {sent_msg.id}")
                    self._log_editor(f"📝 Ожидаю PDF файл: {original_name_without_ext}.pdf от редактора")

            else:
                editor_nickname = (
                    file_info.get("forced_editor_nickname")
                    if fixed_author_editor_route
                    else self._editor_for_file(file_info)
                )
                if editor_nickname:
                    destination = editor_nickname
                    route_to_24_7 = (
                        not fixed_author_editor_route
                        and self._is_editor_unavailable(destination)
                        and Config.get_setting("editor_24_7")
                    )
                    if route_to_24_7:
                        destination = Config.get_setting("editor_24_7")
                        print(f"📤 Редактор недоступен, отправляем файл круглосуточному редактору через НИК-2: {destination}")
                        self._log_editor(f"📤 Редактор недоступен, отправляем файл круглосуточному редактору через НИК-2: {destination} | file={file_info.get('original_file_name', file_info['file_name'])}")
                    else:
                        print(f"📤 Отправляем файл редактору через НИК-2: {destination}")
                        self._log_editor(f"📤 Отправляем файл редактору через НИК-2: {destination} | file={file_info.get('original_file_name', file_info['file_name'])}")

                    sent_msg = await client_nik2.send_document(
                        chat_id=destination,
                        document=temp_path,
                        file_name=file_info.get("original_file_name", file_info["file_name"])
                    )

                    original_name = file_info.get("original_file_name", file_info["file_name"])
                    original_name_without_ext = os.path.splitext(original_name)[0]
                    tracking_key = (
                        self._new_editor_tracking_key("fixed_editor", sent_msg)
                        if fixed_author_editor_route
                        else f"no_checks_{sent_msg.id}"
                    )

                    self.editor_tracking[tracking_key] = {
                        "author": file_info["author"],
                        "author_id": file_info.get("author_id"),
                        "original_name": original_name,
                        "original_name_without_ext": original_name_without_ext,
                        "expected_pdf_name": f"{original_name_without_ext}.pdf",
                        "sent_at": datetime.now(),
                        "chat_id": sent_msg.chat.id,
                        "message_id": sent_msg.id,
                        "destination": destination,
                        "sent_from_account": "НИК-2",
                        "reply_to_message_id": sent_msg.id,
                        "file_uid": file_info.get("file_uid"),
                        "source_platform": file_info.get("source_platform", "telegram"),
                        "route_sender_id": file_info.get("route_sender_id"),
                        "route_chat_id": file_info.get("route_chat_id"),
                        "route_message_id": file_info.get("route_message_id"),
                        "route_is_group": file_info.get("route_is_group", False),
                        "gateway_job_id": file_info.get("gateway_job_id"),
                        "local_path": file_info.get("local_path"),
                        "message": file_info.get("message"),
                        "download_url": file_info.get("download_url"),
                        "force_plagiscan": file_info.get("force_plagiscan", False),
                        "via_normal_destination": file_info.get("via_normal_destination", False),
                    }
                    if fixed_author_editor_route:
                        self.editor_tracking[tracking_key].update(
                            {
                                "expected_ai_pdf_name": f"ИИ {original_name_without_ext}.pdf",
                                "delivered_reports": set(),
                                "fixed_author_editor_route": True,
                                "account_released": False,
                            }
                        )
                    await self._mark_gateway_job_waiting_editor(
                        file_info,
                        "waiting_editor247_response" if route_to_24_7 else "waiting_editor_response",
                    )

                    print(f"✅ Файл отправлен редактору {destination}. ID сообщения: {sent_msg.id}")
                    print(f"📝 Ожидаю PDF файл: {original_name_without_ext}.pdf от редактора")
                    self._log_editor(f"✅ Файл отправлен редактору {destination}. ID сообщения: {sent_msg.id}")
                    self._log_editor(f"📝 Ожидаю PDF файл: {original_name_without_ext}.pdf от редактора")

                else:
                    print("❌ Редактор не настроен (editor_nickname). Файл не отправлен.")

            # Очистка временных файлов
            self.processor.cleanup_temp_files(temp_files_to_cleanup)

        except Exception as e:
            print(f"❌ Ошибка при перенаправлении через НИК-2: {e}")
            self._log_editor(f"❌ Ошибка при перенаправлении через НИК-2: {e}")
            self.processor.cleanup_temp_files(temp_files_to_cleanup)

    async def handle_editor_response(self, client, message, resolved_reply_to_message_id=None):
        """Обработка ответов от редактора (PDF файлов) для НИК-2"""
        response_claim_key = None
        response_accepted = False
        try:
            if self._editor_response_was_claimed(message):
                return
            effective_reply_id = resolved_reply_to_message_id or getattr(message, "reply_to_message_id", None)
            message_chat_id = getattr(getattr(message, "chat", None), "id", None)
            tracking_key = None
            # Проверяем, что это сообщение от НИК-2
            if client.name != "НИК-2":
                # Если это не НИК-2, проверяем стандартное отслеживание
                tracking_info = self.editor_tracking.get(effective_reply_id)
                if tracking_info:
                    tracking_key = effective_reply_id
                if not tracking_info:
                    return
            else:
                # Для НИК-2 ищем tracking по специальному ключу
                tracking_key, tracking_info = self._find_editor_tracking_by_reply(
                    effective_reply_id,
                    chat_id=message_chat_id,
                    sent_from_account="НИК-2",
                )

            if not tracking_info and not effective_reply_id and getattr(message, "document", None):
                file_name = getattr(message.document, "file_name", "") or ""
                if file_name.lower().endswith(".pdf"):
                    sender = (
                        message.from_user.username or str(message.from_user.id)
                        if getattr(message, "from_user", None)
                        else ""
                    )
                    matched_key, matched_info, _reason = self.find_editor_tracking_for_unreplied_pdf(
                        sender,
                        file_name,
                        sent_from_account="НИК-2",
                        reply_chat_id=message_chat_id,
                    )
                    if matched_info:
                        tracking_key = matched_key
                        tracking_info = matched_info
                        effective_reply_id = matched_info.get("reply_to_message_id")

            if not tracking_info:
                if client.name == "НИК-2":
                    self._warn_unknown_editor_response(message, effective_reply_id)
                return

            print(f"\n📨 Ответ от редактора на сообщение {effective_reply_id}")
            print(f"📄 Тип файла: {'АНТИ' if tracking_info.get('is_anti_file') else 'Обычный'}")
            print(f"👤 Получено в аккаунте: {client.name}")
            self._log_editor(f"📨 Ответ от редактора на сообщение {effective_reply_id} | account={client.name} file={tracking_info.get('original_name')}")

            # Проверяем, что редактор отправил документ
            if not message.document:
                response_claim_key, claimed = self._claim_editor_response(message)
                if not claimed:
                    return
                if message.text and self._is_no_checks_message(message.text):
                    destination = tracking_info.get("destination")
                    self._mark_editor_unavailable(destination)
                    print(f"⛔ Редактор {destination or 'неизвестен'} сообщил, что проверки закончились")
                    self._log_editor(
                        f"⛔ Редактор {destination or 'unknown'} сообщил, что проверки закончились | "
                        f"file={tracking_info.get('original_name')}"
                    )
                    print("⛔ Новые файлы этому редактору больше не отправляем")
                    print("ℹ️ Уже отправленные файлы остаются в ожидании: если редактор пришлет PDF, бот доставит его автору")
                    response_accepted = True
                    return
                error_file_name = self._extract_editor_error_file_name(getattr(message, "text", None))
                if error_file_name:
                    if not self._editor_tracking_matches_file(error_file_name, tracking_info):
                        if effective_reply_id:
                            self._release_editor_response_claim(response_claim_key)
                            print(f"⚠️ Ошибка редактора не привязана: {error_file_name}, reply mismatch")
                            self._log_editor(
                                f"⚠️ Ошибка редактора не привязана: file={error_file_name} reply mismatch"
                            )
                            return
                        sender = message.from_user.username or str(message.from_user.id) if getattr(message, "from_user", None) else ""
                        matched_key, matched_info, reason = self.find_editor_tracking_for_unreplied_pdf(
                            sender,
                            error_file_name,
                            sent_from_account=tracking_info.get("sent_from_account", "НИК-2"),
                            reply_chat_id=message_chat_id,
                        )
                        if not matched_info:
                            self._release_editor_response_claim(response_claim_key)
                            print(f"⚠️  Ошибка редактора не привязана: {error_file_name}, {reason}")
                            self._log_editor(f"⚠️ Ошибка редактора не привязана: file={error_file_name} reason={reason}")
                            return
                        tracking_key = matched_key
                        tracking_info = matched_info

                    print(f"⚠️  Редактор сообщил ошибку по файлу: {error_file_name}. Автору ничего не отправляем.")
                    self._log_editor(
                        f"⚠️ Редактор сообщил ошибку по файлу: {error_file_name}; "
                        f"закрываем ожидание без доставки"
                    )
                    if tracking_key:
                        self.editor_tracking.pop(tracking_key, None)
                    self._finish_processing(tracking_info.get("file_uid"), False)
                    if tracking_info.get("gateway_job_id"):
                        await self._mark_gateway_job_failed(
                            tracking_info,
                            f"editor_error: {message.text}",
                            retry_delay=0,
                        )
                    if tracking_info.get('sent_from_account') == "НИК-2":
                        self.manager.mark_account_free("НИК-2", tracking_info.get('original_name', ''))
                        asyncio.create_task(self.process_queue())
                    response_accepted = True
                    return
                self._release_editor_response_claim(response_claim_key)
                print("⚠️  Редактор не отправил документ")
                return

            file_name = message.document.file_name

            # Проверяем, что это PDF файл (редактор должен вернуть PDF)
            if not file_name.lower().endswith('.pdf'):
                print(f"⚠️  Редактор вернул не PDF файл: {file_name}")
                return

            print(f"✅ Редактор вернул PDF: {file_name}")
            self._log_editor(f"✅ Редактор вернул PDF: {file_name}")

            if not self._editor_tracking_matches_file(file_name, tracking_info):
                if effective_reply_id:
                    print(
                        f"⚠️  PDF от редактора не отправлен: имя {file_name} "
                        "не соответствует задаче из reply"
                    )
                    self._log_editor(
                        f"⚠️ PDF от редактора не отправлен: file={file_name} "
                        f"tracking={tracking_info.get('original_name')} — reply mismatch"
                    )
                    return
                sender = message.from_user.username or str(message.from_user.id) if getattr(message, "from_user", None) else ""
                matched_key, matched_info, reason = self.find_editor_tracking_for_unreplied_pdf(
                    sender,
                    file_name,
                    sent_from_account=tracking_info.get("sent_from_account", "НИК-2"),
                    reply_chat_id=message_chat_id,
                )
                if matched_info:
                    tracking_key = matched_key
                    tracking_info = matched_info
                    effective_reply_id = matched_info.get("reply_to_message_id")
                else:
                    print(f"⚠️  PDF от редактора не отправлен: имя {file_name} не совпало с ожидающим файлом ({reason})")
                    self._log_editor(
                        f"⚠️ PDF от редактора не отправлен: file={file_name} "
                        f"tracking={tracking_info.get('original_name')} reason={reason}"
                    )
                    return

            if tracking_info.get("fixed_author_editor_route"):
                delivered_reports = tracking_info.setdefault("delivered_reports", set())
                report_key = self._editor_report_key(file_name)
                if report_key in delivered_reports:
                    print(f"⏭️  PDF редактора уже доставлен: {file_name}")
                    self._log_editor(f"⏭️ PDF редактора уже доставлен повторно: {file_name}")
                    return

            response_claim_key, claimed = self._claim_editor_response(message)
            if not claimed:
                return

            # Редакторский PDF отправляем с тем именем, которое вернул редактор.
            deliver_file_name = file_name
            delivery_accepted = False

            # Если это ответ в НИК-2, нужно отправить файл автору через НИК-1
            if client.name == "НИК-2":
                # Получаем клиент НИК-1 для отправки обратно автору
                client_nik1 = self.manager.get_client("НИК-1")
                if not client_nik1:
                    self._release_editor_response_claim(response_claim_key)
                    print("❌ Аккаунт НИК-1 не найден для отправки автору")
                    return

                # Скачиваем файл (используем реальный путь от Pyrogram)
                temp_path = self._build_result_file_path(deliver_file_name)
                print(f"📥 Скачиваю PDF от редактора: {temp_path}...")
                downloaded_path = await message.download(temp_path)
                if downloaded_path:
                    temp_path = downloaded_path  # реальный путь (Pyrogram может добавить суффикс)

                if not os.path.exists(temp_path):
                    self._release_editor_response_claim(response_claim_key)
                    print(f"❌ Файл не скачался: {temp_path}", flush=True)
                    return
                sent_ok = False
                try:
                    delivery = await self._deliver_document_to_origin(
                        tracking_info,
                        temp_path,
                        file_name=deliver_file_name,
                        telegram_client=client_nik1,
                    )
                    spooled = isinstance(delivery, dict) and delivery.get("status") == "spooled"
                    sent_ok = not spooled
                    delivery_accepted = sent_ok or (
                        spooled
                        and str(tracking_info.get("source_platform") or "telegram").lower() != "telegram"
                    )
                    if sent_ok:
                        print("✅ PDF от редактора доставлен в исходный канал", flush=True)
                        self._log_editor("✅ PDF от редактора доставлен в исходный канал")
                    else:
                        reason = delivery.get("reason") if isinstance(delivery, dict) else None
                        print(f"⚠️ PDF от редактора сохранен в outbox для повторной доставки: {reason}", flush=True)
                        self._log_editor(f"⚠️ PDF от редактора сохранен в outbox для повторной доставки: {reason}")
                except Exception as e:
                    print(f"❌ Сбой доставки PDF от редактора: {e}", flush=True)
                    self._log_editor(f"❌ Сбой доставки PDF от редактора: {e}")
                    import traceback
                    traceback.print_exc()
                if delivery_accepted:
                    response_accepted = True
                if not sent_ok:
                    print("❌ PDF не доставлен — все попытки исчерпаны", flush=True)

                print(f"📁 Файл: {deliver_file_name} ({'обрезан' if tracking_info.get('is_anti_file') else 'без обрезки'})", flush=True)
                self._finish_processing(tracking_info.get("file_uid"), sent_ok)
                if sent_ok and not tracking_info.get("gateway_job_completed"):
                    await self._mark_gateway_job_done(tracking_info, temp_path, "editor_response_delivered")
                    tracking_info["gateway_job_completed"] = True

            else:
                # Старая логика для НИК-1
                # Отправляем файл автору через текущий клиент (НИК-1)
                temp_path = os.path.join(FILES_DIR, f"{uuid.uuid4().hex[:8]}_{deliver_file_name}")
                downloaded_path = await message.download(temp_path)
                if downloaded_path:
                    temp_path = downloaded_path

                # Fix #3: проверка успешного скачивания
                if not os.path.exists(temp_path):
                    self._release_editor_response_claim(response_claim_key)
                    print(f"❌ Файл не скачался: {temp_path}", flush=True)
                    return

                sent_ok = False
                final_path = temp_path
                try:
                    delivery = await self._deliver_document_to_origin(
                        tracking_info,
                        final_path,
                        file_name=deliver_file_name,
                        telegram_client=client,
                    )
                    spooled = isinstance(delivery, dict) and delivery.get("status") == "spooled"
                    sent_ok = not spooled
                    delivery_accepted = sent_ok or (
                        spooled
                        and str(tracking_info.get("source_platform") or "telegram").lower() != "telegram"
                    )
                    if sent_ok:
                        print(f"✅ PDF от редактора доставлен: {tracking_info['author']}")
                        self._log_editor(f"✅ PDF от редактора доставлен: {tracking_info['author']}")
                    else:
                        reason = delivery.get("reason") if isinstance(delivery, dict) else None
                        print(f"⚠️ PDF от редактора сохранен в outbox для повторной доставки: {reason}", flush=True)
                        self._log_editor(f"⚠️ PDF от редактора сохранен в outbox для повторной доставки: {reason}")
                except Exception as e:
                    print(f"❌ Сбой доставки PDF от редактора (НИК-1): {e}", flush=True)
                    self._log_editor(f"❌ Сбой доставки PDF от редактора (НИК-1): {e}")
                    import traceback
                    traceback.print_exc()
                if delivery_accepted:
                    response_accepted = True
                if not sent_ok:
                    print("❌ PDF не доставлен (НИК-1) — все попытки исчерпаны", flush=True)

                self._finish_processing(tracking_info.get("file_uid"), sent_ok)
                if sent_ok and not tracking_info.get("gateway_job_completed"):
                    await self._mark_gateway_job_done(tracking_info, final_path, "editor_response_delivered")
                    tracking_info["gateway_job_completed"] = True

            response_accepted = delivery_accepted
            if not delivery_accepted:
                self._release_editor_response_claim(response_claim_key)

            if tracking_info.get("fixed_author_editor_route") and delivery_accepted:
                tracking_info.setdefault("delivered_reports", set()).add(self._editor_report_key(file_name))

            # Удаляем из отслеживания
            if delivery_accepted and not tracking_info.get("fixed_author_editor_route") and client.name == "НИК-2":
                # Удаляем по специальному ключу
                if tracking_key in self.editor_tracking:
                    del self.editor_tracking[tracking_key]
                    print(f"🗑️  Удален из отслеживания: {tracking_key}")
            elif delivery_accepted and not tracking_info.get("fixed_author_editor_route") and effective_reply_id in self.editor_tracking:
                del self.editor_tracking[effective_reply_id]
                print(f"🗑️  Удален из отслеживания: {effective_reply_id}")

            # Освобождаем аккаунт НИК-2 если он был занят
            if tracking_info.get('sent_from_account') == "НИК-2":
                if not tracking_info.get("fixed_author_editor_route") or not tracking_info.get("account_released"):
                    anti_file_name = tracking_info.get('original_name', '')
                    self.manager.mark_account_free("НИК-2", anti_file_name)
                    tracking_info["account_released"] = True
                    print(f"🔄 Аккаунт НИК-2 освобожден от файла: {anti_file_name}")
                    asyncio.create_task(self.process_queue())

            # Удаляем временные файлы
            if os.path.exists(temp_path):
                os.remove(temp_path)
            if 'final_path' in locals() and final_path != temp_path and os.path.exists(final_path):
                os.remove(final_path)

            # Обновляем отслеживание
            author = tracking_info["author"]
            if delivery_accepted and author in self.file_tracking:
                if file_name not in self.file_tracking[author]["received"]:
                    self.file_tracking[author]["received"].append(file_name)

                # Удаляем из ожидающих
                if tracking_info["original_name"] in self.file_tracking[author]["pending"]:
                    self.file_tracking[author]["pending"].remove(tracking_info["original_name"])

        except Exception as e:
            if response_claim_key is not None and not response_accepted:
                self._release_editor_response_claim(response_claim_key)
            print(f"❌ Ошибка в handle_editor_response: {e}")
            self._log_editor(f"❌ Ошибка в handle_editor_response: {e}")
            import traceback
            traceback.print_exc()

    async def handle_ai_bot_response(self, client, message):
        """Обработка ответов от AI бота"""
        temp_files_to_cleanup = []

        try:
            message_text = message.text[:100] if message.text else 'без текста'
            print(f"\n🤖 ОТВЕТ ОТ AI БОТА: {message_text}")
            self._log_aaa(f"🤖 ОТВЕТ ОТ AI БОТА: {message_text}")

            if message.text and self._is_ai_upload_prompt(message.text):
                self._get_ai_upload_ready_event(client.name).set()
                print(f"✅ AAA бот готов принять файл для аккаунта {client.name}")
                self._log_aaa(f"✅ AAA бот готов принять файл для аккаунта {client.name}")

            # Фильтруем сервисные сообщения ДО поиска processing_info,
            # чтобы не показывать "Не найден файл" на безобидные сообщения
            # НО: не фильтруем сообщения об ошибках (содержащие "Что-то пошло не так", "20 МБ" и т.д.)
            if message.text and "нет проверок" not in message.text.lower() and "Проверка завершена" not in message.text \
                and "Что-то пошло не так" not in message.text \
                and "20 МБ" not in message.text \
                and "Обратитесь к администратору" not in message.text \
                and "не могу загрузить" not in message.text \
                and (
                "У вас осталось" in message.text
                or "Пожалуйста, загрузите файл" in message.text
                or "загружен на проверку" in message.text
                or "Проверяем файл" in message.text
                or "в очереди" in message.text
                or "в обработке" in message.text
                or "файлов сегодня" in message.text
                or "Для покупки нажмите" in message.text
                or "/pay" in message.text
                or "🔄 Проверяем" in message.text
            ):
                print(f"ℹ️ Пропущено сервисное сообщение от бота: {message.text[:50]}...")
                return

            # Пробуем найти соответствующий файл по нескольким методам
            file_info = None
            file_key = None
            processing_info = None

            if message.reply_to_message_id:
                # Ищем файл, который был отправлен этим сообщением
                for key, info in self.current_processing_files.items():
                    if info["account"] == client.name and info["bot_type"] == "ai":
                        map_key = f"{message.chat.id}_{message.reply_to_message_id}"
                        if map_key in self.message_to_file_map:
                            if self.message_to_file_map[map_key] == key:
                                processing_info = info
                                file_key = key
                                break

            if not processing_info:
                oldest_key = None
                for key, info in self.current_processing_files.items():
                    if info["account"] == client.name and info["bot_type"] == "ai":
                        if oldest_key is None or key < oldest_key:
                            oldest_key = key
                            processing_info = info
                            file_key = key

            if not processing_info:
                if message.text and self._is_global_aaa_unavailable_message(message.text):
                    print("⛔ Получено глобальное сообщение AAA об отсутствии проверок")
                    self._log_aaa("⛔ Получено глобальное сообщение AAA об отсутствии проверок")
                    await self._handle_global_aaa_unavailable(client, message)
                    return
                # "Проверка завершена" может прийти после того как файл уже обработан
                if message.text and "Проверка завершена" in message.text:
                    # Блокируем ТОЛЬКО если проверки реально исчерпаны
                    if self._is_no_checks_message(message.text):
                        print("⛔ Проверка завершена + нет проверок — блокируем аккаунт")
                        self.manager.blacklist_account(client.name)
                    else:
                        print("ℹ️ Проверка завершена (файл уже обработан) — аккаунт НЕ блокируем")
                    return
                print("⚠️  Не найден соответствующий файл в обработке")
                return

            if message.text and self._is_global_aaa_unavailable_message(message.text) \
                    and "antiplagiat" not in message.text.lower() \
                    and "report" not in message.text.lower() \
                    and "http" not in message.text:
                print("⛔ Обнаружено глобальное сообщение AAA об отсутствии проверок")
                self._log_aaa("⛔ Обнаружено глобальное сообщение AAA об отсутствии проверок")
                await self._handle_global_aaa_unavailable(client, message, processing_info, file_key)
                return

            # "Проверка завершена" без ссылки на отчёт — это сообщение о предыдущем файле,
            # НЕ трогаем processing_info текущего файла
            if message.text and "Проверка завершена" in message.text \
                    and "antiplagiat" not in message.text.lower() \
                    and "report" not in message.text.lower() \
                    and "http" not in message.text:
                # Блокируем ТОЛЬКО если проверки реально исчерпаны
                if self._is_no_checks_message(message.text):
                    print("⛔ Проверка завершена + нет проверок — блокируем аккаунт, НЕ трогаем текущий файл")
                    self.manager.blacklist_account(client.name)
                else:
                    print("ℹ️ Проверка завершена (без отчёта) — игнорируем, аккаунт НЕ блокируем")
                return

            # Оригинальный docx будет удалён через cleanup в конце (успех)
            # или внутри handle_aaa_error_message / handle_no_more_checks (ошибка/нет проверок)
            orig_docx = processing_info.get("temp_path")
            if orig_docx:
                temp_files_to_cleanup.append(orig_docx)

            # Получаем клиент НИК-1 для отправки ответа
            client_nik1 = self.manager.get_client("НИК-1")
            if not client_nik1:
                print("❌ Аккаунт НИК-1 не найден для отправки ответа")
                return

            # Проверяем на сообщение об ошибке от AAA бота
            if message.text and (
                "Что-то пошло не так" in message.text
                or "Телеграм не пропускает файлы размером более 20 МБ." in message.text
                or "Ваш файл больше 20 МБ" in message.text
                or "быть больше 20 МБ" in message.text
                or "не могу загрузить его напрямую" in message.text
                or "Обратитесь к администратору" in message.text
            ):
                print("⚠️ Обнаружена ошибка от AAA бота (возможно >20МБ)")
                self._log_aaa("⚠️ Обнаружена ошибка от AAA бота (возможно >20МБ)")
                await self.handle_aaa_error_message(client, message, processing_info, file_key)
                return


            # Проверяем, содержит ли сообщение ссылку на отчет (ПРИОРИТЕТ над "нет проверок")
            if message.text and (
                    "antiplagiat.ru/report" in message.text or "antiplagiat.ru/report" in message.text.lower()):
                print("✅ Обнаружен отчет от AI бота")
                self._log_aaa("✅ Обнаружен отчет от AI бота")
                if orig_docx: temp_files_to_cleanup.append(orig_docx) # Теперь можно удалять

                # Извлекаем URL из сообщения (ищем любую ссылку на антиплагиат)
                url_pattern = r'https?://[^\s/]+\.antiplagiat\.ru/report/[^\s]+'
                url_match = re.search(url_pattern, message.text, re.IGNORECASE)

                if not url_match:
                    # Пробуем найти любую ссылку с report
                    url_pattern = r'https?://[^\s]+/report/[^\s]+'
                    url_match = re.search(url_pattern, message.text, re.IGNORECASE)

                if url_match:
                    report_url = url_match.group(0)
                    print(f"📄 URL отчета: {report_url[:80]}...")

                    # Скачиваем PDF с универсальной логикой
                    temp_pdf = self._build_result_file_path(f"report_{datetime.now().strftime('%H%M%S%f')}.pdf")
                    temp_files_to_cleanup.append(temp_pdf)

                    print(f"📥 Скачиваю отчет...")
                    if await self._run_blocking(self.processor.process_antiplagiat_url, report_url, temp_pdf):
                        # Обрезаем PDF
                        cropped_pdf = f"files/cropped_{os.path.basename(temp_pdf)}"
                        temp_files_to_cleanup.append(cropped_pdf)

                        if await self._run_blocking(self.processor.crop_pdf, temp_pdf, cropped_pdf):
                            # Сохраняем с оригинальным именем файла
                            original_name_without_ext = os.path.splitext(processing_info['original_file_name'])[0]
                            final_name = f"{original_name_without_ext}.pdf"  # Используем оригинальное имя
                            final_path = self._build_result_file_path(final_name)

                            os.rename(cropped_pdf, final_path)
                            temp_files_to_cleanup.append(final_path)

                            try:
                                await self._deliver_document_to_origin(
                                    processing_info,
                                    final_path,
                                    file_name=final_name,
                                    telegram_client=client_nik1,
                                )
                                print("✅ Отчет доставлен в исходный канал")
                                self._log_aaa("✅ Отчет доставлен в исходный канал")
                                self._finish_processing(processing_info.get("file_uid"), True)
                                await self._mark_gateway_job_done(processing_info, final_path, "ai_report_delivered")

                                # Обновляем отслеживание (ИИ-отчет не считается отдельным файлом)
                                author = processing_info["author"]
                                if author in self.file_tracking and not self.processor.is_ai_report_filename(final_name):
                                    self.file_tracking[author]["received"].append(final_name)

                                # Освобождаем аккаунт, который использовался для отправки в бота
                                self.manager.mark_account_free(client.name, "")
                                print(f"🔄 Аккаунт {client.name} освобожден")
                                asyncio.create_task(self.process_queue())

                                # Удаляем из текущей обработки
                                if file_key in self.current_processing_files:
                                    del self.current_processing_files[file_key]

                                # Если в том же сообщении проверки исчерпаны — только блокируем аккаунт, не перебрасываем файл
                                if self._is_global_aaa_unavailable_message(message.text):
                                    self._activate_global_aaa_unavailable(trigger_account=client.name)
                                elif self._is_no_checks_message(message.text):
                                    print("⛔ Проверки закончились (после отчёта) — блокируем аккаунт, файл уже отправлен автору")
                                    self.manager.blacklist_account(client.name)

                            except Exception as e:
                                print(f"❌ Ошибка доставки результата в исходный канал: {e}")
                                self._finish_processing(processing_info.get("file_uid"), False)
                                await self._mark_gateway_job_failed(processing_info, f"deliver_result_failed: {e}")
                        else:
                            print("❌ Ошибка обрезки PDF")
                    else:
                        print("❌ Ошибка скачивания PDF")

            # Проверяем, есть ли кнопка с отчетом
            elif message.reply_markup and hasattr(message.reply_markup, 'inline_keyboard'):
                print("📄 Проверяю кнопки в сообщении...")
                self._log_aaa("📄 Проверяю кнопки в сообщении...")
                report_button = None

                for row in message.reply_markup.inline_keyboard:
                    for button in row:
                        if button.url and (
                                '.pdf' in button.url.lower() or 'отчет' in (button.text or '').lower() or 'report' in (
                                button.text or '').lower() or 'antiplagiat' in button.url.lower()):
                            report_button = button
                            print(f"✅ Найдена кнопка отчета: {button.text}")
                            break
                    if report_button:
                        break

                if report_button and report_button.url:
                    print("📄 Обработка кнопки отчета")
                    if orig_docx: temp_files_to_cleanup.append(orig_docx) # Теперь можно удалять

                    # Скачиваем PDF с универсальной логикой
                    temp_pdf = self._build_result_file_path(f"report_{datetime.now().strftime('%H%M%S%f')}.pdf")
                    temp_files_to_cleanup.append(temp_pdf)

                    print(f"📥 Скачиваю отчет...")
                    if await self._run_blocking(self.processor.process_antiplagiat_url, report_button.url, temp_pdf):
                        # Обрезаем PDF
                        cropped_pdf = f"files/cropped_{os.path.basename(temp_pdf)}"
                        temp_files_to_cleanup.append(cropped_pdf)

                        if await self._run_blocking(self.processor.crop_pdf, temp_pdf, cropped_pdf):
                            # Сохраняем с оригинальным именем файла
                            original_name_without_ext = os.path.splitext(processing_info['original_file_name'])[0]
                            final_name = f"{original_name_without_ext}.pdf"  # Используем оригинальное имя
                            final_path = self._build_result_file_path(final_name)

                            os.rename(cropped_pdf, final_path)
                            temp_files_to_cleanup.append(final_path)

                            try:
                                await self._deliver_document_to_origin(
                                    processing_info,
                                    final_path,
                                    file_name=final_name,
                                    telegram_client=client_nik1,
                                )
                                print("✅ Отчет доставлен в исходный канал")
                                self._log_aaa("✅ Отчет доставлен в исходный канал")
                                self._finish_processing(processing_info.get("file_uid"), True)
                                await self._mark_gateway_job_done(processing_info, final_path, "ai_report_delivered")

                                # Обновляем отслеживание (ИИ-отчет не считается отдельным файлом)
                                author = processing_info["author"]
                                if author in self.file_tracking and not self.processor.is_ai_report_filename(final_name):
                                    self.file_tracking[author]["received"].append(final_name)

                                # Освобождаем аккаунт
                                self.manager.mark_account_free(client.name, "")
                                print(f"🔄 Аккаунт {client.name} освобожден")
                                asyncio.create_task(self.process_queue())

                                # Удаляем из текущей обработки
                                if file_key in self.current_processing_files:
                                    del self.current_processing_files[file_key]

                                # Если в том же сообщении проверки исчерпаны — только блокируем аккаунт
                                if self._is_global_aaa_unavailable_message(message.text):
                                    self._activate_global_aaa_unavailable(trigger_account=client.name)
                                elif self._is_no_checks_message(message.text):
                                    print("⛔ Проверки закончились (после отчёта) — блокируем аккаунт")
                                    self.manager.blacklist_account(client.name)

                            except Exception as e:
                                print(f"❌ Ошибка доставки результата в исходный канал: {e}")
                                self._finish_processing(processing_info.get("file_uid"), False)
                                await self._mark_gateway_job_failed(processing_info, f"deliver_result_failed: {e}")
                        else:
                            print("❌ Ошибка обрезки PDF")
                    else:
                        print("❌ Ошибка скачивания PDF")

            # Проверяем, есть ли прямая ссылку в тексте
            elif message.text and "http" in message.text:
                print("🔍 Ищу ссылку в тексте сообщения...")
                self._log_aaa("🔍 Ищу ссылку в тексте сообщения...")
                # Ищем любую ссылку с antiplagiat или report
                url_pattern = r'https?://[^\s]+'
                url_match = re.search(url_pattern, message.text)
                if url_match:
                    report_url = url_match.group(0)
                    print(f"📄 Найдена ссылка: {report_url[:80]}...")

                    # Проверяем, это ли отчет антиплагиата
                    if 'antiplagiat' in report_url.lower() or 'report' in report_url.lower():
                        if orig_docx: temp_files_to_cleanup.append(orig_docx) # Теперь можно удалять
                        # Скачиваем PDF с универсальной логикой
                        temp_pdf = self._build_result_file_path(f"report_{datetime.now().strftime('%H%M%S%f')}.pdf")
                        temp_files_to_cleanup.append(temp_pdf)

                        print(f"📥 Скачиваю отчет...")
                        if await self._run_blocking(self.processor.process_antiplagiat_url, report_url, temp_pdf):
                            # Обрезаем PDF если это PDF
                            if temp_pdf.endswith('.pdf'):
                                cropped_pdf = f"files/cropped_{os.path.basename(temp_pdf)}"
                                temp_files_to_cleanup.append(cropped_pdf)

                                if await self._run_blocking(self.processor.crop_pdf, temp_pdf, cropped_pdf):
                                    # Сохраняем с оригинальным именем файла
                                    original_name_without_ext = os.path.splitext(processing_info['original_file_name'])[
                                        0]
                                    final_name = f"{original_name_without_ext}.pdf"  # Используем оригинальное имя
                                    final_path = self._build_result_file_path(final_name)

                                    os.rename(cropped_pdf, final_path)
                                else:
                                    # Если не удалось обрезать, просто переименовываем
                                    original_name_without_ext = os.path.splitext(processing_info['original_file_name'])[
                                        0]
                                    final_name = f"{original_name_without_ext}.pdf"  # Используем оригинальное имя
                                    final_path = self._build_result_file_path(final_name)
                                    os.rename(temp_pdf, final_path)
                            else:
                                # Если не PDF, просто переименовываем
                                original_name_without_ext = os.path.splitext(processing_info['original_file_name'])[0]
                                final_name = f"{original_name_without_ext}.pdf"  # Используем оригинальное имя
                                final_path = self._build_result_file_path(final_name)
                                os.rename(temp_pdf, final_path)

                            temp_files_to_cleanup.append(final_path)

                            try:
                                await self._deliver_document_to_origin(
                                    processing_info,
                                    final_path,
                                    file_name=final_name,
                                    telegram_client=client_nik1,
                                )
                                print("✅ Отчет доставлен в исходный канал")
                                self._finish_processing(processing_info.get("file_uid"), True)
                                await self._mark_gateway_job_done(processing_info, final_path, "ai_report_delivered")

                                # Обновляем отслеживание (ИИ-отчет не считается отдельным файлом)
                                author = processing_info["author"]
                                if author in self.file_tracking and not self.processor.is_ai_report_filename(final_name):
                                    self.file_tracking[author]["received"].append(final_name)

                                # Освобождаем аккаунт
                                self.manager.mark_account_free(client.name, "")
                                print(f"🔄 Аккаунт {client.name} освобожден")
                                asyncio.create_task(self.process_queue())

                                # Удаляем из текущей обработки
                                if file_key in self.current_processing_files:
                                    del self.current_processing_files[file_key]

                                if self._is_global_aaa_unavailable_message(message.text):
                                    self._activate_global_aaa_unavailable(trigger_account=client.name)

                            except Exception as e:
                                print(f"❌ Ошибка доставки результата в исходный канал: {e}")
                                self._finish_processing(processing_info.get("file_uid"), False)
                                await self._mark_gateway_job_failed(processing_info, f"deliver_result_failed: {e}")
                        else:
                            print("❌ Ошибка скачивания отчета")

            # Случай 1: "Проверка завершена" — проверка ПРОШЛА, файл уже обработан (ссылка была в предыдущем сообщении)
            # Блокируем ТОЛЬКО если также сказано "нет проверок"
            elif message.text and "Проверка завершена" in message.text:
                if self._is_global_aaa_unavailable_message(message.text):
                    self._activate_global_aaa_unavailable(trigger_account=client.name)
                    print("⛔ Проверка завершена + глобально нет проверок — отключаем AAA")
                elif self._is_no_checks_message(message.text):
                    print("⛔ Проверка завершена + нет проверок — блокируем аккаунт")
                    self.manager.blacklist_account(client.name)
                else:
                    print("ℹ️ Проверка завершена — аккаунт НЕ блокируем, проверки ещё есть")
                return

            # Случай 2: "нет проверок" / "У вас пока нет проверок" — файл НЕ проверен
            elif message.text and self._is_no_checks_message(message.text):
                print("⛔ Обнаружено сообщение о завершении проверок")
                if orig_docx: temp_files_to_cleanup.append(orig_docx)
                await self.handle_no_more_checks(client, message, processing_info, file_key)
                return

            else:
                # Неизвестное сообщение от бота (рассылка, новый текст ошибки и т.п.)
                # НЕ удаляем orig_docx — файл ещё в обработке
                print(f"ℹ️ Неизвестное сообщение от AI бота — игнорируем: {message.text[:80] if message.text else 'без текста'}")
                return

            # Очистка временных файлов
            self.processor.cleanup_temp_files(temp_files_to_cleanup)

        except Exception as e:
            print(f"❌ Ошибка обработки ответа AI бота: {e}")
            import traceback
            traceback.print_exc()
            self.processor.cleanup_temp_files(temp_files_to_cleanup)

    async def handle_anti_bot_response(self, client, message):
        """Обработка ответов от антиплагиат бота"""
        temp_files_to_cleanup = []

        try:
            print(f"\n🛡️  ОТВЕТ ОТ АНТИПЛАГИАТ БОТА: {message.text[:100] if message.text else 'без текста'}")
            self._log_plagiscan(f"🛡️  ОТВЕТ ОТ АНТИПЛАГИАТ БОТА: {message.text[:100] if message.text else 'без текста'}")

            # Фильтр сервисных сообщений от плагискана — просто игнорируем
            if message.text and (
                "Проверяем файл" in message.text
                or "загружен на проверку" in message.text
                or "в очереди" in message.text
            ):
                print(f"ℹ️ Пропущено сервисное сообщение от плагискана: {message.text[:50]}...")
                return

            # Находим соответствующий файл: сначала reply к отправленному документу,
            # потом имя PDF/текст ответа, и только последним fallback старейший файл.
            file_key, processing_info, match_reason = self.find_anti_processing_for_response(client.name, message)

            if not processing_info:
                print("⚠️  Не найден соответствующий файл в обработке")
                return
            print(f"ℹ️ Ответ Плагискана привязан к {processing_info.get('original_file_name')} ({match_reason})")

            async def redirect_plagiscan_file_to_editor(reason: str, *, is_anti: bool):
                file_info_for_editor = {
                    "author": processing_info.get("author"),
                    "author_id": processing_info.get("author_id"),
                    "file_name": processing_info.get("original_file_name"),
                    "original_file_name": processing_info.get("original_file_name"),
                    "message": processing_info.get("message"),
                    "chat_id": processing_info.get("chat_id"),
                    "file_uid": processing_info.get("file_uid"),
                    "local_path": processing_info.get("local_path"),
                    "gateway_job_id": processing_info.get("gateway_job_id"),
                    "source_platform": processing_info.get("source_platform", "telegram"),
                    "route_sender_id": processing_info.get("route_sender_id"),
                    "route_sender_name": processing_info.get("route_sender_name"),
                    "route_chat_id": processing_info.get("route_chat_id"),
                    "route_message_id": processing_info.get("route_message_id"),
                    "route_is_group": processing_info.get("route_is_group", False),
                    "is_anti": is_anti,
                }
                await self.send_to_nik2(file_info_for_editor, reason=reason)

            # Проверяем на ошибку размера файла от плагискан бота — просто пропускаем, грузим следующий
            if message.text and (
                "более 20 МБ" in message.text
                or "больше 20 МБ" in message.text
                or "уменьшите размер" in message.text.lower()
                or "уменьшите размер своего документа" in message.text.lower()
            ):
                print("⚠️  Ошибка плагискана (размер файла) — пропускаем файл, загружаем следующий")
                self._log_plagiscan("⚠️  Ошибка плагискана (размер файла) — пропускаем файл, загружаем следующий")
                if file_key in self.current_processing_files:
                    del self.current_processing_files[file_key]
                self.manager.mark_account_free(client.name, processing_info.get("original_file_name", ""))
                asyncio.create_task(self.process_queue())
                return

            # Закончились проверки в Плагискане — проверяем РАНЬШЕ outer guard.
            # Плагискан может прислать КОМБИНИРОВАННОЕ сообщение:
            # "✅ Ваш файл успешно проверен!\nОригинальность: 56.58%\n...\nУ вас закончились проверки 😉"
            # Если нет PDF-вложения — редиректим файл редактору.
            # Если PDF есть — пропускаем сюда: success-путь ниже его обработает (и аккаунт освободит).
            if (
                message.text
                and self._is_no_checks_message(message.text)
                and not self._is_plagiscan_success_message(message.text)
                and not getattr(message, "document", None)
            ):
                if file_key in self.current_processing_files:
                    del self.current_processing_files[file_key]
                self.manager.mark_account_free(client.name, processing_info.get("original_file_name", ""))
                if processing_info.get("via_normal_destination"):
                    # Обычный файл шёл в Плагискан по настройке — перенаправляем редактору
                    print(f"⛔ Плагискан: проверки закончились — перенаправляем обычный файл редактору: {processing_info.get('original_file_name')}")
                    self._log_plagiscan(f"⛔ Плагискан: проверки закончились — normal_destination=плагискан, перенаправляем редактору: {processing_info.get('original_file_name')}")
                    await redirect_plagiscan_file_to_editor("Плагискан: закончились проверки", is_anti=False)
                elif processing_info.get("force_plagiscan"):
                    # Принудительный автор — ничего не делаем
                    print(f"⛔ Плагискан: проверки закончились — принудительная проверка автора, ничего не делаем: {processing_info.get('original_file_name')}")
                    self._log_plagiscan(f"⛔ Плагискан: проверки закончились — force_plagiscan автора, ничего не делаем: {processing_info.get('original_file_name')}")
                else:
                    print(f"⛔ Плагискан: проверки закончились — перенаправляем анти-файл редактору: {processing_info.get('original_file_name')}")
                    self._log_plagiscan(f"⛔ Плагискан: проверки закончились — анти-файл, перенаправляем редактору: {processing_info.get('original_file_name')}")
                    await redirect_plagiscan_file_to_editor("Плагискан: закончились проверки", is_anti=True)
                asyncio.create_task(self.process_queue())
                return

            # Общий catch для любых ошибок от плагискана (⚠/❌/ошибк — но НЕ успешные результаты)
            if message.text and not self._is_plagiscan_success_message(message.text):
                text_lower = message.text.lower()

                # Закончились проверки (чистое сообщение без успеха) — уже обработано выше,
                # но оставляем как дополнительный catch на случай вариантов без ✅/Оригинальность
                if self._is_no_checks_message(message.text):
                    if file_key in self.current_processing_files:
                        del self.current_processing_files[file_key]
                    self.manager.mark_account_free(client.name, processing_info.get("original_file_name", ""))
                    if processing_info.get("via_normal_destination"):
                        print(f"⛔ Плагискан: проверки закончились — перенаправляем обычный файл редактору: {processing_info.get('original_file_name')}")
                        self._log_plagiscan(f"⛔ Плагискан: проверки закончились — normal_destination=плагискан, перенаправляем редактору: {processing_info.get('original_file_name')}")
                        await redirect_plagiscan_file_to_editor("Плагискан: закончились проверки", is_anti=False)
                    elif processing_info.get("force_plagiscan"):
                        print(f"⛔ Плагискан: проверки закончились — принудительная проверка автора, ничего не делаем: {processing_info.get('original_file_name')}")
                        self._log_plagiscan(f"⛔ Плагискан: проверки закончились — force_plagiscan автора, ничего не делаем: {processing_info.get('original_file_name')}")
                    else:
                        print(f"⛔ Плагискан: проверки закончились — перенаправляем анти-файл редактору: {processing_info.get('original_file_name')}")
                        self._log_plagiscan(f"⛔ Плагискан: проверки закончились — анти-файл, перенаправляем редактору: {processing_info.get('original_file_name')}")
                        await redirect_plagiscan_file_to_editor("Плагискан: закончились проверки", is_anti=True)
                    asyncio.create_task(self.process_queue())
                    return

                if "⚠" in message.text or "❌" in message.text or "ошибк" in text_lower:
                    print(f"⚠️  Общая ошибка плагискана — пропускаем файл: {message.text[:80]}")
                    self._log_plagiscan(f"⚠️  Общая ошибка плагискана — пропускаем файл: {message.text[:80]}")
                    if file_key in self.current_processing_files:
                        del self.current_processing_files[file_key]
                    self.manager.mark_account_free(client.name, processing_info.get("original_file_name", ""))
                    asyncio.create_task(self.process_queue())
                    return

            # Получаем клиент НИК-1 для отправки ответа
            client_nik1 = self.manager.get_client("НИК-1")
            if not client_nik1:
                print("❌ Аккаунт НИК-1 не найден для отправки ответа")
                return

            # Функция для обработки и отправки отчета
            async def process_and_send_report(report_url, processing_info, file_key, report_kind="main", finish_after=True):
                """Обработка и отправка отчета"""
                report_label = "ИИ отчет" if report_kind == "ai" else "отчет"
                print(f"📄 Скачиваю {report_label} антиплагиата: {report_url[:80]}...")
                self._log_plagiscan(f"📄 Скачиваю {report_label} антиплагиата: {report_url[:80]}...")

                temp_pdf = self._build_result_file_path(f"anti_report_{datetime.now().strftime('%H%M%S%f')}.pdf")
                temp_files_to_cleanup.append(temp_pdf)

                if await self._run_blocking(self.processor.download_pdf, report_url, temp_pdf):
                    # ОБРЕЗАЕМ PDF как и для обычных отчетов
                    cropped_pdf = self._build_result_file_path(f"cropped_{os.path.basename(temp_pdf)}")
                    temp_files_to_cleanup.append(cropped_pdf)

                    if await self._run_blocking(self.processor.crop_pdf, temp_pdf, cropped_pdf):
                        # Сохраняем с оригинальным именем файла
                        original_name_without_ext = os.path.splitext(processing_info['original_file_name'])[0]
                        final_name = (
                            f"ИИ {original_name_without_ext}.pdf"
                            if report_kind == "ai"
                            else f"{original_name_without_ext}.pdf"
                        )
                        final_path = self._build_result_file_path(final_name)

                        os.rename(cropped_pdf, final_path)
                        temp_files_to_cleanup.append(final_path)

                        try:
                            await self._deliver_document_to_origin(
                                processing_info,
                                final_path,
                                file_name=final_name,
                                telegram_client=client_nik1,
                            )
                            print("✅ Антиплагиат отчет доставлен в исходный канал")
                            self._log_plagiscan("✅ Антиплагиат отчет доставлен в исходный канал")
                            if report_kind == "ai":
                                processing_info["ai_report_delivered"] = True
                            else:
                                processing_info["main_report_delivered"] = True

                            if finish_after:
                                self._finish_processing(processing_info.get("file_uid"), True)
                                await self._mark_gateway_job_done(processing_info, final_path, "anti_report_delivered")

                            # Обновляем отслеживание (ИИ-отчет не считается отдельным файлом)
                            author = processing_info["author"]
                            if author in self.file_tracking and not self.processor.is_ai_report_filename(final_name):
                                self.file_tracking[author]["received"].append(final_name)

                            if finish_after:
                                # Освобождаем аккаунт только от этого файла
                                self.manager.mark_account_free(client.name, processing_info.get("original_file_name", ""))
                                print(f"🔄 Аккаунт {client.name} освобожден от файла: {processing_info.get('original_file_name')}")
                                asyncio.create_task(self.process_queue())

                                # Удаляем из текущей обработки
                                if file_key in self.current_processing_files:
                                    del self.current_processing_files[file_key]

                            return True

                        except Exception as e:
                            print(f"❌ Ошибка доставки результата в исходный канал: {e}")
                            self._finish_processing(processing_info.get("file_uid"), False)
                            await self._mark_gateway_job_failed(processing_info, f"deliver_result_failed: {e}")
                            return False
                    else:
                        print("❌ Ошибка обрезки PDF антиплагиата")
                        # Пробуем отправить необрезанный вариант
                        original_name_without_ext = os.path.splitext(processing_info['original_file_name'])[0]
                        final_name = (
                            f"ИИ {original_name_without_ext}.pdf"
                            if report_kind == "ai"
                            else f"{original_name_without_ext}.pdf"
                        )
                        final_path = self._build_result_file_path(final_name)
                        os.rename(temp_pdf, final_path)
                        temp_files_to_cleanup.append(final_path)

                        try:
                            await self._deliver_document_to_origin(
                                processing_info,
                                final_path,
                                file_name=final_name,
                                telegram_client=client_nik1,
                            )
                            print(f"✅ Отправлен необрезанный антиплагиат отчет")
                            self._log_plagiscan("✅ Отправлен необрезанный антиплагиат отчет")
                            if report_kind == "ai":
                                processing_info["ai_report_delivered"] = True
                            else:
                                processing_info["main_report_delivered"] = True
                            if finish_after:
                                self._finish_processing(processing_info.get("file_uid"), True)
                                await self._mark_gateway_job_done(processing_info, final_path, "anti_report_delivered_uncropped")
                                self.manager.mark_account_free(client.name, processing_info.get("original_file_name", ""))
                                print(f"🔄 Аккаунт {client.name} освобожден от файла: {processing_info.get('original_file_name')}")

                                if file_key in self.current_processing_files:
                                    del self.current_processing_files[file_key]

                                asyncio.create_task(self.process_queue())
                            return True
                        except Exception as e:
                            print(f"❌ Ошибка доставки необрезанного результата в исходный канал: {e}")
                            self._finish_processing(processing_info.get("file_uid"), False)
                            await self._mark_gateway_job_failed(processing_info, f"deliver_uncropped_failed: {e}")
                            return False
                else:
                    print("❌ Ошибка скачивания PDF антиплагиата")
                    return False

            # Проверяем сообщение об успешной проверке
            if message.text and self._is_plagiscan_success_message(message.text):
                print("✅ Файл успешно проверен антиплагиатом")
                self._log_plagiscan("✅ Файл успешно проверен антиплагиатом")

                # Извлекаем процент оригинальности
                originality_match = re.search(r'Оригинальность:\s*([\d.,]+)\s*%', message.text, re.IGNORECASE)
                originality = originality_match.group(1).replace(",", ".") if originality_match else "N/A"

                print(f"📊 Оригинальность: {originality}%")
                machine_percent = self._extract_machine_generation_percent(message.text)
                force_ai_report = bool(processing_info.get("force_plagiscan"))
                need_ai_report = force_ai_report and machine_percent is not None and machine_percent > 0
                if need_ai_report:
                    processing_info["awaiting_ai_report"] = True
                    print(f"🤖 Машинная генерация: {machine_percent}% — нужен отдельный ИИ отчет")
                    self._log_plagiscan(f"🤖 Машинная генерация {machine_percent}% — нужен ИИ отчет")
                elif machine_percent is not None and machine_percent > 0:
                    print(
                        f"🤖 Машинная генерация: {machine_percent}% — второй ИИ отчет "
                        "выдается только принудительным авторам"
                    )
                    self._log_plagiscan(
                        f"🤖 Машинная генерация {machine_percent}% — ИИ отчет пропущен: "
                        "автор не принудительный"
                    )

                # Проверяем, есть ли кнопка для получения отчета
                report_button, ai_report_button = self._find_plagiscan_report_buttons(message)
                if report_button:
                    print(f"📄 Найдена кнопка отчета: {report_button.text}")
                if ai_report_button:
                    print(f"🤖 Найдена кнопка ИИ отчета: {ai_report_button.text}")

                if report_button:
                    print(f"📤 Отправляю команду для получения отчета: {report_button.text}")

                    # Нажимаем на кнопку
                    try:
                        if getattr(report_button, 'url', None):
                            finish_after = not (need_ai_report and ai_report_button)
                            await process_and_send_report(report_button.url, processing_info, file_key, "main", finish_after)
                            if need_ai_report and ai_report_button and getattr(ai_report_button, 'url', None):
                                await process_and_send_report(ai_report_button.url, processing_info, file_key, "ai", True)
                            elif need_ai_report and ai_report_button:
                                print(f"📤 Запрашиваю ИИ отчет: {ai_report_button.text}")
                                await client.request_callback_answer(
                                    message.chat.id,
                                    message.id,
                                    callback_data=ai_report_button.callback_data
                                )
                        elif hasattr(report_button, 'callback_data') and report_button.callback_data:
                            if need_ai_report and ai_report_button and getattr(ai_report_button, "callback_data", None):
                                processing_info["pending_ai_callback"] = {
                                    "chat_id": message.chat.id,
                                    "message_id": message.id,
                                    "callback_data": ai_report_button.callback_data,
                                    "button_text": ai_report_button.text,
                                }
                            # Если это callback кнопка
                            await client.request_callback_answer(
                                message.chat.id,
                                message.id,
                                callback_data=report_button.callback_data
                            )
                            print("✅ Callback отправлен, ждем отчет...")
                            return  # Выходим, ожидаем следующее сообщение с отчетом

                    except Exception as e:
                        print(f"⚠️  Ошибка при нажатии кнопки: {e}")
                        # Пробуем альтернативный метод
                        try:
                            await client.send_message(message.chat.id, "Отчет")
                            print("✅ Запрос отчета отправлен")
                        except Exception as e2:
                            print(f"❌ Ошибка альтернативного запроса: {e2}")

                # Если нет кнопки, просто отмечаем как завершенное
                else:
                    print("ℹ️  Кнопка отчета не найдена, файл проверен")

                    # Освобождаем аккаунт только от этого файла
                    self.manager.mark_account_free(client.name, processing_info.get("original_file_name", ""))
                    print(f"🔄 Аккаунт {client.name} освобожден от файла: {processing_info.get('original_file_name')}")
                    asyncio.create_task(self.process_queue())

                    # Удаляем из текущей обработки
                    if file_key in self.current_processing_files:
                        del self.current_processing_files[file_key]

            # Проверяем, содержит ли сообщение ссылку на отчет
            elif message.text and "https://" in message.text:
                print("📄 Проверяю наличие ссылки на отчет...")

                # Извлекаем URL из сообщения
                url_match = re.search(r'https?://[^\s]+', message.text)
                if url_match:
                    report_url = url_match.group(0)

                    # Проверяем, это ли ссылка на антиплагиат отчет
                    if 'antiplagiat' in report_url.lower() or 'report' in report_url.lower() or 'отчет' in report_url.lower():
                        await process_and_send_report(report_url, processing_info, file_key)
                    else:
                        print(f"⚠️  Ссылка не похожа на отчет антиплагиата: {report_url[:80]}...")

            # Проверяем, есть ли документ (PDF отчет)
            elif message.document and message.document.mime_type == 'application/pdf':
                print("📄 Обнаружен PDF документ от антиплагиат бота")
                report_kind = "ai" if processing_info.get("main_report_delivered") and processing_info.get("awaiting_ai_report") else "main"
                finish_after = report_kind == "ai" or not processing_info.get("awaiting_ai_report")

                # Скачиваем документ
                temp_pdf = self._build_result_file_path(f"anti_doc_{datetime.now().strftime('%H%M%S%f')}.pdf")
                await message.download(temp_pdf)
                temp_files_to_cleanup.append(temp_pdf)

                # Обрабатываем как отчет
                original_name_without_ext = os.path.splitext(processing_info['original_file_name'])[0]
                final_name = (
                    f"ИИ {original_name_without_ext}.pdf"
                    if report_kind == "ai"
                    else f"{original_name_without_ext}.pdf"
                )
                final_path = self._build_result_file_path(final_name)

                # Обрезаем PDF
                if await self._run_blocking(self.processor.crop_pdf, temp_pdf, final_path):
                    print(f"✅ PDF антиплагиата обрезан: {final_path}")
                else:
                    # Если не удалось обрезать, просто копируем
                    import shutil
                    shutil.copy2(temp_pdf, final_path)
                    print(f"⚠️  PDF антиплагиата не обрезан, сохранен как: {final_path}")

                temp_files_to_cleanup.append(final_path)

                # Отправляем автору
                try:
                    await self._deliver_document_to_origin(
                        processing_info,
                        final_path,
                        file_name=final_name,
                        telegram_client=client_nik1,
                    )
                    print(f"✅ Отчет антиплагиата отправлен автору")
                    self._log_plagiscan("✅ Отчет антиплагиата отправлен автору")
                    if report_kind == "ai":
                        processing_info["ai_report_delivered"] = True
                    else:
                        processing_info["main_report_delivered"] = True

                    if processing_info["author"] in self.file_tracking and not self.processor.is_ai_report_filename(final_name):
                        self.file_tracking[processing_info["author"]]["received"].append(final_name)

                    pending_ai_callback = processing_info.get("pending_ai_callback")
                    if report_kind == "main" and pending_ai_callback:
                        print(f"📤 Запрашиваю ИИ отчет: {pending_ai_callback.get('button_text')}")
                        await client.request_callback_answer(
                            pending_ai_callback["chat_id"],
                            pending_ai_callback["message_id"],
                            callback_data=pending_ai_callback["callback_data"],
                        )
                        return

                    if finish_after:
                        self._finish_processing(processing_info.get("file_uid"), True)
                        await self._mark_gateway_job_done(processing_info, final_path, "anti_pdf_document_delivered")

                    if finish_after:
                        self.manager.mark_account_free(client.name, processing_info.get("original_file_name", ""))
                        print(f"🔄 Аккаунт {client.name} освобожден от файла: {processing_info.get('original_file_name')}")
                        asyncio.create_task(self.process_queue())

                        if file_key in self.current_processing_files:
                            del self.current_processing_files[file_key]

                except Exception as e:
                    print(f"❌ Ошибка отправки отчета антиплагиата: {e}")

            # Очистка временных файлов
            self.processor.cleanup_temp_files(temp_files_to_cleanup)

        except Exception as e:
            print(f"❌ Ошибка обработки ответа антиплагиат бота: {e}")
            self._log_plagiscan(f"❌ Ошибка обработки ответа антиплагиат бота: {e}")
            import traceback
            traceback.print_exc()
            self.processor.cleanup_temp_files(temp_files_to_cleanup)

    async def handle_aaa_error_message(self, client, message, processing_info, file_key):
        """Обработка сообщения об ошибке от AAA бота"""
        try:
            print(f"⚠️  Обрабатываю ошибку от AAA бота для файла: {processing_info['original_file_name']}")
            self._log_aaa(f"⚠️  Обрабатываю ошибку от AAA бота для файла: {processing_info['original_file_name']}")

            # При ошибке AAA — всегда через НИК-2 редактору (скрывая отправителя)
            client_nik2 = self.manager.get_client("НИК-2")
            if not client_nik2:
                print("❌ Аккаунт НИК-2 не найден для отправки редактору при ошибке AAA")
                return

            # Определяем редактора в зависимости от режима
            current_mode = Config.get_setting("mode")
            if current_mode == "mode2":
                editor_nickname = Config.get_setting("editor_24_7")
            else:
                editor_nickname = Config.get_setting("normal_editor_nickname") or Config.get_setting("editor_nickname")
            if not editor_nickname:
                print("❌ Редактор не настроен. Файл не отправлен.")
                return

            original_file_path = processing_info.get("temp_path")
            if not original_file_path or not os.path.exists(original_file_path):
                print("❌ Оригинальный файл не найден для отправки редактору — освобождаем аккаунт")
                self.manager.mark_account_free(client.name, "")
                if file_key in self.current_processing_files:
                    del self.current_processing_files[file_key]
                asyncio.create_task(self.process_queue())
                return

            print(f"📤 Ошибка AAA → скачиваем и отправляем через НИК-2 редактору: {editor_nickname}")
            self._log_aaa(f"📤 Ошибка AAA → скачиваем и отправляем через НИК-2 редактору: {editor_nickname}")
            self._log_editor(f"📤 Ошибка AAA → скачиваем и отправляем через НИК-2 редактору: {editor_nickname} | file={processing_info.get('original_file_name', 'document.docx')}")

            # Метод download + send_document гарантирует скрытие данных отправителя (автора)
            sent_msg = await client_nik2.send_document(
                chat_id=editor_nickname,
                document=original_file_path,
                file_name=processing_info.get("original_file_name", "document.docx")
            )

            original_name = processing_info.get("original_file_name", "unknown")
            original_name_without_ext = os.path.splitext(original_name)[0]
            tracking_key = f"aaa_error_{sent_msg.id}"

            self.editor_tracking[tracking_key] = {
                "author": processing_info["author"],
                "author_id": processing_info.get("author_id"),
                "original_name": original_name,
                "original_name_without_ext": original_name_without_ext,
                "expected_pdf_name": f"{original_name_without_ext}.pdf",
                "sent_at": datetime.now(),
                "chat_id": sent_msg.chat.id,
                "message_id": sent_msg.id,
                "destination": editor_nickname,
                "is_from_editor": True,
                "sent_from_account": "НИК-2",
                "reply_to_message_id": sent_msg.id,
                "file_uid": processing_info.get("file_uid"),
                "source_platform": processing_info.get("source_platform", "telegram"),
                "route_sender_id": processing_info.get("route_sender_id"),
                "route_chat_id": processing_info.get("route_chat_id"),
                "route_message_id": processing_info.get("route_message_id"),
                "route_is_group": processing_info.get("route_is_group", False),
                "gateway_job_id": processing_info.get("gateway_job_id"),
            }
            await self._mark_gateway_job_waiting_editor(processing_info, "waiting_editor_response_after_aaa_error")

            print(f"✅ Файл отправлен редактору через НИК-2. Ожидаю: {original_name_without_ext}.pdf")
            self._log_aaa(f"✅ Файл отправлен редактору через НИК-2. Ожидаю: {original_name_without_ext}.pdf")
            self._log_editor(f"✅ Файл отправлен редактору через НИК-2. Ожидаю: {original_name_without_ext}.pdf")

            # Освобождаем аккаунт
            self.manager.mark_account_free(client.name, "")
            print(f"🔄 Аккаунт {client.name} освобожден")
            asyncio.create_task(self.process_queue())

            # Удаляем из текущей обработки
            if file_key in self.current_processing_files:
                del self.current_processing_files[file_key]
                print(f"🗑️  Удален файл {file_key} из обработки")

            # Удаляем временный файл
            if original_file_path and os.path.exists(original_file_path):
                os.remove(original_file_path)
                print(f"🗑️  Удален временный файл: {original_file_path}")

        except Exception as e:
            print(f"❌ Ошибка обработки сообщения об ошибке AAA бота: {e}")
            self._log_aaa(f"❌ Ошибка обработки сообщения об ошибке AAA бота: {e}")
            import traceback
            traceback.print_exc()
            # Освобождаем аккаунт при любой ошибке (в т.ч. PEER_FLOOD)
            try:
                self.manager.mark_account_free(client.name, "")
                print(f"🔄 Аккаунт {client.name} освобожден (после ошибки AAA)")
                asyncio.create_task(self.process_queue())
            except Exception:
                pass
            if file_key in self.current_processing_files:
                del self.current_processing_files[file_key]
                print(f"🗑️  Удален файл {file_key} из обработки (после ошибки AAA)")

    # Fix #2: удалён дублирующий cleanup_old_editor_tracking (основная версия на строке ~1549)

    async def handle_no_more_checks(self, client, message, processing_info, file_key):
        """Обработка сообщения о том, что проверки закончились"""
        try:
            print(f"⛔ Обрабатываю сообщение о завершении проверок для аккаунта: {client.name}")

            # Добавляем аккаунт в черный список (локальный + менеджер)
            if client.name not in self.blacklisted_accounts:
                self.blacklisted_accounts.append(client.name)
            self.manager.blacklist_account(client.name)  # менеджер сам печатает сообщение

            # Проверяем, остались ли еще живые аккаунты
            normal_accounts = self._get_normal_accounts()
            alive_accounts = [acc for acc in normal_accounts if acc not in self.manager.blacklisted_accounts]

            if processing_info.get("force_plagiscan"):
                print("⛔ Проверки закончились — принудительная проверка автора, ничего не делаем.")
                self._log_plagiscan(
                    f"⛔ no_more_checks force_plagiscan автора, ничего не делаем: {processing_info.get('original_file_name')}"
                )
            elif alive_accounts:
                print(f"⏳ Есть другие доступные аккаунты ({alive_accounts}). Возвращаем файл в очередь.")

                # Реконструируем file_info для повторной очереди
                file_info = {
                    "author": processing_info["author"],
                    "author_id": processing_info.get("author_id"),
                    "file_name": processing_info["original_file_name"],
                    "message_id": processing_info["message_id"],
                    "chat_id": processing_info["chat_id"],
                    "is_anti": processing_info.get("bot_type") == "anti",
                    "received_at": datetime.now(),
                    "status": "повторная очередь",
                    "message": processing_info.get("message"), # Используем сохраненный объект сообщения
                    "original_file_name": processing_info["original_file_name"],
                    "sent_to_editor": False,
                    "file_uid": processing_info.get("file_uid"),
                    "local_path": processing_info.get("local_path"),
                    "gateway_job_id": processing_info.get("gateway_job_id"),
                    "source_platform": processing_info.get("source_platform", "telegram"),
                    "route_sender_id": processing_info.get("route_sender_id"),
                    "route_chat_id": processing_info.get("route_chat_id"),
                    "route_message_id": processing_info.get("route_message_id"),
                    "route_is_group": processing_info.get("route_is_group", False),
                }

                if file_info["message"] or file_info.get("local_path"):
                    self.manager.file_queue.insert(0, file_info)
                    print(f"✅ Файл {file_info['file_name']} возвращен в начало очереди")
                else:
                    print("❌ Ошибка: нет ни объекта сообщения, ни local_path. Не удалось вернуть в очередь.")
            else:
                # ВСЕ аккаунты исчерпаны
                print("❌ ВСЕ аккаунты для обычных файлов исчерпаны! Перенаправляем редактору.")

                file_info_to_send = {
                    "author": processing_info["author"],
                    "author_id": processing_info.get("author_id"),
                    "file_name": processing_info["original_file_name"],
                    "message": processing_info.get("message"),
                    "chat_id": processing_info["chat_id"],
                    "original_file_name": processing_info["original_file_name"],
                    "local_path": processing_info.get("local_path"),
                    "gateway_job_id": processing_info.get("gateway_job_id"),
                    "source_platform": processing_info.get("source_platform", "telegram"),
                    "route_sender_id": processing_info.get("route_sender_id"),
                    "route_chat_id": processing_info.get("route_chat_id"),
                    "route_message_id": processing_info.get("route_message_id"),
                    "route_is_group": processing_info.get("route_is_group", False),
                }

                # Если сообщения нет в processing_info, попробуем использовать текущее (хотя оно от бота)
                if not file_info_to_send["message"]:
                    file_info_to_send["message"] = message

                await self.send_to_nik2(file_info_to_send, reason="все аккаунты без проверок")

            # Освобождаем аккаунт
            self.manager.mark_account_free(client.name, "")
            print(f"🔄 Аккаунт {client.name} освобожден и заблокирован")

            # Очищаем mapping для этого сообщения
            if message and hasattr(message, 'id'):
                map_key = f"{message.chat.id}_{message.id}"
                if map_key in self.message_to_file_map:
                    del self.message_to_file_map[map_key]
                    print(f"🗑️  Удален mapping для сообщения {map_key}")

            # Удаляем ТОЛЬКО этот файл из обработки
            if file_key in self.current_processing_files:
                if self.current_processing_files[file_key].get("account") == client.name:
                    del self.current_processing_files[file_key]
                    print(f"🗑️  Удален файл {file_key} из обработки")

            # Удаляем временный файл
            original_file_path = processing_info.get("temp_path")
            if original_file_path and os.path.exists(original_file_path):
                os.remove(original_file_path)
                print(f"🗑️  Удален временный файл: {original_file_path}")

            # Удаляем все сопоставления для этого аккаунта
            self.cleanup_account_mappings(client.name)

            # Запускаем обработку очереди для других аккаунтов
            asyncio.create_task(self.process_queue())

        except Exception as e:
            print(f"❌ Ошибка обработки сообщения о завершении проверок: {e}")
            import traceback
            traceback.print_exc()

    def cleanup_account_mappings(self, account_name):
        """Очистка всех сопоставлений для указанного аккаунта"""
        try:
            # Находим все file_key для этого аккаунта
            file_keys_to_remove = []
            for file_key, info in self.current_processing_files.items():
                if info.get("account") == account_name:
                    file_keys_to_remove.append(file_key)

            # Удаляем найденные файлы
            for file_key in file_keys_to_remove:
                del self.current_processing_files[file_key]
                print(f"🗑️  Удален файл {file_key} для аккаунта {account_name}")

            # Удаляем все mapping для этого аккаунта из message_to_file_map
            mappings_to_remove = []
            for map_key, file_key in self.message_to_file_map.items():
                if file_key in file_keys_to_remove:
                    mappings_to_remove.append(map_key)

            for map_key in mappings_to_remove:
                del self.message_to_file_map[map_key]
                print(f"🗑️  Удален mapping {map_key}")

            print(f"✅ Очищены все mapping для аккаунта {account_name}")

        except Exception as e:
            print(f"⚠️  Ошибка при очистке mapping для {account_name}: {e}")

    def show_statistics(self):
        """Показать статистику по файлам с выводом режима 3"""
        print("\n" + "=" * 50)
        print("📊 СТАТИСТИКА ОБРАБОТКИ ФАЙЛОВ")
        print("=" * 50)

        current_mode = Config.get_setting("mode")

        if current_mode == "mode3":
            # Показываем статистику режима 3
            author = Config.get_setting("counter_author")
            pattern = Config.get_setting("counter_pattern")
            start_file = Config.get_setting("counter_start_file")
            end_file = Config.get_setting("counter_end_file")
            start_time = Config.get_setting("counter_start_time")
            counter_date = Config.get_setting("counter_date")
            end_date = Config.get_setting("counter_end_date")
            end_time = Config.get_setting("counter_end_time")

            print(f"\n⚙️  НАСТРОЙКИ РЕЖИМА 3:")
            print(f"   👤 Автор: {author}")
            print(f"   🔍 Паттерн: {pattern if pattern else 'все файлы'}")
            print(f"   📅 Начало: {counter_date or 'сегодня'} {start_time or '00:00'} | Файл: {start_file or 'с первого'}")
            print(f"   📅 Конец:  {end_date or 'сегодня'} {end_time or 'текущее время'} | Файл: {end_file or 'до последнего'}")
            print("-" * 30)

            # Предлагаем запустить анализ
            print("\n📊 Для получения статистики запустите режим 3")
            print("   (пункт 5 в главном меню)")
        else:
            # Старая статистика для режимов 1 и 2
            if not self.file_tracking:
                print("Нет данных об обработанных файлах")
                return

            total_anti = 0
            total_normal = 0
            not_sent = []

            for author, data in self.file_tracking.items():
                print(f"\n👤 Автор: {author}")
                print(f"   Отправлено: {len(data['sent'])}")
                print(f"   Получено обратно: {len(data['received'])}")
                print(f"   Ожидает ответа: {len(data['pending'])}")

                # Анализируем отправленные файлы
                for sent in data['sent']:
                    if 'анти' in sent.lower() or '@plagaiscan' in sent:
                        total_anti += 1
                    elif '@AAA' in sent:
                        total_normal += 1

                # Собираем неотправленные
                for pending in data['pending']:
                    not_sent.append(f"{author}: {pending}")

            print(f"\n📈 ИТОГО:")
            print(f"   Проверено анти-файлов: {total_anti}")
            print(f"   Проверено обычных файлов: {total_normal}")
            print(f"   Всего файлов в обработке: {len(self.manager.file_queue)}")

            # Показываем заблокированные аккаунты
            if self.blacklisted_accounts:
                print(f"   Заблокированные аккаунты: {', '.join(self.blacklisted_accounts)}")

            if not_sent:
                print(f"\n⚠️  Не отправлено клиенту:")
                for file in not_sent[:10]:  # Показываем первые 10
                    print(f"   - {file}")
                if len(not_sent) > 10:
                    print(f"   ... и еще {len(not_sent) - 10} файлов")

    async def run_counter_mode(self):
        """Запуск режима 3 - Счетчик файлов"""
        print("\n" + "=" * 60)
        print("📊 ЗАПУСК РЕЖИМА 3 - СЧЕТЧИК ФАЙЛОВ")
        print("=" * 60)

        # Определяем тип анализа
        counter_type = Config.get_setting("counter_type", "author")

        # Получаем общие параметры диапазона
        start_file = Config.get_setting("counter_start_file", "")
        end_file = Config.get_setting("counter_end_file", "")
        start_time_str = Config.get_setting("counter_start_time", "")
        date_str = Config.get_setting("counter_date", datetime.now().strftime("%Y-%m-%d"))
        end_date_str = Config.get_setting("counter_end_date", "")
        end_time_str = Config.get_setting("counter_end_time", "")

        if counter_type == "editor":
            editor_nick = Config.get_setting("counter_editor_nick", "")
            if not editor_nick:
                print("❌ Не указан ник редактора для анализа")
                print("Сначала настройте режим 3 в главном меню")
                input("\nНажмите Enter для возврата в меню...")
                return
            print(f"👤 Анализ файлов редактора: {editor_nick}")
            await self.counter_processor.analyze_editor_files_custom(
                editor_nick, start_file, start_time_str, date_str,
                end_file=end_file, end_date_str=end_date_str, end_time_str=end_time_str,
            )
        else:
            author = Config.get_setting("counter_author")
            if not author:
                print("❌ Не указан автор для анализа")
                print("Сначала настройте режим 3 в главном меню")
                input("\nНажмите Enter для возврата в меню...")
                return
            print(f"👤 Анализ файлов автора: {author}")
            await self.counter_processor.analyze_author_files_custom(
                author, start_file, start_time_str, date_str,
                end_file=end_file, end_date_str=end_date_str, end_time_str=end_time_str,
            )

        input("\nНажмите Enter для возврата в меню...")
