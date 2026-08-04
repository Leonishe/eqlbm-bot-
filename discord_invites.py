"""Персональные приглашения в Discord.

Бот выпускает свежую ссылку на каждого игрока: одноразовую и с коротким сроком.
Так она не успевает протухнуть и её нельзя передать дальше.

Что нужно один раз настроить:
  1. Discord Developer Portal → New Application → Bot → скопировать токен
     в переменную DISCORD_BOT_TOKEN.
  2. Пригласить этого бота на свой сервер с правом «Создавать приглашения»
     (Create Instant Invite).
  3. Взять ID канала, куда должны попадать новички (правой кнопкой по каналу →
     Копировать ID, включив режим разработчика), в DISCORD_CHANNEL_ID.

Если переменные не заданы, используется постоянная ссылка из DISCORD_INVITE.
"""

import logging

import httpx

import config

log = logging.getLogger("eqlbm-pay.discord")

API = "https://discord.com/api/v10"


async def personal_invite() -> str:
    """Одноразовая ссылка на сутки. При любой ошибке — запасная из настроек."""
    if not (config.DISCORD_BOT_TOKEN and config.DISCORD_CHANNEL_ID):
        return config.DISCORD_INVITE

    url = f"{API}/channels/{config.DISCORD_CHANNEL_ID}/invites"
    headers = {"Authorization": f"Bot {config.DISCORD_BOT_TOKEN}"}
    payload = {
        "max_age": config.INVITE_MAX_AGE,   # секунд, по умолчанию сутки
        "max_uses": 1,                      # одноразовая
        "unique": True,
        "temporary": False,
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            code = resp.json().get("code")
            if code:
                return f"https://discord.gg/{code}"
    except Exception:
        log.exception("не удалось выпустить приглашение, отдаю запасную ссылку")

    return config.DISCORD_INVITE
