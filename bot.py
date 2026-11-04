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
        "declined": "Продавец отклонил предложение по подарку {gift}.",
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
        "declined": "The seller declined the offer for {gift}.",
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
        "declined": "卖家拒绝了 {gift} 的报价。",
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

def handle_business_message(message):
    # Only act on messages sent from a connected Business account.
    connection_id = message.get("business_connection_id")
    text = message.get("text")
    if not connection_id or not text or message.get("sender_business_bot"):
        return
    parsed = parse_offer(text)
    if not parsed:
        # Avoid replying to every ordinary chat message; only explain on likely NFT commands.
        if "t.me/nft/" in text:
            send_message(message["chat"]["id"], TEXTS["ru"]["bad"], connection_id)
        return

    gift_url, amount, language, gift_name = parsed
    chat = message.get("chat", {})
    seller = chat.get("first_name") or chat.get("title") or "Продавец"
    seller = html.escape(seller)
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

def handle_callback(query):
    data = query.get("data", "")
    match = re.fullmatch(r"(accept|decline):([a-f0-9]{12})", data)
    if not match:
        api("answerCallbackQuery", {"callback_query_id": query["id"], "text": "Unknown action"})
        return
    action, offer_id = match.groups()
    with db() as conn:
        offer = conn.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
        if not offer:
            api("answerCallbackQuery", {"callback_query_id": query["id"], "text": "Offer not found"})
            return
        if offer["status"] != "pending":
            api("answerCallbackQuery", {
                "callback_query_id": query["id"],
                "text": TEXTS[offer["language"]]["expired"],
                "show_alert": True
            })
            return
        status = "accepted" if action == "accept" else "declined"
        conn.execute("UPDATE offers SET status=? WHERE id=? AND status='pending'", (status, offer_id))
        conn.commit()

    api("answerCallbackQuery", {"callback_query_id": query["id"]})
    strings = TEXTS[offer["language"]]
    if action == "accept":
        seller_text = strings["buyer_accept"]
        buyer_text = strings["accepted"].format(
            gift=html.escape(offer["gift_name"]), amount=offer["amount"]
        )
    else:
        seller_text = strings["buyer_decline"]
        buyer_text = strings["declined"].format(gift=html.escape(offer["gift_name"]))

    msg = query.get("message") or {}
    connection_id = msg.get("business_connection_id") or offer["connection_id"]
    try:
        send_message(offer["chat_id"], seller_text, connection_id)
    except Exception:
        log.exception("Could not send seller status for offer %s", offer_id)

    # The callback user is the seller who pressed the button. Send the status
    # to the connected Business chat; do not pretend that any Stars were escrowed.
    log.info("Offer %s changed to %s", offer_id, status)
    try:
        send_message(offer["chat_id"], buyer_text, connection_id)
    except Exception:
        log.exception("Could not send offer status for %s", offer_id)

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
