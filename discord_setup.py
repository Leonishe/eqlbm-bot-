"""Разовая настройка Discord-сервера EQLBM.CLUB.

Создаёт роли, категории и каналы и расставляет права доступа.
Запускать можно сколько угодно раз: то, что уже есть, не трогается,
недостающее достраивается. Ничего не удаляет.

Что нужно до запуска
--------------------
1. Тот же Discord-бот, что выпускает приглашения, должен иметь права
   **Управление ролями** и **Управление каналами**. Если приглашал его
   только с правом на приглашения — пересобери ссылку в OAuth2 → URL Generator
   с этими двумя галками и пройди по ней ещё раз, сервер выбери тот же.
2. Роль бота должна стоять В СПИСКЕ ВЫШЕ создаваемых ролей — иначе Discord
   не даст ему их выдавать. Настройки сервера → Роли → перетащи роль бота
   на самый верх. Это единственное, что придётся сделать мышкой.

Запуск:
    DISCORD_BOT_TOKEN=... DISCORD_GUILD_ID=... python discord_setup.py
"""

import asyncio
import os
import sys

import httpx

API = "https://discord.com/api/v10"

TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
GUILD = os.environ.get("DISCORD_GUILD_ID", "")

VIEW = 1 << 10          # просмотр канала
SEND = 1 << 11          # писать сообщения
CONNECT = 1 << 20       # заходить в голосовой
THREADS = 1 << 34       # создавать ветки в форуме

TEXT, VOICE, CATEGORY, NEWS, FORUM = 0, 2, 4, 5, 15

# ---------------------------------------------------------------- роли
# порядок сверху вниз; цвет в десятичном виде
ROLES = [
    ("VIP", 0xD9A441),
    ("Pro", 0x8B5CF6),
    ("Base", 0xB39BFA),
    ("Коуч", 0x1D9E75),
    ("Психолог", 0x378ADD),
    ("eqrakeback", 0x6F6A62),
]

# Роли-метки уровня: игрок выбирает сам при входе через адаптацию Discord.
# Доступа никуда не дают — это лестница, по которой хочется подниматься.
# hoist=False, чтобы не дробить список участников на пять групп.
ABI_ROLES = [
    ("abi10", 0x9FE1CB),
    ("abi30", 0x5DCAA5),
    ("abi50", 0x1D9E75),
    ("abi100", 0x0F6E56),
    ("abi200", 0x085041),
]

# ---------------------------------------------------------------- структура
# access: какие роли видят категорию. Пустой список — видно всем.
STRUCTURE = [
    {
        "name": "ЛОББИ",
        "access": [],
        "channels": [
            ("правила", TEXT, {"readonly": True}),
            ("объявления", NEWS, {"readonly": True}),
            ("общий-чат", TEXT, {}),
            ("рейкбэк", TEXT, {}),
        ],
    },
    {
        "name": "BASE",
        "access": ["Base", "Pro", "VIP", "Коуч"],
        "channels": [
            ("расписание", TEXT, {"readonly": True}),
            ("материалы", TEXT, {"readonly": True}),
            ("записи-тренировок", TEXT, {"readonly": True}),
            ("вопросы", TEXT, {}),
            ("Тренировка", VOICE, {}),
        ],
    },
    {
        "name": "PRO",
        "access": ["Pro", "VIP", "Коуч"],
        "channels": [
            ("стримы-работы-над-игрой", TEXT, {}),
            ("тематические-тренировки", TEXT, {}),
            ("разборы-баз", FORUM, {}),
            ("споты", FORUM, {}),
        ],
    },
    {
        "name": "VIP",
        "access": ["VIP", "Коуч", "Психолог"],
        "channels": [
            ("vip-чат", TEXT, {}),
            ("очередь-на-сервер", TEXT, {}),
            ("VIP", VOICE, {}),
        ],
    },
]


class Discord:
    def __init__(self, client: httpx.AsyncClient):
        self.c = client

    async def _req(self, method: str, path: str, **kw):
        r = await self.c.request(method, API + path, **kw)
        if r.status_code == 429:                       # лимит запросов
            await asyncio.sleep(r.json().get("retry_after", 2))
            return await self._req(method, path, **kw)
        r.raise_for_status()
        return r.json() if r.text else {}

    async def roles(self):
        return await self._req("GET", f"/guilds/{GUILD}/roles")

    async def channels(self):
        return await self._req("GET", f"/guilds/{GUILD}/channels")

    async def create_role(self, name: str, color: int, hoist: bool = True):
        return await self._req("POST", f"/guilds/{GUILD}/roles",
                               json={"name": name, "color": color,
                                     "hoist": hoist, "mentionable": True})

    async def create_channel(self, payload: dict):
        return await self._req("POST", f"/guilds/{GUILD}/channels", json=payload)

    async def edit_channel(self, cid: str, payload: dict):
        return await self._req("PATCH", f"/channels/{cid}", json=payload)


def overwrites(everyone_id: str, allowed_ids: list[str], readonly: bool, kind: int):
    """Права категории: закрыта для всех, открыта перечисленным ролям."""
    out = []
    if allowed_ids:
        out.append({"id": everyone_id, "type": 0, "deny": str(VIEW), "allow": "0"})
        allow = VIEW | CONNECT | (0 if readonly else SEND) | (THREADS if kind == FORUM else 0)
        for rid in allowed_ids:
            out.append({"id": rid, "type": 0, "allow": str(allow), "deny": "0"})
    elif readonly:
        out.append({"id": everyone_id, "type": 0, "deny": str(SEND), "allow": "0"})
    return out


async def main():
    if not TOKEN or not GUILD:
        sys.exit("Задай DISCORD_BOT_TOKEN и DISCORD_GUILD_ID и запусти снова.")

    headers = {"Authorization": f"Bot {TOKEN}"}
    async with httpx.AsyncClient(headers=headers, timeout=30) as client:
        d = Discord(client)

        existing_roles = {r["name"]: r for r in await d.roles()}
        everyone = next(r["id"] for r in existing_roles.values()
                        if r["name"] == "@everyone")

        # --- роли
        role_id = {}
        for name, color in ROLES:
            if name in existing_roles:
                role_id[name] = existing_roles[name]["id"]
                print(f"роль есть: {name}")
            else:
                r = await d.create_role(name, color)
                role_id[name] = r["id"]
                print(f"роль создана: {name}")

        # --- роли-метки уровня
        for name, color in ABI_ROLES:
            if name in existing_roles:
                print(f"роль есть: {name}")
            else:
                await d.create_role(name, color, hoist=False)
                print(f"роль создана: {name}")

        # --- каналы
        chans = await d.channels()
        by_name = {c["name"].lower(): c for c in chans}

        # английский #rules от мастера настройки переименуем
        if "rules" in by_name and "правила" not in by_name:
            await d.edit_channel(by_name["rules"]["id"], {"name": "правила"})
            by_name["правила"] = by_name.pop("rules")
            print("канал переименован: rules → правила")

        for pos, block in enumerate(STRUCTURE):
            cat = by_name.get(block["name"].lower())
            allowed = [role_id[r] for r in block["access"] if r in role_id]
            perms = overwrites(everyone, allowed, False, TEXT)

            if not cat:
                cat = await d.create_channel({
                    "name": block["name"], "type": CATEGORY,
                    "position": pos, "permission_overwrites": perms,
                })
                print(f"категория создана: {block['name']}")
            else:
                await d.edit_channel(cat["id"], {"permission_overwrites": perms})
                print(f"категория есть, права обновлены: {block['name']}")

            for i, (cname, ctype, opts) in enumerate(block["channels"]):
                found = by_name.get(cname.lower())
                if found:
                    await d.edit_channel(found["id"], {
                        "parent_id": cat["id"], "position": i,
                        "permission_overwrites": overwrites(
                            everyone, allowed, opts.get("readonly", False), ctype),
                    })
                    print(f"  канал есть: {cname}")
                    continue
                await d.create_channel({
                    "name": cname, "type": ctype, "parent_id": cat["id"],
                    "position": i,
                    "permission_overwrites": overwrites(
                        everyone, allowed, opts.get("readonly", False), ctype),
                })
                print(f"  канал создан: {cname}")

    print("\nГотово. Проверь порядок ролей: роль бота должна быть выше VIP.")
    print("Дальше — Настройки сервера → Адаптация: вопрос про лимиты с ролями abi.")


if __name__ == "__main__":
    asyncio.run(main())
