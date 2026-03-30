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
    raise RuntimeError("Define BOT_TOKEN en Render (Environment > Secret).")

PHOTO_SAVE_ROOT = os.getenv("PHOTO_SAVE_ROOT", "./photos")
os.makedirs(PHOTO_SAVE_ROOT, exist_ok=True)

FRENTE_CHOICES = [
    "VEE", "BR-OR", "BR-PON", "BR-SUP",
    "TALL-SUP", "TALL-OR", "TALL-PON",
    "LOE-SUP", "LOE-OR", "LOE-PON",
    "LOE-TEA", "LOE-TEB", "LOE-TEC",
    "RS"
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

# OneDrive / Graph
MS_CLIENT_ID = os.getenv("MS_CLIENT_ID", "")
MS_TENANT_ID = os.getenv("MS_TENANT_ID", "common")
MS_SCOPES = ["Files.ReadWrite"]
ONEDRIVE_ROOT = os.getenv("ONEDRIVE_ROOT", "Bot_FotosITO")
TOKEN_CACHE_PATH = os.getenv("TOKEN_CACHE_PATH", "./token_cache.bin")

# Render / Healthcheck
PORT = int(os.getenv("PORT", "10000"))

# =============== LOGGING ===============
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("BotFotosITO")


# =============== HEALTHCHECK SERVER ===============
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format, *args):
        return


def start_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info(f"Healthcheck HTTP server escuchando en 0.0.0.0:{PORT}")
    return server


# =============== CSV ===============
def ensure_csv():
    if not os.path.exists(CSV_LOG):
        with open(CSV_LOG, "w", encoding="utf-8") as f:
            f.write(CSV_HEADER)
        return

    try:
        with open(CSV_LOG, "r", encoding="utf-8") as f:
            first = f.readline()

        if first.strip() != CSV_HEADER.strip():
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup = os.path.join(PHOTO_SAVE_ROOT, f"registro_fotos-{ts}.bak.csv")
            os.replace(CSV_LOG, backup)

            with open(CSV_LOG, "w", encoding="utf-8") as f:
                f.write(CSV_HEADER)

            log.info(f"CSV antiguo respaldado como: {backup}")
    except Exception as e:
        log.warning(f"No se pudo validar CSV, recreando: {e}")
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
    if codigo == "VEE":
        return "VIA ENLACE EXISTENTE"
    if codigo == "RS":
        return "ROMAN SALINAS"
    return "N/A"


def ensure_saved(path: str) -> None:
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    if os.path.getsize(path) <= 0:
        raise IOError("Archivo vacío")


# ---------- Google Sheets helpers ----------
GS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def get_gspread_client():
    credentials_raw = os.getenv("GOOGLE_SHEETS_CREDENTIALS_JSON", "")
    if not credentials_raw:
        raise RuntimeError("Falta GOOGLE_SHEETS_CREDENTIALS_JSON en Render.")

    credentials_dict = json.loads(credentials_raw)
    credentials = Credentials.from_service_account_info(
        credentials_dict,
        scopes=GS_SCOPES,
    )
    return gspread.authorize(credentials)


def get_registro_worksheet():
    sheet_name = os.getenv("GOOGLE_SHEET_NAME", "")
    worksheet_name = os.getenv("GOOGLE_WORKSHEET_REGISTRO", "RegistroFotos")

    if not sheet_name:
        raise RuntimeError("Falta GOOGLE_SHEET_NAME en Render.")

    client = get_gspread_client()
    spreadsheet = client.open(sheet_name)
    return spreadsheet.worksheet(worksheet_name)


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


# ---------- MSAL helpers ----------
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

    authority = f"https://login.microsoftonline.com/{MS_TENANT_ID}"
    cache = load_cache()
    app = msal.PublicClientApplication(
        MS_CLIENT_ID,
        authority=authority,
        token_cache=cache,
    )

    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(MS_SCOPES, account=accounts[0])
        if result and "access_token" in result:
            save_cache(cache)
            return result["access_token"]

    flow = app.initiate_device_flow(scopes=MS_SCOPES)
    if "user_code" not in flow:
        raise RuntimeError("Fallo iniciando device code flow.")

    # Este mensaje sale en logs de Render con el link y el código vigente
    log.info(f"Autoriza OneDrive: {flow['message']}")

    result = app.acquire_token_by_device_flow(flow)
    save_cache(cache)

    if "access_token" not in result:
        raise RuntimeError(f"No se obtuvo token: {result.get('error_description')}")

    return result["access_token"]


def upload_to_onedrive(local_path: str, remote_dir: str, filename: str):
    token = get_graph_token()
    remote_path = f"/{ONEDRIVE_ROOT}/{remote_dir}/{filename}".replace("//", "/")
    url = f"https://graph.microsoft.com/v1.0/me/drive/root:{remote_path}:/content"

    with open(local_path, "rb") as f:
        r = requests.put(url, headers={"Authorization": f"Bearer {token}"}, data=f)

    if r.status_code not in (200, 201):
        raise RuntimeError(f"Graph upload error {r.status_code}: {r.text}")


# =============== HANDLERS ===============
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
        "- DET"
    )


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.photo:
        return

    photo = update.message.photo[-1]
    photo_file = await photo.get_file()

    now_dt = datetime.now()
    fecha = now_dt.strftime("%Y-%m-%d")
    hora = now_dt.strftime("%H:%M:%S")
    timestamp = now_dt.isoformat()

    user = update.message.from_user

    context.user_data["pending"] = {
        "photo_file": photo_file,
        "file_id": photo.file_id,
        "photo_unique_id": photo.file_unique_id,
        "fecha": fecha,
        "hora": hora,
        "timestamp": timestamp,
        "usuario_id": str(user.id),
        "nombre": user.first_name or "",
        "username": user.username or "",
        "chat_id": str(update.message.chat_id),
        "frente": "",
        "secuencia": "",
        "mr_inicio": "",
        "mr_fin": "",
        "mr_unico": "",
        "comentario": "",
    }

    filas = [
        ["VEE", "BR-OR", "BR-PON"],
        ["BR-SUP", "TALL-SUP", "TALL-OR"],
        ["TALL-PON", "LOE-SUP", "LOE-OR"],
        ["LOE-PON", "LOE-TEA", "LOE-TEB"],
        ["LOE-TEC", "RS"],
    ]

    kb = [[InlineKeyboardButton(op, callback_data=f"FRENTE|{op}") for op in fila] for fila in filas]

    await update.message.reply_text(
        "🏷️ Selecciona Frente/Subfrente:",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return ASK_FRENTE


async def choose_frente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    data = q.data or ""
    if not data.startswith("FRENTE|"):
        await q.edit_message_text("❗ Opción no válida.")
        return ASK_FRENTE

    frente = data.split("|", 1)[1]
    if frente not in FRENTE_CHOICES:
        await q.edit_message_text("❗ Frente no válido.")
        return ASK_FRENTE

    pending = context.user_data.get("pending")
    if not pending:
        await q.edit_message_text("⚠️ No encuentro la foto pendiente. Envía una foto de nuevo.")
        return ConversationHandler.END

    pending["frente"] = frente

    kb = [
        [
            InlineKeyboardButton("SOST", callback_data="SEC|SOST"),
            InlineKeyboardButton("REV", callback_data="SEC|REV"),
            InlineKeyboardButton("CB", callback_data="SEC|CB"),
        ],
        [
            InlineKeyboardButton("OQUEDAD", callback_data="SEC|OQUEDAD"),
            InlineKeyboardButton("LANZA", callback_data="SEC|LANZA"),
            InlineKeyboardButton("DET", callback_data="SEC|DET"),
        ],
    ]

    await q.edit_message_text(
        f"✅ Frente seleccionado: {frente}\n\nAhora selecciona la secuencia:",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return ASK_SECUENCIA


async def choose_secuencia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    data = q.data or ""
    if not data.startswith("SEC|"):
        await q.edit_message_text("❗ Opción no válida.")
        return ASK_SECUENCIA

    secuencia = data.split("|", 1)[1]
    if secuencia not in SECUENCIA_CHOICES:
        await q.edit_message_text("❗ Secuencia no válida.")
        return ASK_SECUENCIA

    pending = context.user_data.get("pending")
    if not pending:
        await q.edit_message_text("⚠️ No encuentro la foto pendiente. Envía una foto de nuevo.")
        return ConversationHandler.END

    pending["secuencia"] = secuencia

    if secuencia == "SOST":
        await q.edit_message_text("🔢 Ingresa el número de marco:")
        return ASK_MR_UNICO

    if secuencia in ("REV", "CB"):
        await q.edit_message_text("🔢 Ingresa el marco de inicio:")
        return ASK_MR_INICIO

    await q.edit_message_text("📝 Escribe un comentario opcional o manda '-' para dejarlo vacío:")
    return ASK_COMENTARIO


async def receive_mr_unico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    pending = context.user_data.get("pending")

    if not pending:
        await update.message.reply_text("⚠️ No encuentro la foto pendiente. Envía una foto de nuevo.")
        return ConversationHandler.END

    pending["mr_unico"] = text
    await finalize_record(update, context)
    return ConversationHandler.END


async def receive_mr_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    pending = context.user_data.get("pending")

    if not pending:
        await update.message.reply_text("⚠️ No encuentro la foto pendiente. Envía una foto de nuevo.")
        return ConversationHandler.END

    pending["mr_inicio"] = text
    await update.message.reply_text("🔢 Ahora ingresa el marco final:")
    return ASK_MR_FIN


async def receive_mr_fin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    pending = context.user_data.get("pending")

    if not pending:
        await update.message.reply_text("⚠️ No encuentro la foto pendiente. Envía una foto de nuevo.")
        return ConversationHandler.END

    pending["mr_fin"] = text
    await finalize_record(update, context)
    return ConversationHandler.END


async def receive_comentario(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    pending = context.user_data.get("pending")

    if not pending:
        await update.message.reply_text("⚠️ No encuentro la foto pendiente. Envía una foto de nuevo.")
        return ConversationHandler.END

    pending["comentario"] = "" if text == "-" else text
    await finalize_record(update, context)
    return ConversationHandler.END


async def finalize_record(update_or_query, context: ContextTypes.DEFAULT_TYPE):
    try:
        pending = context.user_data.get("pending")
        if not pending:
            return ConversationHandler.END

        photo_file = pending["photo_file"]
        fecha = pending["fecha"]
        hora = pending["hora"]
        timestamp = pending["timestamp"]
        frente_codigo = pending["frente"]
        secuencia = pending["secuencia"]
        usuario_id = pending["usuario_id"]

        hora_archivo = hora.replace(":", "-")
        nombre = f"{fecha}_{hora_archivo}_{frente_codigo}_{secuencia}_{usuario_id}.jpg"

        subdir = os.path.join(PHOTO_SAVE_ROOT, frente_codigo)
        os.makedirs(subdir, exist_ok=True)
        dest_path = os.path.join(subdir, nombre)

        await photo_file.download_to_drive(custom_path=dest_path)
        ensure_saved(dest_path)

        frente_largo = frente_from_codigo(frente_codigo)
        with open(CSV_LOG, "a", encoding="utf-8") as f:
            f.write(f"{nombre},{frente_largo},{frente_codigo},{timestamp}\n")

        link_foto = ""
        try:
            upload_to_onedrive(dest_path, remote_dir=frente_codigo, filename=nombre)
            od_note = "☁️ Subida a OneDrive OK."
            link_foto = f"/{ONEDRIVE_ROOT}/{frente_codigo}/{nombre}"
        except Exception as e:
            od_note = f"⚠️ OneDrive falló: {e}"
            log.exception("Error en OneDrive")

        record_id = f"FOTO-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

        try:
            registro = {
                "ID_Registro": record_id,
                "Fecha": fecha,
                "Hora": hora,
                "Timestamp": timestamp,
                "Usuario_ID": pending["usuario_id"],
                "Nombre": pending["nombre"],
                "Username": pending["username"],
                "ChatID": pending["chat_id"],
                "Frente": frente_codigo,
                "Secuencia": secuencia,
                "MR_Inicio": pending["mr_inicio"],
                "MR_Fin": pending["mr_fin"],
                "MR_Unico": pending["mr_unico"],
                "Comentario": pending["comentario"],
                "File_ID": pending["file_id"],
                "Photo_Unique_ID": pending["photo_unique_id"],
                "Nombre_Archivo": nombre,
                "Link_Foto": link_foto,
                "Ruta_Carpeta": subdir,
                "Estado": "Activo",
                "Solicitud_Eliminacion": "NO",
                "Motivo_Eliminacion": "",
                "Fecha_Solicitud": "",
                "Aprobacion": "Pendiente",
                "Aprobado_Por": "",
                "Fecha_Aprobacion": "",
                "Observacion_Admin": "",
            }
            append_registro_foto(registro)
            gs_note = "🟢 Registro guardado en Google Sheets."
        except Exception as e:
            gs_note = f"⚠️ Google Sheets falló: {e}"
            log.exception("Error en Google Sheets")

        context.user_data.clear()

        mensaje = (
            "✅ Registro guardado.\n"
            f"🏷️ Frente: {frente_codigo}\n"
            f"🧩 Secuencia: {secuencia}\n"
            f"🗂️ Archivo: {nombre}\n"
            f"🆔 ID: {record_id}\n"
            f"{od_note}\n"
            f"{gs_note}"
        )

        if hasattr(update_or_query, "message") and update_or_query.message:
            await update_or_query.message.reply_text(mensaje)
        else:
            try:
                await update_or_query.edit_message_text(mensaje)
            except Exception:
                chat_id = pending.get("chat_id")
                if chat_id:
                    await context.bot.send_message(chat_id=chat_id, text=mensaje)

        return ConversationHandler.END

    except Exception as e:
        log.exception("Error fatal en finalize_record")
        chat_id = None
        pending = context.user_data.get("pending")
        if pending:
            chat_id = pending.get("chat_id")
        if chat_id:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Error al guardar el registro: {e}"
            )
        context.user_data.clear()
        return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("🛑 Cancelado. Envía una foto para comenzar de nuevo.")
    return ConversationHandler.END


def main():
    start_health_server()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[MessageHandler(filters.PHOTO, on_photo)],
        states={
            ASK_FRENTE: [CallbackQueryHandler(choose_frente)],
            ASK_SECUENCIA: [CallbackQueryHandler(choose_secuencia)],
            ASK_MR_UNICO: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_mr_unico)],
            ASK_MR_INICIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_mr_inicio)],
            ASK_MR_FIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_mr_fin)],
            ASK_COMENTARIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_comentario)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(conv)

    log.info(
        f"Bot iniciado. Guardando local en: {os.path.abspath(PHOTO_SAVE_ROOT)}  | OneDrive root: /{ONEDRIVE_ROOT}"
    )

    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
