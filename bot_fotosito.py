# -*- coding: utf-8 -*-
import os
import json
import asyncio
import logging
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
import gspread
import msal
from google.oauth2.service_account import Credentials

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ConversationHandler, CallbackQueryHandler, ContextTypes, filters,
)

### =============== CONFIG ===============
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise RuntimeError("Define BOT_TOKEN en Render (Environment > Secret).")

PHOTO_SAVE_ROOT = os.getenv("PHOTO_SAVE_ROOT", "./photos")
os.makedirs(PHOTO_SAVE_ROOT, exist_ok=True)

# Actualizado con los subfrentes de Roman Salinas
FRENTE_CHOICES = [
    "VEE", "BR-OR", "BR-PON", "BR-SUP",
    "TALL-SUP", "TALL-OR", "TALL-PON",
    "LOE-SUP", "LOE-OR", "LOE-PON",
    "LOE-TEA", "LOE-TEB", "LOE-TEC",
    "RS-PIQ", "RS-SUP"
]

SECUENCIA_CHOICES = ["SOST", "REV", "CB", "OQUEDAD", "LANZA", "DET"]

CSV_LOG = os.path.join(PHOTO_SAVE_ROOT, "registro_fotos.csv")
CSV_HEADER = "Archivo,Frente,Ubicacion,FechaHora\n"

ASK_FRENTE = 0
ASK_SECUENCIA = 1
ASK_MR_UNICO = 2
ASK_MR_INICIO = 3
ASK_MR_FIN = 4
ASK_COMENTARIO = 5

### OneDrive / Graph
MS_CLIENT_ID = os.getenv("MS_CLIENT_ID", "")
MS_TENANT_ID = os.getenv("MS_TENANT_ID", "common")
MS_SCOPES = ["Files.ReadWrite"]
ONEDRIVE_ROOT = os.getenv("ONEDRIVE_ROOT", "Bot_FotosITO")
TOKEN_CACHE_PATH = os.getenv("TOKEN_CACHE_PATH", "./token_cache.bin")

### Render / Healthcheck
PORT = int(os.getenv("PORT", "10000"))

### =============== LOGGING ===============
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO,
)
log = logging.getLogger("BotFotosITO")

### =============== OneDrive Device Flow Temp Storage ===============
PENDING_ONEDRIVE_FLOWS = {}

### =============== HEALTHCHECK SERVER ===============
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"ok")

def start_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info(f"Healthcheck HTTP server escuchando en 0.0.0.0:{PORT}")
    return server

### =============== CSV ===============
def ensure_csv():
    if not os.path.exists(CSV_LOG):
        with open(CSV_LOG, "w", encoding="utf-8") as f:
            f.write(CSV_HEADER)
    return

ensure_csv()

### =============== UTILS ===============
def frente_from_codigo(codigo: str) -> str:
    if codigo.startswith("BR"): return "BREMEN"
    if codigo.startswith("TALL"): return "TALLERES"
    if codigo.startswith("LOE"): return "LO ERRAZURIZ"
    if codigo == "VEE": return "VIA ENLACE EXISTENTE"
    # Modificado para aceptar RS-PIQ y RS-SUP
    if codigo.startswith("RS"): return "ROMAN SALINAS" 
    return "N/A"

def ensure_saved(path: str) -> None:
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    if os.path.getsize(path) <= 0:
        raise IOError("Archivo vacío")

### ---------- Google Sheets helpers ----------
GS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

def get_gspread_client():
    credentials_raw = os.getenv("GOOGLE_SHEETS_CREDENTIALS_JSON", "")
    if not credentials_raw:
        raise RuntimeError("Falta GOOGLE_SHEETS_CREDENTIALS_JSON en Render.")

def get_registro_worksheet():
    sheet_name = os.getenv("GOOGLE_SHEET_NAME", "")
    worksheet_name = os.getenv("GOOGLE_WORKSHEET_REGISTRO", "RegistroFotos")

def build_sheet_row(data: dict) -> list:
    return [
        data.get("ID_Registro", ""),
        data.get("Fecha", ""),
        data.get("Hora", ""),
        data.get("Timestamp", ""),
        data.get("Usuario_ID", ""),
        data.get("Nombre", ""),
        data.get("Username", ""),
        data.get("ChatID", ""),
        data.get("Frente", ""),
        data.get("Secuencia", ""),
        data.get("MR_Inicio", ""),
        data.get("MR_Fin", ""),
        data.get("MR_Unico", ""),
        data.get("Comentario", ""),
        data.get("File_ID", ""),
        data.get("Photo_Unique_ID", ""),
        data.get("Nombre_Archivo", ""),
        data.get("Link_Foto", ""),
        data.get("Ruta_Carpeta", ""),
        data.get("Estado", "Activo"),
        data.get("Solicitud_Eliminacion", "NO"),
        data.get("Motivo_Eliminacion", ""),
        data.get("Fecha_Solicitud", ""),
        data.get("Aprobacion", "Pendiente"),
        data.get("Aprobado_Por", ""),
        data.get("Fecha_Aprobacion", ""),
        data.get("Observacion_Admin", ""),
    ]

def append_registro_foto(data: dict):
    ws = get_registro_worksheet()
    row = build_sheet_row(data)
    ws.append_row(row, value_input_option="USER_ENTERED")

### ---------- MSAL helpers ----------
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

def get_graph_token():
    if not MS_CLIENT_ID:
        raise RuntimeError("Define MS_CLIENT_ID en Render (tu App Client ID).")

def upload_to_onedrive(local_path: str, remote_dir: str, filename: str):
    token = get_graph_token()
    remote_path = f"/{ONEDRIVE_ROOT}/{remote_dir}/{filename}".replace("//", "/")
    url = f"https://graph.microsoft.com/v1.0/me/drive/root:{remote_path}:/content"

### =============== HANDLERS ===============
async def cmd_onedrive_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

async def cmd_onedrive_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Envíame una foto.\n"
        "Después te preguntaré:\n"
        "1) Frente/Subfrente\n"
        "2) Secuencia\n"
        "3) Marco o comentario, según corresponda\n\n"
        "Secuencias disponibles:\n"
        "- SOST\n"
        "- REV\n"
        "- CB\n"
        "- OQUEDAD\n"
        "- LANZA\n"
        "- DET\n\n"
        "Comandos útiles:\n"
        "/onedrive_login → iniciar autorización OneDrive\n"
        "/onedrive_finish → terminar autorización OneDrive"
    )

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.photo:
        return

async def choose_frente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

async def choose_secuencia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

async def receive_mr_unico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    pending = context.user_data.get("pending")

async def receive_mr_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    pending = context.user_data.get("pending")

async def receive_mr_fin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    pending = context.user_data.get("pending")

async def receive_comentario(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    pending = context.user_data.get("pending")

async def finalize_record(update_or_query, context: ContextTypes.DEFAULT_TYPE):
    try:
        pending = context.user_data.get("pending")
        if not pending: return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("🛑 Cancelado. Envía una foto para comenzar de nuevo.")
    return ConversationHandler.END

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled exception", exc_info=context.error)

def main():
    # 1. Iniciar el servidor Healthcheck para que Render no apague el bot
    start_health_server()
    
    # --- FIX CRÍTICO: Crear un event loop para versiones nuevas de Python ---
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    # ------------------------------------------------------------------------
    
    # 2. Construir la aplicación del bot de Telegram
    app = Application.builder().token(BOT_TOKEN).build()
    
    # 3. Construir el ConversationHandler con todos tus estados
    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.PHOTO, on_photo)],
        states={
            ASK_FRENTE: [CallbackQueryHandler(choose_frente)],
            ASK_SECUENCIA: [CallbackQueryHandler(choose_secuencia)],
            ASK_MR_UNICO: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_mr_unico)],
            ASK_MR_INICIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_mr_inicio)],
            ASK_MR_FIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_mr_fin)],
            ASK_COMENTARIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_comentario)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False
    )
    
    # 4. Registrar los comandos básicos
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("onedrive_login", cmd_onedrive_login))
    app.add_handler(CommandHandler("onedrive_finish", cmd_onedrive_finish))
    
    # 5. Registrar el manejador de la conversación (fotos) y errores
    app.add_handler(conv_handler)
    app.add_error_handler(error_handler)
    
    # 6. Poner al bot a escuchar a Telegram
    log.info("Bot iniciado y escuchando mensajes...")
    app.run_polling()

if __name__ == "__main__":
    main()
