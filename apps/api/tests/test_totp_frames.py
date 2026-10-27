"""Credential replay and frames: the top document or a frame on the page's own origin; anything else is refused."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from tests.test_totp import SEED, _OtpBody, _create_totp_run, _run_totp


class _FrameBody(_OtpBody):
    def __init__(self, url: str, frame_fields: dict[str, Any]) -> None:
        super().__init__(url)
        self.frame_fields = frame_fields

    async def capture_screenshot(self) -> dict[str, Any]:
        shot = await super().capture_screenshot()
        for control in shot["page_evidence"]["controls"]:
            control.update(self.frame_fields)
        return shot


def _run(monkeypatch: pytest.MonkeyPatch, frame_fields_for) -> tuple[_FrameBody, dict[str, Any]]:
    origin = f"https://login-{uuid.uuid4().hex}.example.test"
    url = f"{origin}/two-step"
    goal = f"Sign in at {origin} using the authenticator key {SEED}."
    run_id, user_id = _create_totp_run(target_url=url, goal=goal)
    body = _FrameBody(url, frame_fields_for(origin))
    return body, _run_totp(monkeypatch, goal=goal, body=body, run_id=run_id, user_id=user_id)


def test_a_same_origin_frame_login_works_once_the_frame_reports_its_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    body, out = _run(monkeypatch, lambda origin: {"frame_origin": origin, "frame_depth": 1})
    assert len(body.credential_calls) == 1 and out["summary"]["state"] == "READY_FOR_REVIEW"


@pytest.mark.parametrize(
    "fields",
    [
        lambda origin: {"frame_origin": "https://evil.example.test", "frame_depth": 1},
        lambda origin: {"frame_origin": "null", "frame_depth": 1},
        lambda origin: {"frame_id": 4},
        lambda origin: {"frame_origin": origin.replace("https://", "http://"), "frame_depth": 1},
    ],
    ids=["foreign-origin", "opaque-origin", "frame-without-origin", "other-scheme"],
)
def test_foreign_or_unknown_frames_are_refused(monkeypatch: pytest.MonkeyPatch, fields) -> None:
    body, out = _run(monkeypatch, fields)
    assert body.credential_calls == [] and out["classifier_calls"] == []
    assert "credential_control_in_foreign_frame" in json.dumps(out["contexts"][1:], default=str)
