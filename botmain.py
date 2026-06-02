import os
import re
import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

import gspread
from google.oauth2.service_account import Credentials
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
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

WEEKLY_SEND_DAY = "mon"
WEEKLY_SEND_HOUR = 9
WEEKLY_SEND_MINUTE = 0

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
SUBSCRIBERS_FILE = DATA_DIR / "subscribers.json"

# ── Conversation states ────────────────────────────────────────────────────────
(
    Q_GENDER,
    Q_NAME,
    Q_PHASE,           # female only
    Q_SLEEP,
    Q_EXERCISE,
    Q_ANXIETY,
    Q_TRIGGER,
    Q_TRIGGER_OTHER,
    Q_COPING,
    Q_COPING_OTHER,
    Q_SUGGESTION,
    Q_ANXIETY_PHASE,   # female only
    Q_SYMPTOMS,        # female only
) = range(13)

# ── Static options ─────────────────────────────────────────────────────────────
GENDERS       = ["Female", "Male"]
PHASES        = ["Menstrual", "Follicular", "Ovulation", "Luteal"]
EXERCISE_OPTS = ["Never", "1-2 days a week", "3-5 days a week", "Daily"]
TRIGGER_OPTS  = [
    "Family", "Studies", "Career", "Future", "Low self-esteem",
    "Relationship", "Health", "Financial", "Social", "Other",
]
COPING_OPTS = [
    "Exercise", "Journaling", "Meditation", "Prayers", "Music",
    "Talking to friend/family", "Gaming", "Eating",
    "Watching phone/movies etc", "Sleeping", "Travelling",
    "Self harm", "None", "Other",
]
SUGGESTION_OPTS = ["Yes", "Partially", "No", "First time"]

SYMPTOMS_BY_PHASE = {
    "Menstrual": [
        "Mild cramps", "Moderate cramps", "Severe cramps",
        "Mood swings", "Fatigue", "Heavy bleeding", "Clots",
    ],
    "Follicular": [
        "More energy", "Good mood", "Bad mood",
        "Stress", "Freaky", "Apathetic", "Motivated",
    ],
    "Ovulation": [
        "Good mood", "More energy", "Mild cramp",
        "Freaky", "Confident", "Stress",
    ],
    "Luteal": [
        "Bad mood", "Low energy", "Dull",
        "Angry", "Sad", "Stress", "Normal", "Cramps",
    ],
}

SYMPTOM_QUESTION_BY_PHASE = {
    "Menstrual":  "🩸 What *menstrual symptoms* are you experiencing?\n_(select all that apply, then tap ✓ Done)_",
    "Follicular": "🌱 What are you *experiencing now* in your Follicular phase?\n_(select all that apply, then tap ✓ Done)_",
    "Ovulation":  "🌸 What are you *experiencing* in your Ovulation phase?\n_(select all that apply, then tap ✓ Done)_",
    "Luteal":     "🌕 What are you *experiencing* in your Luteal phase?\n_(select all that apply, then tap ✓ Done)_",
}

# Female = 11 questions, Male = 8 questions
# Q numbers for shared questions differ by gender:
#   Female: Gender=1 Name=2 Phase=3 Sleep=4 Exercise=5 Anxiety=6 Trigger=7 Coping=8 Suggestion=9 AnxPhase=10 Symptoms=11
#   Male:   Gender=1 Name=2          Sleep=3 Exercise=4 Anxiety=5 Trigger=6 Coping=7 Suggestion=8
Q_NUM = {
    "female": {
        "name": "2/11", "phase": "3/11", "sleep": "4/11", "exercise": "5/11",
        "anxiety": "6/11", "trigger": "7/11", "coping": "8/11",
        "suggestion": "9/11", "anxiety_phase": "10/11", "symptoms": "11/11",
    },
    "male": {
        "name": "2/8", "sleep": "3/8", "exercise": "4/8",
        "anxiety": "5/8", "trigger": "6/8", "coping": "7/8", "suggestion": "8/8",
    },
}

SHEET_HEADERS = [
    "Date", "Gender", "Name", "Current Phase", "Avg Sleep (hrs/day)",
    "Exercise Frequency", "Anxiety Score (1-10)", "Anxiety Trigger",
    "Coping Method(s)", "Previous Suggestion Helped",
    "Most Anxious Phase", "Symptoms/Experience",
]


# ── Keyboard helpers ───────────────────────────────────────────────────────────
def reply_kb(options: list[str], cols: int = 2) -> ReplyKeyboardMarkup:
    rows = [options[i:i + cols] for i in range(0, len(options), cols)]
    return ReplyKeyboardMarkup(rows, one_time_keyboard=True, resize_keyboard=True)


def multiselect_kb(options: list[str], selected: list[str], prefix: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(
            f"{'✅' if opt in selected else '⬜'} {opt}",
            callback_data=f"{prefix}:{opt}",
        )]
        for opt in options
    ]
    buttons.append([InlineKeyboardButton("✓ Done", callback_data=f"{prefix}:DONE")])
    return InlineKeyboardMarkup(buttons)


def is_female(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return context.user_data.get("answers", {}).get("gender", "").lower() == "female"


def qnum(context: ContextTypes.DEFAULT_TYPE, key: str) -> str:
    gender = "female" if is_female(context) else "male"
    return Q_NUM[gender].get(key, "")


# ── Sheet helpers ──────────────────────────────────────────────────────────────
def load_subscribers() -> dict:
    if SUBSCRIBERS_FILE.exists():
        with open(SUBSCRIBERS_FILE) as f:
            return json.load(f)
    return {}


def save_subscribers(subscribers: dict):
    with open(SUBSCRIBERS_FILE, "w") as f:
        json.dump(subscribers, f, indent=2)


def get_sheet():
    if not GOOGLE_SERVICE_ACCOUNT_JSON or not GOOGLE_SHEET_ID:
        logger.error("Google Sheets credentials not set")
        return None
    try:
        creds = Credentials.from_service_account_info(
            json.loads(GOOGLE_SERVICE_ACCOUNT_JSON),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
        try:
            sheet = spreadsheet.worksheet("Health Responses")
        except gspread.WorksheetNotFound:
            sheet = spreadsheet.add_worksheet("Health Responses", rows=1000, cols=20)
            sheet.append_row(SHEET_HEADERS)
        return sheet
    except Exception as e:
        logger.error(f"Google Sheets error: {e}")
        return None


def save_response_to_sheet(user_id: int, answers: dict) -> bool:
    logger.info(f"Saving check-in for user {user_id} — answers: {answers}")
    sheet = get_sheet()
    if not sheet:
        logger.error("save_response_to_sheet: could not get sheet")
        return False
    try:
        female = answers.get("gender", "").lower() == "female"
        row = [
            datetime.now().strftime("%Y-%m-%d"),
            answers.get("gender", ""),
            answers.get("name", ""),
            answers.get("phase", "N/A") if female else "N/A",
            answers.get("sleep", ""),
            answers.get("exercise", ""),
            answers.get("anxiety", ""),
            answers.get("trigger", ""),
            answers.get("coping", ""),
            answers.get("suggestion", ""),
            answers.get("anxiety_phase", "N/A") if female else "N/A",
            answers.get("symptoms", "N/A") if female else "N/A",
        ]
        logger.info(f"Appending row: {row}")
        sheet.append_row(row)
        logger.info(f"Row saved successfully for user {user_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to write row for user {user_id}: {e}", exc_info=True)
        return False


# ── Command handlers ───────────────────────────────────────────────────────────
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
        "You're now subscribed to *weekly anxiety check-ins*.\n"
        "Every Monday at 9 AM I'll send you a quick check-in.\n\n"
        "📋 Commands:\n"
        "/checkin — start your check-in now\n"
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
            "✅ You've been unsubscribed.\nSend /start anytime to re-subscribe."
        )
    else:
        await update.message.reply_text("You're not subscribed. Send /start to subscribe.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    subscribers = load_subscribers()
    if str(user.id) in subscribers:
        since = subscribers[str(user.id)].get("subscribed_at", "")[:10]
        await update.message.reply_text(
            f"✅ Subscribed since: {since}\n"
            "Check-ins every *Monday at 9:00 AM*.\n\nUse /checkin to go now.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text("❌ Not subscribed. Send /start to subscribe.")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Check-in cancelled. Start again with /checkin.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


# ── Check-in flow ──────────────────────────────────────────────────────────────
async def checkin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["answers"] = {}
    total = "11" if is_female(context) else "determining…"
    await update.message.reply_text(
        "📋 *Weekly Anxiety Check-in*\n\nType /cancel anytime to stop.",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    await update.message.reply_text(
        "👤 *Q1 — What is your gender?*",
        parse_mode="Markdown",
        reply_markup=reply_kb(GENDERS, cols=2),
    )
    return Q_GENDER


# Q1 — Gender
async def q_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text not in GENDERS:
        await update.message.reply_text(
            "Please choose one of the options:",
            reply_markup=reply_kb(GENDERS, cols=2),
        )
        return Q_GENDER
    context.user_data["answers"]["gender"] = text
    n = qnum(context, "name")
    await update.message.reply_text(
        f"👋 *Q{n} — What is your name?*",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return Q_NAME


# Q2 — Name
async def q_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["answers"]["name"] = update.message.text.strip()
    if is_female(context):
        n = qnum(context, "phase")
        await update.message.reply_text(
            f"🌙 *Q{n} — Which menstrual phase are you currently in?*",
            parse_mode="Markdown",
            reply_markup=reply_kb(PHASES),
        )
        return Q_PHASE
    else:
        return await _ask_sleep(update.message, context)


# Q3 (female) — Current phase
async def q_phase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text not in PHASES:
        await update.message.reply_text(
            "Please choose one of the options below:",
            reply_markup=reply_kb(PHASES),
        )
        return Q_PHASE
    context.user_data["answers"]["phase"] = text
    return await _ask_sleep(update.message, context)


async def _ask_sleep(message, context: ContextTypes.DEFAULT_TYPE):
    n = qnum(context, "sleep")
    await message.reply_text(
        f"😴 *Q{n} — How many hours do you sleep on average per day?*\n\n"
        "_Reply with a number (e.g. 7)_",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return Q_SLEEP


# Sleep (numeric)
async def q_sleep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        val = float(text)
        if val < 0 or val > 24:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Please enter a number between 0 and 24 (e.g. 7).")
        return Q_SLEEP
    context.user_data["answers"]["sleep"] = text
    n = qnum(context, "exercise")
    await update.message.reply_text(
        f"🏃 *Q{n} — How often do you exercise?*",
        parse_mode="Markdown",
        reply_markup=reply_kb(EXERCISE_OPTS, cols=2),
    )
    return Q_EXERCISE


# Exercise frequency
async def q_exercise(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text not in EXERCISE_OPTS:
        await update.message.reply_text(
            "Please choose one of the options below:",
            reply_markup=reply_kb(EXERCISE_OPTS, cols=2),
        )
        return Q_EXERCISE
    context.user_data["answers"]["exercise"] = text
    n = qnum(context, "anxiety")
    await update.message.reply_text(
        f"😟 *Q{n} — What is your current anxiety score?*\n\n"
        "_Reply with a number from 1 (none) to 10 (very high)_",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return Q_ANXIETY


# Anxiety score (1–10)
async def q_anxiety(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        val = int(text)
        if val < 1 or val > 10:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Please enter a whole number between 1 and 10.")
        return Q_ANXIETY
    context.user_data["answers"]["anxiety"] = text
    context.user_data["trigger_selected"] = []
    n = qnum(context, "trigger")
    kb = multiselect_kb(TRIGGER_OPTS, [], "trigger")
    await update.message.reply_text(
        f"⚡ *Q{n} — What triggers your anxiety?*\n\n"
        "_(tap to select all that apply, then tap ✓ Done)_",
        parse_mode="Markdown",
        reply_markup=kb,
    )
    return Q_TRIGGER


# Anxiety trigger multi-select
async def q_trigger_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, value = query.data.split(":", 1)
    selected: list = context.user_data.setdefault("trigger_selected", [])

    if value == "DONE":
        if not selected:
            await query.answer("Please select at least one option.", show_alert=True)
            return Q_TRIGGER
        context.user_data["answers"]["trigger"] = ", ".join(selected)
        await query.edit_message_reply_markup(reply_markup=None)
        if "Other" in selected:
            await query.message.reply_text(
                "✏️ You selected *Other* — please *describe your trigger*:",
                parse_mode="Markdown",
            )
            return Q_TRIGGER_OTHER
        return await _ask_coping(query.message, context)

    if value in selected:
        selected.remove(value)
    else:
        selected.append(value)
    await query.edit_message_reply_markup(reply_markup=multiselect_kb(TRIGGER_OPTS, selected, "trigger"))
    return Q_TRIGGER


# Trigger "Other" free text
async def q_trigger_other(update: Update, context: ContextTypes.DEFAULT_TYPE):
    extra = update.message.text.strip()
    parts = [p.strip() for p in context.user_data["answers"].get("trigger", "").split(",")]
    context.user_data["answers"]["trigger"] = ", ".join(extra if p == "Other" else p for p in parts)
    return await _ask_coping(update.message, context)


async def _ask_coping(message, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["coping_selected"] = []
    n = qnum(context, "coping")
    kb = multiselect_kb(COPING_OPTS, [], "coping")
    await message.reply_text(
        f"🧘 *Q{n} — What is your primary coping method?*\n\n"
        "_(tap to select all that apply, then tap ✓ Done)_",
        parse_mode="Markdown",
        reply_markup=kb,
    )
    return Q_COPING


# Coping multi-select
async def q_coping_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, value = query.data.split(":", 1)
    selected: list = context.user_data.setdefault("coping_selected", [])

    if value == "DONE":
        if not selected:
            await query.answer("Please select at least one option.", show_alert=True)
            return Q_COPING
        context.user_data["answers"]["coping"] = ", ".join(selected)
        await query.edit_message_reply_markup(reply_markup=None)
        if "Other" in selected:
            await query.message.reply_text(
                "✏️ You selected *Other* — please *describe your coping method*:",
                parse_mode="Markdown",
            )
            return Q_COPING_OTHER
        return await _ask_suggestion(query.message, context)

    if value in selected:
        selected.remove(value)
    else:
        selected.append(value)
    await query.edit_message_reply_markup(reply_markup=multiselect_kb(COPING_OPTS, selected, "coping"))
    return Q_COPING


# Coping "Other" free text
async def q_coping_other(update: Update, context: ContextTypes.DEFAULT_TYPE):
    extra = update.message.text.strip()
    parts = [p.strip() for p in context.user_data["answers"].get("coping", "").split(",")]
    context.user_data["answers"]["coping"] = ", ".join(extra if p == "Other" else p for p in parts)
    return await _ask_suggestion(update.message, context)


async def _ask_suggestion(message, context: ContextTypes.DEFAULT_TYPE):
    n = qnum(context, "suggestion")
    await message.reply_text(
        f"💡 *Q{n} — Did the previous suggestion help you?*",
        parse_mode="Markdown",
        reply_markup=reply_kb(SUGGESTION_OPTS, cols=2),
    )
    return Q_SUGGESTION


# Suggestion helped
async def q_suggestion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text not in SUGGESTION_OPTS:
        await update.message.reply_text(
            "Please choose one of the options below:",
            reply_markup=reply_kb(SUGGESTION_OPTS, cols=2),
        )
        return Q_SUGGESTION
    context.user_data["answers"]["suggestion"] = text

    # Males finish here; females continue to cycle questions
    if not is_female(context):
        return await finish_checkin(update.message, context, update.effective_user)

    n = qnum(context, "anxiety_phase")
    await update.message.reply_text(
        f"🔍 *Q{n} — In which menstrual phase do you feel the most anxiety?*",
        parse_mode="Markdown",
        reply_markup=reply_kb(PHASES),
    )
    return Q_ANXIETY_PHASE


# Most anxious phase (female only)
async def q_anxiety_phase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text not in PHASES:
        await update.message.reply_text(
            "Please choose one of the options below:",
            reply_markup=reply_kb(PHASES),
        )
        return Q_ANXIETY_PHASE
    context.user_data["answers"]["anxiety_phase"] = text
    context.user_data["symptoms_selected"] = []

    n = qnum(context, "symptoms")
    question = SYMPTOM_QUESTION_BY_PHASE[text]
    kb = multiselect_kb(SYMPTOMS_BY_PHASE[text], [], "symptoms")
    await update.message.reply_text(
        f"*Q{n} — {question}*",
        parse_mode="Markdown",
        reply_markup=kb,
    )
    return Q_SYMPTOMS


# Symptoms multi-select (female only)
async def q_symptoms_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, value = query.data.split(":", 1)

    phase = context.user_data["answers"].get("anxiety_phase", "Menstrual")
    options = SYMPTOMS_BY_PHASE[phase]
    selected: list = context.user_data.setdefault("symptoms_selected", [])

    if value == "DONE":
        if not selected:
            await query.answer("Please select at least one option.", show_alert=True)
            return Q_SYMPTOMS
        context.user_data["answers"]["symptoms"] = ", ".join(selected)
        await query.edit_message_reply_markup(reply_markup=None)
        return await finish_checkin(query.message, context, update.effective_user)

    if value in selected:
        selected.remove(value)
    else:
        selected.append(value)
    await query.edit_message_reply_markup(reply_markup=multiselect_kb(options, selected, "symptoms"))
    return Q_SYMPTOMS


# ── Finish ─────────────────────────────────────────────────────────────────────
async def finish_checkin(message, context: ContextTypes.DEFAULT_TYPE, user):
    answers = context.user_data.get("answers", {})
    female = answers.get("gender", "").lower() == "female"
    saved = save_response_to_sheet(user.id, answers)

    summary = (
        "✅ *Check-in complete! Here's your summary:*\n\n"
        f"👤 Gender: {answers.get('gender', '—')}\n"
        f"👋 Name: {answers.get('name', '—')}\n"
    )
    if female:
        summary += f"🌙 Current phase: {answers.get('phase', '—')}\n"
    summary += (
        f"😴 Avg sleep/day: {answers.get('sleep', '—')} hrs\n"
        f"🏃 Exercise: {answers.get('exercise', '—')}\n"
        f"😟 Anxiety score: {answers.get('anxiety', '—')}/10\n"
        f"⚡ Triggers: {answers.get('trigger', '—')}\n"
        f"🧘 Coping: {answers.get('coping', '—')}\n"
        f"💡 Prev. suggestion: {answers.get('suggestion', '—')}\n"
    )
    if female:
        summary += (
            f"🔍 Most anxious phase: {answers.get('anxiety_phase', '—')}\n"
            f"🩺 Symptoms: {answers.get('symptoms', '—')}\n"
        )
    summary += "\n"
    summary += "📊 Saved to Google Sheets!" if saved else "⚠️ Could not save to Google Sheets."

    context.user_data.clear()
    await message.reply_text(summary, parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ── Weekly reminder ────────────────────────────────────────────────────────────
async def send_weekly_checkin(app: Application):
    subscribers = load_subscribers()
    if not subscribers:
        return
    logger.info(f"Sending weekly check-in to {len(subscribers)} subscribers")
    for user_id_str in subscribers:
        try:
            await app.bot.send_message(
                chat_id=int(user_id_str),
                text=(
                    "👋 *It's time for your weekly anxiety check-in!*\n\n"
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
    logger.info(
        f"Scheduler started — weekly check-ins every "
        f"{WEEKLY_SEND_DAY.upper()} at {WEEKLY_SEND_HOUR:02d}:{WEEKLY_SEND_MINUTE:02d}"
    )


# ── App builder ────────────────────────────────────────────────────────────────
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
            Q_GENDER:        [MessageHandler(filters.TEXT & ~filters.COMMAND, q_gender)],
            Q_NAME:          [MessageHandler(filters.TEXT & ~filters.COMMAND, q_name)],
            Q_PHASE:         [MessageHandler(filters.TEXT & ~filters.COMMAND, q_phase)],
            Q_SLEEP:         [MessageHandler(filters.TEXT & ~filters.COMMAND, q_sleep)],
            Q_EXERCISE:      [MessageHandler(filters.TEXT & ~filters.COMMAND, q_exercise)],
            Q_ANXIETY:       [MessageHandler(filters.TEXT & ~filters.COMMAND, q_anxiety)],
            Q_TRIGGER:       [CallbackQueryHandler(q_trigger_callback, pattern=r"^trigger:")],
            Q_TRIGGER_OTHER: [MessageHandler(filters.TEXT & ~filters.COMMAND, q_trigger_other)],
            Q_COPING:        [CallbackQueryHandler(q_coping_callback, pattern=r"^coping:")],
            Q_COPING_OTHER:  [MessageHandler(filters.TEXT & ~filters.COMMAND, q_coping_other)],
            Q_SUGGESTION:    [MessageHandler(filters.TEXT & ~filters.COMMAND, q_suggestion)],
            Q_ANXIETY_PHASE: [MessageHandler(filters.TEXT & ~filters.COMMAND, q_anxiety_phase)],
            Q_SYMPTOMS:      [CallbackQueryHandler(q_symptoms_callback, pattern=r"^symptoms:")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
        persistent=True,
        name="checkin_conversation",
        per_message=False,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(conv_handler)

    return app


# ── Keep-alive server ──────────────────────────────────────────────────────────
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
