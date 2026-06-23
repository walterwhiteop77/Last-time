import asyncio
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from telegram.ext import ApplicationBuilder
from config import BOT_TOKEN
from bot.handlers.admin import register_handlers


def build_app():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    register_handlers(app)
    return app
