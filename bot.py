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
import html as _html
import logging
import requests
from datetime import datetime, timedelta, timezone, time as dtime
from zoneinfo import ZoneInfo
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes
)
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

# Triaje de correo — reutiliza el mismo token de Graph (Mail.Read ya incluido en scope)
TRIAJE_MODEL = os.environ.get("TRIAJE_MODEL", "claude-haiku-4-5-20251001")
TZ_PANAMA    = ZoneInfo("America/Panama")

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ── System Prompts ────────────────────────────────────────────────────────────

SYSTEM_ADMIN = """Eres Dona, la asistente personal de Julián Marín, Director de Venta Directa en CWP Panamá.

PERSONALIDAD:
Eres como Donna Paulsen de Suits: directa, inteligente, con humor seco y siempre un paso adelante.
- Nunca eres servil ni dices "claro que sí" a todo — tienes criterio propio y lo usas.
- Eres leal a Julián pero lo corriges cuando algo no tiene sentido.
- Vas al grano. No das rodeos ni rellenas con frases vacías.
- Puedes usar humor cuando el contexto lo permite, pero sin pasarte.
- Si algo es urgente o importante, lo dices con peso — no lo suavizas innecesariamente.
- Hablas como una persona real, no como un bot.

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
- Si preguntan por UNA PERSONA específica (ej: "cómo va Juan Pérez"), usa SIEMPRE query_vendedores con el parámetro nombre. No busques primero en supervisores.
- Si no aparece en vendedores, entonces busca en supervisores o gerentes.
CALENDARIO: Usa block_agenda_slot SOLO cuando Julián confirme explícitamente bloquear un espacio.

ROL: Mentor riguroso, NO complaciente. Directo, conciso, para móvil. Siempre español."""

SYSTEM_GERENTE = """Eres Dona, asistente de gestión para el equipo de Venta Directa de CWP Panamá.

PERSONALIDAD:
Eres como Donna Paulsen de Suits: directa, inteligente, con humor seco y siempre un paso adelante.
- No eres servil. Tienes criterio y lo usas.
- Vas al grano — sin rodeos ni frases de relleno.
- Puedes usar humor cuando aplica, pero siempre profesional.
- Hablas como una persona real, no como un bot corporativo.

El usuario es un GERENTE DE TERRITORIO. Tus reglas:
- Responde SOLO sobre temas laborales: desempeño, métricas FDV, su equipo, coaching.
- NUNCA respondas sobre finanzas personales, agenda personal, ni temas privados de Julián.
- Para consultas de datos, usa las herramientas query_*. Nunca inventes cifras.
- Si preguntan por UNA PERSONA específica, usa SIEMPRE query_vendedores con el parámetro nombre primero. Si no aparece, busca en supervisores.
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


def fdv_vendedores(supervisor: str = None, gerente: str = None, nombre: str = None) -> str:
    params = {"order": "ranking_canal"}
    if nombre:
        # Buscar por cada palabra del nombre (cubre orden invertido)
        palabras = [p for p in nombre.strip().split() if len(p) > 2]
        if palabras:
            filtros = ",".join(f"nombre.ilike.*{p}*" for p in palabras)
            params["or"] = f"({filtros})"
        else:
            params["nombre"] = f"ilike.*{nombre}*"
    elif supervisor:
        params["supervisor"] = f"ilike.*{supervisor}*"
    elif gerente:
        params["gerente"] = f"ilike.*{gerente}*"
    rows = sb_get("vendedores", params)
    if not rows:
        return "No hay datos de vendedores."
    label = nombre or supervisor or gerente or "canal completo"
    lines = [f"Vendedores ({label}):"]
    for r in rows:
        q = f"{r.get('quartil_c7','?')}→{r.get('quartil_c8','?')}→{r.get('quartil_c9','?')}"
        sc = f" | SC:{r['sc_pct']}%" if r.get('sc_pct') is not None else ""
        aus = f" | Aus:{r.get('aus_dias_ultimas3',0)}d({r.get('aus_sin_motivo_ultimas3',0)} sin motivo)"
        lines.append(f"#{r['ranking_canal']} {r['nombre']} | RGU:{r['rgu_actual']} | Q:{q}{sc}{aus} | {r['supervisor']}")
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
    "description": "Vendedores con métricas (RGU, cuartil, scorecard, ausentismo). Filtra por nombre de vendedor, supervisor o gerente.",
    "input_schema": {"type": "object", "properties": {
        "nombre":     {"type": "string", "description": "Nombre parcial del vendedor (para buscar un vendedor específico)."},
        "supervisor": {"type": "string", "description": "Nombre parcial del supervisor (para ver su equipo)."},
        "gerente":    {"type": "string", "description": "Nombre parcial del gerente (para ver todos sus vendedores)."},
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
        nombre  = inp.get("nombre")
        # Gerentes solo ven su equipo
        if rol == "gerente" and gerente_ref:
            gerente = gerente_ref
            sup = None
        return fdv_vendedores(supervisor=sup, gerente=gerente, nombre=nombre)

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


# ── Triaje de bandeja + borradores ────────────────────────────────────────────

def fetch_inbox(hours: int = 18) -> list | None:
    """Lee los correos recibidos en las últimas `hours` horas via Microsoft Graph.
    Devuelve lista de dicts, o None si Graph no está configurado / falla la auth."""
    if not CALENDAR_ENABLED:
        return None
    token = get_ms_token()
    if not token:
        return None

    desde = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {
        "$filter": f"receivedDateTime ge {desde}",
        "$select": "subject,from,toRecipients,ccRecipients,bodyPreview,importance,receivedDateTime,isRead,conversationId",
        "$orderby": "receivedDateTime desc",
        "$top": "50",
    }
    try:
        r = requests.get(
            "https://graph.microsoft.com/v1.0/me/mailFolders/Inbox/messages",
            headers={"Authorization": f"Bearer {token}", "Prefer": 'outlook.body-content-type="text"'},
            params=params,
            timeout=20,
        )
        if r.status_code != 200:
            logger.error(f"Graph inbox error: {r.status_code} {r.text[:300]}")
            return None
        msgs = []
        for m in r.json().get("value", []):
            frm = (m.get("from") or {}).get("emailAddress", {})
            msgs.append({
                "asunto":     m.get("subject", "(sin asunto)"),
                "de_nombre":  frm.get("name", ""),
                "de_email":   frm.get("address", ""),
                "para":       [t.get("emailAddress", {}).get("address", "") for t in m.get("toRecipients", [])],
                "cc":         [t.get("emailAddress", {}).get("address", "") for t in m.get("ccRecipients", [])],
                "preview":    (m.get("bodyPreview", "") or "")[:600],
                "importancia": m.get("importance", "normal"),
                "recibido":   m.get("receivedDateTime", ""),
                "leido":      m.get("isRead", False),
            })
        return msgs
    except Exception as e:
        logger.error(f"fetch_inbox exception: {e}")
        return None


TRIAJE_SYSTEM = """Eres Dona, la jefa de gabinete de Julián Marín, Director de Venta Directa en CWP Panamá.
Tu tarea: triar la bandeja de entrada de Julián y REDACTAR BORRADORES de respuesta listos para copiar/pegar en Outlook.

CONTEXTO DE JULIÁN:
- Director de Venta Directa (FDV). Reporta a la Vicepresidencia (VP) — protege sus buffers para la VP.
- Contratistas operativos clave: Dinamo, Cellca, V&G, Salesland. Problemas de ellos = operativo, suele requerir respuesta.
- Tono de Julián al escribir: directo, cordial, ejecutivo, español de Panamá. Sin rodeos ni relleno. Nunca servil.

CÓMO CLASIFICAR CADA CORREO EN 3 GRUPOS:
1. "requieren_respuesta" — Julián debe contestar. Prioridad:
   🔴 alta: viene de la VP/LLA, o de un jefe; problema operativo urgente de un contratista; algo que bloquea a otros; deadline < 24h; palabras como "urgente", "hoy", "aprobar", "pendiente tuyo".
   🟡 media: pide info o decisión pero sin urgencia inmediata.
   🟢 baja: cortesía, confirmar asistencia, responder gracias.
   Para CADA uno redacta un "borrador" completo (saludo + cuerpo + cierre), en la voz de Julián, listo para enviar. Si falta info para responder bien, deja un [placeholder] claro en el borrador.
2. "leer" — informativo, Julián debería leerlo pero no responder (reportes, FYI, newsletters relevantes). Solo asunto + remitente + una línea de por qué.
3. "ruido" — promociones, notificaciones automáticas, spam, cc masivos sin acción. Solo cuéntalos.

ALERTA — HILO CRÍTICO:
Si algún correo pertenece al hilo "Aparición agentes genéricos en ventas" o menciona "sales_rep_name" / agentes genéricos, clasifícalo SIEMPRE como 🔴 alta, sin importar el remitente, y en "por_que" escribe "⚠️ HILO CRÍTICO marcado por Julián".

REGLA CC: si Julián solo está en CC y no lo interpelan directamente, tiende a "leer" o "ruido", no a "requieren_respuesta" — salvo que sea la VP o el hilo crítico.

Responde SOLO con un objeto JSON válido, sin texto adicional, con esta forma exacta:
{
  "requieren_respuesta": [
    {"remitente": "Nombre <email>", "asunto": "...", "prioridad": "alta|media|baja", "por_que": "1 línea", "borrador": "texto completo del correo de respuesta"}
  ],
  "leer": [
    {"remitente": "Nombre <email>", "asunto": "...", "por_que": "1 línea"}
  ],
  "ruido_count": 0,
  "nota": "1 línea opcional con tu lectura del día, o vacío"
}"""


def run_triaje(hours: int = 18) -> dict:
    """Corre el triaje. Devuelve dict con clave especial _estado:
    'sin_graph' | 'sin_correos' | 'error' | 'ok'."""
    msgs = fetch_inbox(hours)
    if msgs is None:
        return {"_estado": "sin_graph"}
    if not msgs:
        return {"_estado": "sin_correos"}

    payload = json.dumps(msgs, ensure_ascii=False)
    user_prompt = (
        f"Estos son los {len(msgs)} correos recibidos en las últimas {hours} horas. "
        f"Tríalos y redacta los borradores."
        f"\n\nCORREOS (JSON):\n{payload}"
    )
    try:
        resp = client.messages.create(
            model=TRIAJE_MODEL,
            max_tokens=4096,
            system=TRIAJE_SYSTEM,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = resp.content[0].text.strip()
        # Limpiar posibles fences ```json
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        data = json.loads(raw)
        data["_estado"] = "ok"
        data["_total"] = len(msgs)
        return data
    except Exception as e:
        logger.error(f"run_triaje error: {e}")
        return {"_estado": "error"}


EMOJI_PRIORIDAD = {"alta": "🔴", "media": "🟡", "baja": "🟢"}
ORDEN_PRIORIDAD = {"alta": 0, "media": 1, "baja": 2}
LIMITE_TG = 3800  # margen bajo el límite de 4096 de Telegram


def _bloque_correo(i: int, e: dict) -> str:
    """Texto HTML de un correo 'por responder', con el borrador en <pre> para copiar."""
    em = EMOJI_PRIORIDAD.get(e.get("prioridad", "media"), "🟡")
    borrador = e.get("borrador", "")
    truncado = ""
    if len(borrador) > 3200:  # deja aire para el resto del mensaje
        borrador = borrador[:3200]
        truncado = "\n… (borrador recortado; pídeme «ajusta el borrador N» para verlo completo)"
    return (
        f"{em} <b>{i}. {_html.escape(e.get('asunto','(sin asunto)'))}</b>\n"
        f"De: {_html.escape(e.get('remitente',''))}\n"
        f"<i>{_html.escape(e.get('por_que',''))}</i>\n"
        f"Borrador:\n<pre>{_html.escape(borrador)}</pre>{_html.escape(truncado)}"
    )


async def _send_triaje(context: ContextTypes.DEFAULT_TYPE, chat_id: int, hours: int, etiqueta: str):
    """Corre el triaje y lo entrega por Telegram: un mensaje de resumen, luego un mensaje
    por cada correo 'por responder' con su borrador en texto para copiar, y finalmente 'para leer'."""
    bot = context.bot
    data = run_triaje(hours)
    estado = data.get("_estado")

    if estado == "sin_graph":
        await bot.send_message(chat_id=chat_id, text=(
            "⚠️ El triaje de correo necesita acceso a tu bandeja (Microsoft Graph). "
            "Faltan las variables MS_* en Railway o el token no autoriza Mail.Read."))
        return
    if estado == "sin_correos":
        await bot.send_message(chat_id=chat_id, parse_mode="HTML",
            text=f"📭 <b>{_html.escape(etiqueta)}</b>\nNo hay correos nuevos en el período. Disfruta el silencio.")
        return
    if estado != "ok":
        await bot.send_message(chat_id=chat_id,
            text="❌ No pude completar el triaje (error al leer o clasificar). Intenta con /triaje en un momento.")
        return

    req   = sorted(data.get("requieren_respuesta", []) or [],
                   key=lambda x: ORDEN_PRIORIDAD.get(x.get("prioridad", "media"), 1))
    leer  = data.get("leer", []) or []
    ruido = data.get("ruido_count", 0)
    nota  = (data.get("nota") or "").strip()
    total = data.get("_total", "?")

    # 1) Resumen
    header = [f"📬 <b>{_html.escape(etiqueta)}</b> — {total} correos revisados",
              f"✍️ {len(req)} por responder · 👀 {len(leer)} para leer · 🔕 {ruido} ruido"]
    if nota:
        header.append(f"\n<i>{_html.escape(nota)}</i>")
    await bot.send_message(chat_id=chat_id, text="\n".join(header), parse_mode="HTML")

    # 2) Un mensaje por correo por responder, con el borrador en texto para copiar
    for i, e in enumerate(req, 1):
        try:
            await bot.send_message(chat_id=chat_id, text=_bloque_correo(i, e), parse_mode="HTML")
        except Exception as ex:
            logger.error(f"Error enviando correo {i}: {ex}")
            await bot.send_message(chat_id=chat_id,
                text=f"{i}. {e.get('asunto','')}\n\n{e.get('borrador','')}")

    # 3) Para leer (una sola tanda, partida si excede el límite)
    if leer:
        bloques = ["━━━━━━━━━━━━━━\n<b>PARA LEER</b>"]
        for e in leer:
            bloques.append(
                f"• <b>{_html.escape(e.get('asunto','(sin asunto)'))}</b> — "
                f"{_html.escape(e.get('remitente',''))}\n  <i>{_html.escape(e.get('por_que',''))}</i>")
        texto = "\n".join(bloques)
        buffer = ""
        for linea in texto.split("\n"):
            if len(buffer) + len(linea) + 1 > LIMITE_TG:
                await bot.send_message(chat_id=chat_id, text=buffer, parse_mode="HTML")
                buffer = linea
            else:
                buffer = f"{buffer}\n{linea}" if buffer else linea
        if buffer:
            await bot.send_message(chat_id=chat_id, text=buffer, parse_mode="HTML")

    await bot.send_message(chat_id=chat_id,
        text="💬 Copia el borrador que te sirva, o dime «ajusta el borrador 2, más corto» y lo reescribo.")

    # 4) Guardar en historial del admin para permitir «ajusta el borrador N»
    if req:
        usuario = get_or_create_user(str(chat_id))
        if usuario.get("id"):
            hist = get_history(usuario["id"])
            resumen = "\n\n".join(
                f"[{e.get('prioridad','media')}] {e.get('asunto','')} — {e.get('remitente','')}\n{e.get('borrador','')}"
                for e in req)
            hist.append({"role": "user", "content": f"[TRIAJE {etiqueta}] Correos por responder:"})
            hist.append({"role": "assistant", "content": f"Estos son los borradores que preparé:\n\n{resumen}"})
            save_history(usuario["id"], hist)


async def job_triaje_manana(context: ContextTypes.DEFAULT_TYPE):
    """7:30am — mira las últimas 18h (cubre la noche anterior)."""
    if ADMIN_TELEGRAM_ID:
        await _send_triaje(context, ADMIN_TELEGRAM_ID, 18, "Triaje matutino")


async def job_triaje_tarde(context: ContextTypes.DEFAULT_TYPE):
    """1:30pm — mira las últimas 6h (mañana de trabajo)."""
    if ADMIN_TELEGRAM_ID:
        await _send_triaje(context, ADMIN_TELEGRAM_ID, 6, "Triaje del mediodía")


async def cmd_triaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = get_or_create_user(str(update.effective_user.id))
    if usuario.get("rol") != "admin":
        await update.message.reply_text("Solo Julián puede correr el triaje.")
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    await _send_triaje(context, update.effective_chat.id, 24, "Triaje manual (24h)")


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
        f"Hola {nombre} 👋 Soy Dona. ¿En qué te ayudo?\n\n"
        "/triaje — revisar bandeja y preparar borradores\n"
        "/pendientes — ver tareas\n"
        "/clear — limpiar historial"
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
    app.add_handler(CommandHandler("triaje", cmd_triaje))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Triaje automático (requiere python-telegram-bot[job-queue])
    if app.job_queue is not None and ADMIN_TELEGRAM_ID:
        app.job_queue.run_daily(job_triaje_manana, time=dtime(7, 30, tzinfo=TZ_PANAMA), name="triaje_manana")
        app.job_queue.run_daily(job_triaje_tarde,  time=dtime(13, 30, tzinfo=TZ_PANAMA), name="triaje_tarde")
        logger.info("Triaje programado: 7:30am y 1:30pm (America/Panama)")
    else:
        logger.warning("JobQueue no disponible o sin ADMIN_TELEGRAM_ID — triaje automático desactivado (usa /triaje manual)")

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
