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
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # <--- ID de seguridad para el Módulo Admin

if not BOT_TOKEN:
    raise RuntimeError("Define BOT_TOKEN en Render (Environment > Secret).")

PHOTO_SAVE_ROOT = os.getenv("PHOTO_SAVE_ROOT", "./photos")
os.makedirs(PHOTO_SAVE_ROOT, exist_ok=True)

# Lista de frentes actualizada con las subdivisiones de RS
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
    log.info(f"Healthcheck HTTP server escuchando en 0.0.0.0:{PORT}")
    return server

### =============== SEGURIDAD MÓDULO ADMIN ===============
def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("⛔ Acceso denegado. Este comando es exclusivo para el Administrador.")
            return
        return await func(update, context)
    return wrapper

### =============== COMANDOS ADMIN ===============
@admin_only
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = (
        "🛠️ *PANEL DE ADMINISTRADOR* 🛠️\n\n"
        "Comandos disponibles:\n"
        "📊 /resumen_hoy - Fotos subidas hoy por usuario y frente\n"
        "🏗️ /ultimos_marcos - Último marco instalado por frente\n"
        "🗑️ /eliminaciones - Ver solicitudes de eliminación pendientes"
    )
    await update.message.reply_text(texto, parse_mode="Markdown")

@admin_only
async def cmd_resumen_hoy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Calculando resumen de hoy desde Google Sheets...")

@admin_only
async def cmd_ultimos_marcos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Buscando los últimos marcos por frente...")

### =============== HANDLERS OPERACIÓN (INSPECTORES) ===============
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hola. Envíame una foto del frente de trabajo para comenzar.\n"
        "Si envías un archivo como 'Documento', podré mantener la calidad original."
    )

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or (not update.message.photo and not update.message.document):
        return
    
    usuario_id = update.message.from_user.id
    log.info(f"Archivo recibido de ID: {usuario_id}")
    
    # Inicializamos la memoria temporal del usuario
    context.user_data["pending"] = {"usuario_id": usuario_id}
    
    await update.message.reply_text("📸 Imagen recibida. Por favor, escribe o selecciona el Frente (Ej: BR-PON):")
    return ASK_FRENTE

async def choose_frente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q: await q.answer()
    
    frente = q.data if q else update.message.text
    context.user_data["pending"]["frente"] = frente
    
    await update.message.reply_text("Selecciona la Secuencia (SOST, REV, CB, OQUEDAD, LANZA, DET):")
    return ASK_SECUENCIA

async def choose_secuencia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q: await q.answer()
    
    secuencia = q.data if q else update.message.text
    context.user_data["pending"]["secuencia"] = secuencia
    
    # Lógica condicional según el diseño
    if secuencia == "SOST":
        await update.message.reply_text("Ingresa el Marco Único:")
        return ASK_MR_UNICO
    elif secuencia in ["REV", "CB"]:
        await update.message.reply_text("Ingresa el Marco de Inicio:")
        return ASK_MR_INICIO
    else:
        await update.message.reply_text("Ingresa un comentario opcional (escribe '-' para dejar vacío):")
        return ASK_COMENTARIO

async def receive_mr_unico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["pending"]["mr_unico"] = update.message.text
    return await finalize_record(update, context)

async def receive_mr_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["pending"]["mr_inicio"] = update.message.text
    await update.message.reply_text("Ingresa el Marco de Fin:")
    return ASK_MR_FIN

async def receive_mr_fin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["pending"]["mr_fin"] = update.message.text
    return await finalize_record(update, context)

async def receive_comentario(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["pending"]["comentario"] = update.message.text
    return await finalize_record(update, context)

async def finalize_record(update_or_query, context: ContextTypes.DEFAULT_TYPE):
    # ¡SOLUCIÓN AL SYNTAX ERROR! Un bloque try estructurado y cerrado
    try:
        pending = context.user_data.get("pending")
        if not pending: 
            return ConversationHandler.END
            
        log.info(f"Procesando registro: {pending}")
        
        # Aquí se inyectará la lógica de Google Sheets y OneDrive posteriormente
        
        await update_or_query.message.reply_text("✅ ¡Registro temporal guardado en memoria exitosamente!")
        
    except Exception as e:
        log.error(f"Error al guardar registro: {e}")
        await update_or_query.message.reply_text("❌ Ocurrió un error al intentar procesar los datos.")
    finally:
        # Se asegura de limpiar la memoria para la próxima foto sin importar si falló o no
        context.user_data.clear()
        
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("🛑 Proceso cancelado. Envía una foto para comenzar de nuevo.")
    return ConversationHandler.END

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Exception in telegram handler", exc_info=context.error)

def main():
    # 1. Iniciar el servidor Healthcheck para mantener Render vivo
    start_health_server()
    
    # 2. Fix de Event Loop para las versiones recientes de Python
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    # 3. Construir la aplicación del bot
    app = Application.builder().token(BOT_TOKEN).build()
    
    # 4. Construir el flujo del Módulo Operación
    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_photo)],
        states={
            ASK_FRENTE: [CallbackQueryHandler(choose_frente), MessageHandler(filters.TEXT & ~filters.COMMAND, choose_frente)],
            ASK_SECUENCIA: [CallbackQueryHandler(choose_secuencia), MessageHandler(filters.TEXT & ~filters.COMMAND, choose_secuencia)],
            ASK_MR_UNICO: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_mr_unico)],
            ASK_MR_INICIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_mr_inicio)],
            ASK_MR_FIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_mr_fin)],
            ASK_COMENTARIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_comentario)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False
    )
    
    # 5. Registrar Comandos (Incluyendo los protegidos del Admin)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("resumen_hoy", cmd_resumen_hoy))
    app.add_handler(CommandHandler("ultimos_marcos", cmd_ultimos_marcos))
    
    app.add_handler(conv_handler)
    app.add_error_handler(error_handler)
    
    log.info("Bot iniciado y escuchando mensajes...")
    app.run_polling()

if __name__ == "__main__":
    main()
