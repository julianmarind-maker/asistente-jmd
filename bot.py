import os
import logging
import json
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import anthropic

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

DESEMPEÑO FDV — COHORTE 9 (fecha: 2026-06-19):
Canal: RGU prom C9=8.37 | Ausentismo=35.7% | 5 gerentes

GERENTES (ranking | nombre | RGU_acum | RGU_C9 | aus% | sin_motivo | NPN60_act | NPN60_ref | delta60 | acción):
1 | Luis Vasquez     | 10.82 | 12.27 | 16.7% | 0  | 12.9 | 18.7 | -5.8 | REFUERZO
2 | Marcos Herrera   |  7.75 |  6.60 | 34.8% | 14 | 31.0 | 32.2 | -1.3 | CAMPO
3 | Mayli Santamaria |  7.18 |  7.85 | 31.8% | 11 | 14.3 | 23.2 | -8.9 | REFUERZO
4 | Katiuska Gonzalez|  6.22 |  9.07 | 35.0% |  3 | 17.5 | 19.5 | -1.9 | REFUERZO
5 | Richard Ramirez  |  6.03 |  6.01 | 26.7% |  1 |  4.8 |  3.1 | +1.7 | CAMPO

SUPERVISORES (ranking_canal | nombre | gerente | RGU_acum | RGU_C9 | aus% | sin_motivo | acción):
 1 | Luis Andres Mojica | Katiuska  | 13.29 | 19.31 |  0.0% |  0 | REFUERZO
 2 | Jose Perez         | Luis V.   | 12.24 | 13.70 |  0.0% |  3 | REFUERZO
 3 | Celibeth Gonzalez  | Luis V.   | 11.45 | 14.10 | 16.7% |  0 | CAMPO
 4 | Gaspar Guerrero    | Marcos    |  9.30 |  6.97 |  0.0% |  2 | REFUERZO
 5 | Jose Madrid        | Luis V.   |  8.78 |  9.00 | 16.7% |  1 | CAMPO
 6 | Lisbeth Rosas      | Mayli     |  8.14 |  9.29 |  0.0% |  2 | REFUERZO
 7 | Cristian Espinosa  | Marcos    |  8.12 |  4.70 |  0.0% |  0 | CAMPO
 8 | Daniel Garzon      | Marcos    |  7.38 |  5.14 | 20.0% |  0 | CAMPO
 9 | Joseph Guerra      | Mayli     |  7.32 |  8.49 | 20.8% | 13 | CAMPO
10 | Kathia Camarena    | Richard   |  7.19 |  6.67 | 29.2% | 14 | CAMPO
11 | Alejandra Perez    | Marcos    |  7.00 |  7.08 | 60.0% | 11 | REFUERZO
12 | Maria Gonzalez     | Marcos    |  6.95 |  9.09 | 35.5% | 39 | CAMPO URGENTE
13 | Juan Contreras     | Katiuska  |  6.10 |  8.04 |  0.0% |  0 | REFUERZO
14 | Joshua Rivera      | Mayli     |  6.08 |  5.79 | 45.0% | 28 | CAMPO URGENTE
15 | Dario White        | Katiuska  |  5.47 |  6.17 | 40.0% |  3 | REFUERZO
16 | Edwin Ortiz        | Richard   |  4.86 |  5.34 | 16.7% |  1 | CAMPO
17 | Jonathan Delgado   | Katiuska  |  4.47 |  5.89 | 36.4% |  5 | CAMPO
18 | Carlos Mojica      | Katiuska  |  4.43 |  5.95 | 25.0% |  3 | CAMPO

VENDEDORES TOP (ranking_canal | nombre | supervisor | gerente | score_coaching | skills débiles):
 1 | Laureano Vega      | J.Guerra   | Mayli    | 78.6 | exploración, presentación, cierre
 2 | Isaí Fuertes       | J.Guerra   | Mayli    | 71.4 | exploración, presentación, objeciones, cierre
 3 | Alvis Dominguez    | J.Perez    | Luis V.  | 71.4 | exploración, presentación, disciplina
 4 | Alberto Quintero   | D.White    | Katiuska | 57.1 | todos en 50
 5 | Gustavo Quintero   | J.Guerra   | Mayli    | 57.1 | exploración, presentación, objeciones, cierre
 6 | Carolina Alvarado  | A.Perez    | Marcos   | 57.1 | exploración, presentación, objeciones, cierre
 7 | Royberto Spencer   | J.Rivera   | Mayli    | 50.0 | disciplina
 8 | Ydania Amaranto    | J.Perez    | Luis V.  | 50.0 | exploración
 9 | Analiz Anderson    | L.Mojica   | Katiuska | 50.0 | todos en 50
10 | Victor Rodriguez   | G.Guerrero | Marcos   | 50.0 | todos en 50
11 | Angel Serrano      | J.Rivera   | Mayli    | 50.0 | presentación
12 | Manuel Rodriguez   | J.Madrid   | Luis V.  | 35.7 | objeciones
13 | Juan Soriano       | J.Perez    | Luis V.  | 35.7 | exploración, presentación, objeciones, cierre
14 | Aramis Rivera      | G.Guerrero | Marcos   | 35.7 | disciplina
15 | Amada Arauz        | M.Gonzalez | Marcos   | 28.6 | presentación, objeciones, disciplina
16 | Victor Saavedra    | J.Madrid   | Luis V.  | 21.4 | presentación, objeciones, cierre, disciplina
17 | Eric Herrera       | D.White    | Katiuska | 21.4 | apertura, presentación, cierre, disciplina
18 | Jorge Gonzalez     | J.Rivera   | Mayli    |  0.0 | todos en 0
19 | Evisabel Vanegas   | J.Madrid   | Luis V.  |  0.0 | todos en 0

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
    try:
        resp = requests.request(method, url, headers=headers, json=data, timeout=10)
        logger.info(f"Notion response: {resp.status_code} {resp.text[:200]}")
        return resp.json()
    except Exception as e:
        logger.error(f"Notion API error: {e}")
        return {}


def add_notion_task(task: str) -> bool:
    data = {
        "parent": {"database_id": NOTION_DATABASE_ID},
        "properties": {
            "Nombre": {
                "title": [{"text": {"content": task}}]
            }
        }
    }
    result = notion_request("POST", "/pages", data)
    return bool(result.get("id"))


def get_notion_tasks() -> list[str]:
    result = notion_request("POST", f"/databases/{NOTION_DATABASE_ID}/query", {})
    tasks = []
    for page in result.get("results", []):
        title_prop = page.get("properties", {}).get("Nombre", {}).get("title", [])
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
