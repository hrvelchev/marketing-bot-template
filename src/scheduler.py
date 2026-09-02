"""APScheduler cron jobs for auto-drafting posts.

Examples:
- Daily recap draft every weekday at 23:00 local time
- Weekly review draft every Sunday at 12:00
- Monthly report draft on the 1st at 11:00

Each job calls generate_draft, which DMs the operator with ✅/✏️/❌ buttons.
The operator's choice (or inaction) decides whether anything gets published.

Disable individual jobs by commenting out the scheduler.add_job() lines.
"""
from __future__ import annotations

from typing import Any

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from anthropic import AsyncAnthropic

from .db import Database
from .marketing import execute_generate_draft
from .models import GenerateDraftInput

log = structlog.get_logger()

LOCAL_TIMEZONE = "UTC"  # change to e.g. "Europe/London" if you want local time


async def _draft_job(
    db: Database,
    anthropic_client: AsyncAnthropic,
    apps_by_user_id: dict[int, Any],
    notify_user_key: str,
    post_type: str,
    strategy_notes_path: str,
) -> None:
    """Single scheduled-job body. Resolves the notification user and runs
    generate_draft as if they had typed it."""
    user = await db.get_user_by_key(notify_user_key)
    if user is None:
        log.warning("scheduler_no_user", user_key=notify_user_key)
        return
    try:
        await execute_generate_draft(
            db, user, GenerateDraftInput(post_type=post_type),
            anthropic_client=anthropic_client,
            apps_by_user_id=apps_by_user_id,
            strategy_notes_path=strategy_notes_path,
        )
    except Exception:
        log.exception("scheduled_draft_failed", post_type=post_type)


def start_scheduler(
    db: Database,
    anthropic_client: AsyncAnthropic,
    apps_by_user_id: dict[int, Any],
    *,
    notify_user_key: str = "A",
    strategy_notes_path: str = "./strategy_notes.md",
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=LOCAL_TIMEZONE)

    # Daily recap — every weekday at 23:00
    scheduler.add_job(
        _draft_job,
        CronTrigger(hour=9, minute=0,
                    timezone=LOCAL_TIMEZONE),
        args=[db, anthropic_client, apps_by_user_id, notify_user_key,
              "daily_recap", strategy_notes_path],
        id="daily_recap_draft",
        replace_existing=True,
    )

    # Weekly review — Sundays at 12:00
    scheduler.add_job(
        _draft_job,
        CronTrigger(day_of_week="sat", hour=10, minute=0,
                    timezone=LOCAL_TIMEZONE),
        args=[db, anthropic_client, apps_by_user_id, notify_user_key,
              "weekly_review", strategy_notes_path],
        id="weekly_review_draft",
        replace_existing=True,
    )

    # Monthly report — 1st of month at 11:00
    scheduler.add_job(
        _draft_job,
        CronTrigger(day=1, hour=8, minute=0, timezone=LOCAL_TIMEZONE),
        args=[db, anthropic_client, apps_by_user_id, notify_user_key,
              "monthly_report", strategy_notes_path],
        id="monthly_report_draft",
        replace_existing=True,
    )

    scheduler.start()
    log.info("scheduler_started", jobs=[j.id for j in scheduler.get_jobs()])
    return scheduler
