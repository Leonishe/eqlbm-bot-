"""Хранилище: Google Sheets через gspread.

Две вкладки:
  subs     — по одной строке на игрока, текущее состояние подписки
  payments — журнал подтверждённых платежей, он же защита от повторного TXID
"""

import asyncio
import json
from datetime import date, datetime, timedelta
from decimal import Decimal

import gspread

import config

SUBS_HEADER = [
    "user_id", "username", "name", "tier", "months",
    "locked_price", "paid_at", "expires_at", "status", "note",
]
PAY_HEADER = ["txid", "user_id", "tier", "months", "amount", "confirmed_at"]

_gc = None
_sheet = None


def _client():
    global _gc, _sheet
    if _sheet is None:
        creds = json.loads(config.GOOGLE_CREDS_JSON)
        _gc = gspread.service_account_from_dict(creds)
        _sheet = _gc.open_by_key(config.SHEET_KEY)
    return _sheet


def _ws(name: str, header: list[str]):
    sh = _client()
    try:
        ws = sh.worksheet(name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=name, rows=200, cols=max(len(header), 10))
        ws.append_row(header)
    return ws


def _subs_ws():
    return _ws("subs", SUBS_HEADER)


def _pay_ws():
    return _ws("payments", PAY_HEADER)


# ---------------------------------------------------------------- sync-слой
def _all_subs_sync() -> list[dict]:
    return _subs_ws().get_all_records()


def _find_row_sync(user_id: int) -> int | None:
    ids = _subs_ws().col_values(1)
    for i, val in enumerate(ids[1:], start=2):
        if str(val).strip() == str(user_id):
            return i
    return None


def _upsert_sub_sync(row: dict) -> None:
    ws = _subs_ws()
    values = [str(row.get(k, "")) for k in SUBS_HEADER]
    idx = _find_row_sync(int(row["user_id"]))
    if idx:
        ws.update(f"A{idx}:J{idx}", [values])
    else:
        ws.append_row(values)


def _get_sub_sync(user_id: int) -> dict | None:
    for r in _all_subs_sync():
        if str(r.get("user_id")).strip() == str(user_id):
            return r
    return None


def _txid_used_sync(txid: str) -> bool:
    return txid.lower() in {str(v).lower() for v in _pay_ws().col_values(1)[1:]}


def _add_payment_sync(txid: str, user_id: int, tier: str, months: int, amount: Decimal) -> None:
    _pay_ws().append_row([
        txid, str(user_id), tier, str(months), str(amount),
        datetime.utcnow().isoformat(timespec="seconds"),
    ])


# ---------------------------------------------------------------- async-обёртки
async def all_subs() -> list[dict]:
    return await asyncio.to_thread(_all_subs_sync)


async def get_sub(user_id: int) -> dict | None:
    return await asyncio.to_thread(_get_sub_sync, user_id)


async def save_sub(row: dict) -> None:
    await asyncio.to_thread(_upsert_sub_sync, row)


async def txid_used(txid: str) -> bool:
    return await asyncio.to_thread(_txid_used_sync, txid)


async def add_payment(txid: str, user_id: int, tier: str, months: int, amount: Decimal) -> None:
    await asyncio.to_thread(_add_payment_sync, txid, user_id, tier, months, amount)


# ---------------------------------------------------------------- логика подписки
def parse_date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


async def activate(user, tier: str, months: int, amount: Decimal,
                   locked_price: Decimal, today: date | None = None) -> date:
    """Продлевает подписку: от сегодня или от текущего конца, если он в будущем."""
    today = today or date.today()
    existing = await get_sub(user.id)
    start = today
    if existing:
        cur = parse_date(existing.get("expires_at"))
        if cur and cur > today:
            start = cur
    expires = start + timedelta(days=30 * months)

    keep_locked = locked_price
    if existing and existing.get("locked_price"):
        keep_locked = Decimal(str(existing["locked_price"]))

    await save_sub({
        "user_id": user.id,
        "username": user.username or "",
        "name": user.full_name,
        "tier": tier,
        "months": months,
        "locked_price": keep_locked,
        "paid_at": today.isoformat(),
        "expires_at": expires.isoformat(),
        "status": "active",
        "note": existing.get("note", "") if existing else "",
    })
    return expires


# ---------------------------------------------------------------- приглашения
INVITE_HEADER = ["code", "kind", "tier", "months", "created_at", "used_by", "used_at"]


def _inv_ws():
    return _ws("invites", INVITE_HEADER)


def _create_invite_sync(code: str, kind: str, tier: str, months: int) -> None:
    _inv_ws().append_row([
        code, kind, tier, str(months),
        datetime.utcnow().isoformat(timespec="seconds"), "", "",
    ])


def _get_invite_sync(code: str) -> dict | None:
    for r in _inv_ws().get_all_records():
        if str(r.get("code")).strip() == code:
            return r
    return None


def _use_invite_sync(code: str, user_id: int) -> None:
    ws = _inv_ws()
    codes = ws.col_values(1)
    for i, val in enumerate(codes[1:], start=2):
        if str(val).strip() == code:
            ws.update(f"F{i}:G{i}", [[str(user_id),
                                      datetime.utcnow().isoformat(timespec="seconds")]])
            return


def _pending_by_username_sync(username: str) -> dict | None:
    """Строка, заведённая заранее по нику, к которой ещё не привязан Telegram ID."""
    if not username:
        return None
    uname = username.lstrip("@").lower()
    for r in _all_subs_sync():
        if not str(r.get("user_id", "")).strip():
            if str(r.get("username", "")).lstrip("@").lower() == uname:
                return r
    return None


async def create_invite(code: str, kind: str, tier: str = "", months: int = 0) -> None:
    await asyncio.to_thread(_create_invite_sync, code, kind, tier, months)


async def get_invite(code: str) -> dict | None:
    return await asyncio.to_thread(_get_invite_sync, code)


async def use_invite(code: str, user_id: int) -> None:
    await asyncio.to_thread(_use_invite_sync, code, user_id)


async def pending_by_username(username: str) -> dict | None:
    return await asyncio.to_thread(_pending_by_username_sync, username)


async def set_affiliate(user_id: int, username: str = "", name: str = "",
                        note: str = "аффилейт") -> None:
    """Доступ в чат без подписки: сроков нет, напоминания не идут."""
    existing = await get_sub(user_id) if user_id else None
    await save_sub({
        "user_id": user_id or "",
        "username": (username or (existing or {}).get("username", "")).lstrip("@"),
        "name": name or (existing or {}).get("name", ""),
        "tier": "",
        "months": "",
        "locked_price": (existing or {}).get("locked_price", ""),
        "paid_at": "",
        "expires_at": "",
        "status": "affiliate",
        "note": note,
    })


async def activate_row(user_id: int | str, username: str, name: str, tier: str,
                       months: int, locked_price: Decimal, note: str = "",
                       today: date | None = None) -> date:
    """Активация подписки без объекта Telegram-пользователя.

    Нужна для ручной выдачи: игрок мог оплатить переводом в руме и ещё
    ни разу не написать боту — тогда user_id пустой, строка привяжется
    к нему при первом /start по нику.
    """
    today = today or date.today()
    existing = await get_sub(int(user_id)) if str(user_id).strip().isdigit() else None
    if existing is None and username:
        existing = await pending_by_username(username)

    start = today
    if existing:
        cur = parse_date(existing.get("expires_at"))
        if cur and cur > today:
            start = cur
    expires = start + timedelta(days=30 * months)

    keep_locked = locked_price
    if existing and existing.get("locked_price"):
        keep_locked = Decimal(str(existing["locked_price"]))

    row = {
        "user_id": user_id or "",
        "username": (username or (existing or {}).get("username", "")).lstrip("@"),
        "name": name or (existing or {}).get("name", ""),
        "tier": tier,
        "months": months,
        "locked_price": keep_locked,
        "paid_at": today.isoformat(),
        "expires_at": expires.isoformat(),
        "status": "active",
        "note": note or (existing or {}).get("note", ""),
    }

    # если строка была заведена по нику без ID — обновляем именно её
    if not str(user_id).strip() and existing and not str(existing.get("user_id", "")).strip():
        await asyncio.to_thread(_replace_by_username_sync, row)
    else:
        await save_sub(row)
    return expires


def _replace_by_username_sync(row: dict) -> None:
    ws = _subs_ws()
    uname = str(row.get("username", "")).lstrip("@").lower()
    ids = ws.col_values(1)
    names = ws.col_values(2)
    for i in range(2, len(names) + 1):
        has_id = len(ids) >= i and str(ids[i - 1]).strip()
        if not has_id and str(names[i - 1]).lstrip("@").lower() == uname:
            ws.update(f"A{i}:J{i}", [[str(row.get(k, "")) for k in SUBS_HEADER]])
            return
    ws.append_row([str(row.get(k, "")) for k in SUBS_HEADER])


# ---------------------------------------------------------------- выставленные счета
# Счёт живёт в таблице, а не в памяти процесса: перезапуск бота в момент,
# когда игрок ушёл переводить USDT, больше не теряет его выбор.
INVOICE_HEADER = ["user_id", "tier", "months", "amount", "created_at", "status"]


def _inv2_ws():
    return _ws("invoices", INVOICE_HEADER)


def _save_invoice_sync(user_id: int, tier: str, months: int, amount) -> None:
    ws = _inv2_ws()
    row = [str(user_id), tier, str(months), str(amount),
           datetime.utcnow().isoformat(timespec="seconds"), "open"]
    ids = ws.col_values(1)
    for i, val in enumerate(ids[1:], start=2):
        if str(val).strip() == str(user_id):
            ws.update(f"A{i}:F{i}", [row])
            return
    ws.append_row(row)


def _get_invoice_sync(user_id: int) -> dict | None:
    for r in _inv2_ws().get_all_records():
        if str(r.get("user_id")).strip() == str(user_id) and r.get("status") == "open":
            return {"tier": r["tier"], "months": int(r["months"]), "amount": str(r["amount"])}
    return None


def _close_invoice_sync(user_id: int) -> None:
    ws = _inv2_ws()
    ids = ws.col_values(1)
    for i, val in enumerate(ids[1:], start=2):
        if str(val).strip() == str(user_id):
            ws.update(f"F{i}", [["paid"]])
            return


async def save_invoice(user_id: int, tier: str, months: int, amount) -> None:
    await asyncio.to_thread(_save_invoice_sync, user_id, tier, months, amount)


async def get_invoice(user_id: int) -> dict | None:
    return await asyncio.to_thread(_get_invoice_sync, user_id)


async def close_invoice(user_id: int) -> None:
    await asyncio.to_thread(_close_invoice_sync, user_id)
