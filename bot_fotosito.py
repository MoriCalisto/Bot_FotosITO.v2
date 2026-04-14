
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
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =========================================================
# CONFIG
# =========================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("Falta BOT_TOKEN en Render.")

# Admins: soporta ADMIN_IDS="1,2,3" y/o ADMIN_ID="1"
ADMIN_IDS = set()
_admin_ids_raw = os.getenv("ADMIN_IDS", "").strip()
_admin_id_single = os.getenv("ADMIN_ID", "").strip()

if _admin_ids_raw:
    for x in _admin_ids_raw.split(","):
        x = x.strip()
        if x.isdigit():
            ADMIN_IDS.add(int(x))

if _admin_id_single and _admin_id_single.isdigit():
    ADMIN_IDS.add(int(_admin_id_single))

# Rutas locales
PHOTO_SAVE_ROOT = os.getenv("PHOTO_SAVE_ROOT", "./data/photos").strip()
TOKEN_CACHE_PATH = os.getenv("TOKEN_CACHE_PATH", "./data/token_cache.bin").strip()
FLOW_STORE_PATH = os.getenv("FLOW_STORE_PATH", "./data/pending_onedrive_flows.json").strip()
CSV_LOG = os.path.join(PHOTO_SAVE_ROOT, "registro_fotos.csv")
CSV_HEADER = "Archivo,Frente,Ubicacion,FechaHora\n"

os.makedirs(PHOTO_SAVE_ROOT, exist_ok=True)

token_dir = os.path.dirname(TOKEN_CACHE_PATH)
if token_dir:
    os.makedirs(token_dir, exist_ok=True)

flow_dir = os.path.dirname(FLOW_STORE_PATH)
if flow_dir:
    os.makedirs(flow_dir, exist_ok=True)

# Catálogos
FRENTE_CHOICES = [
    "VEE", "BR-OR", "BR-PON", "BR-SUP",
    "TALL-SUP", "TALL-OR", "TALL-PON",
    "LOE-SUP", "LOE-OR", "LOE-PON",
    "LOE-TEA", "LOE-TEB", "LOE-TEC",
    "RS-PIQ", "RS-SUP"
]

SECUENCIA_CHOICES = ["SOST", "REV", "CB", "OQUEDAD", "LANZA", "DET"]

# Estados conversación
ASK_FRENTE = 0
ASK_SECUENCIA = 1
ASK_MR_UNICO = 2
ASK_MR_INICIO = 3
ASK_MR_FIN = 4
ASK_COMENTARIO = 5

# =========================================================
# LOGGING
# =========================================================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("BotFotosITO")

# =========================================================
# HEALTHCHECK SERVER
# =========================================================
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


# =========================================================
# HELPERS
# =========================================================
def build_menu(buttons, n_cols):
    return [buttons[i:i + n_cols] for i in range(0, len(buttons), n_cols)]


def frente_from_codigo(codigo: str) -> str:
    if codigo.startswith("BR"):
        return "BREMEN"
    if codigo.startswith("TALL"):
        return "TALLERES"
    if codigo.startswith("LOE"):
        return "LO ERRAZURIZ"
    if codigo == "VEE":
        return "VIA ENLACE EXISTENTE"
    if codigo.startswith("RS"):
        return "ROMAN SALINAS"
    return "N/A"


def ensure_saved(path: str) -> None:
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    if os.path.getsize(path) <= 0:
        raise IOError("Archivo vacío")


def ensure_csv():
    if not os.path.exists(CSV_LOG):
        with open(CSV_LOG, "w", encoding="utf-8") as f:
            f.write(CSV_HEADER)


ensure_csv()


# =========================================================
# GOOGLE SHEETS
# =========================================================
GS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
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
    sheet_name = os.getenv("GOOGLE_SHEET_NAME", "").strip()
    worksheet_name = os.getenv("GOOGLE_WORKSHEET_REGISTRO", "RegistroFotos").strip()

    if not sheet_name:
        raise RuntimeError("Falta GOOGLE_SHEET_NAME en Render.")

    return client.open(sheet_name).worksheet(worksheet_name)

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


# =========================================================
# ONEDRIVE / MSAL
# =========================================================
MS_CLIENT_ID = (os.getenv("MS_CLIENT_ID", "") or os.getenv("CLIENT_ID", "")).strip()
MS_TENANT_ID = (os.getenv("MS_TENANT_ID", "") or os.getenv("TENANT_ID", "") or "common").strip()
MS_SCOPES = ["Files.ReadWrite"]
ONEDRIVE_ROOT = os.getenv("ONEDRIVE_ROOT", "Bot_FotosITO").strip()

def load_cache():
    cache = msal.SerializableTokenCache()
    if os.path.exists(TOKEN_CACHE_PATH):
        try:
            with open(TOKEN_CACHE_PATH, "r", encoding="utf-8") as f:
                cache.deserialize(f.read())
        except Exception as e:
            log.warning(f"No se pudo leer token cache: {e}")
    return cache

def save_cache(cache):
    if cache.has_state_changed:
        with open(TOKEN_CACHE_PATH, "w", encoding="utf-8") as f:
            f.write(cache.serialize())

def load_pending_flows():
    if os.path.exists(FLOW_STORE_PATH):
        try:
            with open(FLOW_STORE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as e:
            log.warning(f"No se pudo leer pending flows: {e}")
    return {}

def save_pending_flows(flows):
    try:
        with open(FLOW_STORE_PATH, "w", encoding="utf-8") as f:
            json.dump(flows, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"No se pudo guardar pending flows: {e}", exc_info=True)

PENDING_ONEDRIVE_FLOWS = load_pending_flows()

def persist_flow(user_id: int, flow: dict):
    PENDING_ONEDRIVE_FLOWS[str(user_id)] = flow
    save_pending_flows(PENDING_ONEDRIVE_FLOWS)

def pop_flow(user_id: int):
    flow = PENDING_ONEDRIVE_FLOWS.pop(str(user_id), None)
    save_pending_flows(PENDING_ONEDRIVE_FLOWS)
    return flow

def get_flow(user_id: int):
    return PENDING_ONEDRIVE_FLOWS.get(str(user_id))

def build_msal_app(cache=None):
    return msal.PublicClientApplication(
        MS_CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{MS_TENANT_ID}",
        token_cache=cache,
    )

def get_graph_token():
    if not MS_CLIENT_ID:
        raise RuntimeError("Falta MS_CLIENT_ID o CLIENT_ID en Render.")

    cache = load_cache()
    app = build_msal_app(cache)

    accounts = app.get_accounts()
    if not accounts:
        raise RuntimeError("No hay cuenta Microsoft en caché. Ejecuta /onedrive_login")

    result = app.acquire_token_silent(MS_SCOPES, account=accounts[0])

    if result and "access_token" in result:
        save_cache(cache)
        return result["access_token"]

    raise RuntimeError(f"No se pudo obtener token silencioso. Detalle: {result}")

def upload_to_onedrive(local_path: str, remote_dir: str, filename: str):
    token = get_graph_token()

    safe_remote_dir = (remote_dir or "").strip().replace("\\", "/")
    remote_path = f"/{ONEDRIVE_ROOT}/{safe_remote_dir}/{filename}".replace("//", "/")

    url = f"https://graph.microsoft.com/v1.0/me/drive/root:{remote_path}:/content"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/octet-stream",
    }

    with open(local_path, "rb") as f:
        data = f.read()

    response = requests.put(url, headers=headers, data=data, timeout=120)

    if response.status_code >= 400:
        raise RuntimeError(f"Graph upload error {response.status_code}: {response.text}")

    return response.json()


# =========================================================
# DECORADOR ADMIN
# =========================================================
def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or user.id not in ADMIN_IDS:
            if update.effective_message:
                await update.effective_message.reply_text("⛔ Acceso denegado.")
            return
        return await func(update, context)
    return wrapper


# =========================================================
# COMANDOS
# =========================================================

# =========================================================
# START / HELP
# =========================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()

    mensaje = (
        "👷‍♂️ <b>Bot Fotos ITO v2</b>\n\n"
        "📸 Envía una foto para iniciar el registro.\n\n"
        "<b>Flujo:</b>\n"
        "1. Foto\n"
        "2. Frente\n"
        "3. Secuencia\n"
        "4. Marco / Comentario\n\n"
        "⚠️ <b>Estándar foto:</b> idealmente ~6 marcos visibles."
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("ℹ️ Ver ayuda", callback_data="HELP|OPEN")]
    ])

    await update.message.reply_text(
        mensaje,
        parse_mode="HTML",
        reply_markup=keyboard
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mensaje = (
        "📘 <b>AYUDA – Bot Fotos ITO v2</b>\n\n"

        "<b>🔄 Flujo de uso:</b>\n"
        "1. 📸 Enviar foto\n"
        "2. 🏷️ Seleccionar frente\n"
        "3. 🧩 Seleccionar secuencia\n"
        "4. ✏️ Ingresar marco o comentario\n"
        "5. ✅ Registro automático en sistema\n\n"

        
        "<b>🏷️ Frentes principales:</b>\n"
        "• Bremen\n"
        "• Talleres\n"
        "• Lo Errázuriz\n"
        "• Vía Enlace Existente (VEE)\n"
        "• Román Salinas\n\n"

        "<b>📍 Subsectores:</b>\n"
        "• Superficie\n"
        "• Oriente\n"
        "• Poniente\n"
        "• Túnel Estación A (TEA)\n"
        "• Túnel Estación B (TEB)\n"
        "• Túnel Estación C (TEC)\n\n"

        "<b>🧩 Secuencias:</b>\n"
        "• SOST → Sostenimiento\n"
        "• REV → Revestimiento\n"
        "• CB → Contrabóveda\n"
        "• OQUEDAD → Desprendimientos o Condición terreno\n"
        "• LANZA → Lanzas / Intrucciones de Ingenieria\n"
        "• DET → Detención\n\n"

        "<b>🔢 Número de marco:</b>\n"
        "Debe ingresar solo números (ej: 123).\n"
        "No usar letras ni texto.\n\n"
        
        "<b>✏️ Ingreso de datos:</b>\n"
        "• SOST → 1 marco\n"
        "• REV / CB → rango de marco inicio-fin\n"
        "• Otros → comentario o '.' para omitir\n\n"

        "<b>📸 Fotografía:</b>\n"
        "• Ideal: ~6 marcos\n"
        "• Clara y enfocada\n"
        "• Sin obstrucciones\n\n"

        "<b>🎮 Comando útil:</b>\n"
        "/cancel → cancelar registro actual"
    )

        
    if update.message:
        await update.message.reply_text(mensaje, parse_mode="HTML")
    elif update.callback_query:
        await update.callback_query.message.reply_text(mensaje, parse_mode="HTML")


async def cb_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await cmd_help(update, context)

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🏓 Bot activo.")

@admin_only
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = (
        "🛠️ PANEL DE ADMINISTRADOR\n\n"
        f"Admins configurados: {len(ADMIN_IDS)}\n"
        f"Tu ID: {update.effective_user.id}\n\n"
        "Comandos:\n"
        "/admin\n"
        "/onedrive_status\n"
        "/onedrive_login\n"
        "/onedrive_finish\n"
        "/cancel"
    )
    await update.message.reply_text(texto)

async def cmd_onedrive_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        token = get_graph_token()
        if token:
            await update.message.reply_text("✅ OneDrive conectado y token válido.")
            return
    except Exception as e:
        await update.message.reply_text(f"⚠️ OneDrive no está listo:\n{str(e)}")

async def cmd_onedrive_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not MS_CLIENT_ID:
        await update.message.reply_text("❌ Falta MS_CLIENT_ID o CLIENT_ID en Render.")
        return

    cache = load_cache()
    app = build_msal_app(cache)

    try:
        flow = app.initiate_device_flow(scopes=MS_SCOPES)
    except Exception as e:
        log.error(f"Error iniciando device flow: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Error al iniciar Microsoft Auth:\n{str(e)}")
        return

    if "user_code" not in flow:
        await update.message.reply_text(
            f"❌ Error al iniciar Microsoft Auth:\n{json.dumps(flow, ensure_ascii=False)}"
        )
        return

    persist_flow(update.effective_user.id, flow)

    msg = flow.get("message") or (
        f"Ve a {flow.get('verification_uri')} e ingresa el código {flow.get('user_code')}"
    )

    await update.message.reply_text(
        "🔗 Login OneDrive iniciado\n\n"
        f"{msg}\n\n"
        "Cuando termines, vuelve aquí y ejecuta /onedrive_finish"
    )

async def cmd_onedrive_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    flow = get_flow(user_id)

    if not flow:
        await update.message.reply_text(
            "⚠️ No encontré un login pendiente.\n"
            "Puede que el flujo haya expirado.\n"
            "Vuelve a ejecutar /onedrive_login"
        )
        return

    cache = load_cache()
    app = build_msal_app(cache)

    await update.message.reply_text("⏳ Verificando autorización en Microsoft...")

    try:
        result = await asyncio.to_thread(
            lambda: app.acquire_token_by_device_flow(flow, timeout=5)
        )

        if result and "access_token" in result:
            save_cache(cache)
            pop_flow(user_id)
            await update.message.reply_text("✅ OneDrive conectado correctamente.")
        else:
            err = "Error desconocido"
            if isinstance(result, dict):
                err = result.get("error_description") or result.get("error") or str(result)

            await update.message.reply_text(
                "⚠️ Aún no aparece la autorización o hubo un problema.\n\n"
                f"Detalle: {err}\n\n"
                "Después de ingresar el código en Microsoft, espera unos segundos y vuelve a ejecutar /onedrive_finish"
            )

    except Exception as e:
        log.error(f"Error en /onedrive_finish: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Error al finalizar login de OneDrive:\n{str(e)}")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("🛑 Cancelado. Envía una foto de nuevo.")
    return ConversationHandler.END


# =========================================================
# FLUJO PRINCIPAL FOTO
# =========================================================
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return ConversationHandler.END

    if not update.message.photo and not update.message.document:
        return ConversationHandler.END

    usuario = update.effective_user
    if not usuario:
        await update.message.reply_text("❌ No pude identificar el usuario.")
        return ConversationHandler.END

    now = datetime.now()

    try:
        if update.message.photo:
            tg_file = await update.message.photo[-1].get_file()
            file_id = update.message.photo[-1].file_id
            photo_unique_id = update.message.photo[-1].file_unique_id
        else:
            if not update.message.document.mime_type or not update.message.document.mime_type.startswith("image/"):
                await update.message.reply_text("❌ El archivo enviado no es una imagen.")
                return ConversationHandler.END

            tg_file = await update.message.document.get_file()
            file_id = update.message.document.file_id
            photo_unique_id = update.message.document.file_unique_id

        username = usuario.username or ""
        nombre = usuario.first_name or ""
        apellido = usuario.last_name or ""
        nombre_completo = (nombre + " " + apellido).strip()

        base_filename = f"{now.strftime('%Y%m%d_%H%M%S')}_{usuario.id}.jpg"
        local_path = os.path.join(PHOTO_SAVE_ROOT, base_filename)

        await tg_file.download_to_drive(local_path)

        context.user_data["pending"] = {
            "usuario_id": str(usuario.id),
            "nombre": nombre_completo,
            "username": username,
            "chat_id": str(update.effective_chat.id) if update.effective_chat else "",
            "file_id": file_id,
            "photo_unique_id": photo_unique_id,
            "filename": base_filename,
            "local_path": local_path,
            "fecha": now.strftime("%d/%m/%Y"),
            "hora": now.strftime("%H:%M:%S"),
            "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
        }

        botones_frente = [
            InlineKeyboardButton(f, callback_data=f"FRENTE|{f}")
            for f in FRENTE_CHOICES
        ]
        reply_markup = InlineKeyboardMarkup(build_menu(botones_frente, 3))

        await update.message.reply_text(
            "📸 Imagen guardada localmente.\nSelecciona el Frente:",
            reply_markup=reply_markup
        )
        return ASK_FRENTE

    except Exception as e:
        log.error(f"Error en on_photo: {e}", exc_info=True)
        await update.message.reply_text("❌ Error al descargar la imagen. Intenta nuevamente.")
        context.user_data.clear()
        return ConversationHandler.END

async def choose_frente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    pending = context.user_data.get("pending")
    if not pending:
        await query.edit_message_text("⚠️ La sesión expiró. Envía la foto nuevamente.")
        return ConversationHandler.END

    data = query.data or ""
    if not data.startswith("FRENTE|"):
        await query.edit_message_text("❌ Frente inválido.")
        return ConversationHandler.END

    frente = data.split("|", 1)[1]
    if frente not in FRENTE_CHOICES:
        await query.edit_message_text("❌ Frente no reconocido.")
        return ConversationHandler.END

    pending["frente"] = frente
    log.info(f"Frente seleccionado: {frente} | user={pending.get('usuario_id')}")

    botones_secuencia = [
        InlineKeyboardButton(s, callback_data=f"SEC|{s}")
        for s in SECUENCIA_CHOICES
    ]
    reply_markup = InlineKeyboardMarkup(build_menu(botones_secuencia, 3))

    await query.edit_message_text(
        f"✅ Frente: {frente}\nSelecciona Secuencia:",
        reply_markup=reply_markup
    )
    return ASK_SECUENCIA

async def choose_secuencia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    pending = context.user_data.get("pending")
    if not pending:
        await query.edit_message_text("⚠️ La sesión expiró. Envía la foto nuevamente.")
        return ConversationHandler.END

    data = query.data or ""
    if not data.startswith("SEC|"):
        await query.edit_message_text("❌ Secuencia inválida.")
        return ConversationHandler.END

    secuencia = data.split("|", 1)[1]
    if secuencia not in SECUENCIA_CHOICES:
        await query.edit_message_text("❌ Secuencia no reconocida.")
        return ConversationHandler.END

    pending["secuencia"] = secuencia
    log.info(f"Secuencia seleccionada: {secuencia} | user={pending.get('usuario_id')}")

    if secuencia == "SOST":
        await query.edit_message_text(
            f"✅ Secuencia: {secuencia}\nIngresa el Marco Único:"
        )
        return ASK_MR_UNICO

    if secuencia in ["REV", "CB"]:
        await query.edit_message_text(
            f"✅ Secuencia: {secuencia}\nIngresa el Marco de Inicio:"
        )
        return ASK_MR_INICIO

    await query.edit_message_text(
        f"✅ Secuencia: {secuencia}\nIngresa un comentario (o '-' para omitir):"
    )
    return ASK_COMENTARIO

async def receive_mr_unico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = context.user_data.get("pending")
    if not pending:
        await update.message.reply_text("⚠️ La sesión expiró. Envía la foto nuevamente.")
        return ConversationHandler.END

    pending["mr_unico"] = (update.message.text or "").strip()
    return await finalize_record(update, context)

async def receive_mr_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = context.user_data.get("pending")
    if not pending:
        await update.message.reply_text("⚠️ La sesión expiró. Envía la foto nuevamente.")
        return ConversationHandler.END

    pending["mr_inicio"] = (update.message.text or "").strip()
    await update.message.reply_text("Ingresa el Marco de Fin:")
    return ASK_MR_FIN

async def receive_mr_fin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = context.user_data.get("pending")
    if not pending:
        await update.message.reply_text("⚠️ La sesión expiró. Envía la foto nuevamente.")
        return ConversationHandler.END

    pending["mr_fin"] = (update.message.text or "").strip()
    return await finalize_record(update, context)

async def receive_comentario(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = context.user_data.get("pending")
    if not pending:
        await update.message.reply_text("⚠️ La sesión expiró. Envía la foto nuevamente.")
        return ConversationHandler.END

    comentario = (update.message.text or "").strip()
    pending["comentario"] = "" if comentario == "-" else comentario
    return await finalize_record(update, context)

def build_file_name_for_storage(pending: dict) -> str:
    fecha = datetime.now().strftime("%Y%m%d")
    hora = datetime.now().strftime("%H%M%S")
    frente = pending.get("frente", "SIN-FRENTE").replace(" ", "_")
    secuencia = pending.get("secuencia", "SIN-SEC").replace(" ", "_")
    usuario_id = pending.get("usuario_id", "0")

    mr = "SINMR"
    if pending.get("mr_unico"):
        mr = f"MR{pending['mr_unico']}"
    elif pending.get("mr_inicio") and pending.get("mr_fin"):
        mr = f"MR{pending['mr_inicio']}-{pending['mr_fin']}"

    return f"{fecha}_{hora}_{frente}_{secuencia}_{mr}_{usuario_id}.jpg"

async def finalize_record(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        pending = context.user_data.get("pending")
        if not pending:
            return ConversationHandler.END

        await update.message.reply_text("⏳ Procesando guardado en la nube...")

        final_filename = build_file_name_for_storage(pending)

        old_local_path = pending["local_path"]
        new_local_path = os.path.join(PHOTO_SAVE_ROOT, final_filename)

        try:
            if old_local_path != new_local_path and os.path.exists(old_local_path):
                os.replace(old_local_path, new_local_path)
                pending["local_path"] = new_local_path
                pending["filename"] = final_filename
        except Exception as e:
            log.warning(f"No se pudo renombrar archivo local: {e}")

        row_base = {
            "ID_Registro": f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{pending['usuario_id']}",
            "Fecha": pending["fecha"],
            "Hora": pending["hora"],
            "Timestamp": pending["timestamp"],
            "Usuario_ID": pending["usuario_id"],
            "Nombre": pending["nombre"],
            "Username": pending["username"],
            "ChatID": pending["chat_id"],
            "Frente": pending["frente"],
            "Secuencia": pending["secuencia"],
            "MR_Inicio": pending.get("mr_inicio", ""),
            "MR_Fin": pending.get("mr_fin", ""),
            "MR_Unico": pending.get("mr_unico", ""),
            "Comentario": pending.get("comentario", ""),
            "File_ID": pending.get("file_id", ""),
            "Photo_Unique_ID": pending.get("photo_unique_id", ""),
            "Nombre_Archivo": pending["filename"],
            "Link_Foto": "",
            "Ruta_Carpeta": f"{ONEDRIVE_ROOT}/{pending['frente']}",
            "Estado": "Activo",
            "Solicitud_Eliminacion": "NO",
            "Motivo_Eliminacion": "",
            "Fecha_Solicitud": "",
            "Aprobacion": "Pendiente",
            "Aprobado_Por": "",
            "Fecha_Aprobacion": "",
            "Observacion_Admin": "",
        }

        filas_a_insertar = []

        if pending["secuencia"] in ["REV", "CB"] and pending.get("mr_inicio") and pending.get("mr_fin"):
            try:
                inicio = int(pending["mr_inicio"])
                fin = int(pending["mr_fin"])
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
        sheets_error = ""
        try:
            ws = get_registro_worksheet()
            ws.append_rows(filas_a_insertar, value_input_option="USER_ENTERED")
            sheets_ok = True
        except Exception as e:
            sheets_error = str(e)
            log.error(f"Error Sheets: {e}", exc_info=True)

        onedrive_ok = False
        onedrive_error = ""
        try:
            upload_to_onedrive(
                pending["local_path"],
                pending["frente"],
                pending["filename"]
            )
            onedrive_ok = True
        except Exception as e:
            onedrive_error = str(e)
            log.error(f"Error OneDrive: {e}", exc_info=True)

        resumen = "✅ Registro finalizado\n\n"
        resumen += f"📊 Google Sheets: {'OK' if sheets_ok else 'ERROR'}\n"
        resumen += f"☁️ OneDrive: {'OK' if onedrive_ok else 'ERROR'}\n\n"
        resumen += f"Frente: {pending['frente']}\n"
        resumen += f"Secuencia: {pending['secuencia']}\n"

        if pending.get("mr_unico"):
            resumen += f"Marco: {pending['mr_unico']}\n"
        elif pending.get("mr_inicio") and pending.get("mr_fin"):
            resumen += f"Marcos: {pending['mr_inicio']} al {pending['mr_fin']}\n"

        if pending.get("comentario"):
            resumen += f"Comentario: {pending['comentario']}\n"

        resumen += f"Archivo: {pending['filename']}\n"

        if not sheets_ok and sheets_error:
            resumen += f"\nDetalle Sheets: {sheets_error[:300]}"

        if not onedrive_ok and onedrive_error:
            resumen += f"\nDetalle OneDrive: {onedrive_error[:300]}"

        await update.message.reply_text(resumen)

    except Exception as e:
        log.error(f"Error crítico en finalize_record: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Error interno del bot.\nDetalle: {str(e)}")
    finally:
        context.user_data.clear()

    return ConversationHandler.END


# =========================================================
# ERROR HANDLER
# =========================================================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Excepción no controlada", exc_info=context.error)


# =========================================================
# MAIN
# =========================================================
def main():
    start_health_server()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = Application.builder().token(BOT_TOKEN).concurrent_updates(False).build()

    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_photo)
        ],
        states={
            ASK_FRENTE: [CallbackQueryHandler(choose_frente, pattern=r"^FRENTE\|")],
            ASK_SECUENCIA: [CallbackQueryHandler(choose_secuencia, pattern=r"^SEC\|")],
            ASK_MR_UNICO: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_mr_unico)],
            ASK_MR_INICIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_mr_inicio)],
            ASK_MR_FIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_mr_fin)],
            ASK_COMENTARIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_comentario)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
        per_chat=True,
        per_user=True,
        per_message=False,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("onedrive_status", cmd_onedrive_status))
    app.add_handler(CommandHandler("onedrive_login", cmd_onedrive_login))
    app.add_handler(CommandHandler("onedrive_finish", cmd_onedrive_finish))
    app.add_handler(CallbackQueryHandler(cb_help, pattern=r"^HELP\|OPEN$"))
    app.add_handler(conv_handler)

    app.add_error_handler(error_handler)

    log.info("Bot iniciado...")
    app.run_polling(drop_pending_updates=False, close_loop=False)

if __name__ == "__main__":
    main()
