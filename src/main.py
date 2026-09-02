"""Entry point. Wires everything and starts the bot + scheduler.

Run: `python -m src.main` from the project root.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import structlog
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
import uvicorn

from telegram.ext import Application

from .claude_client import make_client
from .config import load_settings, load_users_config
from .db import Database
from .channel_bot import ChannelBot
from .scheduler import start_scheduler
from .social_publisher import InstagramPublisher, XPublisher
from .telegram_bot import build_application


def configure_logging(log_level: str, log_file: str) -> None:
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
        format="%(message)s",
        force=True,
    )
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def build_api(ig_uploads_dir: str) -> FastAPI:
    """Tiny FastAPI app that serves rendered IG card images so Meta's Graph
    API can fetch them by URL. Single endpoint, no auth — the random filename
    is the secret."""
    api = FastAPI(title="Marketing Bot API", docs_url=None, redoc_url=None)

    @api.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @api.get("/ig-image/{filename}")
    async def ig_image(filename: str) -> FileResponse:
        if (not filename.endswith(".png") or "/" in filename
                or "\\" in filename or ".." in filename):
            raise HTTPException(status_code=404, detail="not_found")
        # The publisher stores temp images in data/ig_temp/, not ig_uploads_dir.
        file_path = Path("./data/ig_temp") / filename
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="not_found")
        return FileResponse(file_path, media_type="image/png")

    return api


async def _amain() -> None:
    settings = load_settings()
    configure_logging(settings.log_level, settings.log_file)
    log = structlog.get_logger()

    users_cfg = load_users_config("users.yaml")
    db = Database(settings.database_path)
    await db.init_schema()

    # Seed users table from users.yaml.
    for u in users_cfg.users:
        await db.upsert_user(
            user_key=u.user_key,
            telegram_user_id=u.telegram_user_id,
            display_name=u.display_name,
            default_language=u.default_language,
            timezone=u.timezone,
            daily_call_limit=u.daily_call_limit,
        )

    client = make_client(settings.anthropic_api_key)

    # ---- Optional integrations ----
    channel_bot: ChannelBot | None = None
    if settings.telegram_bot_token_channel and settings.channel_handle:
        try:
            channel_bot = ChannelBot(
                token=settings.telegram_bot_token_channel,
                channel_handle=settings.channel_handle,
            )
            log.info("channel_bot_initialized",
                     channel=channel_bot.channel_handle)
        except Exception:
            log.exception("channel_bot_init_failed")
    else:
        log.info("channel_bot_disabled_no_config")

    x_publisher = XPublisher(
        api_key=settings.x_api_key,
        api_secret=settings.x_api_secret,
        access_token=settings.x_access_token,
        access_token_secret=settings.x_access_token_secret,
        handle=settings.x_handle,
    )
    ig_publisher = InstagramPublisher(
        access_token=settings.ig_access_token,
        business_account_id=settings.ig_business_account_id,
        public_base_url=settings.ig_public_base_url,
    )
    log.info(
        "publishers_configured",
        x_configured=x_publisher.is_configured(),
        ig_configured=ig_publisher.is_configured(),
    )

    # ---- Telegram operator bot ----
    apps_by_user_id: dict[int, Application] = {}

    tg_app = build_application(
        token=settings.telegram_bot_token_operator,
        db=db,
        client=client,
        channel_bot=channel_bot,
        apps_by_user_id=apps_by_user_id,
        notify_user_key="A",
        ig_publisher=ig_publisher,
        ig_templates_dir=settings.ig_templates_dir,
        ig_uploads_dir=settings.ig_uploads_dir,
        x_publisher=x_publisher,
        strategy_notes_path=settings.strategy_notes_path,
    )

    log.info("starting_bot")
    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling(
        allowed_updates=["message", "callback_query"],
    )

    # Populate the shared dict so handlers can DM the operator (e.g. for IG
    # workflow follow-ups).
    db_user = await db.get_user_by_key("A")
    if db_user is not None:
        apps_by_user_id[db_user.user_id] = tg_app

    # ---- Scheduled jobs ----
    scheduler = start_scheduler(
        db, client, apps_by_user_id,
        notify_user_key="A",
        strategy_notes_path=settings.strategy_notes_path,
    )

    # ---- FastAPI for IG image serving ----
    api = build_api(settings.ig_uploads_dir)
    api_config = uvicorn.Config(
        api, host="0.0.0.0", port=8080,
        log_level="warning", access_log=False,
    )
    api_config.install_signal_handlers = False
    api_server = uvicorn.Server(api_config)
    api_task = asyncio.create_task(api_server.serve())

    log.info("all_running", api_port=8080)
    try:
        stop = asyncio.Event()
        await stop.wait()
    finally:
        log.info("shutting_down")
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            log.exception("scheduler_shutdown_error")
        api_server.should_exit = True
        try:
            await asyncio.wait_for(api_task, timeout=5)
        except Exception:
            log.exception("api_shutdown_error")
        try:
            await tg_app.updater.stop()
            await tg_app.stop()
            await tg_app.shutdown()
        except Exception:
            log.exception("bot_shutdown_error")


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
