"""A picture in the transcript.

The Warden's message history is Anthropic content-block format throughout — the
same choice `model/providers.py` documents, and the reason a photo needs no new
message type here. An image is a block like any other:

    {"type": "image",
     "source": {"type": "base64", "media_type": "image/png", "data": "<b64>"}}

**Why this module exists at all.** Nothing in Forge could carry one. On the peer
path Mark VI sends history that already contains image blocks and the Anthropic
adapter passes them through untouched — so photos worked there by accident, on
one provider, and nowhere else. Every other path lost them SILENTLY: the
OpenAI-compat translator matched `text` / `tool_use` / `tool_result` and an image
fell through all three arms into nothing, and `render_for_summary` did the same.
The operator sent a screenshot, the model received the sentence without it, and
answered anyway. A dropped image is the one failure this harness is otherwise
built to refuse — an agent that cannot see something must know it cannot, or it
reports on what it imagined.

**Four media types, because that is what the APIs take.** PNG, JPEG, GIF, WebP.
A .bmp or a .heic is rejected here, by name, rather than base64'd into a request
that comes back 400 — the operator can convert it, and the error that says so is
worth more than the round trip.

**The size ceiling is the API's, converted.** Anthropic caps a request's image
payload at 5MB; base64 inflates by 4/3, so 3.75MB of file is the real limit.
Refused before encoding rather than after, so a 20MB photo costs a stat call
instead of 27MB of string building.

No resizing. It would need Pillow, which is not a dependency and would be a
strange one to acquire for this; both providers downscale server-side, and an
operator who wants a specific crop has better tools than a coding agent.
"""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

# Suffix → media type. The intersection of what Anthropic and the OpenAI-compat
# vision endpoints accept, which is also the whole list either one documents.
MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# 5MB of base64 is the API ceiling; a file inflates by 4/3 on the way in.
MAX_IMAGE_BYTES = 3_750_000


class ImageError(Exception):
    """Why this file could not become a block. The message is shown to the
    operator verbatim, so it says what to do rather than what went wrong."""


def is_image_path(path: Path | str) -> bool:
    """Whether the suffix is one we can carry. Cheap enough to call on every
    token of a typed line, which is what the TUI does with it."""
    return Path(path).suffix.lower() in MEDIA_TYPES


def block_from_path(path: Path) -> dict[str, Any]:
    """One image block from a file on disk. Raises ImageError with a readable
    reason — the caller is a person typing, not a model retrying."""
    media_type = MEDIA_TYPES.get(path.suffix.lower())
    if media_type is None:
        kinds = ", ".join(sorted(set(MEDIA_TYPES)))
        raise ImageError(f"{path.name}: {path.suffix or 'no extension'} is not an "
                         f"image type I can send ({kinds}).")
    try:
        size = path.stat().st_size
    except OSError as e:
        raise ImageError(f"{path.name}: {e.strerror or e}.") from e
    if size == 0:
        raise ImageError(f"{path.name} is empty.")
    if size > MAX_IMAGE_BYTES:
        raise ImageError(
            f"{path.name} is {size / 1_000_000:.1f}MB — over the "
            f"{MAX_IMAGE_BYTES / 1_000_000:.2f}MB ceiling the model APIs impose. "
            f"Shrink it and try again.")
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise ImageError(f"{path.name}: {e.strerror or e}.") from e
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type,
                   "data": base64.b64encode(raw).decode("ascii")},
    }


def has_image(messages: list[dict[str, Any]]) -> bool:
    """Whether this transcript carries a picture anywhere.

    Read at model-selection time: an agent pinned to a text-only model has to
    move for the turn that has a photo in it, and the whole transcript is the
    right scope — the image the operator sent three turns ago is still in the
    history being replayed, so a text-only model would still be handed it.
    """
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "image":
                return True
    return False


def describe(block: dict[str, Any]) -> str:
    """A one-line stand-in for a summarizer or a log, which cannot take pixels.

    Says an image was here and roughly how big. It deliberately does not say
    what the image showed: nothing at this layer knows, and a summary that
    invents a caption is worse than one that admits a gap.
    """
    source = block.get("source") or {}
    media_type = source.get("media_type") or "image"
    data = source.get("data") or ""
    if data:
        return f"[{media_type} image, {len(data) * 3 // 4 // 1024}KB — not shown in this summary]"
    return f"[{media_type} image — not shown in this summary]"
