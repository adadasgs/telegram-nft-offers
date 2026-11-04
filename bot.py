import asyncio
import html
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "/var/data/offers.sqlite3")
PORT = int(os.getenv("PORT", "10000"))
TRANSFER_URL = os.getenv("TRANSFER_URL", "https://t.me/Autooffersgift")
API = f"https://api.telegram.org/bot{BOT_TOKEN}/"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("nft-offers")

TEXTS = {
    "ru": {
        "offer": "{seller} готов приобрести подарок\n<a href=\"{url}\">{gift}</a>.\n\nПредложенная сумма: <b>{amount}⭐️</b>\nПодтвердите передачу, если условия вам подходят.",
        "accept": "Принять",
        "decline": "Отклонить",
        "accepted": "Продавец принял предложение по подарку {gift} на сумму {amount}⭐️.\n\nЭто подтверждение согласия продавца, а не резервирование или перевод Stars. Передавайте подарок только после самостоятельной проверки условий.",
        "declined": "❌ Продавец отклонил предложение по подарку {gift}.",
        "transfer_link": "Передать подарок",
        "confirm_transfer": "Подтвердить передачу",
        "transfer_marked": "Отметка о передаче получена. Бот не проверяет фактическую передачу NFT или оплату. Проверьте их самостоятельно.",
        "buyer_accept": "Вы приняли предложение. Отправьте подарок покупателю только после проверки условий и получения оплаты самостоятельно. Автоматического удержания или перевода Stars этот бот не выполняет.",
        "buyer_decline": "Вы отклонили предложение.",
        "expired": "Это предложение уже обработано.",
        "bad": "Формат не распознан.\nОтправьте одной строкой:\nhttps://t.me/nft/SnoopCigar-84846 2300 ru\n\nПоследний параметр: ru, en или zh.",
        "help": "Бот работает в подключённых чатах Telegram Business. Отправьте в чате с продавцом одну строку:\n<code>https://t.me/nft/SnoopCigar-84846 2300 ru</code>\n\nСначала подключите бота в Telegram → Настройки → Telegram для бизнеса → Чат-боты и разрешите ему получать и отправлять сообщения."
    },
    "en": {
        "offer": "{seller} is interested in purchasing this gift\n<a href=\"{url}\">{gift}</a>.\n\nOffer amount: <b>{amount}⭐️</b>\nAccept the offer if these terms work for you.",
        "accept": "Accept",
        "decline": "Decline",
        "accepted": "The seller accepted the offer for {gift} at {amount}⭐️.\n\nThis confirms the seller's acceptance only. It does not reserve or transfer Stars. Verify the terms and payment independently before transferring the gift.",
        "declined": "❌ The seller declined the offer for {gift}.",
        "transfer_link": "Transfer the gift",
        "confirm_transfer": "Confirm transfer",
        "transfer_marked": "A participant marked the gift as transferred. The bot does not verify NFT ownership or payment; verify both independently.",
        "buyer_accept": "You accepted the offer. Transfer the gift only after independently checking the terms and payment. This bot does not hold or automatically transfer Stars.",
        "buyer_decline": "You declined the offer.",
        "expired": "This offer has already been processed.",
        "bad": "I couldn't parse that line.\nSend one line in this format:\nhttps://t.me/nft/SnoopCigar-84846 2300 en\n\nLanguage: ru, en, or zh.",
        "help": "Use this bot in a connected Telegram Business chat. Send one line in the seller's chat:\n<code>https://t.me/nft/SnoopCigar-84846 2300 en</code>\n\nConnect the bot in Telegram → Settings → Telegram Business → Chatbots and grant permission to receive and send messages."
    },
    "zh": {
        "offer": "{seller} 有意购买这份礼物\n<a href=\"{url}\">{gift}</a>。\n\n报价：<b>{amount}⭐️</b>\n如果您接受此条件，请确认。",
        "accept": "接受",
        "decline": "拒绝",
        "accepted": "卖家已接受 {gift} 的报价：{amount}⭐️。\n\n这仅表示卖家接受报价，并不代表 Stars 已被托管或转账。转移礼物前，请自行核实付款和交易条件。",
        "declined": "❌ 卖家拒绝了 {gift} 的报价。",
        "transfer_link": "转移礼物",
        "confirm_transfer": "确认已转移",
        "transfer_marked": "参与者已标记礼物转移。机器人无法验证 NFT 所有权或付款，请自行核实。",
        "buyer_accept": "您已接受报价。请先自行核实交易条件和付款，再转移礼物。本机器人不会托管或自动转账 Stars。",
        "buyer_decline": "您已拒绝报价。",
        "expired": "此报价已处理。",
        "bad": "无法识别格式。\n请发送一行，格式如下：\nhttps://t.me/nft/SnoopCigar-84846 2300 zh\n\n语言代码：ru、en 或 zh。",
        "help": "请在已连接的 Telegram Business 聊天中使用。向卖家发送一行：\n<code>https://t.me/nft/SnoopCigar-84846 2300 zh</code>\n\n在 Telegram → 设置 → Telegram Business → 聊天机器人中连接本机器人，并授予接收和发送消息的权限。"
    }
}

def db():
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
            created_at INTEGER NOT NULL
        )
    """)
    conn.commit()
    return conn

def api(method, payload=None):
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is not set")
    body = json.dumps(payload or {}).encode("utf-8")
    req = Request(API + method, data=body, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=40) as response:
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"Telegram API HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Telegram API connection error: {exc}") from exc
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
    return data.get("result")

def send_message(chat_id, text, connection_id=None, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    if connection_id:
        payload["business_connection_id"] = connection_id
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return api("sendMessage", payload)

def parse_offer(text):
    parts = (text or "").strip().split()
    if len(parts) != 3:
        return None
    url, amount_text, language = parts
    language = language.lower()
    if language not in TEXTS:
        return None
    if not re.fullmatch(r"[1-9][0-9]{0,7}", amount_text):
        return None
    if amount_text.startswith("0"):
        return None
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc.lower() not in ("t.me", "www.t.me"):
        return None
    match = re.fullmatch(r"/nft/([A-Za-z0-9_-]+)", parsed.path)
    if not match:
        return None
    gift_name = match.group(1).replace("-", " ")
    return url, int(amount_text), language, gift_name

def parse_gift_url(value):
    parsed = urlparse((value or "").strip())
    if parsed.scheme != "https" or parsed.netloc.lower() not in ("t.me", "www.t.me"):
        return None
    match = re.fullmatch(r"/nft/([A-Za-z0-9_-]+)", parsed.path)
    if not match:
        return None
    return (value.strip(), match.group(1).replace("-", " "))

def create_offer(message, connection_id, gift_url, amount, language, gift_name):
    chat = message.get("chat", {})
    seller = html.escape(chat.get("first_name") or chat.get("title") or "Продавец")
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
        seller=seller, url=html.escape(gift_url, quote=True),
        gift=safe_gift, amount=amount
    )
    sent = send_message(chat["id"], text_out, connection_id, markup)
    with db() as conn:
        conn.execute(
            """INSERT INTO offers
            (id, connection_id, chat_id, message_id, gift_url, gift_name, amount, language, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (offer_id, connection_id, chat["id"], sent["message_id"],
             gift_url, gift_name, amount, language, int(time.time()))
        )
        conn.commit()
    log.info("Offer %s created in chat %s", offer_id, chat["id"])

def handle_business_message(message):
    # Only the connected account owner may start an offer flow.
    connection_id = message.get("business_connection_id")
    text = (message.get("text") or "").strip()
    sender = message.get("from") or {}
    if not connection_id or not text or message.get("sender_business_bot"):
        return

    try:
        connection = api("getBusinessConnection", {"business_connection_id": connection_id})
    except Exception:
        log.exception("Could not verify Business connection")
        return

    owner_id = (connection.get("user") or {}).get("id")
    if not owner_id or sender.get("id") != owner_id:
        return
    if not connection.get("is_enabled", False):
        log.warning("Business connection is disabled")
        return

    chat = message.get("chat", {})
    if chat.get("type") != "private":
        return
    chat_id = chat.get("id")
    key = f"{connection_id}:{chat_id}"

    # Keep the old compact syntax working: URL amount language.
    parsed = parse_offer(text)
    if parsed:
        gift_url, amount, language, gift_name = parsed
        create_offer(message, connection_id, gift_url, amount, language, gift_name)
        return

    # New step-by-step syntax: URL -> amount -> language.
    gift = parse_gift_url(text)
    if gift:
        gift_url, gift_name = gift
        with db() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS offer_drafts (
                    draft_key TEXT PRIMARY KEY,
                    connection_id TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    gift_url TEXT NOT NULL,
                    gift_name TEXT NOT NULL,
                    amount INTEGER,
                    stage TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                )
            """)
            conn.execute("""
                INSERT INTO offer_drafts
                (draft_key, connection_id, chat_id, gift_url, gift_name, amount, stage, updated_at)
                VALUES (?, ?, ?, ?, ?, NULL, 'amount', ?)
                ON CONFLICT(draft_key) DO UPDATE SET
                    gift_url=excluded.gift_url, gift_name=excluded.gift_name,
                    amount=NULL, stage='amount', updated_at=excluded.updated_at
            """, (key, connection_id, chat_id, gift_url, gift_name, int(time.time())))
            conn.commit()
        send_message(chat_id, "Ссылка на NFT получена. Теперь отправь сумму в Stars отдельным сообщением, например: 2300", connection_id)
        return

    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS offer_drafts (
                draft_key TEXT PRIMARY KEY,
                connection_id TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                gift_url TEXT NOT NULL,
                gift_name TEXT NOT NULL,
                amount INTEGER,
                stage TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )
        """)
        draft = conn.execute("SELECT * FROM offer_drafts WHERE draft_key=?", (key,)).fetchone()

    if not draft:
        if "t.me/nft/" in text:
            send_message(chat_id, "Не удалось распознать ссылку. Отправь прямую ссылку вида https://t.me/nft/SnoopCigar-84846", connection_id)
        return

    if draft["stage"] == "amount":
        if not re.fullmatch(r"[1-9][0-9]{0,7}", text):
            send_message(chat_id, "Укажи сумму только цифрами, например: 2300", connection_id)
            return
        amount = int(text)
        with db() as conn:
            conn.execute("UPDATE offer_drafts SET amount=?, stage='language', updated_at=? WHERE draft_key=?",
                         (amount, int(time.time()), key))
            conn.commit()
        send_message(chat_id, "Сумма получена. Теперь отправь язык оффера: ru, en или zh", connection_id)
        return

    if draft["stage"] == "language":
        language = text.lower()
        if language not in TEXTS:
            send_message(chat_id, "Укажи язык одним из вариантов: ru, en или zh", connection_id)
            return
        create_offer(message, connection_id, draft["gift_url"], int(draft["amount"]), language, draft["gift_name"])
        with db() as conn:
            conn.execute("DELETE FROM offer_drafts WHERE draft_key=?", (key,))
            conn.commit()

def handle_callback(query):
    match = re.fullmatch(r"(accept|decline|transfer):([a-f0-9]{12})", query.get("data", ""))
    if not match:
        api("answerCallbackQuery", {"callback_query_id": query["id"], "text": "Unknown action"})
        return

    action, offer_id = match.groups()
    msg = query.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    with db() as conn:
        offer = conn.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
        if not offer or int(chat_id or 0) != int(offer["chat_id"]):
            api("answerCallbackQuery", {"callback_query_id": query["id"], "text": "Offer not found"})
            return
        strings = TEXTS[offer["language"]]
        status = offer["status"]
        if action in ("accept", "decline") and status != "pending":
            api("answerCallbackQuery", {"callback_query_id": query["id"], "text": strings["expired"], "show_alert": True})
            return
        if action == "transfer" and status != "accepted":
            api("answerCallbackQuery", {"callback_query_id": query["id"], "text": strings["expired"], "show_alert": True})
            return

        if action == "accept":
            conn.execute("UPDATE offers SET status='accepted' WHERE id=? AND status='pending'", (offer_id,))
            body = strings["accepted"].format(gift=html.escape(offer["gift_name"]), amount=offer["amount"])
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
        api("editMessageText", {
            "business_connection_id": offer["connection_id"],
            "chat_id": offer["chat_id"],
            "message_id": msg.get("message_id") or offer["message_id"],
            "text": body, "parse_mode": "HTML", "disable_web_page_preview": True,
            "reply_markup": keyboard
        })
    except Exception:
        log.exception("Could not update offer message %s", offer_id)

def process_update(update):
    if "business_message" in update:
        handle_business_message(update["business_message"])
    elif "callback_query" in update:
        handle_callback(update["callback_query"])
    elif "business_connection" in update:
        item = update["business_connection"]
        log.info("Business connection updated: enabled=%s", item.get("is_enabled"))

def polling_loop():
    offset = None
    allowed = ["business_connection", "business_message", "callback_query"]
    while True:
        try:
            payload = {"timeout": 30, "allowed_updates": allowed}
            if offset is not None:
                payload["offset"] = offset
            updates = api("getUpdates", payload)
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    process_update(update)
                except Exception:
                    log.exception("Failed to process update %s", update.get("update_id"))
        except Exception:
            log.exception("Polling error")
            time.sleep(4)

class HealthHandler(BaseHTTPRequestHandler):
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
    threading.Thread(target=polling_loop, daemon=True).start()
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    log.info("Health server listening on port %s", PORT)
    server.serve_forever()

if __name__ == "__main__":
    main()
