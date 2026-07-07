import logging
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler

TOKEN = "8438330820:AAHjMB_PUT_LVXCO54NlEYQErMy0LXW2NqA"
ADMIN_CHAT_ID = 420348563
SHEET_ID = "14bULAhTGj548t65wIZnjKOBVAXuHoIA8pK2CYiWho9s"

CREDS_INFO = {
  "type": "service_account",
  "project_id": "eqlbm-bot",
  "private_key_id": "9bddfbfab0eeb9c6a604028902db244fc8d1ac53",
  "private_key": "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC2WCjKFEEBWC9Y\n8DIg1n7uZMDFBRAUUopSLMJrJlVVSu1VIINzrZQ3f3iWI3yd/sYlw3bp26XlfvUg\n8OV2VHA1a1CMb7s01g4lwbnWqjtSDzSaxjYhn9ZK93FmVa328Znroq/tSt0c3zXQ\ns9QbxR+z/Zgw4e9qyABVvM/3xaf0UE2F2vWpStZBft89H3FB6J1kCSKBLUkhtQHL\nWj+p5QDCe075j5vuzOdKT4WNZmgFhfNZXKCaC0ydK0/B5xA6KINsyEU683OusTIh\njhiQ+0epiPLQ3hbZZberRI/7dScvqgpU+FlEO9D8tyyGgW1lyEmIyVNIP7NC9VnV\nKTutAU37AgMBAAECggEAA1QdUoBc0MB3r3ZFrqcbuWWqGy6aJN9+L5J/mLh3avor\ngO1PWPznLWgsnwOrfQWElSCPUCThT2IqPrKxuEpBo3KqYsCQuJ3bs5a+D2Fwzxur\nSH6ryEtZpxQDREH2I1bftMHLLQK71ztqiRJvTHPPE1hivymwrCYAfjrXH7Js/B8J\n7Qvje1PCe1yS5G61F8q/Y+HX3AvUiy2/4FN7CSdrUhiUTQXa9AuDYVRVJaz98nFX\nEVMv7mB/OCXeFHtPUn6vfuObO1J5JK/YREYxTfvylYHVYR6Q+sO1dtQmtaAKUTCt\nl+7UCLxDZlvePh85ZmppvUYZ9pQzmT2cOq/qsLECGQKBgQDbDIUWHc2NAb35TqWm\nxeJFhBab/OLmcPtDog7JSImjaTDuZeQkNXhG1ybQ0wn7BNTanpEkbIp5f3BnkGz4\np1FhJhf/hCYWs9JacS6Ny3oEj500C2i0uk4+hQbYQerkKhK6/7jGJIkQS3laF4Qh\nEaSLY6bT85fCFWU8e6xtghe6fQKBgQDVGpQsUfJOgF2gBxk92/8ZnMbbIv4ms1+S\nNNhUyoqN4oyOmQ6xBcRO9FN5BhUtbUO2FPi5ZmdNzolI1zvK3YUtx6EyL2lU4rFF\n8C+OcVqfckeR6ljm3Ykfce6kKDmZ2fBNtE+he6Yre0JmqE83ActnedYf47T1E+k/\nKs6cP/Kb1wKBgQC+7ECrsDpS5uvQes5DeELqWGDkgRy7wkoe/wdoRYNCHRN7FvAs\n5zX4eNrqNKeEVQe5rW/QkZJ4p60vd2CjsiJqTKuqGGKicwWrsu7ixDGL/CkHDdKr\ng59jOstmfr3fNRSyTOWePoYA3+fbsJeHwzrqC2eDYdQqZD+i4iC+Kh/IeQKBgGYq\nw/cronu4VyqtvJBHtNnWrA/LiwWK4br60uxz3lF/19tVzhFYrnEb+hj/rY+F3vyg\nuU5JpiVLa84cQnJUGdGE7+dbi6hCtrLNID+uYMAozd9K9yxX8bG9safKETONpQPb\n+oF1Aom+ImuNLc01cws9AkdvqAYHcb/zCfMnRW0pAoGALfkDDDKPFMKlgqI8exrR\n8Wv0K1MvZoBgCXL1mdnHi0dq2Eh667O7HvMIdjwWx/l2CX/cmeoW+MPEqvyDisZR\n1WIiRzWFL9SQH7VXnBuiUUTW5tTcvAriIsJto1Z2EolExWbXbiH1SK+EoYit35qM\nastaPaD7ekJB1R9ePD44KN0=\n-----END PRIVATE KEY-----\n",
  "client_email": "eqlbm-sheets@eqlbm-bot.iam.gserviceaccount.com",
  "client_id": "118330609624712041689",
  "auth_uri": "https://accounts.google.com/o/oauth2/auth",
  "token_uri": "https://oauth2.googleapis.com/token",
  "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
  "client_x509_cert_url": "https://www.googleapis.com/robot/v1/metadata/x509/eqlbm-sheets%40eqlbm-bot.iam.gserviceaccount.com",
  "universe_domain": "googleapis.com"
}

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
    print("Бот запущен...")
    app.run_polling()

if __name__ == "__main__":
    main()