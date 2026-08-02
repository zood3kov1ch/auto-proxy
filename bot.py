import asyncio
import sqlite3
import requests
from bs4 import BeautifulSoup
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from datetime import datetime
import re
import os
import random
import urllib.parse

# ============================================================
BOT_TOKEN = "ВАШ_ТОКЕН_НОВОГО_БОТА"  # Вставьте сюда токен от @BotFather
CHANNEL_ID = "@zood3llotgk_proxy"
MAX_PROXIES_PER_RUN = 3
SOURCE_URL = "https://t.me/s/TProxyRU"
# ============================================================

# Настройка постоянного диска /data на Railway
DB_DIR = "/data"
if not os.path.exists(DB_DIR):
    try:
        os.makedirs(DB_DIR, exist_ok=True)
    except Exception:
        DB_DIR = "."

DB_FILE = os.path.join(DB_DIR, "posted_proxies.db")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


def init_db():
    print(f"[DB] Подключение к базе прокси: {os.path.abspath(DB_FILE)}", flush=True)
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS posted (
            id TEXT PRIMARY KEY,
            server TEXT,
            port TEXT,
            posted_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def is_posted(proxy_id):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.execute("SELECT 1 FROM posted WHERE id = ?", (proxy_id,))
    result = cur.fetchone()
    conn.close()
    return result is not None


def mark_posted(proxy_id, server, port):
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute(
            "INSERT OR IGNORE INTO posted (id, server, port, posted_at) VALUES (?, ?, ?, ?)",
            (proxy_id, server, port, datetime.now().isoformat())
        )
        conn.commit()
        conn.close()
        print(f"[DB] Прокси записан в базу: {server}:{port}", flush=True)
    except Exception as e:
        print(f"[DB ОШИБКА] Не удалось записать: {e}", flush=True)


def fetch_proxies():
    try:
        resp = requests.get(SOURCE_URL, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        proxies = []

        for msg in soup.select("div.tgme_widget_message"):
            text = msg.get_text()
            links = [a.get("href", "") for a in msg.select("a[href]")]

            proxy_link = None
            for link in links:
                if "proxy?" in link:
                    proxy_link = link
                    break

            if not proxy_link:
                match = re.search(r'(https://t\.me/proxy\?[^\s"<>]+|tg://proxy\?[^\s"<>]+)', text)
                if match:
                    proxy_link = match.group(1)

            if proxy_link:
                clean_link = proxy_link.replace("tg://proxy", "https://t.me/proxy")
                parsed = urllib.parse.urlparse(clean_link)
                params = urllib.parse.parse_qs(parsed.query)

                server = params.get("server", [""])[0]
                port = params.get("port", [""])[0]
                secret = params.get("secret", [""])[0]

                if server and port and secret:
                    proxy_id = f"{server}:{port}:{secret}"
                    proxies.append({
                        "id": proxy_id,
                        "server": server,
                        "port": port,
                        "secret": secret
                    })

        proxies.reverse()
        return proxies

    except Exception as e:
        print(f"[ПАРСИНГ ОШИБКА] {e}", flush=True)
        return []


def format_post(proxy):
    return (
        f"Server: {proxy['server']}\n"
        f"Port: {proxy['port']}\n"
        f"Secret: {proxy['secret']}\n"
        f"{CHANNEL_ID}"
    )


def build_keyboard(proxy):
    connect_url = f"https://t.me/proxy?server={proxy['server']}&port={proxy['port']}&secret={proxy['secret']}"
    share_url = f"https://t.me/share/url?url={urllib.parse.quote(connect_url)}"

    # Кнопки Подключиться и Поделиться в 1 ряд
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Подключиться", url=connect_url),
            InlineKeyboardButton("Поделиться", url=share_url)
        ]
    ])
    return keyboard


async def send_proxy(bot, proxy):
    text = format_post(proxy)
    reply_markup = build_keyboard(proxy)

    try:
        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=text,
            reply_markup=reply_markup,
            disable_web_page_preview=True
        )
        print(f"[OK] Прокси отправлен: {proxy['server']}:{proxy['port']}", flush=True)

    except Exception as e:
        print(f"[ОШИБКА ОТПРАВКИ] '{proxy['server']}': {e}", flush=True)
    finally:
        mark_posted(proxy["id"], proxy["server"], proxy["port"])
        await asyncio.sleep(15)


async def run_once(bot):
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Ищу новые прокси...", flush=True)
    proxies = fetch_proxies()
    print(f"Всего найдено в источнике: {len(proxies)}", flush=True)

    new_proxies = [p for p in proxies if not is_posted(p["id"])]
    print(f"Новых прокси для публикации: {len(new_proxies)}", flush=True)

    for p in new_proxies[:MAX_PROXIES_PER_RUN]:
        await send_proxy(bot, p)

    if not new_proxies:
        print("Все прокси уже были выложены ранее.", flush=True)


async def main():
    init_db()
    bot = Bot(token=BOT_TOKEN)
    print("=" * 50, flush=True)
    print("  TProxy -> Telegram Bot запущен!", flush=True)
    print(f"  Канал публикации: {CHANNEL_ID}", flush=True)
    print("  Интервал проверки: 1 – 1.5 часа", flush=True)
    print("=" * 50, flush=True)

    while True:
        await run_once(bot)
        wait_minutes = random.randint(60, 90)
        hours = round(wait_minutes / 60, 1)
        print(f"Следующая проверка через {wait_minutes} минут (~{hours} ч)...", flush=True)
        await asyncio.sleep(wait_minutes * 60)


if __name__ == "__main__":
    asyncio.run(main())
