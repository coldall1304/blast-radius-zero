"""Guards for reasoning models: no content + finish=length must be an explicit error."""
from __future__ import annotations

import pytest

from app import inference as inf
from app.config import settings


class FakeResponse:
    def __init__(self, body: dict) -> None:
        self.status_code = 200
        self._body = body
        self.text = str(body)

    def json(self) -> dict:
        return self._body


class FakeClient:
    """Captures the payload and replays a canned reply."""

    def __init__(self, body: dict) -> None:
        self.body = body
        self.payload: dict | None = None

    async def post(self, url, json=None, headers=None):  # noqa: A002 (mirrors httpx)
        self.payload = json
        return FakeResponse(self.body)


def reply(content: str, finish: str = "stop", reasoning_tokens: int = 0) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish}],
        "usage": {"completion_tokens_details": {"reasoning_tokens": reasoning_tokens}},
    }


@pytest.fixture
def live_settings():
    keys = ("mock_inference", "inference_api_key", "inference_reasoning_effort", "planner_max_tokens")
    saved = {k: getattr(settings, k) for k in keys}
    # settings is a frozen dataclass; the module-level singleton is patched directly.
    object.__setattr__(settings, "mock_inference", False)
    object.__setattr__(settings, "inference_api_key", "test-key")
    object.__setattr__(settings, "inference_reasoning_effort", "low")
    object.__setattr__(settings, "planner_max_tokens", 3000)
    try:
        yield settings
    finally:
        for key, value in saved.items():
            object.__setattr__(settings, key, value)


@pytest.mark.asyncio
async def test_empty_content_with_length_finish_raises(live_settings):
    client = inf.Inference()
    client._client = FakeClient(reply("", finish="length", reasoning_tokens=3000))
    with pytest.raises(inf.InferenceError) as exc:
        await client.chat([{"role": "user", "content": "plan"}])
    assert "hidden reasoning" in str(exc.value)


@pytest.mark.asyncio
async def test_reasoning_effort_is_forwarded(live_settings):
    client = inf.Inference()
    fake = FakeClient(reply('{"action":"finish","answer":"ok","claims":[]}'))
    client._client = fake
    text = await client.chat([{"role": "user", "content": "plan"}])
    assert text.startswith("{")
    assert fake.payload["reasoning_effort"] == "low"
    assert fake.payload["max_tokens"] == 3000


@pytest.mark.asyncio
async def test_default_effort_omits_the_parameter(live_settings):
    object.__setattr__(settings, "inference_reasoning_effort", "default")
    client = inf.Inference()
    fake = FakeClient(reply("{}"))
    client._client = fake
    await client.chat([{"role": "user", "content": "plan"}])
    assert "reasoning_effort" not in fake.payload
