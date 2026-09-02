"""Marketing tool executors.

This is the file you'll customize most. It contains:
- `_fetch_data_for_*` functions: replace with your real data source
  (DB, API, CSV — whatever produces the structured data your posts cite).
- Per-post-type Claude drafting prompts (`_DRAFT_PROMPTS`).
- The orchestrator `execute_approve_pending_post` that publishes everywhere.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import structlog
from anthropic import AsyncAnthropic

from .db import Database
from .channel_bot import ChannelBot
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

log = structlog.get_logger()


# ---- Data fetching (REPLACE WITH YOUR REAL SOURCE) ----------------------

def _fetch_data_for_daily_recap(target_date: str) -> dict[str, Any]:
    """REPLACE: pull whatever real data your daily recap cites.

    Return a dict that gets passed to Claude in the drafting prompt. Claude
    will use these values to construct a brand-voiced narrative.

    The keys here are entirely up to you — they're just used in the prompt
    below. Example values shown for illustration.
    """
    return {
        "date": target_date,
        "headline_metric": "+1.23%",
        "secondary_metric": "5 events",
        "context": "Standard day, nothing unusual.",
    }


def _fetch_data_for_weekly_review(target_week_end: str) -> dict[str, Any]:
    """REPLACE: weekly aggregates from your data source."""
    return {
        "week_label": "WEEK ENDING " + target_week_end,
        "headline_metric": "+2.22%",
        "secondary_metric": "10 events (5 up / 5 down)",
        "context": "Steady week, everything ran to plan.",
    }


def _fetch_data_for_monthly_report(month_label: str) -> dict[str, Any]:
    """REPLACE: month-level report data."""
    return {
        "month_label": month_label,
        "headline_metric": "+11.11%",
        "secondary_metric": "20 events (10 up / 10 down)",
        "context": "Solid month overall; one incident resolved per runbook.",
    }


# ---- Drafting prompts ----------------------------------------------------

_DRAFT_PROMPTS: dict[str, str] = {
    "daily_recap": """You are drafting a DAILY RECAP post for the public marketing
channel. Stay strictly on-brand using the voice anchor below.

Voice anchor:
\"\"\"{voice}\"\"\"

Data:
{data}

Output structure (no markdown, plain text, ~3-6 short paragraphs):
1. Headline line with the date (Day-of-week, YYYY-MM-DD)
2. Stats block: 2-4 lines of "Label: value"
3. One paragraph of context / observation (1-2 sentences)
4. Optional one-line philosophical / brand-aligned closer

Total length: under 500 chars. Match the voice exactly.""",

    "weekly_review": """You are drafting a WEEKLY REVIEW post. Stay strictly on-brand.

Voice anchor:
\"\"\"{voice}\"\"\"

Data:
{data}

Output structure (plain text, ~5-8 paragraphs):
1. Headline with the week range
2. Stats block (4-6 lines of "Label: value")
3. Two short paragraphs: what worked, what didn't
4. One forward-looking line

Total length: under 1500 chars.""",

    "monthly_report": """You are drafting a MONTHLY REPORT post.
This is the longest format — owe the audience a candid look at the month.

Voice anchor:
\"\"\"{voice}\"\"\"

Data:
{data}

Output structure (plain text):
1. Headline with the month
2. Stats block (6-8 lines)
3. Three paragraphs: wins, losses, lessons
4. One forward-looking paragraph

Total length: under 3000 chars. Be candid about failures.""",
}


def _post_type_data_fetcher(post_type: str, target_date: Optional[str] = None) -> dict[str, Any]:
    today = (target_date or datetime.now(tz=timezone.utc).date().isoformat())
    if post_type == "daily_recap":
        return _fetch_data_for_daily_recap(today)
    if post_type == "weekly_review":
        return _fetch_data_for_weekly_review(today)
    if post_type == "monthly_report":
        return _fetch_data_for_monthly_report(today)
    raise ValueError(f"unknown post_type: {post_type}")


def _load_voice_anchor(path: str) -> str:
    p = Path(path)
    if not p.exists():
        return "// Voice anchor file missing — fill in strategy_notes.md"
    return p.read_text(encoding="utf-8").strip()


# ---- generate_draft executor ---------------------------------------------

async def execute_generate_draft(
    db: Database,
    user: User,
    inp: GenerateDraftInput,
    *,
    anthropic_client: AsyncAnthropic,
    apps_by_user_id: dict[int, Any],
    strategy_notes_path: str,
) -> dict[str, Any]:
    """Generate a draft via Claude, store as pending, and DM operator with
    ✅/✏️/❌ buttons."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    if inp.post_type not in _DRAFT_PROMPTS:
        return {"error": "unknown_post_type", "post_type": inp.post_type}

    voice = _load_voice_anchor(strategy_notes_path)
    data = _post_type_data_fetcher(inp.post_type, inp.target_date)
    prompt = _DRAFT_PROMPTS[inp.post_type].format(voice=voice, data=data)

    response = await anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    draft = response.content[0].text.strip()

    pending_id = await db.add_pending_post(
        user_id=user.user_id,
        post_type=inp.post_type,
        draft_content=draft,
    )

    # Send DM with approval buttons.
    app = apps_by_user_id.get(user.user_id)
    if app is not None:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Publish",
                                 callback_data=f"draft_approve_{pending_id}"),
            InlineKeyboardButton("✏️ Edit",
                                 callback_data=f"draft_edit_{pending_id}"),
            InlineKeyboardButton("❌ Skip",
                                 callback_data=f"draft_reject_{pending_id}"),
        ]])
        try:
            await app.bot.send_message(
                chat_id=user.telegram_user_id,
                text=(
                    f"📝 Draft #{pending_id} — {inp.post_type}\n\n"
                    f"{draft}\n\n---\nUse the buttons below, or reply with "
                    f"edit feedback as a message."
                ),
                reply_markup=keyboard,
            )
        except Exception:
            log.exception("draft_dm_send_failed", pending_id=pending_id)

    log.info(
        "generate_draft_call",
        user_key=user.user_key,
        post_type=inp.post_type,
        pending_id=pending_id,
        length=len(draft),
    )
    return {"ok": True, "pending_id": pending_id, "length": len(draft)}


# ---- approve / reject / update -------------------------------------------

async def execute_approve_pending_post(
    db: Database,
    user: User,
    inp: ApprovePendingPostInput,
    *,
    channel_bot: Optional[ChannelBot],
    apps_by_user_id: Optional[dict] = None,
    anthropic_client: Optional[Any] = None,
    ig_notify_user_key: str = "A",
    x_publisher: Optional[Any] = None,
) -> dict[str, Any]:
    pending = await db.get_pending_post(inp.pending_id)
    if pending is None:
        return {"error": "pending_not_found", "pending_id": inp.pending_id}
    if pending["status"] != "pending":
        return {"error": "already_decided",
                "detail": f"Post is in status '{pending['status']}'."}
    if channel_bot is None:
        return {"error": "channel_bot_not_configured"}

    content = pending["draft_content"]
    try:
        msg_id = await channel_bot.post(content)
    except Exception as e:
        log.exception("approve_pending_post_send_failed")
        return {"error": "telegram_send_failed", "detail": str(e)}

    published_id = await db.add_published_post(
        user_id=user.user_id,
        channel=channel_bot.channel_handle,
        content=content,
        post_type=pending["post_type"],
        telegram_message_id=msg_id,
    )
    await db.mark_pending_post_decided(
        inp.pending_id, decision="publish", published_post_id=published_id,
    )

    # Trigger IG manual workflow (DMs operator with ✅/❌).
    ig_workflow_id: Optional[int] = None
    if apps_by_user_id is not None and pending["post_type"] in (
        "daily_recap", "weekly_review", "monthly_report"
    ):
        try:
            from .ig_workflow import start_workflow_after_publish
            ig_workflow_id = await start_workflow_after_publish(
                db=db,
                apps_by_user_id=apps_by_user_id,
                notify_user_key=ig_notify_user_key,
                published_post_id=published_id,
                post_type=pending["post_type"],
                draft_text=content,
            )
        except Exception:
            log.exception("ig_workflow_kickoff_failed",
                          published_post_id=published_id)

    # Auto-post to X (text-only, runs alongside Telegram).
    x_url: Optional[str] = None
    if x_publisher is not None and x_publisher.is_configured():
        try:
            import os
            import asyncio
            hashtags = os.environ.get("IG_HASHTAGS", "")
            # Keep only first 2 paragraphs for X (headline + stats); drop
            # commentary so the post fits in a single tweet.
            paragraphs = content.split("\n\n")
            x_body = (
                "\n\n".join(paragraphs[:2]).strip()
                if len(paragraphs) >= 2 else content.strip()
            )
            x_url = await asyncio.to_thread(x_publisher.post, x_body, hashtags)
        except Exception:
            log.exception("approve_x_post_failed",
                          published_post_id=published_id)

    log.info(
        "approve_pending_post_call",
        user_key=user.user_key,
        pending_id=inp.pending_id,
        published_post_id=published_id,
        x_url=x_url,
        ig_workflow_id=ig_workflow_id,
    )
    return {
        "ok": True,
        "pending_id": inp.pending_id,
        "published_post_id": published_id,
        "channel": channel_bot.channel_handle,
        "telegram_message_id": msg_id,
        "ig_workflow_id": ig_workflow_id,
        "x_url": x_url,
    }


async def execute_reject_pending_post(
    db: Database, user: User, inp: RejectPendingPostInput
) -> dict[str, Any]:
    pending = await db.get_pending_post(inp.pending_id)
    if pending is None:
        return {"error": "pending_not_found"}
    if pending["status"] != "pending":
        return {"error": "already_decided"}
    await db.mark_pending_post_decided(inp.pending_id, decision="reject")
    return {"ok": True, "pending_id": inp.pending_id}


async def execute_update_pending_post(
    db: Database, user: User, inp: UpdatePendingPostInput
) -> dict[str, Any]:
    pending = await db.get_pending_post(inp.pending_id)
    if pending is None:
        return {"error": "pending_not_found"}
    if pending["status"] != "pending":
        return {"error": "already_decided"}
    await db.update_pending_post_content(inp.pending_id, inp.new_content)
    return {"ok": True, "pending_id": inp.pending_id, "length": len(inp.new_content)}


# ---- list helpers --------------------------------------------------------

async def execute_list_pending_posts(
    db: Database, user: User, inp: ListPendingPostsInput
) -> dict[str, Any]:
    rows = await db.list_pending_posts(status=inp.status, limit=inp.limit)
    return {"ok": True, "items": [
        {"pending_id": r["pending_id"], "post_type": r["post_type"],
         "status": r["status"], "created_at": r["created_at"]}
        for r in rows
    ]}


async def execute_list_published_posts(
    db: Database, user: User, inp: ListPublishedPostsInput
) -> dict[str, Any]:
    rows = await db.list_published_posts(limit=inp.limit)
    return {"ok": True, "items": [
        {"post_id": r["post_id"], "post_type": r["post_type"],
         "channel": r["channel"], "published_at": r["published_at"]}
        for r in rows
    ]}


# ---- direct publish + X manual -------------------------------------------

async def execute_publish_to_channel(
    db: Database,
    user: User,
    inp: PublishToChannelInput,
    *,
    channel_bot: Optional[ChannelBot],
) -> dict[str, Any]:
    """Publish content directly to the Telegram channel (bypassing the
    draft/approval flow). Use sparingly — most posts should go through the
    draft → approve loop for safety."""
    if channel_bot is None:
        return {"error": "channel_bot_not_configured"}
    content = inp.content.strip()
    if not content:
        return {"error": "empty_content"}
    try:
        msg_id = await channel_bot.post(content)
    except Exception as e:
        log.exception("publish_to_channel_send_failed")
        return {"error": "telegram_send_failed", "detail": str(e)}
    post_id = await db.add_published_post(
        user_id=user.user_id, channel=channel_bot.channel_handle,
        content=content, post_type=inp.post_type, telegram_message_id=msg_id,
    )
    return {"ok": True, "post_id": post_id, "telegram_message_id": msg_id}


async def execute_post_to_x(
    user: User,
    text: str,
    add_hashtags: bool = True,
    *,
    x_publisher: Optional[Any] = None,
) -> dict[str, Any]:
    """Post raw text to X (manual escape hatch). User-supplied text IS the
    approval — no further confirmation."""
    if x_publisher is None or not x_publisher.is_configured():
        return {"error": "x_not_configured"}
    text = text.strip()
    if not text:
        return {"error": "empty_text"}
    hashtags = ""
    if add_hashtags:
        import os
        hashtags = os.environ.get("IG_HASHTAGS", "")
    import asyncio
    url = await asyncio.to_thread(x_publisher.post, text, hashtags)
    if not url:
        return {"error": "x_post_failed",
                "detail": "Common cause: out of pay-per-use credits."}
    return {"ok": True, "url": url}


# ---- voice anchor reader -------------------------------------------------

async def execute_get_strategy_voice_anchor(
    user: User, inp: GetStrategyVoiceAnchorInput, *, strategy_notes_path: str
) -> dict[str, Any]:
    """Returns the contents of strategy_notes.md so Claude can cite voice
    rules / brand facts directly."""
    voice = _load_voice_anchor(strategy_notes_path)
    return {"ok": True, "voice_anchor": voice, "length": len(voice)}
