"""Manual Instagram posting state machine.

The novel piece of this template. AI image-gen consistently underperformed
free ChatGPT for branded data images, so the bot delegates the image-creation
step to the human via an approval loop:

  Stage 1 (existing): Telegram channel post publishes
  Stage 2 (start_workflow): bot DMs "Want IG prompt + template?" ✅/❌
                            state = awaiting_prompt_decision
  Stage 3 (✅): bot sends filled prompt + template PNG; state = awaiting_image
  Stage 4 (operator sends back the ChatGPT-generated PNG):
                bot DMs "Post to Instagram?" ✅/❌
                state = awaiting_post_decision
  Stage 5 (✅): bot calls InstagramPublisher with the operator's image
                state = posted
  Cancel (❌ at any stage): state = cancelled
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import structlog
from anthropic import AsyncAnthropic
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .db import Database
from .social_publisher import InstagramPublisher, format_ig_prompt

log = structlog.get_logger()


# Hashtags appended to every IG caption. Pulled from IG_HASHTAGS env var
# at module load via lazy lookup (avoids import-order issues).
def _get_hashtags() -> str:
    """Return the configured hashtag string from the environment, or empty."""
    import os
    return os.environ.get("IG_HASHTAGS", "")


def _caption_footer() -> str:
    """IG-style hashtag footer with the standard `.\n.\n.\n` spacer."""
    tags = _get_hashtags()
    if not tags:
        return ""
    return "\n\n.\n.\n.\n" + tags


# ---- Claude-based structured data extraction -----------------------------

DATA_EXTRACTION_PROMPT = """Extract data fields from this {post_type} draft as JSON.

Required fields per post_type:
- daily_recap: date, stat_1, stat_2, highlight, bullet_1, bullet_2
- weekly_review: date_range, stat_1, stat_2, stat_3, highlight_a, highlight_b,
  bullet_1, bullet_2, bullet_3
- monthly_report: month_year, stat_1, stat_2, stat_3, stat_4, stat_5,
  highlight, bullet_1, bullet_2, bullet_3, bullet_4

Rules:
- Use US decimal format (periods, not commas)
- For percentages, include the % sign
- For signed values (gains, losses, deltas), include the +/- sign
- Bullets are short, one sentence each; strip any bullet markers (•, -, *)
- If a field isn't in the draft, use "—" (em-dash) as the value
- Output ONLY a JSON object, nothing else

Draft text:
\"\"\"
{draft}
\"\"\""""


async def _extract_data(
    client: AsyncAnthropic,
    post_type: str,
    draft_text: str,
) -> dict[str, str]:
    """Use Claude to pull structured data out of the long-form draft."""
    prompt = DATA_EXTRACTION_PROMPT.format(post_type=post_type, draft=draft_text)
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    # Strip code fences if Claude wraps the JSON.
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return json.loads(raw)


# ---- Stage 2: kick off the workflow --------------------------------------

async def start_workflow_after_publish(
    *,
    db: Database,
    apps_by_user_id: dict[int, Any],
    notify_user_key: str,
    published_post_id: int,
    post_type: str,
    draft_text: str,
) -> Optional[int]:
    """Called from marketing.execute_approve_pending_post right after a
    successful Telegram channel post. DMs the operator with ✅/❌ buttons
    asking if they want the IG prompt + template. Returns workflow_id."""
    user = await db.get_user_by_key(notify_user_key)
    if user is None:
        log.warning("ig_workflow_no_notify_user", user_key=notify_user_key)
        return None
    app = apps_by_user_id.get(user.user_id)
    if app is None:
        log.warning("ig_workflow_no_app", user_key=notify_user_key)
        return None

    workflow_id = await db.add_ig_workflow(
        published_post_id=published_post_id,
        post_type=post_type,
        notified_user_key=notify_user_key,
        notified_telegram_id=user.telegram_user_id,
        caption=draft_text,
    )

    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                "✅ Yes, send the prompt",
                callback_data=f"igprompt_yes_{workflow_id}",
            ),
            InlineKeyboardButton(
                "❌ Skip IG",
                callback_data=f"igprompt_no_{workflow_id}",
            ),
        ]]
    )
    try:
        await app.bot.send_message(
            chat_id=user.telegram_user_id,
            text=(
                f"📸 Telegram post #{published_post_id} is live.\n\n"
                f"Want the ChatGPT prompt + template image so you can "
                f"generate the matching Instagram card?"
            ),
            reply_markup=keyboard,
        )
        log.info(
            "ig_workflow_started",
            workflow_id=workflow_id,
            published_post_id=published_post_id,
            post_type=post_type,
        )
    except Exception:
        log.exception("ig_workflow_dm_failed", workflow_id=workflow_id)
        await db.update_ig_workflow_state(
            workflow_id, "cancelled", error="initial DM failed"
        )
        return None
    return workflow_id


# ---- Stage 3: operator wants the prompt ----------------------------------

async def handle_prompt_decision(
    *,
    db: Database,
    anthropic_client: AsyncAnthropic,
    apps_by_user_id: dict[int, Any],
    workflow_id: int,
    decision: str,  # 'yes' or 'no'
    templates_dir: str,
) -> dict[str, Any]:
    """User tapped ✅ or ❌ on 'Want the prompt?'. If yes, send the formatted
    prompt + the template PNG and flip state to awaiting_image."""
    wf = await db.get_ig_workflow(workflow_id)
    if wf is None:
        return {"error": "workflow_not_found", "workflow_id": workflow_id}
    if wf["state"] != "awaiting_prompt_decision":
        return {"error": "already_decided", "state": wf["state"]}

    user = await db.get_user_by_key(wf["notified_user_key"])
    if user is None:
        return {"error": "user_not_found"}
    app = apps_by_user_id.get(user.user_id)
    if app is None:
        return {"error": "app_not_found"}

    if decision == "no":
        await db.update_ig_workflow_state(workflow_id, "cancelled")
        return {"ok": True, "decision": "no"}

    # Pull structured data from the published draft text.
    try:
        data = await _extract_data(
            anthropic_client, wf["post_type"], wf["caption"]
        )
    except Exception as e:
        log.exception("ig_workflow_extract_failed", workflow_id=workflow_id)
        await db.update_ig_workflow_state(
            workflow_id, "cancelled", error=f"data extract failed: {e}"
        )
        return {"error": "extract_failed", "detail": str(e)}

    try:
        formatted = format_ig_prompt(wf["post_type"], data)
    except KeyError as e:
        await db.update_ig_workflow_state(
            workflow_id, "cancelled",
            error=f"missing field for prompt template: {e}",
        )
        return {"error": "format_failed", "missing_field": str(e)}

    intro = (
        f"📋 Here's the IG prompt. Paste this into ChatGPT (free) along with "
        f"the template image (attached below). Then send me back the resulting "
        f"PNG and I'll DM you to confirm before posting to IG.\n\n"
        f"```\n{formatted}\n```"
    )

    try:
        await app.bot.send_message(
            chat_id=user.telegram_user_id,
            text=intro,
            parse_mode="Markdown",
        )
    except Exception:
        # Markdown can fail on edge characters; retry plain.
        log.warning("ig_workflow_markdown_send_failed_retrying_plain")
        await app.bot.send_message(
            chat_id=user.telegram_user_id,
            text=intro.replace("```\n", "").replace("\n```", ""),
        )

    # Attach the template PNG.
    template_path = Path(templates_dir) / f"{wf['post_type']}_template.png"
    if template_path.exists():
        try:
            with open(template_path, "rb") as f:
                await app.bot.send_photo(
                    chat_id=user.telegram_user_id,
                    photo=f,
                    caption=(
                        "↑ Use this template as the visual style reference "
                        "when generating in ChatGPT."
                    ),
                )
        except Exception:
            log.exception(
                "ig_workflow_send_template_failed", workflow_id=workflow_id
            )

    await db.update_ig_workflow_state(workflow_id, "awaiting_image")
    return {"ok": True, "decision": "yes"}


# ---- Stage 4: operator sent the image ------------------------------------

async def handle_uploaded_image(
    *,
    db: Database,
    apps_by_user_id: dict[int, Any],
    telegram_user_id: int,
    image_bytes: bytes,
    uploads_dir: str,
) -> dict[str, Any]:
    """Called from telegram_bot's photo handler when an image arrives in a DM.
    Looks up the most recent awaiting_image workflow for this user, saves the
    image, transitions to awaiting_post_decision, and DMs the confirm prompt."""
    wf = await db.find_ig_workflow_awaiting_image(telegram_user_id)
    if wf is None:
        # User sent a random photo; not in any workflow. Caller decides
        # whether to acknowledge or ignore.
        return {"error": "no_active_workflow"}

    user = await db.get_user_by_key(wf["notified_user_key"])
    if user is None:
        return {"error": "user_not_found"}
    app = apps_by_user_id.get(user.user_id)
    if app is None:
        return {"error": "app_not_found"}

    Path(uploads_dir).mkdir(parents=True, exist_ok=True)
    image_path = Path(uploads_dir) / f"workflow_{wf['workflow_id']}.png"
    image_path.write_bytes(image_bytes)

    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                "✅ Post to Instagram",
                callback_data=f"igpost_yes_{wf['workflow_id']}",
            ),
            InlineKeyboardButton(
                "❌ Cancel",
                callback_data=f"igpost_no_{wf['workflow_id']}",
            ),
        ]]
    )
    try:
        await app.bot.send_message(
            chat_id=user.telegram_user_id,
            text=(
                "🖼️ Got the image. Ready to post it to Instagram with the "
                "Telegram text as the caption?"
            ),
            reply_markup=keyboard,
        )
    except Exception:
        log.exception(
            "ig_workflow_confirm_dm_failed", workflow_id=wf["workflow_id"]
        )
        return {"error": "confirm_dm_failed"}

    await db.update_ig_workflow_state(
        wf["workflow_id"],
        "awaiting_post_decision",
        image_path=str(image_path),
    )
    return {"ok": True, "workflow_id": wf["workflow_id"]}


# ---- Stage 5: operator confirmed the post --------------------------------

async def handle_post_decision(
    *,
    db: Database,
    apps_by_user_id: dict[int, Any],
    ig_publisher: InstagramPublisher,
    workflow_id: int,
    decision: str,  # 'yes' or 'no'
) -> dict[str, Any]:
    wf = await db.get_ig_workflow(workflow_id)
    if wf is None:
        return {"error": "workflow_not_found"}
    if wf["state"] != "awaiting_post_decision":
        return {"error": "already_decided", "state": wf["state"]}

    user = await db.get_user_by_key(wf["notified_user_key"])
    app = apps_by_user_id.get(user.user_id) if user else None

    if decision == "no":
        await db.update_ig_workflow_state(workflow_id, "cancelled")
        return {"ok": True, "decision": "no"}

    if not ig_publisher.is_configured():
        await db.update_ig_workflow_state(
            workflow_id, "cancelled", error="IG publisher not configured",
        )
        return {"error": "ig_not_configured"}

    image_path = Path(wf["image_path"]) if wf["image_path"] else None
    if not image_path or not image_path.exists():
        await db.update_ig_workflow_state(
            workflow_id, "cancelled", error="image file missing",
        )
        return {"error": "image_missing"}

    image_bytes = image_path.read_bytes()
    # Caption = exact Telegram draft text + branded hashtag footer.
    caption = (wf["caption"] or "") + _caption_footer()

    import asyncio
    permalink = await asyncio.to_thread(ig_publisher.post, image_bytes, caption)
    if not permalink:
        await db.update_ig_workflow_state(
            workflow_id, "cancelled", error="IG publish returned None",
        )
        if app:
            try:
                await app.bot.send_message(
                    chat_id=user.telegram_user_id,
                    text=(
                        "❌ IG post failed — check the bot logs. "
                        f"The image is saved at {image_path} for manual upload."
                    ),
                )
            except Exception:
                log.exception("ig_workflow_failure_dm_failed")
        return {"error": "ig_publish_failed"}

    await db.update_ig_workflow_state(
        workflow_id, "posted", ig_permalink=permalink
    )
    if app:
        try:
            await app.bot.send_message(
                chat_id=user.telegram_user_id,
                text=f"✅ Posted to Instagram: {permalink}",
                disable_web_page_preview=False,
            )
        except Exception:
            log.exception("ig_workflow_success_dm_failed")
    return {"ok": True, "permalink": permalink}
