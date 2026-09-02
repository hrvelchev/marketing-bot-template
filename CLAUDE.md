# CLAUDE.md — context for Claude Code

This file is read by Claude Code on every session in this project. Keep it
focused; it's not human docs (see `README.md` for that).

---

## What this project is

A **reusable template** for an AI-drafted, human-approved, multi-platform
social-media posting bot. The user (the operator adapting this template) is
adapting it for their own use case. **This is not a finished product to deploy
as-is** — the operator fills in their own brand voice, data source, post types, and
platform handles.

## Architecture at a glance

```
Telegram operator bot (user DMs commands)
        │
        ▼
Claude tool-use loop (claude_client.py + router.py)
        │
        ▼
Marketing executors (marketing.py): generate_draft, approve, etc.
        │
   ┌────┼─────────────────┬───────────────┐
   ▼    ▼                 ▼               ▼
Telegram      X (tweepy)       Instagram (manual ChatGPT step
channel       text only         → operator generates image →
              with hashtag       bot uploads via Meta API)
              footer
```

Two Telegram bots:
- **Operator bot** — the operator DMs it commands. Defined by `TELEGRAM_BOT_TOKEN_OPERATOR`.
- **Channel bot** — channel publisher. Defined by `TELEGRAM_BOT_TOKEN_CHANNEL`.

The novel piece is the **Instagram manual workflow** (state machine in
`src/ig_workflow.py`). AI image-gen produces inconsistent text rendering for
branded data images, so the bot delegates the image step to the human via a
✅/❌ approval loop. Don't suggest "let's just use DALL-E for the IG cards"
unless the operator explicitly asks — that path was tried and rejected.

## First-time setup checklist

When the operator asks you to help set this up, walk them through:

1. **Virtualenv + deps:**
   ```bash
   cd marketing-bot-template
   python -m venv .venv
   .venv\Scripts\activate          # Windows
   pip install -r requirements.txt
   ```

2. **Copy templates to working files:**
   ```bash
   cp .env.example .env
   cp users.example.yaml users.yaml
   cp strategy_notes.example.md strategy_notes.md
   ```

3. **Fill `.env`** — see `.env.example` for the full list. Bare minimum to run:
   - `ANTHROPIC_API_KEY`
   - `TELEGRAM_BOT_TOKEN_OPERATOR`
   - `DATABASE_PATH=./data/bot.db`
   - (others can stay empty; platforms will silently skip)

4. **Fill `users.yaml`** with the operator's Telegram user_id (get from `@userinfobot`).

5. **Run:** `python -m src.main`. In Telegram, DM the operator bot:
   `draft a daily recap`. A draft with ✅/✏️/❌ buttons should arrive.

## Common adaptation tasks

When the operator asks you to customize, here's where to look:

| Task | File(s) |
|------|---------|
| Rename / add post types | `src/marketing.py` `_draft_generators` dict; `src/social_publisher.py` `OPENAI_PROMPTS` dict; `src/scheduler.py` cron jobs |
| Plug in real data source | `src/marketing.py` `_fetch_data_for_*` functions (currently return mock data) |
| Change Claude model | `src/claude_client.py` `make_client()` and any `model=` param in `client.messages.create()` |
| Adjust system prompt / voice | `src/prompts.py` (template) + `strategy_notes.md` (brand voice) |
| Add a new social platform | New publisher class in `src/social_publisher.py` mirroring `XPublisher`; wire into `execute_approve_pending_post` in `marketing.py` |
| Change hashtags | `IG_HASHTAGS` env var (parsed in `src/social_publisher.py`) |
| Disable a platform | Leave its env vars empty — `is_configured()` checks decide at runtime |
| Tweak cron schedule | `src/scheduler.py` |

## Coding conventions

- **Async everywhere.** All DB / Telegram / HTTP handlers are `async def`.
- **Type hints required.** `from __future__ import annotations` at the top of
  every module.
- **Pydantic for tool I/O.** Define `SomethingInput(BaseModel)` in `models.py`
  for each Claude tool's args.
- **Structlog for logs.** Use `log.info("event_name", key=value, ...)` style.
  Never plain `print()`.
- **Parameterized SQL only.** Never f-string into queries.
- **Keep modules under ~400 lines.** Split if longer.
- **Fail loud in dev, fail soft to the user.** Internal errors → structured
  log with traceback. User-facing reply → generic "Sorry, something went wrong."

## Security non-negotiables

- **Never commit `.env`.** It's in `.gitignore`. If you suggest a code change
  that requires a new env var, also update `.env.example`.
- **Never log API keys, tokens, or user-supplied secrets.**
- **Whitelist-gate every command.** Only Telegram user_ids listed in
  `users.yaml` may operate the bot. The `access_control.py` / `check_whitelist`
  pattern enforces this; don't bypass it.
- **All datetime stored in UTC.** Convert for display only.

## Things to avoid

- **Don't reintroduce automated AI image generation for IG cards** — that path
  was tried (DALL-E, gpt-image-2, Playwright HTML render, Pillow overlay) and
  the manual ChatGPT workflow won on quality. If the operator wants to revisit, fine,
  but it's an explicit choice with cost/quality tradeoffs.
- **Don't add features beyond what the operator asks for.** This is a template, not a
  full product. Resist scope creep — three platforms (Telegram, X, IG) is
  enough.
- **Keep the template brand-agnostic.** Domain-specific data code belongs to
  the operator adapting it — the `_fetch_data_for_*` mocks are the intended
  extension points.
- **Don't hardcode brand names, hashtags, or platform handles.** Everything
  brand-specific lives in `.env`, `users.yaml`, or `strategy_notes.md`.

## Where to start when the operator says "help me set this up"

1. Verify deps are installed (`pip list` for `anthropic`, `python-telegram-bot`,
   `tweepy`, etc.).
2. Check `.env` exists and has at least `ANTHROPIC_API_KEY` and
   `TELEGRAM_BOT_TOKEN_OPERATOR` filled.
3. Check `users.yaml` has the operator's Telegram user_id.
4. Check `data/` directory exists (will be created on first run otherwise).
5. Run `python -m src.main` and walk through the startup logs together.

## Where to start when the operator says "make it post about X"

1. Look at the existing `_draft_generators` in `src/marketing.py` for the
   pattern (a function that returns the Claude prompt for one post type).
2. Add a new entry keyed by the new post type string.
3. Add a matching entry to `OPENAI_PROMPTS` in `src/social_publisher.py` if they
   want the IG manual workflow to support it.
4. Optionally wire a cron job in `src/scheduler.py`.

Don't auto-deploy or auto-run anything. Always let the operator trigger the run.

---

## Quick file index

| File | Purpose |
|------|---------|
| `src/main.py` | Entry point. Wires everything, starts the bot + scheduler. |
| `src/config.py` | Pydantic settings (loads `.env`). |
| `src/db.py` | SQLite schema + CRUD helpers. |
| `src/models.py` | Pydantic input models for Claude tools. |
| `src/utils.py` | Small helpers (`now_utc_iso`, etc.). |
| `src/prompts.py` | System prompt renderer. |
| `src/claude_client.py` | Tool definitions + Anthropic SDK wrapper. |
| `src/marketing.py` | Tool executors: `generate_draft`, `approve`, `reject`, `post_to_x`, etc. |
| `src/ig_workflow.py` | Manual IG state machine. |
| `src/social_publisher.py` | `XPublisher`, `InstagramPublisher`, hashtag handling. |
| `src/channel_bot.py` | Telegram channel sender (thin wrapper around `telegram.Bot`). |
| `src/scheduler.py` | APScheduler cron jobs. |
| `src/telegram_bot.py` | Bot handlers, inline-button callbacks, photo handler. |
| `src/router.py` | Routes Claude's tool calls to executors. |
