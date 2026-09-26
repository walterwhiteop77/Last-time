"""
Per-workspace multi-account userbot pools.

Each admin (workspace) owns their OWN pool of logged-in Telegram accounts.
Pools never share accounts, so two admins can run their own jobs at the same
time on their own channels.

Roles inside one pool
---------------------
* listener  — watches that workspace's source channel (default: account 1)
* link      — talks to that workspace's second bot (pinned, default: 1)
* worker    — any healthy account, rotated least-recently-used

Sessions are persisted as StringSessions inside the workspace document, so
restarts never require re-login.
"""

import asyncio
import time

from telethon import TelegramClient
from telethon.sessions import StringSession

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import API_ID, API_HASH, SESSION_STRING
import database as dbm
from database import (
    get_config,
    update_config,
    get_sessions,
    save_sessions,
    get_session_string,
    list_workspaces,
    get_owner,
)


class Account:
    """One logged-in Telegram user account inside a workspace pool."""

    def __init__(self, index: int, session_string: str, label: str = ""):
        self.index = index
        self.session_string = session_string
        self.label = label or f"account{index}"
        self.client: TelegramClient | None = None
        self.lock = asyncio.Lock()
        self.last_used: float = 0.0
        self.flood_until: float = 0.0
        self.jobs_done: int = 0
        self.enabled: bool = True

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


def _new_client(session_string: str = "") -> TelegramClient:
    session = StringSession(session_string) if session_string else StringSession()
    # flood_sleep_threshold: Telethon would otherwise SILENTLY sleep up to 60s
    # on every FloodWait, freezing the account. We raise instead and rotate.
    return TelegramClient(
        session, API_ID, API_HASH,
        flood_sleep_threshold=5,
        auto_reconnect=True,
        connection_retries=None,   # never give up reconnecting
        retry_delay=3,
        request_retries=4,
        timeout=20,
    )


class Pool:
    """All userbot accounts belonging to ONE workspace."""

    def __init__(self, ws: int):
        self.ws = int(ws)
        self.accounts: list[Account] = []
        self._rr = 0
        self.login_done = asyncio.Event()
        # /login in progress (not yet part of the pool)
        self.pending_client: TelegramClient | None = None
        self.pending_phone = ""
        self.pending_hash = ""
        self.listener_task: asyncio.Task | None = None

    # ── loading / persistence ────────────────────────────────────────────────

    async def load(self) -> int:
        self.accounts = []
        stored = await get_sessions(self.ws)

        if not stored:
            legacy = ""
            try:
                legacy = await get_session_string(self.ws)
            except Exception:
                legacy = ""
            if not legacy and self.ws == (await get_owner() or 0):
                legacy = SESSION_STRING
            if legacy:
                print(f"[pool:{self.ws}] Migrating legacy single session into the pool")
                stored = [{"label": "account1", "session_string": legacy, "enabled": True}]
                await save_sessions(stored, self.ws)

        for i, entry in enumerate(stored, start=1):
            acc = Account(i, entry.get("session_string", ""), entry.get("label", ""))
            acc.enabled = entry.get("enabled", True)
            self.accounts.append(acc)

        live = 0
        for acc in self.accounts:
            try:
                acc.client = _new_client(acc.session_string)
                await acc.client.connect()
                if await acc.client.is_user_authorized():
                    me = await acc.client.get_me()
                    acc.label = f"@{me.username}" if me.username else (me.first_name or f"account{acc.index}")
                    live += 1
                    print(f"[pool:{self.ws}] Account {acc.index} connected: {acc.label}")
                else:
                    print(f"[pool:{self.ws}] Account {acc.index} session invalid — /login again")
                    acc.enabled = False
            except Exception as e:
                print(f"[pool:{self.ws}] Account {acc.index} failed to connect: {e}")
                acc.enabled = False

        print(f"[pool:{self.ws}] {live}/{len(self.accounts)} account(s) ready")
        if live:
            self.login_done.set()
        return live

    async def persist(self) -> None:
        await save_sessions([a.to_dict() for a in self.accounts], self.ws)

    async def add_account(self, client: TelegramClient) -> Account:
        session_string = StringSession.save(client.session)
        index = len(self.accounts) + 1
        acc = Account(index, session_string)
        acc.client = client
        try:
            me = await client.get_me()
            acc.label = f"@{me.username}" if me.username else (me.first_name or f"account{index}")
        except Exception:
            pass
        self.accounts.append(acc)
        await self.persist()
        self.login_done.set()
        print(f"[pool:{self.ws}] Added account {index}: {acc.label}")
        return acc

    async def remove_account(self, index: int) -> bool:
        target = self.get(index)
        if target is None:
            return False
        try:
            if target.client:
                await target.client.disconnect()
        except Exception:
            pass
        self.accounts = [a for a in self.accounts if a.index != index]
        for i, a in enumerate(self.accounts, start=1):
            a.index = i
        await self.persist()

        cfg = await get_config(self.ws)
        for key in ("listener_index", "link_account_index"):
            if cfg.get(key, 1) > len(self.accounts):
                await update_config(key, 1, self.ws)
        return True

    async def set_enabled(self, index: int, enabled: bool) -> bool:
        acc = self.get(index)
        if acc is None:
            return False
        acc.enabled = enabled
        await self.persist()
        return True

    # ── role accessors ───────────────────────────────────────────────────────

    def get(self, index: int) -> Account | None:
        return next((a for a in self.accounts if a.index == index), None)

    def live(self) -> list[Account]:
        return [a for a in self.accounts if a.client is not None and a.enabled]

    async def listener(self) -> Account | None:
        cfg = await get_config(self.ws)
        idx = cfg.get("listener_index", 1)
        live = self.live()
        return self.get(idx) or (live[0] if live else None)

    async def link(self) -> Account | None:
        cfg = await get_config(self.ws)
        idx = cfg.get("link_account_index", cfg.get("listener_index", 1))
        return self.get(idx) or await self.listener()

    def listener_client_sync(self) -> TelegramClient | None:
        live = self.live()
        return live[0].client if live else None

    async def next_worker(self) -> Account | None:
        live = self.live()
        if not live:
            return None
        n = len(live)
        for offset in range(n):
            acc = live[(self._rr + offset) % n]
            if acc.available:
                self._rr = (self._rr + offset + 1) % n
                return acc
        ready = [a for a in live if not a.cooling]
        if ready:
            return min(ready, key=lambda a: a.last_used)
        return min(live, key=lambda a: a.flood_until)

    async def disconnect_all(self) -> None:
        for acc in self.accounts:
            try:
                if acc.client:
                    await acc.client.disconnect()
            except Exception:
                pass

    # ── login flow ───────────────────────────────────────────────────────────

    async def begin_login(self, phone: str) -> None:
        self.pending_client = _new_client()
        await self.pending_client.connect()
        result = await self.pending_client.send_code_request(phone)
        self.pending_phone = phone
        self.pending_hash = result.phone_code_hash

    async def complete_login_code(self, code: str):
        if self.pending_client is None:
            raise RuntimeError("No login in progress — send /login first.")
        return await self.pending_client.sign_in(
            phone=self.pending_phone, code=code, phone_code_hash=self.pending_hash
        )

    async def complete_login_2fa(self, password: str):
        if self.pending_client is None:
            raise RuntimeError("No login in progress — send /login first.")
        return await self.pending_client.sign_in(password=password)

    async def finish_login(self) -> Account:
        if self.pending_client is None:
            raise RuntimeError("No login in progress.")
        acc = await self.add_account(self.pending_client)
        self.pending_client = None
        self.pending_phone = ""
        self.pending_hash = ""
        return acc

    def cancel_login(self) -> None:
        if self.pending_client is not None:
            asyncio.create_task(self.pending_client.disconnect())
        self.pending_client = None
        self.pending_phone = ""
        self.pending_hash = ""

    # ── shared channel access ────────────────────────────────────────────────

    async def join_everywhere(self, targets: list) -> dict:
        from telethon.tl.functions.channels import JoinChannelRequest

        report: dict[str, list[str]] = {}
        for acc in self.live():
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


# ── Registry ──────────────────────────────────────────────────────────────────

_pools: dict[int, Pool] = {}


def get_pool(ws=None) -> Pool:
    uid = dbm._resolve(ws)
    if uid not in _pools:
        _pools[uid] = Pool(uid)
    return _pools[uid]


def all_pools() -> dict:
    return dict(_pools)


async def load_all() -> dict:
    """Connect every workspace's accounts. Returns {ws: live_count}."""
    result = {}
    for ws in await list_workspaces():
        p = get_pool(ws)
        try:
            result[ws] = await p.load()
        except Exception as e:
            print(f"[pool:{ws}] load failed: {e}")
            result[ws] = 0
    return result


# ── Module-level shims (workspace-aware, default = current context) ───────────

def accounts_of(ws=None) -> list:
    return get_pool(ws).accounts


def live_accounts(ws=None) -> list:
    return get_pool(ws).live()


def get_account(index: int, ws=None) -> Account | None:
    return get_pool(ws).get(index)


async def listener_account(ws=None) -> Account | None:
    return await get_pool(ws).listener()


async def link_account(ws=None) -> Account | None:
    return await get_pool(ws).link()


def listener_client(ws=None) -> TelegramClient | None:
    return get_pool(ws).listener_client_sync()


async def next_worker(ws=None) -> Account | None:
    return await get_pool(ws).next_worker()


async def remove_account(index: int, ws=None) -> bool:
    return await get_pool(ws).remove_account(index)


async def set_enabled(index: int, enabled: bool, ws=None) -> bool:
    return await get_pool(ws).set_enabled(index, enabled)


async def begin_login(phone: str, ws=None) -> None:
    await get_pool(ws).begin_login(phone)


async def complete_login_code(code: str, ws=None):
    return await get_pool(ws).complete_login_code(code)


async def complete_login_2fa(password: str, ws=None):
    return await get_pool(ws).complete_login_2fa(password)


async def finish_login(ws=None) -> Account:
    return await get_pool(ws).finish_login()


def cancel_login(ws=None) -> None:
    get_pool(ws).cancel_login()


async def join_everywhere(targets: list, ws=None) -> dict:
    return await get_pool(ws).join_everywhere(targets)


async def disconnect_everything() -> None:
    for p in _pools.values():
        await p.disconnect_all()


# ── Watchdog: keeps every account connected & receiving updates ──────────────

async def watchdog(interval: int = 45) -> None:
    """Reconnects dropped accounts and pokes idle ones so the update stream
    never silently stalls (the classic 'userbot sleeps then wakes up' bug)."""
    import asyncio
    while True:
        await asyncio.sleep(interval)
        for ws, p in list(_pools.items()):
            for acc in list(p.accounts):
                c = acc.client
                if not c or not acc.enabled:
                    continue
                try:
                    if not c.is_connected():
                        print(f"[watchdog] ws={ws} {acc.label} disconnected — reconnecting")
                        await asyncio.wait_for(c.connect(), 30)
                        try:
                            await c.catch_up()
                        except Exception:
                            pass
                    else:
                        # cheap ping keeps the MTProto session & updates alive
                        await asyncio.wait_for(c.get_me(), 20)
                except Exception as e:
                    print(f"[watchdog] ws={ws} {acc.label} check failed: {e} — forcing reconnect")
                    try:
                        await c.disconnect()
                    except Exception:
                        pass
                    try:
                        await asyncio.wait_for(c.connect(), 30)
                    except Exception as e2:
                        print(f"[watchdog] reconnect failed: {e2}")
