import os
import re
import json
import logging
import asyncio
import threading
from datetime import datetime
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    PicklePersistence,
    filters,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
_raw_sheet_id = os.environ.get("GOOGLE_SHEET_ID", "")
_match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", _raw_sheet_id)
GOOGLE_SHEET_ID = _match.group(1) if _match else _raw_sheet_id
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
print("TOKEN:",bool(TELEGRAM_TOKEN))
print("SHEET:",bool(GOOGLE_SHEET_ID))
print("JSON:",bool(GOOGLE_SERVICE_ACCOUNT_JSON))

WEEKLY_SEND_DAY = "mon"
WEEKLY_SEND_HOUR = 9
WEEKLY_SEND_MINUTE = 0

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
SUBSCRIBERS_FILE = DATA_DIR / "subscribers.json"

HEALTH_QUESTIONS = [
    ("name",        "👋 What's your *name*?"),
    ("cycle_phase", "🌙 What *cycle phase* are you in this week?\n\nReply with one: *follicular / ovulation / luteal / menstrual*"),
    ("sleep_hours", "😴 How many *hours of sleep* did you get in total this week?\n\nReply with a number (e.g. 49)."),
    ("anxiety",     "😟 What was your *anxiety score* this week?\n\nReply with a number from 1 (none) to 10 (very high)."),
    ("exercise",    "🏃 Did you *exercise* this week?\n\nReply: *yes* or *no*"),
    ("triggers",    "⚡ What *triggers your anxiety*?\n\nDescribe briefly (e.g. work, relationships, sleep)."),
    ("suggestion",  "💡 Did *last week's suggestion* help you?\n\nReply: *yes / no / first time*"),
    ("coping",      "🧘 What do you *do when you have anxiety*?\n\nShare your coping method."),
]

(
    Q_NAME,
    Q_PHASE,
    Q_SLEEP,
    Q_ANXIETY,
    Q_EXERCISE,
    Q_TRIGGERS,
    Q_SUGGESTION,
    Q_COPING,
) = range(8)

STATES = [Q_NAME, Q_PHASE, Q_SLEEP, Q_ANXIETY, Q_EXERCISE, Q_TRIGGERS, Q_SUGGESTION, Q_COPING]


def load_subscribers() -> dict:
    if SUBSCRIBERS_FILE.exists():
        with open(SUBSCRIBERS_FILE) as f:
            return json.load(f)
    return {}


def save_subscribers(subscribers: dict):
    with open(SUBSCRIBERS_FILE, "w") as f:
        json.dump(subscribers, f, indent=2)


def get_sheet():
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        logger.error("GOOGLE_SERVICE_ACCOUNT_JSON not set")
        return None
    if not GOOGLE_SHEET_ID:
        logger.error("GOOGLE_SHEET_ID not set")
        return None
    try:
        creds_dict = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        creds = Credentials.from_service_account_info(
            creds_dict,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
        try:
            sheet = spreadsheet.worksheet("Health Responses")
        except gspread.WorksheetNotFound:
            sheet = spreadsheet.add_worksheet("Health Responses", rows=1000, cols=20)
            headers = [
                "Date", "Telegram User ID", "Telegram Name", "Username",
                "Name", "Cycle Phase", "Sleep Hours (week total)",
                "Anxiety Score (1-10)", "Exercise (yes/no)",
                "Anxiety Triggers", "Last Suggestion Helped", "Coping Method",
            ]
            sheet.append_row(headers)
        return sheet
    except Exception as e:
        logger.error(f"Google Sheets error: {e}")
        return None


def save_response_to_sheet(user_id: int, name: str, username: str, answers: dict):
    logger.info(f"Saving check-in for user {user_id} — answers: {answers}")
    sheet = get_sheet()
    if not sheet:
        logger.error("save_response_to_sheet: could not get sheet")
        return False
    try:
        row = [
            answers.get("name", ""),
            answers.get("cycle_phase", ""),
            answers.get("sleep_hours", ""),
            answers.get("anxiety", ""),
            answers.get("exercise", ""),
            answers.get("triggers", ""),
            answers.get("suggestion", ""),
            answers.get("coping", ""),
        ]
        logger.info(f"Appending row: {row}")
        sheet.append_row(row)
        logger.info(f"Row saved successfully for user {user_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to write row for user {user_id}: {e}", exc_info=True)
        return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    subscribers = load_subscribers()
    subscribers[str(user.id)] = {
        "name": user.full_name,
        "username": user.username,
        "subscribed_at": datetime.now().isoformat(),
    }
    save_subscribers(subscribers)
    await update.message.reply_text(
        f"👋 Hello {user.first_name}!\n\n"
        "You're now subscribed to *weekly health check-ins*. "
        "Every Monday at 9 AM I'll ask you a few quick questions about your health.\n\n"
        "📋 Commands:\n"
        "/checkin — answer your health questions now\n"
        "/report — see your last 4 weeks of trends\n"
        "/stop — unsubscribe\n"
        "/status — see your subscription status",
        parse_mode="Markdown",
    )


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    subscribers = load_subscribers()
    if str(user.id) in subscribers:
        del subscribers[str(user.id)]
        save_subscribers(subscribers)
        await update.message.reply_text(
            "✅ You've been unsubscribed from weekly health check-ins.\n"
            "Send /start anytime to re-subscribe."
        )
    else:
        await update.message.reply_text("You're not currently subscribed. Send /start to subscribe.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    subscribers = load_subscribers()
    if str(user.id) in subscribers:
        sub = subscribers[str(user.id)]
        since = sub.get("subscribed_at", "unknown")[:10]
        await update.message.reply_text(
            f"✅ You are subscribed to weekly health check-ins.\n"
            f"Subscribed since: {since}\n"
            f"Check-ins are sent every *Monday at 9:00 AM*.\n\n"
            "Use /checkin to answer your questions now.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            "❌ You are not subscribed. Send /start to subscribe."
        )


async def checkin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["answers"] = {}
    await update.message.reply_text(
        "📋 *Weekly Health Check-in*\n\nLet's start! Answer each question with a number or short text.\n\nYou can type /cancel at any time to stop.",
        parse_mode="Markdown",
    )
    await update.message.reply_text(HEALTH_QUESTIONS[0][1], parse_mode="Markdown")
    return Q_NAME


async def receive_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current_state = context.user_data.get("current_state", 0)
    key = HEALTH_QUESTIONS[current_state][0]
    context.user_data["answers"][key] = update.message.text.strip()

    next_state = current_state + 1
    context.user_data["current_state"] = next_state

    if next_state < len(HEALTH_QUESTIONS):
        await update.message.reply_text(
            HEALTH_QUESTIONS[next_state][1], parse_mode="Markdown"
        )
        return STATES[next_state]
    else:
        return await finish_checkin(update, context)


async def finish_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    answers = context.user_data.get("answers", {})

    saved = save_response_to_sheet(
        user.id, user.full_name, user.username, answers
    )

    summary = (
        "✅ *Check-in complete! Here's your summary:*\n\n"
        f"👋 Name: {answers.get('name', '—')}\n"
        f"🌙 Cycle phase: {answers.get('cycle_phase', '—')}\n"
        f"😴 Sleep this week: {answers.get('sleep_hours', '—')} hrs\n"
        f"😟 Anxiety score: {answers.get('anxiety', '—')}/10\n"
        f"🏃 Exercise: {answers.get('exercise', '—')}\n"
        f"⚡ Triggers: {answers.get('triggers', '—')}\n"
        f"💡 Last suggestion helped: {answers.get('suggestion', '—')}\n"
        f"🧘 Coping method: {answers.get('coping', '—')}\n\n"
    )
    if saved:
        summary += "📊 Your responses have been saved to Google Sheets!"
    else:
        summary += "⚠️ Could not save to Google Sheets — check your credentials."

    context.user_data.clear()
    await update.message.reply_text(summary, parse_mode="Markdown")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Check-in cancelled. You can start again with /checkin.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


def get_user_rows(user_id: int) -> list[dict]:
    sheet = get_sheet()
    if not sheet:
        return []
    try:
        all_rows = sheet.get_all_records()
        user_rows = [r for r in all_rows if str(r.get("User ID", "")) == str(user_id)]
        user_rows.sort(key=lambda r: r.get("Date", ""), reverse=True)
        return user_rows[:4]
    except Exception as e:
        logger.error(f"Failed to fetch rows: {e}")
        return []


def trend_arrow(values: list[float]) -> str:
    if len(values) < 2:
        return "➡️"
    diff = values[0] - values[-1]
    if diff > 0.4:
        return "📈"
    if diff < -0.4:
        return "📉"
    return "➡️"


def fmt(val: str) -> str:
    try:
        return f"{float(val):.1f}"
    except (ValueError, TypeError):
        return str(val) if val else "—"


async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text("⏳ Fetching your last 4 weeks from Google Sheets...")

    rows = get_user_rows(user.id)
    if not rows:
        await update.message.reply_text(
            "📭 No check-in data found for you yet.\n\nComplete your first check-in with /checkin!"
        )
        return

    COLS = {
        "overall_health": "Overall Health (1-10)",
        "sleep":          "Sleep Hours",
        "exercise":       "Exercise Days",
        "stress":         "Stress Level (1-10)",
        "water":          "Water Glasses",
        "mood":           "Mood (1-10)",
    }

    def col_values(col_name: str) -> list[float]:
        out = []
        for r in rows:
            try:
                out.append(float(r.get(col_name, "")))
            except (ValueError, TypeError):
                pass
        return out

    def avg(vals: list[float]) -> str:
        return f"{sum(vals)/len(vals):.1f}" if vals else "—"

    lines = [f"📊 *Your last {len(rows)} week(s) of health data*\n"]

    for label, col in [
        ("🏥 Overall health", COLS["overall_health"]),
        ("😴 Sleep",          COLS["sleep"]),
        ("🏃 Exercise days",  COLS["exercise"]),
        ("😓 Stress",         COLS["stress"]),
        ("💧 Water glasses",  COLS["water"]),
        ("😊 Mood",           COLS["mood"]),
    ]:
        vals = col_values(col)
        arrow = trend_arrow(vals)
        weekly = " → ".join(fmt(r.get(col, "—")) for r in reversed(rows))
        lines.append(f"{label}: avg *{avg(vals)}* {arrow}\n  _{weekly}_")

    lines.append("")
    lines.append("*Week-by-week dates:*")
    for i, r in enumerate(reversed(rows), 1):
        date = r.get("Date", "—")[:10]
        lines.append(f"  Week {i}: {date}")

    if len(rows) >= 2:
        health_vals = col_values(COLS["overall_health"])
        mood_vals   = col_values(COLS["mood"])
        stress_vals = col_values(COLS["stress"])

        insights = []
        if health_vals and health_vals[0] > (sum(health_vals) / len(health_vals)):
            insights.append("💪 Your overall health is *improving* — keep it up!")
        if stress_vals and stress_vals[0] < (sum(stress_vals) / len(stress_vals)):
            insights.append("🧘 Your stress is *trending down* — great work!")
        if mood_vals and mood_vals[0] < (sum(mood_vals) / len(mood_vals)) - 0.5:
            insights.append("💛 Your mood has dipped recently — make sure to take care of yourself.")

        if insights:
            lines.append("\n*Insights:*")
            lines.extend(insights)

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def send_weekly_checkin(app: Application):
    subscribers = load_subscribers()
    if not subscribers:
        logger.info("No subscribers for weekly check-in")
        return
    logger.info(f"Sending weekly check-in to {len(subscribers)} subscribers")
    for user_id_str in subscribers:
        try:
            await app.bot.send_message(
                chat_id=int(user_id_str),
                text=(
                    "👋 *It's time for your weekly health check-in!*\n\n"
                    "Send /checkin to answer this week's questions — it only takes 2 minutes."
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.warning(f"Could not message {user_id_str}: {e}")


async def post_init(app: Application) -> None:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        send_weekly_checkin,
        trigger="cron",
        day_of_week=WEEKLY_SEND_DAY,
        hour=WEEKLY_SEND_HOUR,
        minute=WEEKLY_SEND_MINUTE,
        args=[app],
    )
    scheduler.start()
    logger.info(f"Scheduler started — weekly check-ins every {WEEKLY_SEND_DAY.upper()} at {WEEKLY_SEND_HOUR:02d}:{WEEKLY_SEND_MINUTE:02d}")


def build_app() -> Application:
    persistence = PicklePersistence(filepath=DATA_DIR / "bot_persistence")
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .persistence(persistence)
        .post_init(post_init)
        .build()
    )

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("checkin", checkin_start)],
        states={
            Q_NAME:       [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: _handle(u, c, 0))],
            Q_PHASE:      [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: _handle(u, c, 1))],
            Q_SLEEP:      [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: _handle(u, c, 2))],
            Q_ANXIETY:    [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: _handle(u, c, 3))],
            Q_EXERCISE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: _handle(u, c, 4))],
            Q_TRIGGERS:   [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: _handle(u, c, 5))],
            Q_SUGGESTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: _handle(u, c, 6))],
            Q_COPING:     [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: _handle(u, c, 7))],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
        persistent=True,
        name="checkin_conversation",
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(conv_handler)

    return app


async def _handle(update: Update, context: ContextTypes.DEFAULT_TYPE, state_index: int):
    context.user_data["current_state"] = state_index
    return await receive_answer(update, context)


class _PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass


def start_keep_alive():
    port = int(os.environ.get("PORT", 8443))
    server = HTTPServer(("0.0.0.0", port), _PingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"Keep-alive server running on port {port}")


if __name__ == "__main__":
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is not set")

    start_keep_alive()
    app = build_app()
    logger.info("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
