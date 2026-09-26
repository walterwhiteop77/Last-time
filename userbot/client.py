"""
Userbot layer — one independent account pool per workspace (per admin).

Each workspace has its own listener account watching its own source channel,
its own worker accounts and its own cancel flag, so two admins can run jobs
at the same time without touching each other's setup.
"""

import asyncio
import re

from telethon import events
from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl
from telethon.tl.functions.messages import GetBotCallbackAnswerRequest
from telethon.tl.functions.channels import JoinChannelRequest

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import database as dbm
from database import get_config, log_event, set_ws, list_workspaces
import userbot.pool as pool

_forward_callback = None

# Per-workspace cancel flags
_cancelled: dict[int, bool] = {}


def _ws(ws=None) -> int:
    return dbm._resolve(ws)


# ── Compatibility shim ────────────────────────────────────────────────────────

def _client(ws=None):
    c = pool.listener_client(ws)
    if c is None:
        raise RuntimeError("No userbot account is logged in for you. Use /login first.")
    return c


class _ListenerProxy:
    """`ub.userbot.<anything>` → the current workspace's listener client."""

    def __getattr__(self, name):
        return getattr(_client(), name)

    def __bool__(self):
        try:
            return pool.listener_client() is not None
        except Exception:
            return False


userbot = _ListenerProxy()


# ── Cancellation (per workspace) ──────────────────────────────────────────────

def cancel_scan(ws=None):
    uid = _ws(ws)
    _cancelled[uid] = True
    try:
        from bot.processor import cancel_current_processing
        cancel_current_processing(uid)
    except Exception as e:
        print(f"[userbot] Could not cancel in-flight processing: {e}")


def reset_scan_cancel(ws=None):
    _cancelled[_ws(ws)] = False


def is_cancelled(ws=None) -> bool:
    try:
        return bool(_cancelled.get(_ws(ws), False))
    except Exception:
        return False


def set_forward_callback(fn):
    global _forward_callback
    _forward_callback = fn


# ── Pool lifecycle ────────────────────────────────────────────────────────────

async def init_client():
    """Connect every workspace's stored accounts."""
    return await pool.load_all()


async def is_authorized(ws=None) -> bool:
    for acc in pool.live_accounts(ws):
        try:
            if await acc.client.is_user_authorized():
                return True
        except Exception:
            continue
    return False


# ── Login (delegates to this workspace's pool) ────────────────────────────────

async def send_code(phone: str, ws=None) -> str:
    p = pool.get_pool(ws)
    await p.begin_login(phone)
    return p.pending_hash


async def sign_in(phone: str, code: str, phone_code_hash: str, ws=None):
    return await pool.complete_login_code(code, ws)


async def sign_in_2fa(password: str, ws=None):
    return await pool.complete_login_2fa(password, ws)


# ── Channel access ────────────────────────────────────────────────────────────

async def join_source_channel(source: str, client=None, ws=None):
    client = client or _client(ws)
    try:
        await client.get_dialogs()
        entity = await client.get_entity(str(source))
        await client(JoinChannelRequest(entity))
        print(f"[userbot] Joined/subscribed to channel: {source}")
    except Exception as e:
        print(f"[userbot] Note: could not join {source} ({e}) — may already be a member")


async def join_all_accounts(targets: list, ws=None):
    return await pool.join_everywhere([t for t in targets if t], ws)


async def _match_source_chat(event, cfg) -> bool:
    source = cfg.get("source_channel")
    if not source:
        return False

    chat_id = event.chat_id
    if cfg.get("debug_channel", False):
        print(f"[userbot][debug] msg from chat_id={chat_id}")

    source = str(source).strip()
    try:
        source_id = int(source)
    except (TypeError, ValueError):
        source_id = None

    if source_id and chat_id == source_id:
        return True

    chat = await event.get_chat()
    if hasattr(chat, "username") and chat.username:
        if source.lstrip("@") == chat.username.lstrip("@"):
            return True

    return False


# ── Listening (one listener task per workspace) ───────────────────────────────

async def begin_listening(ws):
    """Register event handlers on this workspace's listener account."""
    ws = int(ws)
    set_ws(ws)
    p = pool.get_pool(ws)
    acc = await p.listener()
    if acc is None or acc.client is None:
        raise RuntimeError(f"No listener account available for workspace {ws}.")

    client = acc.client
    print(f"[userbot:{ws}] Listener account: {acc.index}. {acc.label}")

    @client.on(events.NewMessage())
    async def on_new_message(event):
        set_ws(ws)
        if event.message.grouped_id:
            return

        cfg = await get_config(ws)
        if not cfg.get("active"):
            return
        if not await _match_source_chat(event, cfg):
            return

        if is_cancelled(ws):
            reset_scan_cancel(ws)

        print(f"[userbot:{ws}] New post in source — msg_id={event.message.id}")
        await log_event("new_post", {"msg_id": event.message.id, "chat_id": event.chat_id}, ws)

        links = _extract_links(event.message)
        if not links:
            return

        if _forward_callback:
            asyncio.create_task(_forward_callback(event.message, links, ws))

    @client.on(events.Album())
    async def on_new_album(event):
        set_ws(ws)
        cfg = await get_config(ws)
        if not cfg.get("active"):
            return
        if not await _match_source_chat(event, cfg):
            return

        if is_cancelled(ws):
            reset_scan_cancel(ws)

        messages = event.messages
        await log_event("new_post", {
            "msg_id": messages[0].id,
            "chat_id": event.chat_id,
            "album_size": len(messages),
        }, ws)

        links = []
        for m in messages:
            found = _extract_links(m)
            if found:
                links = found
                break
        if not links:
            return

        if _forward_callback:
            asyncio.create_task(_forward_callback(messages, links, ws))

    print(f"[userbot:{ws}] Listening for new posts.")
    await client.run_until_disconnected()


async def ensure_listener(ws) -> bool:
    """Start (or restart) the listener task for one workspace."""
    ws = int(ws)
    p = pool.get_pool(ws)
    if p.listener_task and not p.listener_task.done():
        return True
    if not p.live():
        return False

    async def runner():
        set_ws(ws)
        try:
            await begin_listening(ws)
        except Exception as e:
            print(f"[userbot:{ws}] listener stopped: {e}")

    p.listener_task = asyncio.ensure_future(runner())
    return True


async def restart_listening(ws=None):
    ws = _ws(ws)
    p = pool.get_pool(ws)
    if p.listener_task and not p.listener_task.done():
        p.listener_task.cancel()
        p.listener_task = None
    return await ensure_listener(ws)


async def start_all_listeners():
    started = 0
    for ws in await list_workspaces():
        if await ensure_listener(ws):
            started += 1
    return started


# ── Entity resolution ─────────────────────────────────────────────────────────

async def _resolve_entity(source: str, client=None, ws=None):
    client = client or _client(ws)
    source = str(source).strip()

    bare_id = None
    try:
        numeric_id = int(source)
        if numeric_id < -1000000000000:
            bare_id = int(str(abs(numeric_id))[3:])
        elif numeric_id < 0:
            bare_id = abs(numeric_id)
        else:
            bare_id = numeric_id
    except ValueError:
        pass

    try:
        return await client.get_entity(source)
    except Exception:
        pass

    print(f"[userbot] resolving: walking all dialogs to find {source}…")
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        eid = getattr(entity, "id", None)
        if eid is None:
            continue
        username = getattr(entity, "username", None) or ""
        source_clean = source.lstrip("@")

        if bare_id and eid == bare_id:
            return entity
        if username and username.lower() == source_clean.lower():
            return entity

    try:
        await join_source_channel(source, client)
        return await client.get_entity(source)
    except Exception as e:
        raise ValueError(
            f"Cannot resolve '{source}'. Make sure the account is a member. Error: {e}"
        )


def _group_by_album(messages: list) -> list:
    groups = []
    current = []
    current_gid = None
    for m in messages:
        gid = getattr(m, "grouped_id", None)
        if gid is not None and gid == current_gid:
            current.append(m)
        else:
            if current:
                groups.append(current)
            current = [m]
            current_gid = gid
    if current:
        groups.append(current)
    return groups


def _links_for_group(group: list) -> list:
    for m in group:
        found = _extract_links(m)
        if found:
            return found
    return []


# ── Scanning ──────────────────────────────────────────────────────────────────

async def scan_channel(source: str, callback, min_id: int = 0, limit: int = 0, ws=None) -> int:
    ws = _ws(ws)
    client = _client(ws)
    try:
        entity = await _resolve_entity(source, client, ws)
    except Exception as e:
        print(f"[userbot:{ws}] scan: {e}")
        return 0

    fetch_limit = limit if limit > 0 else None
    kwargs = dict(limit=fetch_limit)
    if min_id > 0:
        kwargs["min_id"] = min_id

    all_messages = []
    async for message in client.iter_messages(entity, **kwargs):
        all_messages.append(message)
    all_messages.reverse()

    groups = _group_by_album(all_messages)
    matched = [(g, l) for g in groups if (l := _links_for_group(g))]

    print(f"[userbot:{ws}] scan: {len(matched)} posts with links (oldest→newest)")

    reset_scan_cancel(ws)
    processed = 0
    for group, links in matched:
        if is_cancelled(ws):
            print(f"[userbot:{ws}] scan cancelled after {processed} posts")
            break
        if callback:
            await callback(group, links)
        processed += 1
    return processed


async def scan_recent(source: str, limit: int, callback, after_id: int = 0, ws=None) -> int:
    return await scan_channel(source, callback, min_id=after_id, limit=limit, ws=ws)


async def scan_range(source: str, start_id: int, end_id: int, callback, ws=None) -> int:
    ws = _ws(ws)
    client = _client(ws)
    try:
        entity = await _resolve_entity(source, client, ws)
    except Exception as e:
        print(f"[userbot:{ws}] scan_range: {e}")
        return 0

    all_messages = []
    async for message in client.iter_messages(entity, min_id=start_id - 1, max_id=end_id):
        all_messages.append(message)
    all_messages.reverse()

    groups = _group_by_album(all_messages)
    matched = [(g, l) for g in groups if (l := _links_for_group(g))]

    print(f"[userbot:{ws}] scan_range: {len(matched)} post(s) with links")

    reset_scan_cancel(ws)
    processed = 0
    for group, links in matched:
        if is_cancelled(ws):
            break
        if callback:
            await callback(group, links)
        processed += 1
    return processed


async def process_single(source: str, msg_id: int, callback, ws=None) -> bool:
    ws = _ws(ws)
    try:
        client = _client(ws)
        entity = await _resolve_entity(source, client, ws)
        messages = await client.get_messages(entity, ids=[msg_id])
        if not messages or not messages[0]:
            return False
        message = messages[0]

        group = [message]
        grouped_id = getattr(message, "grouped_id", None)
        if grouped_id is not None:
            window = await client.get_messages(entity, limit=40, min_id=msg_id - 10, max_id=msg_id + 10)
            siblings = [m for m in window if getattr(m, "grouped_id", None) == grouped_id]
            if siblings:
                siblings.sort(key=lambda m: m.id)
                group = siblings

        links = _links_for_group(group)
        if not links:
            return False
        if callback:
            await callback(group, links)
        return True
    except Exception as e:
        print(f"[userbot:{ws}] process_single error: {e}")
        return False


# ── Link extraction ───────────────────────────────────────────────────────────

TG_LINK_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/[^\s<>\"']+", re.I)
TG_RESOLVE_RE = re.compile(r"tg://resolve\?[^\s<>\"']+", re.I)
_TRAILING_JUNK = re.compile(r"[*_~`'\".),!?\]>]+$")
_BOT_DEEP_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/([A-Za-z][A-Za-z0-9_]{3,31})/?\?(?:.*&)?start=([^&\s]+)",
    re.I,
)
_TG_RESOLVE_DEEP_RE = re.compile(r"^tg://resolve\?(?=.*domain=([A-Za-z0-9_]+))(?=.*start=([^&\s]+))", re.I)


def _clean_url(url: str) -> str:
    return _TRAILING_JUNK.sub("", url.strip())


def normalize_bot_link(url: str) -> str | None:
    """Return 'https://t.me/<bot>?start=<param>' for bot deep links, else None.
    Invite links (t.me/+xxx, joinchat), channel/post links are ignored."""
    url = _clean_url(url or "")
    m = _BOT_DEEP_RE.match(url) or _TG_RESOLVE_DEEP_RE.match(url)
    if not m:
        return None
    return f"https://t.me/{m.group(1)}?start={m.group(2)}"


def _extract_links(message) -> list:
    text = (getattr(message, 'text', None) or
            getattr(message, 'message', None) or
            getattr(message, 'caption', None) or "")
    raw_text = getattr(message, 'raw_text', None) or getattr(message, 'message', None) or text

    candidates = []
    for entity in (getattr(message, "entities", None) or []):
        try:
            if isinstance(entity, MessageEntityTextUrl):
                candidates.append(entity.url)
            elif isinstance(entity, MessageEntityUrl):
                candidates.append(raw_text[entity.offset:entity.offset + entity.length])
        except Exception:
            pass
    for src in (raw_text, text):
        candidates += [m.group(0) for m in TG_LINK_RE.finditer(src or "")]
        candidates += [m.group(0) for m in TG_RESOLVE_RE.finditer(src or "")]
    try:
        rm = getattr(message, "reply_markup", None)
        for row in (getattr(rm, "rows", None) or []):
            for btn in row.buttons:
                if getattr(btn, "url", None):
                    candidates.append(btn.url)
    except Exception:
        pass

    seen = {}
    for c in candidates:
        n = normalize_bot_link(c)
        if n and n not in seen:
            seen[n] = True
    return list(seen.keys())


def _extract_link(message) -> str | None:
    links = _extract_links(message)
    return links[0] if links else None


async def click_bot_link_and_get_files(link: str, client=None, ws=None) -> list:
    client = client or _client(ws)
    import re as _re
    link = normalize_bot_link(link) or link
    deep_link_re = _re.compile(r"https://t\.me/([^?/]+)\?start=(.+)")
    m = deep_link_re.match(link)
    if not m:
        return []

    bot_username = m.group(1)
    start_param  = m.group(2)

    async with client.conversation(bot_username, timeout=30) as conv:
        await conv.send_message(f"/start {start_param}")
        resp = await conv.get_response()

        files = []
        if resp.media:
            files.append(resp)

        if resp.reply_markup:
            try:
                for row in resp.reply_markup.rows:
                    for btn in row.buttons:
                        if hasattr(btn, "data"):
                            answer = await client(GetBotCallbackAnswerRequest(
                                peer=bot_username,
                                msg_id=resp.id,
                                data=btn.data,
                            ))
                            if answer.message:
                                follow_resp = await conv.get_response()
                                if follow_resp.media:
                                    files.append(follow_resp)
            except Exception as e:
                print(f"[userbot] button click error: {e}")

        return files
