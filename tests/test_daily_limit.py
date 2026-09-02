"""Daily call-limit enforcement in the Telegram text handler.

build_application registers closures on a real python-telegram-bot
Application (offline: nothing is started), and the text handler is invoked
directly with a duck-typed Update. route_message is patched out so these
tests only exercise the whitelist + rate-limit gate.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.ext import MessageHandler

import src.telegram_bot as telegram_bot_module
from src.telegram_bot import build_application

FAKE_TOKEN = "1111111111:TEST-token-not-real"


def get_text_handler(app):
    handlers = app.handlers[0]
    # CommandHandler(start) is first; the text MessageHandler is second.
    handler = handlers[1]
    assert isinstance(handler, MessageHandler)
    return handler.callback


def make_update(telegram_user_id: int, text: str = "draft a daily recap"):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=telegram_user_id),
        message=SimpleNamespace(
            text=text,
            reply_text=AsyncMock(),
            chat=SimpleNamespace(send_action=AsyncMock()),
        ),
    )


@pytest.fixture
def route_mock(monkeypatch):
    mock = AsyncMock(return_value="routed reply")
    monkeypatch.setattr(telegram_bot_module, "route_message", mock)
    return mock


@pytest.fixture
def handle_text(db):
    app = build_application(FAKE_TOKEN, db, client=None)
    return get_text_handler(app)


async def test_under_limit_routes_message(db, user, handle_text, route_mock):
    update = make_update(user.telegram_user_id)
    await handle_text(update, None)
    route_mock.assert_awaited_once()
    update.message.reply_text.assert_awaited_once_with("routed reply")


async def test_at_limit_blocks_message(db, user, handle_text, route_mock):
    for _ in range(user.daily_call_limit):  # fixture limit is 5
        await db.log_usage(user.user_id, 100, 50, 0.001)
    update = make_update(user.telegram_user_id)
    await handle_text(update, None)
    route_mock.assert_not_awaited()
    reply = update.message.reply_text.await_args.args[0]
    assert "Daily limit reached" in reply


async def test_one_below_limit_still_routes(db, user, handle_text, route_mock):
    for _ in range(user.daily_call_limit - 1):
        await db.log_usage(user.user_id, 100, 50, 0.001)
    update = make_update(user.telegram_user_id)
    await handle_text(update, None)
    route_mock.assert_awaited_once()


async def test_non_whitelisted_user_rejected(db, user, handle_text, route_mock):
    update = make_update(999999)  # not in users table
    await handle_text(update, None)
    route_mock.assert_not_awaited()
    update.message.reply_text.assert_awaited_once_with("This bot is private.")


async def test_router_exception_yields_apology(db, user, handle_text, route_mock):
    route_mock.side_effect = RuntimeError("upstream boom")
    update = make_update(user.telegram_user_id)
    await handle_text(update, None)
    reply = update.message.reply_text.await_args.args[0]
    assert reply == "Sorry, something went wrong. Please try again."


async def test_usage_today_counts_only_this_user(db, user):
    await db.log_usage(user.user_id, 1, 1, 0.0)
    await db.log_usage(user.user_id, 1, 1, 0.0)
    other_id = await db.upsert_user(
        user_key="B", telegram_user_id=222222, display_name="Other",
        default_language="English", timezone="UTC", daily_call_limit=5,
    )
    await db.log_usage(other_id, 1, 1, 0.0)
    assert await db.usage_today(user.user_id) == 2
    assert await db.usage_today(other_id) == 1
