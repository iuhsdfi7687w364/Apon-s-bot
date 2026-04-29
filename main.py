import os
import re
import json
import threading
import logging
import requests
from dotenv import load_dotenv
from faker import Faker
from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import CallbackQueryHandler

import gspread
from google.oauth2.service_account import Credentials

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    CallbackQueryHandler,
)

load_dotenv()

ENV_FILE = ".env"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
COMMON_PASSWORD = os.getenv("COMMON_PASSWORD", "Password123")
BOT_ENABLED = True

logging.basicConfig(level=logging.INFO)
fake = Faker()

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def extract_email_only(text: str):
    if not text:
        return None
    match = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    return match.group(0) if match else None


def get_sheet():
    if GOOGLE_CREDENTIALS_JSON:
        info = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file(
            GOOGLE_CREDENTIALS_FILE,
            scopes=SCOPES
        )

    client = gspread.authorize(creds)
    return client.open(GOOGLE_SHEET_NAME).sheet1


def parse_d_column(raw_data: str):
    parts = raw_data.split("|")
    if len(parts) < 4:
        raise Exception("D column format must be: email|password|refresh_token|client_id")

    email = parts[0].strip()
    password = parts[1].strip()
    refresh_token = parts[2].strip()
    client_id = parts[3].strip()

    return email, password, refresh_token, client_id

def get_unused_email():
    sheet = get_sheet()
    data = sheet.get("D2:E")

    for i, row in enumerate(data, start=2):
        raw_data = row[0] if len(row) > 0 else ""
        used = row[1].strip().upper() if len(row) > 1 else ""

        if used == "YES":
            continue

        email = extract_email_only(raw_data)

        if email:
            return {
                "row": i,
                "email": email,
                "raw_data": raw_data
            }

    return None


def update_sheet_background(row_number):
    thread = threading.Thread(target=update_sheet_data, args=(row_number,))
    thread.daemon = True
    thread.start()


def update_sheet_data(index):
    try:
        sheet = get_sheet()
        sheet.update_cell(index, 2, COMMON_PASSWORD)  # B column
        sheet.update_cell(index, 5, "YES")            # E column
    except Exception as e:
        print("Sheet update error:", e)

def delete_sheet_row(row_number):
    try:
        sheet = get_sheet()
        sheet.delete_rows(row_number)
        return True
    except Exception as e:
        print("Delete row error:", e)
        return False


def save_uid_to_sheet(row_number, uid):
    sheet = get_sheet()
    sheet.update_cell(row_number, 1, uid)  # A column


# =========================
# GRAPH API CODE FETCH
# =========================

def get_graph_access_token(refresh_token, client_id):
    url = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"

    payload = {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": "https://graph.microsoft.com/Mail.Read offline_access"
    }

    r = requests.post(url, data=payload, timeout=30)
    result = r.json()

    if "access_token" not in result:
        raise Exception(f"Token error: {result}")

    return result["access_token"]


def extract_code(text):
    codes = re.findall(r"\b\d{4,8}\b", text or "")
    return codes[0] if codes else None


def fetch_latest_code_graph(raw_data):
    email_address, password, refresh_token, client_id = parse_d_column(raw_data)

    access_token = get_graph_access_token(refresh_token, client_id)

    url = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages"

    params = {
        "$top": "3",
        "$orderby": "receivedDateTime desc",
        "$select": "subject,bodyPreview,receivedDateTime,from"
    }

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json"
    }

    r = requests.get(url, headers=headers, params=params, timeout=30)
    result = r.json()

    if r.status_code != 200:
        raise Exception(f"Graph error: {result}")

    messages = result.get("value", [])

    for msg in messages:
        subject = msg.get("subject", "")
        preview = msg.get("bodyPreview", "")

        full_text = f"{subject}\n{preview}"
        code = extract_code(full_text)

        if code:
            return code

    return None


# =========================
# KEYBOARDS
# =========================

def bottom_keyboard():
    return ReplyKeyboardMarkup(
        [["📋 Task"]],
        resize_keyboard=True
    )


def get_code_keyboard():
    return ReplyKeyboardMarkup(
        [["🔑 Get Code", "❌ Cancel"]],
        resize_keyboard=True
    )

def cancel_only_keyboard():
    return ReplyKeyboardMarkup(
        [["❌ Cancel"]],
        resize_keyboard=True
    )

def done_bottom_keyboard():
    return ReplyKeyboardMarkup(
        [["✅ Done"]],
        resize_keyboard=True
    )


def code_again_inline_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔁 Code Again", callback_data="code_again")]
    ])

def done_inline_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Done", callback_data="done_task")]
    ])


# =========================
# ADMIN COMMANDS
# =========================

def count_unused_emails():
    sheet = get_sheet()  # 🔥 THIS LINE MISSING CHILO

    d_col = sheet.col_values(4)
    e_col = sheet.col_values(5)

    return sum(
        1 for i in range(1, len(d_col))
        if extract_email_only(d_col[i]) and (i >= len(e_col) or e_col[i].strip().upper() != "YES")
    )


async def stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        total = count_unused_emails()
        await update.message.reply_text(f"📦 Available Emails: {total}")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def addpass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global COMMON_PASSWORD

    try:
        if not context.args:
            await update.message.reply_text("Usage: /addpass NewPassword")
            return

        new_pass = context.args[0].strip()
        COMMON_PASSWORD = new_pass

        if os.path.exists(ENV_FILE):
            with open(ENV_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
        else:
            lines = []

        found = False

        with open(ENV_FILE, "w", encoding="utf-8") as f:
            for line in lines:
                if line.startswith("COMMON_PASSWORD="):
                    f.write(f"COMMON_PASSWORD={new_pass}\n")
                    found = True
                else:
                    f.write(line)

            if not found:
                f.write(f"\nCOMMON_PASSWORD={new_pass}\n")

        await update.message.reply_text(f"✅ Password updated: {new_pass}")

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def bot_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_ENABLED
    BOT_ENABLED = False
    await update.message.reply_text("🔴 Bot OFF")


async def bot_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_ENABLED
    BOT_ENABLED = True
    await update.message.reply_text("🟢 Bot ON")


# =========================
# TELEGRAM HANDLERS
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["waiting_for_uid"] = False

    await update.message.reply_text(
        "✅ Bot Ready!\n\n👇 নিচের Task button চাপো",
        reply_markup=bottom_keyboard()
    )


async def send_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        account = get_unused_email()

        if not account:
            await update.message.reply_text("❌ No unused email found.")
            return

        email_address = account["email"]
        row_number = account["row"]
        raw_data = account["raw_data"]

        context.user_data["last_row"] = row_number
        context.user_data["last_raw_data"] = raw_data
        context.user_data["last_email"] = email_address
        context.user_data["otp_received"] = False

        # new task start hole UID/2FA status reset
        context.user_data["waiting_for_uid"] = False
        context.user_data["waiting_for_2fa"] = False
        context.user_data["uid_saved"] = False
        context.user_data["2fa_saved"] = False

        random_name = fake.name()

        result = f"""✅ Account Info

Email: <code>{email_address}</code>
Name: <code>{random_name}</code>
Password: <code>{COMMON_PASSWORD}</code>
"""

        await update.message.reply_text(
            result,
            parse_mode="HTML",
            reply_markup=get_code_keyboard()
        )

        update_sheet_background(row_number)

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def cancel_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    row_number = context.user_data.get("last_row")
    otp_received = context.user_data.get("otp_received") == True

    if row_number and otp_received:
        delete_sheet_row(row_number)
        msg = "❌ Task cancelled and sheet row removed.\n\n👇 Home:"
    else:
        msg = "❌ Task cancelled.\n\n👇 Home:"

    context.user_data.clear()

    await update.message.reply_text(
        msg,
        reply_markup=bottom_keyboard()
    )

async def send_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        raw_data = context.user_data.get("last_raw_data")

        if not raw_data:
            await update.message.reply_text("❌ আগে Task button চাপো.")
            return

        await update.message.reply_text("⏳ Inbox checking...")

        code = fetch_latest_code_graph(raw_data)

        if not code:
            context.user_data["otp_received"] = False
            await update.message.reply_text("❌ No code found.")
            return

        context.user_data["otp_received"] = True

        # remove bottom keyboard
        await update.message.reply_text(
            "📩",
            reply_markup=ReplyKeyboardRemove()
        )

        # send code
        await update.message.reply_text(
            f"📩 OTP Received\n\n🔑 Code:\n<code>{code}</code>",
            parse_mode="HTML",
            reply_markup=code_again_inline_keyboard()
        )

        if not context.user_data.get("uid_saved"):
            context.user_data["waiting_for_uid"] = True
            await update.message.reply_text(
                "📌 Send Account UID Here:",
                reply_markup=cancel_only_keyboard()
            )
  
    except Exception as e:
        await update.message.reply_text(f"❌ Code fetch error: {e}")


async def code_again_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        raw_data = context.user_data.get("last_raw_data")

        if not raw_data:
            await query.message.reply_text("❌ আগে Task button চাপো.")
            return

        await query.message.reply_text("⏳ Inbox checking...")

        code = fetch_latest_code_graph(raw_data)

        if not code:
            await query.message.reply_text("❌ No code found.")
            return

        # ✅ always use query.message এখানে
        await query.message.reply_text(
            f"📩 OTP Received\n\n🔑 Code:\n<code>{code}</code>",
            parse_mode="HTML",
            reply_markup=code_again_inline_keyboard()
        )

        # ✅ Only ONE logic block
        if not context.user_data.get("uid_saved"):
            context.user_data["waiting_for_uid"] = True
            await query.message.reply_text("📌 Send UID")

        elif not context.user_data.get("2fa_saved"):
            context.user_data["waiting_for_2fa"] = True
            await query.message.reply_text("🔐 Send 2FA")

    except Exception as e:
        await query.message.reply_text(f"❌ Code fetch error: {e}")


async def save_uid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = update.message.text.strip()
        row_number = context.user_data.get("last_row")

        if not row_number:
            return

        save_uid_to_sheet(row_number, uid)

        context.user_data["waiting_for_uid"] = False
        context.user_data["uid_saved"] = True

        if not context.user_data.get("2fa_saved"):
            context.user_data["waiting_for_2fa"] = True
            await update.message.reply_text(
                "🔐 Send 2FA:",
                reply_markup=ReplyKeyboardRemove()
            )

    except Exception as e:
        await update.message.reply_text(f"❌ UID save error: {e}")

async def save_2fa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        twofa = update.message.text.strip()
        row = context.user_data.get("last_row")

        if not row:
            return

        sheet = get_sheet()
        sheet.update_cell(row, 3, twofa)  # C column

        context.user_data["waiting_for_2fa"] = False
        context.user_data["2fa_saved"] = True   # ✅ add

        # optional: TOTP send
        import pyotp

        totp = pyotp.TOTP(twofa.replace(" ", "")).now()

        await update.message.reply_text(
            f"🔐 2FA Code:\n<code>{totp}</code>",
            parse_mode="HTML",
            reply_markup=done_bottom_keyboard()
        )

    except Exception as e:
        await update.message.reply_text(f"❌ 2FA error: {e}")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text == "❌ Cancel":
        await cancel_task(update, context)
        return

    if text == "✅ Done":
        context.user_data.clear()

        await update.message.reply_text(
            "✅ Task Completed\n\n👇 Home:",
            reply_markup=bottom_keyboard()
        )
        return

    if context.user_data.get("waiting_for_uid"):
        await save_uid(update, context)
        return

    if context.user_data.get("waiting_for_2fa"):
        await save_2fa(update, context)
        return

    if text == "📋 Task":
        if not BOT_ENABLED:
            await update.message.reply_text("ℹ️ No tasks available.")
            return

        await send_account(update, context)

    elif text == "🔑 Get Code":
        await send_code(update, context)

async def done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    context.user_data.clear()

    await query.message.reply_text(
        "✅ Task Completed\n\n👇 Home:",
        reply_markup=bottom_keyboard()
    )

def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stock", stock))
    app.add_handler(CommandHandler("addpass", addpass))
    app.add_handler(CallbackQueryHandler(code_again_callback, pattern="^code_again$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, button_handler))
    app.add_handler(CallbackQueryHandler(done_callback, pattern="^done_task$"))
    app.add_handler(CommandHandler("botoff", bot_off))
    app.add_handler(CommandHandler("boton", bot_on))

    print("Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
