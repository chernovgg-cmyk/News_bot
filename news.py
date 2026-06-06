#!/usr/bin/env python3
"""
Утренний дайджест новостей.
Собирает новости за прошедшие сутки из RSS-лент по заданным темам,
саммаризирует через Claude API и отправляет в Telegram.
"""

import os
import sys
import time
import html
from datetime import datetime, timezone, timedelta

import feedparser
import requests

# ---------------------------------------------------------------------------
# КОНФИГУРАЦИЯ
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# Окно: новости за последние 24 часа (с небольшим запасом)
HOURS_BACK = 26
MAX_ITEMS_PER_TOPIC = 12   # сколько свежих новостей максимум брать на тему
MAX_TOTAL_ITEMS = 60       # общий потолок, чтобы не раздувать запрос к LLM

# Темы и их RSS-источники.
# Каждая тема — список лент. Можно добавлять/убирать свободно.
TOPICS = {
    "Искусственный интеллект": [
        "https://habr.com/ru/rss/hub/artificial_intelligence/all/?fl=ru",
        "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
        "https://techcrunch.com/category/artificial-intelligence/feed/",
        "https://venturebeat.com/category/ai/feed/",
    ],
    "Энергетика": [
        "https://www.bigpowernews.ru/rss/",
        "https://peretok.ru/rss/",
        "https://www.eprussia.ru/rss/news.xml",
    ],
    "Т Плюс / Энергосбыт Плюс": [
        # Общие энергоотраслевые ленты — фильтруем по ключевым словам ниже
        "https://www.bigpowernews.ru/rss/",
        "https://peretok.ru/rss/",
        "https://www.eprussia.ru/rss/news.xml",
    ],
    "Интеллектуальные системы учёта (ИСУ)": [
        "https://www.bigpowernews.ru/rss/",
        "https://www.eprussia.ru/rss/news.xml",
        "https://habr.com/ru/rss/hub/iot/all/?fl=ru",
    ],
    "Информационная безопасность": [
        "https://habr.com/ru/rss/hub/infosecurity/all/?fl=ru",
        "https://www.securitylab.ru/_services/export/rss/",
        "https://thehackernews.com/feeds/posts/default",
        "https://feeds.feedburner.com/TheHackersNews",
    ],
    "Новинки в ИТ": [
        "https://habr.com/ru/rss/best/daily/?fl=ru",
        "https://3dnews.ru/news/rss/",
        "https://www.cnews.ru/inc/rss/news.xml",
    ],
    "Новости правительства РФ": [
        "http://government.ru/all/rss/",
        "https://tass.ru/rss/v2.xml",
    ],
}

# Ключевые слова для тем, где нужен фильтр по релевантности
TOPIC_KEYWORDS = {
    "Т Плюс / Энергосбыт Плюс": [
        "т плюс", "т-плюс", "энергосбыт плюс", "энергосбыт+", "тплюс",
        "вексельберг", "ренова",
    ],
    "Интеллектуальные системы учёта (ИСУ)": [
        "интеллектуальн", "умный счётчик", "умный счетчик", "учёт электро",
        "учет электро", "ису", "приборы учёта", "приборы учета", "смарт-метр",
        "smart meter", "ace учёт",
    ],
}

# ---------------------------------------------------------------------------
# СБОР НОВОСТЕЙ
# ---------------------------------------------------------------------------

def parse_entry_time(entry):
    """Вернуть datetime записи в UTC, либо None."""
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def clean_text(raw):
    """Убрать html-теги и лишние пробелы из summary."""
    if not raw:
        return ""
    # грубое удаление тегов
    import re
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def matches_keywords(entry, keywords):
    haystack = (entry.get("title", "") + " " +
                clean_text(entry.get("summary", ""))).lower()
    return any(kw in haystack for kw in keywords)


def collect():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS_BACK)
    seen_links = set()
    results = {}

    for topic, feeds in TOPICS.items():
        keywords = TOPIC_KEYWORDS.get(topic)
        items = []
        for url in feeds:
            try:
                feed = feedparser.parse(url)
            except Exception as e:
                print(f"[warn] не удалось разобрать {url}: {e}", file=sys.stderr)
                continue
            for entry in feed.entries:
                ts = parse_entry_time(entry)
                # если у ленты нет даты — пропускаем (не можем гарантировать свежесть)
                if ts is None or ts < cutoff:
                    continue
                link = entry.get("link", "")
                if not link or link in seen_links:
                    continue
                if keywords and not matches_keywords(entry, keywords):
                    continue
                title = clean_text(entry.get("title", "")) or "(без заголовка)"
                summary = clean_text(entry.get("summary", ""))[:400]
                items.append({
                    "title": title,
                    "link": link,
                    "summary": summary,
                    "ts": ts,
                })
                seen_links.add(link)
            time.sleep(0.3)  # вежливость к серверам

        items.sort(key=lambda x: x["ts"], reverse=True)
        items = items[:MAX_ITEMS_PER_TOPIC]
        if items:
            results[topic] = items

    return results


# ---------------------------------------------------------------------------
# САММАРИЗАЦИЯ ЧЕРЕЗ CLAUDE
# ---------------------------------------------------------------------------

def build_prompt(results):
    lines = []
    total = 0
    for topic, items in results.items():
        lines.append(f"\n## ТЕМА: {topic}")
        for it in items:
            if total >= MAX_TOTAL_ITEMS:
                break
            lines.append(f"- Заголовок: {it['title']}")
            if it["summary"]:
                lines.append(f"  Краткое содержание источника: {it['summary']}")
            lines.append(f"  Ссылка: {it['link']}")
            total += 1
    return "\n".join(lines)


def summarize(results):
    raw_block = build_prompt(results)

    system = (
        "Ты — редактор утреннего новостного дайджеста. "
        "Тебе дают сырые новости за последние сутки, сгруппированные по темам. "
        "Сделай сжатый, информативный дайджест на русском языке.\n\n"
        "Требования:\n"
        "- Группируй по темам в том же порядке.\n"
        "- В каждой теме — маркированный список из 2-6 самых важных пунктов.\n"
        "- Каждый пункт: одно-два предложения сути + ссылка в конце в формате <a href=\"URL\">источник</a>.\n"
        "- Объединяй дубли об одном событии.\n"
        "- Если по теме нет ничего значимого — пропусти её.\n"
        "- Используй HTML-разметку Telegram: <b>жирный</b>, <a href>ссылки</a>. "
        "Не используй markdown, не используй заголовки # — для названий тем используй <b>.\n"
        "- Будь нейтрален и фактологичен."
    )

    user = (
        "Вот сырые новости за прошедшие сутки. Составь дайджест.\n"
        + raw_block
    )

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 4096,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        timeout=120,
    )
    if not resp.ok:
        # Печатаем тело ответа — там Anthropic пишет точную причину ошибки
        print(f"[error] Anthropic API вернул {resp.status_code}:", file=sys.stderr)
        print(resp.text, file=sys.stderr)
        resp.raise_for_status()
    data = resp.json()
    parts = [b["text"] for b in data["content"] if b.get("type") == "text"]
    return "\n".join(parts).strip()


# ---------------------------------------------------------------------------
# ОТПРАВКА В TELEGRAM
# ---------------------------------------------------------------------------

def split_message(text, limit=4000):
    """Telegram режет сообщения на 4096 символов. Бьём по абзацам."""
    chunks = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = ""
        current += line + "\n"
    if current.strip():
        chunks.append(current)
    return chunks


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for chunk in split_message(text):
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=30)
        if not r.ok:
            print(f"[error] Telegram ответил {r.status_code}: {r.text}",
                  file=sys.stderr)
            r.raise_for_status()
        time.sleep(0.5)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    print(">>> news.py версия 2 (с выводом тела ошибки) <<<")
    # Санити-проверка ключа: печатаем только длину и первые/последние символы
    k = ANTHROPIC_API_KEY
    print(f"[debug] длина API-ключа: {len(k)}; начинается на: {k[:8]}; "
          f"заканчивается на: {k[-4:]}")
    today = datetime.now(timezone.utc).astimezone(
        timezone(timedelta(hours=3))  # МСК
    ).strftime("%d.%m.%Y")

    print("Собираю новости...")
    results = collect()
    count = sum(len(v) for v in results.values())
    print(f"Найдено {count} новостей по {len(results)} темам.")

    if count == 0:
        send_telegram(f"<b>📰 Дайджест за {today}</b>\n\n"
                      "За прошедшие сутки значимых новостей по вашим темам "
                      "в отслеживаемых источниках не найдено.")
        return

    print("Саммаризирую через Claude...")
    digest = summarize(results)

    header = f"<b>📰 Утренний дайджест — {today}</b>\n\n"
    send_telegram(header + digest)
    print("Отправлено.")


if __name__ == "__main__":
    main()
