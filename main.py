"""
Entry point for the MEXC Passive Market Maker bot.

Startup sequence:
1. Validate required environment variables.
2. Build the Telegram Application.
3. Start polling in the asyncio event loop.

The Telegram Application's built-in event loop handles all concurrency;
WebSocket tasks and the Virtual SL watcher are spawned as asyncio.Tasks
from within command handlers, so no additional thread management is needed.
"""

import asyncio
import logging
import sys

from config.settings import validate_env
from bot.telegram_bot import build_application

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
# Reduce noise from third-party libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    # Fail fast if credentials are missing
    missing = validate_env()
    if missing:
        logger.error("Missing required environment variables: %s", ", ".join(missing))
        logger.error("Copy .env.example to .env and fill in the values.")
        sys.exit(1)

    logger.info("Starting MEXC Market Maker bot…")
    app = build_application()

    # run_polling() manages its own event loop and handles graceful shutdown
    # on SIGINT / SIGTERM automatically (python-telegram-bot v20+).
    app.run_polling(
        allowed_updates=["message"],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
