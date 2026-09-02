"""Router tool-use loop tests with a stubbed Anthropic client."""
from __future__ import annotations

import pytest

from src.claude_client import cost_usd
from src.router import HISTORY_KEEP, MAX_TOOL_ROUNDS, route_message
from conftest import FakeAnthropicClient, end_turn, tool_call


async def test_plain_text_reply(db, user):
    client = FakeAnthropicClient([end_turn("Hello, operator.")])
    reply = await route_message(db, client, user, "hi")
    assert reply == "Hello, operator."
    assert len(client.messages.calls) == 1


async def test_reply_saved_to_history(db, user):
    client = FakeAnthropicClient([end_turn("Saved reply.")])
    await route_message(db, client, user, "hi")
    history = await db.recent_messages(user.user_id)
    assert history == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "Saved reply."},
    ]


async def test_history_prepended_to_messages(db, user):
    await db.add_message(user.user_id, "user", "earlier question")
    await db.add_message(user.user_id, "assistant", "earlier answer")
    client = FakeAnthropicClient([end_turn("ok")])
    await route_message(db, client, user, "new question")
    sent = client.messages.calls[0]["messages"]
    assert sent[0] == {"role": "user", "content": "earlier question"}
    assert sent[1] == {"role": "assistant", "content": "earlier answer"}
    assert sent[-1] == {"role": "user", "content": "new question"}
    assert len(sent) <= HISTORY_KEEP + 1


async def test_tool_use_round_trip(db, user):
    client = FakeAnthropicClient([
        tool_call("list_pending_posts", {}, id="toolu_test_lp1"),
        end_turn("No drafts pending."),
    ])
    reply = await route_message(db, client, user, "show drafts")
    assert reply == "No drafts pending."
    assert len(client.messages.calls) == 2

    # Second API call must carry the assistant tool_use turn + tool_result.
    second = client.messages.calls[1]["messages"]
    assert second[-2]["role"] == "assistant"
    tool_results = second[-1]
    assert tool_results["role"] == "user"
    result_block = tool_results["content"][0]
    assert result_block["type"] == "tool_result"
    assert result_block["tool_use_id"] == "toolu_test_lp1"
    assert "'ok': True" in result_block["content"]


async def test_unknown_tool_returns_error_result(db, user):
    client = FakeAnthropicClient([
        tool_call("does_not_exist", {}),
        end_turn("done"),
    ])
    reply = await route_message(db, client, user, "hi")
    assert reply == "done"
    result_block = client.messages.calls[1]["messages"][-1]["content"][0]
    assert "unknown_tool" in result_block["content"]


async def test_tool_validation_error_is_caught(db, user):
    # Bad input type triggers a pydantic ValidationError inside _execute_tool;
    # the loop must survive and feed the error back as a tool_result.
    client = FakeAnthropicClient([
        tool_call("update_pending_post",
                  {"pending_id": "not-an-int", "new_content": "x"}),
        end_turn("recovered"),
    ])
    reply = await route_message(db, client, user, "hi")
    assert reply == "recovered"
    result_block = client.messages.calls[1]["messages"][-1]["content"][0]
    assert "tool_execution_failed" in result_block["content"]


async def test_max_tool_rounds_bound(db, user):
    # Client that returns tool_use forever: the loop must stop at
    # MAX_TOOL_ROUNDS API calls, not spin indefinitely.
    client = FakeAnthropicClient([tool_call("list_pending_posts", {})])
    reply = await route_message(db, client, user, "loop forever")
    assert len(client.messages.calls) == MAX_TOOL_ROUNDS
    assert reply == "(no response)"  # last response had no text block


async def test_multiple_tool_blocks_in_one_response(db, user):
    from conftest import FakeResponse, FakeToolUseBlock
    multi = FakeResponse(
        [
            FakeToolUseBlock("list_pending_posts", {}, id="toolu_test_a"),
            FakeToolUseBlock("list_published_posts", {}, id="toolu_test_b"),
        ],
        stop_reason="tool_use",
    )
    client = FakeAnthropicClient([multi, end_turn("both done")])
    reply = await route_message(db, client, user, "hi")
    assert reply == "both done"
    results = client.messages.calls[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["toolu_test_a", "toolu_test_b"]


async def test_usage_accumulated_across_rounds(db, user):
    client = FakeAnthropicClient([
        tool_call("list_pending_posts", {}, input_tokens=100, output_tokens=10),
        end_turn("done", input_tokens=200, output_tokens=30),
    ])
    await route_message(db, client, user, "hi")
    assert await db.usage_today(user.user_id) == 1  # one log row per interaction
    import aiosqlite
    async with aiosqlite.connect(db.path) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM usage_log")
        row = await cur.fetchone()
    assert row["input_tokens"] == 300
    assert row["output_tokens"] == 40
    assert row["cost_usd"] == pytest.approx(cost_usd(300, 40))
