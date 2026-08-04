"""Проверка платежа в сети TRON по TXID.

Берём список входящих USDT-переводов на кошелёк через TronGrid и ищем
среди них нужный TXID. Так не приходится разбирать hex-логи транзакции,
и все адреса сразу приходят в привычном base58.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx

import config

API = "https://api.trongrid.io"
TXID_LEN = 64


@dataclass
class Transfer:
    txid: str
    from_address: str
    amount: Decimal
    at: datetime


def looks_like_txid(text: str) -> bool:
    t = text.strip().lower().removeprefix("0x")
    return len(t) == TXID_LEN and all(c in "0123456789abcdef" for c in t)


def normalize_txid(text: str) -> str:
    return text.strip().lower().removeprefix("0x")


async def incoming_transfers(hours: int | None = None) -> list[Transfer]:
    """Входящие USDT-переводы на кошелёк за последние N часов."""
    hours = hours or config.TX_MAX_AGE_HOURS
    since = datetime.now(timezone.utc) - timedelta(hours=hours + 1)
    params = {
        "limit": 200,
        "only_to": "true",
        "only_confirmed": "true",
        "contract_address": config.USDT_CONTRACT,
        "min_timestamp": int(since.timestamp() * 1000),
    }
    headers = {"TRON-PRO-API-KEY": config.TRONGRID_KEY} if config.TRONGRID_KEY else {}
    url = f"{API}/v1/accounts/{config.USDT_ADDRESS}/transactions/trc20"

    out: list[Transfer] = []
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        for item in resp.json().get("data", []):
            if item.get("to") != config.USDT_ADDRESS:
                continue
            decimals = int(item.get("token_info", {}).get("decimals", 6))
            amount = Decimal(item.get("value", "0")) / (Decimal(10) ** decimals)
            out.append(Transfer(
                txid=str(item.get("transaction_id", "")).lower(),
                from_address=item.get("from", ""),
                amount=amount,
                at=datetime.fromtimestamp(item.get("block_timestamp", 0) / 1000, tz=timezone.utc),
            ))
    return out


async def find_transfer(txid: str) -> Transfer | None:
    txid = normalize_txid(txid)
    for t in await incoming_transfers():
        if t.txid == txid:
            return t
    return None


def check(transfer: Transfer, expected: Decimal) -> tuple[bool, str]:
    """Годится ли перевод под этот счёт."""
    age = datetime.now(timezone.utc) - transfer.at
    if age > timedelta(hours=config.TX_MAX_AGE_HOURS):
        return False, "too_old"
    if transfer.amount + config.AMOUNT_TOLERANCE < expected:
        return False, "amount_low"
    return True, "ok"
