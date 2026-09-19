"""
Core automation logic — sequential per post, rate-limit-safe, multi-account.

Pipeline for each post:
  1. Extract deep links from the source-channel post (1 or more)
  2. For EACH link independently:
     a. Pick the next free userbot account from the pool (rotation)
     b. Open the link with that account → collect files from the linked bot
     c. Store the files in the DB channel:
          • normal mode      → re-send by file reference (fast, no traffic)
          • restricted mode  → download to disk, then upload again
            (used when the source bot has forwarding/saving disabled)
     d. Build DB channel message links
     e. Ask the second bot for a new shareable link (link account)
  3. Replace ALL original links with their new counterparts in the post HTML
  4. Strip @usernames / other t.me links (if filter enabled)
  5. Apply caption template (if set)
  6. Send ONE modified post to the output channel
  7. Send a summary to the log channel (if set)
  8. Save the mapping, pause, then continue with the next post

Every wait is configurable at runtime with /setdelay — see database.DEFAULT_DELAYS.

Stop/disable behaviour:
  - the cancel flag is checked inside every sleep via _sleep_cancellable()
  - at most 0.5 s after /stop or /disable the current step aborts
"""

import asyncio
import os
import re
import tempfile

from telethon.tl.types import (
    Message as TelethonMessage,
    MessageMediaPhoto,
    MessageMediaDocument,
    MessageMediaWebPage,
    DocumentAttributeSticker,
    DocumentAttributeFilename,
    DocumentAttributeVideo,
)
from telethon.errors import FloodWaitError
from telethon.extensions import html as tl_html

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from database import get_config, get_delays, save_file_mapping, log_event
import userbot.pool as pool

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

# Only ONE post processed at a time
_processing_lock = asyncio.Lock()
_current_task: asyncio.Task | None = None


def cancel_current_processing():
    """Immediately cancel whatever post is currently being processed, if any."""
    global _current_task
    if _current_task and not _current_task.done():
        print("[processor] Cancel requested — stopping current task immediately")
        _current_task.cancel()


# ── Cancellation helpers ──────────────────────────────────────────────────────

def _is_cancelled() -> bool:
    try:
        import userbot.client as _ub
        return _ub._scan_cancelled
    except Exception:
        return False


async def _sleep_cancellable(seconds: float, step: float = 0.5):
    elapsed = 0.0
    while elapsed < seconds:
        if _is_cancelled():
            return
        chunk = min(step, seconds - elapsed)
        await asyncio.sleep(chunk)
        elapsed += chunk


# ── Account helpers ───────────────────────────────────────────────────────────

async def _pick_worker(cfg):
    """
    Return the account that should do the next unit of work.
    With rotation off, everything runs on the listener account.
    """
    if not cfg.get("rotate_accounts", True):
        return await pool.listener_account()
    acc = await pool.next_worker()
    return acc or await pool.listener_account()


async def _listener_client():
    acc = await pool.listener_account()
    return acc.client if acc else None


def _note_flood(acc, seconds: float):
    if acc is not None:
        acc.note_flood(seconds)


# ── Restricted-content detection ──────────────────────────────────────────────

def _is_restricted(message) -> bool:
    """
    True when Telegram forbids re-sending this message by reference — i.e. the
    sending bot/channel has "Restrict saving content" (noforwards) enabled.
    """
    if getattr(message, "noforwards", False):
        return True
    chat = getattr(message, "chat", None)
    if chat is not None and getattr(chat, "noforwards", False):
        return True
    return False


async def _should_download(cfg, message) -> bool:
    mode = (cfg.get("save_mode") or "auto").lower()
    if mode == "download":
        return True
    if mode == "copy":
        return False
    return _is_restricted(message)   # auto


# ── Public entry point ────────────────────────────────────────────────────────

async def process_post(messages, links, _unused_client=None, bot_app=None):
    """
    Process one source-channel post end-to-end.
    `messages` is a single Telethon Message or a list (album).
    `links` may be a single URL string or a list of URL strings.
    Queues behind _processing_lock so concurrent calls serialise.
    """
    if isinstance(links, str):
        links = [links]
    if isinstance(messages, TelethonMessage):
        messages = [messages]

    global _current_task
    primary_id = messages[0].id
    delays = await get_delays()

    async with _processing_lock:
        if _is_cancelled():
            print(f"[processor] /stop active — skipping post {primary_id}")
            return

        task = asyncio.ensure_future(_process_post_inner(messages, links))
        _current_task = task
        try:
            await task
        except asyncio.CancelledError:
            print(f"[processor] Post {primary_id} processing stopped by /stop")
        finally:
            if _current_task is task:
                _current_task = None

        if not _is_cancelled():
            await _sleep_cancellable(delays["between_posts"])


async def _process_post_inner(messages: list, links: list):
    message = messages[0]
    cfg = await get_config()
    delays = await get_delays()

    db_channel          = cfg.get("db_channel")
    output_channel      = cfg.get("output_channel")
    second_bot_username = cfg.get("second_bot_username")
    log_channel         = cfg.get("log_channel")
    caption_template    = cfg.get("caption_template") or ""
    strip_links         = cfg.get("strip_links", False)
    keep_caption        = cfg.get("keep_caption", True)
    text_rules          = cfg.get("text_rules", [])

    if not all([db_channel, output_channel, second_bot_username]):
        print(f"[processor] Missing config — skipping post {message.id}")
        return

    if not pool.live_accounts():
        print("[processor] No logged-in userbot account — skipping post")
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

    link_replacements = []   # [(original_link, new_link), ...]
    all_db_msg_ids    = []

    for link_idx, original_link in enumerate(links):
        if _is_cancelled():
            print("[processor] /stop — aborting mid-post")
            return

        if link_idx > 0:
            await _sleep_cancellable(delays["between_links"])
            if _is_cancelled():
                return

        worker = await _pick_worker(cfg)
        if worker is None or worker.client is None:
            print("[processor] No available account — aborting post")
            return

        print(f"\n[processor] ── Link {link_idx + 1}/{len(links)} on account "
              f"{worker.index} ({worker.label}): {original_link}")

        async with worker.lock:
            client = worker.client

            # Step A: get files from the linked bot ───────────────────────────
            files = await _get_files_from_link(original_link, client, worker, delays)
            if not files:
                print(f"[processor] No files for {original_link} — skipping this link")
                await log_event("no_files", {"msg_id": message.id, "link": original_link})
                await _send_log(log_channel,
                                f"⚠️ *No files* for link `{original_link}` in post `{message.id}`")
                worker.touch()
                continue

            restricted = any(_is_restricted(f) for f in files)
            mode_note = "restricted (download → upload)" if restricted else "normal (copy)"
            print(f"[processor] {len(files)} file(s) collected — mode: {mode_note}")

            if _is_cancelled():
                return

            # Step B: store files in the DB channel ───────────────────────────
            db_msg_ids = []
            for i, file_msg in enumerate(files):
                if _is_cancelled():
                    return
                if i > 0:
                    await _sleep_cancellable(delays["between_copies"])
                    if _is_cancelled():
                        return
                msg_id = await _store_in_db(client, worker, db_ch, file_msg,
                                            keep_caption, cfg, delays)
                if msg_id:
                    db_msg_ids.append(msg_id)
                    print(f"[processor] Stored file {i+1}/{len(files)} → DB msg {msg_id}")

            worker.touch()

            if not db_msg_ids:
                print(f"[processor] Nothing stored for {original_link} — skipping")
                await _send_log(log_channel,
                                f"❌ *DB save failed* for link `{original_link}` in post `{message.id}`")
                continue

            if _is_cancelled():
                return

            # Step C: build DB links ──────────────────────────────────────────
            db_links = [_make_msg_link(db_channel, mid) for mid in db_msg_ids]
            print(f"[processor] DB links: {db_links}")
            all_db_msg_ids.extend(db_msg_ids)

            await _sleep_cancellable(delays["after_copy_batch"])
            if _is_cancelled():
                return

        # Step D: generate the new link (dedicated link account) ──────────────
        new_link = await _generate_link(second_bot_username, db_links, delays)
        if not new_link:
            print(f"[processor] Second bot returned no link for {original_link} — skipping")
            await log_event("link_gen_failed", {"msg_id": message.id, "db_msg_ids": db_msg_ids})
            await _send_log(log_channel,
                            f"❌ *Link generation failed* for post `{message.id}`\n"
                            f"DB msgs: `{db_msg_ids}`")
            continue

        print(f"[processor] New link: {new_link}")
        link_replacements.append((original_link, new_link))

        # Give the account that just worked a short rest before it is reused
        await _sleep_cancellable(min(delays["account_cooldown"], 1.0))

    if not link_replacements:
        print(f"[processor] No links processed — aborting post {message.id}")
        return

    if _is_cancelled():
        return

    # ── Build and send output ─────────────────────────────────────────────────
    processed_html = _message_to_html(messages)
    for original_link, new_link in link_replacements:
        processed_html = _replace_link_in_html(processed_html, original_link, new_link)

    if strip_links:
        all_new   = [nl for _, nl in link_replacements]
        all_orig  = [ol for ol, _ in link_replacements]
        processed_html = _apply_filter(processed_html, keep_urls=all_new + all_orig)

    if text_rules:
        processed_html = _apply_text_rules(processed_html, text_rules)

    final_html = _apply_template(processed_html, caption_template)

    first_new_link = link_replacements[0][1]
    await _send_to_output(messages, final_html, first_new_link, out_ch, cfg)

    # ── Log summary ───────────────────────────────────────────────────────────
    pairs_text = "\n".join(f"  • `{ol}` → {nl}" for ol, nl in link_replacements)
    await _send_log(
        log_channel,
        f"✅ *Post processed*\n"
        f"• Source msg: `{message.id}`\n"
        f"• Links replaced: `{len(link_replacements)}`\n"
        f"• Files saved: `{len(all_db_msg_ids)}`\n"
        f"{pairs_text}",
    )

    for original_link, new_link in link_replacements:
        await save_file_mapping(message.id, original_link, all_db_msg_ids, new_link)
    await log_event("processed", {
        "msg_id":   message.id,
        "links":    [{"original": ol, "new": nl} for ol, nl in link_replacements],
        "db_msg_ids": all_db_msg_ids,
    })
    print(f"[processor] Post {message.id} complete ({len(link_replacements)} link(s) replaced)")


# ── Text helpers ──────────────────────────────────────────────────────────────

def _message_to_html(messages) -> str:
    if isinstance(messages, TelethonMessage):
        messages = [messages]
    for message in messages:
        raw_text = getattr(message, 'message', None) or getattr(message, 'text', None) or ""
        if not raw_text:
            continue
        entities = getattr(message, 'entities', None) or []
        try:
            if entities:
                return tl_html.unparse(raw_text, entities)
        except Exception:
            pass
        return raw_text
    return ""


def _replace_link_in_html(html: str, original_link: str, new_link: str) -> str:
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


def _apply_text_rules(html: str, rules: list) -> str:
    for rule in rules:
        find = rule.get("find", "")
        replace = rule.get("replace", "")
        if not find:
            continue
        if find in html:
            html = html.replace(find, replace)
    html = re.sub(r" {2,}", " ", html)
    html = re.sub(r"\n{3,}", "\n\n", html)
    return html.strip()


def _apply_template(text: str, template: str) -> str:
    if not template:
        return text
    if "{text}" in template:
        return template.replace("{text}", text)
    return f"{text}\n\n{template}"


def _make_msg_link(channel, msg_id: int) -> str:
    ch = str(channel)
    if ch.startswith("@"):
        return f"https://t.me/{ch.lstrip('@')}/{msg_id}"
    if ch.startswith("-100"):
        return f"https://t.me/c/{ch[4:]}/{msg_id}"
    if ch.lstrip("-").isdigit():
        return f"https://t.me/c/{ch.lstrip('-')}/{msg_id}"
    return f"https://t.me/{ch}/{msg_id}"


# ── Media helpers ─────────────────────────────────────────────────────────────

def _is_supported_media(media) -> bool:
    if media is None:
        return False
    if isinstance(media, MessageMediaWebPage):
        return False
    if isinstance(media, MessageMediaPhoto):
        return True
    if isinstance(media, MessageMediaDocument):
        doc = getattr(media, "document", None)
        attrs = getattr(doc, "attributes", []) or []
        if any(isinstance(a, DocumentAttributeSticker) for a in attrs):
            return False
        return True
    return False


def _media_filename(msg) -> str | None:
    media = getattr(msg, "media", None)
    doc = getattr(media, "document", None)
    for attr in (getattr(doc, "attributes", []) or []):
        if isinstance(attr, DocumentAttributeFilename):
            return attr.file_name
    return None


def _is_video(msg) -> bool:
    """True when the message media is a video (attribute or mime type)."""
    media = getattr(msg, "media", None)
    doc = getattr(media, "document", None)
    if doc is None:
        return False
    attrs = getattr(doc, "attributes", []) or []
    if any(isinstance(a, DocumentAttributeVideo) for a in attrs):
        return True
    mime = (getattr(doc, "mime_type", "") or "").lower()
    return mime.startswith("video/")


def _video_attributes(msg) -> list:
    """
    Rebuild video attributes (duration, size, streaming) for re-upload so the
    video shows as a playable/streamable video instead of a plain file.
    """
    media = getattr(msg, "media", None)
    doc = getattr(media, "document", None)
    out = []
    for a in (getattr(doc, "attributes", []) or []):
        if isinstance(a, DocumentAttributeVideo):
            out.append(DocumentAttributeVideo(
                round_message=False,
                supports_streaming=True,
                duration=getattr(a, "duration", 0) or 0,
                w=getattr(a, "w", 0) or 0,
                h=getattr(a, "h", 0) or 0,
            ))
        elif isinstance(a, DocumentAttributeFilename):
            out.append(a)
    return out


async def _resolve_target(client, chat):
    try:
        return await client.get_entity(chat)
    except Exception:
        pass
    try:
        import userbot.client as _ub
        return await _ub._resolve_entity(str(chat), client)
    except Exception as e:
        print(f"[processor] Could not resolve channel {chat}: {e}")
        return None


async def _store_in_db(client, worker, db_ch, file_msg, keep_caption, cfg, delays) -> int | None:
    """
    Put one file into the DB channel.

    Normal mode re-sends the file by reference (no upload traffic).
    Restricted mode — forced with /setmode download, or detected automatically
    when the source bot has saving/forwarding disabled — downloads the file to
    a temp file and uploads it again, which works for protected content.
    Retries up to 3 times on FloodWait.
    """
    if not _is_supported_media(file_msg.media):
        kind = type(file_msg.media).__name__ if file_msg.media else "text"
        print(f"[processor] Skipping unsupported media: {kind}")
        return None

    target = await _resolve_target(client, db_ch)
    if target is None:
        print(f"[processor] DB channel {db_ch} unreachable — is this account a member/admin?")
        return None

    if keep_caption:
        text = (
            getattr(file_msg, 'text', None) or
            getattr(file_msg, 'message', None) or
            getattr(file_msg, 'caption', None) or ""
        )
    else:
        text = ""

    if await _should_download(cfg, file_msg):
        print("[processor] Restricted/forced mode — downloading then re-uploading")
        return await _upload_via_download(client, worker, target, file_msg, text, delays)

    for attempt in range(3):
        if _is_cancelled():
            return None
        try:
            sent = await client.send_file(
                target,
                file=file_msg.media,
                caption=text or None,
                parse_mode="html",
            )
            return sent.id
        except FloodWaitError as e:
            wait = e.seconds + 5
            _note_flood(worker, wait)
            print(f"[processor] FloodWait on DB copy — waiting {wait}s (attempt {attempt + 1}/3)")
            await _sleep_cancellable(wait)
            if _is_cancelled():
                return None
        except Exception as e:
            print(f"[processor] Copy by reference failed (attempt {attempt + 1}/3): {e}")
            msg_id = await _upload_via_download(client, worker, target, file_msg, text, delays)
            if msg_id:
                return msg_id
            if attempt < 2:
                await _sleep_cancellable(max(delays["between_copies"], 5))
            else:
                return None
    return None


async def _upload_via_download(client, worker, target, file_msg, caption: str, delays) -> int | None:
    """
    Save-restricted path: download the media to a temporary file and upload it
    to the DB channel. Streaming through a file (not memory) keeps large videos
    safe on small hosts.
    """
    tmp_dir = tempfile.mkdtemp(prefix="tgdl_")
    path = None
    try:
        print("[processor] Downloading media…")
        path = await client.download_media(file_msg, file=tmp_dir)
        if not path or not os.path.exists(path):
            print("[processor] Download produced no file")
            return None

        size_mb = os.path.getsize(path) / (1024 * 1024)
        print(f"[processor] Downloaded {os.path.basename(path)} ({size_mb:.1f} MB) — uploading")

        # Short breather between the download and the upload so the account
        # doesn't do two heavy operations back to back.
        await _sleep_cancellable(min(delays["conversation_step"], 2.0))
        if _is_cancelled():
            return None

        is_video = _is_video(file_msg)
        force_doc = bool(_media_filename(file_msg)) and not is_video
        extra_attrs = _video_attributes(file_msg) if is_video else None
        sent = await client.send_file(
            target,
            file=path,
            caption=caption or None,
            parse_mode="html",
            force_document=force_doc,
            supports_streaming=is_video or not force_doc,
            attributes=extra_attrs,
        )
        print(f"[processor] Uploaded → DB msg {sent.id}")
        return sent.id
    except FloodWaitError as e:
        wait = e.seconds + 5
        _note_flood(worker, wait)
        print(f"[processor] FloodWait during re-upload — waiting {wait}s")
        await _sleep_cancellable(wait)
        return None
    except Exception as e:
        print(f"[processor] Download + re-upload failed: {e}")
        return None
    finally:
        try:
            if path and os.path.exists(path):
                os.remove(path)
            os.rmdir(tmp_dir)
        except Exception:
            pass


# ── File collection ───────────────────────────────────────────────────────────

async def _get_files_from_link(link: str, client, worker, delays, _retry: bool = False) -> list:
    """Open a bot deep link with one account and collect all file messages."""
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
        async with client.conversation(bot_username, timeout=60) as conv:
            cmd = f"/start {start_param}" if start_param else "/start"
            await conv.send_message(cmd)
            print(f"[processor]   → sent: {cmd}")

            deadline = asyncio.get_event_loop().time() + 45
            while asyncio.get_event_loop().time() < deadline:
                if _is_cancelled():
                    break
                try:
                    resp = await asyncio.wait_for(conv.get_response(), timeout=10)
                    has_media = bool(resp.media)
                    preview   = repr((getattr(resp, 'text', '') or '')[:80])
                    print(f"[processor]   ← media={has_media} text={preview}")
                    if has_media:
                        files.append(resp)
                except asyncio.TimeoutError:
                    print("[processor]   → silence — bot finished")
                    break
    except FloodWaitError as e:
        wait = e.seconds + 5
        _note_flood(worker, wait)
        print(f"[processor] FloodWait on conversation — waiting {wait}s")
        await _sleep_cancellable(wait)
        if not _is_cancelled() and not _retry:
            return await _get_files_from_link(link, client, worker, delays, _retry=True)
    except Exception as e:
        print(f"[processor] Conversation with @{bot_username} failed: {e}")

    print(f"[processor] Files from @{bot_username}: {len(files)}")
    return files


# ── Link generation (runs on the pinned link account) ─────────────────────────

async def _generate_link(bot_username: str, db_links: list, delays) -> str | None:
    if not db_links:
        return None
    acc = await pool.link_account()
    if acc is None or acc.client is None:
        print("[processor] No account available for link generation")
        return None

    bot = bot_username.lstrip("@")
    async with acc.lock:
        if len(db_links) == 1:
            return await _genlink_single(bot, db_links[0], acc, delays)
        return await _batch_conversational(bot, db_links, acc, delays)


async def _genlink_single(bot: str, link: str, acc, delays) -> str | None:
    step = delays["conversation_step"]
    print(f"[processor] → @{bot}: /genlink (account {acc.index})")
    client = acc.client
    try:
        async with client.conversation(bot, timeout=60) as conv:
            await conv.send_message("/genlink")
            await _sleep_cancellable(step)
            if _is_cancelled():
                return None

            resp1 = await asyncio.wait_for(conv.get_response(), timeout=20)
            print(f"[processor]   ← {repr(_msg_text(resp1)[:100])}")

            await _sleep_cancellable(step)
            if _is_cancelled():
                return None

            await conv.send_message(link)

            await _sleep_cancellable(step)
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
        _note_flood(acc, wait)
        print(f"[processor] FloodWait on /genlink — waiting {wait}s")
        await _sleep_cancellable(wait)
    except Exception as e:
        print(f"[processor] /genlink failed: {e}")
    return None


async def _batch_conversational(bot: str, db_links: list, acc, delays) -> str | None:
    step = delays["conversation_step"]
    first_link = db_links[0]
    last_link  = db_links[-1]
    client = acc.client

    print(f"[processor] → @{bot}: /batch  first={first_link}  last={last_link}")
    try:
        async with client.conversation(bot, timeout=90) as conv:
            await conv.send_message("/batch")
            await _sleep_cancellable(step)
            if _is_cancelled():
                return None

            resp1 = await asyncio.wait_for(conv.get_response(), timeout=20)
            print(f"[processor]   ← {repr(_msg_text(resp1)[:100])}")

            await _sleep_cancellable(step)
            if _is_cancelled():
                return None
            await conv.send_message(first_link)

            await _sleep_cancellable(step)
            if _is_cancelled():
                return None

            resp2 = await asyncio.wait_for(conv.get_response(), timeout=20)
            print(f"[processor]   ← {repr(_msg_text(resp2)[:100])}")

            await _sleep_cancellable(step)
            if _is_cancelled():
                return None
            await conv.send_message(last_link)

            await _sleep_cancellable(step)
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
        print("[processor] /batch timed out")
    except FloodWaitError as e:
        wait = e.seconds + 5
        _note_flood(acc, wait)
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
    if getattr(resp, "reply_markup", None):
        try:
            for row in resp.reply_markup.rows:
                for btn in row.buttons:
                    if hasattr(btn, "url") and btn.url:
                        return btn.url
        except Exception:
            pass
    print(f"[processor] No URL in response: {repr(text[:80])}")
    return None


async def _download_originals(client, messages: list) -> tuple[list, str | None]:
    """Download the source post's media so it can be re-uploaded (restricted source)."""
    tmp_dir = tempfile.mkdtemp(prefix="tgout_")
    paths = []
    for m in messages:
        if not m.media:
            continue
        try:
            p = await client.download_media(m, file=tmp_dir)
            if p:
                paths.append(p)
        except Exception as e:
            print(f"[processor] Could not download source media: {e}")
    return paths, tmp_dir


def _cleanup(paths: list, tmp_dir: str | None):
    for p in paths:
        try:
            os.remove(p)
        except Exception:
            pass
    try:
        if tmp_dir:
            os.rmdir(tmp_dir)
    except Exception:
        pass


async def _send_to_output(original_msgs, html_text: str, new_link: str, output_channel, cfg):
    """
    Send the processed post to the output channel using the listener account.
    When the source channel blocks saving/forwarding (or /setmode download is
    on), the media is downloaded and uploaded again instead of re-sent by
    reference. Retries up to 3 times on FloodWait.
    """
    if isinstance(original_msgs, TelethonMessage):
        original_msgs = [original_msgs]

    acc = await pool.listener_account()
    if acc is None or acc.client is None:
        print("[processor] No account available to post output")
        return
    client = acc.client

    primary_id = original_msgs[0].id
    media_msgs = [m for m in original_msgs if m.media]
    has_media = bool(media_msgs)

    print(f"[processor] Sending to output channel {output_channel} ({len(media_msgs)} media item(s))")

    target = await _resolve_target(client, output_channel)
    if target is None:
        print(f"[processor] Output channel {output_channel} unreachable — post {primary_id} skipped")
        return

    need_download = has_media and await _should_download(cfg, original_msgs[0])
    paths, tmp_dir = ([], None)
    if need_download:
        print("[processor] Source is save-restricted — downloading media for re-upload")
        paths, tmp_dir = await _download_originals(client, media_msgs)
        if not paths:
            need_download = False

    try:
        for attempt in range(3):
            if _is_cancelled():
                return
            try:
                if need_download and paths:
                    # Re-upload videos as playable videos, not plain files.
                    any_video = any(_is_video(m) for m in media_msgs)
                    await client.send_file(
                        target,
                        file=paths if len(paths) > 1 else paths[0],
                        caption=html_text or None,
                        parse_mode="html",
                        force_document=False,
                        supports_streaming=any_video,
                    )
                elif has_media:
                    media_list = [m.media for m in media_msgs]
                    await client.send_file(
                        target,
                        file=media_list if len(media_list) > 1 else media_list[0],
                        caption=html_text or None,
                        parse_mode="html",
                    )
                else:
                    await client.send_message(
                        target,
                        html_text,
                        parse_mode="html",
                        link_preview=False,
                    )
                print(f"[processor] Post {primary_id} sent to output channel")
                return
            except FloodWaitError as e:
                wait = e.seconds + 5
                _note_flood(acc, wait)
                print(f"[processor] FloodWait on output — waiting {wait}s (attempt {attempt+1}/3)")
                await _sleep_cancellable(wait)
            except Exception as e:
                print(f"[processor] Output send failed (attempt {attempt+1}/3): {e}")
                if has_media and not need_download and not paths:
                    # Reference send rejected — fall back to download + upload.
                    print("[processor] Falling back to download + re-upload for output")
                    paths, tmp_dir = await _download_originals(client, media_msgs)
                    need_download = bool(paths)
                await _sleep_cancellable(5)
        print(f"[processor] Giving up on post {primary_id} after 3 attempts")
    finally:
        _cleanup(paths, tmp_dir)


async def _send_log(log_channel, text: str):
    """Send a status line to the log channel (listener account)."""
    if not log_channel:
        return
    try:
        client = await _listener_client()
        if client is None:
            return
        target = await _resolve_target(client, log_channel)
        if target is None:
            return
        await client.send_message(target, text, parse_mode="md", link_preview=False)
    except Exception as e:
        print(f"[processor] Could not write to log channel: {e}")
