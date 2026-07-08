import logging
import os
import json
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler

TOKEN = os.environ["TELEGRAM_TOKEN"]
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])
SHEET_ID = os.environ["SHEET_ID"]
CREDS_INFO = json.loads(os.environ["GOOGLE_CREDS"])

logging.basicConfig(level=logging.INFO)

ASK_CHIPS, ASK_CANCEL = range(2)

def get_sheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(CREDS_INFO, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID).sheet1

def is_registered(user_id):
    try:
        sheet = get_sheet()
        ids = sheet.col_values(1)
        return str(user_id) in ids
    except:
        return False

def save_user(user_id, username, name):
    try:
        sheet = get_sheet()
        sheet.append_row([
            str(user_id),
            username,
            name,
            datetime.now().strftime("%Y-%m-%d %H:%M")
        ])
    except Exception as e:
        logging.error(f"Sheet error: {e}")

def remove_user(user_id):
    try:
        sheet = get_sheet()
        ids = sheet.col_values(1)
        if str(user_id) in ids:
            row = ids.index(str(user_id)) + 1
            sheet.delete_rows(row)
    except Exception as e:
        logging.error(f"Sheet error: {e}")

def get_all_user_ids():
    try:
        sheet = get_sheet()
        rows = sheet.get_all_values()[1:]
        return [r[0] for r in rows if r[0]]
    except Exception as e:
        logging.error(f"Sheet error: {e}")
        return []

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = f"@{user.username}" if user.username else "без username"
    name = user.full_name or "—"

    if is_registered(user.id):
        keyboard = [["ЗАКОНЧИТЬ", "ОТМЕНИТЬ РЕГИСТРАЦИЮ"]]
        await update.message.reply_text(
            "Вы уже зарегистрированы.",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
        )
        return ASK_CANCEL

    save_user(user.id, username, name)

    await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=f"🟢 Новая регистрация\nИмя: {name}\nUsername: {username}\nID: {user.id}"
    )

    keyboard = [["УЗНАТЬ", "ЗАКОНЧИТЬ"]]
    await update.message.reply_text(
        "Ваша регистрация принята.\n\nХотите узнать сколько фишек вы получите?",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
    )
    return ASK_CHIPS

async def handle_chips(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "УЗНАТЬ":
        await update.message.reply_text(
            "🂡 *EARLY BIRD* даёт тебе 50% дисконт на любой уровень подписки и фиксирует эту стоимость навсегда. Обо всех уровнях подписки ты узнаешь позже, когда будут готовы анонсы.\n\n"
            "👑 *OG* даёт возможность участвовать во всех турнирах, встречах комьюнити, оффлайн семинарах и конференциях совершенно бесплатно.\n\n"
            "_(не включает дополнительные расходы каждого участника)_",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove()
        )

    await update.message.reply_text(
        "Дополнительная информация будет поступать тебе через этого бота. Не удаляй диалог.",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END

async def handle_cancel_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "ОТМЕНИТЬ РЕГИСТРАЦИЮ":
        remove_user(update.effective_user.id)
        user = update.effective_user
        username = f"@{user.username}" if user.username else "без username"
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"🔴 Отмена регистрации\nUsername: {username}\nID: {user.id}"
        )
        await update.message.reply_text(
            "Ваша регистрация отменена.",
            reply_markup=ReplyKeyboardRemove()
        )
    else:
        await update.message.reply_text(
            "Дополнительная информация будет поступать тебе через этого бота. Не удаляй диалог.",
            reply_markup=ReplyKeyboardRemove()
        )
    return ConversationHandler.END

async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    try:
        sheet = get_sheet()
        rows = sheet.get_all_values()[1:]
        if not rows:
            await update.message.reply_text("Список пуст.")
            return
        lines = [f"{i+1}. {r[2]} {r[1]} — {r[3]}" for i, r in enumerate(rows)]
        await update.message.reply_text(f"📋 Зарегистрированных: {len(rows)}\n\n" + "\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

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
        await update.message.reply_text("Список пуст — некому отправлять.")
        return

    sent = 0
    failed = 0
    for uid in user_ids:
        try:
            await context.bot.send_message(chat_id=int(uid), text=text)
            sent += 1
        except Exception as e:
            logging.error(f"Failed to send to {uid}: {e}")
            failed += 1

    await update.message.reply_text(
        f"✅ Рассылка завершена\nОтправлено: {sent}\nНе доставлено: {failed}"
    )

def main():
    app = Application.builder().token(TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_CHIPS: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_chips)],
            ASK_CANCEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_cancel_choice)],
        },
        fallbacks=[]
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("list", list_users))
    app.add_handler(CommandHandler("broadcast", broadcast))
    print("Бот запущен...")
    app.run_polling()

if __name__ == "__main__":
    main()
