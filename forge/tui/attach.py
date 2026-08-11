"""Getting a photo into a typed line.

The terminal has no drop target and no file picker, so the only thing an
operator can hand a turn is text. What they actually do is one of two things:
type `@` and let completion finish the filename, or drag the file onto the
window — which every terminal worth using pastes as a path, quoted when it has
a space in it. Both arrive here as ordinary characters in the prompt, and until
this module existed both meant nothing: the model was told a filename and given
no way to open it, since `read_file` is UTF-8 and a PNG is not.

**Recognition is deliberately narrow.** A token counts as a picture only if it
survives all three of: an image suffix, a path that resolves, and a file that
exists. `@` is honoured but not required — requiring it would break drag-and-
drop, which is the gesture people actually reach for, and demanding it of a
pasted `C:\\Users\\…\\shot.png` would be a rule the operator has to know before
the feature works at all. The three-way test is what keeps "email me @bob" and
"the .png pipeline" from being read as attachments: neither resolves to a file.

**The text is never rewritten.** An earlier draft stripped the matched token out
of the sentence, which reads better and is wrong: the filename is information —
it is how the operator refers to the picture in the next sentence, and how the
agent names it back. The path stays where it was typed and the image rides
alongside it.

**A file that cannot be read is said out loud, and the turn still goes.** The
alternative is refusing the whole prompt over one bad path, which loses the
question that was typed with it. The operator sees the reason and can decide
whether the answer without it is worth having.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forge.warden import images

# Candidate tokens: a quoted run (drag-and-drop of a path with spaces), or an
# unquoted run of non-space characters. `@` is stripped after matching so a
# completed `@path` and a pasted path take the same route.
_TOKEN = re.compile(r'"([^"]+)"|\'([^\']+)\'|(\S+)')

# How many pictures one turn may carry. Not an API limit — a guard against a
# glob-ish paste turning into thirty base64 payloads and a context window spent
# before the question is read.
MAX_IMAGES = 8


@dataclass
class Attached:
    """What a typed line turned into."""
    text: str
    blocks: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)   # shown to the operator
    names: list[str] = field(default_factory=list)   # what was attached, for the echo

    @property
    def content(self) -> Any:
        """The `content` field for the user message.

        A line with no picture in it stays a plain string — the shape every
        other turn in this transcript has, and not worth converting to a
        one-element block list for symmetry's sake. Images lead the list when
        there are any; both providers ground better that way.
        """
        if not self.blocks:
            return self.text
        parts: list[dict[str, Any]] = [*self.blocks]
        if self.text:
            parts.append({"type": "text", "text": self.text})
        return parts


def _candidates(text: str) -> list[str]:
    """Every token that could name a file, in the order typed."""
    out: list[str] = []
    for match in _TOKEN.finditer(text):
        token = next(g for g in match.groups() if g is not None)
        token = token.lstrip("@").strip().strip(",;")
        if token:
            out.append(token)
    return out


def _resolve(token: str, workspace: Path) -> Path | None:
    """A token → the file it names, or None.

    Relative paths are read against the workspace rather than the process's
    working directory: the operator is talking about the repository they opened
    Forge in, and `forge chat --cwd` means those are not always the same place.
    """
    try:
        candidate = Path(token).expanduser()
        resolved = candidate if candidate.is_absolute() else (workspace / candidate)
        return resolved if resolved.is_file() else None
    except (OSError, ValueError):
        # A token that is not a well-formed path on this platform — long, or
        # carrying characters Windows refuses. Not an attachment; not an error.
        return None


def from_prompt(text: str, workspace: Path) -> Attached:
    """Split a typed line into its text and any pictures it named."""
    attached = Attached(text=text)
    seen: set[Path] = set()

    for token in _candidates(text):
        if not images.is_image_path(token):
            continue
        path = _resolve(token, workspace)
        if path is None or path in seen:
            continue
        seen.add(path)
        if len(attached.blocks) >= MAX_IMAGES:
            attached.notes.append(
                f"only the first {MAX_IMAGES} images were attached — {path.name} "
                f"and anything after it were left out")
            break
        try:
            attached.blocks.append(images.block_from_path(path))
        except images.ImageError as e:
            attached.notes.append(str(e))
            continue
        attached.names.append(path.name)

    return attached
