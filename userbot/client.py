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

# Set to True to cancel any running scan / fbatch
_scan_cancelled: bool = False


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


async def init_client():
    """
    Create the TelegramClient.
    Priority order for session:
      1. SESSION_STRING env var  (set explicitly by operator)
      2. session_string stored in MongoDB  (saved automatically after /login)
      3. Local .session file  (fallback for local dev)
    This ensures the userbot stays logged in across server restarts.
    """
    global userbot
    session = None

    if SESSION_STRING:
        print("[userbot] Using SESSION_STRING env var (StringSession)")
        session = StringSession(SESSION_STRING)
    else:
        try:
            from database import get_session_string
            stored = await get_session_string()
            if stored:
                session = StringSession(stored)
                print("[userbot] Loaded session string from MongoDB — no re-login needed")
            else:
                print("[userbot] No stored session — using file session:", SESSION_NAME)
                session = SESSION_NAME
        except Exception as e:
            print(f"[userbot] Could not read session from MongoDB ({e}) — using file session")
            session = SESSION_NAME

    userbot = TelegramClient(session, API_ID, API_HASH)


async def _save_session_to_db():
    """
    Export the current in-memory session as a string and store it in MongoDB.
    Called automatically after every successful /login so restarts don't need re-auth.
    """
    try:
        from database import save_session_string
        session_str = StringSession.save(userbot.session)
        await save_session_string(session_str)
        print("[userbot] Session string saved to MongoDB — future restarts will not require re-login")
    except Exception as e:
        print(f"[userbot] Could not save session to MongoDB: {e}")


async def connect():
    """Connect to Telegram without authenticating."""
    await userbot.connect()


async def is_authorized() -> bool:
    return await userbot.is_user_authorized()


async def _watch_for_otp(timeout: int = 300) -> None:
    """
    Background task: listens for the Telegram OTP message (sent by sender 777000)
    and prints the code to stdout so it appears in Render logs.
    Automatically stops after `timeout` seconds or once a code is found.
    """
    OTP_SENDER = 777000          # Telegram's official account that sends login codes
    OTP_RE = re.compile(r"\b(\d{5,6})\b")  # OTP is always 5-6 digits

    found = asyncio.Event()

    async def _handler(event):
        try:
            sender_id = event.sender_id
        except Exception:
            sender_id = None

        if sender_id != OTP_SENDER:
            return

        text = event.raw_text or ""
        match = OTP_RE.search(text)
        code = match.group(1) if match else None

        print("=" * 60)
        print("[userbot][OTP] *** TELEGRAM LOGIN CODE RECEIVED ***")
        if code:
            print(f"[userbot][OTP] Code: {code}")
        print(f"[userbot][OTP] Full message: {text.strip()}")
        print("=" * 60)

        found.set()

    userbot.add_event_handler(_handler, events.NewMessage(incoming=True))

    try:
        await asyncio.wait_for(found.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        print(f"[userbot][OTP] Watcher timed out after {timeout}s — no OTP received.")
    finally:
        userbot.remove_event_handler(_handler, events.NewMessage(incoming=True))


async def send_code(phone: str) -> str:
    """Send OTP to the given phone number. Returns phone_code_hash.
    Also spawns a background task that watches for the incoming OTP message
    from Telegram and prints the code to stdout (visible in Render logs).
    """
    result = await userbot.send_code_request(phone)
    # Start watcher in the background — it will print the code as soon as
    # Telegram delivers it to the userbot account.
    asyncio.create_task(_watch_for_otp(), name="otp-watcher")
    print("[userbot][OTP] Watcher started — waiting for OTP from Telegram (up to 5 min)…")
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


async def _match_source_chat(event, cfg) -> bool:
    """Return True if this event's chat matches the configured source channel."""
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
    """Register event handlers and run until disconnected."""

    @userbot.on(events.NewMessage())
    async def on_new_message(event):
        # Messages that belong to an album (grouped_id set) are handled
        # together by the Album handler below, so all photos get sent —
        # skip them here to avoid processing the same post twice.
        if event.message.grouped_id:
            return

        cfg = await get_config()
        if not cfg.get("active"):
            return
        if not await _match_source_chat(event, cfg):
            return

        print(f"[userbot] New post in source channel — msg_id={event.message.id}")
        await log_event("new_post", {"msg_id": event.message.id, "chat_id": event.chat_id})

        links = _extract_links(event.message)
        if not links:
            print(f"[userbot] No links in post {event.message.id} — skipping")
            return

        print(f"[userbot] Extracted {len(links)} link(s): {links}")

        if _forward_callback:
            asyncio.create_task(_forward_callback(event.message, links))

    @userbot.on(events.Album())
    async def on_new_album(event):
        cfg = await get_config()
        if not cfg.get("active"):
            return
        if not await _match_source_chat(event, cfg):
            return

        messages = event.messages
        print(f"[userbot] New album in source channel — {len(messages)} photo(s), first msg_id={messages[0].id}")
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


def _group_by_album(messages: list) -> list:
    """
    Group a chronologically-ordered list of messages so that consecutive
    messages sharing the same non-null grouped_id (i.e. an album/media
    group with multiple photos) end up together. Returns a list of groups,
    where each group is itself a list of one or more messages.
    """
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
    """Return links found on ANY message in the group (albums usually carry
    the caption/links on just one message of the group)."""
    for m in group:
        found = _extract_links(m)
        if found:
            return found
    return []


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

    all_messages = []
    async for message in userbot.iter_messages(entity, **kwargs):
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
        print(f"[userbot] scan: msg {group[0].id} ({len(group)} photo(s)) → {links}")
        if callback:
            await callback(group, links)
        processed += 1

    return processed


async def scan_range(source: str, start_id: int, end_id: int, callback) -> int:
    try:
        entity = await _resolve_entity(source)
    except Exception as e:
        print(f"[userbot] scan_range: {e}")
        return 0

    print(f"[userbot] scan_range: fetching messages {start_id}–{end_id}")
    all_messages = []
    async for message in userbot.iter_messages(entity, min_id=start_id - 1, max_id=end_id):
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
        print(f"[userbot] scan_range: msg {group[0].id} ({len(group)} photo(s)) → {links}")
        if callback:
            await callback(group, links)
        processed += 1

    return processed


async def process_single(source: str, msg_id: int, callback) -> bool:
    try:
        entity = await _resolve_entity(source)
        messages = await userbot.get_messages(entity, ids=[msg_id])
        if not messages or not messages[0]:
            return False
        message = messages[0]

        group = [message]
        grouped_id = getattr(message, "grouped_id", None)
        if grouped_id is not None:
            # Fetch a small window around the message to pick up its album siblings.
            window = await userbot.get_messages(entity, min_id=msg_id - 10, max_id=msg_id + 10)
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


TG_LINK_RE = re.compile(r"https?://(?:t\.me|telegram\.me)/[^\s]+")
_TRAILING_JUNK = re.compile(r"[*_~`'\".),!?\]>]+$")


def _clean_url(url: str) -> str:
    return _TRAILING_JUNK.sub("", url)


def _extract_links(message) -> list:
    """
    Extract ALL Telegram bot/deep links from a message.
    Returns a deduplicated list of URL strings in order found.
    """
    text = (getattr(message, 'text', None) or
            getattr(message, 'message', None) or
            getattr(message, 'caption', None) or "")

    seen = {}

    if message.entities:
        for entity in message.entities:
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

    if not seen and message.reply_markup:
        try:
            for row in message.reply_markup.rows:
                for btn in row.buttons:
                    if hasattr(btn, "url") and btn.url:
                        seen[btn.url] = True
        except Exception:
            pass

    return list(seen.keys())


def _extract_link(message) -> str | None:
    """Legacy single-link helper — returns the first link found, or None."""
    links = _extract_links(message)
    return links[0] if links else None


async def click_bot_link_and_get_files(link: str) -> list:
    import re as _re
    deep_link_re = _re.compile(r"https://t\.me/([^?/]+)\?start=(.+)")
    m = deep_link_re.match(link)
    if not m:
        return []

    bot_username = m.group(1)
    start_param  = m.group(2)

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
