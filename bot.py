import asyncio
import sqlite3
import requests
from bs4 import BeautifulSoup
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from datetime import datetime, timedelta
import re
import os
import time
import urllib.parse

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHANNEL_ID = "@zood3llotgk_proxy"
MAX_PROXIES_PER_RUN = 1

# Через сколько дней можно повторно опубликовать тот же proxy id
# (server:port:secret), если он снова встретился и всё ещё жив.
REPOST_AFTER_DAYS = 14

# Проверка живости прокси
CHECK_TIMEOUT = 5          # секунд на попытку TCP-коннекта
CHECK_CONCURRENCY = 20     # сколько прокси проверяем параллельно

# Поиск по интернету (не только по каналам) через Google Custom Search API.
# Нужно завести ключ и Search Engine ID (см. пояснение в конце файла).
# Если не задано — этот источник просто пропускается.
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
GOOGLE_CSE_ID = os.environ.get("GOOGLE_CSE_ID", "")
WEB_SEARCH_QUERIES = [
    'site:t.me "proxy?server="',
    'inurl:"tg://proxy?server="',
    '"t.me/proxy?server=" MTProto прокси',
]

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

# Список каналов-источников. Можно смело дописывать новые — чем больше
# каналов, тем ниже риск "пересохнуть". Дубли между источниками отсекаются
# автоматически по server:port:secret.
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
    from datetime import timezone
    msk = timezone(timedelta(hours=MSK_OFFSET))
    return datetime.now(msk)


def is_scheduled_time():
    """Проверяем что сейчас время из графика (±10 минут допуск)"""
    now = get_msk_time()
    for (h, m) in SCHEDULE:
        scheduled = now.replace(hour=h, minute=m, second=0, microsecond=0)
        diff = abs((now - scheduled).total_seconds())
        if diff <= 600:  # 10 минут допуск
            return True
    return False


# ---------------------------------------------------------------------------
# База данных
# ---------------------------------------------------------------------------

def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS posted (
            id TEXT PRIMARY KEY,       -- server:port:secret (полный, а не только server:port)
            server TEXT,
            port TEXT,
            secret TEXT,
            posted_at TEXT,
            last_seen TEXT
        )
    """)
    conn.commit()
    conn.close()


def get_posted_record(proxy_id):
    """Возвращает posted_at (datetime) для данного id, либо None."""
    conn = sqlite3.connect(DB_FILE)
    cur = conn.execute("SELECT posted_at FROM posted WHERE id = ?", (proxy_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    try:
        return datetime.fromisoformat(row[0])
    except Exception:
        return None


def should_skip(proxy_id):
    """
    True, если этот конкретный proxy_id (server:port:secret) уже публиковался
    недавно (< REPOST_AFTER_DAYS назад). Если secret у сервера сменился —
    id будет другим, и это больше НЕ считается дублем.
    Если тот же id "протух" по TTL — разрешаем опубликовать заново.
    """
    posted_at = get_posted_record(proxy_id)
    if posted_at is None:
        return False
    return datetime.now() - posted_at < timedelta(days=REPOST_AFTER_DAYS)


def mark_posted(proxy_id, server, port, secret):
    try:
        conn = sqlite3.connect(DB_FILE)
        now_iso = datetime.now().isoformat()
        conn.execute("""
            INSERT INTO posted (id, server, port, secret, posted_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET posted_at = excluded.posted_at,
                                           last_seen = excluded.last_seen
        """, (proxy_id, server, port, secret, now_iso, now_iso))
        conn.commit()
        conn.close()
        print(f"[DB] Записан: {server}:{port}", flush=True)
    except Exception as e:
        print(f"[DB ОШИБКА] {e}", flush=True)


def cleanup_old_records(older_than_days=90):
    """Опциональная чистка очень старых записей, чтобы база не росла бесконечно."""
    try:
        conn = sqlite3.connect(DB_FILE)
        cutoff = (datetime.now() - timedelta(days=older_than_days)).isoformat()
        conn.execute("DELETE FROM posted WHERE posted_at < ?", (cutoff,))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DB CLEANUP ОШИБКА] {e}", flush=True)


# ---------------------------------------------------------------------------
# Парсинг Telegram-каналов
# ---------------------------------------------------------------------------

def extract_proxy_from_text(text, links):
    proxy_link = None
    for link in links:
        if "proxy?" in link:
            proxy_link = link
            break
    if not proxy_link:
        match = re.search(r'(https://t\.me/proxy\?[^\s"<>]+|tg://proxy\?[^\s"<>]+)', text)
        if match:
            proxy_link = match.group(1)
    if not proxy_link:
        return None

    clean_link = proxy_link.replace("tg://proxy", "https://t.me/proxy")
    parsed = urllib.parse.urlparse(clean_link)
    params = urllib.parse.parse_qs(parsed.query)
    server = params.get("server", [""])[0]
    port = params.get("port", [""])[0]
    secret = params.get("secret", [""])[0]
    if server and port and secret:
        return {
            "id": f"{server}:{port}:{secret}",
            "server": server,
            "port": port,
            "secret": secret,
        }
    return None


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
            p = extract_proxy_from_text(text, links)
            if p:
                proxies.append(p)
        return proxies
    except Exception as e:
        print(f"[ОШИБКА {url}] {e}", flush=True)
        return []


# ---------------------------------------------------------------------------
# Поиск по интернету (не только по заданным каналам)
# ---------------------------------------------------------------------------

def fetch_proxies_from_web_search():
    """
    Ищет ссылки вида t.me/proxy?server=... по всему интернету через
    Google Programmable Search (Custom Search JSON API), а не только
    в списке SOURCES. Требует GOOGLE_API_KEY и GOOGLE_CSE_ID.

    Это НЕ полнотекстовый поиск по всем каналам Telegram — у самого
    Telegram нет публичного API для поиска по всем каналам сразу.
    Это поиск по тому, что уже проиндексировали поисковики (посты,
    репосты, форумы, сайты-агрегаторы, где встречаются такие ссылки).
    """
    if not GOOGLE_API_KEY or not GOOGLE_CSE_ID:
        return []

    found = []
    for query in WEB_SEARCH_QUERIES:
        try:
            resp = requests.get(
                "https://www.googleapis.com/customsearch/v1",
                params={
                    "key": GOOGLE_API_KEY,
                    "cx": GOOGLE_CSE_ID,
                    "q": query,
                    "num": 10,
                },
                timeout=15,
            )
            if resp.status_code != 200:
                continue
            data = resp.json()
            for item in data.get("items", []):
                blob = " ".join([
                    item.get("link", ""),
                    item.get("title", ""),
                    item.get("snippet", ""),
                ])
                p = extract_proxy_from_text(blob, [item.get("link", "")])
                if p:
                    found.append(p)
        except Exception as e:
            print(f"[WEB SEARCH ОШИБКА] {query}: {e}", flush=True)
    return found


def fetch_all_proxies():
    all_found = []
    seen_ids = set()

    for source_url in SOURCES:
        channel_name = source_url.split("/")[-1]
        print(f"[ПАРСИНГ] @{channel_name}...", flush=True)
        for p in fetch_proxies_from_source(source_url):
            if p["id"] not in seen_ids:
                seen_ids.add(p["id"])
                all_found.append(p)

    print("[ПАРСИНГ] web search...", flush=True)
    for p in fetch_proxies_from_web_search():
        if p["id"] not in seen_ids:
            seen_ids.add(p["id"])
            all_found.append(p)

    all_found.reverse()
    return all_found


# ---------------------------------------------------------------------------
# Проверка живости прокси
# ---------------------------------------------------------------------------

async def check_proxy_alive(server, port, timeout=CHECK_TIMEOUT):
    """
    Простая проверка: открывается ли TCP-соединение на server:port.
    Это не полноценная проверка MTProto-хендшейка (для неё нужно слать
    правильный секрет и разбирать ответ), но она отсекает большинство
    мёртвых/недоступных прокси — сервер выключен, порт закрыт, домен не
    резолвится и т.д.
    """
    try:
        port_int = int(port)
    except ValueError:
        return False
    try:
        conn = asyncio.open_connection(server, port_int)
        reader, writer = await asyncio.wait_for(conn, timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


async def filter_alive_proxies(proxies):
    sem = asyncio.Semaphore(CHECK_CONCURRENCY)
    alive = []

    async def check(p):
        async with sem:
            ok = await check_proxy_alive(p["server"], p["port"])
            if ok:
                alive.append(p)

    await asyncio.gather(*(check(p) for p in proxies))
    return alive


# ---------------------------------------------------------------------------
# Публикация в канал
# ---------------------------------------------------------------------------

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
        mark_posted(proxy["id"], proxy["server"], proxy["port"], proxy["secret"])
        await asyncio.sleep(5)


async def run_once(bot):
    now = get_msk_time()
    print(f"\n[{now.strftime('%H:%M:%S')} МСК] Старт...", flush=True)

    if not is_scheduled_time():
        print(f"[ПРОПУСК] Сейчас не время по графику. МСК: {now.strftime('%H:%M')}", flush=True)
        return

    proxies = fetch_all_proxies()
    print(f"Найдено уникальных: {len(proxies)}", flush=True)

    candidates = [p for p in proxies if not should_skip(p["id"])]
    print(f"Кандидатов (с учётом TTL): {len(candidates)}", flush=True)

    if not candidates:
        print("Новых прокси нет.", flush=True)
        return

    print("[ПРОВЕРКА] Тестируем доступность прокси...", flush=True)
    alive = await filter_alive_proxies(candidates)
    print(f"Живых прокси: {len(alive)} из {len(candidates)}", flush=True)

    if not alive:
        print("Живых новых прокси нет.", flush=True)
        return

    for p in alive[:MAX_PROXIES_PER_RUN]:
        await send_proxy(bot, p)


async def main():
    init_db()
    cleanup_old_records()
    bot = Bot(token=BOT_TOKEN)
    await run_once(bot)


if __name__ == "__main__":
    asyncio.run(main())
