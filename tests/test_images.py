"""A photo, from the line the operator typed to the request the provider gets.

The regression these are really about is silent loss. Before this, an image
block matched none of the arms in either translator and fell out of the request
with nothing raised anywhere — so the assertions that matter most below are the
ones checking a picture is still THERE after a translation, and that the paths
which genuinely cannot carry pixels (the summarizer) say so instead of omitting.
"""
import base64

import pytest

from forge.model.providers import (_translate_message,
                                   _translate_message_responses)
from forge.tui import attach
from forge.warden import images
from forge.warden.compaction import render_for_summary

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _png(tmp_path, name="shot.png"):
    path = tmp_path / name
    path.write_bytes(PNG)
    return path


def _block(tmp_path, name="shot.png"):
    return images.block_from_path(_png(tmp_path, name))


# ── the block itself ─────────────────────────────────────────────────────────
def test_block_is_anthropic_shaped(tmp_path):
    block = _block(tmp_path)
    assert block["type"] == "image"
    assert block["source"]["type"] == "base64"
    assert block["source"]["media_type"] == "image/png"
    assert base64.b64decode(block["source"]["data"]) == PNG


def test_media_type_comes_from_the_suffix(tmp_path):
    for name, expected in (("a.jpg", "image/jpeg"), ("a.jpeg", "image/jpeg"),
                           ("a.gif", "image/gif"), ("a.webp", "image/webp")):
        assert _block(tmp_path, name)["source"]["media_type"] == expected


def test_unsupported_type_is_refused_by_name(tmp_path):
    path = tmp_path / "scan.bmp"
    path.write_bytes(PNG)
    with pytest.raises(images.ImageError) as e:
        images.block_from_path(path)
    # The operator has to be able to act on this: it names the file and the
    # types that would have worked.
    assert "scan.bmp" in str(e.value)
    assert ".png" in str(e.value)


def test_oversize_is_refused_before_encoding(tmp_path):
    path = tmp_path / "huge.png"
    path.write_bytes(b"\x00" * (images.MAX_IMAGE_BYTES + 1))
    with pytest.raises(images.ImageError) as e:
        images.block_from_path(path)
    assert "huge.png" in str(e.value)


def test_empty_file_is_refused(tmp_path):
    path = tmp_path / "empty.png"
    path.write_bytes(b"")
    with pytest.raises(images.ImageError):
        images.block_from_path(path)


def test_has_image_scans_the_whole_transcript(tmp_path):
    block = _block(tmp_path)
    assert not images.has_image([{"role": "user", "content": "hello"}])
    assert not images.has_image([{"role": "user",
                                  "content": [{"type": "text", "text": "hi"}]}])
    # Three turns back still counts: the history is replayed every turn, so a
    # text-only model would still be handed it.
    assert images.has_image([
        {"role": "user", "content": [block, {"type": "text", "text": "what is this"}]},
        {"role": "assistant", "content": "a cat"},
        {"role": "user", "content": "and now?"},
    ])


# ── chat-completions translation (the silent drop) ───────────────────────────
def test_image_survives_translation_to_chat_completions(tmp_path):
    block = _block(tmp_path)
    out = _translate_message({"role": "user",
                              "content": [block, {"type": "text", "text": "what is this?"}]})
    assert len(out) == 1
    parts = out[0]["content"]
    assert isinstance(parts, list), "an image forces the multimodal list form"
    kinds = [p["type"] for p in parts]
    assert kinds == ["image_url", "text"], "images lead, then the question"
    url = parts[0]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == PNG


def test_text_only_message_keeps_its_plain_string(tmp_path):
    """The shape every provider in this family has been receiving. Converting it
    to a one-element list for symmetry is how z.ai and Ollama would find out."""
    out = _translate_message({"role": "user",
                              "content": [{"type": "text", "text": "hello"}]})
    assert out == [{"role": "user", "content": "hello"}]


def test_assistant_image_block_is_not_replayed(tmp_path):
    """No provider here returns image content, and an assistant message carrying
    one could not be sent back."""
    block = _block(tmp_path)
    out = _translate_message({"role": "assistant",
                              "content": [block, {"type": "text", "text": "done"}]})
    assert out == [{"role": "assistant", "content": "done"}]


def test_several_images_all_arrive(tmp_path):
    blocks = [_block(tmp_path, "a.png"), _block(tmp_path, "b.jpg")]
    out = _translate_message({"role": "user",
                              "content": [*blocks, {"type": "text", "text": "diff these"}]})
    kinds = [p["type"] for p in out[0]["content"]]
    assert kinds == ["image_url", "image_url", "text"]


def test_image_alongside_tool_results_keeps_the_ordering(tmp_path):
    """tool_result messages still lead the user turn they arrived with — the
    adjacency the translator exists to preserve is not disturbed by a picture."""
    block = _block(tmp_path)
    out = _translate_message({"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
        block,
        {"type": "text", "text": "and this screenshot"},
    ]})
    assert out[0]["role"] == "tool"
    assert [p["type"] for p in out[1]["content"]] == ["image_url", "text"]


# ── responses API translation ────────────────────────────────────────────────
def test_image_survives_translation_to_responses_api(tmp_path):
    block = _block(tmp_path)
    out = _translate_message_responses(
        {"role": "user", "content": [block, {"type": "text", "text": "what is this?"}]})
    assert len(out) == 1
    parts = out[0]["content"]
    # Same picture, different word: input_image, and the URL is a bare string
    # here rather than the nested {"url": …} chat-completions wants.
    assert parts[0]["type"] == "input_image"
    assert parts[0]["image_url"].startswith("data:image/png;base64,")
    assert parts[1] == {"type": "input_text", "text": "what is this?"}


# ── the summarizer, which cannot take pixels ─────────────────────────────────
def test_summary_records_that_an_image_was_here(tmp_path):
    block = _block(tmp_path)
    rendered = render_for_summary([
        {"role": "user", "content": [block, {"type": "text", "text": "what is this?"}]},
    ])
    assert "image" in rendered.lower()
    assert "what is this?" in rendered
    # It must not invent a caption — nothing at that layer knows what was shown.
    assert "not shown" in rendered


# ── recognising a path in a typed line ───────────────────────────────────────
def test_at_path_is_attached(tmp_path):
    _png(tmp_path)
    found = attach.from_prompt("what is wrong in @shot.png", tmp_path)
    assert found.names == ["shot.png"]
    assert found.blocks[0]["source"]["media_type"] == "image/png"


def test_bare_relative_path_is_attached(tmp_path):
    """Drag-and-drop pastes a path, not an @mention. Requiring the @ would be a
    rule the operator has to know before the feature works."""
    _png(tmp_path)
    found = attach.from_prompt("look at shot.png please", tmp_path)
    assert found.names == ["shot.png"]


def test_quoted_path_with_spaces_is_attached(tmp_path):
    path = tmp_path / "my screen shot.png"
    path.write_bytes(PNG)
    found = attach.from_prompt(f'read "{path}" for me', tmp_path)
    assert found.names == ["my screen shot.png"]


def test_the_typed_text_is_never_rewritten(tmp_path):
    """The filename is how the operator refers to the picture in the next
    sentence. Stripping it reads better and loses that."""
    _png(tmp_path)
    text = "compare @shot.png with the mockup"
    found = attach.from_prompt(text, tmp_path)
    assert found.text == text
    assert found.content[-1] == {"type": "text", "text": text}


def test_a_line_with_no_picture_stays_a_plain_string(tmp_path):
    found = attach.from_prompt("email @bob about the .png pipeline", tmp_path)
    assert found.blocks == []
    assert found.content == "email @bob about the .png pipeline"


def test_image_suffix_that_names_no_file_is_ignored(tmp_path):
    """Three-way test: suffix, resolves, exists. This one fails the third."""
    found = attach.from_prompt("the missing.png never existed", tmp_path)
    assert found.blocks == []
    assert found.notes == []


def test_unreadable_attachment_is_reported_and_the_turn_still_goes(tmp_path):
    big = tmp_path / "huge.png"
    big.write_bytes(b"\x00" * (images.MAX_IMAGE_BYTES + 1))
    found = attach.from_prompt("what is in huge.png", tmp_path)
    assert found.blocks == []
    assert len(found.notes) == 1 and "huge.png" in found.notes[0]
    # The question survives — losing it to one bad path costs more than the path.
    assert found.content == "what is in huge.png"


def test_the_same_file_named_twice_is_attached_once(tmp_path):
    _png(tmp_path)
    found = attach.from_prompt("@shot.png vs shot.png", tmp_path)
    assert len(found.blocks) == 1


def test_attachment_count_is_capped(tmp_path):
    names = [f"s{i}.png" for i in range(attach.MAX_IMAGES + 3)]
    for name in names:
        _png(tmp_path, name)
    found = attach.from_prompt(" ".join(names), tmp_path)
    assert len(found.blocks) == attach.MAX_IMAGES
    assert found.notes and "left out" in found.notes[0]


def test_relative_paths_resolve_against_the_workspace(tmp_path):
    """`forge chat --cwd` means the workspace and the process's cwd are not
    always the same place, and the operator is talking about the workspace."""
    nested = tmp_path / "docs"
    nested.mkdir()
    _png(nested, "diagram.png")
    found = attach.from_prompt("see docs/diagram.png", tmp_path)
    assert found.names == ["diagram.png"]


# ── which model a turn with a picture in it runs on ──────────────────────────
def _session(tmp_path, model_ref=None, messages=None):
    import types

    from forge.agents.registry import AgentRegistry

    cfg = AgentRegistry.load().get("optimus")
    return types.SimpleNamespace(
        cfg=cfg, model_ref=model_ref or cfg.model_ref,
        workspace=tmp_path, messages=list(messages or []))


def test_profile_declares_a_vision_model_rather_than_the_code(tmp_path):
    """Rule 10: model IDs live in profiles. A `supports_vision` flag would need
    a table of which IDs can see, in code, going stale in silence."""
    from forge.agents.registry import AgentRegistry

    cfg = AgentRegistry.load().get("optimus")
    assert cfg.vision_model and cfg.vision_model != cfg.model_ref


def test_repl_routes_an_image_turn_to_the_vision_model(tmp_path):
    from forge.tui.repl import _model_ref_for

    session = _session(tmp_path)
    block = _block(tmp_path)
    pending = [{"role": "user", "content": [block, {"type": "text", "text": "?"}]}]
    assert _model_ref_for(session, pending) == session.cfg.vision_model


def test_repl_leaves_an_ordinary_turn_alone(tmp_path):
    from forge.tui.repl import _model_ref_for

    session = _session(tmp_path)
    pending = [{"role": "user", "content": "no picture here"}]
    assert _model_ref_for(session, pending) == session.cfg.model_ref


def test_an_image_earlier_in_the_session_still_routes(tmp_path):
    """The history is replayed every turn, so the follow-up question about a
    screenshot is still a turn the text-only model would be handed it in."""
    from forge.tui.repl import _model_ref_for

    block = _block(tmp_path)
    session = _session(tmp_path, messages=[
        {"role": "user", "content": [block, {"type": "text", "text": "what is this"}]},
        {"role": "assistant", "content": "a chart"},
    ])
    pending = [{"role": "user", "content": "what does the axis say?"}]
    assert _model_ref_for(session, pending) == session.cfg.vision_model


def test_an_explicit_model_flag_still_wins(tmp_path):
    """`forge chat --model x` was meant. Overruling it silently is the worse
    surprise; the provider refuses the picture out loud instead."""
    from forge.tui.repl import _model_ref_for

    session = _session(tmp_path, model_ref="openai:gpt-5.1")
    pending = [{"role": "user", "content": [_block(tmp_path)]}]
    assert _model_ref_for(session, pending) == "openai:gpt-5.1"


def test_no_vision_model_configured_means_no_swap(tmp_path):
    import types

    from forge.tui.repl import _model_ref_for

    session = _session(tmp_path)
    session.cfg = types.SimpleNamespace(model_ref=session.cfg.model_ref, vision_model="")
    session.model_ref = session.cfg.model_ref
    pending = [{"role": "user", "content": [_block(tmp_path)]}]
    assert _model_ref_for(session, pending) == session.cfg.model_ref


def test_peer_job_announces_the_swap(tmp_path):
    """Mark VI sends history that already contains image blocks. The swap must
    be visible — the status line says one model and the turn used another."""
    import asyncio

    from forge.agents.registry import AgentRegistry
    from forge.config import ForgeSettings
    from forge.gate.protocol import JobRequest
    from forge.gate.runner import run_job
    from forge.model.scripted import ScriptedModel

    events = []

    async def _sink(ev):
        events.append(ev)

    request = JobRequest(agent="optimus", task="what is this?", history=[
        {"role": "user", "content": [_block(tmp_path), {"type": "text", "text": "what is this?"}]},
    ])
    asyncio.run(run_job(request, settings=ForgeSettings.from_env(),
                        registry=AgentRegistry.load(), emit=_sink,
                        model=ScriptedModel([lambda _m: ("a cat", [])])))
    chunks = [e.data for e in events if e.type == "chunk" and isinstance(e.data, str)]
    assert any("image in this turn" in c for c in chunks), chunks
