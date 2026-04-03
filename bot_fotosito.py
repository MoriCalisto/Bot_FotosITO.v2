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
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # Tu ID para el Módulo Admin

if not BOT_TOKEN:
    raise RuntimeError("Define BOT_TOKEN en Render (Environment > Secret).")

PHOTO_SAVE_ROOT = os.getenv("PHOTO_SAVE_ROOT", "./photos")
os.makedirs(PHOTO_SAVE_ROOT, exist_ok=True)

# Opciones extraídas exactamente de tu prompt
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

### =============== FUNCIONES AUXILIARES PARA BOTONES ===============
def build_menu(buttons, n_cols):
    """Agrupa los botones en filas para que no se vea una lista gigante"""
    return [buttons[i:i + n_cols] for i in range(0, len(buttons), n_cols)]

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
        "📊 /resumen_hoy - Fotos subidas hoy\n"
        "🏗️ /ultimos_marcos - Último marco instalado\n"
    )
    await update.message.reply_text(texto, parse_mode="Markdown")

@admin_only
async def cmd_resumen_hoy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Esta función se conectará pronto a Google Sheets...")

### =============== HANDLERS OPERACIÓN (INSPECTORES) ===============
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hola. Envíame una foto del frente de trabajo para comenzar el registro."
    )

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or (not update.message.photo and not update.message.document):
        return
    
    usuario_id = update.message.from_user.id
    nombre_usuario = update.message.from_user.first_name
    
    # 1. Obtener y guardar la foto en el contenedor
    if update.message.photo:
        photo_file = await update.message.photo[-1].get_file()
    else:
        photo_file = await update.message.document.get_file()
        
    filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{usuario_id}.jpg"
    local_path = os.path.join(PHOTO_SAVE_ROOT, filename)
    await photo_file.download_to_drive(local_path)
    log.info(f"Foto guardada localmente en: {local_path}")
    
    # 2. Guardar datos en la memoria temporal
    context.user_data["pending"] = {
        "usuario_id": usuario_id,
        "nombre": nombre_usuario,
        "filename": filename,
        "local_path": local_path,
        "fecha": datetime.now().strftime("%d/%m/%Y"),
        "hora": datetime.now().strftime("%H:%M:%S")
    }
    
    # 3. Desplegar BOTONES de Frente
    botones_frente = [InlineKeyboardButton(f, callback_data=f) for f in FRENTE_CHOICES]
    reply_markup = InlineKeyboardMarkup(build_menu(botones_frente, 3)) # 3 botones por fila
    
    await update.message.reply_text("📸 Imagen recibida y guardada.\n\nPor favor, selecciona el Frente:", reply_markup=reply_markup)
    return ASK_FRENTE

async def choose_frente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    frente = query.data
    context.user_data["pending"]["frente"] = frente
    
    # Desplegar BOTONES de Secuencia
    botones_secuencia = [InlineKeyboardButton(s, callback_data=s) for s in SECUENCIA_CHOICES]
    reply_markup = InlineKeyboardMarkup(build_menu(botones_secuencia, 3))
    
    await query.edit_message_text(
        f"✅ Frente seleccionado: {frente}\n\nAhora selecciona la Secuencia:", 
        reply_markup=reply_markup
    )
    return ASK_SECUENCIA

async def choose_secuencia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    secuencia = query.data
    context.user_data["pending"]["secuencia"] = secuencia
    
    # Lógica condicional exacta
    if secuencia == "SOST":
        await query.edit_message_text(f"✅ Secuencia: {secuencia}\n\nIngresa el **Marco Único** (número):", parse_mode="Markdown")
        return ASK_MR_UNICO
    elif secuencia in ["REV", "CB"]:
        await query.edit_message_text(f"✅ Secuencia: {secuencia}\n\nIngresa el **Marco de Inicio** (número):", parse_mode="Markdown")
        return ASK_MR_INICIO
    else:
        await query.edit_message_text(f"✅ Secuencia: {secuencia}\n\nIngresa un comentario (escribe '-' para dejar vacío):", parse_mode="Markdown")
        return ASK_COMENTARIO

async def receive_mr_unico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["pending"]["mr_unico"] = update.message.text
    return await finalize_record(update, context)

async def receive_mr_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["pending"]["mr_inicio"] = update.message.text
    await update.message.reply_text("Ingresa el **Marco de Fin** (número):", parse_mode="Markdown")
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
        if not pending: 
            return ConversationHandler.END
            
        log.info(f"Registro completo listo para Sheets/OneDrive: {pending}")
        
        # Aquí se inyectará la función real de Gspread y OneDrive.
        # Por ahora mostramos el resumen al usuario para confirmar que capturó todo.
        
        resumen = (
            "✅ **¡Registro guardado exitosamente!**\n\n"
            f"👤 Inspector: {pending.get('nombre')}\n"
            f"📍 Frente: {pending.get('frente')}\n"
            f"🔄 Secuencia: {pending.get('secuencia')}\n"
            f"📸 Archivo: {pending.get('filename')}"
        )
        
        if pending.get('mr_unico'):
            resumen += f"\n🏗️ Marco: {pending.get('mr_unico')}"
        elif pending.get('mr_inicio'):
            resumen += f"\n🏗️ Marcos: {pending.get('mr_inicio')} al {pending.get('mr_fin')}"
        if pending.get('comentario'):
            resumen += f"\n📝 Obs: {pending.get('comentario')}"

        await update.message.reply_text(resumen, parse_mode="Markdown")
        
    except Exception as e:
        log.error(f"Error al finalizar registro: {e}")
        await update.message.reply_text("❌ Ocurrió un error al intentar procesar los datos finales.")
    finally:
        context.user_data.clear()
        
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("🛑 Proceso cancelado. Envía una foto para comenzar de nuevo.")
    return ConversationHandler.END

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Exception in telegram handler", exc_info=context.error)

def main():
    # 1. Iniciar servidor Healthcheck para Render
    start_health_server()
    
    # 2. Fix de Event Loop para versiones recientes de Python
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    # 3. Construir bot
    app = Application.builder().token(BOT_TOKEN).build()
    
    # 4. Flujo conversacional
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
    
    # 5. Registrar Comandos
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("resumen_hoy", cmd_resumen_hoy))
    app.add_handler(conv_handler)
    app.add_error_handler(error_handler)
    
    log.info("Bot iniciado y escuchando mensajes...")
    app.run_polling()

if __name__ == "__main__":
    main()
