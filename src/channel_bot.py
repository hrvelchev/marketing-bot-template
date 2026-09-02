"""Thin Telegram wrapper for the channel-publishing bot.

This is a SECOND bot account, separate from the operator bot. It only sends
messages to your public channel — it doesn't listen for incoming updates,
doesn't have command handlers, doesn't get DMs from anyone. Its sole purpose
is `await channel_bot.post(content) -> message_id`.

Why a second bot? Telegram's design: an "operator" bot you DM doesn't have a
clean way to also be the public-channel author. Easier (and standard) to have
one bot for inbound + one for outbound channel posts.
"""
from __future__ import annotations

from typing import Optional

import structlog
from telegram import Bot

log = structlog.get_logger()


class ChannelBot:
    """Sender-only Telegram bot for publishing to a public channel."""

    def __init__(self, token: str, channel_handle: str) -> None:
        if not token:
            raise ValueError("ChannelBot needs a bot token")
        if not channel_handle:
            raise ValueError("ChannelBot needs a channel handle (e.g. '@MyChannel')")
        self.token = token
        self.channel_handle = channel_handle
        self._bot: Optional[Bot] = None

    @property
    def bot(self) -> Bot:
        if self._bot is None:
            self._bot = Bot(token=self.token)
        return self._bot

    async def post(self, content: str) -> int:
        """Publish a message to the channel. Returns the Telegram message_id."""
        msg = await self.bot.send_message(
            chat_id=self.channel_handle,
            text=content,
            disable_web_page_preview=True,
        )
        log.info(
            "channel_post_sent",
            channel=self.channel_handle,
            message_id=msg.message_id,
            length=len(content),
        )
        return msg.message_id

    async def delete(self, message_id: int) -> bool:
        """Delete a previously-published message. Returns True on success."""
        try:
            await self.bot.delete_message(
                chat_id=self.channel_handle, message_id=message_id
            )
            return True
        except Exception:
            log.exception("channel_delete_failed", message_id=message_id)
            return False
