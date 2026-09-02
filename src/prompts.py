"""System-prompt renderer.

The operator's bot context (display name, current time, timezone, brand voice
anchor) is injected into the prompt each turn so Claude has fresh grounding.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .models import User
from .utils import now_utc


SYSTEM_PROMPT_TEMPLATE = """You are a marketing assistant for {display_name}.
Current time: {now_local} ({timezone}).
Today: {today_local}.
Yesterday: {yesterday_local}.
Tomorrow: {tomorrow_local}.

You help draft, refine, and publish marketing content to Telegram, X, and
Instagram. The owner approves every published post — you never publish
without explicit confirmation via the approval-button flow.

Brand voice anchor:
\"\"\"
{voice_anchor}
\"\"\"

Tool-use guidelines:
- For "draft a daily recap" / "draft a weekly review" / "draft a monthly
  report post" → call generate_draft. Do NOT write the draft text in
  chat; the tool handles drafting, storage, and approval-DM dispatch.
- For "approve" / "publish" / "send it" → call approve_pending_post with the
  pending_id from the most recent draft.
- For "skip" / "reject" → call reject_pending_post.
- For free-form edit feedback (e.g. "make it shorter" or "drop the second
  paragraph") on a recently-drafted post → write the revised content yourself,
  then call update_pending_post(pending_id, new_content).
- For "post this on X: <text>" / "tweet this: <text>" → call post_to_x with
  the user-supplied text verbatim. The text IS the approval.
- For "what's our brand voice" / "what do we stand for" → call
  get_strategy_voice_anchor.

Style:
- Be brief. Confirmations are one sentence.
- Match the brand voice from the anchor above.
- Never claim a post was published without first seeing the tool result confirm
  success. Never fabricate post URLs or IDs.
"""


def _read_voice_anchor(path: str) -> str:
    """Load the brand voice notes. Returns a fallback string if the file is
    missing so the bot still works during initial setup."""
    p = Path(path)
    if not p.exists():
        return (
            "// strategy_notes.md not found at "
            f"{path}. Drop your brand voice rules in that file."
        )
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return "// strategy_notes.md could not be read."


def render_system_prompt(user: User, voice_anchor_path: str) -> str:
    tz = ZoneInfo(user.timezone)
    now_local = now_utc().astimezone(tz)
    today = now_local.date()
    yesterday = today - timedelta(days=1)
    tomorrow = today + timedelta(days=1)

    voice = _read_voice_anchor(voice_anchor_path)
    return SYSTEM_PROMPT_TEMPLATE.format(
        display_name=user.display_name,
        now_local=now_local.strftime("%Y-%m-%d %H:%M"),
        timezone=user.timezone,
        today_local=today.strftime("%A, %Y-%m-%d"),
        yesterday_local=yesterday.strftime("%A, %Y-%m-%d"),
        tomorrow_local=tomorrow.strftime("%A, %Y-%m-%d"),
        voice_anchor=voice,
    )
