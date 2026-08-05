"""Конфигурация бота оплаты EQLBM.CLUB."""

import os
from datetime import date
from decimal import Decimal

# ---------------------------------------------------------------- окружение
def _env(*names: str, default=None):
    """Первое заданное значение из нескольких возможных имён переменной.

    Имена от старого бота (TELEGRAM_TOKEN, GOOGLE_CREDS, SHEET_ID, ADMIN_CHAT_ID)
    поддерживаются наравне с новыми — переименовывать в Railway ничего не нужно.
    """
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    if default is None:
        raise RuntimeError(f"не задана переменная окружения: {' или '.join(names)}")
    return default


BOT_TOKEN = _env("BOT_TOKEN", "TELEGRAM_TOKEN")
ADMIN_IDS = [
    int(x) for x in _env("ADMIN_IDS", "ADMIN_CHAT_ID", default="")
    .replace(" ", "").split(",") if x.strip().lstrip("-").isdigit()
]

USDT_ADDRESS = _env("USDT_ADDRESS")                  # твой TRC20-кошелёк
DISCORD_INVITE = _env("DISCORD_INVITE", default="")      # запасная постоянная ссылка
DISCORD_BOT_TOKEN = _env("DISCORD_BOT_TOKEN", default="")
DISCORD_CHANNEL_ID = _env("DISCORD_CHANNEL_ID", default="")
INVITE_MAX_AGE = int(_env("INVITE_MAX_AGE", default="86400"))  # секунд, сутки

GOOGLE_CREDS_JSON = _env("GOOGLE_CREDS_JSON", "GOOGLE_CREDS")
SHEET_KEY = _env("SHEET_KEY", "SHEET_ID")

TRONGRID_KEY = _env("TRONGRID_KEY", default="")    # необязательно, снимает лимиты
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"  # USDT TRC20

TZ = _env("TZ", default="Europe/Moscow")
REMIND_HOUR = int(_env("REMIND_HOUR", default="12"))

# ---------------------------------------------------------------- тарифы
# price_now — цена первого набора, price_after — с 9 сентября
TIERS = {
    "base": {"title": "Base", "price_now": Decimal("35"), "price_after": Decimal("50")},
    "pro": {"title": "Pro", "price_now": Decimal("100"), "price_after": Decimal("150")},
    "vip": {"title": "VIP", "price_now": Decimal("500"), "price_after": Decimal("500")},
}

# скидка за предоплату, % — те же цифры, что на сайте
DISCOUNTS = {1: 0, 3: 10, 6: 17, 12: 20}

PRICE_SWITCH_DATE = date(2026, 9, 9)

# сколько часов транзакция считается свежей (защита от чужих TXID из блокчейна)
TX_MAX_AGE_HOURS = int(_env("TX_MAX_AGE_HOURS", default="24"))

# Окно поиска перевода — заведомо шире срока годности счёта, чтобы старая
# транзакция находилась и получала внятный отказ, а не «не нашёл».
TX_LOOKUP_HOURS = int(_env("TX_LOOKUP_HOURS", default="336"))

# допустимый недобор по сумме (округления в кошельках)
AMOUNT_TOLERANCE = Decimal("0.02")

REMIND_DAYS = (3, 2, 1)


def monthly_price(tier: str, locked: Decimal | None = None, today: date | None = None) -> Decimal:
    """Месячная цена тарифа.

    Если у игрока зафиксирована цена (он из первого набора) — она и действует
    при любом продлении. Иначе берётся актуальный прайс на сегодня.
    """
    if locked is not None:
        return Decimal(locked)
    today = today or date.today()
    key = "price_after" if today >= PRICE_SWITCH_DATE else "price_now"
    return TIERS[tier][key]


def invoice_amount(tier: str, months: int, locked: Decimal | None = None,
                   today: date | None = None) -> Decimal:
    """Итоговая сумма счёта с учётом скидки за срок."""
    base = monthly_price(tier, locked, today)
    off = Decimal(DISCOUNTS.get(months, 0))
    per_month = (base * (100 - off) / 100).quantize(Decimal("1"))
    return per_month * months


def tier_title(tier: str) -> str:
    return TIERS[tier]["title"]


def standard_price(tier: str) -> Decimal:
    """Обычная цена тарифа — та, что действует после 9 сентября."""
    return TIERS[tier]["price_after"]


def is_first_intake(tier: str, locked: Decimal | None = None,
                    today: date | None = None) -> bool:
    """Идёт ли ещё первый набор для этой цены (есть что зачёркивать)."""
    return monthly_price(tier, locked, today) < standard_price(tier)
