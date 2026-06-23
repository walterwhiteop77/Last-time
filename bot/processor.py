"""
Core automation logic — strictly sequential, rate-limit-safe.

Pipeline for each post:
  1. Extract deep link from source-channel post
  2. Open link via userbot → collect files from linked bot
  3. Copy files (no forward tag) to DB channel, one by one with delays
  4. Build DB channel message links
  5. Ask second bot for a new link:
       Single file  → /genlink <link>
       Multiple     → conversational /batch:
                        send /batch → bot asks → send first link
                        bot asks → send last link → bot replies with new link
  6. Strip @usernames / other t.me links (if filter enabled)
  7. Apply caption template (if set)
  8. Send modified post to output channel preserving original formatting
  9. Send summary to log channel (if set)
 10. Save mapping, wait before next post
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
AT_RE     = re.compile(r"@\w{3,}")   # @usernames (≥3 chars to avoid stray @)

# ── Delay settings (seconds) — tune here ─────────────────────────────────────
DELAY_BETWEEN_COPIES    = 4
DELAY_AFTER_COPY_BATCH  = 5
DELAY_CONVERSATION_STEP = 3
DELAY_BETWEEN_POSTS     = 8

# Global semaphore: only ONE post processed at a time
_processing_lock = asyncio.Lock()


# ── Public entry point ────────────────────────────────────────────────────────

def _is_cancelled() -> bool:
    """Check whether /stop has been requested."""
    try:
        import userbot.client as _ub
        return _ub._scan_cancelled
    except Exception:
        return False


async def process_post(message: TelethonMessage, original_link: str, userbot, bot_app):
    """
    Process one source-channel post end-to-end.
    Acquires _processing_lock so concurrent calls queue up instead of racing.
    """
    async with _processing_lock:
        if _is_cancelled():
            print(f"[processor] ⛔ /stop active — skipping post {message.id}")
            return
        await _process_post_inner(message, original_link, userbot, bot_app)
        await asyncio.sleep(DELAY_BETWEEN_POSTS)


async def _process_post_inner(message: TelethonMessage, original_link: str, userbot, bot_app):
    cfg = await get_config()
    db_channel          = cfg.get("db_channel")
    output_channel      = cfg.get("output_channel")
    second_bot_username = cfg.get("second_bot_username")
    log_channel         = cfg.get("log_channel")
    caption_template    = cfg.get("caption_template") or ""
    strip_links         = cfg.get("strip_links", False)

    if not all([db_channel, output_channel, second_bot_username]):
        print(f"[processor] ⚠️ Missing config — db:{db_channel} out:{output_channel} bot:{second_bot_username}")
        return

    print(f"\n[processor] ══ Post {message.id} ══")
    print(f"[processor]    link: {original_link}")

    # ── Step 1: get files from linked bot ─────────────────────────────────────
    files = await _get_files_from_link(original_link, userbot)
    if not files:
        print(f"[processor] ⚠️ No files — skipping post {message.id}")
        await log_event("no_files", {"msg_id": message.id, "link": original_link})
        await _send_log(userbot, log_channel,
                        f"⚠️ *No files* returned for post `{message.id}`\nLink: `{original_link}`")
        return
    print(f"[processor] ✅ {len(files)} file(s) collected")

    if _is_cancelled():
        print(f"[processor] ⛔ /stop — aborting after file fetch")
        return

    # ── Step 2: copy files to DB channel one by one ───────────────────────────
    try:
        db_ch = int(db_channel)
    except (ValueError, TypeError):
        db_ch = db_channel

    db_msg_ids = []
    for i, file_msg in enumerate(files):
        if _is_cancelled():
            print(f"[processor] ⛔ /stop — aborting mid-copy")
            return
        if i > 0:
            print(f"[processor] ⏳ Waiting {DELAY_BETWEEN_COPIES}s before next copy…")
            await asyncio.sleep(DELAY_BETWEEN_COPIES)
        msg_id = await _copy_to_db(userbot, db_ch, file_msg)
        if msg_id:
            db_msg_ids.append(msg_id)
            print(f"[processor] ✅ Copied file {i+1}/{len(files)} → DB msg {msg_id}")

    if not db_msg_ids:
        print("[processor] ❌ Nothing copied to DB channel — aborting")
        await _send_log(userbot, log_channel,
                        f"❌ *DB copy failed* for post `{message.id}` — no files stored")
        return

    if _is_cancelled():
        print(f"[processor] ⛔ /stop — aborting before link generation")
        return

    # ── Step 3: build DB channel message links ────────────────────────────────
    db_links = [_make_msg_link(db_channel, mid) for mid in db_msg_ids]
    print(f"[processor] 🔗 DB links: {db_links}")

    print(f"[processor] ⏳ Waiting {DELAY_AFTER_COPY_BATCH}s before second bot…")
    await asyncio.sleep(DELAY_AFTER_COPY_BATCH)

    # ── Step 4: generate new link from second bot ─────────────────────────────
    new_link = await _generate_link(second_bot_username, db_links, userbot)
    if not new_link:
        print(f"[processor] ❌ Second bot returned no link — aborting")
        await log_event("link_gen_failed", {"msg_id": message.id, "db_msg_ids": db_msg_ids})
        await _send_log(userbot, log_channel,
                        f"❌ *Link generation failed* for post `{message.id}`\n"
                        f"DB msgs: `{db_msg_ids}`")
        return
    print(f"[processor] ✅ New link: {new_link}")

    if _is_cancelled():
        print(f"[processor] ⛔ /stop — aborting before output send")
        return

    # ── Step 5: build output text preserving original HTML formatting ─────────
    # Replace the original link with the new one — works on both entity hrefs
    # and plain-text occurrences, and handles t.me ↔ telegram.me differences.
    processed_html = _replace_link(message, original_link, new_link)

    # Strip @usernames and OTHER t.me links if filter is on.
    if strip_links:
        processed_html = _apply_filter(processed_html, keep_urls=[new_link, original_link])

    # Apply caption template
    final_html = _apply_template(processed_html, caption_template)

    # ── Step 6: send to output channel ────────────────────────────────────────
    try:
        out_ch = int(output_channel)
    except (ValueError, TypeError):
        out_ch = output_channel

    await _send_to_output(message, final_html, new_link, out_ch, userbot)

    # ── Step 7: log channel summary ───────────────────────────────────────────
    await _send_log(
        userbot, log_channel,
        f"✅ *Post processed*\n"
        f"• Source msg: `{message.id}`\n"
        f"• Files: `{len(db_msg_ids)}`\n"
        f"• Old link: `{original_link}`\n"
        f"• New link: {new_link}",
    )

    # ── Step 8: persist mapping ───────────────────────────────────────────────
    await save_file_mapping(message.id, original_link, db_msg_ids, new_link)
    await log_event("processed", {
        "msg_id": message.id,
        "original_link": original_link,
        "new_link": new_link,
        "db_msg_ids": db_msg_ids,
    })
    print(f"[processor] ✅ Post {message.id} complete")


# ── Text helpers ──────────────────────────────────────────────────────────────

def _message_to_html(message: TelethonMessage) -> str:
    """
    Reconstruct the message text as HTML, preserving bold/italic/code/links etc.
    Falls back to plain text if no entities.
    """
    raw_text = getattr(message, 'message', None) or getattr(message, 'text', None) or ""
    entities = getattr(message, 'entities', None) or []
    try:
        if entities:
            return tl_html.unparse(raw_text, entities)
    except Exception:
        pass
    return raw_text


def _normalize_tg_url(url: str) -> str:
    """Normalize t.me ↔ telegram.me and http ↔ https for comparison."""
    return (url
            .replace("http://", "https://")
            .replace("https://telegram.me/", "https://t.me/"))


def _replace_link(message: TelethonMessage, original_link: str, new_link: str) -> str:
    """
    Replace original_link with new_link in the message, producing HTML output.

    Approach: unparse the message to HTML first (preserves all formatting), then
    regex-replace the URL directly in the HTML string.  This handles every case:
    - Plain-text URLs (shown as-is in the message)
    - href="..." attributes produced by MessageEntityUrl / MessageEntityTextUrl
    - Both t.me and telegram.me URL variants
    - URL length changes that would break entity byte-offsets if we re-unparsed
    """
    # Step 1: get the full HTML with all formatting intact
    html = _message_to_html(message)

    # Strip any trailing Markdown/punctuation that may have been grabbed with the URL
    _junk = re.compile(r"[*_~`'\".),!?\]>]+$")
    original_link = _junk.sub("", original_link)

    # Step 2: build a pattern from the path after the domain, matching both domains
    path = re.sub(r"https?://(?:t\.me|telegram\.me)/", "", original_link)
    if not path:
        return html  # nothing to replace

    link_pattern = re.compile(
        r"https?://(?:t\.me|telegram\.me)/" + re.escape(path)
    )

    # Step 3: replace everywhere in the HTML (href attributes AND visible text)
    result = link_pattern.sub(new_link, html)

    if result == html:
        print(f"[processor] ⚠️ original link not found in HTML — sending as-is")
    else:
        print(f"[processor] ✅ link replaced in output HTML")

    return result


def _apply_filter(html: str, keep_urls: list) -> str:
    """
    Remove @username mentions and t.me/telegram.me links from the text,
    but preserve every URL in keep_urls (new generated link + original file-bot link).
    """
    # Replace each protected URL with a numbered placeholder so the regex
    # doesn't touch them.
    placeholders = {}
    for i, url in enumerate(keep_urls):
        if url and url in html:
            ph = f"%%KEEPURL{i}%%"
            placeholders[ph] = url
            html = html.replace(url, ph)

    # Strip all remaining t.me/telegram.me links
    html = TG_URL_RE.sub("", html)

    # Strip @usernames
    html = AT_RE.sub("", html)

    # Restore protected URLs
    for ph, url in placeholders.items():
        html = html.replace(ph, url)

    # Clean up leftover whitespace
    html = re.sub(r" {2,}", " ", html)
    html = re.sub(r"\n{3,}", "\n\n", html)
    return html.strip()


def _apply_template(text: str, template: str) -> str:
    """
    Apply the caption template.
    If template contains {text}, replace it; otherwise append template after text.
    """
    if not template:
        return text
    if "{text}" in template:
        return template.replace("{text}", text)
    # No placeholder — append template below the original text
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
    """Return True only for photos and real video files. Reject everything else."""
    if media is None:
        return False
    if isinstance(media, MessageMediaPhoto):
        return True
    if isinstance(media, MessageMediaDocument):
        attrs = getattr(media.document, "attributes", [])
        attr_types = {type(a) for a in attrs}
        # Must have a video attribute …
        if DocumentAttributeVideo not in attr_types:
            return False
        # … and must NOT be a sticker, animated GIF, or audio
        if attr_types & {DocumentAttributeSticker, DocumentAttributeAnimated, DocumentAttributeAudio}:
            return False
        return True
    return False


async def _copy_to_db(userbot, db_ch, file_msg) -> int | None:
    """Copy one photo/video message to the DB channel without the Forwarded-from tag.
    Stickers, audio, documents, GIFs and text-only messages are silently skipped."""
    if not _is_photo_or_video(file_msg.media):
        kind = type(file_msg.media).__name__ if file_msg.media else "text"
        print(f"[processor] ⏭ Skipping non-photo/video media: {kind}")
        return None
    try:
        text = (
            getattr(file_msg, 'text', None) or
            getattr(file_msg, 'message', None) or
            getattr(file_msg, 'caption', None) or ""
        )
        sent = await userbot.send_file(
            db_ch,
            file=file_msg.media,
            caption=text or None,
            parse_mode="html",
        )
        return sent.id
    except Exception as e:
        print(f"[processor] ❌ DB copy failed: {e}")
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
            print(f"[processor] ⚠️ Unknown link pattern: {link}")
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
    except Exception as e:
        print(f"[processor] ❌ Conversation with @{bot_username} failed: {e}")

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
    """
    Conversational /genlink flow:
      send /genlink → bot asks for the link → send link → bot replies with new link
    """
    print(f"[processor] → @{bot}: /genlink (conversational)")
    print(f"[processor]   link: {link}")
    try:
        async with userbot.conversation(bot, timeout=60) as conv:
            # Step A: send /genlink
            await conv.send_message("/genlink")
            print(f"[processor]   → sent /genlink")

            # Step B: bot asks for the link
            await asyncio.sleep(DELAY_CONVERSATION_STEP)
            resp1 = await asyncio.wait_for(conv.get_response(), timeout=20)
            print(f"[processor]   ← bot: {repr(_msg_text(resp1)[:100])}")

            # Step C: send the DB message link
            await asyncio.sleep(DELAY_CONVERSATION_STEP)
            await conv.send_message(link)
            print(f"[processor]   → sent link")

            # Step D: bot replies with the generated link
            await asyncio.sleep(DELAY_CONVERSATION_STEP)
            resp2 = await asyncio.wait_for(conv.get_response(), timeout=30)
            print(f"[processor]   ← bot final: {repr(_msg_text(resp2)[:120])}")

            url = _extract_url_from_response(resp2)
            if url:
                return url

            # Sometimes arrives in a follow-up message
            try:
                await asyncio.sleep(2)
                resp3 = await asyncio.wait_for(conv.get_response(), timeout=10)
                return _extract_url_from_response(resp3)
            except asyncio.TimeoutError:
                pass

    except Exception as e:
        print(f"[processor] ❌ /genlink conversation failed: {e}")
    return None


async def _batch_conversational(bot: str, db_links: list, userbot) -> str | None:
    """
    Conversational batch flow:
      send /batch → bot asks for first link → send first link
      bot asks for last link → send last link → bot returns new link
    """
    first_link = db_links[0]
    last_link  = db_links[-1]

    print(f"[processor] → @{bot}: /batch (conversational)")
    print(f"[processor]   first: {first_link}")
    print(f"[processor]   last:  {last_link}")

    try:
        async with userbot.conversation(bot, timeout=90) as conv:
            # A: send /batch
            await conv.send_message("/batch")
            print(f"[processor]   → sent /batch")

            # B: bot asks for first link
            await asyncio.sleep(DELAY_CONVERSATION_STEP)
            resp1 = await asyncio.wait_for(conv.get_response(), timeout=20)
            print(f"[processor]   ← bot: {repr(_msg_text(resp1)[:100])}")

            # C: send first link
            await asyncio.sleep(DELAY_CONVERSATION_STEP)
            await conv.send_message(first_link)
            print(f"[processor]   → sent first link")

            # D: bot asks for last link
            await asyncio.sleep(DELAY_CONVERSATION_STEP)
            resp2 = await asyncio.wait_for(conv.get_response(), timeout=20)
            print(f"[processor]   ← bot: {repr(_msg_text(resp2)[:100])}")

            # E: send last link
            await asyncio.sleep(DELAY_CONVERSATION_STEP)
            await conv.send_message(last_link)
            print(f"[processor]   → sent last link")

            # F: bot replies with the generated link
            await asyncio.sleep(DELAY_CONVERSATION_STEP)
            resp3 = await asyncio.wait_for(conv.get_response(), timeout=30)
            print(f"[processor]   ← bot final: {repr(_msg_text(resp3)[:120])}")

            url = _extract_url_from_response(resp3)
            if url:
                return url

            # Sometimes arrives in a follow-up message
            try:
                await asyncio.sleep(2)
                resp4 = await asyncio.wait_for(conv.get_response(), timeout=10)
                return _extract_url_from_response(resp4)
            except asyncio.TimeoutError:
                pass

    except asyncio.TimeoutError:
        print(f"[processor] ❌ /batch conversation timed out")
    except Exception as e:
        print(f"[processor] ❌ /batch conversation failed: {e}")

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
                        print(f"[processor]   URL in button: {btn.url}")
                        return btn.url
        except Exception:
            pass
    print(f"[processor] ⚠️ No URL found in response: {repr(text[:80])}")
    return None


async def _send_to_output(original_msg, html_text: str, new_link: str, output_channel, userbot):
    """Send the processed post to the output channel (no forward tag, HTML formatting)."""
    print(f"[processor] Sending to output channel {output_channel}")
    try:
        if original_msg.media:
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
        print("[processor] ✅ Sent to output channel")
    except Exception as e:
        print(f"[processor] ❌ Send to output failed: {e}")
        try:
            await userbot.send_message(output_channel, f"{html_text}\n\n{new_link}")
            print("[processor] ✅ Fallback send OK")
        except Exception as e2:
            print(f"[processor] ❌ Fallback also failed: {e2}")


async def _send_log(userbot, log_channel, text: str):
    """Send an info message to the log channel (silently ignores errors)."""
    if not log_channel:
        return
    try:
        lc = int(log_channel)
    except (ValueError, TypeError):
        lc = log_channel
    try:
        await userbot.send_message(lc, text, parse_mode="md")
    except Exception as e:
        print(f"[processor] ⚠️ Log channel send failed: {e}")
