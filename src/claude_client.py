"""Anthropic SDK wrapper + tool definitions.

`make_client()` returns an AsyncAnthropic.
`TOOLS` is the list of function-call definitions Claude can invoke.
"""
from __future__ import annotations

from typing import Any

from anthropic import AsyncAnthropic

# Default model. Override here or per-call if you want.
DEFAULT_MODEL = "claude-haiku-4-5-20251001"


def make_client(api_key: str) -> AsyncAnthropic:
    return AsyncAnthropic(api_key=api_key)


# Cost-tracking constants for Haiku 4.5. Update when switching models.
INPUT_COST_PER_M = 1.00   # $1.00 / M input tokens
OUTPUT_COST_PER_M = 5.00  # $5.00 / M output tokens


def cost_usd(input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens * INPUT_COST_PER_M / 1_000_000
        + output_tokens * OUTPUT_COST_PER_M / 1_000_000
    )


# ---- Tool definitions (Anthropic function calling) -----------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "generate_draft",
        "description": (
            "Generate a marketing post draft for one of the supported "
            "post types: 'daily_recap', 'weekly_review', or "
            "'monthly_report'. The bot drafts via Claude using the "
            "brand voice anchor and real data, stores the draft as a "
            "pending_post in the DB, and DMs the operator a message with "
            "approval buttons (✅ Publish / ✏️ Edit / ❌ Skip). "
            "Use this when the user says 'draft a daily recap', 'write a "
            "weekly review', etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "post_type": {
                    "type": "string",
                    "enum": ["daily_recap", "weekly_review", "monthly_report"],
                },
                "target_date": {
                    "type": "string",
                    "description": "Optional ISO date (YYYY-MM-DD). Defaults to today.",
                },
            },
            "required": ["post_type"],
        },
    },
    {
        "name": "approve_pending_post",
        "description": (
            "Approve a pending draft by ID — publishes to Telegram channel + "
            "kicks off IG manual workflow + auto-posts to X. Use ONLY when "
            "the user explicitly approves: 'publish', 'send it', 'approve', "
            "'post that'. Never auto-approve."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"pending_id": {"type": "integer"}},
            "required": ["pending_id"],
        },
    },
    {
        "name": "reject_pending_post",
        "description": "Reject (skip) a pending draft. Use on 'skip', 'reject', 'discard'.",
        "input_schema": {
            "type": "object",
            "properties": {"pending_id": {"type": "integer"}},
            "required": ["pending_id"],
        },
    },
    {
        "name": "update_pending_post",
        "description": (
            "Replace the draft text of a pending post with a new version. "
            "Use when the user gives free-form edit feedback (e.g. 'shorter', "
            "'drop the second paragraph', 'change the tone'). Write the "
            "revised content yourself, then call this tool with it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pending_id": {"type": "integer"},
                "new_content": {"type": "string"},
            },
            "required": ["pending_id", "new_content"],
        },
    },
    {
        "name": "list_pending_posts",
        "description": "List recent drafts awaiting approval.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "default": "pending"},
                "limit": {"type": "integer", "default": 10},
            },
        },
    },
    {
        "name": "list_published_posts",
        "description": "List recent successfully-published posts.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 10}},
        },
    },
    {
        "name": "publish_to_channel",
        "description": (
            "Publish raw content directly to the Telegram channel without "
            "the draft/approval flow. Use sparingly — most posts should go "
            "through generate_draft → approve_pending_post for safety."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "post_type": {"type": "string", "default": "custom"},
            },
            "required": ["content"],
        },
    },
    {
        "name": "post_to_x",
        "description": (
            "Post a text message directly to X. Use when user says 'post "
            "this on X', 'tweet this', etc. The user-supplied text IS the "
            "approval — post it as-is, do not ask again. Bot appends brand "
            "hashtags by default. Threading is automatic if content exceeds "
            "280 chars."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "add_hashtags": {"type": "boolean", "default": True},
            },
            "required": ["text"],
        },
    },
    {
        "name": "get_strategy_voice_anchor",
        "description": (
            "Read the brand voice anchor file (strategy_notes.md). Useful "
            "when the user asks 'what's our voice' or 'remind me of the "
            "brand rules'."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]
