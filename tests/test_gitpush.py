"""git_push keeps the credential out of the Cell and refuses to be redirected.

The tool runs `git push` host-side so the token never enters the sandbox, but the
repo it pushes is controlled by the sandbox — so most of these tests are about
the ways a hostile repo could try to steal the token, and that each is refused:
a non-github remote, an insteadOf rewrite, a pre-push hook. The happy path is
exercised against a local bare repo (a file:// remote needs no credential), which
is why `_github_https` is monkeypatched there — the github-only guard is itself
covered by its own test.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

import forge.tools.gitpush as gp
from forge.tools.gitpush import GitPush, _github_https, configured
from forge.warden.filestate import FileStateCache
from forge.warden.permissions import PermissionEngine
from forge.warden.tool import ToolContext


# ── URL validation (pure) ────────────────────────────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("https://github.com/o/r.git", "https://github.com/o/r.git"),
    ("https://github.com/o/r", "https://github.com/o/r.git"),
    ("git@github.com:o/r.git", "https://github.com/o/r.git"),
    ("ssh://git@github.com/o/r.git", "https://github.com/o/r.git"),
    ("https://gitlab.com/o/r.git", None),
    ("https://evil.com/o/r.git", None),
    ("https://github.com.evil.com/o/r.git", None),   # host suffix trick
    ("https://github.com/o", None),                  # not owner/repo
    ("/local/path/repo.git", None),
    ("", None),
])
def test_github_https_validation(url, expected):
    assert _github_https(url) == expected


def test_hardening_flags_are_present():
    """Running git host-side on a Cell-controlled repo means the repo's config
    could execute commands on the host unless neutralised. These -c overrides are
    that defence; a regression that drops one reopens a host-RCE vector, so pin
    them here."""
    flat = " ".join(gp._HARDEN)
    assert "core.fsmonitor=false" in flat      # the main command-exec vector
    assert "core.hooksPath=/dev/null" in flat  # no hook runs as the host user
    assert "protocol.ext.allow=never" in flat  # no ext:: transport


def test_configured_reads_the_token(monkeypatch):
    monkeypatch.delenv("FORGE_GIT_TOKEN", raising=False)
    monkeypatch.delenv("FORGE_GIT_TOKEN_OPTIMUS", raising=False)
    assert configured() is False
    monkeypatch.setenv("FORGE_GIT_TOKEN", "ghp_x")
    assert configured() is True


def test_per_agent_token_beats_the_generic(monkeypatch):
    monkeypatch.setenv("FORGE_GIT_TOKEN", "generic")
    monkeypatch.setenv("FORGE_GIT_TOKEN_OPTIMUS", "specific")
    assert gp._token("optimus") == "specific"
    assert gp._token("centurion") == "generic"


# ── Harness for the async tool against real git repos ────────────────────────

def _run(cmd, cwd, env=None):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, env=env)


class _Cell:
    """Minimal Cell: git_push only needs host_path."""
    def __init__(self, host_path: Path):
        self._hp = host_path

    @property
    def host_path(self):
        return self._hp


def _ctx(host_path: Path) -> ToolContext:
    return ToolContext(agent_id="optimus", cell=_Cell(host_path), graph=None,
                       files=FileStateCache(), permissions=PermissionEngine(),
                       network_allowed=True)


def _init_repo(path: Path, origin: str | None = None) -> None:
    path.mkdir(parents=True, exist_ok=True)
    env = {"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e",
           "PATH": __import__("os").environ.get("PATH", "")}
    _run(["git", "init", "-b", "main"], path)
    (path / "f.txt").write_text("hello", encoding="utf-8")
    _run(["git", "add", "."], path)
    _run(["git", "commit", "-m", "init"], path, env=env)
    if origin is not None:
        _run(["git", "remote", "add", "origin", origin], path)


def _call(ctx, **kw):
    args = GitPush.Args(**kw)
    return asyncio.run(GitPush().call(args, ctx))


# ── No credential ────────────────────────────────────────────────────────────

def test_refused_without_a_token(tmp_path, monkeypatch):
    monkeypatch.delenv("FORGE_GIT_TOKEN", raising=False)
    monkeypatch.delenv("FORGE_GIT_TOKEN_OPTIMUS", raising=False)
    _init_repo(tmp_path, origin="https://github.com/o/r.git")
    res = _call(_ctx(tmp_path), path=".")
    assert res.is_error
    assert "not configured" in res.content


# ── Security refusals (real repos) ───────────────────────────────────────────

def test_refuses_a_non_github_remote(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_GIT_TOKEN", "ghp_x")
    _init_repo(tmp_path, origin="https://gitlab.com/o/r.git")
    res = _call(_ctx(tmp_path), path=".")
    assert res.is_error
    assert "not a github.com remote" in res.content


def test_refuses_an_insteadof_rewrite(tmp_path, monkeypatch):
    """An insteadOf rewrite that would redirect the validated github URL must be
    refused. Two defences cover this: `git remote get-url` applies the rewrite on
    some git versions (so the URL check sees the evil host and refuses), and on
    versions where it does not, the explicit insteadOf check fires. Either way
    the invariant holds — refused, and the token is never sent to evil.example."""
    monkeypatch.setenv("FORGE_GIT_TOKEN", "ghp_x")
    _init_repo(tmp_path, origin="https://github.com/o/r.git")
    _run(["git", "config", "url.https://evil.example/.insteadOf",
          "https://github.com/"], tmp_path)
    res = _call(_ctx(tmp_path), path=".")
    assert res.is_error
    assert ("insteadOf" in res.content) or ("not a github.com remote" in res.content)
    assert "evil.example" not in res.content or "not a github.com remote" in res.content


def test_refuses_a_path_escape(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_GIT_TOKEN", "ghp_x")
    _init_repo(tmp_path, origin="https://github.com/o/r.git")
    res = _call(_ctx(tmp_path), path="../outside")
    assert res.is_error
    assert "escapes the workspace" in res.content


def test_errors_when_not_a_git_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_GIT_TOKEN", "ghp_x")
    res = _call(_ctx(tmp_path), path=".")
    assert res.is_error
    assert "not a git repository" in res.content


# ── Happy path + hook defence (real push to a local bare repo) ───────────────

def test_pushes_to_the_validated_url(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_GIT_TOKEN", "ghp_x")
    bare = tmp_path / "remote.git"
    _run(["git", "init", "--bare", "-b", "main", str(bare)], tmp_path)
    repo = tmp_path / "work"
    _init_repo(repo, origin="https://github.com/o/r.git")
    # The github-only guard is covered elsewhere; here we redirect the validated
    # URL to the local bare repo so the actual push mechanics run without needing
    # a real remote or a credential (file:// ignores the helper).
    monkeypatch.setattr(gp, "_github_https", lambda url: bare.as_uri())

    res = _call(_ctx(repo), path=".")
    assert not res.is_error, res.content
    # The commit is actually on the remote now.
    out = subprocess.run(["git", "log", "-1", "--format=%s", "main"],
                         cwd=bare, capture_output=True, text=True)
    assert out.stdout.strip() == "init"


def test_a_pre_push_hook_does_not_run(tmp_path, monkeypatch):
    """A hostile repo's pre-push hook must not execute host-side with the token
    in its env — it is the clearest token-exfiltration path. The push disables
    hooks, so the hook's side effect must be absent and the push must still
    succeed."""
    monkeypatch.setenv("FORGE_GIT_TOKEN", "ghp_x")
    bare = tmp_path / "remote.git"
    _run(["git", "init", "--bare", "-b", "main", str(bare)], tmp_path)
    repo = tmp_path / "work"
    _init_repo(repo, origin="https://github.com/o/r.git")

    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    marker = tmp_path / "hook_ran"
    hook = hooks / "pre-push"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    hook.chmod(0o755)

    monkeypatch.setattr(gp, "_github_https", lambda url: bare.as_uri())
    res = _call(_ctx(repo), path=".")

    assert not res.is_error, res.content
    assert not marker.exists(), "pre-push hook ran despite hooks being disabled"
