"""Конфигурация бота оплаты EQLBM.CLUB."""

import os
from datetime import date
from decimal import Decimal

# ---------------------------------------------------------------- окружение
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x]

USDT_ADDRESS = os.environ["USDT_ADDRESS"]            # твой TRC20-кошелёк
DISCORD_INVITE = os.environ.get("DISCORD_INVITE", "")

GOOGLE_CREDS_JSON = os.environ["GOOGLE_CREDS_JSON"]  # содержимое service-account json
SHEET_KEY = os.environ["SHEET_KEY"]                  # id таблицы из её URL

TRONGRID_KEY = os.environ.get("TRONGRID_KEY", "")    # необязательно, снимает лимиты
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"  # USDT TRC20

TZ = os.environ.get("TZ", "Europe/Moscow")
REMIND_HOUR = int(os.environ.get("REMIND_HOUR", "12"))

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
TX_MAX_AGE_HOURS = int(os.environ.get("TX_MAX_AGE_HOURS", "24"))

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
