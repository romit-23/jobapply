"""Tests for ``jobapply.agents._structured.invoke_structured``.

The helper exists because OpenAI-compatible endpoints that route to
open-weights models occasionally return malformed structured output —
most often an object wrapped in a single-element list. We exercise the
recovery / retry paths with a fake ``llm`` that lets each test script
the exact sequence of ``invoke`` outcomes (raise vs. return).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from langchain_core.messages import BaseMessage, SystemMessage
from pydantic import BaseModel, ValidationError

from jobapply.agents._structured import invoke_structured


class _Doc(BaseModel):
    header: str = ""
    body: str = ""


class _ScriptedStructured:
    """Stand-in for ``llm.with_structured_output(schema)``.

    Each entry in ``script`` is either:
    - a callable that returns the value (or raises) — used to script
      ``ValidationError`` failures since constructing one directly
      from Pydantic is tedious;
    - a ``_Doc`` instance to return outright;
    - a ``ValidationError`` instance to raise verbatim.
    """

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls: list[list[BaseMessage]] = []

    def invoke(self, messages: list[BaseMessage]) -> Any:
        self.calls.append(list(messages))
        if not self._script:
            raise AssertionError("ScriptedStructured ran out of responses")
        nxt = self._script.pop(0)
        if isinstance(nxt, ValidationError):
            raise nxt
        if isinstance(nxt, BaseException):
            raise nxt
        if isinstance(nxt, Callable):  # type: ignore[arg-type]
            return nxt()
        return nxt


class _FakeLLM:
    def __init__(self, structured: _ScriptedStructured) -> None:
        self._structured = structured

    def with_structured_output(self, _schema: type) -> _ScriptedStructured:
        return self._structured


def _validation_error_for(schema: type[BaseModel], bad_input: Any) -> ValidationError:
    """Build a real :class:`ValidationError` whose ``input`` contains
    ``bad_input`` — letting tests exercise the recover path the same
    way the OpenAI parser does in production."""
    try:
        schema.model_validate(bad_input)
    except ValidationError as e:
        return e
    raise AssertionError("expected ValidationError")


def test_invoke_structured_returns_first_attempt_when_valid() -> None:
    structured = _ScriptedStructured([_Doc(header="hi", body="there")])
    llm = _FakeLLM(structured)

    out = invoke_structured(llm, _Doc, [SystemMessage(content="you are a bot")])

    assert out == _Doc(header="hi", body="there")
    assert len(structured.calls) == 1


def test_invoke_structured_recovers_from_single_element_list() -> None:
    """The most common open-weights mistake: wrapping the requested
    object in a single-element list. We must unwrap it without
    spending another LLM call."""
    err = _validation_error_for(_Doc, [{"header": "h", "body": "b"}])
    structured = _ScriptedStructured([err])
    llm = _FakeLLM(structured)

    out = invoke_structured(llm, _Doc, [SystemMessage(content="x")], max_retries=2)

    assert out == _Doc(header="h", body="b")
    # Only the original call — no retry needed because we recovered
    # locally from the failing input.
    assert len(structured.calls) == 1


def test_invoke_structured_retries_with_corrective_message() -> None:
    """When the failing input can't be unwrapped (random scalar),
    the helper must retry with a corrective system message."""
    err = _validation_error_for(_Doc, [2026.05, 10])
    structured = _ScriptedStructured([err, _Doc(header="recovered", body="ok")])
    llm = _FakeLLM(structured)

    out = invoke_structured(llm, _Doc, [SystemMessage(content="initial")], max_retries=2)

    assert out == _Doc(header="recovered", body="ok")
    assert len(structured.calls) == 2
    # The retry must include both the original system message AND a
    # corrective system message tacked on at the end.
    second_call = structured.calls[1]
    assert len(second_call) >= 2
    assert isinstance(second_call[-1], SystemMessage)
    last_content = str(second_call[-1].content)
    assert "_Doc" in last_content or "schema" in last_content.lower()


def test_invoke_structured_raises_after_exhausting_retries() -> None:
    """When every attempt fails and recovery is impossible, the most
    recent ``ValidationError`` is re-raised so the caller sees the
    real diagnostic instead of a wrapped exception."""
    err1 = _validation_error_for(_Doc, [1, 2])
    err2 = _validation_error_for(_Doc, [3, 4])
    structured = _ScriptedStructured([err1, err2])
    llm = _FakeLLM(structured)

    with pytest.raises(ValidationError):
        invoke_structured(llm, _Doc, [SystemMessage(content="initial")], max_retries=1)

    assert len(structured.calls) == 2


def test_invoke_structured_handles_dict_passthrough() -> None:
    """Some compatible endpoints return a plain ``dict`` instead of
    the validated Pydantic model. The helper should validate it
    before handing back to the caller."""
    structured = _ScriptedStructured([{"header": "h", "body": "b"}])
    llm = _FakeLLM(structured)

    out = invoke_structured(llm, _Doc, [SystemMessage(content="x")])

    assert out == _Doc(header="h", body="b")
