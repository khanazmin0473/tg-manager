import os
import json
import hmac
import hashlib
import logging
from urllib.parse import parse_qsl
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
import uvicorn

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters,
)
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
log = logging.getLogger("tg-manager")

load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "").strip()
MINIAPP_URL = os.environ.get("MINIAPP_URL", "").strip()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SESSIONS_FILE = os.path.join(BASE_DIR, "sessions.json")
STATIC_DIR = os.path.join(BASE_DIR, "static")

pending = {}          # user_id -> {"phone", "client", "phone_code_hash", "waiting_password"}
active_account = {}   # telegram_user_id -> account_key
accounts = {}         # account_key -> {"phone": str, "session": str}
clients = {}          # account_key -> TelegramClient
ptb_app = None


def load_accounts():
    global accounts
    if os.path.exists(SESSIONS_FILE):
        try:
            with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
                accounts = json.load(f)
        except Exception as e:
            log.error(f"Error loading {SESSIONS_FILE}: {e}")
            accounts = {}


def save_accounts():
    try:
        with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(accounts, f, indent=2)
    except Exception as e:
        log.error(f"Error saving {SESSIONS_FILE}: {e}")


def is_allowed(user_id: int) -> bool:
    return bool(ALLOWED_USER_ID and user_id == ALLOWED_USER_ID)


def validate_init_data(init_data: str) -> int | None:
    if not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        if "hash" not in pairs:
            return None
        received_hash = pairs.pop("hash")
        data_check_string = "\n".join(
            f"{k}={v}" for k, v in sorted(pairs.items())
        )
        secret = hmac.new(
            b"WebAppData",
            BOT_TOKEN.encode(),
            hashlib.sha256,
        ).digest()

        calc = hmac.new(
            secret,
            data_check_string.encode(),
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(calc, received_hash):
            return None

        user = json.loads(pairs["user"])
        return int(user["id"])
    except Exception as e:
        log.warning(f"InitData validation failed: {e}")
        return None


def get_client(account_key: str) -> TelegramClient:
    if account_key in clients:
        return clients[account_key]

    session_string = accounts[account_key]["session"]
    client = TelegramClient(
        StringSession(session_string),
        API_ID,
        API_HASH,
    )
    clients[account_key] = client
    return client


async def ensure_connected(account_key: str) -> TelegramClient:
    client = get_client(account_key)
    if not client.is_connected():
        await client.connect()
    return client


def finish_login(user_id: int, state: dict) -> str:
    client = state["client"]
    session_string = client.session.save()

    # Generate next collision-free key
    i = 1
    while f"acc_{i}" in accounts:
        i += 1
    account_key = f"acc_{i}"

    accounts[account_key] = {
        "phone": state["phone"],
        "session": session_string,
    }

    save_accounts()
    clients[account_key] = client
    active_account[user_id] = account_key
    pending.pop(user_id, None)
    return account_key


# ---------------- BOT HANDLERS ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not is_allowed(user_id):
        await update.message.reply_text("⛔ এই বট শুধু ওনারের (Owner) জন্য সংরক্ষিত।")
        return

    keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton("📱 Share your contact", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )

    msg = (
        "👋 **Telegram Manager-এ স্বাগতম!**\n\n"
        "একাউন্ট লগইন করতে:\n"
        "1️⃣ নিচের Mini App খুলুন\n"
        "2️⃣ যেকোনো ফোন নম্বর দিন (বা কন্ট্যাক্ট শেয়ার করুন)\n"
        "3️⃣ Telegram থেকে পাওয়া OTP দিন\n"
        "4️⃣ সফলভাবে লগইন সম্পন্ন হবে!"
    )
    await update.message.reply_text(msg, reply_markup=keyboard, parse_mode="Markdown")

    if MINIAPP_URL:
        await update.message.reply_text(
            "👇 Mini App খুলতে নিচের বাটনে চাপ দিন:",
            reply_markup={
                "inline_keyboard": [[
                    {"text": "🚀 Open Mini App", "web_app": {"url": MINIAPP_URL}}
                ]]
            },
        )


async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not is_allowed(user_id):
        return

    contact = update.message.contact
    if not contact:
        return

    phone = contact.phone_number
    if not phone.startswith("+"):
        phone = "+" + phone

    # Disconnect previous pending client if any
    if user_id in pending and pending[user_id].get("client"):
        try:
            await pending[user_id]["client"].disconnect()
        except Exception:
            pass

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()

    try:
        sent = await client.send_code_request(phone)
        pending[user_id] = {
            "phone": phone,
            "client": client,
            "phone_code_hash": sent.phone_code_hash,
            "waiting_password": False,
        }
        await update.message.reply_text(
            f"✅ OTP পাঠানো হয়েছে: {phone}\n\nএখন Mini App-এ গিয়ে কোডটি দিন।"
        )
    except Exception as e:
        log.exception("send_code failed from contact")
        try:
            await client.disconnect()
        except Exception:
            pass
        await update.message.reply_text(f"❌ কোড পাঠানো যায়নি: {e}")


async def accounts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return

    if not accounts:
        await update.message.reply_text("কোনো account login করা নেই।")
        return

    lines = ["📋 **যুক্ত একাউন্টসমূহ:**"]
    for key, data in accounts.items():
        active = "✅ (Active)" if active_account.get(user_id) == key else "▫️"
        lines.append(f"{active} `{key}` — {data['phone']}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def use_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return

    if not context.args:
        await update.message.reply_text("ব্যবহার: `/use acc_1`", parse_mode="Markdown")
        return

    key = context.args[0].strip()
    if key not in accounts:
        await update.message.reply_text("❌ এই একাউন্ট পাওয়া যায়নি।")
        return

    active_account[user_id] = key
    await update.message.reply_text(f"✅ `{key}` সিলেক্ট করা হয়েছে।", parse_mode="Markdown")


async def logout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return

    if not context.args:
        await update.message.reply_text("ব্যবহার: `/logout acc_1`", parse_mode="Markdown")
        return

    key = context.args[0].strip()
    if key not in accounts:
        await update.message.reply_text("❌ এই একাউন্ট পাওয়া যায়নি।")
        return

    client = clients.pop(key, None)
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass

    accounts.pop(key, None)
    save_accounts()

    if active_account.get(user_id) == key:
        active_account.pop(user_id, None)

    await update.message.reply_text(f"✅ `{key}` সফলভাবে logout করা হয়েছে।", parse_mode="Markdown")


# ---------------- FASTAPI LIFESPAN & APP ----------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global ptb_app
    load_accounts()
    log.info(f"Loaded {len(accounts)} accounts from sessions.json")

    # Start Telegram Bot inside the same event loop
    if BOT_TOKEN:
        ptb_app = ApplicationBuilder().token(BOT_TOKEN).build()
        ptb_app.add_handler(CommandHandler("start", start))
        ptb_app.add_handler(CommandHandler("accounts", accounts_cmd))
        ptb_app.add_handler(CommandHandler("use", use_cmd))
        ptb_app.add_handler(CommandHandler("logout", logout_cmd))
        ptb_app.add_handler(MessageHandler(filters.CONTACT, handle_contact))

        await ptb_app.initialize()
        await ptb_app.start()
        await ptb_app.updater.start_polling(drop_pending_updates=True)
        log.info("Telegram Bot Polling started smoothly in lifespan.")
    else:
        log.warning("BOT_TOKEN is missing, bot polling not started.")

    yield

    log.info("Shutting down Application...")
    if ptb_app and ptb_app.updater and ptb_app.updater.running:
        await ptb_app.updater.stop()
    if ptb_app:
        await ptb_app.stop()
        await ptb_app.shutdown()

    for k, client in list(clients.items()):
        try:
            if client.is_connected():
                await client.disconnect()
        except Exception:
            pass


api = FastAPI(lifespan=lifespan)


# ---------------- MINI APP API ENDPOINTS ----------------

@api.get("/")
async def home():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return JSONResponse({"status": "running", "service": "Telegram Manager API"})


@api.post("/api/session")
async def get_session(request: Request):
    body = await request.json()
    user_id = validate_init_data(body.get("initData", ""))

    if not user_id or not is_allowed(user_id):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    state = pending.get(user_id)

    # Set default active account if none selected
    if user_id not in active_account and accounts:
        active_account[user_id] = list(accounts.keys())[0]

    return {
        "pending": bool(state),
        "phone": state.get("phone") if state else None,
        "waiting_password": bool(state and state.get("waiting_password")),
        "accounts": [
            {"key": k, "phone": v["phone"]}
            for k, v in accounts.items()
        ],
        "active": active_account.get(user_id),
    }


@api.post("/api/send-code")
async def api_send_code(request: Request):
    body = await request.json()
    user_id = validate_init_data(body.get("initData", ""))
    if not user_id or not is_allowed(user_id):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    phone = (body.get("phone") or "").strip().replace(" ", "").replace("-", "")
    if not phone:
        return JSONResponse({"error": "ফোন নম্বর লিখুন"}, status_code=400)
    if not phone.startswith("+"):
        phone = "+" + phone

    if user_id in pending and pending[user_id].get("client"):
        try:
            await pending[user_id]["client"].disconnect()
        except Exception:
            pass

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()

    try:
        sent = await client.send_code_request(phone)
        pending[user_id] = {
            "phone": phone,
            "client": client,
            "phone_code_hash": sent.phone_code_hash,
            "waiting_password": False,
        }
        log.info(f"OTP code request sent successfully to {phone}")
        return {"status": "ok", "phone": phone}
    except Exception as e:
        log.exception(f"send_code_request failed for {phone}")
        try:
            await client.disconnect()
        except Exception:
            pass
        return JSONResponse({"error": str(e)}, status_code=400)


@api.post("/api/otp")
async def verify_otp(request: Request):
    body = await request.json()
    user_id = validate_init_data(body.get("initData", ""))
    otp = (body.get("otp") or "").strip()

    if not user_id or not is_allowed(user_id):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    state = pending.get(user_id)
    if not state:
        return JSONResponse({"error": "কোনো লগইন অপেক্ষারত নেই। প্রথমে নম্বর দিয়ে কোড পাঠান।"}, status_code=400)

    client = state["client"]

    try:
        await client.sign_in(
            phone=state["phone"],
            code=otp,
            phone_code_hash=state["phone_code_hash"],
        )
    except SessionPasswordNeededError:
        state["waiting_password"] = True
        return {"status": "need_password"}
    except Exception as e:
        log.exception("OTP verification failed")
        return JSONResponse({"error": str(e)}, status_code=400)

    account_key = finish_login(user_id, state)
    return {"status": "ok", "account": account_key}


@api.post("/api/password")
async def verify_password(request: Request):
    body = await request.json()
    user_id = validate_init_data(body.get("initData", ""))
    password = body.get("password") or ""

    if not user_id or not is_allowed(user_id):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    state = pending.get(user_id)
    if not state or not state.get("waiting_password"):
        return JSONResponse({"error": "2FA পাসওয়ার্ড অপেক্ষারত নেই।"}, status_code=400)

    client = state["client"]

    try:
        await client.sign_in(password=password)
    except Exception as e:
        log.exception("2FA verification failed")
        return JSONResponse({"error": str(e)}, status_code=400)

    account_key = finish_login(user_id, state)
    return {"status": "ok", "account": account_key}


@api.post("/api/cancel-login")
async def cancel_login(request: Request):
    body = await request.json()
    user_id = validate_init_data(body.get("initData", ""))
    if not user_id or not is_allowed(user_id):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    if user_id in pending:
        try:
            c = pending[user_id].get("client")
            if c and c.is_connected():
                await c.disconnect()
        except Exception:
            pass
        pending.pop(user_id, None)
    return {"status": "ok"}


@api.post("/api/select")
async def select_account(request: Request):
    body = await request.json()
    user_id = validate_init_data(body.get("initData", ""))
    key = body.get("key")

    if not user_id or not is_allowed(user_id):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    if key not in accounts:
        return JSONResponse({"error": "একাউন্ট পাওয়া যায়নি"}, status_code=404)

    active_account[user_id] = key
    return {"status": "ok", "active": key}


@api.post("/api/inbox")
async def inbox(request: Request):
    body = await request.json()
    user_id = validate_init_data(body.get("initData", ""))

    if not user_id or not is_allowed(user_id):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    key = active_account.get(user_id)
    if not key or key not in accounts:
        return JSONResponse({"error": "কোনো একাউন্ট সিলেক্ট করা নেই"}, status_code=400)

    try:
        client = await ensure_connected(key)
        dialogs = await client.get_dialogs(limit=25)

        chats = []
        for d in dialogs:
            last_text = ""
            if d.message and d.message.message:
                last_text = d.message.message[:60]
            chats.append({
                "id": d.id,
                "name": d.name or str(d.id),
                "unread": d.unread_count or 0,
                "last_message": last_text,
            })
        return {"chats": chats, "account": key}
    except Exception as e:
        log.exception("inbox fetch failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@api.post("/api/send")
async def send_message(request: Request):
    body = await request.json()
    user_id = validate_init_data(body.get("initData", ""))
    chat_id = body.get("chat_id")
    text = (body.get("text") or "").strip()

    if not user_id or not is_allowed(user_id):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    if not chat_id or not text:
        return JSONResponse({"error": "chat_id এবং text আবশ্যক"}, status_code=400)

    key = active_account.get(user_id)
    if not key or key not in accounts:
        return JSONResponse({"error": "কোনো একাউন্ট সিলেক্ট করা নেই"}, status_code=400)

    try:
        client = await ensure_connected(key)
        entity = await client.get_input_entity(int(chat_id))
        await client.send_message(entity, text)
        return {"status": "ok"}
    except Exception as e:
        log.exception("send_message failed")
        return JSONResponse({"error": str(e)}, status_code=400)


# ---------------- MAIN ENTRYPOINT ----------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:api", host="0.0.0.0", port=port, reload=False)
