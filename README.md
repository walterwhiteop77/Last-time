# TG Automation Bot

A Telegram automation bot that:
1. Monitors a **source channel** for posts containing bot links
2. Opens the link as a **userbot** (real account) to fetch files
3. Forwards those files to a **DB channel**
4. Asks your **second bot** to generate a new shareable link
5. Replaces the original link in the post and sends it to an **output channel**

---

## Setup (do this once)

### 1. Authenticate the userbot

The userbot needs to log in as your real Telegram account:

```bash
cd tgbot && python setup_session.py
```

You'll be prompted for your phone number and the OTP Telegram sends you.  
This creates a `userbot_session.session` file — **keep it secret**.

### 2. Configure via Telegram bot commands

Send these commands to your bot (`@your_bot`):

| Command | Description |
|---|---|
| `/setsource <id>` | Source channel ID or @username to monitor |
| `/setdb <id>` | DB channel ID to store files |
| `/setoutput <id>` | Output channel ID where processed posts go |
| `/setsecondbot <@username>` | Username of your existing file-link bot |
| `/addadmin <user_id>` | Add an admin user |
| `/enable` | Start the automation |
| `/disable` | Pause the automation |
| `/status` | Show current config |

### 3. Start the bot

Click **Run** in Replit (workflow: `TG Automation Bot`), or:

```bash
cd tgbot && python main.py
```

---

## Second Bot Protocol

The bot talks to your second bot to generate links. It sends:
- Single file: `/link <message_id>`
- Multiple files: `/batch <id1> <id2> ...`

The second bot should reply with a message containing the generated link URL.

---

## Channel IDs

To get a channel's numeric ID:
- Forward a message from the channel to `@userinfobot`
- Or use `@username_to_id_bot`

Channel IDs are usually negative numbers like `-1001234567890`.

---

## Files

```
tgbot/
├── main.py              # Entry point (runs both bots)
├── config.py            # Loads env vars
├── database.py          # MongoDB operations
├── setup_session.py     # One-time userbot login
├── requirements.txt     # Python dependencies
├── userbot/
│   └── client.py        # Telethon userbot logic
└── bot/
    ├── app.py           # python-telegram-bot setup
    ├── processor.py     # Core automation logic
    └── handlers/
        └── admin.py     # All admin commands
```

---

## Environment Variables (already set in Replit Secrets)

| Variable | Description |
|---|---|
| `BOT_TOKEN` | Admin bot token from @BotFather |
| `API_ID` | Telegram API ID from my.telegram.org |
| `API_HASH` | Telegram API Hash from my.telegram.org |
| `MONGODB_URI` | MongoDB Atlas connection string |
