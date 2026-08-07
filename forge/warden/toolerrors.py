"""Validation failures written in the tool's vocabulary, not Pydantic's.

The harness is built on the assumption that the model gets tool calls wrong —
that is what schema validation is FOR. So the message a rejected call earns is
not an incident report, it is the next call's instructions.

What the model used to receive:

    Invalid input for 'read_file': 1 validation error for ReadFileArgs  path
      Field required [type=missing, input_value={}, input_type=dict]
      For furth…

Three facts in there are useless to a language model: `ReadFileArgs` is a class
it has never seen, `type=missing` is a Pydantic error code, and the trailing URL
is documentation it cannot open. Nowhere does the text say what the tool wants.
A weaker model has to infer the correct call from implementation detail, and the
likeliest inference is to send the same shape again.

This matters more here than in most harnesses because of a provider behaviour we
have to live with: **DeepSeek and Gemini ignore a tool schema's `required`.** A
missing argument is not an edge case on this deployment, it is the ordinary
failure, and the message that greets it should teach rather than merely refuse.

So a failure is sorted into the three kinds a caller can actually act on —
missing, mistyped, unexpected — one sentence each, followed by the tool's
signature so the correct call is on the page rather than inferable from it.
Anything that fits none of the three keeps Pydantic's own sentence, which is
usually serviceable ("Input should be greater than or equal to 1"); the raw
error is a fallback, never the whole message.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ValidationError

# How much of a rejected value to quote back. Enough to recognise which argument
# is meant, never enough for a pasted file to become the error message.
_MAX_VALUE_CHARS = 80

# Pydantic names types after Python; the model wrote JSON. Answering in JSON's
# vocabulary is the difference between "fix this" and "translate this, then fix
# it" — `dict` is not what the schema it was given calls an object.
_JSON_TYPE = {
    "string": "string", "str": "string",
    "int": "integer", "float": "number", "decimal": "number",
    "bool": "boolean",
    "list": "array", "tuple": "array", "set": "array", "frozenset": "array",
    "dict": "object", "model": "object", "dataclass": "object",
}


def _a(noun: str) -> str:
    """`a string`, `an integer`. Small, and worth it: a message that reads as
    though nobody proofread it is a message that reads as though nobody meant
    it, and this one is asking to be taken seriously enough to act on."""
    return f"{'an' if noun[:1].lower() in 'aeiou' else 'a'} {noun}"


def _readable(value: Any) -> str:
    """A rejected value, short enough to sit inside a sentence."""
    text = repr(value)
    if len(text) > _MAX_VALUE_CHARS:
        text = text[:_MAX_VALUE_CHARS] + "…"
    return text


def _json_type_of(value: Any) -> str:
    """What the model actually sent, named the way the schema names it."""
    if isinstance(value, bool):
        return "boolean"          # before int: bool is an int in Python, not in JSON
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if value is None:
        return "null"
    return type(value).__name__


def _field_path(loc: tuple[Any, ...]) -> str:
    """`('items', 0, 'name')` → `items[0].name`. Nested arguments are rare but
    the one place a bare leaf name would point at the wrong thing entirely."""
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        elif out:
            out += f".{part}"
        else:
            out = str(part)
    return out or "(the argument object)"


def _sentence(err: dict[str, Any]) -> str:
    """One validation error, as something to do about it."""
    kind = str(err.get("type", ""))
    where = _field_path(tuple(err.get("loc") or ()))

    if kind == "missing":
        return f"The required parameter `{where}` is missing."

    if kind in ("extra_forbidden", "unexpected_keyword_argument"):
        return f"An unexpected parameter `{where}` was provided."

    # Wrong type, and the two spellings Pydantic uses for it: `string_type` is
    # "this was not a string at all", `int_parsing` is "this was a string that
    # does not read as an integer". Both are the same mistake to the caller.
    if kind.endswith(("_type", "_parsing")):
        expected = _JSON_TYPE.get(kind.rsplit("_", 1)[0], kind.rsplit("_", 1)[0])
        got = _json_type_of(err.get("input"))
        return (f"The parameter `{where}` must be {_a(expected)}, but "
                f"{_a(got)} was provided ({_readable(err.get('input'))}).")

    # Everything else — bounds, enums, patterns. Pydantic's own sentence is
    # written for a human and reads fine; only the framing around it changes.
    return f"The parameter `{where}` is not acceptable: {err.get('msg', 'invalid')}."


def signature(args_model: type[BaseModel]) -> str:
    """The call the tool would have accepted, on one line.

    Deliberately names and types only, no descriptions: the model was already
    given the full schema, and repeating it here would spend a hundred tokens
    restating what it can see to fix what it got wrong. What it evidently does
    NOT have to hand at the moment of failure is the shape — which arguments
    exist and which are compulsory."""
    try:
        schema = args_model.model_json_schema()
    except Exception:  # noqa: BLE001 — a schema that will not render is not worth raising over
        return ""
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or ())
    if not properties:
        return ""
    parts = [f"{name} ({_declared_type(spec)}, "
             f"{'required' if name in required else 'optional'})"
             for name, spec in properties.items()]
    return ", ".join(parts)


def _declared_type(spec: dict[str, Any]) -> str:
    """The JSON type a property declares, including the `X | None` shape Pydantic
    emits as an anyOf — where "integer or null" is a worse answer than
    "integer", because the null branch is what `optional` already said."""
    declared = spec.get("type")
    if isinstance(declared, str):
        return declared
    branches = spec.get("anyOf") or spec.get("oneOf") or []
    named = [b.get("type") for b in branches
             if isinstance(b, dict) and isinstance(b.get("type"), str)
             and b.get("type") != "null"]
    if named:
        return " or ".join(dict.fromkeys(named))
    if spec.get("enum"):
        return "one of " + ", ".join(repr(v) for v in spec["enum"])
    return "value"


def format_validation_error(
    tool_name: str, error: ValidationError, args_model: type[BaseModel] | None = None
) -> str:
    """Turn a rejected tool call into instructions for the next one."""
    try:
        errors = list(error.errors())
    except Exception:  # noqa: BLE001 — never fail while reporting a failure
        errors = []

    if not errors:
        # No structure to work with. The raw text is still better than silence,
        # and this is the one branch where it is the whole message.
        return f"{tool_name} failed: its arguments did not match the schema.\n\n{error}"

    plural = "issue" if len(errors) == 1 else "issues"
    lines = [f"{tool_name} failed due to the following {plural} with its arguments:", ""]
    lines += [f"  - {_sentence(e)}" for e in errors]

    if args_model is not None and (sig := signature(args_model)):
        lines += ["", f"{tool_name} takes: {sig}."]

    lines += ["", ("Nothing ran. Call it again with the arguments corrected — an "
                   "identical retry will fail identically.")]
    return "\n".join(lines)
