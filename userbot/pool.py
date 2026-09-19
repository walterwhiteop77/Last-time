"""
Multi-account userbot pool.

Instead of one hard-coded userbot session, the bot now keeps a *pool* of
logged-in Telegram accounts. Work is spread across them round-robin so that
no single account hits Telegram's flood limits.

Roles inside the pool
---------------------
* listener  — the account that watches the source channel for new posts.
              Exactly one account holds this role (default: account 1).
* link      — the account that talks to the second bot (/genlink, /batch).
              Usually must be the bot's owner/admin, so it is pinned
              (default: account 1) rather than rotated.
* worker    — any healthy account. Used for opening bot deep links,
              downloading files and uploading them to the DB channel.
              Rotated least-recently-used, skipping accounts in FloodWait.

Sessions are persisted as StringSessions in MongoDB, so restarts never
require re-login.
"""

import asyncio
import time

from telethon import TelegramClient
from telethon.sessions import StringSession

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import API_ID, API_HASH, SESSION_STRING
from database import (
    get_config,
    update_config,
    get_sessions,
    save_sessions,
    get_session_string,
)


class Account:
    """One logged-in Telegram user account inside the pool."""

    def __init__(self, index: int, session_string: str, label: str = ""):
        self.index = index                     # 1-based, stable display number
        self.session_string = session_string
        self.label = label or f"account{index}"
        self.client: TelegramClient | None = None
        self.lock = asyncio.Lock()             # one job at a time per account
        self.last_used: float = 0.0
        self.flood_until: float = 0.0          # unix ts; skip while in the future
        self.jobs_done: int = 0
        self.enabled: bool = True

    # ── state helpers ────────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        return (
            self.enabled
            and self.client is not None
            and self.flood_until <= time.time()
            and not self.lock.locked()
        )

    @property
    def cooling(self) -> bool:
        return self.flood_until > time.time()

    def note_flood(self, seconds: float) -> None:
        self.flood_until = max(self.flood_until, time.time() + seconds)
        print(f"[pool] {self.label} in FloodWait for {int(seconds)}s — rotating away")

    def touch(self) -> None:
        self.last_used = time.time()
        self.jobs_done += 1

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "session_string": self.session_string,
            "enabled": self.enabled,
        }

    async def status_line(self) -> str:
        who = "?"
        try:
            if self.client and await self.client.is_user_authorized():
                me = await self.client.get_me()
                who = f"@{me.username}" if me.username else (me.first_name or str(me.id))
            else:
                who = "not authorized"
        except Exception as e:
            who = f"error: {e}"
        if not self.enabled:
            state = "⏸ paused"
        elif self.cooling:
            state = f"🕒 cooling {int(self.flood_until - time.time())}s"
        elif self.lock.locked():
            state = "⚙️ working"
        else:
            state = "🟢 ready"
        return f"`{self.index}.` {who} — {state} · jobs: `{self.jobs_done}`"


# ── Pool state ────────────────────────────────────────────────────────────────

accounts: list[Account] = []
_rr_cursor = 0

# Account currently being logged in through /login (not yet in the pool)
pending_client: TelegramClient | None = None
pending_phone: str = ""
pending_hash: str = ""

login_done = asyncio.Event()


def _new_client(session_string: str = "") -> TelegramClient:
    session = StringSession(session_string) if session_string else StringSession()
    return TelegramClient(session, API_ID, API_HASH)


# ── Loading / persistence ─────────────────────────────────────────────────────

async def load_pool() -> int:
    """
    Build the pool from stored sessions.

    Migration path from the old single-account setup:
      1. `sessions` array in MongoDB (new format)
      2. legacy `session_string` field in MongoDB  -> becomes account 1
      3. SESSION_STRING env var                    -> becomes account 1
    Returns the number of connected & authorized accounts.
    """
    global accounts
    accounts = []

    stored = await get_sessions()

    if not stored:
        legacy = ""
        try:
            legacy = await get_session_string()
        except Exception:
            legacy = ""
        legacy = legacy or SESSION_STRING
        if legacy:
            print("[pool] Migrating legacy single session into the multi-account pool")
            stored = [{"label": "account1", "session_string": legacy, "enabled": True}]
            await save_sessions(stored)

    for i, entry in enumerate(stored, start=1):
        acc = Account(i, entry.get("session_string", ""), entry.get("label", ""))
        acc.enabled = entry.get("enabled", True)
        accounts.append(acc)

    live = 0
    for acc in accounts:
        try:
            acc.client = _new_client(acc.session_string)
            await acc.client.connect()
            if await acc.client.is_user_authorized():
                me = await acc.client.get_me()
                acc.label = f"@{me.username}" if me.username else (me.first_name or f"account{acc.index}")
                live += 1
                print(f"[pool] Account {acc.index} connected: {acc.label}")
            else:
                print(f"[pool] Account {acc.index} session is no longer valid — needs /login again")
                acc.enabled = False
        except Exception as e:
            print(f"[pool] Account {acc.index} failed to connect: {e}")
            acc.enabled = False

    print(f"[pool] {live}/{len(accounts)} account(s) ready")
    if live:
        login_done.set()
    return live


async def persist() -> None:
    await save_sessions([a.to_dict() for a in accounts])


async def add_account(client: TelegramClient) -> Account:
    """Register a freshly authenticated client as a new pool account."""
    session_string = StringSession.save(client.session)
    index = len(accounts) + 1
    acc = Account(index, session_string)
    acc.client = client
    try:
        me = await client.get_me()
        acc.label = f"@{me.username}" if me.username else (me.first_name or f"account{index}")
    except Exception:
        pass
    accounts.append(acc)
    await persist()
    # Keep the legacy single-session field in sync for the first account
    if index == 1:
        from database import save_session_string
        await save_session_string(session_string)
    login_done.set()
    print(f"[pool] Added account {index}: {acc.label}")
    return acc


async def remove_account(index: int) -> bool:
    global accounts
    target = next((a for a in accounts if a.index == index), None)
    if target is None:
        return False
    try:
        if target.client:
            await target.client.disconnect()
    except Exception:
        pass
    accounts = [a for a in accounts if a.index != index]
    for i, a in enumerate(accounts, start=1):
        a.index = i
    await persist()

    cfg = await get_config()
    for key in ("listener_index", "link_account_index"):
        if cfg.get(key, 1) > len(accounts):
            await update_config(key, 1)
    return True


async def set_enabled(index: int, enabled: bool) -> bool:
    acc = get_account(index)
    if acc is None:
        return False
    acc.enabled = enabled
    await persist()
    return True


# ── Role accessors ────────────────────────────────────────────────────────────

def get_account(index: int) -> Account | None:
    return next((a for a in accounts if a.index == index), None)


def live_accounts() -> list[Account]:
    return [a for a in accounts if a.client is not None and a.enabled]


async def listener_account() -> Account | None:
    cfg = await get_config()
    idx = cfg.get("listener_index", 1)
    return get_account(idx) or (live_accounts()[0] if live_accounts() else None)


async def link_account() -> Account | None:
    """Account used to talk to the second (link-generating) bot."""
    cfg = await get_config()
    idx = cfg.get("link_account_index", cfg.get("listener_index", 1))
    return get_account(idx) or await listener_account()


def listener_client() -> TelegramClient | None:
    """Synchronous best-effort accessor used by legacy call sites."""
    live = live_accounts()
    return live[0].client if live else None


async def next_worker() -> Account | None:
    """
    Pick the next account for a unit of work.

    Strategy: round-robin, skipping accounts that are paused, busy or in
    FloodWait. Falls back to the least-recently-used account (waiting on its
    lock) when every account is busy, and to the account with the shortest
    remaining cooldown when all are flood-limited.
    """
    global _rr_cursor
    pool = live_accounts()
    if not pool:
        return None

    n = len(pool)
    for offset in range(n):
        acc = pool[(_rr_cursor + offset) % n]
        if acc.available:
            _rr_cursor = (_rr_cursor + offset + 1) % n
            return acc

    ready = [a for a in pool if not a.cooling]
    if ready:
        return min(ready, key=lambda a: a.last_used)

    return min(pool, key=lambda a: a.flood_until)


async def disconnect_all() -> None:
    for acc in accounts:
        try:
            if acc.client:
                await acc.client.disconnect()
        except Exception:
            pass


# ── Login flow (driven from the admin bot) ────────────────────────────────────

async def begin_login(phone: str) -> None:
    """Start OTP login for a brand-new account. Stores a pending client."""
    global pending_client, pending_phone, pending_hash
    pending_client = _new_client()
    await pending_client.connect()
    result = await pending_client.send_code_request(phone)
    pending_phone = phone
    pending_hash = result.phone_code_hash


async def complete_login_code(code: str):
    if pending_client is None:
        raise RuntimeError("No login in progress — send /login first.")
    return await pending_client.sign_in(
        phone=pending_phone, code=code, phone_code_hash=pending_hash
    )


async def complete_login_2fa(password: str):
    if pending_client is None:
        raise RuntimeError("No login in progress — send /login first.")
    return await pending_client.sign_in(password=password)


async def finish_login() -> Account:
    """Move the pending client into the pool."""
    global pending_client, pending_phone, pending_hash
    if pending_client is None:
        raise RuntimeError("No login in progress.")
    acc = await add_account(pending_client)
    pending_client = None
    pending_phone = ""
    pending_hash = ""
    return acc


def cancel_login() -> None:
    global pending_client, pending_phone, pending_hash
    if pending_client is not None:
        asyncio.create_task(pending_client.disconnect())
    pending_client = None
    pending_phone = ""
    pending_hash = ""


# ── Shared channel access ─────────────────────────────────────────────────────

async def join_everywhere(targets: list[str]) -> dict:
    """
    Make every pool account join the given channels so any of them can copy
    files into the DB channel or post to the output channel.
    Returns {account_label: [messages]}.
    """
    from telethon.tl.functions.channels import JoinChannelRequest

    report: dict[str, list[str]] = {}
    for acc in live_accounts():
        lines: list[str] = []
        for target in targets:
            if not target:
                continue
            try:
                await acc.client.get_dialogs()
                entity = await acc.client.get_entity(str(target))
                try:
                    await acc.client(JoinChannelRequest(entity))
                    lines.append(f"joined {target}")
                except Exception:
                    lines.append(f"already in {target}")
            except Exception as e:
                lines.append(f"cannot reach {target}: {e}")
            await asyncio.sleep(1.5)
        report[f"{acc.index}. {acc.label}"] = lines
    return report
