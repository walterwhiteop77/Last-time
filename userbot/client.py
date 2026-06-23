import asyncio
import re
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl
from telethon.tl.functions.messages import GetBotCallbackAnswerRequest
from telethon.errors import SessionPasswordNeededError

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import API_ID, API_HASH, SESSION_NAME, SESSION_STRING
from database import get_config, log_event

userbot: TelegramClient = None
_forward_callback = None

# Fired once login completes so main.py can start listening
login_done = asyncio.Event()

# Set to True to cancel any running scan
_scan_cancelled: bool = False


def cancel_scan():
    global _scan_cancelled
    _scan_cancelled = True


def reset_scan_cancel():
    global _scan_cancelled
    _scan_cancelled = False


def set_forward_callback(fn):
    global _forward_callback
    _forward_callback = fn


def init_client():
    """
    Create the TelegramClient.
    - If SESSION_STRING env var is set → use StringSession (persists across
      Render restarts without a file).
    - Otherwise fall back to a local .session file (good for local dev).
    """
    global userbot
    if SESSION_STRING:
        print("[userbot] Using SESSION_STRING (StringSession)")
        session = StringSession(SESSION_STRING)
    else:
        print("[userbot] Using file session:", SESSION_NAME)
        session = SESSION_NAME
    userbot = TelegramClient(session, API_ID, API_HASH)


async def connect():
    """Connect to Telegram without authenticating."""
    await userbot.connect()


async def is_authorized() -> bool:
    return await userbot.is_user_authorized()


async def send_code(phone: str) -> str:
    """Send OTP to the given phone number. Returns phone_code_hash."""
    result = await userbot.send_code_request(phone)
    return result.phone_code_hash


async def sign_in(phone: str, code: str, phone_code_hash: str):
    """
    Sign in with OTP. Raises SessionPasswordNeededError if 2FA is enabled.
    Returns the signed-in User object on success.
    """
    return await userbot.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)


async def sign_in_2fa(password: str):
    """Sign in with 2FA password."""
    return await userbot.sign_in(password=password)


async def join_source_channel(source: str):
    """Join / subscribe to the source channel so Telegram sends updates for it."""
    try:
        await userbot.get_dialogs()
        entity = await userbot.get_entity(source)
        from telethon.tl.functions.channels import JoinChannelRequest
        await userbot(JoinChannelRequest(entity))
        print(f"[userbot] Joined/subscribed to source channel: {source}")
    except Exception as e:
        print(f"[userbot] Note: could not join source channel ({e}) — may already be a member")


async def begin_listening():
    """Register event handlers and run until disconnected."""

    @userbot.on(events.NewMessage())
    async def on_new_message(event):
        cfg = await get_config()
        if not cfg.get("active"):
            return
        source = cfg.get("source_channel")
        if not source:
            return

        chat_id = event.chat_id

        if cfg.get("debug_channel", False):
            print(f"[userbot][debug] msg from chat_id={chat_id}")

        source = str(source).strip()
        try:
            source_id = int(source)
        except (TypeError, ValueError):
            source_id = None

        chat = await event.get_chat()

        match = False
        if source_id and chat_id == source_id:
            match = True
        elif hasattr(chat, "username") and chat.username:
            if source.lstrip("@") == chat.username.lstrip("@"):
                match = True

        if not match:
            return

        print(f"[userbot] ✅ New post in source channel — msg_id={event.message.id}")
        await log_event("new_post", {"msg_id": event.message.id, "chat_id": chat_id})

        link = _extract_link(event.message)
        if not link:
            print(f"[userbot] ⚠️ No link in post {event.message.id} — skipping")
            return

        print(f"[userbot] 🔗 Extracted link: {link}")

        if _forward_callback:
            asyncio.create_task(_forward_callback(event.message, link))

    print("[userbot] Authorized and listening for new posts.")
    await userbot.run_until_disconnected()


async def _resolve_entity(source: str):
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
        return await userbot.get_entity(source)
    except Exception:
        pass

    print(f"[userbot] resolving: walking all dialogs to find {source}…")
    async for dialog in userbot.iter_dialogs():
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
        await join_source_channel(source)
        return await userbot.get_entity(source)
    except Exception as e:
        raise ValueError(
            f"Cannot resolve '{source}'. Make sure the userbot is a member. Error: {e}"
        )


async def scan_channel(source: str, callback, min_id: int = 0, limit: int = 0) -> int:
    try:
        entity = await _resolve_entity(source)
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

    matched = []
    async for message in userbot.iter_messages(entity, **kwargs):
        link = _extract_link(message)
        if link:
            matched.append((message, link))

    matched.reverse()
    print(f"[userbot] scan: {len(matched)} posts with links (oldest→newest)")

    reset_scan_cancel()
    processed = 0
    for message, link in matched:
        if _scan_cancelled:
            print(f"[userbot] scan: ⛔ cancelled by /stop after {processed} posts")
            break
        print(f"[userbot] scan: msg {message.id} → {link}")
        if callback:
            await callback(message, link)
        processed += 1

    return processed


async def scan_range(source: str, start_id: int, end_id: int, callback) -> int:
    try:
        entity = await _resolve_entity(source)
    except Exception as e:
        print(f"[userbot] scan_range: {e}")
        return 0

    print(f"[userbot] scan_range: fetching messages {start_id}–{end_id}")
    matched = []
    async for message in userbot.iter_messages(entity, min_id=start_id - 1, max_id=end_id):
        link = _extract_link(message)
        if link:
            matched.append((message, link))

    matched.reverse()
    print(f"[userbot] scan_range: {len(matched)} post(s) with links")

    reset_scan_cancel()
    processed = 0
    for message, link in matched:
        if _scan_cancelled:
            print(f"[userbot] scan_range: ⛔ cancelled after {processed} posts")
            break
        print(f"[userbot] scan_range: msg {message.id} → {link}")
        if callback:
            await callback(message, link)
        processed += 1

    return processed


async def process_single(source: str, msg_id: int, callback) -> bool:
    try:
        entity = await _resolve_entity(source)
        messages = await userbot.get_messages(entity, ids=[msg_id])
        if not messages:
            return False
        message = messages[0]
        link = _extract_link(message)
        if not link:
            return False
        if callback:
            asyncio.create_task(callback(message, link))
        return True
    except Exception as e:
        print(f"[userbot] process_single error: {e}")
        return False


TG_LINK_RE = re.compile(r"https?://(?:t\.me|telegram\.me)/[^\s]+")
_TRAILING_JUNK = re.compile(r"[*_~`'\".),!?\]>]+$")


def _clean_url(url: str) -> str:
    return _TRAILING_JUNK.sub("", url)


def _extract_link(message) -> str | None:
    text = (getattr(message, 'text', None) or
            getattr(message, 'message', None) or
            getattr(message, 'caption', None) or "")

    if message.entities:
        for entity in message.entities:
            if isinstance(entity, MessageEntityTextUrl):
                url = _clean_url(entity.url)
                if TG_LINK_RE.match(url):
                    return url
            elif isinstance(entity, MessageEntityUrl):
                start = entity.offset
                end = entity.offset + entity.length
                url = _clean_url(text[start:end])
                if TG_LINK_RE.match(url):
                    return url

    m = TG_LINK_RE.search(text)
    if m:
        return _clean_url(m.group(0))

    any_url = re.compile(r"https?://[^\s]+")
    m2 = any_url.search(text)
    if m2:
        return m2.group(0)

    if message.reply_markup:
        try:
            for row in message.reply_markup.rows:
                for btn in row.buttons:
                    if hasattr(btn, "url") and btn.url:
                        return btn.url
        except Exception:
            pass

    return None


async def click_bot_link_and_get_files(link: str) -> list:
    import re as _re
    deep_link_re = _re.compile(r"https://t\.me/([^?/]+)\?start=(.+)")
    m = deep_link_re.match(link)
    if not m:
        return []

    bot_username = m.group(1)
    start_param = m.group(2)

    async with userbot.conversation(bot_username, timeout=30) as conv:
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
                            answer = await userbot(GetBotCallbackAnswerRequest(
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
