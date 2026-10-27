import asyncio

from app import extension_run
from app.extension_body import ExtensionBody


class _Accepting:
    async def vision_click_control(self, selector, x, y, *, allow_rebind_on_drift=False):
        return {}


class _Legacy:
    async def vision_click_control(self, selector, x, y):
        return {}


IDENTITY = {"target_selector": "#go", "target_text": "Click me", "target_tag": "button"}


def _kwargs(body=None, **overrides):
    args = dict(ordinary_allowed=True, classification="ordinary_reversible", expected_identity=IDENTITY)
    args.update(overrides)
    return extension_run._rebind_on_drift_kwargs((body or _Accepting()).vision_click_control, **args)


def test_ordinary_stable_id_click_may_rebind():
    assert _kwargs() == {"allow_rebind_on_drift": True}


def test_final_irreversible_uncertain_or_unjudged_clicks_stay_strict():
    for classification in ("consequential_irreversible", "uncertain", ""):
        assert _kwargs(classification=classification, ordinary_allowed=False) == {}
    # Ordinary label but the allowance was not granted (low confidence, fail-closed judge).
    assert _kwargs(ordinary_allowed=False) == {}


def test_click_without_observed_identity_never_rebinds():
    assert _kwargs(expected_identity=None) == {}
    assert _kwargs(expected_identity={"target_text": "Click me"}) == {}


def test_body_without_support_gets_no_flag():
    assert _kwargs(_Legacy()) == {}


def test_extension_body_sends_flag_only_when_set():
    sent = []

    class Body(ExtensionBody):
        def __init__(self):
            pass

        async def _call(self, method, params, timeout=20.0):
            sent.append((method, params))
            return {"ok": True}

        def _params_with_sensitive_mask_binding(self, params):
            return params

    body = Body()
    asyncio.run(body.vision_click_control("#go", 1, 2, allow_rebind_on_drift=True))
    asyncio.run(body.vision_click_control("#go", 1, 2))
    assert sent[0][1]["allow_rebind_on_drift"] is True
    assert "allow_rebind_on_drift" not in sent[1][1]
    # A coordinate click has no identity to rebind: the method has no such parameter.
    assert not extension_run._method_accepts(body.vision_click, "allow_rebind_on_drift")


def test_rebind_is_only_for_the_primary_single_left_click():
    from app.extension_run import _rebind_on_drift_kwargs

    def method(*, allow_rebind_on_drift=False):  # noqa: ANN001
        return None

    base = dict(ordinary_allowed=True, classification="ordinary_reversible", expected_identity={"target_selector": "#go"})
    assert _rebind_on_drift_kwargs(method, **base, action={"action": "click"}) == {"allow_rebind_on_drift": True}
    assert _rebind_on_drift_kwargs(method, **base, action={"action": "click"}, followup=True) == {}
    assert _rebind_on_drift_kwargs(method, **base, action={"action": "click", "follow": True}) == {}
    assert _rebind_on_drift_kwargs(method, **base, action={"action": "click", "clicks": 2}) == {}
    assert _rebind_on_drift_kwargs(method, **base, action={"action": "click", "button": "right"}) == {}
