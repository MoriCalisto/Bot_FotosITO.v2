# -*- coding: utf-8 -*-
import os
import json
import logging
import threading
import asyncio
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
import msal

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =============== CONFIG ===============
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise RuntimeError("Define BOT_TOKEN en Render.")

PHOTO_SAVE_ROOT = os.getenv("PHOTO_SAVE_ROOT", "./photos")
os.makedirs(PHOTO_SAVE_ROOT, exist_ok=True)

MS_CLIENT_ID = os.getenv("MS_CLIENT_ID", "")
MS_TENANT_ID = os.getenv("MS_TENANT_ID", "common")
MS_SCOPES = ["Files.ReadWrite"]
ONEDRIVE_ROOT = os.getenv("ONEDRIVE_ROOT", "Bot_FotosITO")
TOKEN_CACHE_PATH = os.getenv("TOKEN_CACHE_PATH", "./token_cache.bin")

PORT = int(os.getenv("PORT", "10000"))

PRINCIPAL_CHOICES = ["BR-OR", "BR-PON", "TALL-OR", "TALL-PON", "LOE-OR", "LOE-PON"]
ASK_PRINCIPAL = 0

CSV_LOG = os.path.join(PHOTO_SAVE_ROOT, "registro_fotos.csv")
CSV_HEADER = "Archivo,Frente,Ubicacion,FechaHora\n"

# =============== LOGGING ===============
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("BotFotosITO")

PENDING_ONEDRIVE_FLOWS = {}

# =============== HEALTHCHECK ===============
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        return


def start_health():
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info(f"Healthcheck OK puerto {PORT}")


# =============== CSV ===============
def ensure_csv():
    if not os.path.exists(CSV_LOG):
        with open(CSV_LOG, "w", encoding="utf-8") as f:
            f.write(CSV_HEADER)


ensure_csv()


# =============== UTILS ===============
def frente_from_codigo(codigo: str) -> str:
    if codigo.startswith("BR"):
        return "BREMEN"
    if codigo.startswith("TALL"):
        return "TALLERES"
    if codigo.startswith("LOE"):
        return "LO ERRAZURIZ"
    return "N/A"


def ensure_saved(path: str):
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    if os.path.getsize(path) <= 0:
        raise IOError("Archivo vacío")


# =============== TOKEN CACHE ===============
def load_cache():
    cache = msal.SerializableTokenCache()
    if os.path.exists(TOKEN_CACHE_PATH):
        try:
            with open(TOKEN_CACHE_PATH, "r", encoding="utf-8") as f:
                cache.deserialize(f.read())
        except Exception:
            pass
    return cache


def save_cache(cache):
    if cache.has_state_changed:
        with open(TOKEN_CACHE_PATH, "w", encoding="utf-8") as f:
            f.write(cache.serialize())


def build_msal_app(cache=None):
    return msal.PublicClientApplication(
        MS_CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{MS_TENANT_ID}",
        token_cache=cache,
    )


# =============== ONEDRIVE ===============
def get_graph_token():
    if not MS_CLIENT_ID:
        raise RuntimeError("Falta MS_CLIENT_ID en Render.")

    cache = load_cache()
    app = build_msal_app(cache)

    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(MS_SCOPES, account=accounts[0])
        if result and "access_token" in result:
            save_cache(cache)
            return result["access_token"]

    raise RuntimeError("Debes ejecutar /onedrive_login")


def upload_to_onedrive(local_path, folder, filename):
    token = get_graph_token()
    url = f"https://graph.microsoft.com/v1.0/me/drive/root:/{ONEDRIVE_ROOT}/{folder}/{filename}:/content"

    with open(local_path, "rb") as f:
        r = requests.put(
            url,
            headers={"Authorization": f"Bearer {token}"},
            data=f,
        )

    if r.status_code not in (200, 201):
        raise RuntimeError(f"Graph upload error {r.status_code}: {r.text}")


# =============== COMMANDS ===============
async def onedrive_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    if not MS_CLIENT_ID:
        await update.message.reply_text("❌ Falta MS_CLIENT_ID en Render.")
        return

    cache = load_cache()
    app = build_msal_app(cache)
    flow = app.initiate_device_flow(scopes=MS_SCOPES)

    if "user_code" not in flow:
        await update.message.reply_text("❌ Error iniciando login de OneDrive")
        return

    PENDING_ONEDRIVE_FLOWS[str(update.effective_chat.id)] = (app, flow, cache)

    await update.message.reply_text(
        "🔐 Autorización iniciada.\n\n"
        f"{flow['message']}\n\n"
        "Cuando Microsoft te diga que cierres la ventana, vuelve aquí y ejecuta:\n"
        "/onedrive_finish"
    )


async def onedrive_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    chat_id = str(update.effective_chat.id)

    if chat_id not in PENDING_ONEDRIVE_FLOWS:
        await update.message.reply_text("Primero ejecuta /onedrive_login")
        return

    app, flow, cache = PENDING_ONEDRIVE_FLOWS[chat_id]

    await update.message.reply_text("⏳ Finalizando autorización...")

    try:
        result = await asyncio.to_thread(app.acquire_token_by_device_flow, flow)
    except Exception as e:
        await update.message.reply_text(f"❌ Error al finalizar autorización: {e}")
        return

    if "access_token" in result:
        save_cache(cache)
        PENDING_ONEDRIVE_FLOWS.pop(chat_id, None)
        await update.message.reply_text("✅ OneDrive listo")
    else:
        detalle = result.get("error_description", "sin detalle")
        await update.message.reply_text(f"❌ Error en autorización:\n{detalle}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📸 Envíame una foto.\n\n"
        "Comandos:\n"
        "/idchat → obtener ID del chat o grupo\n"
        "/onedrive_login → iniciar autorización OneDrive\n"
        "/onedrive_finish → terminar autorización OneDrive"
    )


async def idchat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat_title = update.effective_chat.title or "Chat privado"

    await update.message.reply_text(
        f"🆔 Chat ID:\n{chat_id}\n\n"
        f"📌 Chat:\n{chat_title}"
    )


# =============== FLOW FOTO ===============
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.photo:
        return

    file = await update.message.photo[-1].get_file()

    now = datetime.now()
    user = update.message.from_user

    usuario = user.username or user.first_name or str(user.id)
    usuario = str(usuario).replace(" ", "_")

    context.user_data["data"] = {
        "file": file,
        "fecha": now.strftime("%Y-%m-%d"),
        "hora": now.strftime("%H-%M-%S"),
        "fecha_hora": now.strftime("%Y-%m-%d %H-%M-%S"),
        "usuario": usuario,
    }

    kb = [[InlineKeyboardButton(x, callback_data=x)] for x in PRINCIPAL_CHOICES]

    await update.message.reply_text(
        "Selecciona frente",
        reply_markup=InlineKeyboardMarkup(kb)
    )

    return ASK_PRINCIPAL


async def choose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    data = context.user_data.get("data")
    if not data:
        await q.edit_message_text("⚠️ No encuentro la foto pendiente.")
        return ConversationHandler.END

    frente = q.data
    nombre = f"{data['fecha']}_{data['hora']}_{frente}_{data['usuario']}.jpg"

    path = os.path.join(PHOTO_SAVE_ROOT, frente)
    os.makedirs(path, exist_ok=True)
    full = os.path.join(path, nombre)

    await data["file"].download_to_drive(full)
    ensure_saved(full)

    with open(CSV_LOG, "a", encoding="utf-8") as f:
        f.write(f"{nombre},{frente_from_codigo(frente)},{frente},{data['fecha_hora']}\n")

    try:
        upload_to_onedrive(full, frente, nombre)
        msg = "☁️ Subido a OneDrive"
    except Exception as e:
        msg = f"⚠️ {e}"

    context.user_data.clear()

    await q.edit_message_text(
        f"✅ {nombre}\n{msg}"
    )

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("🛑 Cancelado")


# =============== MAIN ===============
def main():
    start_health()

    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[MessageHandler(filters.PHOTO, on_photo)],
        states={ASK_PRINCIPAL: [CallbackQueryHandler(choose)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("idchat", idchat))
    app.add_handler(CommandHandler("onedrive_login", onedrive_login))
    app.add_handler(CommandHandler("onedrive_finish", onedrive_finish))
    app.add_handler(conv)

    app.run_polling()


if __name__ == "__main__":
    main()
