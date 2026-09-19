import motor.motor_asyncio
from config import MONGODB_URI

client = motor.motor_asyncio.AsyncIOMotorClient(MONGODB_URI)
db = client["tgbot"]

config_col = db["config"]
files_col  = db["files"]
logs_col   = db["logs"]


# ── Default pacing (seconds) ──────────────────────────────────────────────────
# Tuned to stay well under Telegram's flood limits without dragging: every
# value can be changed live with /setdelay <key> <seconds>.
DEFAULT_DELAYS = {
    "between_copies":     2.5,   # between two files uploaded to the DB channel
    "after_copy_batch":   4.0,   # after all files of one link are stored
    "conversation_step":  1.8,   # between two messages in a bot conversation
    "between_links":      4.0,   # between two links inside the same post
    "between_posts":      6.0,   # between two source posts
    "account_cooldown":   5.0,   # rest for an account after it finishes a job
}

DEFAULT_CONFIG = {
    "_id": "main",
    "source_channel": None,
    "db_channel": None,
    "output_channel": None,
    "second_bot_username": None,
    "log_channel": None,
    "admins": [],
    "enabled_commands": [],
    "active": False,
    "caption_template": "",
    "strip_links": False,
    "keep_caption": False,
    "text_rules": [],
    # multi-account pool
    "sessions": [],
    "listener_index": 1,
    "link_account_index": 1,
    "rotate_accounts": True,
    # restricted-content handling: auto | download | copy
    "save_mode": "auto",
    "delays": dict(DEFAULT_DELAYS),
}


async def get_config() -> dict:
    doc = await config_col.find_one({"_id": "main"})
    if doc is None:
        doc = dict(DEFAULT_CONFIG)
        await config_col.insert_one(doc)
        return doc

    # Backfill keys added by newer versions so old deployments keep working.
    missing = {k: v for k, v in DEFAULT_CONFIG.items() if k not in doc}
    if missing:
        await config_col.update_one({"_id": "main"}, {"$set": missing})
        doc.update(missing)
    return doc


async def update_config(key: str, value) -> None:
    await config_col.update_one(
        {"_id": "main"},
        {"$set": {key: value}},
        upsert=True,
    )


# ── Delays ────────────────────────────────────────────────────────────────────

async def get_delays() -> dict:
    cfg = await get_config()
    delays = dict(DEFAULT_DELAYS)
    delays.update(cfg.get("delays") or {})
    return delays


async def set_delay(key: str, seconds: float) -> None:
    delays = await get_delays()
    delays[key] = seconds
    await update_config("delays", delays)


async def reset_delays() -> None:
    await update_config("delays", dict(DEFAULT_DELAYS))


async def set_all_delays(seconds: float) -> None:
    """Apply the same value to every delay step (see /setalldelay)."""
    delays = {k: seconds for k in DEFAULT_DELAYS}
    await update_config("delays", delays)


# ── Multi-account sessions ────────────────────────────────────────────────────

async def get_sessions() -> list:
    doc = await config_col.find_one({"_id": "main"})
    return (doc or {}).get("sessions", []) or []


async def save_sessions(sessions: list) -> None:
    await config_col.update_one(
        {"_id": "main"},
        {"$set": {"sessions": sessions}},
        upsert=True,
    )


async def save_session_string(session_str: str) -> None:
    """Legacy single-session field, kept in sync with the first pool account."""
    await config_col.update_one(
        {"_id": "main"},
        {"$set": {"session_string": session_str}},
        upsert=True,
    )


async def get_session_string() -> str:
    doc = await config_col.find_one({"_id": "main"})
    return (doc or {}).get("session_string", "") or ""


# ── Files & logs ──────────────────────────────────────────────────────────────

async def save_file_mapping(original_msg_id: int, original_link: str, db_msg_ids: list, new_link: str) -> None:
    await files_col.insert_one({
        "original_msg_id": original_msg_id,
        "original_link":   original_link,
        "db_msg_ids":      db_msg_ids,
        "new_link":        new_link,
    })


async def get_file_mapping(original_msg_id: int) -> dict | None:
    return await files_col.find_one({"original_msg_id": original_msg_id})


async def log_event(event_type: str, data: dict) -> None:
    import datetime
    await logs_col.insert_one({
        "type":      event_type,
        "data":      data,
        "timestamp": datetime.datetime.utcnow(),
    })
