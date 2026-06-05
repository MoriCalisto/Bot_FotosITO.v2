# -*- coding: utf-8 -*-
import os
import csv
import re
import logging
import threading
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
import msal

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.worksheet.table import Table, TableStyleInfo

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

# ============================================================
# CONFIG GENERAL
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise RuntimeError("Define BOT_TOKEN en Render.")

CHILE_TZ = ZoneInfo("America/Santiago")

PHOTO_SAVE_ROOT = os.getenv("PHOTO_SAVE_ROOT", "./photos")
FLASH_SAVE_ROOT = os.getenv("FLASH_SAVE_ROOT", "./Flash_Reportes")
DATA_ROOT = os.getenv("DATA_ROOT", "./data")

os.makedirs(PHOTO_SAVE_ROOT, exist_ok=True)
os.makedirs(FLASH_SAVE_ROOT, exist_ok=True)
os.makedirs(DATA_ROOT, exist_ok=True)

FLASH_GROUP_CHAT_ID = os.getenv("FLASH_GROUP_CHAT_ID", "")

MS_CLIENT_ID = os.getenv("MS_CLIENT_ID", "")
MS_TENANT_ID = os.getenv("MS_TENANT_ID", "common")
MS_SCOPES = ["Files.ReadWrite", "offline_access", "User.Read"]
ONEDRIVE_ROOT = os.getenv("ONEDRIVE_ROOT", "Bot_FotosITO")
TOKEN_CACHE_PATH = os.getenv("TOKEN_CACHE_PATH", "./token_cache.bin")

PORT = int(os.getenv("PORT", "10000"))

# ============================================================
# OPCIONES FORMULARIO FOTOS
# ============================================================

PHOTO_PIQUE, PHOTO_FRENTE, PHOTO_MARCO, PHOTO_ETAPA, PHOTO_COMENTARIO = range(1, 6)

PHOTO_PIQUES = [
    "Túnel Enlace",
    "Bremen",
    "Talleres",
    "Lo Errázuriz",
    "Román Salinas",
    "Otros",
]

FRENTES_BASE = [
    "TIE Oriente",
    "TIE Poniente",
    "Superficie",
    "Pique",
]

FRENTES_LOE = [
    "TIE Oriente",
    "TIE Poniente",
    "Superficie",
    "Pique",
    "TEA",
    "TEB",
    "TEC",
]

FRENTES_RS = [
    "Pique",
    "Superficie",
]

# ============================================================
# OPCIONES INFORME FLASH
# ============================================================

FLASH_PIQUE, FLASH_FRENTE, FLASH_EVENTO, FLASH_DETALLE, FLASH_FOTO = range(10, 15)

FLASH_PIQUES = [
    "Túnel Enlace",
    "Bremen",
    "Talleres",
    "Lo Errázuriz",
    "Román Salinas",
    "VEE",
    "Otros",
]

FLASH_FRENTES = [
    "TIE Oriente",
    "TIE Poniente",
    "Galería Oriente",
    "Galería Poniente",
    "TEA",
    "TEB",
    "TEC",
    "Tramo B",
    "Tramo C",
    "Superficie",
    "Pique",
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

# ============================================================
# ARCHIVOS
# ============================================================

PHOTO_XLSX = os.path.join(DATA_ROOT, "Registro_Fotos.xlsx")
PHOTO_SHEET = "Registro_Fotos"

PHOTO_HEADER = [
    "ID",
    "Fecha",
    "Hora",
    "FechaHora",
    "Usuario",
    "UsuarioID",
    "Pique",
    "Frente",
    "Marco",
    "Etapa",
    "Comentario",
    "Archivo",
    "RutaOneDrive",
    "Estado",
]

FLASH_XLSX = os.path.join(FLASH_SAVE_ROOT, "Flash_Reportes.xlsx")
FLASH_SHEET = "Registro_Flash"

FLASH_HEADER = [
    "ID",
    "Fecha",
    "Hora",
    "FechaHora",
    "Inspector",
    "UsuarioID",
    "Pique",
    "Frente",
    "Evento",
    "Detalle",
    "Foto",
    "Estado",
]

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("BotFotosITO")

PENDING_ONEDRIVE_FLOWS = {}

# ============================================================
# HEALTHCHECK
# ============================================================

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


# ============================================================
# UTILS
# ============================================================

def now_chile():
    return datetime.now(CHILE_TZ)


def normalizar_texto(texto: str) -> str:
    texto = str(texto).strip()
    reemplazos = {
        "á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u",
        "Á": "A", "É": "E", "Í": "I", "Ó": "O", "Ú": "U",
        "ñ": "n", "Ñ": "N",
        "ü": "u", "Ü": "U",
    }
    for a, b in reemplazos.items():
        texto = texto.replace(a, b)
    return texto


def clean_filename(text: str) -> str:
    text = normalizar_texto(text)
    text = re.sub(r"[^\w\-]+", "_", text)
    text = re.sub(r"_+", "_", text)
    return text.strip("_")


def get_user_name(user) -> str:
    if not user:
        return "SinUsuario"
    return user.username or user.full_name or str(user.id)


def get_user_label(user) -> str:
    if not user:
        return "Sin usuario"
    if user.username:
        return f"{user.full_name or user.username} (@{user.username})"
    return user.full_name or str(user.id)


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


def ensure_saved(path: str):
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    if os.path.getsize(path) <= 0:
        raise IOError("Archivo vacío")


def frentes_por_pique(pique: str):
    if pique == "Lo Errázuriz":
        return FRENTES_LOE
    if pique == "Román Salinas":
        return FRENTES_RS
    return FRENTES_BASE


def requiere_marco(frente: str) -> bool:
    return frente in ["TIE Oriente", "TIE Poniente"]


def requiere_etapa(frente: str) -> bool:
    return frente == "Pique"


def make_photo_filename(data: dict):
    fecha = data["fecha_archivo"]
    hora = data["hora_archivo"]
    pique = clean_filename(data["pique"])
    frente = clean_filename(data["frente"])
    usuario = clean_filename(data["usuario"])

    extra = ""

    if data.get("marco"):
        extra = f"_MR-{clean_filename(data['marco'])}"
    elif data.get("etapa"):
        extra = f"_ETAPA-{clean_filename(data['etapa'])}"

    return f"{fecha}_{hora}_{pique}_{frente}{extra}_{usuario}.jpg"


def make_onedrive_photo_folder(data: dict):
    pique = clean_filename(data["pique"])
    frente = clean_filename(data["frente"])
    return f"Fotos/{pique}/{frente}"


# ============================================================
# EXCEL HELPERS
# ============================================================

def crear_excel_si_no_existe(path, sheet_name, headers):
    if os.path.exists(path):
        return

    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name
    ws.append(headers)

    aplicar_formato_excel(ws, len(headers))
    wb.save(path)


def aplicar_formato_excel(ws, total_cols):
    header_fill = PatternFill("solid", fgColor="111827")
    header_font = Font(color="FFFFFF", bold=True)
    row_fill = PatternFill("solid", fgColor="F8FAFC")
    thin = Side(style="thin", color="D9E2F3")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    max_row = ws.max_row

    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = border

    for row in ws.iter_rows(min_row=2, max_row=max_row, max_col=total_cols):
        for cell in row:
            cell.border = border
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            if cell.row % 2 == 0:
                cell.fill = row_fill

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{chr(64 + total_cols)}{max_row}"

    widths = {
        1: 18, 2: 14, 3: 12, 4: 22, 5: 28, 6: 14,
        7: 20, 8: 22, 9: 18, 10: 18, 11: 55,
        12: 55, 13: 45, 14: 18,
    }

    for col_idx in range(1, total_cols + 1):
        letter = ws.cell(row=1, column=col_idx).column_letter
        ws.column_dimensions[letter].width = widths.get(col_idx, 20)

    ws.row_dimensions[1].height = 26

    for r in range(2, max_row + 1):
        ws.row_dimensions[r].height = 36


def guardar_registro_excel(path, sheet_name, headers, row_values, table_name):
    crear_excel_si_no_existe(path, sheet_name, headers)

    wb = load_workbook(path)
    ws = wb[sheet_name]
    ws.append(row_values)

    aplicar_formato_excel(ws, len(headers))

    max_row = ws.max_row
    max_col_letter = ws.cell(row=1, column=len(headers)).column_letter
    table_ref = f"A1:{max_col_letter}{max_row}"

    if table_name not in ws.tables:
        tab = Table(displayName=table_name, ref=table_ref)
        style = TableStyleInfo(
            name="TableStyleMedium2",
            showFirstColumn=False,
            showLastColumn=False,
            showRowStripes=True,
            showColumnStripes=False,
        )
        tab.tableStyleInfo = style
        ws.add_table(tab)
    else:
        ws.tables[table_name].ref = table_ref

    wb.save(path)


def guardar_photo_metadata(data: dict):
    guardar_registro_excel(
        PHOTO_XLSX,
        PHOTO_SHEET,
        PHOTO_HEADER,
        [
            data["id"],
            data["fecha"],
            data["hora"],
            data["fecha_hora"],
            data["usuario_label"],
            data["usuario_id"],
            data["pique"],
            data["frente"],
            data.get("marco", ""),
            data.get("etapa", ""),
            data.get("comentario", ""),
            data["archivo"],
            data.get("ruta_onedrive", ""),
            data.get("estado", "Registrado"),
        ],
        "TablaRegistroFotos",
    )


def guardar_flash_xlsx(flash: dict):
    guardar_registro_excel(
        FLASH_XLSX,
        FLASH_SHEET,
        FLASH_HEADER,
        [
            flash["id"],
            flash["fecha"],
            flash["hora"],
            flash["fecha_hora"],
            flash["inspector"],
            flash["usuario_id"],
            flash["pique"],
            flash["frente"],
            flash["evento"],
            flash["detalle"],
            flash["foto"],
            flash["estado"],
        ],
        "TablaFlash",
    )


crear_excel_si_no_existe(PHOTO_XLSX, PHOTO_SHEET, PHOTO_HEADER)
crear_excel_si_no_existe(FLASH_XLSX, FLASH_SHEET, FLASH_HEADER)

# ============================================================
# TOKEN CACHE
# ============================================================

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
        folder = os.path.dirname(TOKEN_CACHE_PATH)
        if folder:
            os.makedirs(folder, exist_ok=True)

        with open(TOKEN_CACHE_PATH, "w", encoding="utf-8") as f:
            f.write(cache.serialize())


def build_msal_app(cache=None):
    return msal.PublicClientApplication(
        MS_CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{MS_TENANT_ID}",
        token_cache=cache,
    )


# ============================================================
# ONEDRIVE
# ============================================================

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

    root = ONEDRIVE_ROOT.strip("/")
    folder = folder.strip("/")

    if folder:
        remote_path = f"{root}/{folder}/{filename}"
    else:
        remote_path = f"{root}/{filename}"

    url = f"https://graph.microsoft.com/v1.0/me/drive/root:/{remote_path}:/content"

    with open(local_path, "rb") as f:
        r = requests.put(
            url,
            headers={"Authorization": f"Bearer {token}"},
            data=f,
        )

    if r.status_code not in (200, 201):
        raise RuntimeError(f"Graph upload error {r.status_code}: {r.text}")

    return remote_path


# ============================================================
# COMANDOS GENERALES
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await help_cmd(update, context)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = (
        "🚇 *BOT FOTOS ITO + REPORTES FLASH EL6*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Sistema operativo para registrar evidencia en terreno, emitir alertas críticas y respaldar información en OneDrive.\n\n"

        "📸 *REGISTRO DE FOTOS EN TERRENO*\n"
        "Envía una foto directamente al bot.\n\n"
        "El bot preguntará:\n"
        "1️⃣ *Pique*\n"
        "   • Túnel Enlace\n"
        "   • Bremen\n"
        "   • Talleres\n"
        "   • Lo Errázuriz\n"
        "   • Román Salinas\n"
        "   • Otros\n\n"
        "2️⃣ *Frente*\n"
        "   • TIE Oriente\n"
        "   • TIE Poniente\n"
        "   • Superficie\n"
        "   • Pique\n"
        "   • TEA / TEB / TEC solo para Lo Errázuriz\n\n"
        "3️⃣ *Dato técnico si corresponde*\n"
        "   • Si es TIE Oriente o TIE Poniente → pide N° de Marco\n"
        "   • Si es Pique → pide Etapa\n"
        "   • Si es Superficie / TEA / TEB / TEC → omite este paso\n\n"
        "4️⃣ *Comentario opcional*\n"
        "   Puedes escribir una observación o presionar `Sin comentario`.\n\n"
        "✅ El bot guarda:\n"
        "   • Foto ordenada por fecha y metadata\n"
        "   • Registro en `Registro_Fotos.xlsx`\n"
        "   • Respaldo en OneDrive\n\n"

        "🚨 *INFORME FLASH*\n"
        "Comando:\n"
        "/flash\n\n"
        "Úsalo para eventos críticos:\n"
        "⚠️ Desprendimientos\n"
        "💧 Inundación\n"
        "⛏️ Sobreexcavación\n"
        "⏱️ Tiempo Frente Abierta\n"
        "🧱 Falta Alzaprima\n"
        "🚧 No aplicación de 5cm\n"
        "➕ Otro\n\n"
        "✅ El bot guarda el informe en `Flash_Reportes.xlsx`, sube respaldo a OneDrive y envía el aviso al grupo configurado.\n\n"

        "🧪 *COMANDOS ÚTILES*\n"
        "/status → estado del bot, hora Chile y grupo Flash\n"
        "/testgrupo → envía mensaje de prueba al grupo configurado\n"
        "/idchat → obtiene ID del chat o grupo\n"
        "/cancel → cancela el flujo actual\n"
        "/reset → limpia cualquier flujo pendiente\n"
        "/help → muestra esta guía\n\n"

        "🔐 *ONEDRIVE*\n"
        "/onedrive_login → iniciar autorización\n"
        "/onedrive_finish → finalizar autorización\n\n"

        "🛰️ *Recomendación operacional:*\n"
        "Para fotos normales, solo envía la imagen. Para eventos críticos, usa siempre /flash."
    )

    await update.message.reply_text(texto, parse_mode="Markdown")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = now_chile()

    texto = (
        "🛰️ *ESTADO DEL SISTEMA*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"✅ Bot activo\n"
        f"🕒 Hora Chile: `{now.strftime('%Y-%m-%d %H:%M:%S')}`\n"
        f"📁 OneDrive Root: `{ONEDRIVE_ROOT}`\n"
        f"🚨 Grupo Flash ID: `{FLASH_GROUP_CHAT_ID or 'No configurado'}`\n"
        f"📸 Archivo fotos: `Registro_Fotos.xlsx`\n"
        f"🚨 Archivo flash: `Flash_Reportes.xlsx`\n"
    )

    await update.message.reply_text(texto, parse_mode="Markdown")


async def testgrupo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not FLASH_GROUP_CHAT_ID:
        await update.message.reply_text("⚠️ FLASH_GROUP_CHAT_ID no está configurado.")
        return

    now = now_chile().strftime("%Y-%m-%d %H:%M:%S")

    try:
        await context.bot.send_message(
            chat_id=int(FLASH_GROUP_CHAT_ID),
            text=(
                "🧪 PRUEBA DE CONEXIÓN\n\n"
                f"✅ Bot conectado correctamente.\n"
                f"🕒 Hora Chile: {now}\n"
                "🚇 Sistema Reportes Flash EL6 operativo."
            ),
        )
        await update.message.reply_text("✅ Mensaje de prueba enviado al grupo configurado.")
    except Exception as e:
        await update.message.reply_text(f"❌ No se pudo enviar al grupo:\n{e}")


async def idchat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat_title = update.effective_chat.title or "Chat privado"

    await update.message.reply_text(
        f"🆔 Chat ID:\n{chat_id}\n\n"
        f"📌 Chat:\n{chat_title}"
    )


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("🔄 Flujo limpiado correctamente. Puedes iniciar de nuevo.")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("🛑 Proceso cancelado correctamente.")
    return ConversationHandler.END


# ============================================================
# ONEDRIVE COMMANDS
# ============================================================

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
        await update.message.reply_text("❌ Error iniciando login de OneDrive.")
        return

    PENDING_ONEDRIVE_FLOWS[str(update.effective_chat.id)] = (app, flow, cache)

    await update.message.reply_text(
        "🔐 *AUTORIZACIÓN ONEDRIVE*\n\n"
        "1️⃣ Abre el enlace indicado por Microsoft.\n"
        "2️⃣ Ingresa el código.\n"
        "3️⃣ Inicia sesión.\n"
        "4️⃣ Acepta permisos.\n"
        "5️⃣ Vuelve aquí y ejecuta:\n\n"
        "/onedrive_finish\n\n"
        f"{flow['message']}",
        parse_mode="Markdown",
    )


async def onedrive_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    chat_id = str(update.effective_chat.id)

    if chat_id not in PENDING_ONEDRIVE_FLOWS:
        await update.message.reply_text("⚠️ Primero ejecuta /onedrive_login.")
        return

    app, flow, cache = PENDING_ONEDRIVE_FLOWS[chat_id]

    await update.message.reply_text("⏳ Finalizando autorización OneDrive...")

    try:
        result = await asyncio.to_thread(app.acquire_token_by_device_flow, flow)
    except Exception as e:
        await update.message.reply_text(f"❌ Error al finalizar autorización:\n{e}")
        return

    if "access_token" in result:
        save_cache(cache)
        PENDING_ONEDRIVE_FLOWS.pop(chat_id, None)
        await update.message.reply_text("✅ OneDrive autorizado correctamente.")
    else:
        detalle = result.get("error_description", "Sin detalle.")
        await update.message.reply_text(f"❌ Error en autorización:\n{detalle}")


# ============================================================
# FLUJO FOTOS
# ============================================================

async def photo_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.photo:
        return ConversationHandler.END

    context.user_data.clear()

    file = await update.message.photo[-1].get_file()
    now = now_chile()
    user = update.message.from_user

    context.user_data["photo"] = {
        "file": file,
        "id": now.strftime("%Y%m%d%H%M%S"),
        "fecha": now.strftime("%Y-%m-%d"),
        "hora": now.strftime("%H:%M:%S"),
        "fecha_hora": now.strftime("%Y-%m-%d %H:%M:%S"),
        "fecha_archivo": now.strftime("%Y%m%d"),
        "hora_archivo": now.strftime("%H%M%S"),
        "usuario": get_user_name(user),
        "usuario_label": get_user_label(user),
        "usuario_id": str(user.id) if user else "",
        "pique": "",
        "frente": "",
        "marco": "",
        "etapa": "",
        "comentario": "",
        "archivo": "",
        "ruta_onedrive": "",
        "estado": "Registrado",
    }

    await update.message.reply_text(
        "📸 *FOTO RECIBIDA*\n\n"
        "Paso 1 de 4\n"
        "Selecciona el *PIQUE*:",
        reply_markup=build_keyboard(PHOTO_PIQUES, "photo_pique", cols=2),
        parse_mode="Markdown",
    )

    return PHOTO_PIQUE


async def photo_pique(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    data = context.user_data.get("photo")
    if not data:
        await q.edit_message_text("⚠️ No encuentro foto pendiente. Envía la foto nuevamente.")
        return ConversationHandler.END

    _, pique = q.data.split("|", 1)
    data["pique"] = pique

    frentes = frentes_por_pique(pique)

    await q.edit_message_text(
        f"🏗️ Pique seleccionado: {pique}\n\n"
        "Paso 2 de 4\n"
        "Selecciona el *FRENTE*:",
        reply_markup=build_keyboard(frentes, "photo_frente", cols=2),
        parse_mode="Markdown",
    )

    return PHOTO_FRENTE


async def photo_frente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    data = context.user_data.get("photo")
    if not data:
        await q.edit_message_text("⚠️ No encuentro foto pendiente. Envía la foto nuevamente.")
        return ConversationHandler.END

    _, frente = q.data.split("|", 1)
    data["frente"] = frente

    if requiere_marco(frente):
        await q.edit_message_text(
            f"📍 Frente seleccionado: {frente}\n\n"
            "Paso 3 de 4\n"
            "Ingresa el *N° de Marco*.\n\n"
            "Ejemplo:\n"
            "`347`",
            parse_mode="Markdown",
        )
        return PHOTO_MARCO

    if requiere_etapa(frente):
        await q.edit_message_text(
            f"📍 Frente seleccionado: {frente}\n\n"
            "Paso 3 de 4\n"
            "Ingresa la *Etapa*.\n\n"
            "Ejemplo:\n"
            "`Etapa 3`",
            parse_mode="Markdown",
        )
        return PHOTO_ETAPA

    return await pedir_comentario(q, context)


async def photo_marco(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data.get("photo")
    if not data:
        await update.message.reply_text("⚠️ No encuentro foto pendiente. Envía la foto nuevamente.")
        return ConversationHandler.END

    marco = (update.message.text or "").strip()
    if not marco:
        await update.message.reply_text("⚠️ Ingresa un N° de Marco válido.")
        return PHOTO_MARCO

    data["marco"] = marco
    return await pedir_comentario(update, context)


async def photo_etapa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data.get("photo")
    if not data:
        await update.message.reply_text("⚠️ No encuentro foto pendiente. Envía la foto nuevamente.")
        return ConversationHandler.END

    etapa = (update.message.text or "").strip()
    if not etapa:
        await update.message.reply_text("⚠️ Ingresa una etapa válida.")
        return PHOTO_ETAPA

    data["etapa"] = etapa
    return await pedir_comentario(update, context)


async def pedir_comentario(update_or_query, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Sin comentario", callback_data="photo_sin_comentario")]
    ])

    texto = (
        "📝 Paso 4 de 4\n\n"
        "Agrega un comentario opcional.\n\n"
        "Ejemplo:\n"
        "`Instalación de marco finalizada sin observaciones.`\n\n"
        "Si no deseas agregar comentario, presiona:"
    )

    if hasattr(update_or_query, "edit_message_text"):
        await update_or_query.edit_message_text(texto, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await update_or_query.message.reply_text(texto, reply_markup=keyboard, parse_mode="Markdown")

    return PHOTO_COMENTARIO


async def photo_comentario(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data.get("photo")
    if not data:
        await update.message.reply_text("⚠️ No encuentro foto pendiente. Envía la foto nuevamente.")
        return ConversationHandler.END

    data["comentario"] = (update.message.text or "").strip()
    return await finalizar_photo(update, context)


async def photo_sin_comentario(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    data = context.user_data.get("photo")
    if not data:
        await q.edit_message_text("⚠️ No encuentro foto pendiente. Envía la foto nuevamente.")
        return ConversationHandler.END

    data["comentario"] = "Sin comentario"
    await q.edit_message_text("📸 Guardando foto y metadata...")
    return await finalizar_photo(update, context)


async def finalizar_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data.get("photo")
    if not data:
        return ConversationHandler.END

    nombre = make_photo_filename(data)
    data["archivo"] = nombre

    local_folder = os.path.join(PHOTO_SAVE_ROOT, clean_filename(data["pique"]), clean_filename(data["frente"]))
    os.makedirs(local_folder, exist_ok=True)

    local_path = os.path.join(local_folder, nombre)

    await data["file"].download_to_drive(local_path)
    ensure_saved(local_path)

    onedrive_msg = ""
    remote_folder = make_onedrive_photo_folder(data)

    try:
        remote_path = upload_to_onedrive(local_path, remote_folder, nombre)
        data["ruta_onedrive"] = remote_path
        onedrive_msg = "☁️ Foto subida a OneDrive."
    except Exception as e:
        data["ruta_onedrive"] = ""
        onedrive_msg = f"⚠️ Foto guardada localmente, pero no se pudo subir a OneDrive:\n{e}"

    try:
        guardar_photo_metadata(data)
        try:
            upload_to_onedrive(PHOTO_XLSX, "Registros", "Registro_Fotos.xlsx")
            onedrive_msg += "\n☁️ Registro_Fotos.xlsx actualizado en OneDrive."
        except Exception as e:
            onedrive_msg += f"\n⚠️ Metadata guardada localmente, pero no se pudo subir a OneDrive:\n{e}"
    except Exception as e:
        onedrive_msg += f"\n❌ Error guardando metadata:\n{e}"

    respuesta = (
        "✅ *FOTO REGISTRADA CORRECTAMENTE*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🆔 ID: `{data['id']}`\n"
        f"📅 Fecha: `{data['fecha_hora']}`\n"
        f"👷 Usuario: {data['usuario_label']}\n"
        f"🏗️ Pique: {data['pique']}\n"
        f"📍 Frente: {data['frente']}\n"
    )

    if data.get("marco"):
        respuesta += f"🔢 Marco: {data['marco']}\n"

    if data.get("etapa"):
        respuesta += f"🏷️ Etapa: {data['etapa']}\n"

    respuesta += (
        f"📝 Comentario: {data['comentario']}\n\n"
        f"📄 Archivo:\n`{nombre}`\n\n"
        f"{onedrive_msg}"
    )

    context.user_data.clear()

    if update.message:
        await update.message.reply_text(respuesta, parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.message.reply_text(respuesta, parse_mode="Markdown")

    return ConversationHandler.END


# ============================================================
# FLUJO FLASH
# ============================================================

async def flash_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return ConversationHandler.END

    context.user_data.clear()
    user = update.message.from_user
    now = now_chile()

    context.user_data["flash"] = {
        "id": now.strftime("%Y%m%d%H%M%S"),
        "fecha": now.strftime("%Y-%m-%d"),
        "hora": now.strftime("%H:%M:%S"),
        "fecha_hora": now.strftime("%Y-%m-%d %H:%M:%S"),
        "inspector": get_user_label(user),
        "usuario_id": str(user.id) if user else "",
        "pique": "",
        "frente": "",
        "evento": "",
        "detalle": "",
        "foto": "",
        "foto_path": "",
        "estado": "Emitido",
    }

    await update.message.reply_text(
        "🚨 *INFORME FLASH EL6*\n\n"
        "Paso 1 de 5\n"
        "Selecciona el *PIQUE*:",
        reply_markup=build_keyboard(FLASH_PIQUES, "flash_pique", cols=2),
        parse_mode="Markdown",
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
        f"🏗️ Pique seleccionado: {pique}\n\n"
        "Paso 2 de 5\n"
        "Selecciona el FRENTE:",
        reply_markup=build_keyboard(FLASH_FRENTES, "flash_frente", cols=2),
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
        f"📍 Frente seleccionado: {frente}\n\n"
        "Paso 3 de 5\n"
        "Selecciona el EVENTO:",
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
        "Paso 4 de 5\n"
        "Escribe el detalle del informe.\n\n"
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

    detalle = (update.message.text or "").strip()

    if not detalle:
        await update.message.reply_text("⚠️ Debes escribir un detalle para el informe Flash.")
        return FLASH_DETALLE

    context.user_data["flash"]["detalle"] = detalle

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Sin foto", callback_data="flash_sin_foto")]
    ])

    await update.message.reply_text(
        "📸 Paso 5 de 5\n\n"
        "Envía una foto como respaldo.\n\n"
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


def mensaje_flash(flash: dict) -> str:
    return (
        "🚨 INFORME FLASH EL6\n\n"
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
        return ConversationHandler.END

    guardar_flash_xlsx(flash)

    onedrive_msg = ""

    try:
        upload_to_onedrive(FLASH_XLSX, "Flash_Reportes", "Flash_Reportes.xlsx")
        onedrive_msg += "\n☁️ Excel Flash actualizado en OneDrive."
    except Exception as e:
        onedrive_msg += f"\n⚠️ Excel guardado localmente, pero no se pudo subir a OneDrive:\n{e}"

    if flash.get("foto_path"):
        try:
            upload_to_onedrive(flash["foto_path"], "Flash_Reportes/Fotos", flash["foto"])
            onedrive_msg += "\n☁️ Foto Flash subida a OneDrive."
        except Exception as e:
            onedrive_msg += f"\n⚠️ Foto guardada localmente, pero no se pudo subir a OneDrive:\n{e}"

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


# ============================================================
# MAIN
# ============================================================

def main():
    start_health()

    app = Application.builder().token(BOT_TOKEN).build()

    photo_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.PHOTO, photo_inicio)],
        states={
            PHOTO_PIQUE: [CallbackQueryHandler(photo_pique, pattern=r"^photo_pique\|")],
            PHOTO_FRENTE: [CallbackQueryHandler(photo_frente, pattern=r"^photo_frente\|")],
            PHOTO_MARCO: [MessageHandler(filters.TEXT & ~filters.COMMAND, photo_marco)],
            PHOTO_ETAPA: [MessageHandler(filters.TEXT & ~filters.COMMAND, photo_etapa)],
            PHOTO_COMENTARIO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, photo_comentario),
                CallbackQueryHandler(photo_sin_comentario, pattern=r"^photo_sin_comentario$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("reset", reset_cmd)],
        allow_reentry=True,
    )

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
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("reset", reset_cmd)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("testgrupo", testgrupo_cmd))
    app.add_handler(CommandHandler("idchat", idchat))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("onedrive_login", onedrive_login))
    app.add_handler(CommandHandler("onedrive_finish", onedrive_finish))

    app.add_handler(flash_conv)
    app.add_handler(photo_conv)

    app.run_polling()


if __name__ == "__main__":
    main()
