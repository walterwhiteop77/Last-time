# TG Automation Bot — multi-account edition

A Telegram automation bot that:

1. Watches a **source channel** for posts containing bot links
2. Opens each link with one of your **logged-in user accounts** to fetch the files
3. Saves those files to a **DB channel** — by copying them, or by
   **downloading and re-uploading** them when saving is restricted
4. Asks your **second bot** to generate a fresh shareable link
5. Replaces the old link in the post and publishes it to an **output channel**

Everything is paced with short, configurable waits so no account gets
rate-limited by Telegram.

---

## What's new in this version

### 1. Multi-login (a pool of user accounts)

You are no longer limited to one user account. Send `/login` as many times as
you like — each account is stored (as an encrypted session string) in MongoDB
and reconnects automatically after every restart.

Work is spread across the pool **round-robin**:

| Role | Which account | Why |
|---|---|---|
| Listener | fixed, `/setlistener <n>` (default 1) | watches the source channel and publishes the output |
| Worker | rotating, any healthy account | opens bot links, downloads and uploads files |
| Link generator | fixed, `/setlinkaccount <n>` (default 1) | talks to your second bot, which usually only trusts its owner |

An account that hits a Telegram FloodWait is put on a cooldown and skipped
until it recovers, so the queue keeps moving on the other accounts.

Commands: `/accounts`, `/login`, `/removeaccount`, `/pauseaccount`,
`/resumeaccount`, `/setlistener`, `/setlinkaccount`, `/rotate on|off`,
`/joinall`.

> `/joinall` subscribes every account to your source, DB and output channels.
> Run it after adding a new account — a worker can only upload to the DB
> channel if it is a member of it.

### 2. Restricted / protected content mode

When the source bot or channel has **"restrict saving content"** enabled,
files cannot be re-sent by reference. The bot now downloads the file to the
server and uploads it again as a brand-new file.

`/setmode auto|download|copy`

* **auto** (default) — copy normally; automatically switch to
  download + upload the moment a protected file is detected, and also if a
  copy attempt is rejected
* **download** — always download then upload (safest, uses more bandwidth)
* **copy** — always re-send by reference (fastest, fails on protected files)

The same logic applies when publishing to the output channel: if the source
post itself is protected, its photos/videos are downloaded and re-uploaded
instead of forwarded. Files stream through a temp file, not memory, so large
videos work on small servers.

### 3. Tunable pacing

Every wait is configurable, with defaults tuned to be safe but not slow:

| Name | Default | Applies |
|---|---|---|
| `between_copies` | 2.5 s | between two files saved to the DB channel |
| `after_copy_batch` | 4 s | after all files of one link are saved |
| `conversation_step` | 1.8 s | between two messages sent to a bot |
| `between_links` | 4 s | between two links in the same post |
| `between_posts` | 6 s | between two posts |
| `account_cooldown` | 5 s | rest for an account after a job |

`/delays` to view, `/setdelay between_posts 5` to change one,
`/resetdelays` to restore the defaults. With several accounts in the pool you
can safely keep these low, because consecutive jobs land on different numbers.

---

## Setup

### 1. Deploy

Render (blueprint included in `render.yaml`) or any host that can run a
Python 3.11 worker:

```bash
pip install -r requirements.txt
python main.py
```

Environment variables:

| Variable | Description |
|---|---|
| `BOT_TOKEN` | Admin bot token from @BotFather |
| `API_ID` | Telegram API ID from my.telegram.org |
| `API_HASH` | Telegram API hash from my.telegram.org |
| `MONGODB_URI` | MongoDB connection string |
| `SESSION_STRING` | Optional — seeds the first account (otherwise use `/login`) |

### 2. Add your accounts

In the admin bot:

```
/login          → phone number → OTP → (2FA password if set)
/login          → repeat for every extra account
/accounts       → check they are all green
```

### 3. Configure channels

```
/setsource  <id|@username>    channel to monitor
/setdb      <id>              where files are stored
/setoutput  <id>              where processed posts go
/setsecondbot <@username>     bot that generates the new links
/setlog     <id>              optional status channel
/joinall                      subscribe every account to the above
/enable                       start
```

### 4. Tune (optional)

```
/setmode auto         how protected files are handled
/delays               current waiting times
/rotate on            spread work across accounts
```

---

## Second bot protocol

* Single file: `/genlink` → send the DB message link
* Multiple files: `/batch` → send the first link, then the last link

The second bot should reply with a message (or button) containing the
generated URL.

---

## Commands

Send `/help` in the admin bot for the full, always-current list.

---

## Files

```
├── main.py                 # entry point: web health check + admin bot + userbot pool
├── config.py               # environment variables
├── database.py             # MongoDB: config, sessions, delays, file mappings
├── setup_session.py        # optional first-account session generator
├── requirements.txt
├── render.yaml
├── userbot/
│   ├── pool.py             # multi-account pool: login, rotation, cooldowns
│   └── client.py           # listening, scanning, link extraction
└── bot/
    ├── app.py              # python-telegram-bot setup
    ├── processor.py        # the automation pipeline (incl. restricted mode)
    └── handlers/
        └── admin.py        # all admin commands
```

---

## Notes & limits

* Telegram user accounts are subject to Telegram's own limits; very low delay
  values can still get an account temporarily restricted. The defaults here
  are conservative but brisk.
* A worker account must be a member of the DB channel (and able to post) —
  run `/joinall` after adding an account.
* Changing the listener account takes effect after a restart.
* Uploading re-encodes nothing: the file is stored byte-identical, only the
  file reference changes.
