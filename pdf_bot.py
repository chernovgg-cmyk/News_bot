#!/usr/bin/env python3
"""
Анализатор PDF из Telegram.
Опрашивает бота через getUpdates, находит присланные PDF-файлы,
извлекает текст, анализирует через DeepSeek (пересказ + важные моменты)
и отправляет результат обратно в тот же чат.

Состояние (последний обработанный update_id) хранится в state.json,
который workflow коммитит обратно в репозиторий — так файлы не
обрабатываются повторно между запусками.
"""

import os
import sys
import json
import time
import html
import io

import requests
from pypdf import PdfReader

# ---------------------------------------------------------------------------
# КОНФИГУРАЦИЯ
# ---------------------------------------------------------------------------

DEEPSEEK_API_KEY = os.environ["DEEPSEEK_API_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]

# Опционально: ограничить приём файлов только от тебя (твой chat_id).
# Если переменная задана — бот реагирует на PDF только от этого пользователя,
# а чужие файлы игнорирует. Рекомендуется задать.
ALLOWED_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

STATE_FILE = "state.json"
MAX_PDF_BYTES = 20 * 1024 * 1024          # лимит Telegram getFile = 20 МБ
MAX_TEXT_CHARS = 60000                     # потолок текста для отправки в LLM
MIN_TEXT_CHARS = 200                       # ниже этого считаем PDF сканом/пустым

API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


# ---------------------------------------------------------------------------
# СОСТОЯНИЕ
# ---------------------------------------------------------------------------

def load_offset():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f).get("last_update_id", 0)
    except (FileNotFoundError, json.JSONDecodeError):
        return 0


def save_offset(update_id):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"last_update_id": update_id}, f)


# ---------------------------------------------------------------------------
# TELEGRAM
# ---------------------------------------------------------------------------

def get_updates(offset):
    # offset = last_update_id + 1 => получаем только новые апдейты,
    # и заодно подтверждаем (acknowledge) старые на стороне Telegram.
    r = requests.get(f"{API}/getUpdates", params={
        "offset": offset + 1,
        "timeout": 0,
        "allowed_updates": json.dumps(["message"]),
    }, timeout=40)
    r.raise_for_status()
    return r.json().get("result", [])


def send_message(chat_id, text):
    for chunk in split_message(text):
        r = requests.post(f"{API}/sendMessage", json={
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=30)
        if not r.ok:
            print(f"[error] sendMessage {r.status_code}: {r.text}", file=sys.stderr)
        time.sleep(0.4)


def send_chat_action(chat_id, action="typing"):
    try:
        requests.post(f"{API}/sendChatAction",
                      json={"chat_id": chat_id, "action": action}, timeout=10)
    except requests.RequestException:
        pass


def split_message(text, limit=4000):
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = ""
        current += line + "\n"
    if current.strip():
        chunks.append(current)
    return chunks or [""]


def download_file(file_id):
    """Вернуть (bytes, filename) или (None, reason)."""
    r = requests.get(f"{API}/getFile", params={"file_id": file_id}, timeout=30)
    if not r.ok:
        return None, f"getFile вернул {r.status_code}: {r.text}"
    file_path = r.json()["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
    fr = requests.get(url, timeout=120)
    if not fr.ok:
        return None, f"скачивание вернуло {fr.status_code}"
    return fr.content, file_path


# ---------------------------------------------------------------------------
# PDF -> ТЕКСТ
# ---------------------------------------------------------------------------

def extract_pdf_text(data):
    """Извлечь текст из PDF. Вернуть (text, n_pages)."""
    reader = PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception as e:
            print(f"[warn] страница не извлеклась: {e}", file=sys.stderr)
    text = "\n".join(pages).strip()
    return text, len(reader.pages)


# ---------------------------------------------------------------------------
# АНАЛИЗ ЧЕРЕЗ DEEPSEEK
# ---------------------------------------------------------------------------

def analyze(text):
    system = (
        "Ты — аналитик, который разбирает обзоры СМИ и новостные сводки. "
        "Тебе дают извлечённый из PDF текст. Сделай разбор на русском языке.\n\n"
        "Структура ответа строго такая:\n"
        "<b>📌 Краткий пересказ</b>\n"
        "3-6 предложений: о чём документ в целом, ключевые темы.\n\n"
        "<b>⚠️ На что обратить внимание</b>\n"
        "Маркированный список (символ •) из самого важного и потенциально "
        "критичного: негатив, риски, упоминания компании в невыгодном свете, "
        "регуляторные/правовые сигналы, резонансные инфоповоды. "
        "Если критичного нет — честно напиши, что особо острых моментов не выявлено.\n\n"
        "<b>🗂 Основные сюжеты</b>\n"
        "Маркированный список (•) главных новостных сюжетов, по одной строке: суть.\n\n"
        "Требования к разметке:\n"
        "- Только HTML-теги Telegram: <b>жирный</b>. НЕ используй markdown (* # _).\n"
        "- Будь сжатым, фактологичным, без воды.\n"
        "- Не выдумывай: опирайся только на текст документа."
    )
    user = "Вот текст документа для разбора:\n\n" + text

    r = requests.post(
        "https://api.deepseek.com/chat/completions",
        headers={
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": "deepseek-v4-flash",
            "max_tokens": 4096,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        },
        timeout=180,
    )
    if not r.ok:
        print(f"[error] DeepSeek {r.status_code}: {r.text}", file=sys.stderr)
        r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


# ---------------------------------------------------------------------------
# ОБРАБОТКА ОДНОГО ДОКУМЕНТА
# ---------------------------------------------------------------------------

def handle_document(chat_id, doc):
    name = doc.get("file_name", "файл")
    mime = doc.get("mime_type", "")
    size = doc.get("file_size", 0)

    is_pdf = mime == "application/pdf" or name.lower().endswith(".pdf")
    if not is_pdf:
        send_message(chat_id, f"Файл «{html.escape(name)}» не PDF — пропускаю. "
                              "Пришли PDF-файл для разбора.")
        return

    if size and size > MAX_PDF_BYTES:
        send_message(chat_id, "Файл больше 20 МБ — Telegram не даёт ботам "
                              "скачивать такие. Попробуй сжать PDF.")
        return

    send_chat_action(chat_id)
    data, info = download_file(doc["file_id"])
    if data is None:
        send_message(chat_id, f"Не удалось скачать файл: {html.escape(str(info))}")
        return

    try:
        text, n_pages = extract_pdf_text(data)
    except Exception as e:
        send_message(chat_id, f"Не смог прочитать PDF: {html.escape(str(e))}")
        return

    if len(text) < MIN_TEXT_CHARS:
        send_message(chat_id,
            f"📄 «{html.escape(name)}» ({n_pages} стр.): в файле почти нет "
            "извлекаемого текста — похоже, это скан (картинки). "
            "Распознавание сканов в этом боте не настроено, поэтому разобрать "
            "не могу. Если есть текстовая версия файла — пришли её.")
        return

    truncated = False
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]
        truncated = True

    send_chat_action(chat_id)
    try:
        result = analyze(text)
    except Exception as e:
        send_message(chat_id, f"Ошибка анализа: {html.escape(str(e))}")
        return

    header = f"<b>📄 Разбор: {html.escape(name)}</b> ({n_pages} стр.)\n\n"
    footer = ""
    if truncated:
        footer = ("\n\n<i>⚠️ Документ длинный — разобрана только первая часть "
                  "текста.</i>")
    send_message(chat_id, header + result + footer)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    print(">>> pdf_bot.py — опрос PDF <<<")
    offset = load_offset()
    print(f"[debug] стартовый offset: {offset}")

    updates = get_updates(offset)
    print(f"[debug] получено апдейтов: {len(updates)}")

    max_id = offset
    handled = 0
    for upd in updates:
        max_id = max(max_id, upd["update_id"])
        msg = upd.get("message")
        if not msg:
            continue
        chat_id = msg["chat"]["id"]

        # Фильтр по разрешённому пользователю
        if ALLOWED_CHAT_ID and str(chat_id) != ALLOWED_CHAT_ID:
            print(f"[skip] чужой chat_id {chat_id}")
            continue

        doc = msg.get("document")
        if doc:
            print(f"[info] документ: {doc.get('file_name')}")
            handle_document(chat_id, doc)
            handled += 1
        elif msg.get("text", "").startswith("/start"):
            send_message(chat_id,
                "Привет! Пришли мне PDF-файл (например, обзор СМИ), и я верну "
                "краткий пересказ, важные моменты и основные сюжеты.")

    # Сохраняем offset, даже если ничего не обрабатывали — чтобы подтвердить апдейты
    if max_id != offset:
        save_offset(max_id)
        print(f"[debug] новый offset сохранён: {max_id}")

    print(f"Обработано документов: {handled}")


if __name__ == "__main__":
    main()
