import functools
from telegram import Update
from telegram.ext import (
    ContextTypes,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)
from database import (
    get_config,
    update_config,
    get_admins,
    get_owner,
    set_owner,
    add_admin,
    remove_admin,
    ensure_workspace,
    list_workspaces,
    set_ws,
)

# Conversation states for /login
PHONE, CODE, PASSWORD = range(3)


def _uid(update: Update) -> int:
    return update.effective_user.id


def admin_only(func):
    """
    Allow listed admins only, and bind this command to the caller's own
    workspace: their accounts, their channels, their jobs.
    """
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = _uid(update)
        admins = await get_admins()
        owner = await get_owner()

        if not admins and not owner:
            # First person to talk to a fresh bot becomes the owner.
            await set_owner(user_id)
            await ensure_workspace(user_id)
            admins = [user_id]

        if user_id not in [int(a) for a in admins]:
            await update.message.reply_text(
                "⛔ You are not authorized to use this bot.\n"
                f"Ask the owner to run `/addadmin {user_id}`.",
                parse_mode="Markdown",
            )
            return

        set_ws(user_id)
        await ensure_workspace(user_id)
        return await func(update, context)
    return wrapper


def owner_only(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = _uid(update)
        owner = await get_owner()
        if owner and int(owner) != user_id:
            await update.message.reply_text("⛔ Only the bot owner can use this command.")
            return
        set_ws(user_id)
        await ensure_workspace(user_id)
        return await func(update, context)
    return wrapper


# ─── /login conversation ────────────────────────────────────────────────────

@admin_only
async def cmd_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Log in an ADDITIONAL userbot account (the pool can hold many)."""
    import userbot.pool as pool

    count = len(pool.live_accounts())
    await update.message.reply_text(
        "📱 *Add a userbot account*\n\n"
        f"Accounts already in the pool: `{count}`\n\n"
        "Send the Telegram phone number of the account you want to add, "
        "with country code.\nExample: `+91XXXXXXXXXX`\n\n"
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
    import userbot.pool as pool
    import userbot.client as ub
    from telethon.tl.functions.channels import JoinChannelRequest

    acc = await pool.finish_login()

    cfg = await get_config()
    targets = [t for t in (cfg.get("source_channel"), cfg.get("db_channel"),
                           cfg.get("output_channel")) if t]
    joined = ""
    for t in targets:
        try:
            entity = await acc.client.get_entity(str(t))
            try:
                await acc.client(JoinChannelRequest(entity))
            except Exception:
                pass
        except Exception:
            pass
    if targets:
        joined = "\nThis account was also subscribed to your configured channels."

    # Start (or keep) this admin's own listener as soon as they have an account
    try:
        await ub.ensure_listener(_uid(update))
    except Exception as e:
        print(f"[bot] Could not start listener: {e}")

    await update.message.reply_text(
        f"✅ *Account added: {acc.label}* (number `{acc.index}` in your pool)\n\n"
        f"Your accounts now ready: `{len(pool.live_accounts())}`\n"
        "Session saved — no re-login needed after restarts.\n"
        "This account belongs to *you* only — other admins have their own."
        f"{joined}\n\n"
        "Use `/accounts` to see your pool, or `/login` again to add one more.",
        parse_mode="Markdown",
    )


@admin_only
async def login_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import userbot.pool as pool
    pool.cancel_login()
    context.user_data.clear()
    await update.message.reply_text("❌ Login cancelled.")
    return ConversationHandler.END


# ─── Config commands ─────────────────────────────────────────────────────────

@admin_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        "👋 *TG Automation Bot*\n\n"
        "You have your *own private setup*: your own logged-in accounts, your "
        "own source / storage / output channels, your own settings and your "
        "own jobs. Nothing is shared with other admins.\n\n"
        "1️⃣ `/login` — add your Telegram account(s)\n"
        "2️⃣ `/setsource` `/setdb` `/setoutput` `/setsecondbot`\n"
        "3️⃣ `/enable` — start\n\n"
        "Send /settings for the interactive menu, /help for all commands, /myspace for your setup.",
        parse_mode="Markdown",
    )
    try:
        cfg = await get_config()
        log_channel = cfg.get("log_channel")
        if log_channel and user:
            name = user.full_name or user.username or f"`{user.id}`"
            await context.bot.send_message(
                log_channel,
                f"👤 *New bot user started the bot*\n"
                f"• Name: `{name}`\n"
                f"• ID: `{user.id}`\n"
                f"• Username: `@{user.username or 'none'}`",
                parse_mode="Markdown",
            )
    except Exception as e:
        print(f"[bot] Could not log new user start: {e}")


HELP_TEXT = """
🤖 *TG Automation Bot — Admin Commands*

_Everything below applies to YOUR own setup only._

━━━━━━━━━━━━━━━━━━━━
🏠 *Your workspace*
━━━━━━━━━━━━━━━━━━━━
/settings — Interactive settings menu
/myspace — Your setup at a glance
/whoami — Your user ID
/workspaces — All workspaces (owner only)

━━━━━━━━━━━━━━━━━━━━
🔑 *Accounts (multi-login)*
━━━━━━━━━━━━━━━━━━━━
/login — Add another userbot account (yours)
/accounts — List accounts & their state
/removeaccount `<n>` — Remove an account
/pauseaccount `<n>` — Stop giving it work
/resumeaccount `<n>` — Use it again
/setlistener `<n>` — Account that watches the source
/setlinkaccount `<n>` — Account that talks to the second bot
/rotate `on|off` — Spread work across accounts
/joinall — Subscribe all accounts to your channels

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
/addadmin `<user_id>` — Give someone their own workspace (owner)
/removeadmin `<user_id>` — Revoke access (owner)

━━━━━━━━━━━━━━━━━━━━
⚙️ *Automation Control*
━━━━━━━━━━━━━━━━━━━━
/enable — Start live monitoring (your source)
/disable — Stop your automation + cancel your jobs
/stop — Cancel all of YOUR running jobs instantly
/jobs — Show your running jobs + progress
/cancel `<id>` — Cancel one of your jobs
/status — Show your current config

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
/setfilter `on|off` — Strip @usernames & t.me links from output
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

━━━━━━━━━━━━━━━━━━━━
📦 *Restricted content*
━━━━━━━━━━━━━━━━━━━━
/setmode `auto|download|copy` — How files are saved
  • auto — copy, switch to download+upload if saving is blocked
  • download — always download then upload (protected files)
  • copy — always re-send by reference

━━━━━━━━━━━━━━━━━━━━
⏱ *Speed / rate limits*
━━━━━━━━━━━━━━━━━━━━
/delays — Show all waiting times
/setdelay `<name> <seconds>` — Change one
/setalldelay `<seconds>` — Same wait for all steps
/resetdelays — Back to recommended values
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


@owner_only
async def cmd_add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage: `/addadmin <user_id>`\n"
            "The user gets their *own empty workspace*: their own accounts, "
            "channels, settings and jobs.",
            parse_mode="Markdown",
        )
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ User ID must be a number.")
        return
    await add_admin(uid)
    await update.message.reply_text(
        f"✅ `{uid}` can now use the bot with their own private setup.\n"
        "They should send /start, then /login and set their own channels.",
        parse_mode="Markdown",
    )


@owner_only
async def cmd_remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /removeadmin <user_id>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ User ID must be a number.")
        return
    owner = await get_owner()
    if owner and int(owner) == uid:
        await update.message.reply_text("❌ The owner cannot be removed.")
        return
    if await remove_admin(uid):
        from bot import jobs
        jobs.cancel_all(uid)
        await update.message.reply_text(
            f"✅ `{uid}` no longer has access. Their saved setup is kept.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text("❌ That user is not an admin.")


@admin_only
async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = _uid(update)
    owner = await get_owner()
    role = "owner" if owner and int(owner) == uid else "admin"
    await update.message.reply_text(
        f"🪪 Your user ID: `{uid}`\nRole: *{role}*\nYour workspace: `ws:{uid}`",
        parse_mode="Markdown",
    )


@admin_only
async def cmd_myspace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import userbot.pool as pool
    from bot import jobs
    uid = _uid(update)
    cfg = await get_config()
    running = jobs.running_jobs(uid)
    await update.message.reply_text(
        f"🏠 *Your workspace* `ws:{uid}`\n\n"
        f"👤 Your accounts: `{len(pool.live_accounts(uid))}`\n"
        f"📥 Source: `{cfg.get('source_channel') or 'not set'}`\n"
        f"💾 Storage: `{cfg.get('db_channel') or 'not set'}`\n"
        f"📤 Output: `{cfg.get('output_channel') or 'not set'}`\n"
        f"🤖 Second bot: `{cfg.get('second_bot_username') or 'not set'}`\n"
        f"⚙️ Automation: {'🟢 on' if cfg.get('active') else '🔴 off'}\n"
        f"🧵 Your running jobs: `{len(running)}`\n\n"
        "_Only you can see or change this setup._",
        parse_mode="Markdown",
    )


@owner_only
async def cmd_workspaces(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import userbot.pool as pool
    from bot import jobs
    admins = await get_admins()
    spaces = await list_workspaces()
    owner = await get_owner()
    lines = []
    for ws in sorted(set(list(admins) + list(spaces))):
        cfg = await get_config(ws)
        tag = "👑" if owner and int(owner) == int(ws) else "👤"
        state = "🟢" if cfg.get("active") else "🔴"
        lines.append(
            f"{tag} `{ws}` {state} accounts: `{len(pool.live_accounts(ws))}` · "
            f"jobs: `{len(jobs.running_jobs(ws))}` · src: `{cfg.get('source_channel') or '—'}`"
        )
    await update.message.reply_text(
        "🗂 *All workspaces*\n" + ("\n".join(lines) or "none"),
        parse_mode="Markdown",
    )


@admin_only
async def cmd_enable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import userbot.client as ub
    uid = _uid(update)
    if not await ub.is_authorized(uid):
        await update.message.reply_text("❌ You have no logged-in account. Use /login first.")
        return
    # Clear any leftover cancel flag from a previous /stop or /disable,
    # otherwise every new post would be silently skipped.
    ub.reset_scan_cancel(uid)
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
    await ub.join_source_channel(source, None, uid)
    await ub.ensure_listener(uid)
    await update.message.reply_text(
        "✅ Your automation is now *enabled* — your accounts watch your source channel.",
        parse_mode="Markdown",
    )


@admin_only
async def cmd_disable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import userbot.client as ub
    from bot import jobs
    uid = _uid(update)
    await update_config("active", False)
    killed = jobs.cancel_all(uid)
    ub.cancel_scan(uid)
    await update.message.reply_text(
        f"⏸ Your automation is *disabled*. {killed} of your job(s) stopped.\n"
        "_Other admins are not affected._",
        parse_mode="Markdown",
    )


@admin_only
async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hard-stop this admin's running jobs immediately (no restart needed)."""
    import asyncio
    import userbot.client as ub
    from bot import jobs

    uid = _uid(update)
    killed = jobs.cancel_all(uid)
    ub.cancel_scan(uid)

    async def _release_flag():
        await asyncio.sleep(3)
        ub.reset_scan_cancel(uid)

    asyncio.ensure_future(_release_flag())

    await update.message.reply_text(
        f"⛔ Stopped — *{killed}* of your running job(s) cancelled.\n"
        "The bot stays online and your automation is still *enabled* for new posts.\n"
        "Use `/disable` to fully stop your automation.",
        parse_mode="Markdown",
    )


@admin_only
async def cmd_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from bot import jobs
    running = jobs.running_jobs(_uid(update))
    if not running:
        await update.message.reply_text("💤 None of your jobs are running right now.")
        return
    lines = "\n".join(j.describe() for j in running)
    await update.message.reply_text(
        f"⚙️ *Your running jobs*\n\n{lines}\n\n"
        "Cancel one with `/cancel <id>` or all of yours with `/stop`.",
        parse_mode="Markdown",
    )


@admin_only
async def cmd_cancel_job(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from bot import jobs
    if not context.args:
        await update.message.reply_text(
            "Usage: `/cancel <job_id>` — see `/jobs`. Use `/stop` to cancel all of yours.",
            parse_mode="Markdown",
        )
        return
    job_id = context.args[0].strip()
    if jobs.cancel_job(job_id, _uid(update)):
        await update.message.reply_text(f"⛔ Job `{job_id}` cancelled.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ You have no running job with id `{job_id}`.", parse_mode="Markdown")


@admin_only
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import userbot.client as ub
    uid = _uid(update)
    cfg = await get_config()
    active = "🟢 Active" if cfg.get("active") else "🔴 Inactive"
    try:
        authorized = "✅ Logged in" if await ub.is_authorized(uid) else "❌ Not logged in"
    except Exception:
        authorized = "❓ Unknown"
    cmds = cfg.get("enabled_commands", [])
    cmd_list = ", ".join(cmds) if cmds else "none"
    caption_state = "🟢 KEEP" if cfg.get("keep_caption", False) else "🔴 REMOVE"
    import userbot.pool as pool
    from database import get_delays
    delays = await get_delays()
    acc_count = len(pool.live_accounts(uid))
    mode = (cfg.get("save_mode") or "auto").lower()
    rotate = "on" if cfg.get("rotate_accounts", True) else "off"
    text = (
        f"*Your status*: {active}  (workspace `ws:{uid}`)\n"
        f"*Your userbot*: {authorized} (`{acc_count}` account(s), rotation {rotate})\n\n"
        f"📥 Source channel: `{cfg.get('source_channel') or 'not set'}`\n"
        f"💾 DB channel: `{cfg.get('db_channel') or 'not set'}`\n"
        f"📤 Output channel: `{cfg.get('output_channel') or 'not set'}`\n"
        f"🤖 Second bot: `{cfg.get('second_bot_username') or 'not set'}`\n"
        f"📝 DB caption: *{caption_state}* (`/setcaption keep|remove`)\n"
        f"🔧 Enabled commands: `{cmd_list}`\n"
        f"📦 Save mode: `{mode}` (`/setmode`)\n"
        f"⏱ Post gap: `{delays['between_posts']}s` · file gap: `{delays['between_copies']}s` (`/delays`)\n"
    )
    from bot import jobs
    running = jobs.running_jobs(uid)
    if running:
        text += "\n⚙️ *Your running jobs*\n" + "\n".join(j.describe() for j in running) + "\n"
    else:
        text += "\n⚙️ No job of yours running\n"
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
    uid = _uid(update)
    if not await ub.is_authorized(uid):
        await update.message.reply_text("❌ You have no logged-in account. Use /login first.")
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

    from bot import jobs
    from bot.processor import process_post
    import userbot.pool as pool

    ub.reset_scan_cancel(uid)
    chat_id = update.effective_chat.id
    bot = context.bot

    async def run(job):
        async def callback(message, links):
            job.progress = f"post {getattr(message[0] if isinstance(message, list) else message, 'id', '?')}"
            await process_post(message, links, None, None, ws=uid)

        count = await ub.scan_channel(source, callback, min_id=min_id, limit=limit, ws=uid)

        if count > 0 and min_id:
            try:
                client = pool.listener_client(uid)
                entity = await client.get_entity(source)
                msgs = await client.get_messages(entity, limit=1)
                if msgs:
                    await update_config("scan_start_id", msgs[0].id, uid)
                    await bot.send_message(
                        chat_id,
                        f"✅ Scan complete — *{count}* post(s) processed.\n"
                        f"📌 Start ID auto-advanced to `{msgs[0].id}`.",
                        parse_mode="Markdown",
                    )
                    return
            except Exception:
                pass

        await bot.send_message(
            chat_id,
            f"✅ Scan complete — *{count}* post(s) with links processed.",
            parse_mode="Markdown",
        )

    job = jobs.start_job("scan", desc, run, chat_id, uid, ws=uid)

    await update.message.reply_text(
        f"🔍 Scanning {desc} in your source channel…\n"
        f"Job id `{job.id}` — the bot stays fully usable while this runs, "
        "for you and for everyone else.\n"
        "_Cancel with /stop (all yours) or /cancel " + job.id + "._",
        parse_mode="Markdown",
    )


@admin_only
async def cmd_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import userbot.client as ub
    uid = _uid(update)
    if not await ub.is_authorized(uid):
        await update.message.reply_text("❌ You have no logged-in account. Use /login first.")
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

    from bot import jobs
    from bot.processor import process_post

    ub.reset_scan_cancel(uid)
    chat_id = update.effective_chat.id
    bot = context.bot

    async def run(job):
        async def callback(message, links):
            await process_post(message, links, None, None, ws=uid)

        found = await ub.process_single(source, msg_id, callback, ws=uid)
        await bot.send_message(
            chat_id,
            "✅ Message processed." if found else "❌ Message not found or has no bot link.",
        )

    job = jobs.start_job("process", f"msg {msg_id}", run, chat_id, uid, ws=uid)
    await update.message.reply_text(
        f"⏳ Processing message `{msg_id}` in the background (job `{job.id}`).",
        parse_mode="Markdown",
    )


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
    uid = _uid(update)
    if not await ub.is_authorized(uid):
        await update.message.reply_text("❌ You have no logged-in account. Use /login first.")
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

    from bot import jobs
    from bot.processor import process_post

    ub.reset_scan_cancel(uid)
    chat_id = update.effective_chat.id
    bot = context.bot

    async def run(job):
        async def callback(message, links):
            job.progress = f"post {getattr(message[0] if isinstance(message, list) else message, 'id', '?')}"
            await process_post(message, links, None, None, ws=uid)

        count = await ub.scan_range(source, start_id, end_id, callback, ws=uid)
        st = ub.last_scan_stats.get(uid, {})
        msg = (f"✅ Batch complete — {count} post(s) with bot links processed.\n"
               f"Messages read from {source}: {st.get('read', '?')}")
        if st.get("error"):
            msg += f"\n⚠️ {st['error']}"
        if not count:
            if not st.get("read"):
                msg += ("\n\nNo messages were found in that ID range. Check the IDs (open a post → "
                        "Copy link, the number at the end is the ID) and that your logged-in account "
                        "has joined the source channel (/joinall).")
            elif st.get("other_links"):
                msg += "\n\nLinks seen, but none were bot ?start= links:\n" + "\n".join(st["other_links"][:10])
            else:
                msg += "\n\nThe messages contain no links at all."
        await bot.send_message(chat_id, msg, disable_web_page_preview=True)

    job = jobs.start_job("fbatch", f"{start_id} → {end_id}", run, chat_id, uid, ws=uid)

    await update.message.reply_text(
        f"🔍 Scanning messages `{start_id}` → `{end_id}` in the background (job `{job.id}`).\n"
        "The bot stays fully usable — cancel with `/stop` or `/cancel " + job.id + "`.",
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




# ─── Multi-account pool management ───────────────────────────────────────────

@admin_only
async def cmd_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import userbot.pool as pool
    uid = _uid(update)
    cfg = await get_config()
    accounts = pool.accounts_of(uid)
    if not accounts:
        await update.message.reply_text(
            "ℹ️ You have no userbot accounts yet. Send /login to add one."
        )
        return
    lines = []
    for acc in accounts:
        lines.append(await acc.status_line())
    listener = cfg.get("listener_index", 1)
    linkacc  = cfg.get("link_account_index", 1)
    rotate   = "🟢 ON" if cfg.get("rotate_accounts", True) else "🔴 OFF"
    await update.message.reply_text(
        "👥 *Your userbot accounts*\n" + "\n".join(lines) +
        f"\n\n👁 Listener: account `{listener}`"
        f"\n🔗 Link generator: account `{linkacc}`"
        f"\n🔁 Rotation: *{rotate}*\n\n"
        "`/login` add • `/removeaccount <n>` • `/pauseaccount <n>` • "
        "`/resumeaccount <n>` • `/setlistener <n>` • `/setlinkaccount <n>` • "
        "`/rotate on|off` • `/joinall`",
        parse_mode="Markdown",
    )


@admin_only
async def cmd_remove_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import userbot.pool as pool
    if not context.args:
        await update.message.reply_text("Usage: `/removeaccount <number>` — see /accounts", parse_mode="Markdown")
        return
    try:
        idx = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ The account number must be a number.")
        return
    ok = await pool.remove_account(idx)
    await update.message.reply_text(
        f"✅ Account `{idx}` removed from the pool." if ok else "❌ No account with that number.",
        parse_mode="Markdown",
    )


@admin_only
async def cmd_pause_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import userbot.pool as pool
    if not context.args:
        await update.message.reply_text("Usage: `/pauseaccount <number>`", parse_mode="Markdown")
        return
    try:
        idx = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ The account number must be a number.")
        return
    ok = await pool.set_enabled(idx, False)
    await update.message.reply_text(
        f"⏸ Account `{idx}` paused — it will not receive work." if ok else "❌ No account with that number.",
        parse_mode="Markdown",
    )


@admin_only
async def cmd_resume_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import userbot.pool as pool
    if not context.args:
        await update.message.reply_text("Usage: `/resumeaccount <number>`", parse_mode="Markdown")
        return
    try:
        idx = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ The account number must be a number.")
        return
    ok = await pool.set_enabled(idx, True)
    await update.message.reply_text(
        f"▶️ Account `{idx}` resumed." if ok else "❌ No account with that number.",
        parse_mode="Markdown",
    )


@admin_only
async def cmd_set_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import userbot.pool as pool
    if not context.args:
        cfg = await get_config()
        await update.message.reply_text(
            f"👁 Listener is account `{cfg.get('listener_index', 1)}`.\n"
            "Usage: `/setlistener <number>` (restart the bot to apply)",
            parse_mode="Markdown",
        )
        return
    try:
        idx = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ The account number must be a number.")
        return
    if pool.get_account(idx) is None:
        await update.message.reply_text("❌ No account with that number. See /accounts")
        return
    await update_config("listener_index", idx)
    await update.message.reply_text(
        f"✅ Account `{idx}` will watch the source channel.\n"
        "_Restart the service so the change takes effect._",
        parse_mode="Markdown",
    )


@admin_only
async def cmd_set_link_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import userbot.pool as pool
    if not context.args:
        cfg = await get_config()
        await update.message.reply_text(
            f"🔗 Link generation runs on account `{cfg.get('link_account_index', 1)}`.\n"
            "Usage: `/setlinkaccount <number>`",
            parse_mode="Markdown",
        )
        return
    try:
        idx = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ The account number must be a number.")
        return
    if pool.get_account(idx) is None:
        await update.message.reply_text("❌ No account with that number. See /accounts")
        return
    await update_config("link_account_index", idx)
    await update.message.reply_text(
        f"✅ Account `{idx}` will talk to the second bot. "
        "Make sure that account is allowed to use it.",
        parse_mode="Markdown",
    )


@admin_only
async def cmd_rotate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = await get_config()
    if not context.args:
        state = "🟢 ON" if cfg.get("rotate_accounts", True) else "🔴 OFF"
        await update.message.reply_text(
            f"🔁 Account rotation is *{state}*\n\n"
            "With rotation ON the work is spread over all accounts, which keeps "
            "each one far below Telegram's limits.\n"
            "Usage: `/rotate on` or `/rotate off`",
            parse_mode="Markdown",
        )
        return
    val = context.args[0].lower()
    if val in ("on", "yes", "true"):
        await update_config("rotate_accounts", True)
        await update.message.reply_text("✅ Rotation *ON* — work is shared across all accounts.", parse_mode="Markdown")
    elif val in ("off", "no", "false"):
        await update_config("rotate_accounts", False)
        await update.message.reply_text("✅ Rotation *OFF* — everything runs on the listener account.", parse_mode="Markdown")
    else:
        await update.message.reply_text("Usage: `/rotate on` or `/rotate off`", parse_mode="Markdown")


@admin_only
async def cmd_joinall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import userbot.client as ub
    cfg = await get_config()
    targets = [cfg.get("source_channel"), cfg.get("db_channel"), cfg.get("output_channel")]
    targets = [t for t in targets if t]
    if not targets:
        await update.message.reply_text("❌ No channels configured yet.")
        return
    await update.message.reply_text("⏳ Subscribing every account to your channels…")
    report = await ub.join_all_accounts(targets)
    lines = []
    for label, results in report.items():
        lines.append(f"*{label}*")
        lines.extend(f"  • {r}" for r in results)
    await update.message.reply_text("\n".join(lines) or "Nothing to do.", parse_mode="Markdown")


# ─── Restricted-content (save) mode ──────────────────────────────────────────

@admin_only
async def cmd_set_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = await get_config()
    mode = (cfg.get("save_mode") or "auto").lower()
    labels = {
        "auto":     "🤖 AUTO — copy normally, switch to download+upload when saving is blocked",
        "download": "⬇️ DOWNLOAD — always download the file and upload it again",
        "copy":     "⚡ COPY — always re-send by reference (fails on protected files)",
    }
    if not context.args:
        await update.message.reply_text(
            f"📦 *Save mode*: {labels.get(mode, mode)}\n\n"
            "Usage: `/setmode auto` • `/setmode download` • `/setmode copy`\n\n"
            "Use *download* when the source bot or channel has "
            "“restrict saving content” turned on — the file is fetched to the "
            "server and uploaded to your DB channel as a fresh file.",
            parse_mode="Markdown",
        )
        return
    val = context.args[0].lower()
    if val in ("auto", "download", "copy"):
        await update_config("save_mode", val)
        await update.message.reply_text(f"✅ Save mode set to: {labels[val]}", parse_mode="Markdown")
    else:
        await update.message.reply_text("Usage: `/setmode auto|download|copy`", parse_mode="Markdown")


# ─── Timing / rate-limit pacing ──────────────────────────────────────────────

DELAY_LABELS = {
    "between_copies":    "between two files saved to the DB channel",
    "after_copy_batch":  "after all files of one link are saved",
    "conversation_step": "between two messages sent to a bot",
    "between_links":     "between two links in the same post",
    "between_posts":     "between two posts",
    "account_cooldown":  "rest for an account after a job",
}


@admin_only
async def cmd_delays(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from database import get_delays
    delays = await get_delays()
    lines = [f"`{k}` = *{delays[k]}s* — {DELAY_LABELS.get(k, '')}" for k in DELAY_LABELS]
    await update.message.reply_text(
        "⏱ *Waiting times*\n" + "\n".join(lines) +
        "\n\nChange one: `/setdelay between_posts 5`\nRestore defaults: `/resetdelays`",
        parse_mode="Markdown",
    )


@admin_only
async def cmd_set_delay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from database import set_delay, get_delays
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: `/setdelay <name> <seconds>`\nSee `/delays` for the names.",
            parse_mode="Markdown",
        )
        return
    key = context.args[0].lower()
    if key not in DELAY_LABELS:
        await update.message.reply_text(
            "❌ Unknown name. Valid: " + ", ".join(f"`{k}`" for k in DELAY_LABELS),
            parse_mode="Markdown",
        )
        return
    try:
        seconds = float(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ The value must be a number of seconds.")
        return
    if seconds < 0 or seconds > 600:
        await update.message.reply_text("❌ Please choose a value between 0 and 600 seconds.")
        return
    if seconds < 1:
        await update.message.reply_text(
            "⚠️ Very short waits raise the risk of Telegram temporarily blocking the account."
        )
    await set_delay(key, seconds)
    delays = await get_delays()
    await update.message.reply_text(
        f"✅ `{key}` set to *{delays[key]}s* — {DELAY_LABELS[key]}.",
        parse_mode="Markdown",
    )


@admin_only
async def cmd_reset_delays(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from database import reset_delays
    await reset_delays()
    await update.message.reply_text("✅ Waiting times restored to the recommended defaults.")


@admin_only
async def cmd_set_all_delays(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from database import set_all_delays
    if not context.args:
        await update.message.reply_text(
            "Usage: `/setalldelay <seconds>`\nApplies the same wait to every step.",
            parse_mode="Markdown",
        )
        return
    try:
        seconds = float(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ The value must be a number of seconds.")
        return
    if seconds < 0 or seconds > 600:
        await update.message.reply_text("❌ Please choose a value between 0 and 600 seconds.")
        return
    if seconds < 1:
        await update.message.reply_text(
            "⚠️ Very short waits raise the risk of Telegram temporarily blocking the account."
        )
    await set_all_delays(seconds)
    await update.message.reply_text(
        f"✅ All waiting times set to *{seconds}s*.\n"
        "Fine-tune a single step with `/setdelay`, or restore defaults with `/resetdelays`.",
        parse_mode="Markdown",
    )


# ─── Handler registration ────────────────────────────────────────────────────

def register_handlers(app):
    from bot.handlers.settings import register_settings
    login_conv = ConversationHandler(
        entry_points=[CommandHandler("login", cmd_login)],
        states={
            PHONE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, login_got_phone)],
            CODE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, login_got_code)],
            PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_got_password)],
        },
        fallbacks=[CommandHandler("cancel", login_cancel)],
        # NOTE: no conversation_timeout — it needs the optional JobQueue extra
        # (pip install "python-telegram-bot[job-queue]") and is ignored otherwise.
    )

    app.add_handler(login_conv)
    register_settings(app)
    app.add_handler(CommandHandler("start",        cmd_start))
    app.add_handler(CommandHandler("help",         cmd_help))
    app.add_handler(CommandHandler("whoami",       cmd_whoami))
    app.add_handler(CommandHandler("myspace",      cmd_myspace))
    app.add_handler(CommandHandler("workspaces",   cmd_workspaces))
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
    app.add_handler(CommandHandler("jobs",         cmd_jobs))
    app.add_handler(CommandHandler("cancel",       cmd_cancel_job))
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
    # multi-account pool
    app.add_handler(CommandHandler("accounts",       cmd_accounts))
    app.add_handler(CommandHandler("removeaccount",  cmd_remove_account))
    app.add_handler(CommandHandler("pauseaccount",   cmd_pause_account))
    app.add_handler(CommandHandler("resumeaccount",  cmd_resume_account))
    app.add_handler(CommandHandler("setlistener",    cmd_set_listener))
    app.add_handler(CommandHandler("setlinkaccount", cmd_set_link_account))
    app.add_handler(CommandHandler("rotate",         cmd_rotate))
    app.add_handler(CommandHandler("joinall",        cmd_joinall))
    # restricted content + pacing
    app.add_handler(CommandHandler("setmode",        cmd_set_mode))
    app.add_handler(CommandHandler("delays",         cmd_delays))
    app.add_handler(CommandHandler("setdelay",       cmd_set_delay))
    app.add_handler(CommandHandler("setalldelay",    cmd_set_all_delays))
    app.add_handler(CommandHandler("resetdelays",    cmd_reset_delays))
