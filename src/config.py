"""Settings + user-config loader.

Settings come from .env (via pydantic-settings). Per-user config (Telegram
user_ids, timezones, daily call limits, etc.) comes from users.yaml.
"""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ---- Anthropic ----
    anthropic_api_key: str

    # ---- Telegram ----
    telegram_bot_token_operator: str
    telegram_bot_token_channel: str = ""
    channel_handle: str = ""

    # ---- X / Twitter ----
    x_api_key: str = ""
    x_api_secret: str = ""
    x_access_token: str = ""
    x_access_token_secret: str = ""
    x_handle: str = "YourHandle"

    # ---- Instagram ----
    ig_access_token: str = ""
    ig_business_account_id: str = ""
    ig_public_base_url: str = ""
    ig_templates_dir: str = "./assets/ig_templates"
    ig_uploads_dir: str = "./data/ig_uploads"
    ig_hashtags: str = ""  # space-separated #tags

    # ---- Voice anchor ----
    strategy_notes_path: str = "./strategy_notes.md"

    # ---- Storage / logging ----
    database_path: str = "./data/bot.db"
    log_level: str = "INFO"
    log_file: str = "./logs/app.log"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )


class UserConfig(BaseModel):
    user_key: str
    display_name: str
    telegram_user_id: int
    default_language: str = "English"
    timezone: str = "UTC"
    daily_call_limit: int = 200


class UsersConfig(BaseModel):
    users: list[UserConfig]


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


def load_users_config(path: str = "users.yaml") -> UsersConfig:
    """Load whitelist + per-user settings from users.yaml."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return UsersConfig(**data)
