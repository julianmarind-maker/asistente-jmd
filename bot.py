import os
import logging
import json
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import anthropic
import urllib.request

# ── Config (via environment variables en Railway) ─────────────────────────────
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
NOTION_TOKEN      = os.environ["NOTION_TOKEN"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
ALLOWED_USER_ID   = int(os.environ.get("ALLOWED_USER_ID", "0"))

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """Eres el asistente personal de Julián Marín, Director de Venta Directa en CWP Panamá.

CONTEXTO PERSONAL:
- Tiene TDAH, toma medicamento estimulante a las 7am
- Pico de foco en la mañana; medicamento baja a las 4-5pm
- Llega a casa 6:30pm con rebote de ansiedad y cansancio mental
- Duerme 10-10:30pm. Zona horaria: GMT-5 (Panamá)

AGENDA SEMANAL FIJA (bloques inamovibles):
Todos los días:
  07:00         Despertar + medicamento + desayuno
  12:00–14:00   Almuerzo en casa + ejercicio 30 min
  18:30         Llegada a casa
  22:00–22:30   Dormir

LUNES (día más cargado, sin trabajo profundo real):
  08:00–09:00   Comité semanal
  09:30–10:30   Pre ExCo VP
  10:30–12:00   LIBRE (buffer/admin ligero)
  14:00–15:00   1-1 Katiuska
  15:00–16:00   1-1 Marcos
  16:00–17:00   1-1 Richard
  17:00–18:00   Bloque autónomo VP (protegido)

MARTES (buena tarde para trabajo profundo cuando no hay campo):
  08:00–12:00   Trabajo profundo
  14:00–15:00   1-1 Luis V.
  15:00–16:00   1-1 Mayli
  16:00–18:00   Dinamo admin (Sem A quincenal) / Cellca admin (Sem B quincenal)

MIÉRCOLES:
  08:00–12:00   Trabajo profundo
  14:00–16:00   Admin / reuniones varias
  16:30–18:00   Dinamo campo (semanal)
  17:00–18:00   Bloque autónomo VP (protegido)
  19:30–21:00   Clase de inglés

JUEVES (mejor día para deep work):
  08:00–10:00   Trabajo profundo
  10:30–12:00   Mejor bloque deep work de la semana (protegido)
  14:00–15:00   Supervisores Katiuska
  15:00–16:00   Supervisores Marcos
  16:00–17:00   Supervisores Richard
  17:00–18:00   V&G campo (semanal)

VIERNES:
  08:00–10:00   Salesland campo (semanal)
  10:00–12:00   LIBRE (trabajo profundo o admin)
  14:00–15:00   Landing Revenue
  16:00–17:00   Review semanal / planificación siguiente semana
  17:00–18:00   LIBRE

BLOQUES DISPONIBLES PARA REUNIONES NUEVAS:
- Lunes 10:30–12:00
- Martes 08:00–12:00 (sacrifica deep work, solo si es necesario)
- Miércoles 14:00–16:00
- Jueves 08:00–10:00
- Viernes 10:00–12:00 o 17:00–18:00

EQUIPO DIRECTO (gerentes de territorio, de mayor a menor criticidad):
1. Katiuska | 2. Marcos | 3. Richard | 4. Luis V. | 5. Mayli

CONTRATISTAS FDV:
- Dinamo: martes tarde admin (quincenal Sem A) + campo semanal miércoles 4:30pm
- V&G: jueves campo semanal 5pm
- Salesland: viernes campo semanal 8am
- Cellca: martes quincenal Sem B (solo admin, menor criticidad)

GESTIÓN DE PENDIENTES EN NOTION:
Puedes guardar tareas pendientes en Notion cuando Julián te lo pida.
Frases que indican guardar pendiente: "anota", "agrega pendiente", "guarda", "recuérdame", "pendiente:", "tarea:"
Cuando detectes una de estas frases, usa la función add_notion_task con la tarea.
También puedes listar los pendientes cuando te pregunte "¿qué tengo pendiente?" o "muéstrame mis pendientes".

TU ROL:
- Cuando Julián pregunte disponibilidad u horarios, consulta la agenda arriba y responde con precisión
- Mentor riguroso, NO asistente complaciente
- Directo, claro, conversacional — nunca robótico
- Ayudas a priorizar, preparar reuniones, redactar correos, tomar decisiones
- Si algo no tiene sentido o hay un error, lo dices
- Respuestas concisas optimizadas para leer en móvil
- Siempre en español"""

# Historial de conversación por usuario (en memoria)
histories: dict[int, list] = {}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# ── Notion helpers ────────────────────────────────────────────────────────────

def notion_request(method: str, path: str, data: dict = None) -> dict:
    url = f"https://api.notion.com/v1{path}"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }
    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        logger.error(f"Notion API error: {e}")
        return {}


def add_notion_task(task: str) -> bool:
    today = datetime.now().strftime("%Y-%m-%d")
    data = {
        "parent": {"database_id": NOTION_DATABASE_ID},
        "properties": {
            "Name": {
                "title": [{"text": {"content": task}}]
            },
            "Estado": {
                "select": {"name": "Pendiente"}
            },
            "Fecha": {
                "date": {"start": today}
            }
        }
    }
    result = notion_request("POST", "/pages", data)
    return bool(result.get("id"))


def get_notion_tasks() -> list[str]:
    data = {
        "filter": {
            "property": "Estado",
            "select": {"equals": "Pendiente"}
        },
        "sorts": [{"property": "Fecha", "direction": "descending"}]
    }
    result = notion_request("POST", f"/databases/{NOTION_DATABASE_ID}/query", data)
    tasks = []
    for page in result.get("results", []):
        title_prop = page.get("properties", {}).get("Name", {}).get("title", [])
        if title_prop:
            tasks.append(title_prop[0]["text"]["content"])
    return tasks


# ── Tool definitions for Claude ───────────────────────────────────────────────

TOOLS = [
    {
        "name": "add_task",
        "description": "Guarda una tarea o pendiente en Notion. Úsala cuando Julián pida anotar, guardar o recordar algo.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "La tarea o pendiente a guardar"}
            },
            "required": ["task"]
        }
    },
    {
        "name": "list_tasks",
        "description": "Lista las tareas pendientes de Notion. Úsala cuando Julián pregunte qué tiene pendiente.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    }
]


def handle_tool_call(tool_name: str, tool_input: dict) -> str:
    if tool_name == "add_task":
        task = tool_input.get("task", "")
        success = add_notion_task(task)
        if success:
            return f"✅ Guardado en Notion: '{task}'"
        else:
            return "❌ No pude guardar la tarea en Notion."
    elif tool_name == "list_tasks":
        tasks = get_notion_tasks()
        if not tasks:
            return "No tienes tareas pendientes en Notion."
        return "📋 Tus pendientes:\n" + "\n".join(f"• {t}" for t in tasks)
    return "Herramienta no reconocida."


# ── Auth ──────────────────────────────────────────────────────────────────────

def is_allowed(user_id: int) -> bool:
    return ALLOWED_USER_ID == 0 or user_id == ALLOWED_USER_ID


# ── Handlers ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    histories[update.effective_user.id] = []
    await update.message.reply_text(
        "Hola Julián 👋 Soy tu asistente. ¿En qué te ayudo?\n\n"
        "/clear — limpiar historial\n"
        "/pendientes — ver tareas en Notion"
    )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    histories[update.effective_user.id] = []
    await update.message.reply_text("Historial limpiado. ¿En qué te ayudo?")


async def cmd_pendientes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    tasks = get_notion_tasks()
    if not tasks:
        await update.message.reply_text("No tienes tareas pendientes en Notion.")
    else:
        text = "📋 *Tus pendientes:*\n" + "\n".join(f"• {t}" for t in tasks)
        await update.message.reply_text(text, parse_mode="Markdown")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return

    user_text = update.message.text
    if user_id not in histories:
        histories[user_id] = []

    histories[user_id].append({"role": "user", "content": user_text})

    if len(histories[user_id]) > 20:
        histories[user_id] = histories[user_id][-20:]

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=histories[user_id],
        )

        # Handle tool use
        if response.stop_reason == "tool_use":
            tool_results = []
            reply_parts = []

            for block in response.content:
                if block.type == "tool_use":
                    result_text = handle_tool_call(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text
                    })
                    reply_parts.append(result_text)

            # Add assistant response and tool results to history
            histories[user_id].append({"role": "assistant", "content": response.content})
            histories[user_id].append({"role": "user", "content": tool_results})

            # Get final response from Claude after tool execution
            final_response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=512,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=histories[user_id],
            )
            reply = final_response.content[0].text
            histories[user_id].append({"role": "assistant", "content": reply})

        else:
            reply = response.content[0].text
            histories[user_id].append({"role": "assistant", "content": reply})

        await update.message.reply_text(reply)

    except Exception as e:
        logger.error(f"Error Claude API: {e}")
        await update.message.reply_text(
            "Hubo un error al procesar tu mensaje. Intenta de nuevo."
        )


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("pendientes", cmd_pendientes))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot corriendo...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
