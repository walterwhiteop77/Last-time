"""
Optional one-time helper: generate a StringSession for the FIRST userbot
account without using the admin bot.

Normally you don't need this — just send /login to your admin bot (as many
times as you have accounts). Use this script only if you prefer to seed the
first account through the SESSION_STRING environment variable.

    python setup_session.py
"""
import asyncio

from telethon import TelegramClient
from telethon.sessions import StringSession

from config import API_ID, API_HASH


async def main() -> None:
    print("Logging in a Telegram user account…")
    async with TelegramClient(StringSession(), API_ID, API_HASH) as client:
        me = await client.get_me()
        print()
        print(f"Logged in as: {me.first_name} (@{me.username})")
        print()
        print("Copy the line below into SESSION_STRING in your environment:")
        print()
        print(client.session.save())
        print()
        print("Extra accounts are added later with /login in the admin bot.")


if __name__ == "__main__":
    asyncio.run(main())
