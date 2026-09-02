"""IG manual-posting state machine: happy path + invalid transitions."""
from __future__ import annotations

import json

import pytest

from src.ig_workflow import (
    _extract_data,
    handle_post_decision,
    handle_prompt_decision,
    handle_uploaded_image,
    start_workflow_after_publish,
)
from src.social_publisher import InstagramPublisher
from conftest import FakeAnthropicClient, end_turn, make_fake_app

DAILY_RECAP_DATA = {
    "date": "2026-01-15",
    "stat_1": "+1.23%",
    "stat_2": "4 events",
    "highlight": "+0.80%",
    "bullet_1": "Steady day.",
    "bullet_2": "Everything to plan.",
}


def extraction_client(data: dict) -> FakeAnthropicClient:
    return FakeAnthropicClient([end_turn(json.dumps(data))])


@pytest.fixture
def ig_publisher(tmp_path):
    return InstagramPublisher(
        access_token="test-token-not-real",
        business_account_id="111111",
        public_base_url="https://example.invalid",
        temp_image_dir=tmp_path / "ig_temp",
    )


async def start_workflow(db, user, app) -> int:
    workflow_id = await start_workflow_after_publish(
        db=db,
        apps_by_user_id={user.user_id: app},
        notify_user_key=user.user_key,
        published_post_id=1,
        post_type="daily_recap",
        draft_text="Daily recap draft text.",
    )
    assert workflow_id is not None
    return workflow_id


# ---- Stage 2: start ------------------------------------------------------

async def test_start_creates_workflow_and_dms(db, user):
    app = make_fake_app()
    workflow_id = await start_workflow(db, user, app)
    wf = await db.get_ig_workflow(workflow_id)
    assert wf["state"] == "awaiting_prompt_decision"
    assert wf["caption"] == "Daily recap draft text."
    app.bot.send_message.assert_awaited_once()
    assert app.bot.send_message.await_args.kwargs["chat_id"] == user.telegram_user_id


async def test_start_with_unknown_user_returns_none(db):
    result = await start_workflow_after_publish(
        db=db, apps_by_user_id={}, notify_user_key="nobody",
        published_post_id=1, post_type="daily_recap", draft_text="x",
    )
    assert result is None


async def test_start_dm_failure_cancels_workflow(db, user):
    app = make_fake_app()
    app.bot.send_message.side_effect = RuntimeError("network down")
    result = await start_workflow_after_publish(
        db=db, apps_by_user_id={user.user_id: app},
        notify_user_key=user.user_key,
        published_post_id=1, post_type="daily_recap", draft_text="x",
    )
    assert result is None
    wf = await db.get_ig_workflow(1)
    assert wf["state"] == "cancelled"


# ---- Stage 3: prompt decision --------------------------------------------

async def test_prompt_decision_yes_sends_prompt(db, user, tmp_path):
    app = make_fake_app()
    workflow_id = await start_workflow(db, user, app)
    result = await handle_prompt_decision(
        db=db, anthropic_client=extraction_client(DAILY_RECAP_DATA),
        apps_by_user_id={user.user_id: app},
        workflow_id=workflow_id, decision="yes",
        templates_dir=str(tmp_path / "templates"),
    )
    assert result == {"ok": True, "decision": "yes"}
    wf = await db.get_ig_workflow(workflow_id)
    assert wf["state"] == "awaiting_image"
    prompt_text = app.bot.send_message.await_args.kwargs["text"]
    assert "+1.23%" in prompt_text
    assert "DAILY RECAP" in prompt_text


async def test_prompt_decision_yes_attaches_template_png(db, user, tmp_path):
    app = make_fake_app()
    workflow_id = await start_workflow(db, user, app)
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "daily_recap_template.png").write_bytes(b"not-a-real-png")
    await handle_prompt_decision(
        db=db, anthropic_client=extraction_client(DAILY_RECAP_DATA),
        apps_by_user_id={user.user_id: app},
        workflow_id=workflow_id, decision="yes",
        templates_dir=str(templates),
    )
    app.bot.send_photo.assert_awaited_once()


async def test_prompt_decision_no_cancels(db, user, tmp_path):
    app = make_fake_app()
    workflow_id = await start_workflow(db, user, app)
    result = await handle_prompt_decision(
        db=db, anthropic_client=extraction_client({}),
        apps_by_user_id={user.user_id: app},
        workflow_id=workflow_id, decision="no",
        templates_dir=str(tmp_path),
    )
    assert result == {"ok": True, "decision": "no"}
    wf = await db.get_ig_workflow(workflow_id)
    assert wf["state"] == "cancelled"


async def test_prompt_decision_wrong_state_rejected(db, user, tmp_path):
    app = make_fake_app()
    workflow_id = await start_workflow(db, user, app)
    await db.update_ig_workflow_state(workflow_id, "cancelled")
    result = await handle_prompt_decision(
        db=db, anthropic_client=extraction_client({}),
        apps_by_user_id={user.user_id: app},
        workflow_id=workflow_id, decision="yes",
        templates_dir=str(tmp_path),
    )
    assert result == {"error": "already_decided", "state": "cancelled"}


async def test_prompt_decision_missing_workflow(db, user, tmp_path):
    result = await handle_prompt_decision(
        db=db, anthropic_client=extraction_client({}),
        apps_by_user_id={}, workflow_id=999, decision="yes",
        templates_dir=str(tmp_path),
    )
    assert result["error"] == "workflow_not_found"


async def test_prompt_decision_incomplete_data_cancels(db, user, tmp_path):
    # Extraction returned a dict missing template fields -> KeyError on
    # format -> workflow cancelled, error reported.
    app = make_fake_app()
    workflow_id = await start_workflow(db, user, app)
    result = await handle_prompt_decision(
        db=db, anthropic_client=extraction_client({"date": "2026-01-15"}),
        apps_by_user_id={user.user_id: app},
        workflow_id=workflow_id, decision="yes",
        templates_dir=str(tmp_path),
    )
    assert result["error"] == "format_failed"
    wf = await db.get_ig_workflow(workflow_id)
    assert wf["state"] == "cancelled"


async def test_extract_data_strips_code_fences():
    fenced = "```json\n" + json.dumps(DAILY_RECAP_DATA) + "\n```"
    client = FakeAnthropicClient([end_turn(fenced)])
    data = await _extract_data(client, "daily_recap", "irrelevant draft")
    assert data == DAILY_RECAP_DATA


# ---- Stage 4: image upload -----------------------------------------------

async def advance_to_awaiting_image(db, user, app, tmp_path) -> int:
    workflow_id = await start_workflow(db, user, app)
    await handle_prompt_decision(
        db=db, anthropic_client=extraction_client(DAILY_RECAP_DATA),
        apps_by_user_id={user.user_id: app},
        workflow_id=workflow_id, decision="yes",
        templates_dir=str(tmp_path / "templates"),
    )
    return workflow_id


async def test_uploaded_image_advances_state(db, user, tmp_path):
    app = make_fake_app()
    workflow_id = await advance_to_awaiting_image(db, user, app, tmp_path)
    result = await handle_uploaded_image(
        db=db, apps_by_user_id={user.user_id: app},
        telegram_user_id=user.telegram_user_id,
        image_bytes=b"fake-png-bytes",
        uploads_dir=str(tmp_path / "uploads"),
    )
    assert result == {"ok": True, "workflow_id": workflow_id}
    wf = await db.get_ig_workflow(workflow_id)
    assert wf["state"] == "awaiting_post_decision"
    assert (tmp_path / "uploads" / f"workflow_{workflow_id}.png").read_bytes() \
        == b"fake-png-bytes"


async def test_uploaded_image_without_workflow(db, user, tmp_path):
    result = await handle_uploaded_image(
        db=db, apps_by_user_id={}, telegram_user_id=user.telegram_user_id,
        image_bytes=b"x", uploads_dir=str(tmp_path),
    )
    assert result == {"error": "no_active_workflow"}


# ---- Stage 5: post decision ----------------------------------------------

async def advance_to_awaiting_post(db, user, app, tmp_path) -> int:
    workflow_id = await advance_to_awaiting_image(db, user, app, tmp_path)
    await handle_uploaded_image(
        db=db, apps_by_user_id={user.user_id: app},
        telegram_user_id=user.telegram_user_id,
        image_bytes=b"fake-png-bytes",
        uploads_dir=str(tmp_path / "uploads"),
    )
    return workflow_id


async def test_post_decision_yes_publishes(db, user, tmp_path, ig_publisher,
                                           monkeypatch):
    app = make_fake_app()
    workflow_id = await advance_to_awaiting_post(db, user, app, tmp_path)
    captured = {}

    def fake_post(image_bytes, caption):
        captured["caption"] = caption
        return "https://instagram.example.invalid/p/TESTPOST"

    monkeypatch.setattr(ig_publisher, "post", fake_post)
    monkeypatch.setenv("IG_HASHTAGS", "#test #fake")
    result = await handle_post_decision(
        db=db, apps_by_user_id={user.user_id: app},
        ig_publisher=ig_publisher, workflow_id=workflow_id, decision="yes",
    )
    assert result["ok"] is True
    wf = await db.get_ig_workflow(workflow_id)
    assert wf["state"] == "posted"
    assert wf["ig_permalink"] == "https://instagram.example.invalid/p/TESTPOST"
    assert captured["caption"].startswith("Daily recap draft text.")
    assert captured["caption"].endswith("#test #fake")


async def test_post_decision_no_cancels(db, user, tmp_path, ig_publisher):
    app = make_fake_app()
    workflow_id = await advance_to_awaiting_post(db, user, app, tmp_path)
    result = await handle_post_decision(
        db=db, apps_by_user_id={user.user_id: app},
        ig_publisher=ig_publisher, workflow_id=workflow_id, decision="no",
    )
    assert result == {"ok": True, "decision": "no"}
    wf = await db.get_ig_workflow(workflow_id)
    assert wf["state"] == "cancelled"


async def test_post_decision_wrong_state_rejected(db, user, tmp_path,
                                                  ig_publisher):
    # Still in awaiting_prompt_decision: posting must be refused.
    app = make_fake_app()
    workflow_id = await start_workflow(db, user, app)
    result = await handle_post_decision(
        db=db, apps_by_user_id={user.user_id: app},
        ig_publisher=ig_publisher, workflow_id=workflow_id, decision="yes",
    )
    assert result["error"] == "already_decided"
    wf = await db.get_ig_workflow(workflow_id)
    assert wf["state"] == "awaiting_prompt_decision"  # unchanged


async def test_post_decision_unconfigured_publisher(db, user, tmp_path):
    app = make_fake_app()
    workflow_id = await advance_to_awaiting_post(db, user, app, tmp_path)
    unconfigured = InstagramPublisher(
        access_token="", business_account_id="", public_base_url="",
    )
    result = await handle_post_decision(
        db=db, apps_by_user_id={user.user_id: app},
        ig_publisher=unconfigured, workflow_id=workflow_id, decision="yes",
    )
    assert result == {"error": "ig_not_configured"}
    wf = await db.get_ig_workflow(workflow_id)
    assert wf["state"] == "cancelled"


async def test_post_decision_publish_failure(db, user, tmp_path, ig_publisher,
                                             monkeypatch):
    app = make_fake_app()
    workflow_id = await advance_to_awaiting_post(db, user, app, tmp_path)
    monkeypatch.setattr(ig_publisher, "post", lambda image_bytes, caption: None)
    result = await handle_post_decision(
        db=db, apps_by_user_id={user.user_id: app},
        ig_publisher=ig_publisher, workflow_id=workflow_id, decision="yes",
    )
    assert result == {"error": "ig_publish_failed"}
    wf = await db.get_ig_workflow(workflow_id)
    assert wf["state"] == "cancelled"
    assert wf["error"] == "IG publish returned None"
