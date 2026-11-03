"""extract_json tolerates model chatter but never invents an object."""
from __future__ import annotations

import pytest

from app.inference import InferenceError, extract_json


def test_plain_json_object():
    assert extract_json('{"action": "run", "title": "x"}') == {"action": "run", "title": "x"}


def test_plain_json_object_with_surrounding_whitespace():
    assert extract_json("\n\n  {\"ok\": true}\n\n") == {"ok": True}


def test_json_fence_with_language_tag():
    raw = '```json\n{"action": "finish", "answer": "done"}\n```'
    assert extract_json(raw) == {"action": "finish", "answer": "done"}


def test_json_fence_without_language_tag():
    raw = '```\n{"a": 1}\n```'
    assert extract_json(raw) == {"a": 1}


def test_json_with_prose_before_and_after():
    raw = 'Sure, here is the plan you asked for:\n{"action": "run", "code": "print(1)"}\nLet me know!'
    assert extract_json(raw) == {"action": "run", "code": "print(1)"}


def test_json_fence_with_prose_after_it():
    raw = 'Here you go:\n```json\n{"action": "run"}\n```\nThat should do it.'
    assert extract_json(raw) == {"action": "run"}


def test_nested_objects_and_braces_inside_strings():
    raw = 'note: {"outer": {"inner": [1, {"deep": 2}]}, "text": "a {fake} brace", "esc": "q\\"x"}'
    assert extract_json(raw) == {
        "outer": {"inner": [1, {"deep": 2}]},
        "text": "a {fake} brace",
        "esc": 'q"x',
    }


def test_first_object_wins_when_several_are_present():
    raw = 'first {"a": 1} second {"b": 2}'
    assert extract_json(raw) == {"a": 1}


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "I could not produce a plan.",
        "[1, 2, 3]",
        '"just a string"',
        '{"a": }',
        "{action: run}",
        '{"a": 1',
        '```json\n{"a": }\n```',
    ],
)
def test_invalid_input_raises_inference_error(raw):
    with pytest.raises(InferenceError):
        extract_json(raw)


def test_inference_error_is_runtime_error():
    assert issubclass(InferenceError, RuntimeError)
