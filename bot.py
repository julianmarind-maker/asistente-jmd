import os
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import anthropic

# ── Config (via environment variables en Railway) ─────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
# Tu Telegram user ID (número). Obtenerlo mandando /start a @userinfobot en Telegram
ALLOWED_USER_ID  = int(os.environ.get("ALLOWED_USER_ID", "0"))

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """Eres el asistente personal de Julián Marín, Director de Venta Directa en CWP Panamá.

CONTEXTO PERSONAL:
- Tiene TDAH, toma medicamento estimulante a las 7am
- Pico de foco en la mañana; medicamento baja a las 4-5pm
- Llega a casa 6:30pm con rebote de ansiedad y cansancio mental
- Duerme 10-10:30pm. Zona horaria: GMT-5 (Panamá)

AGENDA ANCLA:
- Lunes: día más cargado. Comité 8am, Pre ExCo VP 9:30am. Sin trabajo profundo real.
- Martes y jueves: mejores tardes para trabajo profundo (cuando no hay campo).
- Miércoles: Dinamo campo 4:30pm. Clase inglés 7:30pm.
- Jueves: mejor bloque deep work 10:30-12:00. V&G campo 5pm.
- Viernes: Salesland campo 8am. Landing Revenue 2pm. Review/planificación 4pm.
- Bloque de trabajo autónomo protegido lunes y miércoles 5pm.

EQUIPO DIRECTO (gerentes de territorio, de mayor a menor criticidad):
1. Katiuska | 2. Marcos | 3. Richard | 4. Luis V. | 5. Mayli

CONTRATISTAS FDV:
- Dinamo: martes tarde admin + campo semanal miércoles
- V&G: jueves tarde admin + campo semanal
- Salesland: viernes mañana campo semanal
- Cellca: martes quincenal Sem B (solo admin, menor criticidad)

TU ROL:
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


def is_allowed(user_id: int) -> bool:
    return ALLOWED_USER_ID == 0 or user_id == ALLOWED_USER_ID


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    histories[update.effective_user.id] = []
    await update.message.reply_text(
        "Hola Julián 👋 Soy tu asistente. ¿En qué te ayudo?\n\n"
        "/clear — limpiar historial de conversación"
    )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    histories[update.effective_user.id] = []
    await update.message.reply_text("Historial limpiado. ¿En qué te ayudo?")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return

    user_text = update.message.text
    if user_id not in histories:
        histories[user_id] = []

    histories[user_id].append({"role": "user", "content": user_text})

    # Mantener solo los últimos 20 turnos para no saturar el contexto
    if len(histories[user_id]) > 20:
        histories[user_id] = histories[user_id][-20:]

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="typing"
    )

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=histories[user_id],
        )
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot corriendo...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
