import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN   = os.environ["BOT_TOKEN"]
API_ID      = int(os.environ["API_ID"])
API_HASH    = os.environ["API_HASH"]
MONGODB_URI = os.environ["MONGODB_URI"]

SESSION_NAME = "userbot_session"

# On Render the filesystem is ephemeral — store session as a string instead.
# Generate it once with:  python setup_session.py
# Then paste the output into Render → Environment → SESSION_STRING
SESSION_STRING = os.environ.get("SESSION_STRING", "")

# Port that Render injects; also used by the health-check web server
PORT = int(os.environ.get("PORT", 8080))
