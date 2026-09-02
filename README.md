# Marketing Bot Template

A reusable scaffold for an AI-drafted, human-approved, multi-platform social-media
posting bot. Drafts content via Claude → owner approves over Telegram → fans out
to **Telegram channel + X + Instagram** in one tap.

Built for the case where you want automation + the option to ship branded image
content to Instagram, but don't want to fight AI image generation for daily
templated cards.

---

## Architecture

```
                    ┌────────────────────────────────────────┐
                    │  Owner (you) — talks to the bot in DM  │
                    └─────────────┬──────────────────────────┘
                                  │
                ┌─────────────────┴─────────────────────┐
                │  Telegram Bot (python-telegram-bot)   │
                │  • Receives commands ("draft a post") │
                │  • Sends approval DMs with ✅/✏️/❌  │
                │  • Routes user messages to Claude     │
                └─────────────────┬─────────────────────┘
                                  │
                ┌─────────────────┴────────────────┐
                │       Claude tool-use loop       │
                │  generate_draft, approve, etc.   │
                └─────────────────┬────────────────┘
                                  │
                  ┌───────────────┼──────────────────┐
                  │               │                  │
        ┌─────────▼──────┐ ┌──────▼──────┐  ┌────────▼──────┐
        │  Telegram      │ │      X      │  │   Instagram   │
        │  channel       │ │   (tweepy)  │  │ (Meta Graph,  │
        │ (channel_bot)  │ │             │  │  manual flow) │
        └────────────────┘ └─────────────┘  └───────────────┘
```

**Two Telegram bots in this template:**
1. **Operator bot** — the one you DM with commands.
2. **Channel bot** — sender-only, publishes to your public channel. Doesn't
   listen for messages; just sends.

---

## The novel piece — manual Instagram workflow

AI image-gen produces inconsistent text rendering for branded data-heavy
images. We tried Pillow text-overlay, Playwright HTML screenshot, OpenAI
`images.edit`, OpenAI Responses API + gpt-image-2 — all underperformed the free
ChatGPT web UI. So the final IG path is a **state machine that delegates the
generation step to the human**:

```
Stage 1: Telegram channel post publishes (existing)
Stage 2: Bot DMs "Want the IG prompt + template?" → ✅ / ❌
Stage 3 (✅): Bot sends the filled prompt + reference template PNG
              State → awaiting_image
Stage 4: User generates in free ChatGPT, sends the result PNG back to the bot
Stage 5: Bot DMs "Post to Instagram?" → ✅ / ❌
Stage 6 (✅): Bot uploads via Meta Graph API
              State → posted
Cancel (❌ at any stage): State → cancelled
```

This sounds clunky but it's 60–90s of the operator's time per post and produces
pixel-perfect output for branded content. The state machine lives in
`src/ig_workflow.py`; the photo-handler in `src/telegram_bot.py` is what bridges
"user uploaded a photo" back to the workflow.

If you want pure automation (no human-in-the-loop for IG), swap in any of:
- Bannerbear / Placid API (paid, ~$19–49/mo, pixel-perfect)
- DALL-E 3 / gpt-image-2 (variable quality, ~$0.05–0.20/image)
- HTML/CSS + Playwright (free, full control, needs you to design the HTML)

---

## Setup

### 1. Install

```bash
cd marketing-bot-template
python -m venv .venv
.venv\Scripts\activate            # Windows
# OR: source .venv/bin/activate   # macOS/Linux
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
cp users.example.yaml users.yaml
```

Fill in `.env` with your tokens:
- **`ANTHROPIC_API_KEY`** — get from `console.anthropic.com`
- **`TELEGRAM_BOT_TOKEN_OPERATOR`** — DM `@BotFather` on Telegram, `/newbot`
- **`TELEGRAM_BOT_TOKEN_CHANNEL`** — second bot for the channel publisher
- **`CHANNEL_HANDLE`** — e.g. `@YourChannel`
- **`X_*`** — `developer.x.com` → app keys (needs Read+Write permission;
  pay-per-use credits required as of 2025+)
- **`IG_ACCESS_TOKEN` + `IG_BUSINESS_ACCOUNT_ID`** —
  `developers.facebook.com` (Instagram Graph API; IG must be Business account
  linked to a Facebook Page)
- **`IG_PUBLIC_BASE_URL`** — public HTTPS URL where your FastAPI serves IG
  card images so Meta can fetch them (Tailscale Funnel works well; or DuckDNS +
  Caddy; or any reverse-proxied domain)
- **`IG_HASHTAGS`** — the 5 hashtags appended to every IG post + last X tweet

Fill in `users.yaml` with the Telegram user_id(s) of whoever should be allowed
to operate the bot. Get your Telegram numeric ID via `@userinfobot`.

### 3. Voice anchor

Copy `strategy_notes.example.md` → `strategy_notes.md` and replace with your
brand's voice rules. This file is read into every draft-generation prompt so
Claude stays on-brand.

### 4. Instagram templates

Drop three PNGs into `assets/ig_templates/`:
- `daily_recap_template.png`
- `weekly_review_template.png`
- `monthly_report_template.png`

These get attached to the DM you receive when you tap ✅ on a draft. You'll
paste them into ChatGPT along with the auto-generated prompt to produce the
final IG card. Naming convention matches the `post_type` strings in
`src/marketing.py` — rename / add types to fit your needs.

### 5. Run

```bash
python -m src.main
```

In your Telegram chat with the operator bot, type:
```
draft a daily recap
```

The bot drafts via Claude → DMs you with ✅/✏️/❌ → ✅ publishes everywhere.

---

## File structure

```
marketing-bot-template/
├── README.md                        # this file
├── .env.example                     # env template
├── requirements.txt
├── .gitignore
├── users.example.yaml               # rename to users.yaml + fill in
├── strategy_notes.example.md        # rename + put your brand voice
├── assets/
│   └── ig_templates/                # drop 3 PNGs here
└── src/
    ├── main.py                       # entry point
    ├── config.py                     # pydantic settings
    ├── db.py                         # SQLite schema + helpers
    ├── models.py                     # pydantic models for tool I/O
    ├── utils.py                      # small helpers (now_utc_iso, etc.)
    ├── prompts.py                    # system prompt renderer
    ├── claude_client.py              # tool defs + agentic loop client
    ├── marketing.py                  # draft / approve / publish executors
    ├── ig_workflow.py                # the manual IG state machine
    ├── social_publisher.py           # X + IG publishers
    ├── channel_bot.py                # channel sender wrapper
    ├── scheduler.py                  # cron jobs for auto-drafts
    ├── telegram_bot.py               # bot handlers + ✅/✏️/❌ + photo
    └── router.py                     # routes Claude tool calls
```

---

## Adapting for your use case

### Replace the post types

`src/marketing.py` ships with three example post types: `daily_recap`,
`weekly_review`, `monthly_report`. Rename them or add new ones — they're
just strings used as keys in:
- `OPENAI_PROMPTS` dict in `social_publisher.py` (the prompt sent to user
  alongside the IG template)
- `_draft_generators` dict in `marketing.py` (the Claude prompt for the draft)
- The scheduler crons in `scheduler.py`

### Plug in your own data source

`src/marketing.py` has placeholder data-fetching functions (e.g.
`_fetch_data_for_daily_recap`). They currently return mock data. Replace these
with whatever fetches your real numbers — pulling from a DB, scraping an API,
reading a CSV, etc.

### Change the hashtags

`IG_HASHTAGS` env var. Comma- or space-separated list of `#tags`. Used on every
IG post and the last X tweet.

### Disable platforms you don't want

Leave the corresponding env vars empty:
- `X_API_KEY=` (empty) → X posting silently skipped
- `IG_ACCESS_TOKEN=` (empty) → IG workflow silently skipped
- `TELEGRAM_BOT_TOKEN_CHANNEL=` (empty) → channel publishing disabled

Each module's `is_configured()` check decides at runtime.

### Add platforms

Add a new publisher class in `social_publisher.py` mirroring `XPublisher` or
`InstagramPublisher`. Wire it into `execute_approve_pending_post` in
`marketing.py` and pass it through `main.py`. The fan-out is just sequential
async calls — no clever orchestration needed.

---

## Known tradeoffs and gotchas

- **X is pay-per-use** as of 2025. Free posting via API is gone. ~$0.001–0.005
  per post; $5 credit top-up lasts hundreds of posts.
- **IG Graph API requires a public HTTPS URL** for image fetch. Local dev →
  Tailscale Funnel is the cleanest free option. Or any reverse-proxied domain.
- **X 280-char limit** is real. For long posts, the publisher threads
  automatically; or you can pre-truncate to a single tweet (see
  `XPublisher.post()` and the trim helper).
- **Claude API costs** for daily drafting: ~$0.001–0.002 per draft via Haiku
  4.5. Trivial.
- **No retry logic** on transient API failures. Bot logs the error and continues
  with the next platform. Add a retry queue if you want.
- **Single-tenant design.** One bot, one channel, one IG account. Multi-tenant
  rewrite is non-trivial.

---

## License

Do whatever you want with it. Attribution not required but appreciated.
