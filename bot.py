"""
EQLBM.CLUB — Telegram bot
Early Bird регистрация + Google Sheets как хранилище.

Архитектура:
  - Данные живут в памяти (UserStore); Sheets — персистентный слой.
  - Активность батчится и пишется раз в минуту (экономим квоту Google).
  - Регистрация / удаление пишутся немедленно.
  - Служебные флаги — на отдельном листе `_meta`, основная таблица не задета.
  - Интерфейс — инлайн-кнопки, без состояний и ConversationHandler.
"""

import asyncio
import html
import json
import logging
import os
import re
import secrets
from collections import Counter
from datetime import datetime, timezone, timedelta

import gspread
from gspread.exceptions import WorksheetNotFound
from google.oauth2.service_account import Credentials
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeAllPrivateChats,
)
from telegram.constants import ParseMode
from telegram.error import Forbidden, BadRequest, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ─── КОНФИГ ───────────────────────────────────────────────────────────────
TOKEN = os.environ["TELEGRAM_TOKEN"]
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])
SHEET_ID = os.environ["SHEET_ID"]
CREDS_INFO = json.loads(os.environ["GOOGLE_CREDS"])

TZ = timezone(timedelta(hours=3))                       # Минск, UTC+3
LAUNCH_DATE = datetime(2026, 9, 9, 0, 0, tzinfo=TZ)     # полночь по Минску

# Колонки: A=ID B=Username C=Name D=RegDate E=Activity F=Status G=Source
COL_ID, COL_USERNAME, COL_NAME, COL_REGDATE, COL_ACTIVITY, COL_STATUS, COL_SOURCE = range(7)
HEADER = ["ID", "Username", "Имя", "Дата регистрации", "Активность", "Статус", "Источник"]
NCOLS = len(HEADER)

META_SHEET = "_meta"          # служебный лист: флаги рассылок
REMINDER_KEYS = ("7d", "1d", "launch")

FLUSH_INTERVAL = 60           # сек между сбросами активности в Sheets
BROADCAST_DELAY = 0.06        # ~16 msg/s при лимите Telegram ~30
BROADCAST_TTL = 600           # сек жизни неподтверждённого черновика
TG_MSG_LIMIT = 3800           # запас до 4096
FORWARD_MESSAGES = True       # пересылать вопросы участников админу
FORWARD_COOLDOWN = 30         # сек между пересылками от одного человека
ERROR_COOLDOWN = 60           # сек между уведомлениями об ошибках

# Читаемые названия источников из deep-link (t.me/BOT?start=<payload>)
SOURCES = {
    "early": "сайт eqlbm.club",
    "site": "сайт eqlbm.club",
    "ig": "Instagram",
    "tg": "Telegram-канал",
}

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("eqlbm")


# ─── УТИЛИТЫ ──────────────────────────────────────────────────────────────
def esc(text) -> str:
    """Экранирование пользовательских данных для parse_mode=HTML."""
    return html.escape(str(text or ""), quote=False)


def now_local() -> datetime:
    return datetime.now(TZ)


def now_str() -> str:
    return now_local().strftime("%Y-%m-%d %H:%M")


def plural(n: int, one: str, few: str, many: str) -> str:
    m = abs(n) % 100
    if 11 <= m <= 14:
        return many
    m %= 10
    if m == 1:
        return one
    if 2 <= m <= 4:
        return few
    return many


def _col_letter(n: int) -> str:
    """1 -> A, 7 -> G, 27 -> AA."""
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def _pad(row, size=NCOLS):
    row = list(row)
    return row + [""] * (size - len(row))


# ─── ХРАНИЛИЩЕ ────────────────────────────────────────────────────────────
class UserStore:
    def __init__(self):
        self._book = None
        self._sheet = None
        self._meta = None
        self._users: dict[str, dict] = {}
        self._flags: dict[str, str] | None = None   # None = ещё не читали
        self._pending: dict[str, dict] = {}
        self._loaded = False
        self._lock = asyncio.Lock()

    # -- сеть ------------------------------------------------------------
    def _connect(self):
        if self._book is None:
            scopes = ["https://www.googleapis.com/auth/spreadsheets"]
            creds = Credentials.from_service_account_info(CREDS_INFO, scopes=scopes)
            self._book = gspread.authorize(creds).open_by_key(SHEET_ID)
            self._sheet = self._book.sheet1
        return self._sheet

    def _meta_sheet(self):
        if self._meta is None:
            self._connect()
            try:
                self._meta = self._book.worksheet(META_SHEET)
            except WorksheetNotFound:
                self._meta = self._book.add_worksheet(META_SHEET, rows=20, cols=2)
                self._meta.update(values=[["key", "value"]], range_name="A1:B1")
                log.info("Создан служебный лист %s", META_SHEET)
        return self._meta

    def _reload_sync(self):
        sheet = self._connect()
        values = sheet.get_all_values()

        if not values:
            sheet.append_row(HEADER, table_range="A1", insert_data_option="INSERT_ROWS")
            values = [HEADER]
        elif _pad(values[0])[:NCOLS] != HEADER:
            # Миграция схемы: дописываем/чиним заголовок.
            # ВАЖНО: в gspread 6 сигнатура update(values, range_name).
            sheet.update(values=[HEADER], range_name=f"A1:{_col_letter(NCOLS)}1")
            log.info("Заголовок приведён к %d колонкам", NCOLS)

        users = {}
        for idx, raw in enumerate(values[1:], start=2):
            r = _pad(raw)
            uid = r[COL_ID].strip()
            if not uid:
                continue
            users[uid] = {
                "row": idx,
                "username": r[COL_USERNAME],
                "name": r[COL_NAME],
                "reg": r[COL_REGDATE],
                "activity": r[COL_ACTIVITY],
                "status": r[COL_STATUS] or "active",
                "source": r[COL_SOURCE] or "—",
            }
        self._users = users

        # Флаги читаем СТРОГО: ошибка -> исключение -> retry в load().
        # Обнулить их значит разослать напоминания повторно всем.
        flags = {}
        for row in self._meta_sheet().get_all_values()[1:]:
            if row and row[0]:
                flags[row[0]] = row[1] if len(row) > 1 else ""
        self._flags = flags

        self._loaded = True
        log.info("Sheets загружены: %d участников, флаги: %s", len(users), flags or "нет")

    async def load(self, force=False, retries=4):
        for attempt in range(1, retries + 1):
            try:
                async with self._lock:          # лок держим только на само чтение
                    if self._loaded and not force:
                        return
                    await asyncio.to_thread(self._reload_sync)
                return
            except Exception as e:
                if attempt == retries:
                    log.error("Загрузка Sheets провалена окончательно: %s", e)
                    raise
                wait = 2 ** attempt
                log.error("Загрузка Sheets не удалась (%d/%d): %s. Жду %ds",
                          attempt, retries, e, wait)
                await asyncio.sleep(wait)       # спим БЕЗ лока

    @property
    def ready(self) -> bool:
        return self._loaded and self._flags is not None

    # -- чтение из памяти -------------------------------------------------
    def is_registered(self, uid) -> bool:
        return str(uid) in self._users

    def get(self, uid) -> dict | None:
        return self._users.get(str(uid))

    def all_ids(self) -> list[str]:
        return list(self._users.keys())

    def reachable_ids(self) -> list[str]:
        return [u for u, d in self._users.items() if d["status"] != "blocked"]

    def all_users(self) -> list[tuple[str, dict]]:
        return sorted(self._users.items(), key=lambda kv: kv[1]["row"])

    def count(self) -> int:
        return len(self._users)

    # -- запись ------------------------------------------------------------
    async def add(self, uid, username, name, source="—") -> bool:
        """True — добавлен, False — уже был (защита от даблклика и повторов)."""
        if not self._loaded:
            raise RuntimeError("Store не загружен — запись запрещена")
        uid = str(uid)
        async with self._lock:
            if uid in self._users:
                return False
            ts = now_str()
            payload = [uid, username, name, ts, ts, "active", source]

            def _append():
                sheet = self._connect()
                # table_range="A1": считаем таблицей только блок от A1.
                resp = sheet.append_row(
                    payload, value_input_option="RAW",
                    table_range="A1", insert_data_option="INSERT_ROWS",
                )
                rng = resp.get("updates", {}).get("updatedRange", "")
                m = re.search(r"![A-Z]+(\d+)", rng)
                return int(m.group(1)) if m else len(sheet.col_values(1))

            row_num = await asyncio.to_thread(_append)
            self._users[uid] = {
                "row": row_num, "username": username, "name": name,
                "reg": ts, "activity": ts, "status": "active", "source": source,
            }
            log.info("Регистрация %s (строка %d, источник %s), всего %d",
                     uid, row_num, source, len(self._users))
            return True

    async def remove(self, uid) -> bool:
        uid = str(uid)
        async with self._lock:
            user = self._users.get(uid)
            if not user:
                return False
            row = user["row"]
            await asyncio.to_thread(lambda: self._connect().delete_rows(row))
            self._pending.pop(uid, None)
            del self._users[uid]
            for u in self._users.values():   # строки ниже сдвинулись вверх
                if u["row"] > row:
                    u["row"] -= 1
            log.info("Удалён %s, осталось %d", uid, len(self._users))
            return True

    def touch(self, uid):
        uid = str(uid)
        if uid not in self._users:
            return
        ts = now_str()
        self._users[uid]["activity"] = ts
        self._pending.setdefault(uid, {})["activity"] = ts

    def set_status(self, uid, status: str):
        uid = str(uid)
        u = self._users.get(uid)
        if not u or u["status"] == status:
            return
        u["status"] = status
        self._pending.setdefault(uid, {})["status"] = status

    async def flush(self) -> int:
        async with self._lock:
            if not self._pending:
                return 0
            updates = []
            for uid, changes in self._pending.items():
                u = self._users.get(uid)
                if not u:
                    continue
                if "activity" in changes:
                    updates.append({"range": f"E{u['row']}", "values": [[changes["activity"]]]})
                if "status" in changes:
                    updates.append({"range": f"F{u['row']}", "values": [[changes["status"]]]})
            count = len(self._pending)
            self._pending.clear()

        if not updates:
            return 0
        try:
            await asyncio.to_thread(
                lambda: self._connect().batch_update(updates, value_input_option="RAW")
            )
            log.info("Flush: %d записей", count)
        except Exception as e:
            log.error("Ошибка флаша: %s", e)
        return count

    # -- флаги (лист _meta) -------------------------------------------------
    def flag_is_set(self, key) -> bool:
        if self._flags is None:
            return True    # флаги не прочитаны -> молчим, а не спамим
        return bool(self._flags.get(key))

    async def set_flag(self, key):
        if self._flags is None:
            self._flags = {}
        self._flags[key] = f"sent {now_str()}"

        def _write():
            meta = self._meta_sheet()
            keys = meta.col_values(1)
            if key in keys:
                meta.update_cell(keys.index(key) + 1, 2, self._flags[key])
            else:
                meta.append_row([key, self._flags[key]],
                                table_range="A1", insert_data_option="INSERT_ROWS")

        try:
            await asyncio.to_thread(_write)
        except Exception as e:
            log.error("Флаг %s не записан: %s", key, e)


store = UserStore()
_send_lock = asyncio.Lock()     # одна массовая рассылка за раз (лимит Telegram)
_kb_cleaned: set[int] = set()   # у кого уже убрали legacy reply-клавиатуру
_last_error_notify = datetime.min.replace(tzinfo=timezone.utc)


# ─── ТЕКСТЫ ───────────────────────────────────────────────────────────────
def countdown_line() -> str:
    delta = LAUNCH_DATE - now_local()
    if delta.total_seconds() <= 0:
        return "клуб уже открыт"
    # Округляем до минуты, иначе 1д 2ч 0м 0.1с покажется как «1 день и 1 час».
    total_min = round(delta.total_seconds() / 60)
    d, rem = divmod(total_min, 1440)
    h, m = divmod(rem, 60)
    if d > 0:
        days = f"{d} {plural(d, 'день', 'дня', 'дней')}"
        return days if h == 0 else f"{days} и {h} {plural(h, 'час', 'часа', 'часов')}"
    if h == 0:
        return f"{m} {plural(m, 'минута', 'минуты', 'минут')}"
    hours = f"{h} {plural(h, 'час', 'часа', 'часов')}"
    return hours if m == 0 else f"{hours} {m} {plural(m, 'минута', 'минуты', 'минут')}"


def _launch_line(bold: bool = False) -> str:
    if LAUNCH_DATE - now_local() <= timedelta(0):
        return "🎉 Клуб открыт"
    v = countdown_line()
    return f"⏳ До запуска: <b>{v}</b>" if bold else f"⏳ До запуска: {v}"


def text_countdown() -> str:
    if LAUNCH_DATE - now_local() <= timedelta(0):
        return "🎉 <b>Equilibrium Club открыт.</b>\n\nСсылки уже у тебя в этом чате."
    return (
        "⏳ <b>До запуска осталось</b>\n\n"
        f"<b>{countdown_line()}</b>\n\n"
        "📅 9 сентября 2026\n"
        "Я напишу сюда за неделю и накануне."
    )


TEXT_PERKS = (
    "🎁 <b>Что даёт Early Bird</b>\n\n"
    "🂡 <b>Скидка 50%</b> на любой уровень подписки — цена фиксируется навсегда. "
    "Тарифы объявим ближе к запуску, твоя ставка уже закреплена.\n\n"
    "👑 <b>Роль OG</b> в Discord: все турниры клуба, встречи, офлайн-семинары "
    "и конференции — бесплатно.\n"
    "<i>Личные расходы участника (дорога, проживание) не входят.</i>\n\n"
    "Эти условия закроются 9 сентября и больше не повторятся."
)

TEXT_HELP = (
    "🂡 <b>Equilibrium Club</b>\n"
    "Закрытое покерное комьюнити. Запуск 9 сентября 2026.\n\n"
    "<b>Команды</b>\n"
    "/start — главное меню\n"
    "/time — сколько осталось до запуска\n"
    "/check — мой статус\n"
    "/help — это сообщение\n\n"
    "Есть вопрос — просто напиши его сюда, я передам команде.\n"
    "Ссылки на Discord и закрытый канал придут в этот чат. Не удаляй диалог."
)


def text_welcome_new() -> str:
    return (
        "🂡 <b>Ты в списке Early Bird.</b>\n\n"
        "Equilibrium Club — закрытое покерное комьюнити. "
        f"Открываемся через <b>{countdown_line()}</b>, 9 сентября 2026.\n\n"
        "<b>Что уже закреплено за тобой:</b>\n"
        "• Скидка 50% на подписку — навсегда\n"
        "• Роль OG: турниры и офлайн-встречи бесплатно\n\n"
        "Ссылки на Discord и закрытый канал придут сюда. Не удаляй диалог."
    )


def text_menu_returning(uid) -> str:
    u = store.get(uid)
    return (
        "👑 <b>Ты в списке Early Bird.</b>\n\n"
        f"📅 Регистрация: {esc(u['reg']) if u else '—'}\n"
        f"{_launch_line(bold=True)}"
    )


def text_status(uid) -> str:
    u = store.get(uid)
    if not u:
        return (
            "❌ <b>Тебя нет в списке.</b>\n\n"
            "Нажми /start — регистрация занимает одно касание, "
            "условия Early Bird ещё доступны."
        )
    return (
        "✅ <b>Ты в списке Early Bird</b>\n\n"
        f"👤 {esc(u['name'])} {esc(u['username'])}\n"
        f"📅 Регистрация: {esc(u['reg'])}\n"
        f"{_launch_line()}\n\n"
        "🂡 Скидка 50% + 👑 роль OG зафиксированы."
    )


# ─── КЛАВИАТУРЫ ───────────────────────────────────────────────────────────
def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏳ Сколько осталось", callback_data="time")],
        [InlineKeyboardButton("🎁 Что даёт Early Bird", callback_data="perks")],
        [
            InlineKeyboardButton("✅ Мой статус", callback_data="status"),
            InlineKeyboardButton("⚙️ Управление", callback_data="settings"),
        ],
    ])


def kb_guest() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🂡 Занять место в списке", callback_data="join")],
        [InlineKeyboardButton("🎁 Что даёт Early Bird", callback_data="perks")],
    ])


def kb_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("← Назад", callback_data="menu")]])


def kb_settings() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚪 Отменить регистрацию", callback_data="cancel_ask")],
        [InlineKeyboardButton("← Назад", callback_data="menu")],
    ])


def kb_confirm_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Да, удалить меня из списка", callback_data="cancel_yes")],
        [InlineKeyboardButton("← Оставить всё как есть", callback_data="menu")],
    ])


def kb_for(uid) -> InlineKeyboardMarkup:
    return kb_main() if store.is_registered(uid) else kb_guest()


# ─── ОТПРАВКА ─────────────────────────────────────────────────────────────
async def safe_send(bot, uid, text, parse_mode=ParseMode.HTML, markup=None) -> str:
    """'ok' | 'blocked' | 'error'"""
    for _ in range(3):
        try:
            await bot.send_message(chat_id=int(uid), text=text,
                                   parse_mode=parse_mode, reply_markup=markup)
            return "ok"
        except Forbidden:
            return "blocked"
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except BadRequest as e:
            m = str(e).lower()
            if "chat not found" in m or "user is deactivated" in m:
                return "blocked"
            if parse_mode is not None:
                parse_mode = None      # разметка битая — шлём как plain
                continue
            log.error("BadRequest %s: %s", uid, e)
            return "error"
        except TelegramError as e:
            log.error("TelegramError %s: %s", uid, e)
            await asyncio.sleep(1)
    return "error"


async def deliver(bot, ids, text, parse_mode=ParseMode.HTML, progress=None) -> dict:
    """Массовая отправка. Сериализована: две рассылки разом превысят лимит TG."""
    async with _send_lock:
        stats = {"ok": 0, "blocked": 0, "error": 0}
        for i, uid in enumerate(ids, 1):
            result = await safe_send(bot, uid, text, parse_mode)
            stats[result] += 1
            store.set_status(uid, "blocked" if result == "blocked" else "active")
            if progress and i % 25 == 0:
                await progress(i, len(ids))
            await asyncio.sleep(BROADCAST_DELAY)
        await store.flush()
        return stats


async def ack(query) -> None:
    """Ответ на callback. После рестарта запрос протухает — это не ошибка."""
    try:
        await query.answer()
    except TelegramError as e:
        log.debug("callback.answer(): %s", e)


async def edit(query, text: str, markup=None) -> bool:
    """Отредактировать сообщение. False — если не вышло."""
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return True
    except TelegramError as e:
        log.debug("edit_message_text(): %s", e)
        return False


async def show(update: Update, text: str, markup=None):
    """Показать экран: отредактировать сообщение с кнопкой или отправить новое."""
    q = update.callback_query
    if q and await edit(q, text, markup):
        return
    try:
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except TelegramError as e:
        log.warning("Не смог показать экран: %s", e)


# ─── КОМАНДЫ ПОЛЬЗОВАТЕЛЯ ─────────────────────────────────────────────────
async def _drop_legacy_keyboard(update: Update, uid: int):
    """Разово убираем reply-клавиатуру у тех, кто застал старую версию бота."""
    if uid in _kb_cleaned or not store.is_registered(uid):
        return
    _kb_cleaned.add(uid)
    try:
        msg = await update.message.reply_text("⌛", reply_markup=ReplyKeyboardRemove())
        await msg.delete()
    except Exception:
        pass


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id
    await _drop_legacy_keyboard(update, uid)

    if store.is_registered(uid):
        store.touch(uid)
        await update.message.reply_text(
            text_menu_returning(uid), parse_mode=ParseMode.HTML, reply_markup=kb_main()
        )
        return

    payload = (context.args[0].lower() if context.args else "")[:32]
    source = SOURCES.get(payload, payload or "напрямую")
    await _register(update, context, user, source)


async def _register(update: Update, context: ContextTypes.DEFAULT_TYPE, user, source="напрямую"):
    username = f"@{user.username}" if user.username else "без username"
    name = user.full_name or "—"
    try:
        created = await store.add(user.id, username, name, source)
    except Exception as e:
        log.error("Регистрация не удалась: %s", e)
        await show(update, "⚠️ Не удалось сохранить регистрацию.\n"
                           "Попробуй ещё раз через минуту — /start")
        return

    if created:
        await safe_send(
            context.bot, ADMIN_CHAT_ID,
            f"🟢 <b>Новая регистрация</b>\n"
            f"Имя: {esc(name)}\nUsername: {esc(username)}\n"
            f"Источник: {esc(source)}\n"
            f"ID: <code>{user.id}</code>\nВсего: {store.count()}",
        )
    await show(update, text_welcome_new(), kb_main())


async def cmd_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store.touch(update.effective_user.id)
    await update.message.reply_text(text_countdown(), parse_mode=ParseMode.HTML,
                                    reply_markup=kb_for(update.effective_user.id))


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    store.touch(uid)
    await update.message.reply_text(text_status(uid), parse_mode=ParseMode.HTML,
                                    reply_markup=kb_for(uid))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    store.touch(uid)
    await update.message.reply_text(TEXT_HELP, parse_mode=ParseMode.HTML, reply_markup=kb_for(uid))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Свободный текст: пересылаем команде, а не отвечаем меню в пустоту."""
    uid = update.effective_user.id
    user = update.effective_user
    text = (update.message.text or "").strip()
    store.touch(uid)

    if not store.is_registered(uid):
        await update.message.reply_text(
            "🂡 <b>Equilibrium Club</b> — закрытое покерное комьюнити.\n"
            "Тебя ещё нет в списке Early Bird.",
            parse_mode=ParseMode.HTML, reply_markup=kb_guest(),
        )
        return

    if not FORWARD_MESSAGES or uid == ADMIN_CHAT_ID:
        await update.message.reply_text("Вот что доступно:", reply_markup=kb_main())
        return

    last = context.user_data.get("last_fwd")
    now = now_local()
    if last and (now - last).total_seconds() < FORWARD_COOLDOWN:
        await update.message.reply_text(
            "Сообщение уже передано — подожди немного перед следующим.",
            reply_markup=kb_main(),
        )
        return
    context.user_data["last_fwd"] = now

    username = f"@{user.username}" if user.username else "без username"
    await safe_send(
        context.bot, ADMIN_CHAT_ID,
        f"💬 <b>Вопрос от участника</b>\n"
        f'<a href="tg://user?id={uid}">{esc(user.full_name)}</a> {esc(username)}\n'
        f"ID: <code>{uid}</code>\n\n"
        f"{esc(text[:2000])}\n\n"
        f"<i>Ответить: /reply {uid} текст</i>",
    )
    await update.message.reply_text(
        "✉️ Передал вопрос команде. Ответ придёт сюда же.",
        reply_markup=kb_main(),
    )


# ─── ИНЛАЙН-КНОПКИ ────────────────────────────────────────────────────────
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    uid = update.effective_user.id
    store.touch(uid)
    await ack(q)
    registered = store.is_registered(uid)

    if data.startswith("bc:"):
        if uid == ADMIN_CHAT_ID:
            await _broadcast_callback(update, context, data)
        return

    if data == "join":
        if registered:
            await show(update, text_menu_returning(uid), kb_main())
        else:
            await _register(update, context, update.effective_user, "кнопка в боте")

    elif data == "time":
        await show(update, text_countdown(), kb_back() if registered else kb_guest())

    elif data == "perks":
        await show(update, TEXT_PERKS, kb_back() if registered else kb_guest())

    elif data == "status":
        await show(update, text_status(uid), kb_back() if registered else kb_guest())

    elif data == "settings":
        if not registered:
            await show(update, text_status(uid), kb_guest())
            return
        await show(
            update,
            "⚙️ <b>Управление</b>\n\nЗдесь можно выйти из списка Early Bird.\n"
            "<i>Вернуться потом можно, но условия к тому моменту могут закрыться.</i>",
            kb_settings(),
        )

    elif data == "cancel_ask":
        await show(
            update,
            "⚠️ <b>Точно отменить регистрацию?</b>\n\n"
            "Ты потеряешь:\n"
            "• Скидку 50% на подписку навсегда\n"
            "• Роль OG и бесплатные турниры\n\n"
            "После 9 сентября эти условия не вернуть.",
            kb_confirm_cancel(),
        )

    elif data == "cancel_yes":
        user = update.effective_user
        try:
            removed = await store.remove(uid)
        except Exception as e:
            log.error("Удаление не удалось: %s", e)
            await show(update, "⚠️ Не получилось. Попробуй позже.", kb_main())
            return
        if removed:
            username = f"@{user.username}" if user.username else "без username"
            await safe_send(
                context.bot, ADMIN_CHAT_ID,
                f"🔴 <b>Отмена регистрации</b>\nUsername: {esc(username)}\n"
                f"ID: <code>{uid}</code>\nОсталось: {store.count()}",
            )
        await show(update, "Регистрация отменена.\n\nПередумаешь — просто нажми /start.",
                   kb_guest())

    else:  # "menu" и любые незнакомые/устаревшие кнопки
        await show(update, text_menu_returning(uid) if registered else TEXT_HELP,
                   kb_main() if registered else kb_guest())


# ─── АДМИН ────────────────────────────────────────────────────────────────
def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_CHAT_ID:
            return
        return await func(update, context)
    return wrapper


async def send_chunked(message, lines, header=""):
    buf, chunks = header, []
    for line in lines:
        if len(buf) + len(line) + 1 > TG_MSG_LIMIT:
            chunks.append(buf)
            buf = ""
        buf += line + "\n"
    if buf.strip():
        chunks.append(buf)
    for chunk in chunks:
        await message.reply_text(chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        await asyncio.sleep(0.05)


@admin_only
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = store.all_users()
    if not users:
        await update.message.reply_text("Список пуст.")
        return
    lines = []
    for i, (uid, u) in enumerate(users, 1):
        mark = "🚫" if u["status"] == "blocked" else "•"
        lines.append(
            f'{i}. {mark} <a href="tg://user?id={uid}">{esc(u["name"])}</a> '
            f'{esc(u["username"])} — {esc(u["reg"])} · {esc(u["source"])}'
        )
    await send_chunked(update.message, lines, header=f"📋 <b>Участников: {len(users)}</b>\n\n")


@admin_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = [u for _, u in store.all_users()]
    total = len(users)
    week = (now_local() - timedelta(days=7)).strftime("%Y-%m-%d")
    today = now_local().strftime("%Y-%m-%d")
    active = sum(1 for u in users if u["activity"] and u["activity"][:10] >= week)
    new_today = sum(1 for u in users if u["reg"][:10] == today)
    blocked = sum(1 for u in users if u["status"] == "blocked")

    sources = Counter(u["source"] for u in users).most_common()
    src_lines = "\n".join(f"  · {esc(s)}: <b>{n}</b>" for s, n in sources) or "  · нет данных"
    flags = ", ".join(k for k in REMINDER_KEYS if store.flag_is_set(k)) or "нет"

    await update.message.reply_text(
        f"📊 <b>Статистика</b>\n\n"
        f"👥 Всего: <b>{total}</b>\n"
        f"🆕 Сегодня: <b>{new_today}</b>\n"
        f"🟢 Активны за 7 дней: <b>{active}</b>\n"
        f"😴 Молчат: <b>{total - active}</b>\n"
        f"🚫 Заблокировали бота: <b>{blocked}</b>\n"
        f"📬 Доступны для рассылки: <b>{total - blocked}</b>\n\n"
        f"<b>Источники</b>\n{src_lines}\n\n"
        f"⏳ До запуска: {countdown_line()}\n"
        f"🔔 Отправленные авторассылки: {flags}",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def cmd_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = (update.message.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        await update.message.reply_text(
            "Использование:\n<code>/reply 123456789 Текст ответа</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    uid, body = parts[1], parts[2]
    result = await safe_send(
        context.bot, uid,
        f"💬 <b>Ответ от команды Equilibrium</b>\n\n{esc(body)}",
        markup=kb_main(),
    )
    if result == "blocked":
        store.set_status(uid, "blocked")
    await update.message.reply_text(
        {"ok": "✅ Отправлено", "blocked": "🚫 Пользователь заблокировал бота",
         "error": "⚠️ Не доставлено"}[result]
    )


@admin_only
async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = (update.message.text or "").split(maxsplit=1)
    body = parts[1].strip() if len(parts) > 1 else ""

    if not body:
        await update.message.reply_text(
            "<b>Рассылка</b>\n\n"
            "<code>/broadcast Текст сообщения</code>\n\n"
            "Поддерживается HTML: &lt;b&gt;жирный&lt;/b&gt;, &lt;i&gt;курсив&lt;/i&gt;, "
            '&lt;a href="…"&gt;ссылка&lt;/a&gt;.\n'
            "Переносы строк сохраняются. Перед отправкой покажу превью.",
            parse_mode=ParseMode.HTML,
        )
        return

    ids = store.reachable_ids()
    if not ids:
        await update.message.reply_text("Некому отправлять.")
        return

    parse_mode = ParseMode.HTML
    try:
        await context.bot.send_message(ADMIN_CHAT_ID, body, parse_mode=parse_mode)
    except BadRequest:
        parse_mode = None
        await context.bot.send_message(
            ADMIN_CHAT_ID, "⚠️ HTML-разметка невалидна, уйдёт обычным текстом:\n\n" + body
        )

    token = secrets.token_urlsafe(6)
    context.bot_data.setdefault("bc", {})[token] = {
        "text": body, "parse_mode": parse_mode, "created": now_local(),
    }
    await update.message.reply_text(
        f"☝️ Так увидят получатели.\n\n"
        f"Отправить <b>{len(ids)}</b> "
        f"{plural(len(ids), 'участнику', 'участникам', 'участникам')}?",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(f"✅ Отправить ({len(ids)})", callback_data=f"bc:go:{token}"),
            InlineKeyboardButton("❌ Отмена", callback_data=f"bc:no:{token}"),
        ]]),
    )


async def _broadcast_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    q = update.callback_query
    try:
        _, action, token = data.split(":", 2)
    except ValueError:
        return
    job = context.bot_data.setdefault("bc", {}).pop(token, None)

    if not job:
        await edit(q, "⌛ Черновик устарел. Набери /broadcast заново.")
        return
    if action == "no":
        await edit(q, "❌ Рассылка отменена.")
        return
    if now_local() - job["created"] > timedelta(seconds=BROADCAST_TTL):
        await edit(q, "⌛ Черновик протух. Набери /broadcast заново.")
        return

    if _send_lock.locked():
        await edit(q, "⏳ Уже идёт другая рассылка. Попробуй через пару минут.")
        return

    ids = store.reachable_ids()
    await edit(q, f"📤 Отправляю… 0 / {len(ids)}")

    async def progress(done, total):
        await edit(q, f"📤 Отправляю… {done} / {total}")

    stats = await deliver(context.bot, ids, job["text"], job["parse_mode"], progress)
    await edit(
        q,
        f"✅ <b>Рассылка завершена</b>\n\n"
        f"Доставлено: {stats['ok']}\n"
        f"Заблокировали бота: {stats['blocked']}\n"
        f"Ошибки: {stats['error']}",
    )


@admin_only
async def cmd_reload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await store.flush()
    await store.load(force=True)
    await update.message.reply_text(f"♻️ Перечитал таблицу: {store.count()} участников.")


# ─── ФОНОВЫЕ ЗАДАЧИ ───────────────────────────────────────────────────────
REMINDERS = [
    ("7d", timedelta(days=7),
     "🚀 До запуска Equilibrium Club осталась <b>неделя</b>.\n\n"
     "9 сентября 2026 — открываемся. Твой Early Bird и роль OG уже зафиксированы, "
     "делать ничего не нужно. Ссылки придут сюда."),
    ("1d", timedelta(days=1),
     "🂡 <b>Завтра.</b>\n\n"
     "Equilibrium Club открывается 9 сентября. Держи Telegram под рукой — "
     "приглашения в Discord и закрытый канал придут в этот чат."),
    ("launch", timedelta(0),
     "🎉 <b>Equilibrium Club открыт.</b>\n\n"
     "Ты в списке Early Bird — скидка 50% и роль OG закреплены навсегда."),
]


async def job_reminders(context: ContextTypes.DEFAULT_TYPE):
    if not store.ready:
        return
    remaining = LAUNCH_DATE - now_local()

    # Ближайшее к запуску из подошедших (список — от дальнего к ближнему).
    due = None
    for i in range(len(REMINDERS) - 1, -1, -1):
        if remaining <= REMINDERS[i][1]:
            due = i
            break
    if due is None:
        return

    key, _, text = REMINDERS[due]

    # Пропущенные гасим молча (бот мог впервые подняться уже после даты).
    for skipped, _, _ in REMINDERS[:due]:
        if not store.flag_is_set(skipped):
            log.info("Гашу устаревшее напоминание %s", skipped)
            await store.set_flag(skipped)

    if store.flag_is_set(key):
        return

    await store.set_flag(key)   # флаг ДО отправки: рестарт не задублит рассылку
    ids = store.reachable_ids()
    log.info("Авторассылка %s → %d получателей", key, len(ids))
    stats = await deliver(context.bot, ids, text)
    await safe_send(
        context.bot, ADMIN_CHAT_ID,
        f"🔔 Авторассылка <b>{esc(key)}</b>\n"
        f"Доставлено: {stats['ok']}, заблокировали: {stats['blocked']}, ошибки: {stats['error']}",
    )


async def job_flush(context: ContextTypes.DEFAULT_TYPE):
    await store.flush()


async def job_gc(context: ContextTypes.DEFAULT_TYPE):
    """Чистим протухшие черновики рассылки."""
    jobs = context.bot_data.get("bc", {})
    now = now_local()
    for token in [t for t, j in jobs.items()
                  if now - j["created"] > timedelta(seconds=BROADCAST_TTL)]:
        jobs.pop(token, None)


# ─── ОШИБКИ ───────────────────────────────────────────────────────────────
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    global _last_error_notify
    log.exception("Ошибка в хэндлере", exc_info=context.error)

    now = datetime.now(timezone.utc)
    if (now - _last_error_notify).total_seconds() < ERROR_COOLDOWN:
        return          # не флудим админа при циклической ошибке
    _last_error_notify = now
    try:
        await context.bot.send_message(
            ADMIN_CHAT_ID,
            f"⚠️ <b>Ошибка бота</b>\n<code>{esc(type(context.error).__name__)}: "
            f"{esc(str(context.error)[:400])}</code>",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass


# ─── ЗАПУСК ───────────────────────────────────────────────────────────────
async def post_init(app: Application):
    await store.load()      # упадём громко, если Sheets недоступны
    public = [
        BotCommand("start", "Главное меню"),
        BotCommand("time", "Сколько осталось до запуска"),
        BotCommand("check", "Мой статус"),
        BotCommand("help", "Что умеет бот"),
    ]
    admin = public + [
        BotCommand("list", "Список участников"),
        BotCommand("stats", "Статистика"),
        BotCommand("broadcast", "Рассылка"),
        BotCommand("reply", "Ответить участнику"),
        BotCommand("reload", "Перечитать таблицу"),
    ]
    await app.bot.set_my_commands(public, scope=BotCommandScopeAllPrivateChats())
    await app.bot.set_my_commands(admin, scope=BotCommandScopeChat(chat_id=ADMIN_CHAT_ID))
    log.info("Команды установлены")


async def post_shutdown(app: Application):
    await store.flush()
    log.info("Остановлен, данные сохранены")


def main():
    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    private = filters.ChatType.PRIVATE

    app.add_handler(CommandHandler("start", cmd_start, filters=private))
    app.add_handler(CommandHandler("time", cmd_time, filters=private))
    app.add_handler(CommandHandler("check", cmd_check, filters=private))
    app.add_handler(CommandHandler("help", cmd_help, filters=private))
    app.add_handler(CommandHandler("list", cmd_list, filters=private))
    app.add_handler(CommandHandler("stats", cmd_stats, filters=private))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast, filters=private))
    app.add_handler(CommandHandler("reply", cmd_reply, filters=private))
    app.add_handler(CommandHandler("reload", cmd_reload, filters=private))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & private, on_text))
    app.add_error_handler(on_error)

    app.job_queue.run_repeating(job_reminders, interval=1800, first=30)
    app.job_queue.run_repeating(job_flush, interval=FLUSH_INTERVAL, first=FLUSH_INTERVAL)
    app.job_queue.run_repeating(job_gc, interval=900, first=900)

    log.info("Бот запущен")
    # drop_pending_updates=False: не теряем регистрации, нажатые во время деплоя.
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
