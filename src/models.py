"""Pydantic models for Claude tool inputs + the User record."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class User(BaseModel):
    """Loaded from the DB at handler entry. Represents the operator."""
    user_id: int
    user_key: str
    telegram_user_id: int
    display_name: str
    default_language: str
    timezone: str
    daily_call_limit: int
    active: bool = True


# ---- Claude tool inputs --------------------------------------------------

class GenerateDraftInput(BaseModel):
    post_type: str  # 'daily_recap' | 'weekly_review' | 'monthly_report'
    target_date: Optional[str] = None  # YYYY-MM-DD; defaults to today


class ApprovePendingPostInput(BaseModel):
    pending_id: int


class RejectPendingPostInput(BaseModel):
    pending_id: int


class UpdatePendingPostInput(BaseModel):
    pending_id: int
    new_content: str


class ListPendingPostsInput(BaseModel):
    status: Optional[str] = "pending"
    limit: int = 10


class ListPublishedPostsInput(BaseModel):
    limit: int = 10


class PublishToChannelInput(BaseModel):
    content: str
    post_type: str = "custom"


class PostToXInput(BaseModel):
    text: str
    add_hashtags: bool = True


class GetStrategyVoiceAnchorInput(BaseModel):
    pass  # no args; reads strategy_notes.md
