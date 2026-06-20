"""
Dona v2 — Bot Telegram con Supabase + Microsoft Graph (calendario)
Arquitectura WhatsApp-ready: lógica separada del transporte, historial en DB.

Variables de entorno requeridas (Railway):
  TELEGRAM_TOKEN, ANTHROPIC_API_KEY, NOTION_TOKEN, NOTION_DATABASE_ID
  SUPABASE_URL, SUPABASE_ANON_KEY, ALLOWED_USER_ID

Variables opcionales (habilitan calendario — agregar en Fase B):
  MS_CLIENT_ID, MS_CLIENT_SECRET, MS_REFRESH_TOKEN
"""

import os
import json
import logging
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import anthropic

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN     = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
NOTION_TOKEN       = os.environ["NOTION_TOKEN"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
SUPABASE_URL       = os.environ["SUPABASE_URL"]
SUPABASE_KEY       = os.environ["SUPABASE_ANON_KEY"]
ADMIN_TELEGRAM_ID  = int(os.environ.get("ALLOWED_USER_ID", "0"))

# Microsoft Graph — opcional, se activa solo si están las 3 vars
MS_CLIENT_ID     = os.environ.get("MS_CLIENT_ID", "")
MS_CLIENT_SECRET = os.environ.get("MS_CLIENT_SECRET", "")
MS_REFRESH_TOKEN = os.environ.get("MS_REFRESH_TOKEN", "")
CALENDAR_ENABLED = bool(MS_CLIENT_ID and MS_CLIENT_SECRET and MS_REFRESH_TOKEN)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ── System Prompts ────────────────────────────────────────────────────────────

SYSTEM_ADMIN = """Eres Dona, la asistente personal de Julián Marín, Director de Venta Directa en CWP Panamá.

CONTEXTO PERSONAL:
- Tiene TDAH, toma medicamento a las 7am. Pico de foco en la mañana, baja a las 4-5pm.
- Llega a casa 6:30pm, duerme 10-10:30pm. Zona horaria: GMT-5 (Panamá)

AGENDA SEMANAL (bloques inamovibles):
Todos los días: 07:00 despertar | 12:00–14:00 almuerzo+ejercicio | 18:30 llegada | 22:00 dormir
LUNES: 08:00 Comité semanal | 09:30 Pre ExCo VP | 10:30–12:00 LIBRE | 14:00 1-1 Katiuska | 15:00 1-1 Marcos | 16:00 1-1 Richard | 17:00–18:00 VP protegido
MARTES: 08:00–12:00 Deep work | 14:00 1-1 Luis V. | 15:00 1-1 Mayli | 16:00–18:00 Dinamo/Cellca admin
MIÉRCOLES: 08:00–12:00 Deep work | 14:00–16:00 Admin | 16:30 Dinamo campo | 17:00–18:00 VP protegido | 19:30 Inglés
JUEVES: 08:00–10:00 Deep work | 10:30–12:00 Deep work (mejor bloque, protegido) | 14:00 Sup.Katiuska | 15:00 Sup.Marcos | 16:00 Sup.Richard | 17:00 V&G campo
VIERNES: 08:00–10:00 Salesland campo | 10:00–12:00 LIBRE | 14:00 Landing Revenue | 16:00 Review semanal | 17:00–18:00 LIBRE
DISPONIBLE PARA REUNIONES: Lun 10:30–12:00 | Mar 08:00–12:00 | Mié 14:00–16:00 | Jue 08:00–10:00 | Vie 10:00–12:00 o 17:00–18:00

PENDIENTES: Usa add_task / list_tasks para manejar Notion.
DATOS FDV: Usa query_* para responder sobre métricas del equipo. Nunca inventes cifras.
CALENDARIO: Usa block_agenda_slot SOLO cuando Julián confirme explícitamente bloquear un espacio.

ROL: Mentor riguroso, NO complaciente. Directo, conciso, para móvil. Siempre español."""

SYSTEM_GERENTE = """Eres Dona, asistente de gestión para el equipo de Venta Directa de CWP Panamá.

El usuario es un GERENTE DE TERRITORIO. Tus reglas:
- Responde SOLO sobre temas laborales: desempeño, métricas FDV, su equipo, coaching.
- NUNCA respondas sobre finanzas personales, agenda personal, ni temas privados de Julián.
- Para consultas de datos, usa las herramientas query_*. Nunca inventes cifras.
- Puedes dar recomendaciones de gestión basadas en los datos (quartiles, ausentismo, RGU).
- Si el gerente pide una reunión con Julián, usa notify_julian para avisarle.
- El gerente solo puede ver datos de SU equipo (sus supervisores y vendedores).
  Para ranking nacional, muestra su posición pero no datos privados de otros gerentes.

Directo, conciso, profesional. Siempre en español."""

# ── Supabase helpers ──────────────────────────────────────────────────────────

SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}


def sb_get(table: str, params: dict = None) -> list:
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}", headers=SB_HEADERS, params=params, timeout=10)
        return r.json() if r.status_code == 200 else []
    except Exception as e:
        logger.error(f"Supabase GET {table}: {e}")
        return []


def sb_post(table: str, data, upsert: bool = False) -> list | None:
    h = {**SB_HEADERS, "Prefer": "resolution=merge-duplicates,return=representation"}
    try:
        r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}", headers=h, json=data, timeout=10)
        return r.json() if r.status_code in (200, 201) else None
    except Exception as e:
        logger.error(f"Supabase POST {table}: {e}")
        return None


def sb_patch(table: str, params: dict, data: dict) -> bool:
    h = {**SB_HEADERS, "Prefer": "return=minimal"}
    try:
        r = requests.patch(f"{SUPABASE_URL}/rest/v1/{table}", headers=h, params=params, json=data, timeout=10)
        return r.status_code in (200, 204)
    except Exception as e:
        logger.error(f"Supabase PATCH {table}: {e}")
        return False


# ── User & history ────────────────────────────────────────────────────────────

def get_or_create_user(platform_id: str, platform: str = "telegram") -> dict:
    rows = sb_get("usuarios_dona", {"platform": f"eq.{platform}", "platform_id": f"eq.{platform_id}"})
    if rows:
        return rows[0]
    rol = "admin" if platform == "telegram" and str(platform_id) == str(ADMIN_TELEGRAM_ID) else "viewer"
    result = sb_post("usuarios_dona", {"platform": platform, "platform_id": str(platform_id), "rol": rol})
    return result[0] if isinstance(result, list) and result else {"platform_id": platform_id, "rol": rol, "id": None}


def get_history(usuario_id: int) -> list:
    if not usuario_id:
        return []
    rows = sb_get("conversaciones", {"usuario_id": f"eq.{usuario_id}"})
    if rows:
        msgs = rows[0].get("messages", [])
        return msgs if isinstance(msgs, list) else []
    return []


def save_history(usuario_id: int, messages: list):
    if not usuario_id:
        logger.warning("save_history: usuario_id es None, no se guarda historial")
        return
    messages = messages[-20:]
    # Upsert directo — más confiable que PATCH+POST
    h = {**SB_HEADERS, "Prefer": "resolution=merge-duplicates,return=minimal"}
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/conversaciones",
            headers=h,
            json={"usuario_id": usuario_id, "messages": messages, "actualizado": "now()"},
            timeout=10,
        )
        if r.status_code not in (200, 201):
            logger.error(f"save_history error: {r.status_code} {r.text[:200]}")
    except Exception as e:
        logger.error(f"save_history exception: {e}")


def clear_history(usuario_id: int):
    if not usuario_id:
        return
    h = {**SB_HEADERS, "Prefer": "resolution=merge-duplicates,return=minimal"}
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/conversaciones",
            headers=h,
            json={"usuario_id": usuario_id, "messages": [], "actualizado": "now()"},
            timeout=10,
        )
    except Exception as e:
        logger.error(f"clear_history exception: {e}")


# ── Microsoft Graph helpers ───────────────────────────────────────────────────

def get_ms_token() -> str:
    if not CALENDAR_ENABLED:
        return ""
    try:
        r = requests.post(
            "https://login.microsoftonline.com/common/oauth2/v2.0/token",
            data={
                "grant_type":    "refresh_token",
                "client_id":     MS_CLIENT_ID,
                "client_secret": MS_CLIENT_SECRET,
                "refresh_token": MS_REFRESH_TOKEN,
                "scope":         "Calendars.ReadWrite Mail.Read offline_access",
            },
            timeout=15,
        )
        return r.json().get("access_token", "")
    except Exception as e:
        logger.error(f"MS token error: {e}")
        return ""


def block_calendar_slot(subject: str, start_dt: str, end_dt: str, attendee_email: str = None) -> str:
    """Crea evento en el calendario de Julián via Microsoft Graph."""
    if not CALENDAR_ENABLED:
        return "⚠️ Calendario no configurado aún. Agrega MS_CLIENT_ID, MS_CLIENT_SECRET y MS_REFRESH_TOKEN en Railway."

    token = get_ms_token()
    if not token:
        return "❌ No pude autenticar con Microsoft. Verifica las variables MS_* en Railway."

    body = {
        "subject": subject,
        "start": {"dateTime": start_dt, "timeZone": "America/Panama"},
        "end":   {"dateTime": end_dt,   "timeZone": "America/Panama"},
    }
    if attendee_email:
        body["attendees"] = [{"emailAddress": {"address": attendee_email}, "type": "required"}]

    r = requests.post(
        "https://graph.microsoft.com/v1.0/me/events",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=body,
        timeout=15,
    )

    if r.status_code == 201:
        fecha = start_dt[:10]
        hora_i = start_dt[11:16]
        hora_f = end_dt[11:16]
        return f"✅ Bloqueado en calendario: '{subject}' — {fecha} {hora_i}–{hora_f}"
    else:
        logger.error(f"Graph calendar error: {r.status_code} {r.text[:300]}")
        return f"❌ Error al crear evento: {r.status_code}"


def notify_julian(mensaje: str) -> str:
    """Envía mensaje proactivo a Julián via Telegram Bot API."""
    if not ADMIN_TELEGRAM_ID:
        return "No hay admin configurado."
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": ADMIN_TELEGRAM_ID, "text": f"📬 *Notificación de Dona:*\n\n{mensaje}", "parse_mode": "Markdown"},
            timeout=10,
        )
        return "✅ Notificación enviada a Julián." if r.status_code == 200 else "❌ No pude notificar."
    except Exception as e:
        return f"❌ Error: {e}"


# ── FDV tools ─────────────────────────────────────────────────────────────────

def fdv_canal() -> str:
    rows = sb_get("canal_metrics", {"order": "cohort.desc", "limit": "1"})
    if not rows:
        return "No hay datos del canal."
    r = rows[0]
    return f"Canal FDV — Cohort {r['cohort']} ({r['fecha']})\nRGU promedio: {r['rgu_prom']} | Ausentismo: {r['aus_pct']}% | Gerentes: {r['n_gerentes']}"


def fdv_gerentes(solo_gerente: str = None) -> str:
    rows = sb_get("gerentes", {"order": "ranking"})
    if not rows:
        return "No hay datos de gerentes."
    if solo_gerente:
        # Para gerentes: muestra su posición y el promedio del canal, no datos de otros
        mi_fila = next((r for r in rows if solo_gerente.lower() in r["nombre"].lower()), None)
        if not mi_fila:
            return "No encontré tu registro."
        return (
            f"Tu posición: #{mi_fila['ranking']} de {len(rows)}\n"
            f"RGU C9: {mi_fila['rgu_c9']} | Aus: {mi_fila['aus_pct']}% | SM: {mi_fila['sin_motivo']} | {mi_fila['accion']}"
        )
    # Admin: tabla completa
    lines = ["Ranking Gerentes:"]
    for r in rows:
        lines.append(f"{r['ranking']}. {r['nombre']} | RGU C9: {r['rgu_c9']} | Aus: {r['aus_pct']}% | SM: {r['sin_motivo']} | {r['accion']}")
    return "\n".join(lines)


def fdv_supervisores(gerente: str = None) -> str:
    params = {"order": "ranking_canal"}
    if gerente:
        params["gerente"] = f"ilike.*{gerente}*"
    rows = sb_get("supervisores", params)
    if not rows:
        return "No hay datos de supervisores."
    header = f"Supervisores de {gerente}:" if gerente else "Supervisores (canal):"
    lines = [header]
    for r in rows:
        q = f"{r.get('quartil_c7','?')}→{r.get('quartil_c8','?')}→{r.get('quartil_c9','?')}"
        lines.append(f"#{r['ranking_canal']} {r['nombre']} | RGU:{r['rgu_c9']} | Q:{q} | Aus:{r['aus_pct']}% | {r['accion']}")
    return "\n".join(lines)


def fdv_vendedores(supervisor: str = None, gerente: str = None) -> str:
    params = {"order": "ranking_canal"}
    if supervisor:
        params["supervisor"] = f"ilike.*{supervisor}*"
    elif gerente:
        params["gerente"] = f"ilike.*{gerente}*"
    rows = sb_get("vendedores", params)
    if not rows:
        return "No hay datos de vendedores."
    label = supervisor or gerente or "canal completo"
    lines = [f"Vendedores ({label}):"]
    for r in rows:
        q = f"{r.get('quartil_c7','?')}→{r.get('quartil_c8','?')}→{r.get('quartil_c9','?')}"
        lines.append(f"#{r['ranking_canal']} {r['nombre']} | RGU:{r['rgu_actual']} | Q:{q} | {r['supervisor']}")
    return "\n".join(lines)


# ── Notion helpers ────────────────────────────────────────────────────────────

def notion_request(method: str, path: str, data: dict = None) -> dict:
    url = f"https://api.notion.com/v1{path}"
    headers = {"Authorization": f"Bearer {NOTION_TOKEN}", "Content-Type": "application/json", "Notion-Version": "2022-06-28"}
    try:
        r = requests.request(method, url, headers=headers, json=data, timeout=10)
        return r.json()
    except Exception as e:
        logger.error(f"Notion: {e}")
        return {}


def add_notion_task(task: str) -> bool:
    data = {"parent": {"database_id": NOTION_DATABASE_ID}, "properties": {"Nombre": {"title": [{"text": {"content": task}}]}}}
    return bool(notion_request("POST", "/pages", data).get("id"))


def get_notion_tasks() -> list[str]:
    result = notion_request("POST", f"/databases/{NOTION_DATABASE_ID}/query", {})
    tasks = []
    for page in result.get("results", []):
        t = page.get("properties", {}).get("Nombre", {}).get("title", [])
        if t:
            tasks.append(t[0]["text"]["content"])
    return tasks


# ── Tool definitions por rol ──────────────────────────────────────────────────

_TOOL_QUERY_CANAL = {
    "name": "query_canal",
    "description": "Estadísticas generales del canal FDV (RGU promedio, ausentismo, cohort).",
    "input_schema": {"type": "object", "properties": {}},
}
_TOOL_QUERY_GERENTES = {
    "name": "query_gerentes",
    "description": "Ranking de gerentes con métricas. Para gerentes, pasa su propio nombre en 'solo_gerente' para ver solo su posición.",
    "input_schema": {"type": "object", "properties": {
        "solo_gerente": {"type": "string", "description": "Nombre del gerente para ver solo su posición (opcional)."},
    }},
}
_TOOL_QUERY_SUPERVISORES = {
    "name": "query_supervisores",
    "description": "Supervisores con métricas. Filtra por gerente si se especifica.",
    "input_schema": {"type": "object", "properties": {
        "gerente": {"type": "string", "description": "Nombre parcial del gerente para filtrar (opcional)."},
    }},
}
_TOOL_QUERY_VENDEDORES = {
    "name": "query_vendedores",
    "description": "Vendedores con métricas. Filtra por supervisor o gerente.",
    "input_schema": {"type": "object", "properties": {
        "supervisor": {"type": "string", "description": "Nombre parcial del supervisor (opcional)."},
        "gerente":    {"type": "string", "description": "Nombre parcial del gerente (opcional)."},
    }},
}
_TOOL_NOTIFY_JULIAN = {
    "name": "notify_julian",
    "description": "Envía una notificación a Julián por Telegram. Usar cuando el gerente solicita algo que requiere su aprobación (ej: reunión).",
    "input_schema": {"type": "object", "properties": {
        "mensaje": {"type": "string", "description": "Mensaje para Julián (incluye nombre del gerente, qué pide y posible horario)."},
    }, "required": ["mensaje"]},
}
_TOOL_ADD_TASK = {
    "name": "add_task",
    "description": "Guarda una tarea/pendiente en Notion de Julián.",
    "input_schema": {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]},
}
_TOOL_LIST_TASKS = {
    "name": "list_tasks",
    "description": "Lista tareas pendientes de Notion de Julián.",
    "input_schema": {"type": "object", "properties": {}},
}
_TOOL_BLOCK_AGENDA = {
    "name": "block_agenda_slot",
    "description": "Bloquea un espacio en el calendario de Julián. SOLO usar cuando Julián confirme explícitamente.",
    "input_schema": {"type": "object", "properties": {
        "subject":        {"type": "string", "description": "Título del evento."},
        "start_dt":       {"type": "string", "description": "Fecha y hora inicio ISO: 2026-06-23T10:00:00"},
        "end_dt":         {"type": "string", "description": "Fecha y hora fin ISO: 2026-06-23T11:00:00"},
        "attendee_email": {"type": "string", "description": "Email del asistente (opcional)."},
    }, "required": ["subject", "start_dt", "end_dt"]},
}

# Herramientas por rol
TOOLS_ADMIN   = [_TOOL_ADD_TASK, _TOOL_LIST_TASKS, _TOOL_QUERY_CANAL, _TOOL_QUERY_GERENTES, _TOOL_QUERY_SUPERVISORES, _TOOL_QUERY_VENDEDORES, _TOOL_BLOCK_AGENDA]
TOOLS_GERENTE = [_TOOL_QUERY_CANAL, _TOOL_QUERY_GERENTES, _TOOL_QUERY_SUPERVISORES, _TOOL_QUERY_VENDEDORES, _TOOL_NOTIFY_JULIAN]


def get_tools_for_role(rol: str) -> list:
    return TOOLS_ADMIN if rol == "admin" else TOOLS_GERENTE


def handle_tool_call(name: str, inp: dict, usuario: dict) -> str:
    rol          = usuario.get("rol", "viewer")
    gerente_ref  = usuario.get("gerente_ref")

    if name == "add_task":
        if rol != "admin":
            return "No tienes acceso a esta función."
        task = inp.get("task", "")
        return f"✅ Guardado: '{task}'" if add_notion_task(task) else "❌ Error en Notion."

    if name == "list_tasks":
        if rol != "admin":
            return "No tienes acceso a esta función."
        tasks = get_notion_tasks()
        return "No hay pendientes." if not tasks else "📋 Pendientes:\n" + "\n".join(f"• {t}" for t in tasks)

    if name == "query_canal":
        return fdv_canal()

    if name == "query_gerentes":
        # Gerentes solo ven su posición, admin ve todo
        solo = inp.get("solo_gerente") or (gerente_ref if rol == "gerente" else None)
        return fdv_gerentes(solo_gerente=solo)

    if name == "query_supervisores":
        filtro = inp.get("gerente")
        # Gerentes solo pueden ver su propio equipo
        if rol == "gerente" and gerente_ref:
            filtro = gerente_ref
        return fdv_supervisores(gerente=filtro)

    if name == "query_vendedores":
        sup     = inp.get("supervisor")
        gerente = inp.get("gerente")
        # Gerentes solo ven su equipo
        if rol == "gerente" and gerente_ref:
            gerente = gerente_ref
            sup = None
        return fdv_vendedores(supervisor=sup, gerente=gerente)

    if name == "block_agenda_slot":
        if rol != "admin":
            return "Solo Julián puede bloquear su calendario."
        return block_calendar_slot(inp["subject"], inp["start_dt"], inp["end_dt"], inp.get("attendee_email"))

    if name == "notify_julian":
        return notify_julian(inp.get("mensaje", ""))

    return "Herramienta no reconocida."


# ── Core: process_message (agnóstico de plataforma) ───────────────────────────

def process_message(user_text: str, usuario: dict) -> str:
    usuario_id = usuario.get("id")
    if not usuario_id:
        return "Error: usuario no registrado en Supabase."

    rol     = usuario.get("rol", "viewer")
    history = get_history(usuario_id)
    history.append({"role": "user", "content": user_text})

    system = SYSTEM_ADMIN if rol == "admin" else SYSTEM_GERENTE
    if rol == "gerente" and usuario.get("gerente_ref"):
        system += f"\n\nNOMBRE DEL GERENTE: {usuario['gerente_ref']}"

    tools = get_tools_for_role(rol)

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=system,
            tools=tools,
            messages=history,
        )

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = handle_tool_call(block.name, block.input, usuario)
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

            history.append({"role": "assistant", "content": response.content})
            history.append({"role": "user", "content": tool_results})

            final = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                system=system,
                tools=tools,
                messages=history,
            )
            reply = final.content[0].text
            history.append({"role": "assistant", "content": reply})
        else:
            reply = response.content[0].text
            history.append({"role": "assistant", "content": reply})

        save_history(usuario_id, history)
        return reply

    except Exception as e:
        logger.error(f"Error Claude: {e}")
        return "Hubo un error. Intenta de nuevo."


# ── Telegram handlers ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = get_or_create_user(str(update.effective_user.id))
    if usuario.get("rol") == "viewer":
        await update.message.reply_text("No tienes acceso a Dona. Contacta a Julián.")
        return
    if usuario.get("id"):
        clear_history(usuario["id"])
    nombre = update.effective_user.first_name or "hola"
    await update.message.reply_text(
        f"Hola {nombre} 👋 Soy Dona. ¿En qué te ayudo?\n\n/clear — limpiar historial\n/pendientes — ver tareas"
    )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = get_or_create_user(str(update.effective_user.id))
    if usuario.get("id"):
        clear_history(usuario["id"])
    await update.message.reply_text("Historial limpiado.")


async def cmd_pendientes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = get_or_create_user(str(update.effective_user.id))
    if usuario.get("rol") != "admin":
        await update.message.reply_text("Solo Julián puede ver los pendientes.")
        return
    tasks = get_notion_tasks()
    text = "No hay pendientes." if not tasks else "📋 *Pendientes:*\n" + "\n".join(f"• {t}" for t in tasks)
    await update.message.reply_text(text, parse_mode="Markdown")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    platform_id = str(update.effective_user.id)
    usuario = get_or_create_user(platform_id)

    if usuario.get("rol") == "viewer":
        await update.message.reply_text("No tienes acceso a Dona.")
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    reply = process_message(update.message.text, usuario)
    await update.message.reply_text(reply)


def main():
    cal_status = "✅ Calendario activo" if CALENDAR_ENABLED else "⚠️  Calendario desactivado (faltan vars MS_*)"
    logger.info(f"Dona v2 iniciando... {cal_status}")
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("pendientes", cmd_pendientes))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
