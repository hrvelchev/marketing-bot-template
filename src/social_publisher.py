"""Cross-platform publishing primitives.

Public surface:
- OPENAI_PROMPTS: prompt templates the bot fills in with real data and DMs
  to the operator so they can paste them into ChatGPT to generate the IG card
  image. Free ChatGPT consistently produced higher-fidelity results than the
  OpenAI API for branded data images — see README for the rationale.
- XPublisher: text posting to X via tweepy.
- InstagramPublisher: posts an operator-supplied image + caption to IG via
  the Meta Graph API. Used by ig_workflow after the operator sends back the
  generated image.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import structlog

log = structlog.get_logger()


# ---- IG ChatGPT-prompt templates (rendered + sent to operator) -----------
#
# Customize per post type. Each prompt is filled with structured data
# extracted from the Telegram draft and DMed to the operator. They paste
# this prompt + the reference template PNG into ChatGPT to produce the
# final IG card.

BRAND_STYLE_PREAMBLE = (
    "Visual style — replace with your brand's visual description. "
    "Background, accent color, logo placement, typography, layout. "
    "The more specific you are here, the closer the AI's output will "
    "match your reference template."
)

ACCURACY_RULES = (
    "CRITICAL TEXT-RENDERING RULES: render every number and word in the data "
    "EXACTLY as written below. Do not approximate, abbreviate, paraphrase, "
    "drop characters, swap digits, add asterisks, or insert extra punctuation. "
    "Every label and value must match the source text character-for-character. "
    "Use US/English decimal format (periods, not commas). Preserve em-dash "
    "spacing as written."
)


OPENAI_PROMPTS: dict[str, str] = {
    "daily_recap": (
        BRAND_STYLE_PREAMBLE + "\n\n" + ACCURACY_RULES + "\n\n"
        "Generate a 1024x1024 square Instagram post.\n\n"
        "Header title: DAILY RECAP\n"
        "Date: {date}\n\n"
        "Stats grid:\n"
        "  KEY_STAT_1: {stat_1}\n"
        "  KEY_STAT_2: {stat_2}\n"
        "  HIGHLIGHT (accent color): {highlight}\n\n"
        "Commentary — 2 bullet points near the bottom:\n"
        "  - {bullet_1}\n"
        "  - {bullet_2}\n"
    ),
    "weekly_review": (
        BRAND_STYLE_PREAMBLE + "\n\n" + ACCURACY_RULES + "\n\n"
        "Generate a 1024x1024 square Instagram post.\n\n"
        "Header title: WEEKLY REVIEW\n"
        "Date range: {date_range}\n\n"
        "Stats grid:\n"
        "  KEY_STAT_1: {stat_1}\n"
        "  KEY_STAT_2: {stat_2}\n"
        "  KEY_STAT_3: {stat_3}\n"
        "  HIGHLIGHT_A (accent): {highlight_a}\n"
        "  HIGHLIGHT_B (accent): {highlight_b}\n\n"
        "Commentary — 3 bullet points near the bottom:\n"
        "  - {bullet_1}\n"
        "  - {bullet_2}\n"
        "  - {bullet_3}\n"
    ),
    "monthly_report": (
        BRAND_STYLE_PREAMBLE + "\n\n" + ACCURACY_RULES + "\n\n"
        "Generate a 1024x1024 square Instagram post.\n\n"
        "Header title: MONTHLY REVIEW\n"
        "Month/year: {month_year}\n\n"
        "Stats grid (6 cells):\n"
        "  KEY_STAT_1: {stat_1}\n"
        "  KEY_STAT_2: {stat_2}\n"
        "  KEY_STAT_3: {stat_3}\n"
        "  KEY_STAT_4: {stat_4}\n"
        "  KEY_STAT_5: {stat_5}\n"
        "  HIGHLIGHT (accent): {highlight}\n\n"
        "Commentary — 4 bullet points near the bottom:\n"
        "  - {bullet_1}\n"
        "  - {bullet_2}\n"
        "  - {bullet_3}\n"
        "  - {bullet_4}\n"
    ),
}


def format_ig_prompt(post_type: str, data: dict[str, str]) -> str:
    """Substitute structured data into the per-post-type prompt template.
    Returns the final prompt string ready for the operator to paste into ChatGPT."""
    if post_type not in OPENAI_PROMPTS:
        raise ValueError(f"unknown post_type: {post_type}")
    return OPENAI_PROMPTS[post_type].format(**data)


# ---- X (Twitter) publisher -----------------------------------------------

@dataclass
class XPublisher:
    """Posts text to X via tweepy. Threads if content exceeds 280 chars.

    Hashtags (if provided) are placed at the END of the thread — appended to
    the last tweet if combined fits ≤ 280 chars, otherwise posted as their
    own final tweet. Keeps the body readable.
    """

    api_key: str
    api_secret: str
    access_token: str
    access_token_secret: str
    handle: str = "YourHandle"  # for building post URLs

    def is_configured(self) -> bool:
        return bool(
            self.api_key and self.api_secret
            and self.access_token and self.access_token_secret
        )

    def _client(self) -> Any:
        import tweepy
        return tweepy.Client(
            consumer_key=self.api_key,
            consumer_secret=self.api_secret,
            access_token=self.access_token,
            access_token_secret=self.access_token_secret,
        )

    @staticmethod
    def _split_into_thread(text: str, max_len: int = 280) -> list[str]:
        """Split long text into thread-friendly chunks at sentence/line boundaries.
        No (n/N) markers — X's UI displays the thread natively via reply chains."""
        if len(text) <= max_len:
            return [text]
        parts: list[str] = []
        remaining = text.strip()
        while remaining:
            if len(remaining) <= max_len:
                parts.append(remaining)
                break
            for sep in ("\n\n", "\n", ". ", " "):
                cut = remaining[:max_len].rfind(sep)
                if cut > max_len // 2:
                    parts.append(remaining[:cut].strip())
                    remaining = remaining[cut + len(sep):].strip()
                    break
            else:
                parts.append(remaining[:max_len])
                remaining = remaining[max_len:]
        return parts

    def post(self, text: str, hashtags: str = "") -> Optional[str]:
        """Post text to X. Returns the URL of the first tweet on success."""
        if not self.is_configured():
            log.warning("x_publisher_not_configured")
            return None
        try:
            c = self._client()
            parts = self._split_into_thread(text)
            if hashtags:
                tail = parts[-1]
                merged = f"{tail}\n\n{hashtags}"
                if len(merged) <= 280:
                    parts[-1] = merged
                else:
                    parts.append(hashtags)
            first_id: Optional[str] = None
            reply_to: Optional[str] = None
            for p in parts:
                resp = c.create_tweet(text=p, in_reply_to_tweet_id=reply_to)
                tweet_id = str(resp.data["id"])
                if first_id is None:
                    first_id = tweet_id
                reply_to = tweet_id
            url = f"https://x.com/{self.handle}/status/{first_id}"
            log.info("x_post_published", url=url, parts=len(parts))
            return url
        except Exception:
            log.exception("x_post_failed")
            return None


# ---- Instagram publisher (Meta Graph API) --------------------------------

@dataclass
class InstagramPublisher:
    """Posts an operator-supplied image + caption to IG via the new
    Instagram Platform API (graph.instagram.com).

    Three-step flow per Meta's docs:
    1. POST /media — Meta enqueues the container & fetches image_url
    2. Poll container's status_code until FINISHED (Meta processes the image)
    3. POST /media_publish — actually publishes the post

    Without step 2 you get spurious 400s on /media_publish because the
    container isn't ready yet.
    """

    access_token: str
    business_account_id: str
    public_base_url: str
    temp_image_dir: Path = Path("./data/ig_temp")
    api_base: str = "https://graph.instagram.com/v22.0"

    def is_configured(self) -> bool:
        return bool(
            self.access_token
            and self.business_account_id
            and self.public_base_url
        )

    def _save_temp_image(self, image_bytes: bytes) -> tuple[Path, str]:
        """Save bytes to a temp file with a random token name. Returns the
        local Path + the public URL Meta will fetch from."""
        self.temp_image_dir.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(16)
        file_path = self.temp_image_dir / f"{token}.png"
        file_path.write_bytes(image_bytes)
        public_url = f"{self.public_base_url.rstrip('/')}/ig-image/{token}.png"
        return file_path, public_url

    def post(self, image_bytes: bytes, caption: str) -> Optional[str]:
        """Post image+caption to IG. Returns IG permalink on success."""
        if not self.is_configured():
            log.warning("ig_publisher_not_configured")
            return None
        import time
        import requests
        file_path: Optional[Path] = None
        try:
            file_path, public_url = self._save_temp_image(image_bytes)
            caption = caption[:2150]  # IG limit is 2200; safety margin
            base = f"{self.api_base}/{self.business_account_id}"

            # Step 1: create container
            r = requests.post(
                f"{base}/media",
                data={
                    "image_url": public_url,
                    "caption": caption,
                    "access_token": self.access_token,
                },
                timeout=30,
            )
            if not r.ok:
                log.error("ig_media_create_http_error",
                          status=r.status_code, body=r.text)
                return None
            creation_id = r.json().get("id")
            if not creation_id:
                log.error("ig_media_create_no_id", response=r.json())
                return None

            # Step 2: poll until FINISHED
            ready = False
            for attempt in range(30):  # up to ~45s
                time.sleep(1.5)
                status_r = requests.get(
                    f"{self.api_base}/{creation_id}",
                    params={"fields": "status_code,status",
                            "access_token": self.access_token},
                    timeout=15,
                )
                if not status_r.ok:
                    continue
                status_code = status_r.json().get("status_code")
                if status_code == "FINISHED":
                    ready = True
                    break
                if status_code in ("ERROR", "EXPIRED"):
                    log.error("ig_container_failed",
                              status=status_code, body=status_r.text)
                    return None
            if not ready:
                log.error("ig_container_timeout", creation_id=creation_id)
                return None

            # Step 3: publish
            r2 = requests.post(
                f"{base}/media_publish",
                data={
                    "creation_id": creation_id,
                    "access_token": self.access_token,
                },
                timeout=30,
            )
            if not r2.ok:
                log.error("ig_media_publish_http_error",
                          status=r2.status_code, body=r2.text)
                return None
            media_id = r2.json().get("id")
            if not media_id:
                log.error("ig_publish_no_id", response=r2.json())
                return None

            r3 = requests.get(
                f"{self.api_base}/{media_id}",
                params={"fields": "permalink", "access_token": self.access_token},
                timeout=15,
            )
            permalink = r3.json().get("permalink") if r3.ok else None
            log.info("ig_post_published", media_id=media_id, permalink=permalink)
            return permalink or f"ig_media_id:{media_id}"
        except Exception:
            log.exception("ig_post_failed")
            return None
        finally:
            if file_path and file_path.exists():
                try:
                    file_path.unlink()
                except OSError:
                    pass
