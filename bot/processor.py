"""
Core automation logic — strictly sequential, rate-limit-safe.

Pipeline for each post:
  1. Extract deep links from source-channel post (1 or 2 links supported)
  2. For EACH link independently:
     a. Open link via userbot → collect files from linked bot
     b. Copy files to DB channel one by one with delays
     c. Build DB channel message links
     d. Ask second bot for a new shareable link
  3. Replace ALL original links with their new counterparts in the post HTML
  4. Strip @usernames / other t.me links (if filter enabled)
  5. Apply caption template (if set)
  6. Send ONE modified post to output channel
  7. Send summary to log channel (if set)
  8. Save mapping, wait before next post

Stop/disable behaviour:
  - _scan_cancelled flag is checked inside every sleep via _sleep_cancellable()
  - At most 0.5 s after /stop or /disable the current step will abort
"""

import asyncio
import re
from telethon.tl.types import (
    Message as TelethonMessage,
    MessageMediaPhoto,
    MessageMediaDocument,
    DocumentAttributeVideo,
    DocumentAttributeSticker,
    DocumentAttributeAnimated,
    DocumentAttributeAudio,
)
from telethon.errors import FloodWaitError
from telethon.extensions import html as tl_html

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from database import get_config, save_file_mapping, log_event

# ── Regex helpers ─────────────────────────────────────────────────────────────
TG_DEEP_LINK_RE = re.compile(
    r"https?://(?:t\.me|telegram\.me)/([^?/\s]+)\?start=([^\s]+)"
)
TG_PLAIN_RE = re.compile(
    r"https?://(?:t\.me|telegram\.me)/([^/\?\s]+)/?([^\s]*)$"
)
URL_RE    = re.compile(r"https?://[^\s]+")
TG_URL_RE = re.compile(r"https?://(?:t\.me|telegram\.me)/\S+")
AT_RE     = re.compile(r"@\w{3,}")

# ── Delay settings (seconds) ─────────────────────────────────────────────────
DELAY_BETWEEN_COPIES    = 4
DELAY_AFTER_COPY_BATCH  = 5
DELAY_CONVERSATION_STEP = 3
DELAY_BETWEEN_POSTS     = 8
DELAY_BETWEEN_LINKS     = 6

# Global semaphore: only ONE post processed at a time
_processing_lock = asyncio.Lock()


# ── Cancellation helpers ──────────────────────────────────────────────────────

def _is_cancelled() -> bool:
    """Check whether /stop or /disable has been requested."""
    try:
        import userbot.client as _ub
        return _ub._scan_cancelled
    except Exception:
        return False


async def _sleep_cancellable(seconds: float, step: float = 0.5):
    """
    Drop-in replacement for asyncio.sleep that respects the cancel flag.
    Wakes every `step` seconds to check; at most `step` s after /stop
    the current operation will abort.
    """
    elapsed = 0.0
    while elapsed < seconds:
        if _is_cancelled():
            return
        chunk = min(step, seconds - elapsed)
        await asyncio.sleep(chunk)
        elapsed += chunk


# ── Public entry point ────────────────────────────────────────────────────────

async def process_post(message: TelethonMessage, links, userbot, bot_app):
    """
    Process one source-channel post end-to-end.
    `links` may be a single URL string or a list of URL strings.
    Queues behind _processing_lock so concurrent calls serialise.
    """
    if isinstance(links, str):
        links = [links]

    async with _processing_lock:
        if _is_cancelled():
            print(f"[processor] /stop active — skipping post {message.id}")
            return
        await _process_post_inner(message, links, userbot, bot_app)
        await _sleep_cancellable(DELAY_BETWEEN_POSTS)


async def _process_post_inner(message: TelethonMessage, links: list, userbot, bot_app):
    cfg = await get_config()
    db_channel          = cfg.get("db_channel")
    output_channel      = cfg.get("output_channel")
    second_bot_username = cfg.get("second_bot_username")
    log_channel         = cfg.get("log_channel")
    caption_template    = cfg.get("caption_template") or ""
    strip_links         = cfg.get("strip_links", False)

    if not all([db_channel, output_channel, second_bot_username]):
        print(f"[processor] Missing config — skipping post {message.id}")
        return

    print(f"\n[processor] ══ Post {message.id} — {len(links)} link(s) ══")
    for i, lnk in enumerate(links, 1):
        print(f"[processor]    link {i}: {lnk}")

    try:
        db_ch = int(db_channel)
    except (ValueError, TypeError):
        db_ch = db_channel

    try:
        out_ch = int(output_channel)
    except (ValueError, TypeError):
        out_ch = output_channel

    # ── Process each link independently ──────────────────────────────────────
    link_replacements = []   # [(original_link, new_link), ...]
    all_db_msg_ids    = []

    for link_idx, original_link in enumerate(links):
        if _is_cancelled():
            print(f"[processor] /stop — aborting mid-post")
            return

        if link_idx > 0:
            await _sleep_cancellable(DELAY_BETWEEN_LINKS)
            if _is_cancelled():
                return

        print(f"\n[processor] ── Link {link_idx + 1}/{len(links)}: {original_link}")

        # Step A: get files from linked bot ───────────────────────────────────
        files = await _get_files_from_link(original_link, userbot)
        if not files:
            print(f"[processor] No files for {original_link} — skipping this link")
            await log_event("no_files", {"msg_id": message.id, "link": original_link})
            await _send_log(userbot, log_channel,
                            f"⚠️ *No files* for link `{original_link}` in post `{message.id}`")
            continue

        print(f"[processor] {len(files)} file(s) collected")

        if _is_cancelled():
            return

        # Step B: copy files to DB channel ────────────────────────────────────
        db_msg_ids = []
        for i, file_msg in enumerate(files):
            if _is_cancelled():
                return
            if i > 0:
                await _sleep_cancellable(DELAY_BETWEEN_COPIES)
                if _is_cancelled():
                    return
            msg_id = await _copy_to_db(userbot, db_ch, file_msg)
            if msg_id:
                db_msg_ids.append(msg_id)
                print(f"[processor] Copied file {i+1}/{len(files)} → DB msg {msg_id}")

        if not db_msg_ids:
            print(f"[processor] Nothing copied to DB for {original_link} — skipping")
            await _send_log(userbot, log_channel,
                            f"❌ *DB copy failed* for link `{original_link}` in post `{message.id}`")
            continue

        if _is_cancelled():
            return

        # Step C: build DB links ───────────────────────────────────────────────
        db_links = [_make_msg_link(db_channel, mid) for mid in db_msg_ids]
        print(f"[processor] DB links: {db_links}")
        all_db_msg_ids.extend(db_msg_ids)

        await _sleep_cancellable(DELAY_AFTER_COPY_BATCH)
        if _is_cancelled():
            return

        # Step D: generate new link ────────────────────────────────────────────
        new_link = await _generate_link(second_bot_username, db_links, userbot)
        if not new_link:
            print(f"[processor] Second bot returned no link for {original_link} — skipping")
            await log_event("link_gen_failed", {"msg_id": message.id, "db_msg_ids": db_msg_ids})
            await _send_log(userbot, log_channel,
                            f"❌ *Link generation failed* for post `{message.id}`\n"
                            f"DB msgs: `{db_msg_ids}`")
            continue

        print(f"[processor] New link: {new_link}")
        link_replacements.append((original_link, new_link))

    if not link_replacements:
        print(f"[processor] No links processed — aborting post {message.id}")
        return

    if _is_cancelled():
        return

    # ── Build and send output ─────────────────────────────────────────────────
    processed_html = _message_to_html(message)
    for original_link, new_link in link_replacements:
        processed_html = _replace_link_in_html(processed_html, original_link, new_link)

    if strip_links:
        all_new   = [nl for _, nl in link_replacements]
        all_orig  = [ol for ol, _ in link_replacements]
        processed_html = _apply_filter(processed_html, keep_urls=all_new + all_orig)

    final_html = _apply_template(processed_html, caption_template)

    first_new_link = link_replacements[0][1]
    await _send_to_output(message, final_html, first_new_link, out_ch, userbot)

    # ── Log summary ───────────────────────────────────────────────────────────
    pairs_text = "\n".join(f"  • `{ol}` → {nl}" for ol, nl in link_replacements)
    await _send_log(
        userbot, log_channel,
        f"✅ *Post processed*\n"
        f"• Source msg: `{message.id}`\n"
        f"• Links replaced: `{len(link_replacements)}`\n"
        f"• Files copied: `{len(all_db_msg_ids)}`\n"
        f"{pairs_text}",
    )

    # ── Persist mappings ──────────────────────────────────────────────────────
    for original_link, new_link in link_replacements:
        await save_file_mapping(message.id, original_link, all_db_msg_ids, new_link)
    await log_event("processed", {
        "msg_id":   message.id,
        "links":    [{"original": ol, "new": nl} for ol, nl in link_replacements],
        "db_msg_ids": all_db_msg_ids,
    })
    print(f"[processor] Post {message.id} complete ({len(link_replacements)} link(s) replaced)")


# ── Text helpers ──────────────────────────────────────────────────────────────

def _message_to_html(message: TelethonMessage) -> str:
    raw_text = getattr(message, 'message', None) or getattr(message, 'text', None) or ""
    entities = getattr(message, 'entities', None) or []
    try:
        if entities:
            return tl_html.unparse(raw_text, entities)
    except Exception:
        pass
    return raw_text


def _replace_link_in_html(html: str, original_link: str, new_link: str) -> str:
    """Replace one original_link with new_link inside an HTML string."""
    _junk = re.compile(r"[*_~`'\".),!?\]>]+$")
    original_link = _junk.sub("", original_link)

    path = re.sub(r"https?://(?:t\.me|telegram\.me)/", "", original_link)
    if not path:
        return html

    link_pattern = re.compile(
        r"https?://(?:t\.me|telegram\.me)/" + re.escape(path)
    )
    result = link_pattern.sub(new_link, html)
    if result == html:
        print(f"[processor] Link not found in HTML — no replacement: {original_link}")
    else:
        print(f"[processor] Replaced: {original_link} → {new_link}")
    return result


def _apply_filter(html: str, keep_urls: list) -> str:
    placeholders = {}
    for i, url in enumerate(keep_urls):
        if url and url in html:
            ph = f"%%KEEPURL{i}%%"
            placeholders[ph] = url
            html = html.replace(url, ph)
    html = TG_URL_RE.sub("", html)
    html = AT_RE.sub("", html)
    for ph, url in placeholders.items():
        html = html.replace(ph, url)
    html = re.sub(r" {2,}", " ", html)
    html = re.sub(r"\n{3,}", "\n\n", html)
    return html.strip()


def _apply_template(text: str, template: str) -> str:
    if not template:
        return text
    if "{text}" in template:
        return template.replace("{text}", text)
    return f"{text}\n\n{template}" if text else template


# ── DB channel helpers ────────────────────────────────────────────────────────

def _make_msg_link(db_channel: str, msg_id: int) -> str:
    ch = str(db_channel).strip()
    if ch.startswith("-100"):
        bare = ch[4:]
        return f"https://t.me/c/{bare}/{msg_id}"
    elif ch.startswith("-"):
        bare = ch[1:]
        return f"https://t.me/c/{bare}/{msg_id}"
    else:
        username = ch.lstrip("@")
        return f"https://t.me/{username}/{msg_id}"


def _is_photo_or_video(media) -> bool:
    if media is None:
        return False
    if isinstance(media, MessageMediaPhoto):
        return True
    if isinstance(media, MessageMediaDocument):
        attrs     = getattr(media.document, "attributes", [])
        attr_types = {type(a) for a in attrs}
        if DocumentAttributeVideo not in attr_types:
            return False
        if attr_types & {DocumentAttributeSticker, DocumentAttributeAnimated, DocumentAttributeAudio}:
            return False
        return True
    return False


async def _copy_to_db(userbot, db_ch, file_msg) -> int | None:
    """
    Copy one photo/video to the DB channel.
    Retries up to 3 times on FloodWaitError; skips non-photo/video media.
    """
    if not _is_photo_or_video(file_msg.media):
        kind = type(file_msg.media).__name__ if file_msg.media else "text"
        print(f"[processor] Skipping non-photo/video media: {kind}")
        return None

    text = (
        getattr(file_msg, 'text', None) or
        getattr(file_msg, 'message', None) or
        getattr(file_msg, 'caption', None) or ""
    )

    for attempt in range(3):
        try:
            sent = await userbot.send_file(
                db_ch,
                file=file_msg.media,
                caption=text or None,
                parse_mode="html",
            )
            return sent.id
        except FloodWaitError as e:
            wait = e.seconds + 5
            print(f"[processor] FloodWait on DB copy — waiting {wait}s (attempt {attempt + 1}/3)")
            await _sleep_cancellable(wait)
            if _is_cancelled():
                return None
        except Exception as e:
            print(f"[processor] DB copy failed (attempt {attempt + 1}/3): {e}")
            if attempt < 2:
                await _sleep_cancellable(10)
            else:
                return None
    return None


# ── File collection ───────────────────────────────────────────────────────────

async def _get_files_from_link(link: str, userbot) -> list:
    """Open a bot deep link as userbot and collect all file messages."""
    m = TG_DEEP_LINK_RE.match(link)
    if m:
        bot_username = m.group(1)
        start_param  = m.group(2)
    else:
        pm = TG_PLAIN_RE.match(link)
        if not pm:
            print(f"[processor] Unknown link pattern: {link}")
            return []
        bot_username = pm.group(1)
        start_param  = pm.group(2) or ""

    print(f"[processor] Opening @{bot_username} start='{start_param}'")
    files = []
    try:
        async with userbot.conversation(bot_username, timeout=60) as conv:
            cmd = f"/start {start_param}" if start_param else "/start"
            await conv.send_message(cmd)
            print(f"[processor]   → sent: {cmd}")

            deadline = asyncio.get_event_loop().time() + 20
            while asyncio.get_event_loop().time() < deadline:
                if _is_cancelled():
                    break
                try:
                    resp = await asyncio.wait_for(conv.get_response(), timeout=5)
                    has_media = bool(resp.media)
                    preview   = repr((getattr(resp, 'text', '') or '')[:80])
                    print(f"[processor]   ← media={has_media} text={preview}")
                    if has_media:
                        files.append(resp)
                except asyncio.TimeoutError:
                    print(f"[processor]   → silence — bot finished")
                    break
    except FloodWaitError as e:
        wait = e.seconds + 5
        print(f"[processor] FloodWait on conversation — waiting {wait}s then retrying once")
        await _sleep_cancellable(wait)
        if not _is_cancelled():
            return await _get_files_from_link(link, userbot)
    except Exception as e:
        print(f"[processor] Conversation with @{bot_username} failed: {e}")

    print(f"[processor] Files from @{bot_username}: {len(files)}")
    return files


# ── Link generation ───────────────────────────────────────────────────────────

async def _generate_link(bot_username: str, db_links: list, userbot) -> str | None:
    if not db_links:
        return None
    bot = bot_username.lstrip("@")
    if len(db_links) == 1:
        return await _genlink_single(bot, db_links[0], userbot)
    return await _batch_conversational(bot, db_links, userbot)


async def _genlink_single(bot: str, link: str, userbot) -> str | None:
    print(f"[processor] → @{bot}: /genlink")
    try:
        async with userbot.conversation(bot, timeout=60) as conv:
            await conv.send_message("/genlink")
            await _sleep_cancellable(DELAY_CONVERSATION_STEP)
            if _is_cancelled():
                return None

            resp1 = await asyncio.wait_for(conv.get_response(), timeout=20)
            print(f"[processor]   ← {repr(_msg_text(resp1)[:100])}")

            await _sleep_cancellable(DELAY_CONVERSATION_STEP)
            if _is_cancelled():
                return None

            await conv.send_message(link)

            await _sleep_cancellable(DELAY_CONVERSATION_STEP)
            if _is_cancelled():
                return None

            resp2 = await asyncio.wait_for(conv.get_response(), timeout=30)
            print(f"[processor]   ← final: {repr(_msg_text(resp2)[:120])}")

            url = _extract_url_from_response(resp2)
            if url:
                return url

            try:
                await _sleep_cancellable(2)
                resp3 = await asyncio.wait_for(conv.get_response(), timeout=10)
                return _extract_url_from_response(resp3)
            except asyncio.TimeoutError:
                pass

    except FloodWaitError as e:
        wait = e.seconds + 5
        print(f"[processor] FloodWait on /genlink — waiting {wait}s")
        await _sleep_cancellable(wait)
    except Exception as e:
        print(f"[processor] /genlink failed: {e}")
    return None


async def _batch_conversational(bot: str, db_links: list, userbot) -> str | None:
    first_link = db_links[0]
    last_link  = db_links[-1]

    print(f"[processor] → @{bot}: /batch  first={first_link}  last={last_link}")
    try:
        async with userbot.conversation(bot, timeout=90) as conv:
            await conv.send_message("/batch")
            await _sleep_cancellable(DELAY_CONVERSATION_STEP)
            if _is_cancelled():
                return None

            resp1 = await asyncio.wait_for(conv.get_response(), timeout=20)
            print(f"[processor]   ← {repr(_msg_text(resp1)[:100])}")

            await _sleep_cancellable(DELAY_CONVERSATION_STEP)
            if _is_cancelled():
                return None
            await conv.send_message(first_link)

            await _sleep_cancellable(DELAY_CONVERSATION_STEP)
            if _is_cancelled():
                return None

            resp2 = await asyncio.wait_for(conv.get_response(), timeout=20)
            print(f"[processor]   ← {repr(_msg_text(resp2)[:100])}")

            await _sleep_cancellable(DELAY_CONVERSATION_STEP)
            if _is_cancelled():
                return None
            await conv.send_message(last_link)

            await _sleep_cancellable(DELAY_CONVERSATION_STEP)
            if _is_cancelled():
                return None

            resp3 = await asyncio.wait_for(conv.get_response(), timeout=30)
            print(f"[processor]   ← final: {repr(_msg_text(resp3)[:120])}")

            url = _extract_url_from_response(resp3)
            if url:
                return url

            try:
                await _sleep_cancellable(2)
                resp4 = await asyncio.wait_for(conv.get_response(), timeout=10)
                return _extract_url_from_response(resp4)
            except asyncio.TimeoutError:
                pass

    except asyncio.TimeoutError:
        print(f"[processor] /batch timed out")
    except FloodWaitError as e:
        wait = e.seconds + 5
        print(f"[processor] FloodWait on /batch — waiting {wait}s")
        await _sleep_cancellable(wait)
    except Exception as e:
        print(f"[processor] /batch failed: {e}")
    return None


# ── Output helpers ────────────────────────────────────────────────────────────

def _msg_text(msg) -> str:
    return (
        getattr(msg, 'text', None) or
        getattr(msg, 'message', None) or
        getattr(msg, 'caption', None) or ""
    )


def _extract_url_from_response(resp) -> str | None:
    text  = _msg_text(resp)
    match = URL_RE.search(text)
    if match:
        return match.group(0)
    if resp.reply_markup:
        try:
            for row in resp.reply_markup.rows:
                for btn in row.buttons:
                    if hasattr(btn, "url") and btn.url:
                        return btn.url
        except Exception:
            pass
    print(f"[processor] No URL in response: {repr(text[:80])}")
    return None


async def _send_to_output(original_msg, html_text: str, new_link: str, output_channel, userbot):
    """
    Send the processed post to the output channel.
    Retries up to 3 times on FloodWait.
    Never falls back to a text-only message when the original had media —
    that would produce a broken/distorted post.
    """
    print(f"[processor] Sending to output channel {output_channel}")
    has_media = bool(original_msg.media)

    for attempt in range(3):
        if _is_cancelled():
            return
        try:
            if has_media:
                await userbot.send_file(
                    output_channel,
                    file=original_msg.media,
                    caption=html_text or None,
                    parse_mode="html",
                )
            else:
                await userbot.send_message(
                    output_channel,
                    html_text,
                    parse_mode="html",
                    link_preview=True,
                )
            print("[processor] Sent to output channel")
            return
        except FloodWaitError as e:
            wait = e.seconds + 5
            print(f"[processor] FloodWait on output send — waiting {wait}s (attempt {attempt + 1}/3)")
            await _sleep_cancellable(wait)
        except Exception as e:
            print(f"[processor] Output send failed (attempt {attempt + 1}/3): {e}")
            if attempt < 2:
                await _sleep_cancellable(15)
            else:
                print(f"[processor] All output attempts failed — post {original_msg.id} skipped")


async def _send_log(userbot, log_channel, text: str):
    if not log_channel:
        return
    try:
        lc = int(log_channel)
    except (ValueError, TypeError):
        lc = log_channel
    try:
        await userbot.send_message(lc, text, parse_mode="md")
    except Exception as e:
        print(f"[processor] Log channel send failed: {e}")
