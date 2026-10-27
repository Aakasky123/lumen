import asyncio

import pytest

from app.agent_eval.contract import EvalBody, EvalTask
from app.agent_eval.playwright_body import PlaywrightEvalBody


def test_playwright_eval_body_imports_but_is_disabled() -> None:
    body = PlaywrightEvalBody()
    assert isinstance(body, EvalBody)

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="disabled"):
            await body.reset(EvalTask(id="disabled", goal="Do not launch a browser.", initial_url=None))

    asyncio.run(exercise())
