"""Routes incoming user messages through Claude's tool-use loop.

The flow:
1. Build messages = [recent history] + [new user message]
2. Call Claude with TOOLS + system prompt
3. If Claude returns tool_use blocks, execute each tool and feed results back
4. Loop until Claude returns end_turn with text
5. Save the final text to conversation_history + log usage
"""
from __future__ import annotations

from typing import Any

import structlog
from anthropic import AsyncAnthropic
from anthropic.types import Message

from .claude_client import DEFAULT_MODEL, TOOLS, cost_usd
from .db import Database
from .channel_bot import ChannelBot
from .marketing import (
    execute_approve_pending_post,
    execute_generate_draft,
    execute_get_strategy_voice_anchor,
    execute_list_pending_posts,
    execute_list_published_posts,
    execute_post_to_x,
    execute_publish_to_channel,
    execute_reject_pending_post,
    execute_update_pending_post,
)
from .models import (
    ApprovePendingPostInput,
    GenerateDraftInput,
    GetStrategyVoiceAnchorInput,
    ListPendingPostsInput,
    ListPublishedPostsInput,
    PostToXInput,
    PublishToChannelInput,
    RejectPendingPostInput,
    UpdatePendingPostInput,
    User,
)
from .prompts import render_system_prompt

log = structlog.get_logger()

HISTORY_KEEP = 10
MAX_TOOL_ROUNDS = 6


async def route_message(
    db: Database,
    client: AsyncAnthropic,
    user: User,
    user_message: str,
    *,
    channel_bot: ChannelBot | None = None,
    apps_by_user_id: dict | None = None,
    x_publisher: Any | None = None,
    strategy_notes_path: str = "./strategy_notes.md",
) -> str:
    system_prompt = render_system_prompt(user, strategy_notes_path)

    recent = await db.recent_messages(user.user_id, limit=HISTORY_KEEP)
    messages: list[dict[str, Any]] = list(recent)
    messages.append({"role": "user", "content": user_message})

    total_in = 0
    total_out = 0
    response: Message | None = None

    # Cache-able prefix to cut input cost on rapid follow-ups.
    system_param = [
        {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}},
    ]
    tools_param = [
        {**t, "cache_control": {"type": "ephemeral"}} if i == len(TOOLS) - 1 else t
        for i, t in enumerate(TOOLS)
    ]

    for _ in range(MAX_TOOL_ROUNDS):
        response = await client.messages.create(
            model=DEFAULT_MODEL,
            max_tokens=2048,
            system=system_param,
            tools=tools_param,
            messages=messages,
        )
        total_in += response.usage.input_tokens
        total_out += response.usage.output_tokens

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            result = await _execute_tool(
                db, user, block.name, block.input,
                channel_bot=channel_bot,
                apps_by_user_id=apps_by_user_id,
                anthropic_client=client,
                x_publisher=x_publisher,
                strategy_notes_path=strategy_notes_path,
            )
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": str(result),
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    # Extract the final text reply.
    final_text = ""
    if response is not None:
        for block in response.content:
            if block.type == "text":
                final_text += block.text

    await db.add_message(user.user_id, "user", user_message)
    if final_text:
        await db.add_message(user.user_id, "assistant", final_text)
    await db.log_usage(user.user_id, total_in, total_out,
                       cost_usd(total_in, total_out))
    log.info(
        "interaction",
        user_id=user.user_id,
        in_tokens=total_in,
        out_tokens=total_out,
        cost_usd=cost_usd(total_in, total_out),
    )
    return final_text or "(no response)"


async def _execute_tool(
    db: Database,
    user: User,
    name: str,
    args: dict[str, Any],
    *,
    channel_bot: ChannelBot | None,
    apps_by_user_id: dict | None,
    anthropic_client: AsyncAnthropic,
    x_publisher: Any | None,
    strategy_notes_path: str,
) -> dict[str, Any]:
    try:
        if name == "generate_draft":
            return await execute_generate_draft(
                db, user, GenerateDraftInput(**args),
                anthropic_client=anthropic_client,
                apps_by_user_id=apps_by_user_id or {},
                strategy_notes_path=strategy_notes_path,
            )
        if name == "approve_pending_post":
            return await execute_approve_pending_post(
                db, user, ApprovePendingPostInput(**args),
                channel_bot=channel_bot,
                apps_by_user_id=apps_by_user_id,
                anthropic_client=anthropic_client,
                x_publisher=x_publisher,
            )
        if name == "reject_pending_post":
            return await execute_reject_pending_post(
                db, user, RejectPendingPostInput(**args),
            )
        if name == "update_pending_post":
            return await execute_update_pending_post(
                db, user, UpdatePendingPostInput(**args),
            )
        if name == "list_pending_posts":
            return await execute_list_pending_posts(
                db, user, ListPendingPostsInput(**args),
            )
        if name == "list_published_posts":
            return await execute_list_published_posts(
                db, user, ListPublishedPostsInput(**args),
            )
        if name == "publish_to_channel":
            return await execute_publish_to_channel(
                db, user, PublishToChannelInput(**args),
                channel_bot=channel_bot,
            )
        if name == "post_to_x":
            inp_x = PostToXInput(**args)
            return await execute_post_to_x(
                user, inp_x.text, inp_x.add_hashtags,
                x_publisher=x_publisher,
            )
        if name == "get_strategy_voice_anchor":
            return await execute_get_strategy_voice_anchor(
                user, GetStrategyVoiceAnchorInput(**args),
                strategy_notes_path=strategy_notes_path,
            )
        return {"error": "unknown_tool", "name": name}
    except Exception as e:
        log.exception("tool_execution_error", tool=name, error=str(e))
        return {"error": "tool_execution_failed", "tool": name, "detail": str(e)}
