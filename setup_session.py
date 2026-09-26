"""
Optional helper — prints a StringSession for one Telegram account.

You normally do NOT need this: every admin adds their own accounts straight
from the bot with /login. Use it only if you want to seed the owner's first
account through the SESSION_STRING environment variable.

    python setup_session.py
"""
import asyncio

from telethon import TelegramClient
from telethon.sessions import StringSession

from config import API_ID, API_HASH


async def main():
    async with TelegramClient(StringSession(), API_ID, API_HASH) as client:
        session = StringSession.save(client.session)
        print("\n=== SESSION_STRING (keep this secret) ===\n")
        print(session)
        print("\nAdd it to your .env as SESSION_STRING=… (optional).\n")


if __name__ == "__main__":
    asyncio.run(main())
