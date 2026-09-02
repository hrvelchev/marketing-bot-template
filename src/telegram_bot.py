"""Telegram operator-bot handlers.

Three handler categories:
1. /start — greet whitelisted users, reject everyone else.
2. Text messages — route through Claude (router.route_message).
3. Inline button callbacks — draft_*, igprompt_*, igpost_*.
4. Photo uploads — bridge to ig_workflow when a workflow is in
   awaiting_image state.
"""
from __future__ import annotations

from typing import Any

import structlog
from anthropic import AsyncAnthropic
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .db import Database
from .ig_workflow import (
    handle_post_decision as ig_handle_post_decision,
    handle_prompt_decision as ig_handle_prompt_decision,
    handle_uploaded_image as ig_handle_uploaded_image,
)
from .channel_bot import ChannelBot
from .marketing import execute_approve_pending_post, execute_reject_pending_post
from .models import ApprovePendingPostInput, RejectPendingPostInput
from .router import route_message
from .social_publisher import InstagramPublisher, XPublisher

log = structlog.get_logger()


def build_application(
    token: str,
    db: Database,
    client: AsyncAnthropic,
    *,
    channel_bot: ChannelBot | None = None,
    apps_by_user_id: dict | None = None,
    notify_user_key: str = "A",
    ig_publisher: InstagramPublisher | None = None,
    ig_templates_dir: str = "./assets/ig_templates",
    ig_uploads_dir: str = "./data/ig_uploads",
    x_publisher: XPublisher | None = None,
    strategy_notes_path: str = "./strategy_notes.md",
) -> Application:
    app = Application.builder().token(token).build()

    async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or update.message is None:
            return
        user = await db.get_user_by_telegram_id(update.effective_user.id)
        if user is None:
            await update.message.reply_text("This bot is private.")
            return
        await update.message.reply_text(
            f"Hello {user.display_name}! I'm your marketing assistant. "
            f"Try: 'draft a daily recap' or 'show recent posts'."
        )

    async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if (update.effective_user is None or update.message is None
                or update.message.text is None):
            return
        user = await db.get_user_by_telegram_id(update.effective_user.id)
        if user is None:
            await update.message.reply_text("This bot is private.")
            return
        # Rate limit
        if await db.usage_today(user.user_id) >= user.daily_call_limit:
            await update.message.reply_text(
                "Daily limit reached. Resets at midnight UTC."
            )
            return
        try:
            await update.message.chat.send_action("typing")
            reply = await route_message(
                db, client, user, update.message.text,
                channel_bot=channel_bot,
                apps_by_user_id=apps_by_user_id,
                x_publisher=x_publisher,
                strategy_notes_path=strategy_notes_path,
            )
        except Exception:
            log.exception("router_error", user_id=user.user_id)
            reply = "Sorry, something went wrong. Please try again."
        await update.message.reply_text(reply)

    async def handle_draft_callback(
        update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Inline button on draft DM: ✅ Publish / ✏️ Edit / ❌ Skip."""
        query = update.callback_query
        if query is None or query.data is None or query.from_user is None:
            return
        await query.answer()
        parts = query.data.split("_")
        if len(parts) != 3 or parts[0] != "draft":
            return
        action = parts[1]
        try:
            pending_id = int(parts[2])
        except ValueError:
            return

        user = await db.get_user_by_telegram_id(query.from_user.id)
        if user is None:
            await query.answer("Unauthorized.", show_alert=True)
            return

        original_text = (query.message.text if query.message else "") or ""

        if action == "approve":
            result = await execute_approve_pending_post(
                db, user, ApprovePendingPostInput(pending_id=pending_id),
                channel_bot=channel_bot,
                apps_by_user_id=apps_by_user_id,
                anthropic_client=client,
                ig_notify_user_key=notify_user_key,
                x_publisher=x_publisher,
            )
            if result.get("ok"):
                tail = f"\n\n✅ Published by {user.display_name}."
            else:
                tail = f"\n\n❌ Approve failed: {result.get('error')}"
            try:
                await query.edit_message_text(text=original_text + tail, reply_markup=None)
            except Exception:
                log.exception("draft_callback_edit_failed")
            return

        if action == "reject":
            await execute_reject_pending_post(
                db, user, RejectPendingPostInput(pending_id=pending_id),
            )
            tail = f"\n\n❌ Skipped by {user.display_name}."
            try:
                await query.edit_message_text(text=original_text + tail, reply_markup=None)
            except Exception:
                log.exception("draft_callback_edit_failed")
            return

        if action == "edit":
            await query.answer(
                "Reply with your edit feedback as a message — I'll revise the draft.",
                show_alert=True,
            )
            return

    async def handle_ig_prompt_callback(
        update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        if query is None or query.data is None or query.from_user is None:
            return
        await query.answer()
        parts = query.data.split("_")
        if len(parts) != 3 or parts[0] != "igprompt":
            return
        decision = parts[1]
        try:
            workflow_id = int(parts[2])
        except ValueError:
            return

        user = await db.get_user_by_telegram_id(query.from_user.id)
        if user is None:
            await query.answer("Unauthorized.", show_alert=True)
            return

        result = await ig_handle_prompt_decision(
            db=db, anthropic_client=client,
            apps_by_user_id=apps_by_user_id or {},
            workflow_id=workflow_id, decision=decision,
            templates_dir=ig_templates_dir,
        )
        original = (query.message.text if query.message else "") or ""
        tail = (
            "\n\n✅ Prompt + template sent — send me the generated PNG when ready."
            if decision == "yes" else "\n\n❌ IG skipped."
        )
        if not result.get("ok"):
            tail += f"\n(workflow note: {result.get('error', 'unknown')})"
        try:
            await query.edit_message_text(text=original + tail, reply_markup=None)
        except Exception:
            log.exception("ig_prompt_callback_edit_failed")

    async def handle_ig_post_callback(
        update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        if query is None or query.data is None or query.from_user is None:
            return
        await query.answer()
        parts = query.data.split("_")
        if len(parts) != 3 or parts[0] != "igpost":
            return
        decision = parts[1]
        try:
            workflow_id = int(parts[2])
        except ValueError:
            return

        user = await db.get_user_by_telegram_id(query.from_user.id)
        if user is None:
            await query.answer("Unauthorized.", show_alert=True)
            return
        if ig_publisher is None:
            await query.answer("IG publisher not configured.", show_alert=True)
            return

        result = await ig_handle_post_decision(
            db=db, apps_by_user_id=apps_by_user_id or {},
            ig_publisher=ig_publisher,
            workflow_id=workflow_id, decision=decision,
        )
        original = (query.message.text if query.message else "") or ""
        if decision == "yes" and result.get("ok"):
            tail = "\n\n✅ Posted to Instagram."
        elif decision == "no":
            tail = "\n\n❌ Cancelled."
        else:
            tail = f"\n\n❌ Post failed: {result.get('error', 'unknown')}"
        try:
            await query.edit_message_text(text=original + tail, reply_markup=None)
        except Exception:
            log.exception("ig_post_callback_edit_failed")

    async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """When a whitelisted user sends a photo to the bot AND has an active
        IG workflow in awaiting_image state, treat it as the ChatGPT-generated
        card and advance the workflow."""
        if update.effective_user is None or update.message is None:
            return
        if update.message.chat.type != "private":
            return
        user = await db.get_user_by_telegram_id(update.effective_user.id)
        if user is None:
            return

        photo = update.message.photo[-1] if update.message.photo else None
        if photo is None:
            return

        wf = await db.find_ig_workflow_awaiting_image(update.effective_user.id)
        if wf is None:
            return  # not in any workflow; silently ignore

        try:
            file = await ctx.bot.get_file(photo.file_id)
            image_bytes = bytes(await file.download_as_bytearray())
        except Exception:
            log.exception("ig_workflow_photo_download_failed")
            await update.message.reply_text(
                "Couldn't download that image — try sending it again."
            )
            return

        result = await ig_handle_uploaded_image(
            db=db, apps_by_user_id=apps_by_user_id or {},
            telegram_user_id=update.effective_user.id,
            image_bytes=image_bytes, uploads_dir=ig_uploads_dir,
        )
        if not result.get("ok"):
            log.warning("ig_workflow_photo_handler_no_op", error=result.get("error"))

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_draft_callback, pattern=r"^draft_"))
    app.add_handler(CallbackQueryHandler(handle_ig_prompt_callback, pattern=r"^igprompt_"))
    app.add_handler(CallbackQueryHandler(handle_ig_post_callback, pattern=r"^igpost_"))
    return app
