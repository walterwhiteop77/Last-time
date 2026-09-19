"""
Userbot layer — now backed by the multi-account pool in `userbot/pool.py`.

The *listener* account watches the source channel. Every heavy job (opening
bot links, downloading, uploading) is handed to a rotating worker account so
no single number carries all the traffic.
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

from database import get_config, log_event
import userbot.pool as pool

_forward_callback = None

# Fired once at least one account is logged in
login_done = pool.login_done

# Set to True to cancel any running scan / fbatch
_scan_cancelled: bool = False


# ── Compatibility shim ────────────────────────────────────────────────────────
# Older code (and several admin commands) referenced the module-level
# `userbot` client. It now resolves to the listener account's client.

def _client():
    c = pool.listener_client()
    if c is None:
        raise RuntimeError("No userbot account is logged in. Use /login first.")
    return c


class _ListenerProxy:
    """Attribute proxy so `ub.userbot.<anything>` keeps working."""

    def __getattr__(self, name):
        return getattr(_client(), name)

    def __bool__(self):
        return pool.listener_client() is not None


userbot = _ListenerProxy()


# ── Cancellation ──────────────────────────────────────────────────────────────

def cancel_scan():
    global _scan_cancelled
    _scan_cancelled = True
    try:
        from bot.processor import cancel_current_processing
        cancel_current_processing()
    except Exception as e:
        print(f"[userbot] Could not cancel in-flight processing: {e}")


def reset_scan_cancel():
    global _scan_cancelled
    _scan_cancelled = False


def set_forward_callback(fn):
    global _forward_callback
    _forward_callback = fn


# ── Pool lifecycle ────────────────────────────────────────────────────────────

async def init_client():
    """Connect every stored account (migrating any legacy single session)."""
    return await pool.load_pool()


async def connect():
    """Kept for backwards compatibility — load_pool() already connects."""
    return None


async def is_authorized() -> bool:
    for acc in pool.live_accounts():
        try:
            if await acc.client.is_user_authorized():
                return True
        except Exception:
            continue
    return False


# ── Login (delegates to the pool) ─────────────────────────────────────────────

async def send_code(phone: str) -> str:
    await pool.begin_login(phone)
    return pool.pending_hash


async def sign_in(phone: str, code: str, phone_code_hash: str):
    return await pool.complete_login_code(code)


async def sign_in_2fa(password: str):
    return await pool.complete_login_2fa(password)


# ── Channel access ────────────────────────────────────────────────────────────

async def join_source_channel(source: str, client=None):
    """Join / subscribe to a channel with one account (default: listener)."""
    client = client or _client()
    try:
        await client.get_dialogs()
        entity = await client.get_entity(str(source))
        await client(JoinChannelRequest(entity))
        print(f"[userbot] Joined/subscribed to channel: {source}")
    except Exception as e:
        print(f"[userbot] Note: could not join {source} ({e}) — may already be a member")


async def join_all_accounts(targets: list):
    return await pool.join_everywhere([t for t in targets if t])


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


async def begin_listening():
    """Register event handlers on the listener account and run forever."""
    acc = await pool.listener_account()
    if acc is None or acc.client is None:
        raise RuntimeError("No listener account available.")

    client = acc.client
    print(f"[userbot] Listener account: {acc.index}. {acc.label}")

    @client.on(events.NewMessage())
    async def on_new_message(event):
        if event.message.grouped_id:
            return

        cfg = await get_config()
        if not cfg.get("active"):
            return
        if not await _match_source_chat(event, cfg):
            return

        if _scan_cancelled:
            reset_scan_cancel()

        print(f"[userbot] New post in source channel — msg_id={event.message.id}")
        await log_event("new_post", {"msg_id": event.message.id, "chat_id": event.chat_id})

        links = _extract_links(event.message)
        if not links:
            print(f"[userbot] No links in post {event.message.id} — skipping")
            return

        print(f"[userbot] Extracted {len(links)} link(s): {links}")

        if _forward_callback:
            asyncio.create_task(_forward_callback(event.message, links))

    @client.on(events.Album())
    async def on_new_album(event):
        cfg = await get_config()
        if not cfg.get("active"):
            return
        if not await _match_source_chat(event, cfg):
            return

        if _scan_cancelled:
            reset_scan_cancel()

        messages = event.messages
        print(f"[userbot] New album — {len(messages)} item(s), first msg_id={messages[0].id}")
        await log_event("new_post", {
            "msg_id": messages[0].id,
            "chat_id": event.chat_id,
            "album_size": len(messages),
        })

        links = []
        for m in messages:
            found = _extract_links(m)
            if found:
                links = found
                break

        if not links:
            print(f"[userbot] No links in album {messages[0].id} — skipping")
            return

        print(f"[userbot] Extracted {len(links)} link(s) from album: {links}")

        if _forward_callback:
            asyncio.create_task(_forward_callback(messages, links))

    print("[userbot] Authorized and listening for new posts.")
    await client.run_until_disconnected()


async def restart_listening():
    """Used after /setlistener — reconnect handlers on the new listener."""
    asyncio.create_task(begin_listening())


# ── Entity resolution ─────────────────────────────────────────────────────────

async def _resolve_entity(source: str, client=None):
    client = client or _client()
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
            print(f"[userbot] found entity via dialog walk: {getattr(entity, 'title', eid)}")
            return entity
        if username and username.lower() == source_clean.lower():
            print(f"[userbot] found entity via username match: {username}")
            return entity

    try:
        print(f"[userbot] trying to join {source}…")
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

async def scan_channel(source: str, callback, min_id: int = 0, limit: int = 0) -> int:
    """Scan the source channel (oldest → newest) and process posts with links."""
    client = _client()
    try:
        entity = await _resolve_entity(source, client)
    except Exception as e:
        print(f"[userbot] scan: {e}")
        return 0

    fetch_limit = limit if limit > 0 else None
    kwargs = dict(limit=fetch_limit)
    if min_id > 0:
        kwargs["min_id"] = min_id
        print(f"[userbot] scan: fetching messages after ID {min_id}" + (f" (limit {limit})" if limit else ""))
    else:
        print(f"[userbot] scan: fetching last {limit} messages")

    all_messages = []
    async for message in client.iter_messages(entity, **kwargs):
        all_messages.append(message)
    all_messages.reverse()

    groups = _group_by_album(all_messages)
    matched = []
    for group in groups:
        links = _links_for_group(group)
        if links:
            matched.append((group, links))

    print(f"[userbot] scan: {len(matched)} posts with links (oldest→newest)")

    reset_scan_cancel()
    processed = 0
    for group, links in matched:
        if _scan_cancelled:
            print(f"[userbot] scan: cancelled by /stop after {processed} posts")
            break
        if callback:
            await callback(group, links)
        processed += 1
    return processed


# Backwards-compatible alias
async def scan_recent(source: str, limit: int, callback, after_id: int = 0) -> int:
    return await scan_channel(source, callback, min_id=after_id, limit=limit)


async def scan_range(source: str, start_id: int, end_id: int, callback) -> int:
    client = _client()
    try:
        entity = await _resolve_entity(source, client)
    except Exception as e:
        print(f"[userbot] scan_range: {e}")
        return 0

    print(f"[userbot] scan_range: fetching messages {start_id}–{end_id}")
    all_messages = []
    async for message in client.iter_messages(entity, min_id=start_id - 1, max_id=end_id):
        all_messages.append(message)
    all_messages.reverse()

    groups = _group_by_album(all_messages)
    matched = []
    for group in groups:
        links = _links_for_group(group)
        if links:
            matched.append((group, links))

    print(f"[userbot] scan_range: {len(matched)} post(s) with links")

    reset_scan_cancel()
    processed = 0
    for group, links in matched:
        if _scan_cancelled:
            print(f"[userbot] scan_range: cancelled by /stop after {processed} posts")
            break
        print(f"[userbot] scan_range: msg {group[0].id} ({len(group)} item(s)) → {links}")
        if callback:
            await callback(group, links)
        processed += 1

    return processed


async def process_single(source: str, msg_id: int, callback) -> bool:
    try:
        client = _client()
        entity = await _resolve_entity(source, client)
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
            asyncio.create_task(callback(group, links))
        return True
    except Exception as e:
        print(f"[userbot] process_single error: {e}")
        return False


# ── Link extraction ───────────────────────────────────────────────────────────

TG_LINK_RE = re.compile(r"https?://(?:t\.me|telegram\.me)/[^\s]+")
_TRAILING_JUNK = re.compile(r"[*_~`'\".),!?\]>]+$")


def _clean_url(url: str) -> str:
    return _TRAILING_JUNK.sub("", url)


def _extract_links(message) -> list:
    text = (getattr(message, 'text', None) or
            getattr(message, 'message', None) or
            getattr(message, 'caption', None) or "")

    seen = {}

    entities = getattr(message, "entities", None) or []
    if entities:
        for entity in entities:
            if isinstance(entity, MessageEntityTextUrl):
                url = _clean_url(entity.url)
                if TG_LINK_RE.match(url):
                    seen[url] = True
            elif isinstance(entity, MessageEntityUrl):
                start = entity.offset
                end   = entity.offset + entity.length
                url   = _clean_url(text[start:end])
                if TG_LINK_RE.match(url):
                    seen[url] = True

    for m in TG_LINK_RE.finditer(text):
        url = _clean_url(m.group(0))
        if url not in seen:
            seen[url] = True

    if not seen and getattr(message, "reply_markup", None):
        try:
            for row in message.reply_markup.rows:
                for btn in row.buttons:
                    if hasattr(btn, "url") and btn.url:
                        seen[btn.url] = True
        except Exception:
            pass

    return list(seen.keys())


def _extract_link(message) -> str | None:
    links = _extract_links(message)
    return links[0] if links else None


async def click_bot_link_and_get_files(link: str, client=None) -> list:
    client = client or _client()
    import re as _re
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
