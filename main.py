"""
Main entry point — runs the health-check web server, every workspace's userbot
accounts and the admin bot concurrently.

Each admin owns an isolated workspace (own accounts, own channels, own jobs),
so several people can run their own automations side by side.
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from aiohttp import web

from bot.app import build_app
from bot.processor import process_post
import userbot.client as ub
import userbot.pool as pool
from config import PORT
from database import (
    get_config,
    get_admins,
    list_workspaces,
    migrate_legacy,
    ensure_workspace,
    set_ws,
)


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


# ── Userbot listeners (one per workspace) ─────────────────────────────────────

async def _run_userbots() -> None:
    asyncio.create_task(pool.watchdog())
    started = await ub.start_all_listeners()
    print(f"[userbot] {started} workspace listener(s) started.")
    if not started:
        print("[userbot] No accounts yet — waiting for /login in the admin bot…")
    await asyncio.Event().wait()


# ── Admin bot (python-telegram-bot) ──────────────────────────────────────────

async def _notify_admins_restart(app) -> None:
    try:
        admins = await get_admins()
        for admin_id in admins:
            try:
                set_ws(admin_id)
                cfg = await get_config(admin_id)
                accounts = len(pool.live_accounts(admin_id))
                note = (
                    "🔄 *Bot restarted* and is back online.\n"
                    f"👤 Your userbot accounts ready: `{accounts}`\n"
                    f"📥 Your source: `{cfg.get('source_channel') or 'not set'}`"
                )
                await app.bot.send_message(admin_id, note, parse_mode="Markdown")
                log_channel = cfg.get("log_channel")
                if log_channel:
                    try:
                        await app.bot.send_message(log_channel, note, parse_mode="Markdown")
                    except Exception:
                        pass
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
    print("[bot] Admin bot started. Send /login to add a userbot account.")
    await _notify_admins_restart(app)
    await asyncio.Event().wait()


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    print("[main] Starting TG Automation Bot (multi-user, multi-account)…")

    owner = await migrate_legacy()
    if owner:
        await ensure_workspace(owner)
        print(f"[main] Owner: {owner}")
    for admin in await get_admins():
        await ensure_workspace(admin)

    await pool.load_all()          # connect every workspace's accounts
    print(f"[main] Workspaces: {await list_workspaces()}")

    async def on_new_post(message, links, ws):
        await process_post(message, links, None, None, ws=ws)

    ub.set_forward_callback(on_new_post)
    ptb_app = build_app()

    await asyncio.gather(
        _start_web_server(),
        _run_ptb(ptb_app),
        _run_userbots(),
    )


if __name__ == "__main__":
    asyncio.run(main())
