"""SQLite schema + helpers for the marketing bot.

Tables:
- users: whitelist + per-user settings (mirrors users.yaml)
- conversation_history: last ~10 turns per user (for Claude context)
- usage_log: per-Claude-call accounting (token + cost)
- pending_posts: draft posts awaiting ✅/✏️/❌ decision
- published_posts: history of approved posts
- ig_workflows: state machine rows for manual IG posting flow
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import aiosqlite

from .models import User
from .utils import now_utc_iso


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id           INTEGER PRIMARY KEY,
    user_key          TEXT UNIQUE NOT NULL,
    telegram_user_id  INTEGER UNIQUE NOT NULL,
    display_name      TEXT NOT NULL,
    default_language  TEXT NOT NULL DEFAULT 'English',
    timezone          TEXT NOT NULL DEFAULT 'UTC',
    daily_call_limit  INTEGER NOT NULL DEFAULT 100,
    created_at        TEXT NOT NULL,
    active            INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS conversation_history (
    message_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(user_id),
    role        TEXT NOT NULL,             -- 'user' | 'assistant'
    content     TEXT NOT NULL,
    timestamp   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_convo_user_time
    ON conversation_history(user_id, timestamp);

CREATE TABLE IF NOT EXISTS usage_log (
    log_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(user_id),
    timestamp     TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_posts (
    pending_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER REFERENCES users(user_id),
    post_type           TEXT NOT NULL,        -- e.g. 'daily_recap'
    draft_content       TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending',  -- pending | published | rejected
    created_at          TEXT NOT NULL,
    decided_at          TEXT,
    decided_by_user_id  INTEGER REFERENCES users(user_id),
    edit_note           TEXT,
    published_post_id   INTEGER REFERENCES published_posts(post_id)
);

CREATE TABLE IF NOT EXISTS published_posts (
    post_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id              INTEGER REFERENCES users(user_id),
    channel              TEXT NOT NULL,
    content              TEXT NOT NULL,
    post_type            TEXT NOT NULL DEFAULT 'custom',
    telegram_message_id  INTEGER,
    published_at         TEXT NOT NULL,
    deleted              INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ig_workflows (
    workflow_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    published_post_id    INTEGER REFERENCES published_posts(post_id),
    post_type            TEXT NOT NULL,
    notified_user_key    TEXT NOT NULL,
    notified_telegram_id INTEGER NOT NULL,
    -- States: awaiting_prompt_decision | awaiting_image | awaiting_post_decision
    --       | posted | cancelled
    state                TEXT NOT NULL,
    image_path           TEXT,
    ig_permalink         TEXT,
    error                TEXT,
    caption              TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ig_workflows_user_state
    ON ig_workflows(notified_telegram_id, state);
"""


def _row_to_user(row: aiosqlite.Row) -> User:
    return User(
        user_id=row["user_id"],
        user_key=row["user_key"],
        telegram_user_id=row["telegram_user_id"],
        display_name=row["display_name"],
        default_language=row["default_language"],
        timezone=row["timezone"],
        daily_call_limit=row["daily_call_limit"],
        active=bool(row["active"]),
    )


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    async def init_schema(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    # ---- users -------------------------------------------------------
    async def upsert_user(
        self,
        *,
        user_key: str,
        telegram_user_id: int,
        display_name: str,
        default_language: str,
        timezone: str,
        daily_call_limit: int,
    ) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                """
                INSERT INTO users
                    (user_key, telegram_user_id, display_name,
                     default_language, timezone, daily_call_limit, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_key) DO UPDATE SET
                    telegram_user_id=excluded.telegram_user_id,
                    display_name=excluded.display_name,
                    default_language=excluded.default_language,
                    timezone=excluded.timezone,
                    daily_call_limit=excluded.daily_call_limit
                """,
                (user_key, telegram_user_id, display_name,
                 default_language, timezone, daily_call_limit, now_utc_iso()),
            )
            await db.commit()
            return int(cur.lastrowid or 0)

    async def get_user_by_telegram_id(self, telegram_user_id: int) -> Optional[User]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM users WHERE telegram_user_id = ? AND active = 1",
                (telegram_user_id,),
            )
            row = await cur.fetchone()
            return _row_to_user(row) if row else None

    async def get_user_by_key(self, user_key: str) -> Optional[User]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM users WHERE user_key = ?", (user_key,)
            )
            row = await cur.fetchone()
            return _row_to_user(row) if row else None

    # ---- conversation history ----------------------------------------
    async def add_message(self, user_id: int, role: str, content: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO conversation_history (user_id, role, content, timestamp) "
                "VALUES (?, ?, ?, ?)",
                (user_id, role, content, now_utc_iso()),
            )
            await db.commit()

    async def recent_messages(
        self, user_id: int, limit: int = 10
    ) -> list[dict[str, str]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT role, content FROM conversation_history "
                "WHERE user_id = ? ORDER BY message_id DESC LIMIT ?",
                (user_id, limit),
            )
            rows = await cur.fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    # ---- usage log ---------------------------------------------------
    async def log_usage(
        self, user_id: int, input_tokens: int, output_tokens: int, cost_usd: float
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO usage_log (user_id, timestamp, input_tokens, "
                "output_tokens, cost_usd) VALUES (?, ?, ?, ?, ?)",
                (user_id, now_utc_iso(), input_tokens, output_tokens, cost_usd),
            )
            await db.commit()

    async def usage_today(self, user_id: int) -> int:
        """Number of Claude calls in the last 24h. Used for daily rate-limiting."""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM usage_log WHERE user_id = ? "
                "AND timestamp > datetime('now', '-1 day')",
                (user_id,),
            )
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    # ---- pending_posts -----------------------------------------------
    async def add_pending_post(
        self, *, user_id: int, post_type: str, draft_content: str
    ) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "INSERT INTO pending_posts (user_id, post_type, draft_content, created_at) "
                "VALUES (?, ?, ?, ?)",
                (user_id, post_type, draft_content, now_utc_iso()),
            )
            await db.commit()
            return int(cur.lastrowid or 0)

    async def get_pending_post(self, pending_id: int) -> Optional[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM pending_posts WHERE pending_id = ?", (pending_id,)
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def list_pending_posts(
        self, status: Optional[str] = "pending", limit: int = 10
    ) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            sql = "SELECT * FROM pending_posts"
            params: tuple = ()
            if status:
                sql += " WHERE status = ?"
                params = (status,)
            sql += " ORDER BY pending_id DESC LIMIT ?"
            cur = await db.execute(sql, params + (limit,))
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def mark_pending_post_decided(
        self,
        pending_id: int,
        *,
        decision: str,                       # 'publish' | 'reject'
        published_post_id: Optional[int] = None,
    ) -> None:
        status = "published" if decision == "publish" else "rejected"
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE pending_posts SET status = ?, decided_at = ?, "
                "published_post_id = ? WHERE pending_id = ? AND status = 'pending'",
                (status, now_utc_iso(), published_post_id, pending_id),
            )
            await db.commit()

    async def update_pending_post_content(self, pending_id: int, new_content: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE pending_posts SET draft_content = ?, edit_note = COALESCE(edit_note, '') "
                "WHERE pending_id = ? AND status = 'pending'",
                (new_content, pending_id),
            )
            await db.commit()

    # ---- published_posts ---------------------------------------------
    async def add_published_post(
        self,
        *,
        user_id: int,
        channel: str,
        content: str,
        post_type: str = "custom",
        telegram_message_id: Optional[int] = None,
    ) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "INSERT INTO published_posts (user_id, channel, content, "
                "post_type, telegram_message_id, published_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, channel, content, post_type,
                 telegram_message_id, now_utc_iso()),
            )
            await db.commit()
            return int(cur.lastrowid or 0)

    async def list_published_posts(self, limit: int = 10) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM published_posts WHERE deleted = 0 "
                "ORDER BY post_id DESC LIMIT ?", (limit,),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    # ---- ig_workflows ------------------------------------------------
    async def add_ig_workflow(
        self,
        *,
        published_post_id: int,
        post_type: str,
        notified_user_key: str,
        notified_telegram_id: int,
        caption: str,
    ) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                """
                INSERT INTO ig_workflows
                    (published_post_id, post_type, notified_user_key,
                     notified_telegram_id, state, caption, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'awaiting_prompt_decision', ?, ?, ?)
                """,
                (published_post_id, post_type, notified_user_key,
                 notified_telegram_id, caption, now_utc_iso(), now_utc_iso()),
            )
            await db.commit()
            return int(cur.lastrowid or 0)

    async def get_ig_workflow(self, workflow_id: int) -> Optional[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM ig_workflows WHERE workflow_id = ?", (workflow_id,),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def find_ig_workflow_awaiting_image(
        self, telegram_user_id: int
    ) -> Optional[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT * FROM ig_workflows
                WHERE notified_telegram_id = ? AND state = 'awaiting_image'
                ORDER BY workflow_id DESC LIMIT 1
                """,
                (telegram_user_id,),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def update_ig_workflow_state(
        self,
        workflow_id: int,
        state: str,
        *,
        image_path: Optional[str] = None,
        ig_permalink: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                UPDATE ig_workflows
                SET state = ?,
                    image_path = COALESCE(?, image_path),
                    ig_permalink = COALESCE(?, ig_permalink),
                    error = COALESCE(?, error),
                    updated_at = ?
                WHERE workflow_id = ?
                """,
                (state, image_path, ig_permalink, error, now_utc_iso(), workflow_id),
            )
            await db.commit()
