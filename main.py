"""
Main entry point — runs the health-check web server, userbot and admin bot concurrently.

Render requires the process to bind PORT within the first 60 s,
so the aiohttp server starts first.
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from aiohttp import web

from bot.app import build_app
from bot.processor import process_post
import userbot.client as ub
from config import PORT


# ── Render health-check web server ────────────────────────────────────────────

async def _health(_request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def _start_web_server() -> None:
    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"[web] Health-check server listening on port {PORT}")
    await asyncio.Event().wait()          # run forever


# ── Userbot ───────────────────────────────────────────────────────────────────

async def _run_userbot() -> None:
    if await ub.is_authorized():
        print("[userbot] Session found — starting listener.")
        ub.login_done.set()
    else:
        print("[userbot] No session — waiting for /login command in bot...")
        await ub.login_done.wait()
        print("[userbot] Login complete — starting listener.")
    await ub.begin_listening()


# ── Admin bot (python-telegram-bot) ──────────────────────────────────────────

async def _run_ptb(app) -> None:
    await app.initialize()
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        print(f"[bot] delete_webhook warning (non-fatal): {e}")
    await asyncio.sleep(3)
    await app.start()
    await app.updater.start_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query"],
    )
    print("[bot] Admin bot started. Send /login to authenticate the userbot.")
    await asyncio.Event().wait()


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    print("[main] Starting TG Automation Bot...")

    # init_client is now async — it loads the session from MongoDB if available
    await ub.init_client()
    await ub.connect()

    async def on_new_post(message, links):
        await process_post(message, links, ub.userbot, None)

    ub.set_forward_callback(on_new_post)
    ptb_app = build_app()

    await asyncio.gather(
        _start_web_server(),
        _run_ptb(ptb_app),
        _run_userbot(),
    )


if __name__ == "__main__":
    asyncio.run(main())
