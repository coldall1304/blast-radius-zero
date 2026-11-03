"""Vultr Serverless Inference client (OpenAI-compatible).

The agent never hands credentials to the sandbox. Only this module holds the
API key, and it lives in the host process.
"""
from __future__ import annotations

import json
import re
from typing import Any

import httpx

from .config import settings


class InferenceError(RuntimeError):
    pass


class Inference:
    """Thin async wrapper over /chat/completions."""

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=settings.inference_timeout_s)

    @property
    def model(self) -> str:
        return settings.inference_model

    async def chat(self, messages: list[dict[str, str]], temperature: float = 0.2,
                   max_tokens: int | None = None,
                   reasoning_effort: str | None = None) -> str:
        text, _finish = await self.chat_ex(messages, temperature, max_tokens, reasoning_effort)
        return text

    async def chat_ex(self, messages: list[dict[str, str]], temperature: float = 0.2,
                      max_tokens: int | None = None,
                      reasoning_effort: str | None = None) -> tuple[str, str]:
        """Return ``(content, finish_reason)`` so callers can spot a cut-off reply."""
        budget = max_tokens or settings.planner_max_tokens
        if settings.mock_inference:
            return _mock_chat(messages), "stop"
        if not settings.inference_api_key:
            raise InferenceError("INFERENCE_API_KEY is not configured")

        payload = {
            "model": settings.inference_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": budget,
        }
        effort = (reasoning_effort if reasoning_effort is not None
                  else settings.inference_reasoning_effort)
        if effort and effort != "default":
            payload["reasoning_effort"] = effort
        headers = {"Authorization": f"Bearer {settings.inference_api_key}"}
        url = f"{settings.inference_base_url.rstrip('/')}/chat/completions"
        try:
            r = await self._client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:  # network level
            raise InferenceError(f"inference transport error: {exc}") from exc

        if r.status_code >= 400:
            detail = r.text[:400]
            raise InferenceError(f"inference HTTP {r.status_code}: {detail}")

        try:
            body = r.json()
            choice = body["choices"][0]
            content = choice["message"]["content"] or ""
            finish = str(choice.get("finish_reason") or "")
        except (KeyError, IndexError, ValueError) as exc:
            raise InferenceError(f"unexpected inference payload: {r.text[:300]}") from exc
        if not content.strip() and finish == "length":
            usage = body.get("usage") or {}
            spent = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
            raise InferenceError(
                f"model returned no content: it spent the whole {budget}-token budget "
                f"on hidden reasoning (reasoning_tokens={spent})"
            )
        return content, finish


def extract_json(text: str) -> dict[str, Any]:
    """Parse the first JSON object out of a model reply.

    Models occasionally wrap JSON in prose or fences; we tolerate that but
    reject anything that is not a single object.
    """
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    raise InferenceError("model did not return a JSON object")


def _mock_chat(messages: list[dict[str, str]]) -> str:
    """Deterministic stand-in used for tests and offline demos.

    It reads the task from the last user message and walks a tiny scripted
    plan so the sandbox and verification plumbing can be exercised without a
    key. The UI labels these runs as MOCK.
    """
    prompt = messages[-1]["content"] if messages else ""
    system = (messages[0]["content"] if messages else "").lower()
    if "audit" in system or "verifier" in system:
        return json.dumps({
            "sufficient": True,
            "reason": "mock audit: the recorded stdout contains the value the answer cites",
            "followup": "",
        })
    task = prompt.split("TASK:", 1)[-1].strip().splitlines()[0] if "TASK:" in prompt else prompt
    step_marker = prompt.count("STEP_RESULT")
    if step_marker == 0:
        return json.dumps({
            "action": "run",
            "title": "Compute summary statistics",
            "explain": "First pass: inspect the environment and produce a small deterministic artifact.",
            "code": (
                "import sys, platform, json\n"
                "rows = [{'n': i, 'sq': i*i} for i in range(1, 11)]\n"
                "total = sum(r['sq'] for r in rows)\n"
                "print('python', sys.version.split()[0], platform.machine())\n"
                "print('sum_of_squares', total)\n"
                "json.dump(rows, open('/out/rows.json','w'))\n"
                "print('wrote /out/rows.json')\n"
                "RESULT = {'sum_of_squares': total}\n"
            ),
        })
    if step_marker == 1:
        return json.dumps({
            "action": "finish",
            "answer": f"Task handled in the sandbox. {task[:200]}",
            "claims": [{"claim": "computed sum of squares 1..10 = 385", "evidence_step": 1}],
        })
    return json.dumps({"action": "finish", "answer": "mock run complete", "claims": []})
