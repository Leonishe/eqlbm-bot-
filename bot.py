"""Бот оплаты подписки EQLBM.CLUB.

Сценарий:
  ссылка с сайта  →  счёт с суммой и адресом  →  игрок платит USDT (TRC20)
  →  присылает TXID  →  бот сверяет перевод в блокчейне  →  доступ открыт.
"""

import logging
import secrets
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

import config
import discord_invites
import storage
import tron

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("eqlbm-pay")

MONTHS = (1, 3, 6, 12)


# ---------------------------------------------------------------- вспомогательное
def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


def money(value) -> str:
    return f"${int(value):,}".replace(",", " ")


def tiers_kb(locked=None) -> InlineKeyboardMarkup:
    rows = []
    for t in config.TIERS:
        price = config.monthly_price(t, locked)
        rows.append([InlineKeyboardButton(
            f"{config.tier_title(t)} · {money(price)}/мес", callback_data=f"t:{t}")])
    return InlineKeyboardMarkup(rows)


def months_kb(tier: str, locked=None) -> InlineKeyboardMarkup:
    rows, row = [], []
    for m in MONTHS:
        off = config.DISCOUNTS[m]
        total = config.invoice_amount(tier, m, locked)
        label = f"{m} мес · {money(total)}" + (f" −{off}%" if off else "")
        row.append(InlineKeyboardButton(label, callback_data=f"m:{tier}:{m}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("← Тарифы", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


async def locked_price_of(user_id: int) -> Decimal | None:
    sub = await storage.get_sub(user_id)
    if sub and sub.get("locked_price"):
        try:
            return Decimal(str(sub["locked_price"]))
        except Exception:
            return None
    return None


async def send_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE,
                       tier: str, months: int) -> None:
    user = update.effective_user
    locked = await locked_price_of(user.id)
    amount = config.invoice_amount(tier, months, locked)

    context.user_data["invoice"] = {"tier": tier, "months": months, "amount": str(amount)}
    try:
        await storage.save_invoice(user.id, tier, months, amount)
    except Exception:
        log.exception("счёт не записался в таблицу — останется только в памяти")

    per_month = (amount / months).quantize(Decimal("1"))
    per_line = f" ({money(per_month)} в месяц)" if months > 1 else ""

    if config.is_first_intake(tier, locked):
        full = config.standard_price(tier) * months
        was = f"<s>{money(full)}</s> "
        note = "\nЦена первого набора и остаётся твоей при всех продлениях.\n"
    else:
        was, note = "", ""

    text = (
        f"<b>{config.tier_title(tier)} · {months} мес</b>\n"
        f"К оплате: {was}<b>{amount} USDT</b>{per_line}\n"
        f"{note}\n"
        "Сеть — <b>TRC20 (TRON)</b>. Адрес:\n"
        f"<code>{config.USDT_ADDRESS}</code>\n\n"
        "Переведи ровно эту сумму, затем пришли сюда <b>хеш транзакции (TXID)</b> — "
        "я проверю его в блокчейне и открою доступ.\n\n"
        "TXID — это 64 символа, кошелёк показывает его после отправки."
    )
    target = update.callback_query.message if update.callback_query else update.message
    await target.reply_html(text, disable_web_page_preview=True)


# ---------------------------------------------------------------- команды
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    user = update.effective_user

    # приглашение от админа: aff_<код> — доступ в чат без оплаты
    if args and args[0].startswith("aff_"):
        code = args[0][4:]
        inv = await storage.get_invite(code)
        if not inv:
            await update.message.reply_text("Ссылка не найдена. Попроси новую у @leonishe")
            return
        if str(inv.get("used_by", "")).strip():
            await update.message.reply_text("Эта ссылка уже использована.")
            return
        await storage.set_affiliate(user.id, user.username or "", user.full_name)
        await storage.use_invite(code, user.id)
        link = await discord_invites.personal_invite()
        invite = f"\n\nDiscord (ссылка личная, на сутки): {link}" if link else ""
        await update.message.reply_html(
            "Готово, ты в списке комьюнити по аффилейт-программе. "
            f"Платить ничего не нужно.{invite}\n\n"
            "Если захочешь подписку с тренировками — /start и выбери тариф.",
            disable_web_page_preview=True)
        for admin in config.ADMIN_IDS:
            try:
                await context.bot.send_message(
                    admin, f"По ссылке зашёл: {user.full_name} (@{user.username or '—'})")
            except Exception:
                pass
        return

    # если админ завёл строку заранее по нику — привязываем Telegram ID
    pending = await storage.pending_by_username(user.username or "")
    if pending:
        pending["user_id"] = user.id
        pending["name"] = user.full_name
        await storage.save_sub(pending)

    if args and args[0].startswith("pay_"):
        parts = args[0].split("_")
        if len(parts) == 3 and parts[1] in config.TIERS and parts[2].isdigit():
            months = int(parts[2])
            if months in config.DISCOUNTS:
                await send_invoice(update, context, parts[1], months)
                return

    await update.message.reply_html(
        "Это бот оплаты <b>EQLBM.CLUB</b>.\n\n"
        "Выбери тариф — покажу сумму и адрес. Оплата в USDT, доступ открываю в тот же день.\n\n"
        "/status — состояние подписки",
        reply_markup=tiers_kb(),
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sub = await storage.get_sub(update.effective_user.id)
    if sub and sub.get("status") == "affiliate":
        await update.message.reply_html(
            "У тебя доступ в чат комьюнити по аффилейт-программе — подписки нет "
            "и платить за него не нужно.\n\n"
            "Тренировки, разборы и материалы идут по подписке:",
            reply_markup=tiers_kb())
        return

    exp = storage.parse_date(sub.get("expires_at")) if sub else None
    if not sub or not exp:
        await update.message.reply_html(
            "Активной подписки нет.", reply_markup=tiers_kb())
        return

    left = (exp - date.today()).days
    if left < 0:
        await update.message.reply_html(
            f"Подписка <b>{config.tier_title(sub['tier'])}</b> закончилась "
            f"{exp.strftime('%d.%m.%Y')}.",
            reply_markup=tiers_kb())
        return

    await update.message.reply_html(
        f"Тариф: <b>{config.tier_title(sub['tier'])}</b>\n"
        f"Оплачено до: <b>{exp.strftime('%d.%m.%Y')}</b> (осталось {left} дн.)"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_html(
        "/start — выбрать тариф\n"
        "/status — сколько осталось\n\n"
        "Оплата: USDT в сети TRC20. После перевода пришли TXID — проверю сам.\n"
        "Вопросы: @leonishe"
    )


# ---------------------------------------------------------------- кнопки
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    data = q.data

    locked = await locked_price_of(update.effective_user.id)

    if data == "menu":
        await q.edit_message_text("Выбери тариф:", reply_markup=tiers_kb(locked))
        return

    if data.startswith("t:"):
        tier = data.split(":")[1]
        price = config.monthly_price(tier, locked)
        if config.is_first_intake(tier, locked):
            head = (f"<b>{config.tier_title(tier)}</b> — "
                    f"<s>{money(config.standard_price(tier))}</s> "
                    f"<b>{money(price)}</b> в месяц\n"
                    "Цена первого набора, до 9 сентября.")
        else:
            head = f"<b>{config.tier_title(tier)}</b> — {money(price)} в месяц."
        await q.edit_message_text(
            head + "\n\nНа какой срок берёшь? Чем длиннее, тем дешевле месяц.",
            parse_mode=ParseMode.HTML, reply_markup=months_kb(tier, locked))
        return

    if data.startswith("m:"):
        _, tier, months = data.split(":")
        await send_invoice(update, context, tier, int(months))


# ---------------------------------------------------------------- проверка TXID
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    if not tron.looks_like_txid(text):
        await update.message.reply_html(
            "Это не похоже на TXID. Нужен хеш транзакции — 64 символа из цифр и латинских букв.\n"
            "Если ещё не выбрал тариф — /start",
            reply_markup=tiers_kb())
        return

    invoice = context.user_data.get("invoice")
    if not invoice:
        # бот мог перезапуститься, пока игрок ходил переводить — берём из таблицы
        try:
            invoice = await storage.get_invoice(update.effective_user.id)
            if invoice:
                context.user_data["invoice"] = invoice
        except Exception:
            log.exception("не удалось прочитать счёт из таблицы")
    if not invoice:
        await update.message.reply_html(
            "Сначала выбери тариф, чтобы я знал, какую сумму проверять.",
            reply_markup=tiers_kb())
        return

    txid = tron.normalize_txid(text)
    note = await update.message.reply_text("Проверяю транзакцию…")

    if await storage.txid_used(txid):
        await note.edit_text("Эта транзакция уже засчитана. Если это ошибка — напиши @leonishe")
        return

    try:
        transfer = await tron.find_transfer(txid)
    except Exception as e:
        log.exception("tron lookup failed")
        await note.edit_text("Сеть не отвечает, попробуй ещё раз через минуту.")
        return

    if transfer is None:
        await note.edit_text(
            "Не нашёл такой перевод на мой кошелёк.\n\n"
            "Проверь: сеть TRC20, адрес совпадает, транзакция подтверждена. "
            "Иногда нужно подождать пару минут — потом пришли TXID снова."
        )
        return

    expected = Decimal(invoice["amount"])
    ok, reason = tron.check(transfer, expected)
    if not ok:
        if reason == "amount_low":
            await note.edit_text(
                f"Пришло {transfer.amount} USDT, а по счёту {expected} USDT. "
                "Допереведи разницу и пришли новый TXID, либо напиши @leonishe")
        else:
            await note.edit_text(
                "Транзакция слишком старая для этого счёта. Напиши @leonishe, разберёмся вручную.")
        return

    expires = await storage.activate(
        update.effective_user, invoice["tier"], int(invoice["months"]),
        transfer.amount, config.monthly_price(invoice["tier"], await locked_price_of(update.effective_user.id)),
    )
    await storage.add_payment(txid, update.effective_user.id,
                              invoice["tier"], int(invoice["months"]), transfer.amount)
    context.user_data.pop("invoice", None)
    try:
        await storage.close_invoice(update.effective_user.id)
    except Exception:
        pass

    link = await discord_invites.personal_invite()
    invite = f"\n\nЗаходи в Discord (ссылка личная, на сутки): {link}" if link else ""
    await note.edit_text(
        f"Оплата принята. Тариф {config.tier_title(invoice['tier'])} "
        f"до {expires.strftime('%d.%m.%Y')}.{invite}",
        disable_web_page_preview=True,
    )

    for admin in config.ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin,
                f"Оплата: {update.effective_user.full_name} "
                f"(@{update.effective_user.username or '—'}) — "
                f"{config.tier_title(invoice['tier'])}, {invoice['months']} мес, "
                f"{transfer.amount} USDT, до {expires.strftime('%d.%m.%Y')}")
        except Exception:
            pass


# ---------------------------------------------------------------- напоминания
async def job_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    today = date.today()
    for sub in await storage.all_subs():
        if sub.get("status") == "affiliate":
            continue
        exp = storage.parse_date(sub.get("expires_at"))
        if not exp or not str(sub.get("user_id", "")).strip().isdigit():
            continue
        user_id = int(sub["user_id"])
        left = (exp - today).days

        if left in config.REMIND_DAYS:
            word = "день" if left == 1 else "дня"
            try:
                await context.bot.send_message(
                    user_id,
                    f"Подписка {config.tier_title(sub['tier'])} заканчивается через {left} {word}, "
                    f"{exp.strftime('%d.%m')}. Продлить — /start",
                )
            except Exception:
                pass

        elif left == 0:
            try:
                await context.bot.send_message(
                    user_id,
                    "Подписка закончилась сегодня. Продлить — /start")
            except Exception:
                pass
            if sub.get("status") != "expired":
                sub["status"] = "expired"
                await storage.save_sub(sub)
            for admin in config.ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        admin,
                        f"Истекла подписка: {sub.get('name')} (@{sub.get('username') or '—'}), "
                        f"{config.tier_title(sub['tier'])}. Пора убирать с сервера.")
                except Exception:
                    pass


# ---------------------------------------------------------------- админ
async def cmd_subs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    rows = await storage.all_subs()
    if not rows:
        await update.message.reply_text("Подписок нет.")
        return
    today = date.today()
    paid, free = [], []
    for r in sorted(rows, key=lambda x: str(x.get("expires_at"))):
        who = f"{r.get('name') or '—'} (@{r.get('username') or '—'})"
        if r.get("status") == "affiliate":
            bound = "" if str(r.get("user_id", "")).strip() else "  · ещё не запускал бота"
            free.append(f"{who}{bound}")
            continue
        exp = storage.parse_date(r.get("expires_at"))
        left = (exp - today).days if exp else "—"
        tier = config.tier_title(r["tier"]) if r.get("tier") in config.TIERS else "—"
        paid.append(f"{who} — {tier}, до {exp} ({left} дн.)")

    out = []
    if paid:
        out.append("ПОДПИСКИ\n" + "\n".join(paid[:40]))
    if free:
        out.append("АФФИЛЕЙТ (без оплаты)\n" + "\n".join(free[:40]))
    await update.message.reply_text("\n\n".join(out) or "Пусто.")


async def cmd_paid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ручная выдача подписки, когда игрок оплатил мимо бота.

    /paid @ник pro 3 перевод в PokerOK
    /paid 123456789 base 1 CoinPoker
    """
    if not is_admin(update.effective_user.id):
        return

    args = context.args or []
    if len(args) < 3:
        await update.message.reply_html(
            "Формат: <code>/paid @ник тариф месяцев [способ оплаты]</code>\n"
            "Например: <code>/paid @petrov pro 3 перевод в PokerOK</code>")
        return

    who, tier, months_raw = args[0], args[1].lower(), args[2]
    method = " ".join(args[3:]).strip()
    if tier not in config.TIERS or not months_raw.isdigit():
        await update.message.reply_html(
            "Тариф — base, pro или vip. Срок — число месяцев.\n"
            "Например: <code>/paid @petrov pro 3 перевод в PokerOK</code>")
        return

    months = int(months_raw)
    user_id = who if who.isdigit() else ""
    username = "" if who.isdigit() else who.lstrip("@")
    locked = None
    if user_id:
        locked = await locked_price_of(int(user_id))

    amount = config.invoice_amount(tier, months, locked)
    note = f"оплата вручную: {method}" if method else "оплата вручную"

    expires = await storage.activate_row(
        user_id=user_id, username=username, name="", tier=tier, months=months,
        locked_price=config.monthly_price(tier, locked), note=note)

    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    await storage.add_payment(f"manual-{stamp}", int(user_id) if user_id else 0,
                              tier, months, amount)

    label = f"@{username}" if username else user_id
    await update.message.reply_html(
        f"{label} — <b>{config.tier_title(tier)}</b> до "
        f"<b>{expires.strftime('%d.%m.%Y')}</b>.\n"
        f"В журнал записано {amount} USDT, {note}."
        + ("" if user_id else "\n\nID пока нет — попроси его запустить бота, "
                              "я привяжу строку по нику автоматически."))

    if user_id:
        link = await discord_invites.personal_invite()
        invite = f"\n\nDiscord (ссылка личная, на сутки): {link}" if link else ""
        try:
            await context.bot.send_message(
                int(user_id),
                f"Оплата принята. Тариф {config.tier_title(tier)} "
                f"до {expires.strftime('%d.%m.%Y')}.{invite}",
                disable_web_page_preview=True)
        except Exception:
            pass

async def cmd_invite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/invite — одноразовая ссылка на бесплатный доступ по аффилейт-программе."""
    if not is_admin(update.effective_user.id):
        return
    code = secrets.token_urlsafe(8).replace("-", "").replace("_", "")[:10]
    await storage.create_invite(code, "affiliate")
    me = await context.bot.get_me()
    await update.message.reply_html(
        "Ссылка на бесплатный доступ (одноразовая):\n"
        f"<code>https://t.me/{me.username}?start=aff_{code}</code>\n\n"
        "Отправь её игроку — он нажмёт, и я запишу его в комьюнити без оплаты."
    )


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/add @ник [заметка] — завести человека заранее, до того как он напишет боту."""
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Формат: /add @ник [заметка]")
        return
    username = context.args[0].lstrip("@")
    note = " ".join(context.args[1:]) or "аффилейт"
    await storage.set_affiliate(0, username=username, name="", note=note)
    me = await context.bot.get_me()
    await update.message.reply_html(
        f"@{username} записан как аффилейт. Попроси его запустить бота: "
        f"https://t.me/{me.username} — я привяжу его к этой строке автоматически.",
        disable_web_page_preview=True)


async def cmd_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/link — выпустить приглашение в Discord руками."""
    if not is_admin(update.effective_user.id):
        return
    link = await discord_invites.personal_invite()
    await update.message.reply_text(
        link or "Приглашение не выпустилось: проверь DISCORD_BOT_TOKEN и DISCORD_CHANNEL_ID.",
        disable_web_page_preview=True)


def main() -> None:
    app = Application.builder().token(config.BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("subs", cmd_subs))
    app.add_handler(CommandHandler("paid", cmd_paid))
    app.add_handler(CommandHandler("grant", cmd_paid))
    app.add_handler(CommandHandler("invite", cmd_invite))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("link", cmd_link))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    tz = ZoneInfo(config.TZ)
    app.job_queue.run_daily(
        job_reminders,
        time=datetime.now(tz).replace(hour=config.REMIND_HOUR, minute=0, second=0).timetz(),
    )

    log.info("bot started")
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
