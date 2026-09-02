"""Shared fixtures + Anthropic response stubs.

All tests run fully offline: the Anthropic client is a hand-rolled stub
(FakeAnthropicClient) that replays canned responses, Telegram apps are
AsyncMock bots, and the X/IG publishers have their network entry points
monkeypatched. The only real I/O is a per-test SQLite file in tmp_path.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

# Make `src` importable when tests run from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.db import Database  # noqa: E402
from src.models import User  # noqa: E402


# ---- Anthropic API stubs -------------------------------------------------

class FakeTextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class FakeToolUseBlock:
    type = "tool_use"

    def __init__(self, name: str, input: dict, id: str = "toolu_test_001") -> None:
        self.name = name
        self.input = input
        self.id = id


class FakeResponse:
    def __init__(self, content, stop_reason="end_turn",
                 input_tokens=100, output_tokens=50) -> None:
        self.content = content
        self.stop_reason = stop_reason
        self.usage = SimpleNamespace(
            input_tokens=input_tokens, output_tokens=output_tokens
        )


class FakeMessages:
    """Replays a scripted list of responses; records every create() call."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def create(self, **kwargs) -> FakeResponse:
        self.calls.append(kwargs)
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]  # repeat the last response forever


class FakeAnthropicClient:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.messages = FakeMessages(responses)


def end_turn(text: str, **kw) -> FakeResponse:
    return FakeResponse([FakeTextBlock(text)], stop_reason="end_turn", **kw)


def tool_call(name: str, input: dict, id: str = "toolu_test_001", **kw) -> FakeResponse:
    return FakeResponse(
        [FakeToolUseBlock(name, input, id=id)], stop_reason="tool_use", **kw
    )


# ---- Telegram app stub ---------------------------------------------------

def make_fake_app():
    """Duck-typed telegram Application: just a bot with async send methods."""
    bot = SimpleNamespace(
        send_message=AsyncMock(),
        send_photo=AsyncMock(),
    )
    return SimpleNamespace(bot=bot)


# ---- Fixtures ------------------------------------------------------------

@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.init_schema()
    return database


@pytest.fixture
async def user(db):
    user_id = await db.upsert_user(
        user_key="A",
        telegram_user_id=111111,
        display_name="Test Operator",
        default_language="English",
        timezone="UTC",
        daily_call_limit=5,
    )
    return User(
        user_id=user_id,
        user_key="A",
        telegram_user_id=111111,
        display_name="Test Operator",
        default_language="English",
        timezone="UTC",
        daily_call_limit=5,
    )
