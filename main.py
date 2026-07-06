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
from database import get_config


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

async def _notify_admins_restart(app) -> None:
    """Send a restart notice to every configured admin. Non-fatal if it fails."""
    try:
        cfg = await get_config()
        admins = cfg.get("admins", [])
        if not admins:
            print("[bot] No admins configured — skipping restart notification.")
            return
        for admin_id in admins:
            try:
                await app.bot.send_message(
                    admin_id,
                    "🔄 *Bot restarted* and is back online.",
                    parse_mode="Markdown",
                )
            except Exception as e:
                print(f"[bot] Could not notify admin {admin_id}: {e}")
    except Exception as e:
        print(f"[bot] Restart notification failed: {e}")


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
    await _notify_admins_restart(app)
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
