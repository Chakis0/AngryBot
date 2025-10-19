# --- imports ---
import os
import uuid
import hashlib
import requests
from fastapi import FastAPI, Request, HTTPException, Header
import telebot
from telebot import types
import json
from pathlib import Path

# --- env ---
PUBLIC_BASE_URL    = os.getenv("PUBLIC_BASE_URL", "https://alexabot-kg4y.onrender.com")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
MERCHANT_ID        = os.getenv("MERCHANT_ID", "")
SECRET_KEY         = os.getenv("SECRET_KEY", "")
TG_WEBHOOK_SECRET  = os.getenv("TG_WEBHOOK_SECRET", "")

# --- init app & bot (ВАЖНО: app создаём ДО декораторов @app.*) ---
app = FastAPI()
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, threaded=False)

# Память: последняя ссылка на оплату для каждого chat_id
# chat_id -> { "message_id": int, "order_id": str, "base_text": str }
last_link_msg = {}

# --- helpers ---
# === ACCESS CONTROL (постоянные + динамические) ===
BASE_WHITELIST = {958579430, 8051914154, 2095741832, 7167283179, 959967956}

WHITELIST_FILE = Path("whitelist.json")

def load_dynamic_whitelist() -> set[int]:
    if WHITELIST_FILE.exists():
        try:
            with open(WHITELIST_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return set(int(x) for x in data)
        except Exception:
            return set()
    return set()

def save_dynamic_whitelist(ids: set[int]) -> None:
    with open(WHITELIST_FILE, "w", encoding="utf-8") as f:
        json.dump(list(ids), f)

DYNAMIC_WHITELIST: set[int] = load_dynamic_whitelist()

def has_access(chat_id: int) -> bool:
    return (chat_id in BASE_WHITELIST) or (chat_id in DYNAMIC_WHITELIST)

def fmt_rub(amount_int: int) -> str:
    # Красиво форматируем 1000 -> "1 000"
    return f"{amount_int:,}".replace(",", " ")

# --- commands ---
@bot.message_handler(commands=['getid'])
def getid(message):
    bot.send_message(message.chat.id, f"Твой chat_id: {message.chat.id}")

@bot.message_handler(commands=['info'])
def info(message):
    if not has_access(message.chat.id):
        bot.send_message(message.chat.id, "⛔ У вас нет доступа")
        return

    if message.chat.id not in last_link_msg:
        bot.send_message(message.chat.id, "⚠️ Нет последнего платежа для редактирования")
        return

    try:
        raw = message.text[len("/info"):].strip()
        # Собираем блок с разделителями
        if "|" not in raw:
            # Просто текст → единый комментарий
            extra_block = f"\n────────────────\nКомментарий:\n{raw}\n────────────────"
        else:
            parts = [p.strip() for p in raw.split("|")]
            trader  = parts[0] if len(parts) > 0 else ""
            details = parts[1] if len(parts) > 1 else ""
            tm      = parts[2] if len(parts) > 2 else ""
            amt     = parts[3] if len(parts) > 3 else ""

            lines = []
            if trader:  lines.append(f"Трейдер: {trader}")
            if details: lines.append(f"Реквизит: {details}")
            if tm:      lines.append(f"Время: {tm}")
            if amt:     lines.append(f"Сумма: {amt}")

            body = "\n".join(lines) if lines else "(нет данных)"
            extra_block = f"\n────────────────\n{body}\n────────────────"

        new_text = last_link_msg[message.chat.id]["base_text"] + extra_block

        bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=last_link_msg[message.chat.id]["message_id"],
            text=new_text,
            disable_web_page_preview=True
        )
    except Exception as e:
        bot.send_message(
            message.chat.id,
            f"⚠️ Ошибка: {e}\n\n"
            "Форматы:\n"
            "/info свободный текст\n"
            "/info трейдер | реквизит | время | сумма"
        )

# --- admin: add/delete ---
@bot.message_handler(commands=['add'])
def add_user(message):
    if message.chat.id not in BASE_WHITELIST:
        bot.send_message(message.chat.id, "⛔ У тебя нет прав")
        return
    parts = message.text.strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        bot.send_message(message.chat.id, "⚠️ Используй: /add <chat_id>")
        return
    new_id = int(parts[1])
    DYNAMIC_WHITELIST.add(new_id)
    save_dynamic_whitelist(DYNAMIC_WHITELIST)
    bot.send_message(message.chat.id, f"✅ Пользователь {new_id} добавлен")

@bot.message_handler(commands=['delete'])
def delete_user(message):
    if message.chat.id not in BASE_WHITELIST:
        bot.send_message(message.chat.id, "⛔ У тебя нет прав")
        return
    parts = message.text.strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        bot.send_message(message.chat.id, "⚠️ Используй: /delete <chat_id>")
        return
    del_id = int(parts[1])
    if del_id in DYNAMIC_WHITELIST:
        DYNAMIC_WHITELIST.remove(del_id)
        save_dynamic_whitelist(DYNAMIC_WHITELIST)
        bot.send_message(message.chat.id, f"🚫 Пользователь {del_id} удалён")
    else:
        bot.send_message(message.chat.id, "⚠️ Такого chat_id нет среди добавленных")

# --- helpers ---
def tg_send(chat_id: int, text: str):
    if not TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": int(chat_id), "text": text}, timeout=10)
    except Exception:
        pass

# --- core: создание платежа ---
def create_payment_core(amount: int, chat_id: int, currency: str = "RUB"):
    if currency == "RUB":
        if amount < 200 or amount > 85000:
            raise HTTPException(400, "Amount must be between 200 and 85000 RUB")
        amount_minor = amount * 100
    elif currency == "USD":
        if amount < 10 or amount > 990:
            raise HTTPException(400, "Amount must be between 10 and 990 USD")
        amount_minor = amount * 100
    else:
        raise HTTPException(400, "Unsupported currency")

    order_id = f"{chat_id}-{uuid.uuid4().hex[:8]}"
    uniq = uuid.uuid4().hex[:4]
    customer_id = f"u{chat_id}{uniq}"

    payload = {
        "merchant_id": MERCHANT_ID,
        "secret":      SECRET_KEY,
        "order_id":    order_id,
        "customer":    customer_id,
        "account":     customer_id,
        "amount":      amount_minor,
        "currency":    currency,
        "description": "Top up from Telegram bot",
    }

    try:
        r = requests.post("https://nicepay.io/public/api/payment", json=payload, timeout=25)
        data = r.json()
    except Exception as e:
        raise HTTPException(502, f"Nicepay request failed: {e}")

    if data.get("status") == "success":
        link = (data.get("data") or {}).get("link")
        if not link:
            raise HTTPException(502, "Nicepay success without link")
        return {"payment_link": link, "order_id": order_id}
    else:
        msg = (data.get("data") or {}).get("message", "Unknown Nicepay error")
        raise HTTPException(400, f"Nicepay error: {msg}")

# --- telegram flow ---
@bot.message_handler(commands=['start'])
def start(message):
    if not has_access(message.chat.id):
        bot.send_message(message.chat.id, "⛔ У вас нет доступа")
        return
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("Оплатить", callback_data="pay_custom"))
    kb.add(types.InlineKeyboardButton("Проснись", callback_data="wake_up"))
    bot.send_message(message.chat.id, "Нажми «Оплатить», затем введи сумму (200–85000 ₽).", reply_markup=kb)

@bot.callback_query_handler(func=lambda call: True)
def callback(call):
    if not has_access(call.message.chat.id):
        bot.answer_callback_query(call.id, "⛔ У вас нет доступа")
        return

    if call.data == "wake_up":
        bot.answer_callback_query(call.id, "Я на связи ✅")
        return

    if call.data == "pay_custom":
        msg = bot.send_message(call.message.chat.id, "Введи сумму в рублях (200–85000):")
        bot.register_next_step_handler(msg, handle_custom_amount)
        return

def handle_custom_amount(message):
    if not has_access(message.chat.id):
        bot.send_message(message.chat.id, "⛔ У вас нет доступа")
        return
    try:
        amt = int(message.text.strip())
        if amt < 200 or amt > 85000:
            bot.send_message(message.chat.id, "Сумма вне лимитов Nicepay (200–85000 ₽).")
            return
        result = create_payment_core(amt, message.chat.id, "RUB")
        link = result.get("payment_link")
        oid  = result.get("order_id")

        # Сообщение со ссылкой: сумма в скобках, без Order ID, с разделителем
        text = (
            f"💳 Ссылка на оплату ({fmt_rub(amt)} ₽):\n{link}\n\n"
            f"────────────────"
        )
        msg = bot.send_message(message.chat.id, text, disable_web_page_preview=True)

        # Сохраняем последнее сообщение (для /info)
        last_link_msg[message.chat.id] = {
            "message_id": msg.message_id,
            "order_id": oid,
            "base_text": text
        }
    except ValueError:
        bot.send_message(message.chat.id, "Введите целое число без копеек.")
    except Exception as e:
        bot.send_message(message.chat.id, f"Ошибка при создании платежа ❌\n{e}")

# --- webhook endpoints ---
@app.post("/tg-webhook")
async def tg_webhook(request: Request, x_telegram_bot_api_secret_token: str = Header(None)):
    if TG_WEBHOOK_SECRET and x_telegram_bot_api_secret_token != TG_WEBHOOK_SECRET:
        return {"ok": True}
    try:
        payload = await request.body()
        update = telebot.types.Update.de_json(payload.decode("utf-8"))
        bot.process_new_updates([update])
    except Exception as e:
        print("TG webhook error:", e)
    return {"ok": True}

@app.get("/webhook")
async def nicepay_webhook(request: Request):
    params = dict(request.query_params)
    received_hash = params.pop("hash", None)
    if not received_hash:
        raise HTTPException(400, "hash missing")

    base = "{np}".join([v for _, v in sorted(params.items(), key=lambda x: x[0])] + [SECRET_KEY])
    calc_hash = hashlib.sha256(base.encode()).hexdigest()
    if calc_hash != received_hash:
        raise HTTPException(400, "bad hash")

    result   = params.get("result")
    order_id = params.get("order_id", "")
    amount_str = params.get("amount", "0")
    amount_cur = params.get("amount_currency", "")
    profit_str = params.get("profit")
    profit_cur = params.get("profit_currency")

    def minor_to_human(x: str, cur: str) -> str:
        try:
            val = int(x)
        except Exception:
            return x
        if cur in ("RUB", "USD", "USDT"):
            return f"{val/100:.2f}"
        return str(val)

    amount_human = minor_to_human(amount_str, amount_cur)
    profit_human = minor_to_human(profit_str, profit_cur) if profit_str is not None else None

    chat_id = order_id.split("-", 1)[0] if "-" in order_id else None

    if result == "success" and chat_id:
        if profit_human and profit_cur:
            text = f"✅ Оплата подтверждена. Сумма: {amount_human} {amount_cur} (на счёт: {profit_human} {profit_cur})"
        else:
            text = f"✅ Оплата подтверждена. Сумма: {amount_human} {amount_cur}"
        tg_send(chat_id, text)

    return {"ok": True}

@app.get("/create_payment")
def create_payment(amount: int, chat_id: int, currency: str = "RUB"):
    return create_payment_core(amount, chat_id, currency)

@app.get("/health")
def health():
    return {"ok": True}
