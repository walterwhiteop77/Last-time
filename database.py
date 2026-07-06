import motor.motor_asyncio
from config import MONGODB_URI

client = motor.motor_asyncio.AsyncIOMotorClient(MONGODB_URI)
db = client["tgbot"]

config_col = db["config"]
files_col  = db["files"]
logs_col   = db["logs"]


async def get_config() -> dict:
    doc = await config_col.find_one({"_id": "main"})
    if doc is None:
        doc = {
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
            "keep_caption": True,
            "text_rules": [],
        }
        await config_col.insert_one(doc)
    return doc


async def update_config(key: str, value) -> None:
    await config_col.update_one(
        {"_id": "main"},
        {"$set": {key: value}},
        upsert=True,
    )


async def save_session_string(session_str: str) -> None:
    """Persist the userbot session string to MongoDB so it survives restarts."""
    await config_col.update_one(
        {"_id": "main"},
        {"$set": {"session_string": session_str}},
        upsert=True,
    )


async def get_session_string() -> str:
    """Return the stored session string, or empty string if none."""
    doc = await config_col.find_one({"_id": "main"})
    return (doc or {}).get("session_string", "") or ""


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
