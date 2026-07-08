import logging
import os
import json
from datetime import datetime, timezone, timedelta
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler, JobQueue
)

TOKEN = os.environ["TELEGRAM_TOKEN"]
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])
SHEET_ID = os.environ["SHEET_ID"]
CREDS_INFO = json.loads(os.environ["GOOGLE_CREDS"])
LAUNCH_DATE = datetime(2026, 9, 9, 0, 0, 0, tzinfo=timezone.utc)

logging.basicConfig(level=logging.INFO)

ASK_CHIPS, ASK_CANCEL, ASK_CONFIRM_CANCEL = range(3)

# ─── SHEETS ───────────────────────────────────────────────
_sheet_cache = None

def get_sheet():
    global _sheet_cache
    try:
        if _sheet_cache:
            _sheet_cache.spreadsheet.fetch_sheet_metadata()
            return _sheet_cache
    except:
        _sheet_cache = None
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(CREDS_INFO, scopes=scopes)
    client = gspread.authorize(creds)
    _sheet_cache = client.open_by_key(SHEET_ID).sheet1
    return _sheet_cache

def is_registered(user_id):
    try:
        ids = get_sheet().col_values(1)
        return str(user_id) in ids
    except:
        return False

def save_user(user_id, username, name):
    try:
        get_sheet().append_row([
            str(user_id), username, name,
            datetime.now().strftime("%Y-%m-%d %H:%M"), "", "active"
        ])
    except Exception as e:
        logging.error(f"Sheet save error: {e}")

def mark_blocked(user_id):
    try:
        sheet = get_sheet()
        ids = sheet.col_values(1)
        if str(user_id) in ids:
            row = ids.index(str(user_id)) + 1
            sheet.update_cell(row, 6, "blocked")
    except Exception as e:
        logging.error(f"Mark blocked error: {e}")

def mark_active(user_id):
    try:
        sheet = get_sheet()
        ids = sheet.col_values(1)
        if str(user_id) in ids:
            row = ids.index(str(user_id)) + 1
            sheet.update_cell(row, 6, "active")
    except Exception as e:
        logging.error(f"Mark active error: {e}")

def remove_user(user_id):
    try:
        sheet = get_sheet()
        ids = sheet.col_values(1)
        if str(user_id) in ids:
            sheet.delete_rows(ids.index(str(user_id)) + 1)
    except Exception as e:
        logging.error(f"Sheet remove error: {e}")

def update_activity(user_id):
    try:
        sheet = get_sheet()
        ids = sheet.col_values(1)
        if str(user_id) in ids:
            row = ids.index(str(user_id)) + 1
            sheet.update_cell(row, 5, datetime.now().strftime("%Y-%m-%d %H:%M"))
    except Exception as e:
        logging.error(f"Activity update error: {e}")

def get_all_users():
    try:
        return get_sheet().get_all_values()[1:]
    except:
        return []

def get_all_user_ids():
    return [r[0] for r in get_all_users() if r and r[0]]

# ─── HELPERS ──────────────────────────────────────────────
MAIN_KB = ReplyKeyboardMarkup([["⏳ Time", "✅ Мой статус"]], resize_keyboard=True)

def get_countdown():
    delta = LAUNCH_DATE - datetime.now(timezone.utc)
    if delta.total_seconds() <= 0:
        return "🚀 Equilibrium Club запущен!"
    d = delta.days
    h, rem = divmod(delta.seconds, 3600)
    m = rem // 60
    return f"⏳ До запуска Equilibrium Club:\n\n*{d}* дн. *{h}* ч. *{m}* мин.\n\n📅 Старт: 9 сентября 2026"

def get_user_status(user_id):
    try:
        rows = get_all_users()
        for r in rows:
            if r[0] == str(user_id):
                last = r[4] if len(r) > 4 and r[4] else "—"
                return (
                    f"✅ *Ты в списке Early Bird!*\n\n"
                    f"👤 {r[2]} {r[1]}\n"
                    f"📅 Регистрация: {r[3]}\n"
                    f"🕐 Активность: {last}\n\n"
                    f"🂡 EARLY BIRD + 👑 OG — твои привилегии зафиксированы."
                )
        return "❌ Ты не зарегистрирован.\n\nНажми /start чтобы зарегистрироваться."
    except Exception as e:
        return f"Ошибка при проверке: {e}"

# ─── HANDLERS ─────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = f"@{user.username}" if user.username else "без username"
    name = user.full_name or "—"

    if is_registered(user.id):
        update_activity(user.id)
        kb = [["ЗАКОНЧИТЬ", "ОТМЕНИТЬ РЕГИСТРАЦИЮ"], ["⏳ Time", "✅ Мой статус"]]
        await update.message.reply_text(
            "Ты уже в списке Early Bird. 👑\n\nЧто хочешь сделать?",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True)
        )
        return ASK_CANCEL

    save_user(user.id, username, name)
    await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=f"🟢 Новая регистрация\nИмя: {name}\nUsername: {username}\nID: {user.id}"
    )

    kb = [["УЗНАТЬ", "ЗАКОНЧИТЬ"]]
    await update.message.reply_text(
        "👋 Добро пожаловать в Equilibrium Club — закрытое покерное комьюнити.\n\n"
        "Ваша регистрация принята. Вы в списке Early Bird.\n\n"
        "Хотите узнать какие привилегии вы получите?",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True)
    )
    return ASK_CHIPS

async def handle_chips(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_activity(update.effective_user.id)
    if update.message.text == "УЗНАТЬ":
        await update.message.reply_text(
            "*EARLY BIRD*\n"
            "🂡 50% дисконт на любой уровень подписки и фиксирует эту стоимость навсегда. "
            "Обо всех уровнях подписки ты узнаешь позже, когда будут готовы анонсы.\n\n"
            "👑 *OG* даёт возможность участвовать во всех турнирах, встречах комьюнити, "
            "оффлайн семинарах и конференциях совершенно бесплатно.\n\n"
            "_(не включает дополнительные расходы каждого участника)_\n\n"
            "Дополнительная информация будет поступать тебе через этого бота. Не удаляй диалог.",
            parse_mode="Markdown",
            reply_markup=MAIN_KB
        )
    else:
        await update.message.reply_text(
            "Дополнительная информация будет поступать тебе через этого бота. Не удаляй диалог.",
            reply_markup=MAIN_KB
        )
    return ConversationHandler.END

async def handle_cancel_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    update_activity(update.effective_user.id)

    if text == "ОТМЕНИТЬ РЕГИСТРАЦИЮ":
        kb = [["ДА, отменить", "НЕТ, оставить"]]
        await update.message.reply_text(
            "⚠️ Вы уверены что хотите отменить регистрацию?\n\n"
            "Вы потеряете статус Early Bird и роль OG.",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True)
        )
        return ASK_CONFIRM_CANCEL

    elif text in ["⏳ Time", "⏳ Сколько осталось"]:
        await update.message.reply_text(
            get_countdown(), parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup([["ЗАКОНЧИТЬ", "ОТМЕНИТЬ РЕГИСТРАЦИЮ"], ["⏳ Time", "✅ Мой статус"]], resize_keyboard=True)
        )
        return ASK_CANCEL

    elif text == "✅ Мой статус":
        await update.message.reply_text(
            get_user_status(update.effective_user.id), parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup([["ЗАКОНЧИТЬ", "ОТМЕНИТЬ РЕГИСТРАЦИЮ"], ["⏳ Time", "✅ Мой статус"]], resize_keyboard=True)
        )
        return ASK_CANCEL

    else:
        await update.message.reply_text(
            "До встречи 9 сентября! 🂡",
            reply_markup=MAIN_KB
        )
        return ConversationHandler.END

async def handle_confirm_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user = update.effective_user

    if text == "ДА, отменить":
        remove_user(user.id)
        username = f"@{user.username}" if user.username else "без username"
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"🔴 Отмена регистрации\nUsername: {username}\nID: {user.id}"
        )
        await update.message.reply_text(
            "Регистрация отменена. Если передумаешь — /start.",
            reply_markup=ReplyKeyboardRemove()
        )
    else:
        kb = [["ЗАКОНЧИТЬ", "ОТМЕНИТЬ РЕГИСТРАЦИЮ"], ["⏳ Time", "✅ Мой статус"]]
        await update.message.reply_text(
            "Хорошо, регистрация сохранена. 👑",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True)
        )
    return ConversationHandler.END

async def time_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_activity(update.effective_user.id)
    await update.message.reply_text(get_countdown(), parse_mode="Markdown", reply_markup=MAIN_KB)

async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_activity(update.effective_user.id)
    await update.message.reply_text(
        get_user_status(update.effective_user.id), parse_mode="Markdown", reply_markup=MAIN_KB
    )

async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text in ["⏳ Time", "⏳ Сколько осталось"]:
        await time_command(update, context)
    elif text == "✅ Мой статус":
        await check_command(update, context)

async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    rows = get_all_users()
    if not rows:
        await update.message.reply_text("Список пуст.")
        return
    lines = []
    for i, r in enumerate(rows):
        username = r[1] if r[1] else "—"
        name = r[2] if r[2] else "—"
        date = r[3] if len(r) > 3 else "—"
        link = f"tg://user?id={r[0]}"
        lines.append(f"{i+1}. [{name}]({link}) {username} — {date}")
    await update.message.reply_text(
        f"📋 Зарегистрированных: {len(rows)}\n\n" + "\n".join(lines),
        parse_mode="Markdown"
    )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    rows = get_all_users()
    total = len(rows)
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    active = sum(1 for r in rows if len(r) > 4 and r[4] and r[4][:10] >= week_ago)
    blocked = sum(1 for r in rows if len(r) > 5 and r[5] == "blocked")
    await update.message.reply_text(
        f"📊 *Статистика*\n\n"
        f"👥 Всего зарегистрированных: *{total}*\n"
        f"🟢 Активных за 7 дней: *{active}*\n"
        f"😴 Неактивных: *{total - active}*\n"
        f"🚫 Заблокировали бота: *{blocked}*\n\n"
        f"📅 {get_countdown()}",
        parse_mode="Markdown"
    )

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    text = ' '.join(context.args)
    if not text:
        await update.message.reply_text(
            "Использование: /broadcast Текст сообщения\n\nПример:\n/broadcast Комьюнити открывается 9 сентября!"
        )
        return
    user_ids = get_all_user_ids()
    if not user_ids:
        await update.message.reply_text("Список пуст.")
        return
    sent = failed = 0
    for uid in user_ids:
        try:
            await context.bot.send_message(chat_id=int(uid), text=text)
            sent += 1
        except Exception as e:
            logging.error(f"Broadcast failed for {uid}: {e}")
            failed += 1
    await update.message.reply_text(f"✅ Рассылка завершена\nОтправлено: {sent}\nНе доставлено: {failed}")

async def auto_reminder(context: ContextTypes.DEFAULT_TYPE):
    delta = LAUNCH_DATE - datetime.now(timezone.utc)
    if 6 * 24 * 3600 < delta.total_seconds() <= 7 * 24 * 3600:
        user_ids = get_all_user_ids()
        for uid in user_ids:
            try:
                await context.bot.send_message(
                    chat_id=int(uid),
                    text="🚀 До запуска Equilibrium Club осталась *1 неделя!*\n\n9 сентября 2026 — мы открываемся. Твой статус Early Bird и роль OG уже зафиксированы.",
                    parse_mode="Markdown"
                )
                mark_active(uid)
            except Exception as e:
                if "Forbidden" in str(e) or "blocked" in str(e).lower():
                    mark_blocked(uid)
                    logging.info(f"User {uid} blocked the bot")
                else:
                    logging.error(f"Failed to send to {uid}: {e}")

# ─── MAIN ─────────────────────────────────────────────────
def main():
    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_CHIPS: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_chips)],
            ASK_CANCEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_cancel_choice)],
            ASK_CONFIRM_CANCEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_confirm_cancel)],
        },
        fallbacks=[
            CommandHandler("time", time_command),
            CommandHandler("check", check_command),
        ]
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("list", list_users))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("time", time_command))
    app.add_handler(CommandHandler("check", check_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons))

    app.job_queue.run_repeating(auto_reminder, interval=3600, first=10)

    print("Бот запущен...")
    app.run_polling()

if __name__ == "__main__":
    main()
