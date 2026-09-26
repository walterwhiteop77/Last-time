"""
Storage layer — multi-user (one isolated workspace per admin).

Every admin owns a *workspace*: their own userbot accounts, their own source /
storage / output channels, their own delays, rules and jobs. A workspace is a
single Mongo document with `_id = "ws:<telegram_user_id>"`.

Global (shared) settings — the admin list and the owner — live in one extra
document with `_id = "global"`.

Which workspace a piece of code is working on is carried in a contextvar
(`current_ws`) that the admin-command decorator and each workspace's listener
task set. Every helper also accepts an explicit `ws=` argument.
"""

import contextvars
import datetime

import motor.motor_asyncio

from config import MONGODB_URI, OWNER_ID

client = motor.motor_asyncio.AsyncIOMotorClient(MONGODB_URI)
db = client["tgbot"]

config_col = db["config"]
files_col  = db["files"]
logs_col   = db["logs"]

GLOBAL_ID = "global"
LEGACY_ID = "main"


# ── Current workspace (per task) ──────────────────────────────────────────────

current_ws: contextvars.ContextVar = contextvars.ContextVar("current_ws", default=None)


def set_ws(ws) -> None:
    current_ws.set(int(ws))


def get_ws():
    return current_ws.get()


def _resolve(ws=None) -> int:
    ws = ws if ws is not None else current_ws.get()
    if ws is None:
        raise RuntimeError("No workspace selected (internal error).")
    return int(ws)


def ws_doc_id(ws) -> str:
    return f"ws:{int(ws)}"


# ── Default pacing (seconds) ──────────────────────────────────────────────────
DEFAULT_DELAYS = {
    "between_copies":     2.5,
    "after_copy_batch":   4.0,
    "conversation_step":  1.8,
    "between_links":      4.0,
    "between_posts":      6.0,
    "account_cooldown":   5.0,
}

DEFAULT_WORKSPACE = {
    "source_channel": None,
    "db_channel": None,
    "output_channel": None,
    "second_bot_username": None,
    "log_channel": None,
    "enabled_commands": [],
    "active": False,
    "caption_template": "",
    "strip_links": False,
    "keep_caption": False,
    "text_rules": [],
    "scan_start_id": 0,
    # this workspace's own userbot accounts
    "sessions": [],
    "listener_index": 1,
    "link_account_index": 1,
    "rotate_accounts": True,
    # restricted-content handling: auto | download | copy
    "save_mode": "auto",
    "delays": dict(DEFAULT_DELAYS),
}


# ── Workspace config ──────────────────────────────────────────────────────────

async def get_config(ws=None) -> dict:
    uid = _resolve(ws)
    doc_id = ws_doc_id(uid)
    doc = await config_col.find_one({"_id": doc_id})
    if doc is None:
        doc = dict(DEFAULT_WORKSPACE)
        doc["_id"] = doc_id
        doc["owner"] = uid
        await config_col.insert_one(doc)
        return doc

    missing = {k: v for k, v in DEFAULT_WORKSPACE.items() if k not in doc}
    if missing:
        await config_col.update_one({"_id": doc_id}, {"$set": missing})
        doc.update(missing)
    # `admins` is global now, but old code may read cfg["admins"]
    doc["admins"] = await get_admins()
    return doc


async def update_config(key: str, value, ws=None) -> None:
    uid = _resolve(ws)
    await config_col.update_one(
        {"_id": ws_doc_id(uid)},
        {"$set": {key: value, "owner": uid}},
        upsert=True,
    )


async def ensure_workspace(ws) -> dict:
    return await get_config(ws)


async def delete_workspace(ws) -> None:
    await config_col.delete_one({"_id": ws_doc_id(ws)})


async def list_workspaces() -> list:
    """All workspace owner IDs that exist in the database."""
    out = []
    cursor = config_col.find({"_id": {"$regex": "^ws:"}}, {"_id": 1})
    async for doc in cursor:
        try:
            out.append(int(str(doc["_id"]).split(":", 1)[1]))
        except (ValueError, IndexError):
            continue
    return out


# ── Global settings (admins / owner) ──────────────────────────────────────────

async def get_global() -> dict:
    doc = await config_col.find_one({"_id": GLOBAL_ID})
    if doc is None:
        doc = {"_id": GLOBAL_ID, "admins": [], "owner": OWNER_ID or None}
        await config_col.insert_one(doc)
    return doc


async def get_admins() -> list:
    doc = await get_global()
    admins = list(doc.get("admins") or [])
    owner = doc.get("owner") or OWNER_ID
    if owner and owner not in admins:
        admins.append(int(owner))
    return admins


async def add_admin(uid: int) -> None:
    doc = await get_global()
    admins = list(doc.get("admins") or [])
    if int(uid) not in admins:
        admins.append(int(uid))
    await config_col.update_one({"_id": GLOBAL_ID}, {"$set": {"admins": admins}}, upsert=True)
    await ensure_workspace(uid)


async def remove_admin(uid: int) -> bool:
    doc = await get_global()
    admins = list(doc.get("admins") or [])
    if int(uid) not in admins:
        return False
    admins.remove(int(uid))
    await config_col.update_one({"_id": GLOBAL_ID}, {"$set": {"admins": admins}}, upsert=True)
    return True


async def get_owner():
    doc = await get_global()
    return doc.get("owner") or (OWNER_ID or None)


async def set_owner(uid: int) -> None:
    await config_col.update_one({"_id": GLOBAL_ID}, {"$set": {"owner": int(uid)}}, upsert=True)


async def is_admin(uid: int) -> bool:
    return int(uid) in [int(a) for a in await get_admins()]


# ── Migration from the old single-workspace layout ────────────────────────────

async def migrate_legacy() -> int | None:
    """
    Move the old shared `main` config into the owner's personal workspace.
    Returns the owner id when a migration happened / an owner is known.
    """
    legacy = await config_col.find_one({"_id": LEGACY_ID})
    glob = await get_global()

    owner = glob.get("owner") or OWNER_ID or None
    legacy_admins = list((legacy or {}).get("admins") or [])
    if not owner and legacy_admins:
        owner = int(legacy_admins[0])

    updates = {}
    if owner:
        updates["owner"] = int(owner)
    merged = list({int(a) for a in (list(glob.get("admins") or []) + legacy_admins)})
    if merged != list(glob.get("admins") or []):
        updates["admins"] = merged
    if updates:
        await config_col.update_one({"_id": GLOBAL_ID}, {"$set": updates}, upsert=True)

    if legacy and owner:
        target = ws_doc_id(owner)
        existing = await config_col.find_one({"_id": target})
        if existing is None:
            doc = {k: v for k, v in legacy.items() if k not in ("_id", "admins")}
            doc["_id"] = target
            doc["owner"] = int(owner)
            for k, v in DEFAULT_WORKSPACE.items():
                doc.setdefault(k, v)
            await config_col.insert_one(doc)
            await config_col.update_one({"_id": LEGACY_ID}, {"$set": {"migrated_to": target}})
            print(f"[db] Migrated the old shared setup into workspace {target}")
    return owner


# ── Delays ────────────────────────────────────────────────────────────────────

async def get_delays(ws=None) -> dict:
    cfg = await get_config(ws)
    delays = dict(DEFAULT_DELAYS)
    delays.update(cfg.get("delays") or {})
    return delays


async def set_delay(key: str, seconds: float, ws=None) -> None:
    delays = await get_delays(ws)
    delays[key] = seconds
    await update_config("delays", delays, ws)


async def reset_delays(ws=None) -> None:
    await update_config("delays", dict(DEFAULT_DELAYS), ws)


async def set_all_delays(seconds: float, ws=None) -> None:
    await update_config("delays", {k: seconds for k in DEFAULT_DELAYS}, ws)


# ── Per-workspace userbot sessions ────────────────────────────────────────────

async def get_sessions(ws=None) -> list:
    uid = _resolve(ws)
    doc = await config_col.find_one({"_id": ws_doc_id(uid)})
    return (doc or {}).get("sessions", []) or []


async def save_sessions(sessions: list, ws=None) -> None:
    await update_config("sessions", sessions, ws)


async def get_session_string(ws=None) -> str:
    uid = _resolve(ws)
    doc = await config_col.find_one({"_id": ws_doc_id(uid)})
    return (doc or {}).get("session_string", "") or ""


async def save_session_string(session_str: str, ws=None) -> None:
    await update_config("session_string", session_str, ws)


# ── Files & logs ──────────────────────────────────────────────────────────────

async def save_file_mapping(original_msg_id: int, original_link: str, db_msg_ids: list,
                            new_link: str, ws=None) -> None:
    await files_col.insert_one({
        "ws":              _resolve(ws),
        "original_msg_id": original_msg_id,
        "original_link":   original_link,
        "db_msg_ids":      db_msg_ids,
        "new_link":        new_link,
    })


async def get_file_mapping(original_msg_id: int, ws=None) -> dict | None:
    return await files_col.find_one({"ws": _resolve(ws), "original_msg_id": original_msg_id})


async def log_event(event_type: str, data: dict, ws=None) -> None:
    try:
        who = _resolve(ws)
    except RuntimeError:
        who = None
    await logs_col.insert_one({
        "ws":        who,
        "type":      event_type,
        "data":      data,
        "timestamp": datetime.datetime.utcnow(),
    })
