from __future__ import annotations

from app.agent_eval import decisions_replay as dr


def _record(image_sent: bool = True) -> dict:
    return {
        "request": {
            "system": "You are a judge.",
            "prompt": {
                "task": "t",
                "rules": ["r1"],
                "entries": [{"index": 0, "source": "profile", "label": "Country", "value": "US"}, {"index": 1, "source": "page", "label": "page text"}],
                "response_shape": {"grounded": "boolean"},
            },
            "entry_count": 2,
            "image_sent": image_sent,
        },
        "outcome": {"ok": True, "verdict": {"grounded": True, "entry_index": 1, "instrumental": False, "commits_or_destroys": False}},
    }


def test_grounding_mapping_text_and_image_state():
    image = {"media_type": "image/jpeg", "data": "QUJD"}
    state, questions, text = dr.grounding_request(_record(), [image])
    assert isinstance(state, list) and state[0]["type"] == "text" and state[0]["text"] == text
    assert state[1] == {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJD"}}
    assert "response_shape" not in text and "You are a judge." in text
    assert {name: q["type"] for name, q in questions.items()} == {
        "grounded": "noul", "instrumental": "noul", "commits_or_destroys": "noul", "supporting_entry": "choice",
    }
    assert set(questions["supporting_entry"]["criteria"]) == {"none", "e0", "e1"}
    state_text, _, _ = dr.grounding_request(_record(False), None)
    assert isinstance(state_text, str)


def test_payment_mapping():
    state, questions, _ = dr.payment_request({"viewport_text": "x" * 3000, "label": "Hold booking", "payment": False})
    assert len(state["viewport_text_excerpt"]) == 1200 and state["submit_control_label"] == "Hold booking"
    assert questions["payment"]["type"] == "noul"


def test_recorded_grounding_normalizes_like_the_judge():
    record = _record()
    assert dr.recorded_grounding(record)["entry_index"] == 1
    record["outcome"]["verdict"]["entry_index"] = 7  # outside the entries: not grounded
    assert dr.recorded_grounding(record) == {"grounded": False, "entry_index": -1, "instrumental": False, "commits_or_destroys": False}
    record["outcome"]["verdict"] = {}
    assert dr.recorded_grounding(record) is None


def test_client_with_mock_transport_parses_answers_and_retries(monkeypatch):
    monkeypatch.setattr(dr.time, "sleep", lambda _s: None)  # no real waiting in the retry
    calls = []

    def post(url, body, headers, timeout):
        calls.append(body)
        if len(calls) == 1:
            return 429, {"error": "slow down"}
        return 200, {"answers": {"payment": {"type": "noul", "noul": 0.9}}, "usage": {"input_tokens": 100, "cost": 1e-5}}

    client = dr.DecisionsClient("k", post=post, retries=2)
    result = client.decide("s", {"payment": {"type": "noul", "instructions": "i"}})
    assert result["ok"] and len(calls) == 2 and calls[0]["model"] == dr.DECISIONS_MODEL
    assert dr.noul_probability(result["answers"], "payment") == 0.9


def test_image_trap_flags_text_read_images():
    assert dr.image_trap({"usage": {"input_tokens": 63000}}, "x" * 17000, True)
    assert not dr.image_trap({"usage": {"input_tokens": 6000}}, "x" * 17000, True)
    assert not dr.image_trap({"usage": {"input_tokens": 63000}}, "x", False)


def test_safe_threshold_and_auroc():
    # grounded-style (yes is permissive): recorded-no item scores 0.6, yes items 0.8/0.95
    items = [(0.6, 0.0, False), (0.8, 0.0, True), (0.95, 0.0, True)]
    result = dr.safe_threshold(items, permissive_when="yes")
    assert result["exists"] and result["agreement"] == 1.0 and 0.6 < result["threshold_min"] <= 0.8
    # payment-style (no is permissive): a recorded-yes item at 0.2 below a recorded-no at 0.3 cannot be cut safely at full agreement
    pay = [(0.2, 0.0, True), (0.3, 0.0, False)]
    cut = dr.safe_threshold(pay, permissive_when="no")
    assert cut["exists"] and cut["agreement"] < 1.0
    assert dr.auroc([(0.9, True), (0.1, False), (0.5, True), (0.5, False)]) == 0.875
