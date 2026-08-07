"""Validation errors that teach the correct call.

The premise these tests protect is that the harness assumes the model gets tool
calls wrong, so a rejected call is answered with the call it should have made.
Every assertion here is about what the MODEL can do with the text, which is why
they check for absent implementation detail as often as for present advice.
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel, Field, ValidationError

from forge.warden.toolerrors import format_validation_error, signature


class Args(BaseModel):
    path: str = Field(description="Where to read from.")
    offset: int | None = Field(default=None, ge=1)
    limit: int | None = None
    recursive: bool = False


def _error(payload: dict) -> ValidationError:
    with pytest.raises(ValidationError) as caught:
        Args.model_validate(payload)
    return caught.value


def _message(payload: dict) -> str:
    return format_validation_error("read_file", _error(payload), Args)


# ── the three kinds a caller can act on ──────────────────────────────────────


def test_a_missing_parameter_is_named_in_the_tools_own_vocabulary():
    """The ordinary failure on this deployment: DeepSeek and Gemini ignore a
    schema's `required`, so a missing argument is not an edge case."""
    text = _message({})
    assert "The required parameter `path` is missing." in text


def test_a_wrong_type_says_what_was_wanted_and_what_arrived():
    text = _message({"path": "a.py", "limit": "ten"})
    assert "`limit` must be an integer" in text
    assert "a string was provided" in text
    assert "'ten'" in text


def test_json_types_are_named_the_way_the_schema_names_them():
    """Pydantic says `dict`; the model was handed a schema that says `object`,
    and answering in the other vocabulary makes it translate before it can fix."""
    text = _message({"path": {"a": 1}})
    assert "an object was provided" in text
    assert "dict" not in text


def test_a_boolean_is_not_reported_as_an_integer():
    """bool is an int in Python and is not in JSON. Getting this backwards
    would tell the model its `true` was a number."""
    text = _message({"path": True})
    assert "a boolean was provided" in text


def test_a_constraint_failure_keeps_pydantics_sentence():
    """Bounds and enums already read well; only the framing changes. The
    fallback exists so an unrecognised error class degrades to serviceable
    rather than to nothing."""
    text = _message({"path": "a.py", "offset": 0})
    assert "`offset`" in text
    assert "greater than or equal to 1" in text


def test_several_problems_are_all_reported_at_once():
    """One call, one round trip. Reporting the first and discovering the second
    on the retry costs a turn per mistake."""
    text = _message({"limit": "ten"})
    assert "`path` is missing" in text
    assert "`limit`" in text
    assert text.count("  - ") == 2


# ── the signature, which is the half that says what to do ────────────────────


def test_the_message_states_the_call_the_tool_would_have_accepted():
    text = _message({})
    assert "read_file takes:" in text
    assert "path (string, required)" in text
    assert "offset (integer, optional)" in text


def test_an_optional_type_is_not_reported_as_or_null():
    """`int | None` renders as an anyOf. "integer or null" is a worse answer
    than "integer", because `optional` already said the null part."""
    assert "null" not in signature(Args)


def test_the_signature_omits_descriptions():
    """The model already has the full schema. Restating it here spends tokens
    to tell it what it can see; what it lacks at the moment of failure is the
    shape."""
    assert "Where to read from" not in signature(Args)


# ── what is deliberately absent ──────────────────────────────────────────────


def test_no_pydantic_internals_reach_the_model():
    """The old message named a class the model has never seen, a Pydantic error
    code, and trailed off mid-sentence into a URL it cannot open."""
    text = _message({})
    for leak in ("ValidationError", "type=missing", "input_type=", "pydantic.dev",
                 "For further information"):
        assert leak not in text


def test_it_says_that_an_identical_retry_is_pointless():
    """The likeliest wrong inference from a bare rejection is to send the same
    shape again."""
    text = _message({})
    assert "identical retry will fail identically" in text


def test_a_nested_argument_points_at_the_right_leaf():
    class Nested(BaseModel):
        class Item(BaseModel):
            name: str
        items: list[Item]

    with pytest.raises(ValidationError) as caught:
        Nested.model_validate({"items": [{}]})
    text = format_validation_error("batch", caught.value, Nested)
    assert "`items[0].name` is missing" in text


def test_reporting_a_failure_never_fails():
    """A formatter that raises while explaining a rejection would turn a
    correctable mistake into a crash."""
    class Broken(BaseModel):
        x: int

        @classmethod
        def model_json_schema(cls, *args, **kwargs):
            raise RuntimeError("no schema for you")

    with pytest.raises(ValidationError) as caught:
        Broken.model_validate({})
    text = format_validation_error("broken", caught.value, Broken)
    assert "`x` is missing" in text        # the sentence survived
    assert "broken takes:" not in text     # the signature was simply skipped
