"""Config loading, cost math, tool-definition sanity, prompt rendering."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.claude_client import (
    INPUT_COST_PER_M,
    OUTPUT_COST_PER_M,
    TOOLS,
    cost_usd,
)
from src.config import UserConfig, load_users_config
from src.prompts import render_system_prompt

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ---- users.yaml loading --------------------------------------------------

def test_load_users_example_yaml():
    cfg = load_users_config(str(PROJECT_ROOT / "users.example.yaml"))
    assert len(cfg.users) == 1
    u = cfg.users[0]
    assert u.user_key == "A"
    assert u.telegram_user_id == 123456789
    assert u.daily_call_limit == 200
    assert u.timezone == "Europe/London"


def test_user_config_defaults():
    u = UserConfig(user_key="B", display_name="Test", telegram_user_id=111111)
    assert u.default_language == "English"
    assert u.timezone == "UTC"
    assert u.daily_call_limit == 200


def test_load_users_config_rejects_bad_shape(tmp_path):
    bad = tmp_path / "users.yaml"
    bad.write_text("users:\n  - user_key: 'A'\n", encoding="utf-8")
    with pytest.raises(Exception):  # pydantic ValidationError
        load_users_config(str(bad))


# ---- cost math -----------------------------------------------------------

def test_cost_usd_input_only():
    assert cost_usd(1_000_000, 0) == pytest.approx(INPUT_COST_PER_M)


def test_cost_usd_output_only():
    assert cost_usd(0, 1_000_000) == pytest.approx(OUTPUT_COST_PER_M)


def test_cost_usd_combined():
    expected = 500 * INPUT_COST_PER_M / 1e6 + 200 * OUTPUT_COST_PER_M / 1e6
    assert cost_usd(500, 200) == pytest.approx(expected)
    assert cost_usd(0, 0) == 0.0


# ---- tool definitions ----------------------------------------------------

def test_tool_definitions_are_well_formed():
    names = [t["name"] for t in TOOLS]
    assert len(names) == len(set(names)), "duplicate tool names"
    for t in TOOLS:
        assert t["description"], t["name"]
        assert t["input_schema"]["type"] == "object", t["name"]


def test_tools_cover_router_dispatch():
    # Every tool Claude can call must have a dispatch branch in the router.
    router_src = (PROJECT_ROOT / "src" / "router.py").read_text(encoding="utf-8")
    for t in TOOLS:
        assert f'name == "{t["name"]}"' in router_src, t["name"]


# ---- system prompt -------------------------------------------------------

def test_render_system_prompt_missing_anchor(user):
    prompt = render_system_prompt(user, "./does_not_exist.md")
    assert "Test Operator" in prompt
    assert "strategy_notes.md not found" in prompt


def test_render_system_prompt_with_anchor(user, tmp_path):
    anchor = tmp_path / "strategy_notes.md"
    anchor.write_text("Voice rule: be terse.", encoding="utf-8")
    prompt = render_system_prompt(user, str(anchor))
    assert "Voice rule: be terse." in prompt
