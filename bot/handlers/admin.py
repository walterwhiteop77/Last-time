import functools
from telegram import Update
from telegram.ext import (
    ContextTypes,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)
from database import get_config, update_config

# Conversation states for /login
PHONE, CODE, PASSWORD = range(3)


def admin_only(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        cfg = await get_config()
        admins = cfg.get("admins", [])
        user_id = update.effective_user.id
        if admins and user_id not in admins:
            await update.message.reply_text("⛔ You are not authorized to use this command.")
            return
        return await func(update, context)
    return wrapper


# ─── /login conversation ────────────────────────────────────────────────────

@admin_only
async def cmd_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import userbot.client as ub

    if await ub.is_authorized():
        await update.message.reply_text("✅ Userbot is already logged in.")
        return ConversationHandler.END

    await update.message.reply_text(
        "📱 *Userbot Login*\n\n"
        "Send your Telegram phone number with country code.\n"
        "Example: `+91XXXXXXXXXX`\n\n"
        "Send /cancel to abort.",
        parse_mode="Markdown",
    )
    return PHONE


@admin_only
async def login_got_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import userbot.client as ub

    phone = update.message.text.strip()
    context.user_data["login_phone"] = phone

    try:
        phone_code_hash = await ub.send_code(phone)
        context.user_data["login_hash"] = phone_code_hash
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to send code: {e}\n\nTry /login again.")
        return ConversationHandler.END

    await update.message.reply_text(
        "📨 OTP sent to your Telegram account.\n\n"
        "Send the code you received (e.g. `12345`).\n"
        "If Telegram sent it as `1 2 3 4 5` with spaces, remove the spaces.\n\n"
        "Send /cancel to abort.",
        parse_mode="Markdown",
    )
    return CODE


@admin_only
async def login_got_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from telethon.errors import SessionPasswordNeededError
    import userbot.client as ub

    code = update.message.text.strip().replace(" ", "")
    phone = context.user_data.get("login_phone")
    phone_code_hash = context.user_data.get("login_hash")

    try:
        await ub.sign_in(phone, code, phone_code_hash)
    except SessionPasswordNeededError:
        await update.message.reply_text(
            "🔐 Two-factor authentication is enabled.\n\n"
            "Send your 2FA password now.\n\n"
            "Send /cancel to abort.",
        )
        return PASSWORD
    except Exception as e:
        await update.message.reply_text(f"❌ Login failed: {e}\n\nTry /login again.")
        return ConversationHandler.END

    await _finish_login(update)
    return ConversationHandler.END


@admin_only
async def login_got_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import userbot.client as ub

    password = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass

    try:
        await ub.sign_in_2fa(password)
    except Exception as e:
        await update.message.reply_text(f"❌ 2FA login failed: {e}\n\nTry /login again.")
        return ConversationHandler.END

    await _finish_login(update)
    return ConversationHandler.END


async def _finish_login(update: Update):
    import userbot.client as ub

    me = await ub.userbot.get_me()
    name     = me.first_name or ""
    username = f"@{me.username}" if me.username else str(me.id)

    # ── Persist session so restarts don't require re-login ──────────────────
    await ub._save_session_to_db()

    # Signal the userbot coroutine to start listening
    ub.login_done.set()

    await update.message.reply_text(
        f"✅ *Logged in as {name} ({username})*\n\n"
        "Userbot is now active. Session saved — no re-login needed after restarts.\n"
        "Use /enable after configuring channels to start automation.",
        parse_mode="Markdown",
    )


@admin_only
async def login_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Login cancelled.")
    return ConversationHandler.END


# ─── Config commands ─────────────────────────────────────────────────────────

@admin_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *TG Automation Bot*\n\n"
        "Send /help to see all admin commands.",
        parse_mode="Markdown",
    )


HELP_TEXT = """
🤖 *TG Automation Bot — Admin Commands*

━━━━━━━━━━━━━━━━━━━━
🔑 *Authentication*
━━━━━━━━━━━━━━━━━━━━
/login — Log in the userbot account

━━━━━━━━━━━━━━━━━━━━
📌 *Channel Setup*
━━━━━━━━━━━━━━━━━━━━
/setsource `<id>` — Source channel to monitor
/setdb `<id>` — DB channel (file storage)
/setoutput `<id>` — Output channel for processed posts
/setsecondbot `<@user>` — Bot that generates new links
/setlog `<id|off>` — Log channel for status updates

━━━━━━━━━━━━━━━━━━━━
👥 *Admin Management*
━━━━━━━━━━━━━━━━━━━━
/addadmin `<user_id>` — Grant admin access
/removeadmin `<user_id>` — Revoke admin access

━━━━━━━━━━━━━━━━━━━━
⚙️ *Automation Control*
━━━━━━━━━━━━━━━━━━━━
/enable — Start live monitoring
/disable — Stop automation + cancel scan
/stop — Cancel running scan only
/status — Show full current config

━━━━━━━━━━━━━━━━━━━━
📥 *Scanning & Processing*
━━━━━━━━━━━━━━━━━━━━
/scan — Scan using saved start ID (or last 50)
/scan `<n>` — Scan last N posts
/scan from — Scan all posts after saved start ID
/fbatch `<start_id>` `<end_id>` — Process a specific ID range
/process `<msg_id>` — Process one specific post
/setstart `<msg_id>` — Set start ID for /scan

━━━━━━━━━━━━━━━━━━━━
✍️ *Output Customisation*
━━━━━━━━━━━━━━━━━━━━
/settemplate `<text>` — Caption template (`{text}` = original)
/showtemplate — View current template
/cleartemplate — Remove template
/setfilter `on|off` — Strip @usernames & t\.me links from output
/setcaption `keep|remove` — Keep or strip captions when copying files to the DB channel
/addtextrule `<find> => <replace>` — Replace text in output posts (omit `=> replace` to remove it)
/removetextrule `<index>` — Remove a text rule by its number
/listtextrules — List all active text rules
/cleartextrules — Remove all text rules

━━━━━━━━━━━━━━━━━━━━
🔧 *Second Bot Commands*
━━━━━━━━━━━━━━━━━━━━
/enablecmd `<cmd>` — Enable a command on second bot
/disablecmd `<cmd>` — Disable a command
/listcmds — List enabled commands

━━━━━━━━━━━━━━━━━━━━
🐛 *Debug*
━━━━━━━━━━━━━━━━━━━━
/debugchannel — Toggle chat ID logging
""".strip()


@admin_only
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


@admin_only
async def cmd_set_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /setsource <channel_id or @username>")
        return
    val = context.args[0]
    await update_config("source_channel", val)
    await update.message.reply_text(f"✅ Source channel set to: `{val}`", parse_mode="Markdown")


@admin_only
async def cmd_set_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /setdb <channel_id or @username>")
        return
    val = context.args[0]
    await update_config("db_channel", val)
    await update.message.reply_text(f"✅ DB channel set to: `{val}`", parse_mode="Markdown")


@admin_only
async def cmd_set_output(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /setoutput <channel_id or @username>")
        return
    val = context.args[0]
    await update_config("output_channel", val)
    await update.message.reply_text(f"✅ Output channel set to: `{val}`", parse_mode="Markdown")


@admin_only
async def cmd_set_second_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /setsecondbot <@username>")
        return
    val = context.args[0].lstrip("@")
    await update_config("second_bot_username", val)
    await update.message.reply_text(f"✅ Second bot set to: `@{val}`", parse_mode="Markdown")


@admin_only
async def cmd_add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /addadmin <user_id>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ User ID must be a number.")
        return
    cfg = await get_config()
    admins = cfg.get("admins", [])
    if uid not in admins:
        admins.append(uid)
        await update_config("admins", admins)
    await update.message.reply_text(f"✅ Admin `{uid}` added.", parse_mode="Markdown")


@admin_only
async def cmd_remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /removeadmin <user_id>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ User ID must be a number.")
        return
    cfg = await get_config()
    admins = cfg.get("admins", [])
    if uid in admins:
        admins.remove(uid)
        await update_config("admins", admins)
        await update.message.reply_text(f"✅ Admin `{uid}` removed.", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ That user is not an admin.")


@admin_only
async def cmd_enable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import userbot.client as ub
    if not await ub.is_authorized():
        await update.message.reply_text("❌ Userbot is not logged in. Use /login first.")
        return
    cfg = await get_config()
    missing = [k for k in ["source_channel", "db_channel", "output_channel", "second_bot_username"] if not cfg.get(k)]
    if missing:
        await update.message.reply_text(
            f"❌ Cannot enable. Missing config: {', '.join(missing)}\n"
            "Set them first with /setsource, /setdb, /setoutput, /setsecondbot"
        )
        return
    await update_config("active", True)
    source = cfg.get("source_channel")
    await update.message.reply_text("⏳ Joining source channel…")
    await ub.join_source_channel(source)
    await update.message.reply_text("✅ Automation is now *enabled*.", parse_mode="Markdown")


@admin_only
async def cmd_disable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import userbot.client as ub
    await update_config("active", False)
    ub.cancel_scan()
    await update.message.reply_text(
        "⏸ Automation *disabled*. Any running scan has been stopped.",
        parse_mode="Markdown",
    )


@admin_only
async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import userbot.client as ub
    ub.cancel_scan()
    await update.message.reply_text(
        "⛔ Scan stopped. Automation is still *enabled* for new posts.\n"
        "Use `/disable` to fully stop automation.",
        parse_mode="Markdown",
    )


@admin_only
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import userbot.client as ub
    cfg = await get_config()
    active = "🟢 Active" if cfg.get("active") else "🔴 Inactive"
    try:
        authorized = "✅ Logged in" if await ub.is_authorized() else "❌ Not logged in"
    except Exception:
        authorized = "❓ Unknown"
    cmds = cfg.get("enabled_commands", [])
    cmd_list = ", ".join(cmds) if cmds else "none"
    text = (
        f"*Bot Status*: {active}\n"
        f"*Userbot*: {authorized}\n\n"
        f"📥 Source channel: `{cfg.get('source_channel') or 'not set'}`\n"
        f"💾 DB channel: `{cfg.get('db_channel') or 'not set'}`\n"
        f"📤 Output channel: `{cfg.get('output_channel') or 'not set'}`\n"
        f"🤖 Second bot: `{cfg.get('second_bot_username') or 'not set'}`\n"
        f"👥 Admins: `{cfg.get('admins', [])}`\n"
        f"🔧 Enabled commands: `{cmd_list}`\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


@admin_only
async def cmd_enable_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /enablecmd <command_name>")
        return
    cmd = context.args[0].lstrip("/")
    cfg = await get_config()
    cmds = cfg.get("enabled_commands", [])
    if cmd not in cmds:
        cmds.append(cmd)
        await update_config("enabled_commands", cmds)
    await update.message.reply_text(f"✅ Command `/{cmd}` enabled on second bot.", parse_mode="Markdown")


@admin_only
async def cmd_disable_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /disablecmd <command_name>")
        return
    cmd = context.args[0].lstrip("/")
    cfg = await get_config()
    cmds = cfg.get("enabled_commands", [])
    if cmd in cmds:
        cmds.remove(cmd)
        await update_config("enabled_commands", cmds)
        await update.message.reply_text(f"✅ Command `/{cmd}` disabled.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"ℹ️ Command `/{cmd}` was not enabled.", parse_mode="Markdown")


@admin_only
async def cmd_list_cmds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = await get_config()
    cmds = cfg.get("enabled_commands", [])
    if cmds:
        text = "🔧 *Enabled commands on second bot:*\n" + "\n".join(f"• `/{c}`" for c in cmds)
    else:
        text = "ℹ️ No commands currently enabled on second bot."
    await update.message.reply_text(text, parse_mode="Markdown")


@admin_only
async def cmd_set_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        cfg = await get_config()
        current = cfg.get("scan_start_id", 0)
        await update.message.reply_text(
            f"📌 Current scan start ID: `{current or 'not set (uses limit)'}`\n\n"
            "Usage: `/setstart <message_id>`\n"
            "The bot will fetch all messages *after* this ID.\n"
            "Set to `0` to disable: `/setstart 0`",
            parse_mode="Markdown",
        )
        return
    try:
        msg_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Message ID must be a number.")
        return
    await update_config("scan_start_id", msg_id)
    if msg_id == 0:
        await update.message.reply_text("✅ Scan start ID cleared.", parse_mode="Markdown")
    else:
        await update.message.reply_text(
            f"✅ Scan start ID set to `{msg_id}`.",
            parse_mode="Markdown",
        )


@admin_only
async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import userbot.client as ub
    if not await ub.is_authorized():
        await update.message.reply_text("❌ Userbot not logged in. Use /login first.")
        return
    cfg = await get_config()
    source = cfg.get("source_channel")
    if not source:
        await update.message.reply_text("❌ Source channel not set. Use /setsource first.")
        return

    saved_start_id = cfg.get("scan_start_id", 0) or 0
    min_id = 0
    limit  = 0

    if context.args:
        arg = context.args[0].lower()
        if arg == "from":
            if not saved_start_id:
                await update.message.reply_text("❌ No start ID saved. Use /setstart <msg_id> first.")
                return
            min_id = saved_start_id
        else:
            try:
                limit = max(1, min(int(arg), 5000))
            except ValueError:
                await update.message.reply_text("Usage: /scan | /scan <limit> | /scan from")
                return
    else:
        if saved_start_id:
            min_id = saved_start_id
        else:
            limit = 50

    if min_id:
        desc = f"all posts after message ID `{min_id}`"
    else:
        desc = f"last *{limit}* posts"

    await update.message.reply_text(
        f"🔍 Scanning {desc} in source channel…\n_(use /stop to cancel at any time)_",
        parse_mode="Markdown",
    )

    from bot.processor import process_post

    async def callback(message, links):
        await process_post(message, links, ub.userbot, None)

    count = await ub.scan_channel(source, callback, min_id=min_id, limit=limit)

    if count > 0 and min_id:
        try:
            entity = await ub.userbot.get_entity(source)
            msgs = await ub.userbot.get_messages(entity, limit=1)
            if msgs:
                await update_config("scan_start_id", msgs[0].id)
                new_start = msgs[0].id
                await update.message.reply_text(
                    f"✅ Scan complete — *{count}* post(s) processed.\n"
                    f"📌 Start ID auto-advanced to `{new_start}`.",
                    parse_mode="Markdown",
                )
                return
        except Exception:
            pass

    await update.message.reply_text(
        f"✅ Scan complete — *{count}* post(s) with links processed.",
        parse_mode="Markdown",
    )


@admin_only
async def cmd_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import userbot.client as ub
    if not await ub.is_authorized():
        await update.message.reply_text("❌ Userbot not logged in. Use /login first.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /process <message_id>")
        return
    try:
        msg_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Message ID must be a number.")
        return

    cfg = await get_config()
    source = cfg.get("source_channel")
    if not source:
        await update.message.reply_text("❌ Source channel not set. Use /setsource first.")
        return

    await update.message.reply_text(f"⏳ Fetching and processing message `{msg_id}`…", parse_mode="Markdown")

    from bot.processor import process_post

    async def callback(message, links):
        await process_post(message, links, ub.userbot, None)

    found = await ub.process_single(source, msg_id, callback)
    if found:
        await update.message.reply_text("✅ Message queued for processing. Check logs for progress.")
    else:
        await update.message.reply_text("❌ Message not found or has no bot link.")


@admin_only
async def cmd_set_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        cfg = await get_config()
        current = cfg.get("log_channel") or "not set"
        await update.message.reply_text(
            f"📋 Current log channel: `{current}`\n\n"
            "Usage: `/setlog <channel_id>` or `/setlog off`",
            parse_mode="Markdown",
        )
        return
    val = context.args[0].strip()
    if val.lower() == "off":
        await update_config("log_channel", None)
        await update.message.reply_text("✅ Log channel disabled.")
    else:
        await update_config("log_channel", val)
        await update.message.reply_text(
            f"✅ Log channel set to `{val}`.",
            parse_mode="Markdown",
        )


@admin_only
async def cmd_set_template(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage: `/settemplate <template>`\n\n"
            "Use `{text}` as a placeholder for the original post text.\n"
            "Example:\n`/settemplate {text}\\n\\n📢 Join @MyChannel`",
            parse_mode="Markdown",
        )
        return
    raw = " ".join(context.args).replace("\\n", "\n")
    await update_config("caption_template", raw)
    await update.message.reply_text(
        f"✅ Caption template saved:\n\n`{raw}`",
        parse_mode="Markdown",
    )


@admin_only
async def cmd_show_template(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = await get_config()
    t = cfg.get("caption_template") or ""
    if t:
        await update.message.reply_text(f"📋 *Current caption template:*\n\n`{t}`", parse_mode="Markdown")
    else:
        await update.message.reply_text("ℹ️ No caption template set.")


@admin_only
async def cmd_clear_template(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update_config("caption_template", "")
    await update.message.reply_text("✅ Caption template cleared.")


@admin_only
async def cmd_set_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        cfg = await get_config()
        state = "🟢 ON" if cfg.get("strip_links") else "🔴 OFF"
        await update.message.reply_text(
            f"🔍 Link/username filter is currently *{state}*\n\n"
            "Usage: `/setfilter on` or `/setfilter off`",
            parse_mode="Markdown",
        )
        return
    val = context.args[0].lower()
    if val == "on":
        await update_config("strip_links", True)
        await update.message.reply_text("✅ Filter *ON*.", parse_mode="Markdown")
    elif val == "off":
        await update_config("strip_links", False)
        await update.message.reply_text("✅ Filter *OFF*.", parse_mode="Markdown")
    else:
        await update.message.reply_text("Usage: `/setfilter on` or `/setfilter off`", parse_mode="Markdown")


@admin_only
async def cmd_set_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        cfg = await get_config()
        state = "🟢 KEEP" if cfg.get("keep_caption", True) else "🔴 REMOVE"
        await update.message.reply_text(
            f"📝 Caption on DB-channel copies is currently set to *{state}*\n\n"
            "Usage: `/setcaption keep` or `/setcaption remove`",
            parse_mode="Markdown",
        )
        return
    val = context.args[0].lower()
    if val in ("keep", "on"):
        await update_config("keep_caption", True)
        await update.message.reply_text("✅ Captions will be *kept* when copying files to the DB channel.", parse_mode="Markdown")
    elif val in ("remove", "off", "strip"):
        await update_config("keep_caption", False)
        await update.message.reply_text("✅ Captions will be *removed* when copying files to the DB channel.", parse_mode="Markdown")
    else:
        await update.message.reply_text("Usage: `/setcaption keep` or `/setcaption remove`", parse_mode="Markdown")


@admin_only
async def cmd_add_text_rule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage: `/addtextrule <find> => <replace>`\n"
            "Omit `=> <replace>` to remove the text entirely.\n\n"
            "Examples:\n"
            "`/addtextrule Join @OldChannel => Join @NewChannel`\n"
            "`/addtextrule Powered by XYZ`",
            parse_mode="Markdown",
        )
        return
    raw = " ".join(context.args).replace("\\n", "\n")
    if "=>" in raw:
        find, replace = raw.split("=>", 1)
        find, replace = find.strip(), replace.strip()
    else:
        find, replace = raw.strip(), ""

    if not find:
        await update.message.reply_text("❌ The text to find cannot be empty.")
        return

    cfg = await get_config()
    rules = cfg.get("text_rules", [])
    rules.append({"find": find, "replace": replace})
    await update_config("text_rules", rules)

    if replace:
        await update.message.reply_text(
            f"✅ Rule added: `{find}` → `{replace}`", parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"✅ Rule added: `{find}` will be *removed* from output posts.", parse_mode="Markdown"
        )


@admin_only
async def cmd_remove_text_rule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/removetextrule <index>` — see `/listtextrules` for indexes.", parse_mode="Markdown")
        return
    try:
        idx = int(context.args[0]) - 1
    except ValueError:
        await update.message.reply_text("❌ Index must be a number. See `/listtextrules`.", parse_mode="Markdown")
        return

    cfg = await get_config()
    rules = cfg.get("text_rules", [])
    if idx < 0 or idx >= len(rules):
        await update.message.reply_text("❌ Invalid index. See `/listtextrules`.", parse_mode="Markdown")
        return

    removed = rules.pop(idx)
    await update_config("text_rules", rules)
    label = f"`{removed['find']}` → `{removed['replace']}`" if removed.get("replace") else f"`{removed['find']}` (remove)"
    await update.message.reply_text(f"✅ Removed rule: {label}", parse_mode="Markdown")


@admin_only
async def cmd_list_text_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = await get_config()
    rules = cfg.get("text_rules", [])
    if not rules:
        await update.message.reply_text("ℹ️ No text find/replace rules set.")
        return
    lines = []
    for i, r in enumerate(rules, 1):
        if r.get("replace"):
            lines.append(f"{i}. `{r['find']}` → `{r['replace']}`")
        else:
            lines.append(f"{i}. `{r['find']}` → *(removed)*")
    await update.message.reply_text(
        "📋 *Text rules (applied to output posts):*\n" + "\n".join(lines),
        parse_mode="Markdown",
    )


@admin_only
async def cmd_clear_text_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update_config("text_rules", [])
    await update.message.reply_text("✅ All text rules cleared.")


@admin_only
async def cmd_fbatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import userbot.client as ub
    if not await ub.is_authorized():
        await update.message.reply_text("❌ Userbot not logged in. Use /login first.")
        return

    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Usage: `/fbatch <start_msg_id> <end_msg_id>`",
            parse_mode="Markdown",
        )
        return

    try:
        start_id = int(context.args[0])
        end_id   = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Both IDs must be numbers.")
        return

    if start_id > end_id:
        start_id, end_id = end_id, start_id

    cfg = await get_config()
    source = cfg.get("source_channel")
    if not source:
        await update.message.reply_text("❌ Source channel not set. Use /setsource first.")
        return

    await update.message.reply_text(
        f"🔍 Scanning messages `{start_id}` → `{end_id}` in source channel…\n"
        f"_(use /stop or /disable to cancel at any time)_",
        parse_mode="Markdown",
    )

    from bot.processor import process_post

    async def callback(message, links):
        await process_post(message, links, ub.userbot, None)

    count = await ub.scan_range(source, start_id, end_id, callback)

    await update.message.reply_text(
        f"✅ Batch complete — *{count}* post(s) with links processed.",
        parse_mode="Markdown",
    )


@admin_only
async def cmd_debugchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from database import get_config, update_config
    cfg = await get_config()
    current = cfg.get("debug_channel", False)
    new_val = not current
    await update_config("debug_channel", new_val)
    state = "🟢 ON" if new_val else "🔴 OFF"
    await update.message.reply_text(
        f"Debug channel logging is now *{state}*",
        parse_mode="Markdown",
    )


# ─── Handler registration ────────────────────────────────────────────────────

def register_handlers(app):
    login_conv = ConversationHandler(
        entry_points=[CommandHandler("login", cmd_login)],
        states={
            PHONE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, login_got_phone)],
            CODE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, login_got_code)],
            PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_got_password)],
        },
        fallbacks=[CommandHandler("cancel", login_cancel)],
        conversation_timeout=120,
    )

    app.add_handler(login_conv)
    app.add_handler(CommandHandler("start",        cmd_start))
    app.add_handler(CommandHandler("help",         cmd_help))
    app.add_handler(CommandHandler("setsource",    cmd_set_source))
    app.add_handler(CommandHandler("setdb",        cmd_set_db))
    app.add_handler(CommandHandler("setoutput",    cmd_set_output))
    app.add_handler(CommandHandler("setsecondbot", cmd_set_second_bot))
    app.add_handler(CommandHandler("addadmin",     cmd_add_admin))
    app.add_handler(CommandHandler("removeadmin",  cmd_remove_admin))
    app.add_handler(CommandHandler("enable",       cmd_enable))
    app.add_handler(CommandHandler("disable",      cmd_disable))
    app.add_handler(CommandHandler("status",       cmd_status))
    app.add_handler(CommandHandler("enablecmd",    cmd_enable_cmd))
    app.add_handler(CommandHandler("disablecmd",   cmd_disable_cmd))
    app.add_handler(CommandHandler("listcmds",     cmd_list_cmds))
    app.add_handler(CommandHandler("stop",         cmd_stop))
    app.add_handler(CommandHandler("setstart",     cmd_set_start))
    app.add_handler(CommandHandler("scan",         cmd_scan))
    app.add_handler(CommandHandler("fbatch",       cmd_fbatch))
    app.add_handler(CommandHandler("process",      cmd_process))
    app.add_handler(CommandHandler("setlog",       cmd_set_log))
    app.add_handler(CommandHandler("settemplate",  cmd_set_template))
    app.add_handler(CommandHandler("showtemplate", cmd_show_template))
    app.add_handler(CommandHandler("cleartemplate",cmd_clear_template))
    app.add_handler(CommandHandler("setfilter",    cmd_set_filter))
    app.add_handler(CommandHandler("setcaption",   cmd_set_caption))
    app.add_handler(CommandHandler("addtextrule",     cmd_add_text_rule))
    app.add_handler(CommandHandler("removetextrule",  cmd_remove_text_rule))
    app.add_handler(CommandHandler("listtextrules",   cmd_list_text_rules))
    app.add_handler(CommandHandler("cleartextrules",  cmd_clear_text_rules))
    app.add_handler(CommandHandler("debugchannel", cmd_debugchannel))
