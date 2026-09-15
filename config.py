import os
from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            "Set it in your .env file or in the host's environment settings."
        )
    return value


BOT_TOKEN   = _required("BOT_TOKEN")
API_HASH    = _required("API_HASH")
MONGODB_URI = _required("MONGODB_URI")

try:
    API_ID = int(_required("API_ID"))
except ValueError as exc:
    raise RuntimeError("API_ID must be a number (from my.telegram.org).") from exc

SESSION_NAME = "userbot_session"

# On hosts with an ephemeral filesystem the session is stored as a string.
# Generate it once with:  python setup_session.py
SESSION_STRING = os.environ.get("SESSION_STRING", "")

# Port injected by the host; also used by the health-check web server
PORT = int(os.environ.get("PORT", 8080))
