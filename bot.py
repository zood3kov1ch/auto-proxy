import asyncio
import sqlite3
import requests
from bs4 import BeautifulSoup
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from datetime import datetime
import re
import os
import time
import urllib.parse

# ============================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHANNEL_ID = "@zood3llotgk_proxy"
MAX_PROXIES_PER_RUN = 1

SOURCES = [
    "https://t.me/s/ProxyMTProto",
    "https://t.me/s/freedomvpnofficial",
    "https://t.me/s/TProxyRU",
    "https://t.me/s/ProxyFreeMTProto",
    "https://t.me/s/MTPproxy",
    "https://t.me/s/ProxyCatalog_bot",
    "https://t.me/ConfigiHapp",
    "https://t.me/s/ProxyFree_Ru",
    "https://t.me/free_vpn123456"
]
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "posted_proxies.db")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


def init_db():
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

def is_server_posted_today(server):
    conn = sqlite3.connect(DB_FILE)
    today = datetime.now().strftime('%Y-%m-%d')
    cur = conn.execute("SELECT 1 FROM posted WHERE server = ? AND posted_at LIKE ?", (server, f"{today}%"))
    result = cur.fetchone()
    conn.close()
    return result is not None
    conn = sqlite3.connect(DB_FILE)
    # Важно: скобка с запятой (proxy_id,)
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


def fetch_proxies_from_source(url):
    resp = None
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code == 200:
                break
        except Exception:
            time.sleep(2)

    if not resp or resp.status_code != 200:
        return []

    try:
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

        return proxies

    except Exception as e:
        print(f"[ОШИБКА ИСТОЧНИКА {url}] {e}", flush=True)
        return []


def fetch_all_proxies():
    all_found = []
    seen_ids = set()

    for source_url in SOURCES:
        channel_name = source_url.split("/")[-1]
        print(f"[ПАРСИНГ] Поиск в @{channel_name}...", flush=True)
        
        found = fetch_proxies_from_source(source_url)
        for p in found:
            if p["id"] not in seen_ids:
                seen_ids.add(p["id"])
                all_found.append(p)

    all_found.reverse()
    return all_found


def format_post(proxy):
    return (
        f"Server: <code>{proxy['server']}</code>\n"
        f"Port: <code>{proxy['port']}</code>\n"
        f"Secret: <code>{proxy['secret']}</code>\n"
        f"@zood3llotgk_proxy"
    )


def build_keyboard(proxy):
    connect_url = f"https://t.me/proxy?server={proxy['server']}&port={proxy['port']}&secret={proxy['secret']}"
    share_url = f"https://t.me/share/url?url={urllib.parse.quote(connect_url)}"

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
            parse_mode="HTML",
            disable_web_page_preview=True
        )
        print(f"[OK] Прокси отправлен в Telegram: {proxy['server']}:{proxy['port']}", flush=True)

    except Exception as e:
        print(f"[ОШИБКА ОТПРАВКИ] '{proxy['server']}': {e}", flush=True)
    finally:
        mark_posted(proxy["id"], proxy["server"], proxy["port"])
        await asyncio.sleep(5)


async def run_once(bot):
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Старт сканирования 7 каналов...", flush=True)
    proxies = fetch_all_proxies()
    print(f"Всего уникальных прокси найдено: {len(proxies)}", flush=True)

    posted_servers = set()
new_proxies = []
for p in proxies:
    key = f"{p['server']}:{p['port']}"
    if not is_posted(p["id"]) and key not in posted_servers:
        posted_servers.add(key)
        new_proxies.append(p)
    print(f"Новых прокси для публикации: {len(new_proxies)}", flush=True)

    for p in new_proxies[:MAX_PROXIES_PER_RUN]:
        await send_proxy(bot, p)

    if not new_proxies:
        print("Новых прокси во всех 7 каналах не найдено.", flush=True)


async def main():
    init_db()
    bot = Bot(token=BOT_TOKEN)
    await run_once(bot)


if __name__ == "__main__":
    asyncio.run(main())
