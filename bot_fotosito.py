# -*- coding: utf-8 -*-
import os
import csv
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

FLASH_SAVE_ROOT = os.getenv("FLASH_SAVE_ROOT", "./Flash_Reportes")
os.makedirs(FLASH_SAVE_ROOT, exist_ok=True)

FLASH_GROUP_CHAT_ID = os.getenv("FLASH_GROUP_CHAT_ID", "")

MS_CLIENT_ID = os.getenv("MS_CLIENT_ID", "")
MS_TENANT_ID = os.getenv("MS_TENANT_ID", "common")
MS_SCOPES = ["Files.ReadWrite"]
ONEDRIVE_ROOT = os.getenv("ONEDRIVE_ROOT", "Bot_FotosITO")
TOKEN_CACHE_PATH = os.getenv("TOKEN_CACHE_PATH", "./token_cache.bin")

PORT = int(os.getenv("PORT", "10000"))

PRINCIPAL_CHOICES = ["BR-OR", "BR-PON", "TALL-OR", "TALL-PON", "LOE-OR", "LOE-PON"]
ASK_PRINCIPAL = 0

FLASH_PIQUE, FLASH_FRENTE, FLASH_EVENTO, FLASH_DETALLE, FLASH_FOTO = range(10, 15)

PIQUES = ["Bremen", "Talleres", "Lo Errázuriz", "Roman Salinas", "VEE", "Otro"]

FRENTES = [
    "TIE Oriente",
    "TIE Poniente",
    "Galería Oriente",
    "Galería Poniente",
    "Tramo B",
    "Tramo C",
    "Superficie",
    "Otro",
]

EVENTOS_FLASH = [
    "Desprendimientos",
    "Inundación",
    "Sobreexcavación",
    "Tiempo Frente Abierta",
    "Falta Alzaprima",
    "No aplicación de 5cm",
    "Otro",
]

CSV_LOG = os.path.join(PHOTO_SAVE_ROOT, "registro_fotos.csv")
CSV_HEADER = "Archivo,Frente,Ubicacion,FechaHora\n"

FLASH_CSV = os.path.join(FLASH_SAVE_ROOT, "flash_reportes.csv")
FLASH_HEADER = [
    "ID",
    "FechaHora",
    "Inspector",
    "UsuarioID",
    "Pique",
    "Frente",
    "Evento",
    "Detalle",
    "Foto",
]

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


def ensure_flash_csv():
    if not os.path.exists(FLASH_CSV):
        with open(FLASH_CSV, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(FLASH_HEADER)


ensure_csv()
ensure_flash_csv()


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


def clean_filename(text: str) -> str:
    text = str(text).strip()
    replacements = {
        " ": "_",
        "/": "-",
        "\\": "-",
        ":": "-",
        "*": "",
        "?": "",
        '"': "",
        "<": "",
        ">": "",
        "|": "",
        "á": "a",
        "é": "e",
        "í": "i",
        "ó": "o",
        "ú": "u",
        "Á": "A",
        "É": "E",
        "Í": "I",
        "Ó": "O",
        "Ú": "U",
        "ñ": "n",
        "Ñ": "N",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def build_keyboard(items, prefix, cols=2):
    keyboard = []
    row = []

    for item in items:
        row.append(InlineKeyboardButton(item, callback_data=f"{prefix}|{item}"))
        if len(row) == cols:
            keyboard.append(row)
            row = []

    if row:
        keyboard.append(row)

    return InlineKeyboardMarkup(keyboard)


def get_inspector_name(user) -> str:
    if not user:
        return "Sin usuario"

    name = user.full_name or user.username or str(user.id)
    if user.username:
        name = f"{name} (@{user.username})"

    return name


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
    await help_cmd(update, context)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = (
        "🤖 BOT FOTOS ITO / INFORME FLASH\n\n"
        "📸 REGISTRO DE FOTOS\n"
        "1. Envía una foto al bot.\n"
        "2. El bot mostrará botones de frente.\n"
        "3. Selecciona el frente correspondiente.\n"
        "4. La foto se guarda y se intenta subir a OneDrive.\n\n"
        "🚨 INFORME FLASH\n"
        "Usa el comando:\n"
        "/flash\n\n"
        "Luego completa:\n"
        "1. Pique\n"
        "2. Frente\n"
        "3. Evento\n"
        "4. Detalle escrito\n"
        "5. Foto opcional o botón 'Sin foto'\n\n"
        "📌 Eventos disponibles:\n"
        "- Desprendimientos\n"
        "- Inundación\n"
        "- Sobreexcavación\n"
        "- Tiempo Frente Abierta\n"
        "- Falta Alzaprima\n"
        "- No aplicación de 5cm\n"
        "- Otro\n\n"
        "📤 Al finalizar, el bot guarda el registro y envía el reporte al grupo configurado.\n\n"
        "Otros comandos:\n"
        "/idchat → obtener ID del chat o grupo\n"
        "/cancel → cancelar proceso actual\n"
        "/onedrive_login → iniciar autorización OneDrive\n"
        "/onedrive_finish → terminar autorización OneDrive"
    )

    await update.message.reply_text(texto)


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


# =============== FLOW FLASH ===============
async def flash_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return ConversationHandler.END

    context.user_data.clear()
    user = update.message.from_user
    now = datetime.now()

    context.user_data["flash"] = {
        "id": now.strftime("%Y%m%d%H%M%S"),
        "fecha_hora": now.strftime("%Y-%m-%d %H:%M:%S"),
        "inspector": get_inspector_name(user),
        "usuario_id": str(user.id) if user else "",
        "pique": "",
        "frente": "",
        "evento": "",
        "detalle": "",
        "foto": "",
        "foto_path": "",
    }

    await update.message.reply_text(
        "🚨 INFORME FLASH\n\nSelecciona el PIQUE:",
        reply_markup=build_keyboard(PIQUES, "flash_pique", cols=2),
    )

    return FLASH_PIQUE


async def flash_pique(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if "flash" not in context.user_data:
        await q.edit_message_text("⚠️ No encuentro el informe Flash pendiente. Usa /flash nuevamente.")
        return ConversationHandler.END

    _, pique = q.data.split("|", 1)
    context.user_data["flash"]["pique"] = pique

    await q.edit_message_text(
        f"🏗️ Pique seleccionado: {pique}\n\nAhora selecciona el FRENTE:",
        reply_markup=build_keyboard(FRENTES, "flash_frente", cols=2),
    )

    return FLASH_FRENTE


async def flash_frente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if "flash" not in context.user_data:
        await q.edit_message_text("⚠️ No encuentro el informe Flash pendiente. Usa /flash nuevamente.")
        return ConversationHandler.END

    _, frente = q.data.split("|", 1)
    context.user_data["flash"]["frente"] = frente

    await q.edit_message_text(
        f"📍 Frente seleccionado: {frente}\n\nAhora selecciona el EVENTO:",
        reply_markup=build_keyboard(EVENTOS_FLASH, "flash_evento", cols=1),
    )

    return FLASH_EVENTO


async def flash_evento(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if "flash" not in context.user_data:
        await q.edit_message_text("⚠️ No encuentro el informe Flash pendiente. Usa /flash nuevamente.")
        return ConversationHandler.END

    _, evento = q.data.split("|", 1)
    context.user_data["flash"]["evento"] = evento

    await q.edit_message_text(
        f"⚠️ Evento seleccionado: {evento}\n\n"
        "Ahora escribe el detalle del informe Flash.\n\n"
        "Ejemplo:\n"
        "Frente abierto desde las 10:30 hrs, sin aplicación de shotcrete de 5 cm."
    )

    return FLASH_DETALLE


async def flash_detalle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return ConversationHandler.END

    if "flash" not in context.user_data:
        await update.message.reply_text("⚠️ No encuentro el informe Flash pendiente. Usa /flash nuevamente.")
        return ConversationHandler.END

    detalle = update.message.text or ""
    detalle = detalle.strip()

    if not detalle:
        await update.message.reply_text("⚠️ Debes escribir un detalle para el informe Flash.")
        return FLASH_DETALLE

    context.user_data["flash"]["detalle"] = detalle

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Sin foto", callback_data="flash_sin_foto")]
    ])

    await update.message.reply_text(
        "📸 Ahora puedes enviar una foto como respaldo.\n\n"
        "Si no tienes foto, presiona:",
        reply_markup=keyboard,
    )

    return FLASH_FOTO


async def flash_recibe_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.photo:
        return FLASH_FOTO

    if "flash" not in context.user_data:
        await update.message.reply_text("⚠️ No encuentro el informe Flash pendiente. Usa /flash nuevamente.")
        return ConversationHandler.END

    flash = context.user_data["flash"]
    file = await update.message.photo[-1].get_file()

    fecha_archivo = clean_filename(flash["fecha_hora"])
    pique = clean_filename(flash["pique"])
    frente = clean_filename(flash["frente"])
    evento = clean_filename(flash["evento"])

    nombre = f"FLASH_{fecha_archivo}_{pique}_{frente}_{evento}.jpg"

    foto_dir = os.path.join(FLASH_SAVE_ROOT, "Fotos")
    os.makedirs(foto_dir, exist_ok=True)

    full = os.path.join(foto_dir, nombre)

    await file.download_to_drive(full)
    ensure_saved(full)

    flash["foto"] = nombre
    flash["foto_path"] = full

    return await finalizar_flash(update, context)


async def flash_sin_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if "flash" not in context.user_data:
        await q.edit_message_text("⚠️ No encuentro el informe Flash pendiente. Usa /flash nuevamente.")
        return ConversationHandler.END

    context.user_data["flash"]["foto"] = "Sin foto"
    context.user_data["flash"]["foto_path"] = ""

    await q.edit_message_text("📋 Generando y enviando Informe Flash...")

    return await finalizar_flash(update, context)


def guardar_flash_csv(flash: dict):
    ensure_flash_csv()

    with open(FLASH_CSV, "a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            flash["id"],
            flash["fecha_hora"],
            flash["inspector"],
            flash["usuario_id"],
            flash["pique"],
            flash["frente"],
            flash["evento"],
            flash["detalle"],
            flash["foto"],
        ])


def mensaje_flash(flash: dict) -> str:
    return (
        "🚨 INFORME FLASH\n\n"
        f"🆔 ID: {flash['id']}\n"
        f"📅 Fecha: {flash['fecha_hora']}\n"
        f"👷 Inspector: {flash['inspector']}\n"
        f"🏗️ Pique: {flash['pique']}\n"
        f"📍 Frente: {flash['frente']}\n"
        f"⚠️ Evento: {flash['evento']}\n\n"
        "📝 Detalle:\n"
        f"{flash['detalle']}\n\n"
        f"📸 Foto: {flash['foto']}\n\n"
        "✅ Registro guardado automáticamente."
    )


async def finalizar_flash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    flash = context.user_data.get("flash")

    if not flash:
        if update.message:
            await update.message.reply_text("⚠️ No encuentro el informe Flash pendiente. Usa /flash nuevamente.")
        return ConversationHandler.END

    guardar_flash_csv(flash)

    onedrive_msg = ""

    try:
        upload_to_onedrive(FLASH_CSV, "Flash_Reportes", "flash_reportes.csv")
        onedrive_msg += "\n☁️ CSV actualizado en OneDrive."
    except Exception as e:
        onedrive_msg += f"\n⚠️ CSV guardado localmente, pero no se pudo subir a OneDrive: {e}"

    if flash.get("foto_path"):
        try:
            upload_to_onedrive(flash["foto_path"], "Flash_Reportes/Fotos", flash["foto"])
            onedrive_msg += "\n☁️ Foto Flash subida a OneDrive."
        except Exception as e:
            onedrive_msg += f"\n⚠️ Foto guardada localmente, pero no se pudo subir a OneDrive: {e}"

    texto = mensaje_flash(flash)

    enviado_grupo = False

    if FLASH_GROUP_CHAT_ID:
        try:
            if flash.get("foto_path") and os.path.exists(flash["foto_path"]):
                with open(flash["foto_path"], "rb") as img:
                    await context.bot.send_photo(
                        chat_id=int(FLASH_GROUP_CHAT_ID),
                        photo=img,
                        caption=texto,
                    )
            else:
                await context.bot.send_message(
                    chat_id=int(FLASH_GROUP_CHAT_ID),
                    text=texto,
                )
            enviado_grupo = True
        except Exception as e:
            log.error(f"Error enviando Flash al grupo: {e}")

    context.user_data.clear()

    respuesta = texto

    if enviado_grupo:
        respuesta += "\n\n📤 Informe enviado al grupo."
    else:
        respuesta += "\n\n⚠️ No se pudo enviar al grupo. Revisa FLASH_GROUP_CHAT_ID."

    respuesta += onedrive_msg

    if update.message:
        await update.message.reply_text(respuesta)
    elif update.callback_query:
        await update.callback_query.message.reply_text(respuesta)

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("🛑 Cancelado")
    return ConversationHandler.END


# =============== MAIN ===============
def main():
    start_health()

    app = Application.builder().token(BOT_TOKEN).build()

    flash_conv = ConversationHandler(
        entry_points=[CommandHandler("flash", flash_inicio)],
        states={
            FLASH_PIQUE: [CallbackQueryHandler(flash_pique, pattern=r"^flash_pique\|")],
            FLASH_FRENTE: [CallbackQueryHandler(flash_frente, pattern=r"^flash_frente\|")],
            FLASH_EVENTO: [CallbackQueryHandler(flash_evento, pattern=r"^flash_evento\|")],
            FLASH_DETALLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, flash_detalle)],
            FLASH_FOTO: [
                MessageHandler(filters.PHOTO, flash_recibe_foto),
                CallbackQueryHandler(flash_sin_foto, pattern=r"^flash_sin_foto$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    foto_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.PHOTO, on_photo)],
        states={ASK_PRINCIPAL: [CallbackQueryHandler(choose)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("idchat", idchat))
    app.add_handler(CommandHandler("onedrive_login", onedrive_login))
    app.add_handler(CommandHandler("onedrive_finish", onedrive_finish))

    app.add_handler(flash_conv)
    app.add_handler(foto_conv)

    app.run_polling()


if __name__ == "__main__":
    main()
