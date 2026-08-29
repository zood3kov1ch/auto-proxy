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

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHANNEL_ID = "@zood3llotgk_proxy"
MAX_PROXIES_PER_RUN = 1

# --- Гарантированный резервный прокси (fallback) ---
# Если за последние GUARANTEED_FALLBACK_HOURS часов в канал не улетело
# ни одного прокси (например, все источники исчерпаны/недоступны),
# бот один раз постит этот резервный прокси, чтобы канал не "молчал".
GUARANTEED_PROXY_SERVER = os.environ.get("GUARANTEED_PROXY_SERVER", "")
GUARANTEED_PROXY_PORT = os.environ.get("GUARANTEED_PROXY_PORT", "")
GUARANTEED_PROXY_SECRET = os.environ.get("GUARANTEED_PROXY_SECRET", "")
GUARANTEED_FALLBACK_HOURS = 20

# Событие, которым был запущен workflow (schedule / workflow_dispatch).
# При ручном запуске игнорируем проверку расписания.
GITHUB_EVENT_NAME = os.environ.get("GITHUB_EVENT_NAME", "")

# График по МСК: с 12:00 до 22:00, каждые 30 минут
# Формат: (час, минута) по МСК
SCHEDULE = [
    (12, 0), (12, 30),
    (13, 0), (13, 30),
    (14, 0), (14, 30),
    (15, 0), (15, 30),
    (16, 0), (16, 30),
    (17, 0), (17, 30),
    (18, 0), (18, 30),
    (19, 0), (19, 30),
    (20, 0), (20, 30),
    (21, 0), (21, 30),
    (22, 0),
]

SOURCES = [
    "https://t.me/s/ProxyMTProto",
    "https://t.me/s/freedomvpnofficial",
    "https://t.me/s/TProxyRU",
    "https://t.me/s/ProxyFreeMTProto",
    "https://t.me/s/MTPproxy",
    "https://t.me/s/ProxyCatalog_bot",
    "https://t.me/s/ProxyFree_Ru",
]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "posted_proxies.db")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

MSK_OFFSET = 3  # UTC+3


def get_msk_time():
    from datetime import timezone, timedelta
    msk = timezone(timedelta(hours=MSK_OFFSET))
    return datetime.now(msk)


def is_scheduled_time():
    """Проверяем что сейчас время из графика (±10 минут допуск)."""
    # Ручной запуск (workflow_dispatch) всегда пропускаем через график —
    # иначе тестовый прогон вне получасового окна просто ничего не сделает.
    if GITHUB_EVENT_NAME == "workflow_dispatch":
        return True

    now = get_msk_time()
    for (h, m) in SCHEDULE:
        scheduled = now.replace(hour=h, minute=m, second=0, microsecond=0)
        diff = abs((now - scheduled).total_seconds())
        if diff <= 600:  # 10 минут допуск
            return True
    return False


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


def is_server_posted(server, port):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.execute("SELECT 1 FROM posted WHERE server = ? AND port = ?", (server, port))
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
        print(f"[DB] Записан: {server}:{port}", flush=True)
    except Exception as e:
        print(f"[DB ОШИБКА] {e}", flush=True)


def hours_since_last_post():
    """Сколько часов прошло с последнего успешного поста (любого прокси).
    Если постов ещё не было — считаем, что прошло "бесконечно много"."""
    conn = sqlite3.connect(DB_FILE)
    cur = conn.execute("SELECT posted_at FROM posted ORDER BY posted_at DESC LIMIT 1")
    row = cur.fetchone()
    conn.close()
    if not row:
        return float("inf")
    try:
        last = datetime.fromisoformat(row[0])
    except ValueError:
        return float("inf")
    return (datetime.now() - last).total_seconds() / 3600


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
        print(f"[ИСТОЧНИК НЕДОСТУПЕН] {url} (status={resp.status_code if resp else 'нет ответа'})", flush=True)
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
        print(f"[ОШИБКА {url}] {e}", flush=True)
        return []


def fetch_all_proxies():
    all_found = []
    seen_keys = set()
    for source_url in SOURCES:
        channel_name = source_url.split("/")[-1]
        print(f"[ПАРСИНГ] @{channel_name}...", flush=True)
        found = fetch_proxies_from_source(source_url)
        print(f"[РЕЗУЛЬТАТ] @{channel_name}: найдено {len(found)}", flush=True)
        for p in found:
            key = f"{p['server']}:{p['port']}"
            if key not in seen_keys:
                seen_keys.add(key)
                all_found.append(p)
    all_found.reverse()
    return all_found


def get_guaranteed_proxy():
    if GUARANTEED_PROXY_SERVER and GUARANTEED_PROXY_PORT and GUARANTEED_PROXY_SECRET:
        return {
            "id": f"{GUARANTEED_PROXY_SERVER}:{GUARANTEED_PROXY_PORT}:{GUARANTEED_PROXY_SECRET}",
            "server": GUARANTEED_PROXY_SERVER,
            "port": GUARANTEED_PROXY_PORT,
            "secret": GUARANTEED_PROXY_SECRET,
        }
    return None


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
        print(f"[OK] Отправлен: {proxy['server']}:{proxy['port']}", flush=True)
    except Exception as e:
        print(f"[ОШИБКА ОТПРАВКИ] {e}", flush=True)
    finally:
        mark_posted(proxy["id"], proxy["server"], proxy["port"])
        await asyncio.sleep(5)


async def run_once(bot):
    now = get_msk_time()
    print(f"\n[{now.strftime('%H:%M:%S')} МСК] Старт... (event={GITHUB_EVENT_NAME or 'schedule'})", flush=True)

    if not is_scheduled_time():
        print(f"[ПРОПУСК] Сейчас не время по графику. МСК: {now.strftime('%H:%M')}", flush=True)
        return

    proxies = fetch_all_proxies()
    print(f"Найдено уникальных: {len(proxies)}", flush=True)

    new_proxies = [p for p in proxies if not is_server_posted(p["server"], p["port"])]
    print(f"Новых для публикации: {len(new_proxies)}", flush=True)

    for p in new_proxies[:MAX_PROXIES_PER_RUN]:
        await send_proxy(bot, p)

    if not new_proxies:
        idle_hours = hours_since_last_post()
        print(f"Новых прокси нет. Часов с последнего поста: {idle_hours:.1f}", flush=True)
        if idle_hours >= GUARANTEED_FALLBACK_HOURS:
            fallback = get_guaranteed_proxy()
            if fallback:
                print("[FALLBACK] Публикуем гарантированный резервный прокси", flush=True)
                # Гарантированный прокси может быть уже отмечен как "posted" —
                # это ок, нам важно только реально отправить его в канал.
                await send_proxy(bot, fallback)
            else:
                print("[FALLBACK] Резервный прокси не настроен (нет секретов GUARANTEED_PROXY_*)", flush=True)


async def main():
    init_db()
    bot = Bot(token=BOT_TOKEN)
    await run_once(bot)


if __name__ == "__main__":
    asyncio.run(main())
