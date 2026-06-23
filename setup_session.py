"""
Run this ONCE locally to generate a SESSION_STRING for Render deployment.

Usage:
    pip install telethon python-dotenv
    python setup_session.py

Then copy the SESSION_STRING value into Render > Environment Variables.
"""
import asyncio
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

API_ID   = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]

from telethon import TelegramClient
from telethon.sessions import StringSession


async def main():
    print("=== Userbot Session Setup ===")
    print("Logs in as YOUR Telegram account (not a bot).")
    print("You will receive an OTP on Telegram.\n")

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.start()

    me = await client.get_me()
    session_str = client.session.save()

    print(f"\nLogged in as: {me.first_name} (@{me.username}) — ID: {me.id}")
    print("\n" + "=" * 60)
    print("SESSION_STRING (copy this into Render Environment Variables):")
    print("=" * 60)
    print(session_str)
    print("=" * 60)
    print("\nSet this as SESSION_STRING in Render > Environment.")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
