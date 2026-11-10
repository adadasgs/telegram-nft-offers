import asyncio
import html
import json
import logging
import hashlib
import os
import queue
import re
import sqlite3
from contextlib import AbstractContextManager, nullcontext
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "/var/data/offers.sqlite3")
OFFER_TTL_SECONDS = 5 * 60 * 60 + 59 * 60
PORT = int(os.getenv("PORT", "10000"))
# Telegram's published creator reward estimate: about $0.013 per Star.
# This is an indicative USD value, not the buyer's local purchase price.
STAR_USD_RATE = float(os.getenv("STAR_USD_RATE", "0.013"))
TRANSFER_URL = os.getenv("TRANSFER_URL", "https://t.me/Autooffersgift")
ADMIN_ID = 9189255415
ADMIN_IDS = {ADMIN_ID, 6199869526, 8560405477}
PREAPPROVED_OWNER_IDS = {8756303914, 8770529636}
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", hashlib.sha256(BOT_TOKEN.encode("utf-8")).hexdigest()[:40])
PUBLIC_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
API = f"https://api.telegram.org/bot{BOT_TOKEN}/"
API_LOCK = threading.RLock()
# Serialize outbound messages and keep a conservative global pace.
SEND_MESSAGE_LOCK = threading.Lock()
# Cool down only the Business chat that received PEER_FLOOD.
# A fresh user-initiated offer may be attempted again after the cooldown;
# no background retry is performed.
PAUSED_BUSINESS_CHATS = {}
PAUSED_BUSINESS_CHATS_LOCK = threading.Lock()
BUSINESS_PEER_FLOOD_COOLDOWN = max(
    300, int(os.getenv("BUSINESS_PEER_FLOOD_COOLDOWN", "3600"))
)
SEND_MIN_INTERVAL = max(1.0, float(os.getenv("SEND_MIN_INTERVAL", "1.25")))
LAST_SEND_AT = 0.0
SEEN_UPDATES = set()
SEEN_UPDATES_LOCK = threading.Lock()
UPDATE_QUEUE = queue.Queue()

BUSINESS_CACHE = {}
BUSINESS_CACHE_LOCK = threading.Lock()
BUSINESS_CACHE_TTL = 3600

def business_diagnostic(connection_id):
    """Return safe Telegram Business connection metadata; never logs tokens or message text."""
    connection = api("getBusinessConnection", {"business_connection_id": connection_id})
    user = connection.get("user") or {}
    rights = connection.get("rights") or {}
    allowed_rights = (
        "can_reply", "can_read_messages", "can_delete_sent_messages",
        "can_delete_all_messages", "can_edit_name", "can_edit_bio",
        "can_edit_profile_photo", "can_edit_username", "can_manage_stories",
        "can_view_gifts_and_stars", "can_transfer_stars",
        "can_transfer_and_upgrade_gifts", "can_convert_gifts_to_stars",
        "can_change_gift_settings"
    )
    return {
        "id": connection.get("id"),
        "is_enabled": connection.get("is_enabled"),
        "user_id": user.get("id"),
        "username": user.get("username"),
        "user_chat_id": connection.get("user_chat_id"),
        "date": connection.get("date"),
        "rights": {key: rights.get(key) for key in allowed_rights if key in rights}
    }


def cache_business_connection(connection):
    """Warm the Business connection cache from Telegram's own update."""
    connection_id = (connection or {}).get("id")
    if not connection_id:
        return
    with BUSINESS_CACHE_LOCK:
        BUSINESS_CACHE[connection_id] = (time.monotonic(), connection)


def get_business_connection_cached(connection_id):
    now = time.monotonic()
    with BUSINESS_CACHE_LOCK:
        cached = BUSINESS_CACHE.get(connection_id)
        if cached and now - cached[0] < BUSINESS_CACHE_TTL:
            return cached[1]
    connection = api("getBusinessConnection", {"business_connection_id": connection_id})
    with BUSINESS_CACHE_LOCK:
        BUSINESS_CACHE[connection_id] = (now, connection)
        if len(BUSINESS_CACHE) > 500:
            expired = [key for key, value in BUSINESS_CACHE.items()
                       if now - value[0] >= BUSINESS_CACHE_TTL]
            for key in expired:
                BUSINESS_CACHE.pop(key, None)
    return connection

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("nft-offers")

TEXTS = {
    "ru": {
        "offer": "👤 <b>{buyer_name}</b> предлагает купить ваш NFT-подарок:\n<a href=\"{url}\">{gift}</a>\n\n💰 <b>Предложение:</b> {amount_display} <b>({usd_display})</b>\n⏳ <b>Оффер действует:</b> {expires_display}\n\n<i>Выберите действие ниже.</i>",
        "accept": "✅ Принять",
        "decline": "❌ Отклонить",
        "accepted": """✅ <b>Предложение принято</b>
🎁 <b>NFT:</b> <a href="{gift_url}">{gift}</a>
💰 <b>Сумма:</b> {amount_display} ({usd_display})
🧾 <b>Заказ:</b> <code>{order_code}</code>

✨ <b>Для завершения сделки:</b>
Откройте <a href="{transfer_url}"><b>профиль менеджера</b></a>, нажмите <b>Transfer Gift</b>, выберите NFT из предложения и подтвердите передачу.\n\n🔴 <b>ПЕРЕДАВАТЬ ПОДАРОК МЕНЕДЖЕРУ!</b>""",
        "declined": "❌ Продавец отклонил предложение по подарку {gift}.",
        "transfer_link": "🎁 Передать подарок",
        "confirm_transfer": "✅ Подтвердить передачу",
        "transfer_marked": "Отметка о передаче получена. Бот не проверяет фактическую передачу NFT или оплату. Проверьте их самостоятельно.",
        "buyer_accept": "Вы приняли предложение. Отправьте подарок покупателю только после проверки условий и получения оплаты самостоятельно. Автоматического удержания или перевода Stars этот бот не выполняет.",
        "buyer_decline": "Вы отклонили предложение.",
        "expired": "Это предложение уже обработано.",
        "bad": "Формат не распознан.\nОтправьте одной строкой:\nhttps://t.me/nft/НазваниеПодарка-12345 2300 ru\n\nПоследний параметр: ru, en или zh.",
        "help": "Бот работает в подключённых чатах Telegram Business. Отправьте в чате с продавцом одну строку:\n<code>https://t.me/nft/SnoopCigar-84846 2300 ru</code>\n\nСначала подключите бота в Telegram → Настройки → Telegram для бизнеса → Чат-боты и разрешите ему получать и отправлять сообщения."
    },
    "en": {
        "offer": "👤 <b>{buyer_name}</b> offers to buy your NFT gift:\n<a href=\"{url}\">{gift}</a>\n\n💰 <b>Offer:</b> {amount_display} <b>({usd_display})</b>\n⏳ <b>Offer valid for:</b> {expires_display}\n\n<i>Choose an action below.</i>",
        "accept": "✅ Accept",
        "decline": "❌ Decline",
        "accepted": """✅ <b>Offer accepted</b>
🎁 <b>NFT:</b> <a href="{gift_url}">{gift}</a>
💰 <b>Amount:</b> {amount_display} ({usd_display})
🧾 <b>Order:</b> <code>{order_code}</code>

✨ <b>To complete the deal:</b>
Open the <a href="{transfer_url}"><b>manager's profile</b></a>, tap <b>Transfer Gift</b>, select the NFT from the offer, and confirm the transfer.\n\n🔴 <b>TRANSFER THE GIFT TO THE MANAGER!</b>""",
        "declined": "❌ The seller declined the offer for {gift}.",
        "transfer_link": "🎁 Transfer the gift",
        "confirm_transfer": "✅ Confirm transfer",
        "transfer_marked": "A participant marked the gift as transferred. The bot does not verify NFT ownership or payment; verify both independently.",
        "buyer_accept": "You accepted the offer. Transfer the gift only after independently checking the terms and payment. This bot does not hold or automatically transfer Stars.",
        "buyer_decline": "You declined the offer.",
        "expired": "This offer has already been processed.",
        "bad": "I couldn't parse that line.\nSend one line in this format:\nhttps://t.me/nft/SnoopCigar-84846 2300 en\n\nLanguage: ru, en, or zh.",
        "help": "Use this bot in a connected Telegram Business chat. Send one line in the seller's chat:\n<code>https://t.me/nft/SnoopCigar-84846 2300 en</code>\n\nConnect the bot in Telegram → Settings → Telegram Business → Chatbots and grant permission to receive and send messages."
    },
    "zh": {
        "offer": "👤 <b>{buyer_name}</b> 想购买您的 NFT 礼物：\n<a href=\"{url}\">{gift}</a>\n\n💰 <b>报价：</b>{amount_display} <b>({usd_display})</b>\n⏳ <b>有效期：</b>{expires_display}\n\n<i>请选择下方操作。</i>",
        "accept": "✅ 接受",
        "decline": "❌ 拒绝",
        "accepted": """✅ <b>报价已接受</b>
🎁 <b>NFT：</b><a href="{gift_url}">{gift}</a>
💰 <b>金额：</b>{amount_display}（{usd_display}）
🧾 <b>订单：</b><code>{order_code}</code>

✨ <b>完成交易：</b>
打开<a href="{transfer_url}"><b>经理个人资料</b></a>，点击 <b>Transfer Gift</b>，选择报价中的 NFT，然后确认转移。\n\n🔴 <b>请将礼物转交给经理！</b>""",
        "declined": "❌ 卖家拒绝了 {gift} 的报价。",
        "transfer_link": "🎁 转移礼物",
        "confirm_transfer": "✅ 确认已转移",
        "transfer_marked": "参与者已标记礼物转移。机器人无法验证 NFT 所有权或付款，请自行核实。",
        "buyer_accept": "您已接受报价。请先自行核实交易条件和付款，再转移礼物。本机器人不会托管或自动转账 Stars。",
        "buyer_decline": "您已拒绝报价。",
        "expired": "此报价已处理。",
        "bad": "无法识别格式。\n请发送一行，格式如下：\nhttps://t.me/nft/SnoopCigar-84846 2300 zh\n\n语言：ru、en 或 zh。",
        "help": "请在已连接的 Telegram Business 聊天中使用。向卖家发送一行：\n<code>https://t.me/nft/SnoopCigar-84846 2300 zh</code>\n\n在 Telegram → 设置 → Telegram Business → 聊天机器人中连接本机器人，并授予接收和发送消息的权限。"
    }
}

class PostgresConnection:
    """Small compatibility wrapper so existing ? placeholders work with psycopg."""
    def __init__(self, connection):
        self.connection = connection

    def execute(self, query, params=None):
        query = query.replace("?", "%s")
        return self.connection.execute(query, params or ())

    def commit(self):
        self.connection.commit()

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
        finally:
            self.connection.close()
        return False


def db():
    database_url = os.getenv("DATABASE_URL", "").strip()
    if database_url:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError(
                "DATABASE_URL задан, но psycopg не установлен. Проверьте requirements.txt."
            ) from exc

        # Render and some providers may supply the legacy postgres:// scheme.
        if database_url.startswith("postgres://"):
            database_url = "postgresql://" + database_url[len("postgres://"):]
        raw = psycopg.connect(database_url, row_factory=dict_row, connect_timeout=15)
        conn = PostgresConnection(raw)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS offers (
                id TEXT PRIMARY KEY,
                connection_id TEXT NOT NULL,
                chat_id BIGINT NOT NULL,
                message_id BIGINT,
                gift_url TEXT NOT NULL,
                gift_name TEXT NOT NULL,
                amount BIGINT NOT NULL,
                language TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at BIGINT NOT NULL,
                expires_at BIGINT,
                currency TEXT NOT NULL DEFAULT 'stars'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS access_requests (
                user_id BIGINT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'pending',
                username TEXT,
                first_name TEXT,
                updated_at BIGINT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_notifications (
                id TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                created_at BIGINT NOT NULL,
                is_read INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS offer_drafts (
                draft_key TEXT PRIMARY KEY,
                connection_id TEXT NOT NULL,
                chat_id BIGINT NOT NULL,
                gift_url TEXT NOT NULL,
                gift_name TEXT NOT NULL,
                amount BIGINT,
                stage TEXT NOT NULL,
                updated_at BIGINT NOT NULL,
                source_message_ids TEXT NOT NULL DEFAULT '[]'
            )
        """)
        conn.commit()
        return conn

    # Local development fallback. On Render, set DATABASE_URL to use durable PostgreSQL.
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS offers (
            id TEXT PRIMARY KEY,
            connection_id TEXT NOT NULL,
            chat_id INTEGER NOT NULL,
            message_id INTEGER,
            gift_url TEXT NOT NULL,
            gift_name TEXT NOT NULL,
            amount INTEGER NOT NULL,
            language TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at INTEGER NOT NULL,
            expires_at INTEGER,
            currency TEXT NOT NULL DEFAULT 'stars'
        )
    """)
    offer_columns = {row["name"] for row in conn.execute("PRAGMA table_info(offers)")}
    if "currency" not in offer_columns:
        conn.execute("ALTER TABLE offers ADD COLUMN currency TEXT NOT NULL DEFAULT 'stars'")
    if "expires_at" not in offer_columns:
        conn.execute("ALTER TABLE offers ADD COLUMN expires_at INTEGER")
        conn.execute("UPDATE offers SET expires_at = created_at + ? WHERE expires_at IS NULL", (OFFER_TTL_SECONDS,))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS access_requests (
            user_id INTEGER PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'pending',
            username TEXT,
            first_name TEXT,
            updated_at INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_notifications (
            id TEXT PRIMARY KEY,
            text TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            is_read INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS offer_drafts (
            draft_key TEXT PRIMARY KEY,
            connection_id TEXT NOT NULL,
            chat_id INTEGER NOT NULL,
            gift_url TEXT NOT NULL,
            gift_name TEXT NOT NULL,
            amount INTEGER,
            stage TEXT NOT NULL,
            updated_at INTEGER NOT NULL,
            source_message_ids TEXT NOT NULL DEFAULT '[]'
        )
    """)
    draft_columns = {row["name"] for row in conn.execute("PRAGMA table_info(offer_drafts)")}
    if "source_message_ids" not in draft_columns:
        conn.execute("ALTER TABLE offer_drafts ADD COLUMN source_message_ids TEXT NOT NULL DEFAULT '[]'")
    conn.commit()
    return conn

def api(method, payload=None):
    global LAST_SEND_AT
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is not set")

    payload = payload or {}
    business_connection_id = payload.get("business_connection_id")
    business_chat_id = payload.get("chat_id")
    business_key = (str(business_connection_id), int(business_chat_id)) if business_connection_id and business_chat_id is not None else None

    # Only Business sends are paused for a chat after PEER_FLOOD. Normal bot
    # commands and private bot chats (/start, admin panel) remain available.
    send_guard = SEND_MESSAGE_LOCK if method == "sendMessage" else nullcontext()
    with send_guard:
        if method == "sendMessage":
            if business_key:
                with PAUSED_BUSINESS_CHATS_LOCK:
                    paused_until = PAUSED_BUSINESS_CHATS.get(business_key, 0)
                    now = time.monotonic()
                    if paused_until > now:
                        remaining = int(paused_until - now)
                        raise RuntimeError(
                            f"Business chat is in PEER_FLOOD cooldown for {remaining}s; "
                            "send was not retried."
                        )
                    if paused_until:
                        PAUSED_BUSINESS_CHATS.pop(business_key, None)
            delay = SEND_MIN_INTERVAL - (time.monotonic() - LAST_SEND_AT)
            if delay > 0:
                time.sleep(delay)

        try:
            body = json.dumps(payload).encode("utf-8")
            for attempt in range(6):
                req = Request(API + method, data=body, headers={"Content-Type": "application/json"})
                try:
                    with urlopen(req, timeout=15) as response:
                        data = json.loads(response.read().decode("utf-8"))
                except HTTPError as exc:
                    detail = exc.read().decode("utf-8", "replace")
                    if method == "sendMessage" and business_key and "PEER_FLOOD" in detail.upper():
                        with PAUSED_BUSINESS_CHATS_LOCK:
                            PAUSED_BUSINESS_CHATS[business_key] = (
                                time.monotonic() + BUSINESS_PEER_FLOOD_COOLDOWN
                            )
                        log.critical(
                            "BUSINESS_CHAT_PAUSED: Telegram returned PEER_FLOOD; "
                            "this Business chat is paused for %ss (connection_id=%s chat_id=%s).",
                            BUSINESS_PEER_FLOOD_COOLDOWN, business_connection_id, business_chat_id
                        )
                        raise RuntimeError(
                            "Telegram PEER_FLOOD: this Business chat is paused; no automatic retry."
                        ) from exc
                    if exc.code == 429 and attempt < 5:
                        try:
                            error_data = json.loads(detail)
                            wait_seconds = int((error_data.get("parameters") or {}).get("retry_after", 1))
                        except (ValueError, TypeError):
                            wait_seconds = 1
                        wait_seconds = max(1, wait_seconds)
                        log.warning(
                            "Telegram rate limit on %s; respecting retry_after=%s seconds (attempt %s/6)",
                            method, wait_seconds, attempt + 1
                        )
                        time.sleep(wait_seconds)
                        continue
                    raise RuntimeError(f"Telegram API HTTP {exc.code}: {detail}") from exc
                except URLError as exc:
                    raise RuntimeError(f"Telegram API connection error: {exc}") from exc

                if not data.get("ok"):
                    detail = json.dumps(data, ensure_ascii=False)
                    if method == "sendMessage" and business_key and "PEER_FLOOD" in detail.upper():
                        with PAUSED_BUSINESS_CHATS_LOCK:
                            PAUSED_BUSINESS_CHATS[business_key] = (
                                time.monotonic() + BUSINESS_PEER_FLOOD_COOLDOWN
                            )
                        log.critical(
                            "BUSINESS_CHAT_PAUSED: Telegram returned PEER_FLOOD; "
                            "only this Business chat is suspended (connection_id=%s chat_id=%s).",
                            business_connection_id, business_chat_id
                        )
                        raise RuntimeError(
                            "Telegram PEER_FLOOD: this Business chat is paused; no automatic retry."
                        )
                    if data.get("error_code") == 429 and attempt < 5:
                        wait_seconds = int((data.get("parameters") or {}).get("retry_after", 1))
                        wait_seconds = max(1, wait_seconds)
                        log.warning(
                            "Telegram rate limit on %s; respecting retry_after=%s seconds (attempt %s/6)",
                            method, wait_seconds, attempt + 1
                        )
                        time.sleep(wait_seconds)
                        continue
                    raise RuntimeError(f"Telegram API error: {data}")
                return data.get("result")
            raise RuntimeError(f"Telegram API rate limit persisted for {method} after 6 attempts")
        finally:
            if method == "sendMessage":
                LAST_SEND_AT = time.monotonic()

def send_message(chat_id, text, connection_id=None, reply_markup=None, link_preview_url=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if connection_id:
        payload["business_connection_id"] = connection_id
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if link_preview_url:
        payload["link_preview_options"] = {
            "url": link_preview_url,
            "prefer_large_media": True,
            "show_above_text": True
        }
    else:
        payload["link_preview_options"] = {"is_disabled": True}
    return api("sendMessage", payload)

def fetch_nft_image(gift_url):
    """Try to read the public Telegram NFT page's Open Graph image."""
    try:
        request = Request(
            gift_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; NFTOfferBot/1.0)"}
        )
        with urlopen(request, timeout=4) as response:
            page = response.read(1_500_000).decode("utf-8", "replace")
        patterns = [
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
            r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']'
        ]
        for pattern in patterns:
            found = re.search(pattern, page, re.IGNORECASE)
            if found:
                image_url = html.unescape(found.group(1)).strip()
                if image_url.startswith("https://"):
                    return image_url
    except Exception as exc:
        log.info("NFT preview image unavailable for %s: %s", gift_url, exc)
    return None

def send_offer_card(chat_id, caption, connection_id, reply_markup, gift_url):
    # Keep Telegram's large NFT link preview so the offer includes the gift image.
    return send_message(chat_id, caption, connection_id, reply_markup, gift_url)

def pin_business_offer(chat_id, message_id, connection_id):
    """Pin the sent offer message in the connected Business chat."""
    try:
        api("pinChatMessage", {
            "business_connection_id": connection_id,
            "chat_id": chat_id,
            "message_id": message_id,
            "disable_notification": True
        })
        log.info("Pinned offer message %s in chat %s", message_id, chat_id)
    except Exception:
        log.exception("Could not pin offer message %s in chat %s", message_id, chat_id)

def delete_source_messages(connection_id, chat_id, message_ids):
    # Business chats must use deleteBusinessMessages, not deleteMessage.
    ids = sorted({int(message_id) for message_id in (message_ids or []) if message_id})
    if not ids:
        return
    try:
        api("deleteBusinessMessages", {
            "business_connection_id": connection_id,
            "message_ids": ids
        })
        log.info("Deleted source messages in business chat %s: %s", chat_id, ids)
    except Exception:
        log.exception(
            "Could not delete source messages in business chat %s. "
            "Check that the connected bot has can_delete_all_messages permission.",
            chat_id
        )

def normalize_nft_url(value):
    value = (value or "").strip()
    if value.startswith("t.me/") or value.startswith("www.t.me/"):
        value = "https://" + value
    parsed = urlparse(value)
    if parsed.scheme not in ("https", "http") or parsed.netloc.lower() not in ("t.me", "www.t.me"):
        return None
    if parsed.query or parsed.fragment:
        # Telegram NFT links do not need query strings or fragments.
        return None
    if not re.fullmatch(r"/nft/[A-Za-z0-9_-]+", parsed.path):
        return None
    return "https://t.me" + parsed.path


def parse_offer(text):
    parts = (text or "").strip().split()
    if len(parts) == 3:
        url, amount_text, language = parts
        currency = "stars"
    elif len(parts) == 4:
        url, amount_text, currency, language = parts
        currency = currency.lower()
    else:
        return None
    language = language.lower()
    aliases = {
        "star": "stars", "stars": "stars", "⭐": "stars",
        "gram": "gram", "grams": "gram",
        "rub": "rub", "ruble": "rub", "rubles": "rub",
        "руб": "rub", "рубли": "rub"
    }
    currency = aliases.get(currency)
    if currency not in ("stars", "gram", "rub") or language not in TEXTS:
        return None
    if not re.fullmatch(r"[1-9][0-9]{0,17}", amount_text):
        return None
    gift_url = normalize_nft_url(url)
    if not gift_url:
        return None
    slug = urlparse(gift_url).path.rsplit("/", 1)[-1]
    gift_name = format_gift_name(slug)
    return gift_url, int(amount_text), currency, language, gift_name


def gift_emoji(gift_name):
    name = (gift_name or "").lower()
    rules = [
        (("cigar", "сигар"), "🚬"),
        (("dog", "собак", "puppy"), "🐶"),
        (("cat", "кот", "кошка"), "🐱"),
        (("frog", "лягуш"), "🐸"),
        (("pepe",), "🐸"),
        (("cap", "шап", "hat"), "🧢"),
        (("rose", "роз"), "🌹"),
        (("heart", "серд"), "❤️"),
        (("bear", "медвед"), "🐻"),
        (("rabbit", "крол"), "🐰"),
        (("duck", "утк"), "🦆"),
        (("diamond", "алмаз"), "💎"),
        (("trophy", "кубок"), "🏆"),
        (("cake", "торт"), "🎂"),
        (("star", "звезд"), "⭐️"),
    ]
    for words, emoji in rules:
        if any(word in name for word in words):
            return emoji
    return "🎁"


def format_amount(amount, currency, gift_name=""):
    gram_marks = ("💎", "🪙", "🔷", "✨")
    gram_mark = gram_marks[int(amount) % len(gram_marks)]
    labels = {"stars": "⭐️ Stars", "gram": f"{gram_mark} GRAM", "rub": "₽ RUB"}
    return f"{amount} {labels.get(currency, '⭐️ Stars')} {gift_emoji(gift_name)}"


def format_amount_html(amount, currency, gift_name=""):
    if currency == "stars":
        premium_star = '<tg-emoji emoji-id="5848021027782661221">⭐️</tg-emoji>'
        return f"{amount} {premium_star} <b>Stars</b> {gift_emoji(gift_name)}"
    return html.escape(format_amount(amount, currency, gift_name))


def format_usd_equivalent(amount, currency):
    if currency != "stars":
        return "—"
    return f"≈ ${amount * STAR_USD_RATE:,.2f} USD"

def format_gift_name(slug):
    value = re.sub(r"([a-z])([A-Z])", r"\1 \2", slug)
    value = re.sub(r"[-_](\d+)$", r" #\1", value)
    value = value.replace("-", " ").replace("_", " ")
    return re.sub(r"\s+", " ", value).strip()

def parse_gift_url(value):
    normalized = normalize_nft_url(value)
    if not normalized:
        return None
    slug = urlparse(normalized).path.rsplit("/", 1)[-1]
    return (normalized, format_gift_name(slug))

def create_offer(message, connection_id, gift_url, amount, language, gift_name, source_message_ids=None, currency="stars", seller_name=None):
    chat = message.get("chat", {})
    seller = html.escape(seller_name or chat.get("first_name") or chat.get("title") or "Продавец")
    buyer_label = ("@" + chat.get("username")) if chat.get("username") else (chat.get("first_name") or "Покупатель")
    buyer_label = html.escape(str(buyer_label))
    safe_gift = html.escape(gift_name)
    strings = TEXTS[language]
    offer_id = uuid.uuid4().hex[:12]
    markup = {
        "inline_keyboard": [[
            {"text": strings["accept"], "callback_data": f"accept:{offer_id}"},
            {"text": strings["decline"], "callback_data": f"decline:{offer_id}"}
        ]]
    }
    text_out = strings["offer"].format(
        seller=seller, buyer_name=buyer_label,
        url=html.escape(gift_url, quote=True),
        gift=safe_gift, amount_display=format_amount_html(amount, currency, gift_name),
        usd_display=format_usd_equivalent(amount, currency),
        expires_display="5 ч 59 мин" if language == "ru" else ("5 hr 59 min" if language == "en" else "5小时59分钟")
    )
    # Persist first: if Telegram refuses delivery (for example PEER_FLOOD),
    # the offer remains recorded as queued instead of disappearing.
    created_at = int(time.time())
    with db() as conn:
        conn.execute(
            """INSERT INTO offers
            (id, connection_id, chat_id, message_id, gift_url, gift_name, amount, language, status, created_at, currency, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)""",
            (offer_id, connection_id, chat["id"], None, gift_url, gift_name,
             amount, language, created_at, currency, created_at + OFFER_TTL_SECONDS)
        )
        conn.commit()

    # Telegram is still authoritative: failed deliveries are recorded and
    # surfaced to admins; the bot never force-retries a Business send.
    send_started = time.perf_counter()
    try:
        sent = send_offer_card(chat["id"], text_out, connection_id, markup, gift_url)
    except Exception as exc:
        with db() as conn:
            conn.execute(
                "UPDATE offers SET status='failed' WHERE id=? AND status='queued'",
                (offer_id,)
            )
            conn.commit()
        log.exception(
            "OFFER_DELIVERY_FAILED offer_id=%s chat_id=%s connection_id=%s error=%s",
            offer_id, chat["id"], connection_id, str(exc)
        )
        try:
            notify_admin(
                "⚠️ <b>Оффер не доставлен</b>\n"
                f"ID: <code>{offer_id}</code>\n"
                f"Чат: <code>{chat['id']}</code>\n"
                f"Подарок: {html.escape(gift_name)}\n"
                f"Причина: <code>{html.escape(str(exc)[:500])}</code>"
            )
        except Exception:
            log.exception("Could not notify admins about failed offer %s", offer_id)
        raise
    log.info("Telegram sendMessage for offer %s completed in %.3fs", offer_id, time.perf_counter() - send_started)
    with db() as conn:
        conn.execute(
            "UPDATE offers SET message_id=?, status='pending' WHERE id=? AND status='queued'",
            (sent["message_id"], offer_id)
        )
        conn.commit()

    source_ids = source_message_ids or [message.get("message_id")]
    delete_thread = threading.Thread(
        target=delete_source_messages,
        args=(connection_id, chat["id"], source_ids),
        name=f"delete-offer-source-{offer_id}",
        daemon=True
    )
    delete_thread.start()

    # Pin independently; neither deletion nor pinning blocks offer delivery.
    threading.Thread(
        target=pin_business_offer,
        args=(chat["id"], sent["message_id"], connection_id),
        name=f"pin-offer-{offer_id}",
        daemon=True
    ).start()
    log.info("Offer %s created in chat %s; source deletion started", offer_id, chat["id"])

def handle_business_message(message):
    # Only the connected account owner may start an offer flow.
    connection_id = message.get("business_connection_id")
    # Telegram may deliver a Business command as a text message or as a media caption.
    text = (message.get("text") or message.get("caption") or "").strip()
    sender = message.get("from") or {}
    if not connection_id:
        log.warning("BUSINESS_DIAG rejected: missing business_connection_id; update has no usable Business context")
        return
    if not text:
        log.info("BUSINESS_DIAG ignored: empty text/caption; chat_id=%s message_id=%s",
                 (message.get("chat") or {}).get("id"), message.get("message_id"))
        return
    if message.get("sender_business_bot"):
        log.info("BUSINESS_DIAG ignored: message was sent by a business bot; chat_id=%s",
                 (message.get("chat") or {}).get("id"))
        return

    chat = message.get("chat") or {}
    log.info(
        "BUSINESS_DIAG received: connection_id=%s chat_id=%s chat_type=%s sender_id=%s "
        "message_id=%s text_present=%s",
        connection_id, chat.get("id"), chat.get("type"), sender.get("id"),
        message.get("message_id"), bool(text)
    )
    connection_started = time.perf_counter()
    try:
        connection = get_business_connection_cached(connection_id)
    except Exception:
        log.exception("Could not verify Business connection")
        return
    log.info("Business connection lookup completed in %.3fs (cache_or_api)", time.perf_counter() - connection_started)

    owner = connection.get("user") or {}
    owner_id = owner.get("id")
    rights = connection.get("rights") or {}
    log.info(
        "BUSINESS_DIAG connection: enabled=%s owner_id=%s sender_matches_owner=%s "
        "can_reply=%s can_read_messages=%s can_view_gifts_and_stars=%s",
        connection.get("is_enabled"), owner_id, bool(owner_id and sender.get("id") == owner_id),
        rights.get("can_reply"), rights.get("can_read_messages"),
        rights.get("can_view_gifts_and_stars")
    )
    # In a Business chat, sender_id is normally the customer, while owner_id
    # is the connected business account. They are expected to be different.
    # Validate the connection and its reply permission, not the customer's ID.
    if not owner_id:
        log.warning("BUSINESS_DIAG rejected: connection has no owner; connection_id=%s", connection_id)
        return
    if not connection.get("is_enabled", False):
        log.warning("BUSINESS_DIAG rejected: Business connection is disabled; connection_id=%s", connection_id)
        return
    if rights.get("can_reply") is not True:
        log.warning("BUSINESS_DIAG rejected: Business connection cannot reply; connection_id=%s owner_id=%s",
                    connection_id, owner_id)
        return
    if not sender.get("id"):
        log.warning("BUSINESS_DIAG rejected: message has no sender ID; chat_id=%s", chat.get("id"))
        return

    if chat.get("type") != "private":
        log.warning("BUSINESS_DIAG rejected: unsupported chat type=%s chat_id=%s",
                    chat.get("type"), chat.get("id"))
        return
    chat_id = chat.get("id")
    key = f"{connection_id}:{chat_id}"

    # Keep the old compact syntax working: URL amount language.
    parsed = parse_offer(text)
    if parsed:
        gift_url, amount, currency, language, gift_name = parsed
        try:
            create_offer(message, connection_id, gift_url, amount, language, gift_name, [message.get("message_id")], currency, (connection.get("user") or {}).get("first_name"))
        except Exception as exc:
            log.exception(
                "One-line offer creation failed: language=%s chat_id=%s sender_id=%s "
                "connection_id=%s chat_type=%s error=%s",
                language, chat_id, sender.get("id"), connection_id, chat.get("type"), str(exc)
            )
            if "PEER_FLOOD" in str(exc):
                try:
                    info = business_diagnostic(connection_id)
                    log.error("PEER_FLOOD_CONTEXT %s", json.dumps(info, ensure_ascii=False, sort_keys=True))
                except Exception:
                    log.exception("Could not collect Business connection context after PEER_FLOOD")
            if "PEER_FLOOD" not in str(exc).upper():
                try:
                    send_message(chat_id, "❌ Telegram отклонил отправку оффера. Проверьте журнал Render; повторные попытки подряд не делайте.", connection_id)
                except Exception:
                    log.exception("Could not send offer failure notice to business chat %s", chat_id)
        return

    # New step-by-step syntax: URL -> amount -> language.
    gift = parse_gift_url(text)
    if gift:
        gift_url, gift_name = gift
        with db() as conn:
            conn.execute("""
                INSERT INTO offer_drafts
                (draft_key, connection_id, chat_id, gift_url, gift_name, amount, stage, updated_at, source_message_ids)
                VALUES (?, ?, ?, ?, ?, NULL, 'amount', ?, ?)
                ON CONFLICT(draft_key) DO UPDATE SET
                    gift_url=excluded.gift_url, gift_name=excluded.gift_name,
                    amount=NULL, stage='amount', updated_at=excluded.updated_at,
                    source_message_ids=excluded.source_message_ids
            """, (key, connection_id, chat_id, gift_url, gift_name,
                  int(time.time()), json.dumps([message.get("message_id")])))
            conn.commit()
        send_message(chat_id, "Ссылка на NFT получена. Теперь отправь сумму в Stars отдельным сообщением, например: 2300", connection_id)
        return

    with db() as conn:
        draft = conn.execute("SELECT * FROM offer_drafts WHERE draft_key=?", (key,)).fetchone()

    if not draft:
        if "t.me/nft/" in text:
            send_message(chat_id, "Не удалось распознать ссылку. Отправь прямую ссылку вида https://t.me/nft/SnoopCigar-84846", connection_id)
        return

    if draft["stage"] == "amount":
        if not re.fullmatch(r"[1-9][0-9]{0,17}", text):
            send_message(chat_id, "Укажи сумму только цифрами, например: 2300", connection_id)
            return
        amount = int(text)
        with db() as conn:
            source_ids = json.loads(draft["source_message_ids"] or "[]")
            source_ids.append(message.get("message_id"))
            conn.execute("UPDATE offer_drafts SET amount=?, stage='language', updated_at=?, source_message_ids=? WHERE draft_key=?",
                         (amount, int(time.time()), json.dumps(source_ids), key))
            conn.commit()
        send_message(chat_id, "Сумма получена. Теперь отправь язык оффера: ru, en или zh", connection_id)
        return

    if draft["stage"] == "language":
        language_input = text.strip().lower()
        language_aliases = {
            "рус": "ru", "русский": "ru", "russian": "ru",
            "англ": "en", "английский": "en", "english": "en",
            "cn": "zh", "中文": "zh", "китайский": "zh", "chinese": "zh"
        }
        language = language_aliases.get(language_input, language_input)
        if language not in TEXTS:
            send_message(chat_id, "Выбери язык: ru, en или zh.", connection_id)
            return
        source_ids = json.loads(draft["source_message_ids"] or "[]")
        source_ids.append(message.get("message_id"))
        try:
            create_offer(message, connection_id, draft["gift_url"], int(draft["amount"]), language, draft["gift_name"], source_ids, seller_name=(connection.get("user") or {}).get("first_name"))
            with db() as conn:
                conn.execute("DELETE FROM offer_drafts WHERE draft_key=?", (key,))
                conn.commit()
        except Exception as exc:
            log.exception(
                "Offer creation failed: language=%s chat_id=%s sender_id=%s "
                "connection_id=%s error=%s",
                language, chat_id, sender.get("id"), connection_id, str(exc)
            )
            if "PEER_FLOOD" in str(exc):
                try:
                    info = business_diagnostic(connection_id)
                    log.error("PEER_FLOOD_CONTEXT %s", json.dumps(info, ensure_ascii=False, sort_keys=True))
                except Exception:
                    log.exception("Could not collect Business connection context after PEER_FLOOD")
            if "PEER_FLOOD" not in str(exc).upper():
                try:
                    send_message(chat_id, "❌ Telegram отклонил отправку оффера. Проверьте журнал Render; повторные попытки подряд не делайте.", connection_id)
                except Exception:
                    log.exception("Could not send offer failure notice to business chat %s", chat_id)

def notify_admin(text, reply_markup=None):
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO admin_notifications (id, text, created_at, is_read) VALUES (?, ?, ?, 0)",
                (uuid.uuid4().hex, str(text), int(time.time()))
            )
            conn.commit()
    except Exception:
        log.exception("Could not store admin notification")
    for admin_id in ADMIN_IDS:
        try:
            send_message(admin_id, text, reply_markup=reply_markup)
        except Exception:
            log.exception("Could not notify admin %s", admin_id)

def malik_keyboard():
    try:
        with db() as conn:
            unread = conn.execute("SELECT COUNT(*) AS n FROM admin_notifications WHERE is_read=0").fetchone()["n"]
    except Exception:
        unread = 0
    label = f"🔔 Уведомления ({unread})" if unread else "🔔 Уведомления"
    return {"inline_keyboard": [
        [{"text": "👥 Пользователи", "callback_data": "malik:users"}],
        [{"text": "🎁 Офферы", "callback_data": "malik:offers"}],
        [{"text": label, "callback_data": "malik:notifications"}]
    ]}


def malik_screen(chat_id, message_id, text, keyboard):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": keyboard
    }
    return api("editMessageText", payload)


def handle_malik_callback(query, data, actor_id):
    if not data.startswith("malik:"):
        return False
    if actor_id not in ADMIN_IDS:
        api("answerCallbackQuery", {
            "callback_query_id": query["id"],
            "text": "Нет доступа",
            "show_alert": True
        })
        return True

    msg = query.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    if not chat_id or not message_id:
        api("answerCallbackQuery", {"callback_query_id": query["id"], "text": "Не удалось открыть панель"})
        return True

    parts = data.split(":")
    screen = "home"
    body = "⚙️ <b>Malik — панель управления</b>\nВыберите раздел:"
    keyboard = malik_keyboard()

    if data == "malik:notifications":
        with db() as conn:
            rows = conn.execute(
                "SELECT text, created_at FROM admin_notifications ORDER BY created_at DESC LIMIT 15"
            ).fetchall()
            conn.execute("UPDATE admin_notifications SET is_read=1 WHERE is_read=0")
            conn.commit()
        if rows:
            lines = ["🔔 <b>Последние уведомления</b>"]
            for row in rows:
                stamp = time.strftime("%d.%m %H:%M", time.localtime(int(row["created_at"])))
                plain = html.unescape(re.sub(r"<[^>]*>", "", str(row["text"])))
                lines.append(f"\n🕒 <b>{stamp}</b>\n{html.escape(plain)[:700]}")
            body = "\n".join(lines)
        else:
            body = "🔔 <b>Уведомления</b>\nПока уведомлений нет."
        keyboard = {"inline_keyboard": [
            [{"text": "🧹 Очистить историю", "callback_data": "malik:notifications_clear"}],
            [{"text": "⬅️ Назад", "callback_data": "malik:home"}]
        ]}

    elif data == "malik:notifications_clear":
        with db() as conn:
            conn.execute("DELETE FROM admin_notifications")
            conn.commit()
        body = "✅ История уведомлений очищена."
        keyboard = {"inline_keyboard": [
            [{"text": "🔔 Уведомления", "callback_data": "malik:notifications"}],
            [{"text": "⬅️ В меню", "callback_data": "malik:home"}]
        ]}

    elif data == "malik:users":
        screen = "users"
        with db() as conn:
            rows = conn.execute("""
                SELECT user_id AS uid, status,
                       COALESCE(first_name, '') AS first_name,
                       COALESCE(username, '') AS username
                FROM access_requests
                ORDER BY updated_at DESC LIMIT 30
            """).fetchall()
            offer_users = conn.execute("""
                SELECT chat_id AS uid, COUNT(*) AS offer_count
                FROM offers GROUP BY chat_id ORDER BY MAX(created_at) DESC LIMIT 30
            """).fetchall()
        users = {}
        for row in rows:
            users[int(row["uid"])] = {
                "status": row["status"],
                "name": row["first_name"] or (("@" + row["username"]) if row["username"] else "Пользователь"),
                "offers": 0
            }
        for row in offer_users:
            uid = int(row["uid"])
            if uid not in users:
                users[uid] = {"status": "есть офферы", "name": "Пользователь", "offers": int(row["offer_count"])}
            else:
                users[uid]["offers"] = int(row["offer_count"])
        body = "👥 <b>Пользователи</b>\nНажмите на пользователя для управления:"
        buttons = []
        for uid, item in list(users.items())[:30]:
            label = f'{item["name"]} · {uid} · офферов: {item["offers"]}'
            buttons.append([{"text": label[:60], "callback_data": f"malik:u:{uid}"}])
        if not buttons:
            body += "\nПока пользователей нет."
        buttons.append([{"text": "⬅️ Назад", "callback_data": "malik:home"}])
        keyboard = {"inline_keyboard": buttons}

    elif len(parts) == 3 and parts[1] == "u" and parts[2].isdigit():
        uid = int(parts[2])
        with db() as conn:
            access = conn.execute("SELECT status, first_name, username FROM access_requests WHERE user_id=?", (uid,)).fetchone()
            count = conn.execute("SELECT COUNT(*) AS n FROM offers WHERE chat_id=?", (uid,)).fetchone()["n"]
        label = (access["first_name"] if access else None) or (("@" + access["username"]) if access and access["username"] else "Пользователь")
        status = access["status"] if access else "нет заявки"
        body = (f"👤 <b>{html.escape(label)}</b>\nID: <code>{uid}</code>\n"
                f"Доступ: {html.escape(status)}\nОфферов: {count}\n\n"
                "Удаление пользователя удалит его заявки на доступ, черновики и офферы из базы бота.")
        keyboard = {"inline_keyboard": [
            [{"text": "🗑 Удалить пользователя и его офферы", "callback_data": f"malik:ud:{uid}"}],
            [{"text": "⬅️ К пользователям", "callback_data": "malik:users"}]
        ]}

    elif len(parts) == 3 and parts[1] == "ud" and parts[2].isdigit():
        uid = int(parts[2])
        with db() as conn:
            conn.execute("DELETE FROM offers WHERE chat_id=?", (uid,))
            conn.execute("DELETE FROM offer_drafts WHERE chat_id=?", (uid,))
            conn.execute("DELETE FROM access_requests WHERE user_id=?", (uid,))
            conn.commit()
        body = f"✅ Пользователь <code>{uid}</code> и связанные с ним данные удалены из базы бота."
        keyboard = {"inline_keyboard": [[{"text": "⬅️ К пользователям", "callback_data": "malik:users"}],
                                        [{"text": "🏠 В меню", "callback_data": "malik:home"}]]}

    elif data == "malik:offers":
        with db() as conn:
            rows = conn.execute("SELECT id, chat_id, gift_name, amount, currency, language, status FROM offers ORDER BY created_at DESC LIMIT 20").fetchall()
        body = "🎁 <b>Последние офферы</b>\nНажмите на оффер для просмотра:"
        buttons = []
        for row in rows:
            label = f'{row["gift_name"]} · {format_amount(row["amount"], row["currency"], row["gift_name"])} · {row["status"]}'
            buttons.append([{"text": label[:60], "callback_data": f'malik:o:{row["id"]}'}])
        if not buttons:
            body += "\nОфферов пока нет."
        buttons.append([{"text": "⬅️ Назад", "callback_data": "malik:home"}])
        keyboard = {"inline_keyboard": buttons}

    elif len(parts) == 3 and parts[1] == "o" and re.fullmatch(r"[a-f0-9]{12}", parts[2]):
        offer_id = parts[2]
        with db() as conn:
            offer = conn.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
        if not offer:
            body = "Оффер уже не найден."
            keyboard = {"inline_keyboard": [[{"text": "⬅️ К офферам", "callback_data": "malik:offers"}]]}
        else:
            body = (f"🎁 <b>{html.escape(offer['gift_name'])}</b>\n"
                    f"ID оффера: <code>{offer_id}</code>\n"
                    f"Пользователь: <code>{offer['chat_id']}</code>\n"
                    f"Сумма: <b>{html.escape(format_amount(offer['amount'], offer['currency'], offer['gift_name']))}</b>\n"
                    f"Язык: {offer['language']}\nСтатус: {html.escape(offer['status'])}\n"
                    f'<a href="{html.escape(offer["gift_url"], quote=True)}">Открыть подарок</a>')
            keyboard = {"inline_keyboard": [
                [{"text": "🗑 Удалить оффер", "callback_data": f"malik:od:{offer_id}"}],
                [{"text": "⬅️ К офферам", "callback_data": "malik:offers"}]
            ]}

    elif len(parts) == 3 and parts[1] == "od" and re.fullmatch(r"[a-f0-9]{12}", parts[2]):
        offer_id = parts[2]
        with db() as conn:
            conn.execute("DELETE FROM offers WHERE id=?", (offer_id,))
            conn.commit()
        body = f"✅ Оффер <code>{offer_id}</code> удалён из базы бота."
        keyboard = {"inline_keyboard": [[{"text": "⬅️ К офферам", "callback_data": "malik:offers"}],
                                        [{"text": "🏠 В меню", "callback_data": "malik:home"}]]}

    elif data != "malik:home":
        body = "Неизвестный раздел."
        keyboard = malik_keyboard()

    try:
        malik_screen(chat_id, message_id, body, keyboard)
    except Exception:
        log.exception("Could not update Malik admin panel")
    api("answerCallbackQuery", {"callback_query_id": query["id"]})
    return True


def handle_private_message(message):
    text = (message.get("text") or "").strip()
    user = message.get("from") or {}
    user_id = user.get("id")
    command = text.split(maxsplit=1)[0].split("@")[0] if text else ""
    if command == "/0133":
        send_message(
            user_id,
            "✅ <b>Доступ к созданию NFT-офферов открыт.</b>\n\n"
            "Создавать офферы может любой пользователь без одобрения администратора. "
            "Для отправки офферов подключите этого бота в Telegram → Настройки → Telegram для бизнеса → Чат-боты, "
            "выдайте разрешения и отправляйте оффер из нужного личного Business-чата.\n\n"
            "Формат: <code>https://t.me/nft/НазваниеПодарка-12345 2300 ru</code>\n"
            "Язык в конце: ru, en или zh."
        )
        return
    if command == "/malik":
        if user_id not in ADMIN_IDS:
            if user_id:
                send_message(user_id, "⛔ Нет доступа к этой команде.")
            return
        send_message(user_id, "⚙️ <b>Malik — панель управления</b>\nВыберите раздел:", reply_markup=malik_keyboard())
        return
    if command == "/diag":
        if user_id not in ADMIN_IDS:
            if user_id:
                send_message(user_id, "⛔ Нет доступа к диагностике.")
            return
        try:
            with db() as conn:
                rows = conn.execute(
                    "SELECT connection_id, MAX(created_at) AS last_used, COUNT(*) AS offer_count "
                    "FROM offers GROUP BY connection_id ORDER BY last_used DESC LIMIT 5"
                ).fetchall()
            if not rows:
                send_message(user_id, "Диагностика: в базе пока нет Business Connection из созданных офферов.")
                return
            lines = ["🔎 <b>Диагностика Telegram Business</b>"]
            for row in rows:
                try:
                    info = business_diagnostic(row["connection_id"])
                    rights = info.get("rights") or {}
                    rights_text = ", ".join(
                        f"{key}={'да' if value else 'нет'}" for key, value in rights.items()
                    ) or "права не переданы Telegram"
                    lines.append(
                        "\n<b>Подключение:</b> <code>" + html.escape(str(info.get("id"))) + "</code>"
                        + "\nАккаунт: <code>" + html.escape(str(info.get("user_id"))) + "</code>"
                        + (" (@" + html.escape(str(info.get("username"))) + ")" if info.get("username") else "")
                        + "\nВключено: <b>" + ("да" if info.get("is_enabled") else "нет") + "</b>"
                        + "\nПрава: " + html.escape(rights_text)
                        + "\nОфферов в базе: " + str(row["offer_count"])
                    )
                    log.info("BUSINESS_DIAGNOSTIC %s", json.dumps(info, ensure_ascii=False, sort_keys=True))
                except Exception as exc:
                    lines.append("\nПодключение <code>" + html.escape(str(row["connection_id"])) +
                                 "</code>: ошибка проверки — " + html.escape(str(exc)[:300]))
                    log.exception("Business diagnostic failed for connection %s", row["connection_id"])
            lines.append("\n<i>Диагностика не отправляет тестовые сообщения и не меняет права подключения.</i>")
            send_message(user_id, "\n".join(lines))
        except Exception:
            log.exception("Admin business diagnostic command failed")
            send_message(user_id, "❌ Не удалось выполнить диагностику. Подробности — в журнале Render.")
        return
    if command != "/start":
        return
    if not user_id:
        return
    if user_id in ADMIN_IDS:
        send_message(user_id, "Панель управления: /malik")
        return
    if command == "/start":
        send_message(
            user_id,
            "🎮 <b>Добро пожаловать в игру!</b>\n\n"
            "Рады видеть тебя здесь. Выбери раздел ниже 👇",
            reply_markup={"inline_keyboard": [
                [{"text": "🎮 Играть", "callback_data": "game:play"}],
                [{"text": "ℹ️ О нас", "callback_data": "game:about"}]
            ]}
        )
        return
    
def handle_callback(query):
    data = query.get("data", "")
    actor_id = (query.get("from") or {}).get("id")
    log.info("Callback received: data=%s actor_id=%s", data, actor_id)
    if handle_malik_callback(query, data, actor_id):
        return
    if data in ("game:play", "game:about"):
        body = ("🎮 <b>Игровой раздел</b>\n\nЭтот раздел скоро будет доступен."
                if data == "game:play" else
                "ℹ️ <b>О нас</b>\n\nЭто игровой информационный бот. Добро пожаловать и приятного времени!")
        api("editMessageText", {
            "chat_id": query["message"]["chat"]["id"],
            "message_id": query["message"]["message_id"],
            "text": body,
            "reply_markup": {"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "game:home"}]]}
        })
        api("answerCallbackQuery", {"callback_query_id": query["id"]})
        return
    if data == "game:home":
        api("editMessageText", {
            "chat_id": query["message"]["chat"]["id"],
            "message_id": query["message"]["message_id"],
            "text": "🎮 <b>Добро пожаловать в игру!</b>\n\nРады видеть тебя здесь. Выбери раздел ниже 👇",
            "reply_markup": {"inline_keyboard": [
                [{"text": "🎮 Играть", "callback_data": "game:play"}],
                [{"text": "ℹ️ О нас", "callback_data": "game:about"}]
            ]}
        })
        api("answerCallbackQuery", {"callback_query_id": query["id"]})
        return
    access_match = re.fullmatch(r"(request_access|access_approve|access_decline):([0-9]{1,20})", data)
    review_match = re.fullmatch(r"(transfer_approve|transfer_reject):([a-f0-9]{12})", data)
    offer_match = re.fullmatch(r"(accept|decline|transfer):([a-f0-9]{12})", data)

    if access_match:
        action, user_text = access_match.groups()
        target_id = int(user_text)
        if action == "request_access":
            if actor_id != target_id:
                api("answerCallbackQuery", {"callback_query_id": query["id"], "text": "Это не ваша заявка", "show_alert": True})
                return
            user = query.get("from") or {}
            with db() as conn:
                existing = conn.execute(
                    "SELECT status FROM access_requests WHERE user_id=?", (target_id,)
                ).fetchone()
                if existing and existing["status"] == "approved":
                    api("answerCallbackQuery", {
                        "callback_query_id": query["id"],
                        "text": "✅ Доступ уже одобрен — повторная заявка не нужна",
                        "show_alert": True
                    })
                    return
                already_pending = bool(existing and existing["status"] == "pending")
                conn.execute("""INSERT INTO access_requests (user_id, status, username, first_name, updated_at)
                    VALUES (?, 'pending', ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                    username=excluded.username, first_name=excluded.first_name,
                    updated_at=excluded.updated_at""",
                    (target_id, user.get("username"), user.get("first_name"), int(time.time())))
                conn.commit()
            if not already_pending:
                notify_admin(
                    "🆕 <b>Новая заявка на доступ к офферам</b>\n"
                    f"Пользователь: {html.escape(user.get('first_name') or 'Без имени')} "
                    f"(@{html.escape(user.get('username') or 'нет username')})\nID: <code>{target_id}</code>",
                    {"inline_keyboard": [[
                        {"text": "✅ Принять", "callback_data": f"access_approve:{target_id}"},
                        {"text": "❌ Отклонить", "callback_data": f"access_decline:{target_id}"}
                    ]]}
                )
            api("answerCallbackQuery", {"callback_query_id": query["id"], "text": "Заявка уже отправлена" if already_pending else "Заявка отправлена"})
            try:
                api("editMessageText", {"chat_id": target_id, "message_id": query["message"]["message_id"],
                    "text": "⏳ Заявка отправлена администратору. Ожидайте решения."})
            except Exception:
                pass
            return
        if actor_id not in ADMIN_IDS:
            log.warning("Unauthorized access review: actor=%s target=%s", actor_id, target_id)
            api("answerCallbackQuery", {"callback_query_id": query["id"], "text": "Только администратор может решать заявки", "show_alert": True})
            return
        new_status = "approved" if action == "access_approve" else "rejected"
        with db() as conn:
            conn.execute("""INSERT INTO access_requests (user_id, status, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at""",
                (target_id, new_status, int(time.time())))
            conn.commit()
        send_message(target_id, "✅ Доступ одобрен. Напишите /start." if new_status == "approved" else "❌ Заявка отклонена.")
        api("answerCallbackQuery", {"callback_query_id": query["id"], "text": "Решение сохранено"})
        try:
            api("editMessageReplyMarkup", {"chat_id": query["message"]["chat"]["id"],
                "message_id": query["message"]["message_id"], "reply_markup": {"inline_keyboard": []}})
        except Exception:
            pass
        return

    if review_match:
        action, offer_id = review_match.groups()
        if actor_id not in ADMIN_IDS:
            api("answerCallbackQuery", {"callback_query_id": query["id"], "text": "Только администратор может подтвердить передачу", "show_alert": True})
            return
        with db() as conn:
            offer = conn.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
            if not offer or offer["status"] != "transfer_marked":
                api("answerCallbackQuery", {"callback_query_id": query["id"], "text": "Запрос уже обработан", "show_alert": True})
                return
            new_status = "transfer_approved" if action == "transfer_approve" else "transfer_rejected"
            conn.execute("UPDATE offers SET status=? WHERE id=?", (new_status, offer_id))
            conn.commit()
        notice = "✅ Администратор подтвердил отметку о передаче. Ожидайте в чате." if action == "transfer_approve" else "❌ Администратор не подтвердил передачу. Свяжитесь с поддержкой."
        try:
            send_message(offer["chat_id"], notice, offer["connection_id"])
        except Exception:
            log.exception("Could not send transfer decision to business chat")
        api("answerCallbackQuery", {"callback_query_id": query["id"], "text": "Решение отправлено"})
        try:
            api("editMessageReplyMarkup", {"chat_id": query["message"]["chat"]["id"],
                "message_id": query["message"]["message_id"], "reply_markup": {"inline_keyboard": []}})
        except Exception:
            pass
        return

    if not offer_match:
        log.warning("Unknown callback data: %s", data)
        api("answerCallbackQuery", {"callback_query_id": query["id"], "text": "Неизвестное действие"})
        return
    action, offer_id = offer_match.groups()
    log.info("Offer callback action=%s offer_id=%s", action, offer_id)
    msg = query.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    with db() as conn:
        offer = conn.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
        if not offer:
            log.warning("Offer lookup failed: id=%s", offer_id)
            api("answerCallbackQuery", {"callback_query_id": query["id"], "text": "Предложение не найдено"})
            return
        strings = TEXTS[offer["language"]]
        status = offer["status"]
        expires_at = offer["expires_at"] or (offer["created_at"] + OFFER_TTL_SECONDS)
        if status == "pending" and int(time.time()) >= int(expires_at):
            conn.execute("UPDATE offers SET status='expired' WHERE id=? AND status='pending'", (offer_id,))
            conn.commit()
            api("answerCallbackQuery", {"callback_query_id": query["id"], "text": "⏳ Срок действия оффера истёк", "show_alert": True})
            return
        # Repeated accept taps must re-render the accepted deal instead of hiding it
        # behind an "already processed" alert (e.g. if the first edit failed).
        if action == "decline" and status != "pending":
            api("answerCallbackQuery", {"callback_query_id": query["id"], "text": strings["expired"], "show_alert": True})
            return
        if action == "accept" and status not in ("pending", "accepted"):
            api("answerCallbackQuery", {"callback_query_id": query["id"], "text": strings["expired"], "show_alert": True})
            return
        if action == "transfer" and status != "accepted":
            api("answerCallbackQuery", {"callback_query_id": query["id"], "text": strings["expired"], "show_alert": True})
            return

        if action == "accept":
            if status == "pending":
                changed = conn.execute("UPDATE offers SET status='accepted' WHERE id=? AND status='pending'", (offer_id,)).rowcount
                if changed != 1:
                    current = conn.execute("SELECT status FROM offers WHERE id=?", (offer_id,)).fetchone()
                    if not current or current["status"] != "accepted":
                        api("answerCallbackQuery", {"callback_query_id": query["id"], "text": strings["expired"], "show_alert": True})
                        return
            order_code = f"TG-{offer_id[:7].upper()}"
            body = strings["accepted"].format(
                order_code=order_code,
                gift=html.escape(offer["gift_name"]),
                gift_url=html.escape(offer["gift_url"], quote=True),
                amount_display=format_amount_html(offer["amount"], offer["currency"], offer["gift_name"]),
                usd_display=format_usd_equivalent(offer["amount"], offer["currency"]),
                transfer_url=html.escape(TRANSFER_URL, quote=True)
            )
            keyboard = {"inline_keyboard": [
                [{"text": strings["transfer_link"], "url": TRANSFER_URL}],
                [{"text": strings["confirm_transfer"], "callback_data": f"transfer:{offer_id}"}]
            ]}
        elif action == "decline":
            conn.execute("UPDATE offers SET status='declined' WHERE id=? AND status='pending'", (offer_id,))
            body = strings["declined"].format(gift=html.escape(offer["gift_name"]))
            keyboard = {"inline_keyboard": []}
        else:
            conn.execute("UPDATE offers SET status='transfer_marked' WHERE id=? AND status='accepted'", (offer_id,))
            body = strings["transfer_marked"]
            keyboard = {"inline_keyboard": []}
        conn.commit()


    api("answerCallbackQuery", {"callback_query_id": query["id"]})
    try:
        edit_method = "editMessageCaption" if msg.get("photo") else "editMessageText"
        edit_payload = {
            "business_connection_id": offer["connection_id"],
            "chat_id": offer["chat_id"],
            "message_id": offer["message_id"],
            "parse_mode": "HTML",
            "reply_markup": keyboard
        }
        if msg.get("photo"):
            edit_payload["caption"] = body
        else:
            edit_payload["text"] = body
            edit_payload["link_preview_options"] = {"url": offer["gift_url"], "prefer_large_media": True, "show_above_text": True}
        api(edit_method, edit_payload)
    except Exception:
        log.exception("Could not edit original offer message %s; sending fallback message", offer_id)
        try:
            send_message(offer["chat_id"], body, offer["connection_id"], keyboard, offer["gift_url"] if action == "accept" else None)
        except Exception:
            log.exception("Fallback offer response failed for %s", offer_id)

    # Notifications are secondary: a Telegram notification failure must never
    # prevent the accepted-deal message from being rendered in the business chat.
    try:
        if action == "accept":
            notify_admin(f"✅ <b>Оффер принят</b>\nПодарок: <a href=\"{html.escape(offer['gift_url'], quote=True)}\">{html.escape(offer['gift_name'])}</a>\nСумма: {html.escape(format_amount(offer['amount'], offer['currency']))}\nID: <code>{offer_id}</code>")
        elif action == "decline":
            notify_admin(f"❌ <b>Оффер отклонён</b>\nПодарок: <a href=\"{html.escape(offer['gift_url'], quote=True)}\">{html.escape(offer['gift_name'])}</a>\nСумма: {html.escape(format_amount(offer['amount'], offer['currency']))}\nID: <code>{offer_id}</code>")
        else:
            notify_admin(
                f"📩 <b>Пользователь подтвердил передачу</b>\nПодарок: <a href=\"{html.escape(offer['gift_url'], quote=True)}\">{html.escape(offer['gift_name'])}</a>\n"
                f"Сумма: {html.escape(format_amount(offer['amount'], offer['currency']))}\nID: <code>{offer_id}</code>\nПодтвердить передачу?",
                {"inline_keyboard": [[
                    {"text": "✅ Принять", "callback_data": f"transfer_approve:{offer_id}"},
                    {"text": "❌ Отклонить", "callback_data": f"transfer_reject:{offer_id}"}
                ]]}
            )
    
    except Exception:
        log.exception("Could not send admin notification for offer %s action=%s", offer_id, action)


def process_update(update):
    if "message" in update:
        handle_private_message(update["message"])
    elif "business_message" in update:
        handle_business_message(update["business_message"])
    elif "callback_query" in update:
        handle_callback(update["callback_query"])
    elif "business_connection" in update:
        item = update["business_connection"]
        cache_business_connection(item)
        log.info("Business connection cache refreshed: enabled=%s", item.get("is_enabled"))

def configure_webhook():
    if not PUBLIC_URL:
        raise SystemExit("RENDER_EXTERNAL_URL is missing. Deploy this app as a Render Web Service.")
    webhook_url = f"{PUBLIC_URL}/telegram/{WEBHOOK_SECRET}"
    allowed = ["message", "business_connection", "business_message", "callback_query"]
    result = api("setWebhook", {
        "url": webhook_url,
        "secret_token": WEBHOOK_SECRET,
        "allowed_updates": allowed,
        "drop_pending_updates": False
    })
    log.info("Telegram webhook configured: url=%s result=%s", webhook_url, result)


class HealthHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        expected_path = f"/telegram/{WEBHOOK_SECRET}"
        if self.path != expected_path:
            self.send_response(404)
            self.end_headers()
            return
        supplied_secret = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if supplied_secret != WEBHOOK_SECRET:
            self.send_response(403)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 2_000_000:
                raise ValueError("Invalid webhook payload size")
            update = json.loads(self.rfile.read(length).decode("utf-8"))
            update_id = update.get("update_id")
            update_type = next((key for key in ("callback_query", "business_message", "message", "business_connection") if key in update), "unknown")
            log.info("Webhook update received: type=%s update_id=%s", update_type, update_id)
            if update_id is not None:
                with SEEN_UPDATES_LOCK:
                    if update_id in SEEN_UPDATES:
                        self.send_response(200)
                        self.end_headers()
                        self.wfile.write(b"OK")
                        return
                    SEEN_UPDATES.add(update_id)
                    if len(SEEN_UPDATES) > 10000:
                        SEEN_UPDATES.clear()
                        SEEN_UPDATES.add(update_id)
            # Queue updates in arrival order. One worker processes them sequentially,
            # preventing URL/amount/language messages from racing in separate threads.
            UPDATE_QUEUE.put(update)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        except Exception:
            log.exception("Webhook update processing failed")
            self.send_response(500)
            self.end_headers()

    def do_GET(self):
        if self.path not in ("/", "/health"):
            self.send_response(404)
            self.end_headers()
            return
        body = b'{"service":"Telegram NFT Offers","status":"running"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        return

def main():
    if not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN in Render Environment before starting the service.")
    with db() as conn:
        conn.execute("SELECT 1")
    api("setMyCommands", {"commands": []})
    configure_webhook()

    def update_worker():
        while True:
            update = UPDATE_QUEUE.get()
            try:
                process_update(update)
            except Exception:
                log.exception("Queued webhook update processing failed")
            finally:
                UPDATE_QUEUE.task_done()

    threading.Thread(target=update_worker, name="telegram-update-worker", daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), HealthHandler)
    log.info("Webhook and health server listening on port %s; sequential update worker started", PORT)
    server.serve_forever()

if __name__ == "__main__":
    main()
