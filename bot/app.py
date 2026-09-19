import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from telegram.ext import ApplicationBuilder
from config import BOT_TOKEN
from bot.handlers.admin import register_handlers


def build_app():
    # concurrent_updates: commands are handled in parallel, so a long-running
    # job can never block /status, /stop, /setdelay, … or another admin's chat.
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )
    register_handlers(app)
    return app
