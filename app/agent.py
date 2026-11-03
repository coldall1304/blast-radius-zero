"""Plan → dispatch → verify loop.

The control plane is deliberately dumb about *what* to run: the model proposes
one Python program at a time, the sandbox executes it, and the model sees only
the recorded output. The model never gets credentials, a shell on the host, or
a reusable sandbox.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

from . import db, sandbox
from .config import settings
from .inference import Inference, InferenceError, extract_json

PLANNER_SYSTEM = """You are the planner of a containment-first autonomous agent.

You work in strict turns. On each turn reply with ONE JSON object and nothing else.

Allowed actions:

1. Run code in an isolated sandbox:
{"action":"run","title":"short imperative title","explain":"one sentence on why","code":"<complete python program>"}

2. Finish and answer the user:
{"action":"finish","answer":"what you concluded, grounded in observed output","claims":[{"claim":"specific factual claim","evidence_step":2}]}

Sandbox facts you must respect:
- Fresh container per step. Only files under /out survive between steps of the same run, so write anything a later step needs to /out.
- Filesystem is read-only except /tmp and /out. No network access at all.
- Runs as an unprivileged user with no capabilities. Not root. No package installs.
- Python 3.12 with stdlib, numpy, pandas and matplotlib available.
- Every run has a hard wall-clock limit, memory cap, process cap and disk cap.
- Your program must print the evidence a reader needs. Long output is truncated.
- Never assume data exists. Generate or read it explicitly, and check for errors.
- Prefer small, verifiable steps over one giant script.

Finishing discipline:
- Return "finish" as soon as the evidence supports the answer. Do not run extra confirmation steps.
- If this is the last step the budget allows, you MUST return "finish" instead of running more code.

If a step failed, read stderr and fix the specific cause on the next turn.
Do not repeat an identical failing program.
Answer in the user's language.
"""

VERIFIER_SYSTEM = """You audit an agent's answer against observed sandbox evidence.
Reply with ONE JSON object:
{"sufficient":true|false,"reason":"one sentence","followup":"if not sufficient, the single most useful next check, else empty"}

Rules:
- sufficient=true only if the answer's concrete claims are supported by the recorded stdout/stderr/artifacts.
- Unsupported claims, invented numbers, or claims about files that were never produced => sufficient=false.
- Do not reward confident prose. Evidence only.
"""

FINALIZER_SYSTEM = """The step budget for this run is exhausted. Close the run honestly.

Reply with ONE JSON object:
{"answer":"best answer supported by the recorded evidence","claims":[{"claim":"specific factual claim","evidence_step":2}]}

Rules:
- Use only what the recorded steps actually show.
- If the evidence is incomplete, say exactly what was and was not verified.
- Never invent numbers, file contents or success.
Answer in the user's language.
"""


def _evidence_block(steps: list[dict[str, Any]]) -> str:
    parts = []
    for s in steps:
        out = (s.get("stdout") or "").strip()
        err = (s.get("stderr") or "").strip()
        if len(out) > 2500:
            out = out[:2500] + "\n...[truncated]"
        if len(err) > 800:
            err = err[:800] + "\n...[truncated]"
        parts.append(
            f"STEP {s['no']} — {s['title']}\n"
            f"exit_code={s['exit_code']} timed_out={s['timed_out']} "
            f"killed={s.get('killed_reason') or 'none'}\n"
            f"stdout:\n{out or '(empty)'}\n"
            f"stderr:\n{err or '(empty)'}\n"
            f"artifacts: {[a['name'] for a in s.get('artifacts', [])]}"
        )
    return "\n\n".join(parts)


def _strip_code(code: str) -> str:
    code = code.strip()
    if code.startswith("```"):
        lines = code.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        code = "\n".join(lines)
    return code


async def _ask_planner(inference: Inference, task: str, steps: list[dict[str, Any]],
                       feedback: str) -> dict[str, Any]:
    user = [
        f"TASK:\n{task}",
        f"STEP_BUDGET: {settings.max_steps} (this is step {len(steps) + 1})",
    ]
    if steps:
        user.append(f"STEP_RESULT history:\n{_evidence_block(steps)}")
    if feedback:
        user.append(f"AUDITOR FEEDBACK:\n{feedback}")
    messages = [
        {"role": "system", "content": PLANNER_SYSTEM},
        {"role": "user", "content": "\n\n".join(user)},
    ]
    last_error = ""
    budgets = [settings.planner_max_tokens, settings.planner_max_tokens * 2]
    for attempt, budget in enumerate(budgets):
        try:
            raw, finish = await inference.chat_ex(messages, max_tokens=budget)
        except InferenceError as exc:
            last_error = str(exc)
            messages.append({
                "role": "user",
                "content": "Your last turn produced no usable JSON. Reply with the JSON object only "
                           "and keep any internal reasoning extremely short.",
            })
            continue
        try:
            return extract_json(raw)
        except InferenceError as exc:
            last_error = str(exc)
            if finish == "length":
                last_error = f"reply was cut off at {budget} tokens before the JSON object was complete"
            messages.append({"role": "assistant", "content": raw[:1500]})
            messages.append({
                "role": "user",
                "content": "That was not a single valid JSON object. Reply again with JSON only.",
            })
    raise InferenceError(f"planner produced no JSON after {len(budgets)} attempts: {last_error}")


async def _verify(inference: Inference, task: str, answer: str,
                  steps: list[dict[str, Any]]) -> dict[str, Any]:
    user = (
        f"TASK:\n{task}\n\nPROPOSED ANSWER:\n{answer}\n\n"
        f"EVIDENCE:\n{_evidence_block(steps)}"
    )
    try:
        raw = await inference.chat([
            {"role": "system", "content": VERIFIER_SYSTEM},
            {"role": "user", "content": user},
        ], temperature=0.0, max_tokens=settings.verifier_max_tokens)
        obj = extract_json(raw)
        return {"sufficient": bool(obj.get("sufficient")), "reason": str(obj.get("reason", "")),
                "followup": str(obj.get("followup", ""))}
    except (InferenceError, Exception) as exc:  # verifier must never break a run
        return {"sufficient": None, "reason": f"verifier unavailable: {exc}", "followup": ""}


async def run_task(run_id: str, task: str, publish: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    inference = Inference()
    steps: list[dict[str, Any]] = []
    feedback = ""
    verifier_rejections = 0
    final: dict[str, Any] = {
        "answer": "",
        "verified": None,
        "verification": "",
        "model": inference.model,
        "mock": settings.mock_inference,
    }

    publish({"type": "run.status", "status": "planning"})

    for step_no in range(1, settings.max_steps + 1):
        try:
            decision = await _ask_planner(inference, task, steps, feedback)
        except InferenceError as exc:
            final["answer"] = f"Inference failed before a plan was produced: {exc}"
            final["error"] = True
            publish({"type": "run.error", "message": str(exc)})
            return final

        action = str(decision.get("action", "")).lower()

        if action == "finish":
            answer = str(decision.get("answer", "")).strip()
            claims = decision.get("claims", [])
            if not isinstance(claims, list):
                claims = []
            publish({"type": "run.status", "status": "verifying"})
            verdict = await _verify(inference, task, answer, steps)
            final.update({
                "answer": answer,
                "verified": verdict["sufficient"],
                "verification": verdict["reason"],
                "claims": claims,
            })
            publish({"type": "verify", **verdict})
            if verdict["sufficient"] is not False:
                return final
            verifier_rejections += 1
            if verifier_rejections > settings.max_verify_retries or step_no == settings.max_steps:
                final["answer"] = answer + "\n\n[unverified] " + verdict["reason"]
                return final
            feedback = (
                "Your previous answer was rejected by the auditor: "
                f"{verdict['reason']}. Do this instead: {verdict.get('followup') or 'produce missing evidence'}"
            )
            continue

        if action != "run":
            feedback = "Invalid action. Reply with one of: run, finish."
            continue

        code = _strip_code(str(decision.get("code", "")))
        title = str(decision.get("title", f"Step {step_no}"))[:120]
        explain = str(decision.get("explain", ""))[:400]
        if not code:
            feedback = "You returned an empty program. Return a complete, runnable program."
            continue
        if len(code) > 20_000:
            feedback = "Program too large. Keep it under 20000 characters."
            continue

        publish({"type": "step.started", "no": step_no, "title": title,
                 "explain": explain, "code": code})

        def on_line(channel: str, line: str, _no: int = step_no) -> None:
            publish({"type": "step.log", "no": _no, "channel": channel, "line": line[:2000]})

        try:
            result = await asyncio.to_thread(
                sandbox.execute, run_id, step_no, code, on_line
            )
        except sandbox.SandboxError as exc:
            publish({"type": "run.error", "message": str(exc)})
            final["answer"] = f"Sandbox unavailable: {exc}"
            final["error"] = True
            return final

        step = {
            "no": step_no,
            "title": title,
            "explain": explain,
            "code": code,
            **{k: result[k] for k in (
                "exit_code", "stdout", "stderr", "duration_ms", "timed_out",
                "oom", "killed_reason", "artifacts", "runtime", "gvisor",
            )},
        }
        steps.append(step)
        db.add_step(run_id, step)
        publish({"type": "step.finished", **step})

        if result["timed_out"]:
            feedback = ("The program was killed by the wall-clock limit. "
                        "Make the next step cheaper and print intermediate results.")
        elif result["oom"]:
            feedback = ("The program was killed by the memory cap. "
                        "Process data in chunks; do not hold large objects in memory.")
        elif result["exit_code"] not in (0, None):
            feedback = "The program exited non-zero. Read stderr and fix that exact error."

    # Budget exhausted: force a final answer from the evidence we already have.
    last = steps[-1] if steps else None
    publish({"type": "run.status", "status": "finalizing"})
    if steps:
        try:
            raw = await inference.chat([
                {"role": "system", "content": FINALIZER_SYSTEM},
                {"role": "user", "content": f"TASK:\n{task}\n\nEVIDENCE:\n{_evidence_block(steps)}"},
            ], temperature=0.1, max_tokens=settings.finalizer_max_tokens)
            decision = extract_json(raw)
            answer = str(decision.get("answer", "")).strip()
            if answer:
                verdict = await _verify(inference, task, answer, steps)
                final.update({
                    "answer": answer,
                    "verified": verdict["sufficient"],
                    "verification": verdict["reason"],
                    "claims": decision.get("claims", []),
                    "budget_exhausted": True,
                })
                publish({"type": "verify", **verdict})
                return final
        except InferenceError:
            pass  # fall through to the honest failure below

    final["answer"] = (
        "Step budget exhausted before the agent produced a final answer. "
        + (f"Last step exit code: {last['exit_code']}." if last else "")
    )
    final["verified"] = False
    final["verification"] = "no finish action within step budget"
    publish({"type": "run.status", "status": "exhausted"})
    return final
