# -*- coding: utf-8 -*-
import os
import json
import asyncio
import logging
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from functools import wraps

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
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

if not BOT_TOKEN:
    raise RuntimeError("Define BOT_TOKEN en Render (Environment > Secret).")

PHOTO_SAVE_ROOT = os.getenv("PHOTO_SAVE_ROOT", "./photos")
os.makedirs(PHOTO_SAVE_ROOT, exist_ok=True)

FRENTE_CHOICES = [
    "VEE", "BR-OR", "BR-PON", "BR-SUP",
    "TALL-SUP", "TALL-OR", "TALL-PON",
    "LOE-SUP", "LOE-OR", "LOE-PON",
    "LOE-TEA", "LOE-TEB", "LOE-TEC",
    "RS-PIQ", "RS-SUP"
]

SECUENCIA_CHOICES = ["SOST", "REV", "CB", "OQUEDAD", "LANZA", "DET"]

ASK_FRENTE = 0
ASK_SECUENCIA = 1
ASK_MR_UNICO = 2
ASK_MR_INICIO = 3
ASK_MR_FIN = 4
ASK_COMENTARIO = 5

PORT = int(os.getenv("PORT", "10000"))

### =============== LOGGING ===============
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO,
)
log = logging.getLogger("BotFotosITO")

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
    return server

### =============== HELPERS DE BOTONES ===============
def build_menu(buttons, n_cols):
    return [buttons[i:i + n_cols] for i in range(0, len(buttons), n_cols)]

### =============== GOOGLE SHEETS & MSAL (ONEDRIVE) ===============
GS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

def get_gspread_client():
    creds_raw = os.getenv("GOOGLE_SHEETS_CREDENTIALS_JSON", "")
    if not creds_raw:
        raise RuntimeError("Falta GOOGLE_SHEETS_CREDENTIALS_JSON en Render.")
    creds_dict = json.loads(creds_raw, strict=False)
    creds = Credentials.from_service_account_info(creds_dict, scopes=GS_SCOPES)
    return gspread.authorize(creds)

def get_registro_worksheet():
    client = get_gspread_client()
    sheet_name = os.getenv("GOOGLE_SHEET_NAME", "")
    if not sheet_name:
        raise RuntimeError("Falta GOOGLE_SHEET_NAME en Render.")
    return client.open(sheet_name).worksheet("RegistroFotos")

def build_sheet_row(data: dict) -> list:
    return [
        data.get("ID_Registro", ""), data.get("Fecha", ""), data.get("Hora", ""),
        data.get("Timestamp", ""), data.get("Usuario_ID", ""), data.get("Nombre", ""),
        data.get("Username", ""), data.get("ChatID", ""), data.get("Frente", ""),
        data.get("Secuencia", ""), data.get("MR_Inicio", ""), data.get("MR_Fin", ""),
        data.get("MR_Unico", ""), data.get("Comentario", ""), data.get("File_ID", ""),
        data.get("Photo_Unique_ID", ""), data.get("Nombre_Archivo", ""),
        data.get("Link_Foto", ""), data.get("Ruta_Carpeta", ""), data.get("Estado", "Activo"),
        data.get("Solicitud_Eliminacion", "NO"), data.get("Motivo_Eliminacion", ""),
        data.get("Fecha_Solicitud", ""), data.get("Aprobacion", "Pendiente"),
        data.get("Aprobado_Por", ""), data.get("Fecha_Aprobacion", ""),
        data.get("Observacion_Admin", ""),
    ]

# OneDrive Auth config
MS_CLIENT_ID = os.getenv("MS_CLIENT_ID", "")
MS_TENANT_ID = os.getenv("MS_TENANT_ID", "common")
MS_SCOPES = ["Files.ReadWrite", "offline_access"]
ONEDRIVE_ROOT = os.getenv("ONEDRIVE_ROOT", "Bot_FotosITO")
TOKEN_CACHE_PATH = os.getenv("TOKEN_CACHE_PATH", "./token_cache.bin")
PENDING_ONEDRIVE_FLOWS = {}

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
        raise RuntimeError("Falta MS_CLIENT_ID en Render.")
    authority = f"https://login.microsoftonline.com/{MS_TENANT_ID}"
    cache = load_cache()
    app = msal.PublicClientApplication(MS_CLIENT_ID, authority=authority, token_cache=cache)
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(MS_SCOPES, account=accounts)
        if result and "access_token" in result:
            save_cache(cache)
            return result["access_token"]
    raise RuntimeError("Falta Login OneDrive")

def upload_to_onedrive(local_path: str, remote_dir: str, filename: str):
    token = get_graph_token()
    remote_path = f"/{ONEDRIVE_ROOT}/{remote_dir}/{filename}".replace("//", "/")
    url = f"https://graph.microsoft.com/v1.0/me/drive/root:{remote_path}:/content"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/octet-stream"}
    with open(local_path, "rb") as f:
        data = f.read()
    response = requests.put(url, headers=headers, data=data)
    response.raise_for_status()

### =============== COMANDOS ADMIN Y ONEDRIVE ===============
def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("⛔ Acceso denegado.")
            return
        return await func(update, context)
    return wrapper

@admin_only
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🛠️ *PANEL DE ADMINISTRADOR*\n\nPróximamente más funciones...", parse_mode="Markdown")

async def cmd_onedrive_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    authority = f"https://login.microsoftonline.com/{MS_TENANT_ID}"
    app = msal.PublicClientApplication(MS_CLIENT_ID, authority=authority)
    flow = app.initiate_device_flow(scopes=MS_SCOPES)
    if "user_code" not in flow:
        await update.message.reply_text("Error al iniciar Microsoft Auth.")
        return
    PENDING_ONEDRIVE_FLOWS[update.message.from_user.id] = flow
    await update.message.reply_text(
        f"🔗 *Conectar OneDrive*\n1. Entra a {flow['verification_uri']}\n"
        f"2. Escribe este código: `{flow['user_code']}`\n"
        "3. Vuelve a Telegram y envía /onedrive_finish", parse_mode="Markdown"
    )

async def cmd_onedrive_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    flow = PENDING_ONEDRIVE_FLOWS.get(user_id)
    if not flow:
        await update.message.reply_text("Usa /onedrive_login primero.")
        return
    authority = f"https://login.microsoftonline.com/{MS_TENANT_ID}"
    cache = load_cache()
    app = msal.PublicClientApplication(MS_CLIENT_ID, authority=authority, token_cache=cache)
    result = app.acquire_token_by_device_flow(flow)
    if "access_token" in result:
        save_cache(cache)
        del PENDING_ONEDRIVE_FLOWS[user_id]
        await update.message.reply_text("✅ ¡OneDrive conectado y autorizado exitosamente!")
    else:
        await update.message.reply_text("❌ Error de autorización.")

### =============== HANDLERS OPERACIÓN (INSPECTORES) ===============
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Hola. Envíame una foto para comenzar.")

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or (not update.message.photo and not update.message.document):
        return
    
    usuario_id = update.message.from_user.id
    nombre_usuario = update.message.from_user.first_name
    
    if update.message.photo:
        photo_file = await update.message.photo[-1].get_file()
    else:
        photo_file = await update.message.document.get_file()
        
    filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{usuario_id}.jpg"
    local_path = os.path.join(PHOTO_SAVE_ROOT, filename)
    await photo_file.download_to_drive(local_path)
    
    context.user_data["pending"] = {
        "usuario_id": usuario_id,
        "nombre": nombre_usuario,
        "filename": filename,
        "local_path": local_path,
        "fecha": datetime.now().strftime("%d/%m/%Y"),
        "hora": datetime.now().strftime("%H:%M:%S")
    }
    
    botones_frente = [InlineKeyboardButton(f, callback_data=f) for f in FRENTE_CHOICES]
    reply_markup = InlineKeyboardMarkup(build_menu(botones_frente, 3))
    await update.message.reply_text("📸 Imagen guardada.\nSelecciona el Frente:", reply_markup=reply_markup)
    return ASK_FRENTE

async def choose_frente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["pending"]["frente"] = query.data
    
    botones_secuencia = [InlineKeyboardButton(s, callback_data=s) for s in SECUENCIA_CHOICES]
    reply_markup = InlineKeyboardMarkup(build_menu(botones_secuencia, 3))
    await query.edit_message_text(f"✅ Frente: {query.data}\nSelecciona Secuencia:", reply_markup=reply_markup)
    return ASK_SECUENCIA

async def choose_secuencia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    secuencia = query.data
    context.user_data["pending"]["secuencia"] = secuencia
    
    if secuencia == "SOST":
        await query.edit_message_text(f"✅ Secuencia: {secuencia}\nIngresa el Marco Único (número):")
        return ASK_MR_UNICO
    elif secuencia in ["REV", "CB"]:
        await query.edit_message_text(f"✅ Secuencia: {secuencia}\nIngresa el Marco de Inicio (número):")
        return ASK_MR_INICIO
    else:
        await query.edit_message_text(f"✅ Secuencia: {secuencia}\nIngresa un comentario (o '-' para omitir):")
        return ASK_COMENTARIO

async def receive_mr_unico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["pending"]["mr_unico"] = update.message.text
    return await finalize_record(update, context)

async def receive_mr_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["pending"]["mr_inicio"] = update.message.text
    await update.message.reply_text("Ingresa el Marco de Fin (número):")
    return ASK_MR_FIN

async def receive_mr_fin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["pending"]["mr_fin"] = update.message.text
    return await finalize_record(update, context)

async def receive_comentario(update: Update, context: ContextTypes.DEFAULT_TYPE):
    comentario = update.message.text
    context.user_data["pending"]["comentario"] = "" if comentario == "-" else comentario
    return await finalize_record(update, context)

async def finalize_record(update, context: ContextTypes.DEFAULT_TYPE):
    try:
        pending = context.user_data.get("pending")
        if not pending: return ConversationHandler.END
        
        await update.message.reply_text("⏳ Procesando guardado en la nube...")

        row_base = {
            "Fecha": pending["fecha"], "Hora": pending["hora"],
            "Usuario_ID": pending["usuario_id"], "Nombre": pending["nombre"],
            "Frente": pending["frente"], "Secuencia": pending["secuencia"],
            "MR_Inicio": pending.get("mr_inicio", ""), "MR_Fin": pending.get("mr_fin", ""),
            "MR_Unico": pending.get("mr_unico", ""), "Comentario": pending.get("comentario", ""),
            "Nombre_Archivo": pending["filename"], "Estado": "Activo", "Aprobacion": "Pendiente"
        }

        filas_a_insertar = []
        if pending["secuencia"] in ["REV", "CB"] and pending.get("mr_inicio") and pending.get("mr_fin"):
            try:
                inicio, fin = int(pending["mr_inicio"]), int(pending["mr_fin"])
                step = 1 if inicio <= fin else -1
                for mr in range(inicio, fin + step, step):
                    clon = row_base.copy()
                    clon["MR_Unico"] = str(mr)
                    filas_a_insertar.append(build_sheet_row(clon))
            except ValueError:
                filas_a_insertar.append(build_sheet_row(row_base))
        else:
            filas_a_insertar.append(build_sheet_row(row_base))

        sheets_ok = False
        try:
            ws = get_registro_worksheet()
            ws.append_rows(filas_a_insertar, value_input_option="USER_ENTERED")
            sheets_ok = True
        except Exception as e:
            log.error(f"Error Sheets: {e}", exc_info=True)

        onedrive_ok = False
        try:
            upload_to_onedrive(pending["local_path"], pending["frente"], pending["filename"])
            onedrive_ok = True
        except Exception as e:
            log.error(f"Error OneDrive: {e}", exc_info=True)

        # FIX CRÍTICO: Construimos el texto SIN parse_mode="Markdown" para que nunca más colapse
        resumen = "✅ ¡Registro Finalizado!\n\n"
        resumen += "📊 Google Sheets: " + ("OK" if sheets_ok else "⚠️ ERROR (Revisa las variables en Render)") + "\n"
        resumen += "☁️ OneDrive: " + ("OK" if onedrive_ok else "⚠️ ERROR (Usa /onedrive_login)") + "\n\n"
        resumen += f"Frente: {pending['frente']}\nSecuencia: {pending['secuencia']}\n"
        
        if pending.get('mr_unico'):
            resumen += f"🏗️ Marco: {pending.get('mr_unico')}\n"
        elif pending.get('mr_inicio'):
            resumen += f"🏗️ Marcos: {pending.get('mr_inicio')} al {pending.get('mr_fin')}\n"
        
        resumen += f"📸 Archivo: {pending['filename']}"

        await update.message.reply_text(resumen)

    except Exception as e:
        log.error(f"Error Critico en finalize: {e}", exc_info=True)
        await update.message.reply_text("❌ Error interno de Telegram. Intenta enviar la foto nuevamente.")
    finally:
        context.user_data.clear()
        
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("🛑 Cancelado. Envía una foto de nuevo.")
    return ConversationHandler.END

def main():
    start_health_server()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_photo)],
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
    
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("onedrive_login", cmd_onedrive_login))
    app.add_handler(CommandHandler("onedrive_finish", cmd_onedrive_finish))
    app.add_handler(conv_handler)
    
    log.info("Bot iniciado...")
    app.run_polling()

if __name__ == "__main__":
    main()
