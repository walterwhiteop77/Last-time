"""Private, per-user inline settings panel for the Telegram admin bot."""
import math
import re

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from database import (get_admins, get_config, get_delays, get_owner, set_ws,
                      update_config, set_delay, set_all_delays, reset_delays)
from bot.handlers.admin import DELAY_LABELS, admin_only

PREFIX = "settings:"
FIELDS = {
    "source_channel": ("Source channel", "Send a channel ID or @username"),
    "db_channel": ("Storage channel", "Send a channel ID or @username"),
    "output_channel": ("Output channel", "Send a channel ID or @username"),
    "second_bot_username": ("Link bot", "Send its @username"),
    "log_channel": ("Log channel", "Send a channel ID or @username, or 'off'"),
    "caption_template": ("Caption template", "Send the new template (use {text} for the original), or 'off'"),
}


def keyboard(rows):
    return InlineKeyboardMarkup([[Button(label, callback_data=PREFIX + action) for label, action in row] for row in rows])


def footer():
    return [("‹ Back", "home"), ("✕ Close", "close")]


def short(value):
    result = str(value) if value else "Not set"
    return result[:45] + "…" if len(result) > 46 else result


async def render(section, uid):
    cfg = await get_config(uid)
    if section == "channels":
        text = ("📌 Channels & link bot\n\n" + "\n".join(
            f"{label}: {short(cfg.get(key))}" for key, (label, _) in FIELDS.items() if key != "caption_template"))
        rows = [[(label, "edit:" + key)] for key, (label, _) in FIELDS.items() if key != "caption_template"]
    elif section == "behavior":
        mode = cfg.get("save_mode") or "auto"
        text = ("⚙️ Processing\n\n"
                f"Save mode: {mode}\n"
                f"Link filter: {'On' if cfg.get('strip_links') else 'Off'}\n"
                f"Keep captions: {'Yes' if cfg.get('keep_caption', False) else 'No'}\n"
                f"Account rotation: {'On' if cfg.get('rotate_accounts', True) else 'Off'}\n"
                f"Caption template: {short(cfg.get('caption_template'))}")
        rows = [
            [(f"{'✓ ' if mode == option else ''}{option.title()}", "mode:" + option)
             for option in ("auto", "download", "copy")],
            [("Toggle link filter", "toggle:strip_links")],
            [("Toggle captions", "toggle:keep_caption")],
            [("Toggle rotation", "toggle:rotate_accounts")],
            [("Edit caption template", "edit:caption_template")],
        ]
    elif section == "delays":
        delays = await get_delays(uid)
        text = "⏱ Waiting times\n\n" + "\n".join(
            f"{label}: {delays[key]}s" for key, label in DELAY_LABELS.items())
        rows = [[(label, "delay:" + key)] for key, label in DELAY_LABELS.items()]
        rows += [[("Set all delays", "delay:all"), ("Restore defaults", "confirm:delays")]]
    elif section == "accounts":
        import userbot.pool as pool
        accounts = pool.accounts_of(uid)
        text = ("👥 Accounts\n\n" +
                ("\n".join(f"{a.index}. {short(a.label)} — {'active' if a.enabled else 'paused'}" for a in accounts)
                 if accounts else "No accounts added yet. Send /login to add one.") +
                f"\n\nListener: {cfg.get('listener_index', 1)} · Link bot: {cfg.get('link_account_index', 1)}")
        rows = []
        for account in accounts:
            idx = account.index
            rows.append([(f"👁 {idx} Listen", f"account:listener:{idx}"),
                         (f"🔗 {idx} Link", f"account:link:{idx}")])
            rows.append([(("⏸ Pause" if account.enabled else "▶ Resume") + f" {idx}", f"account:enabled:{idx}")])
    else:
        text = "⚙️ Settings\n\nYour private workspace"
        rows = [[("📌 Channels", "channels"), ("⚙️ Processing", "behavior")],
                [("⏱ Delays", "delays"), ("👥 Accounts", "accounts")]]
    rows.append(footer() if section != "home" else [("✕ Close", "close")])
    return text, keyboard(rows)


@admin_only
async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Open my private chat to change settings.")
        return
    context.user_data.pop("settings_pending", None)
    text, markup = await render("home", update.effective_user.id)
    await update.message.reply_text(text, reply_markup=markup)


async def on_settings_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = update.effective_user.id
    # Never trust callback data: the button can be forwarded or clicked after access is revoked.
    if (update.effective_chat.type != "private" or
            (uid not in [int(a) for a in await get_admins()] and uid != await get_owner())):
        await query.answer("Not authorized. Open your own private chat.", show_alert=True)
        return
    set_ws(uid)
    action = (query.data or "")[len(PREFIX):]
    section = "home"
    context.user_data.pop("settings_pending", None)
    if action == "close":
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=None)
        return
    if action in ("home", "channels", "behavior", "delays", "accounts"):
        section = action
    elif action.startswith("edit:") and action[5:] in FIELDS:
        key = action[5:]
        context.user_data["settings_pending"] = {"kind": "field", "key": key, "chat": update.effective_chat.id}
        await query.answer()
        label, instruction = FIELDS[key]
        prompt = await query.message.reply_text(f"{label}\n{instruction}\n\nReply to this message with the new value, or use the buttons to cancel.")
        context.user_data["settings_pending"]["prompt"] = prompt.message_id
        return
    elif action.startswith("delay:") and action[6:] in (*DELAY_LABELS, "all"):
        key = action[6:]
        context.user_data["settings_pending"] = {"kind": "delay", "key": key, "chat": update.effective_chat.id}
        await query.answer()
        prompt = await query.message.reply_text("Send seconds (0–600) as a reply to this message.")
        context.user_data["settings_pending"]["prompt"] = prompt.message_id
        return
    elif action.startswith("mode:") and action[5:] in ("auto", "download", "copy"):
        await update_config("save_mode", action[5:], uid)
        section = "behavior"
    elif action.startswith("toggle:") and action[7:] in ("strip_links", "keep_caption", "rotate_accounts"):
        key = action[7:]
        cfg = await get_config(uid)
        await update_config(key, not cfg.get(key, key == "rotate_accounts"), uid)
        section = "behavior"
    elif action == "confirm:delays":
        await query.answer()
        await query.edit_message_text("Restore all waiting times to their defaults?", reply_markup=keyboard([
            [("Restore defaults", "reset:delays"), ("Cancel", "delays")]]))
        return
    elif action == "reset:delays":
        await reset_delays(uid)
        section = "delays"
    elif action.startswith("account:"):
        import userbot.pool as pool
        parts = action.split(":")
        if len(parts) != 3 or parts[1] not in ("listener", "link", "enabled") or not parts[2].isdigit():
            await query.answer("Invalid option", show_alert=True)
            return
        idx = int(parts[2])
        account = pool.get_account(idx, uid)
        if account is None:
            await query.answer("Account not found", show_alert=True)
            return
        if parts[1] == "listener":
            await update_config("listener_index", idx, uid)
            await query.answer("Restart the service to switch the listener.")
        elif parts[1] == "link":
            await update_config("link_account_index", idx, uid)
        else:
            await pool.set_enabled(idx, not account.enabled, uid)
        section = "accounts"
    else:
        await query.answer("This menu is no longer valid.", show_alert=True)
        return
    text, markup = await render(section, uid)
    await query.answer()
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except Exception as exc:
        if "Message is not modified" not in str(exc):
            raise


async def on_settings_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = context.user_data.get("settings_pending")
    reply = update.message.reply_to_message
    if (not pending or not reply or reply.message_id != pending.get("prompt") or
            update.effective_chat.id != pending.get("chat") or update.effective_chat.type != "private"):
        return
    uid = update.effective_user.id
    if uid not in [int(a) for a in await get_admins()] and uid != await get_owner():
        context.user_data.pop("settings_pending", None)
        await update.message.reply_text("Not authorized.")
        return
    set_ws(uid)
    value = update.message.text.strip()
    if pending["kind"] == "delay":
        try:
            seconds = float(value)
        except ValueError:
            await update.message.reply_text("Send a number between 0 and 600 seconds. Reply to the same prompt.")
            return
        if not math.isfinite(seconds) or not 0 <= seconds <= 600:
            await update.message.reply_text("Send a number between 0 and 600 seconds. Reply to the same prompt.")
            return
        if pending["key"] == "all":
            await set_all_delays(seconds, uid)
        else:
            await set_delay(pending["key"], seconds, uid)
        notice = "⚠️ Short waits can trigger Telegram limits.\n" if seconds < 1 else ""
        section = "delays"
    else:
        key = pending["key"]
        if key == "caption_template":
            if len(value) > 3500:
                await update.message.reply_text("Template is too long (maximum 3500 characters). Reply to the same prompt.")
                return
            value = "" if value.lower() == "off" else value
        elif key == "second_bot_username":
            value = value.lstrip("@")
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", value):
                await update.message.reply_text("Send a valid bot username, like @MyLinkBot. Reply to the same prompt.")
                return
        elif key == "log_channel" and value.lower() == "off":
            value = None
        elif not re.fullmatch(r"-?\d{5,20}|@[A-Za-z][A-Za-z0-9_]{4,31}", value):
            await update.message.reply_text("Send a numeric channel ID or @username. Reply to the same prompt.")
            return
        await update_config(key, value, uid)
        notice = ""
        section = "behavior" if key == "caption_template" else "channels"
    context.user_data.pop("settings_pending", None)
    text, markup = await render(section, uid)
    await update.message.reply_text(f"✅ Saved.\n{notice}\n{text}", reply_markup=markup)


def register_settings(app):
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CallbackQueryHandler(on_settings_button, pattern=r"^settings:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.REPLY & filters.ChatType.PRIVATE,
                                   on_settings_reply))
