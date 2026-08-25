"""`git_push` and `open_pr` — publishing the work, with the credential kept out
of the Cell.

Optimus does all its git work INSIDE the Cell: clone, branch, add, commit — none
of which need a secret, because the commit identity rides in on env vars
(`cfg.git.env()`) and everything else is local. Only the two calls that reach
GitHub — pushing the branch, then opening the PR that gets it reviewed — need a
credential, and those are the two operations this module takes Warden-side. They
share one token (a push-capable PAT that also carries `pull_requests: write`
opens both doors with one secret to place and one to rotate) and the same
remote-resolution safety checks, so `open_pr` cannot be pointed anywhere
`git_push` itself would refuse.

**Why not just push from the Cell.** The Cell runs model-written code as root
with the network on. A token placed in its environment is readable by that code
and by anything in a repo it was told to work on (a malicious `.git/config`, a
pre-push hook, a prompt-injecting README that talks the model into `cat`-ing its
env). That is the exact reason `web.py` and `hisar.py` are Warden-side and their
comments say a credential "must not be reachable from generated code that
happens to be running with network on." Push is the same class of secret.

**But the repo is still hostile.** Running `git push` host-side keeps the token
out of the Cell, yet the repo it pushes lives in the Cell's workspace and the
Cell controls its `.git/config` and hooks. A naive host-side push would hand the
Cell four ways to steal the token it was denied:

  * a rewritten `remote.origin.url` pointing at an attacker → the token is sent
    there. Defeated by resolving the URL and REQUIRING https://github.com.
  * a `url.<evil>.insteadOf = https://github.com/` rewrite that redirects even a
    URL we validated. Defeated by refusing to push when any such rewrite exists.
  * a `credential.helper` in the repo config that runs an arbitrary command when
    git asks for the password. Defeated by resetting the helper list on the
    command line before adding ours.
  * a `pre-push` hook that runs host-side, as us, with the token in its env.
    Defeated by disabling hooks (`core.hooksPath` to nothing + `--no-verify`).

The token reaches only the push subprocess's env — never the Cell, and never the
git subprocess's env alongside the model-provider keys (the subprocess gets a
MINIMAL env, not the peer's whole environment).

Withheld entirely when no token is configured, like the vault tools: an agent is
never offered a push it has no key for.
"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from forge.warden.tool import Tool, ToolContext, ToolResult

_TOKEN_ENV = "FORGE_GIT_TOKEN"
_GIT_TIMEOUT_S = 120.0
_API_TIMEOUT_S = 30.0
_API_BASE = "https://api.github.com"

# The credential helper handed to git on the command line: it prints the token
# from the subprocess env when git asks for github.com's password. Kept out of
# the URL (which would land in error text and the reflog) and off disk.
_CRED_HELPER = ('!f() { echo username=x-access-token; '
                'echo "password=$%s"; }; f' % _TOKEN_ENV)

# Running git host-side against a repo the Cell controls means the repo's own
# config could otherwise execute commands ON THE HOST (as the peer, root) during
# ANY git call — a host RCE, worse than token theft. Several git config keys run
# a shell command; neutralise the ones reachable by the subcommands this tool
# runs, on EVERY call, before the repo's config can supply its own value
# (command-line -c wins). fsmonitor is the dangerous one (fires on index/ref
# work); the transport/proxy/pager entries close the smaller vectors. Hooks are
# disabled here too, not only on push. Builtins (config/remote/rev-parse/push)
# cannot be shadowed by aliases, so alias.* is not a vector.
_HARDEN = [
    "-c", "core.fsmonitor=false",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.pager=cat",
    "-c", "core.gitProxy=",
    "-c", "protocol.ext.allow=never",
]


def _token(agent_id: str = "") -> str:
    """The push token for this agent, or "". Per-agent first (so one deployment
    can arm exactly one agent — the peer loads `.env.<agent>`), then a shared
    fallback. Read from the PEER's environment; it never enters a Cell."""
    if agent_id:
        specific = os.environ.get(
            f"{_TOKEN_ENV}_{agent_id.upper().replace('-', '_')}", "")
        if specific.strip():
            return specific.strip()
    return os.environ.get(_TOKEN_ENV, "").strip()


def configured(agent_id: str = "") -> bool:
    """Whether this agent has a push credential. Read by the tool source, which
    withholds the tool when it is false."""
    return bool(_token(agent_id))


def _github_https(url: str) -> str | None:
    """The canonical https://github.com/owner/repo(.git) for `url`, or None if it
    is not a GitHub remote. Accepts the https and ssh spellings GitHub hands out;
    everything else (another host, a local path, a redirect target) returns None
    so the caller refuses rather than sending the token somewhere it does not
    belong."""
    url = url.strip()
    # scp-like ssh form: git@github.com:owner/repo.git
    m = re.match(r"^git@github\.com:(?P<path>[^/].*)$", url)
    if m:
        path = m.group("path")
    else:
        p = urlparse(url)
        if p.scheme not in ("https", "ssh"):
            return None
        host = (p.hostname or "").lower()
        if host != "github.com":
            return None
        path = p.path.lstrip("/")
    path = path.removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", path):
        return None
    return f"https://github.com/{path}.git"


def _owner_repo(push_url: str) -> tuple[str, str]:
    """Split a `_github_https` URL back into (owner, repo). Only ever called on
    a URL that already passed that validation, so the shape is guaranteed."""
    path = push_url.removeprefix("https://github.com/").removesuffix(".git")
    owner, repo = path.split("/", 1)
    return owner, repo


async def _resolve_repo(ctx: ToolContext, path: str) -> tuple[Path | None, str | None]:
    """(repo, None) on success, or (None, error message) — the containment and
    "is this even a repo" checks `git_push` and `open_pr` both need before doing
    anything else. Shared so the two cannot drift on what counts as a valid,
    in-bounds repository directory."""
    cell = getattr(ctx, "cell", None)
    host_root = getattr(cell, "host_path", None) if cell is not None else None
    if host_root is None:
        return None, (
            "This workspace has no host-visible path, so this cannot run "
            "outside the sandbox. (An ephemeral Cell — deposit the work or "
            "run this from a workspace-backed run instead.)")
    root = Path(host_root).resolve()
    repo = (root / path).resolve()
    # Containment: the model names the path, so it must not escape the
    # workspace the same way the Cell's own read/write guard enforces.
    if os.path.commonpath([str(repo), str(root)]) != str(root):
        return None, f"path escapes the workspace: {path!r}"
    if not (repo / ".git").exists():
        return None, f"{path!r} is not a git repository."
    return repo, None


async def _resolve_remote(repo: Path, remote: str) -> tuple[str | None, str | None]:
    """(push_url, None) on success, or (None, error message). Reads the remote
    WITHOUT the token (no credential needed to inspect config) and applies the
    same two checks `git_push`'s security posture rests on: the URL must
    resolve to github.com, and the repo must not carry an `insteadOf` rewrite
    that could redirect a validated URL out from under it after the fact.
    `open_pr` shares this so it can never reach a repo `git_push` would refuse."""
    code, out, err = await _git(repo, ["remote", "get-url", remote])
    if code != 0:
        return None, (
            f"No remote {remote!r} in this repository ({err.strip() or 'not found'}). "
            f"Add it with `git remote add {remote} https://github.com/<owner>/<repo>.git` "
            f"first.")
    push_url = _github_https(out.strip())
    if push_url is None:
        return None, (
            f"Refused: remote {remote!r} is {out.strip()!r}, which is not a "
            "github.com remote. The credential is scoped to GitHub and will "
            "not be sent anywhere else.")
    code, rewrites, _ = await _git(
        repo, ["config", "--get-regexp", r"^url\..*\.insteadof$"])
    if code == 0 and rewrites.strip():
        return None, (
            "Refused: this repository configures url.*.insteadOf rewrites, "
            "which can redirect this call away from the address just "
            "verified. Remove them first.")
    return push_url, None


async def _git(repo: Path, git_args: list[str], token: str | None = None
               ) -> tuple[int, str, str]:
    """Run one git command host-side in `repo`. A MINIMAL env — never the
    peer's full environment — so the model-provider keys the Cell is denied
    do not reach a git subprocess either; the push token is added only when
    this call is the push itself."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/root"),
        "GIT_TERMINAL_PROMPT": "0",   # never block waiting on a tty for creds
        # Ignore the operator's own global/system git config entirely — only
        # the repo's config (which we harden below) and our -c flags apply.
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }
    if token is not None:
        env[_TOKEN_ENV] = token
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(repo), *_HARDEN, *git_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=_GIT_TIMEOUT_S)
    except FileNotFoundError:
        return 127, "", "git is not installed on the host."
    except asyncio.TimeoutError:
        return 124, "", f"git timed out after {int(_GIT_TIMEOUT_S)}s."
    return (proc.returncode or 0,
            out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))


def _scrub(text: str) -> str:
    """Defence in depth: keep the credential-helper snippet (which names the
    token env var, not its value) out of anything shown to the model."""
    return text.replace(_CRED_HELPER, "<credential-helper>")


def _client():
    """The httpx module, or None when it is not installed. Mirrors
    forge/tools/hisar.py's `_client()` — same reasoning, same shape: a
    networked tool degrades to "unavailable", it does not crash the turn."""
    try:
        import httpx
    except ImportError:
        return None
    return httpx


def _explain_api(status: int, body: str) -> str:
    """Turn a GitHub API failure into something with a next step in it, the
    same job hisar.py's `_explain` does for the vault."""
    detail = body[:300].strip()
    if status == 401:
        return (f"GitHub rejected the credential ({_TOKEN_ENV}). Operator "
                f"setup — do not retry. {detail}")
    if status == 403:
        return (f"GitHub refused: {detail or 'forbidden'}. The token may lack "
                f"the `pull_requests: write` scope for this repository, or has "
                f"hit a rate limit. Do not retry this call unchanged.")
    if status == 404:
        return (f"GitHub has no such repository, or this token cannot see it: "
                f"{detail or 'not found'}.")
    if status == 422:
        return (f"GitHub refused the request: {detail or 'unprocessable'}. This "
                f"usually means the head branch has no commits ahead of base, "
                f"or a pull request between them already exists.")
    return f"GitHub returned HTTP {status}: {detail}"


async def _github_api(method: str, path: str, token: str, **kwargs
                      ) -> tuple[dict | None, str]:
    """One GitHub API call. Returns (payload, "") or (None, readable error) —
    the same errors-as-values contract every networked tool here keeps."""
    httpx = _client()
    if httpx is None:
        return None, ("GitHub is unavailable: the `httpx` package is not "
                      "installed in this Forge. Operator setup — do not retry.")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with httpx.AsyncClient(timeout=_API_TIMEOUT_S) as client:
            response = await client.request(
                method, f"{_API_BASE}{path}", headers=headers, **kwargs)
    except Exception as e:  # noqa: BLE001 — unreachable GitHub is a result, not a crash
        return None, f"Could not reach GitHub: {e}"
    if response.status_code >= 400:
        return None, _explain_api(response.status_code, response.text)
    try:
        return response.json(), ""
    except Exception:  # noqa: BLE001
        return None, "GitHub returned a malformed response."


class GitPushArgs(BaseModel):
    path: str = Field(
        default=".",
        description="Repository directory, relative to your workspace. Defaults "
                    "to the workspace itself; pass a subfolder if you cloned into one.")
    branch: str | None = Field(
        default=None,
        description="Branch to push. Defaults to the repository's current branch.")
    remote: str = Field(
        default="origin",
        description="Which remote's URL to push to. Defaults to 'origin'. The URL "
                    "must be a github.com remote.")


class GitPush(Tool):
    name = "git_push"
    description = (
        "Push commits to a GitHub repository. Do all the rest of your git work "
        "normally with run_command — clone, branch, add, commit — then use this "
        "to publish, because the push credential is held by the harness and "
        "never enters your sandbox. Pushes the current branch of the repo in "
        "your workspace to its github.com 'origin' by default. It will refuse a "
        "remote that is not github.com, and it does not force-push or delete "
        "branches. Say in one sentence what you pushed and where."
    )
    Args = GitPushArgs
    READ_ONLY = False
    CONCURRENCY_SAFE = False

    async def call(self, args: GitPushArgs, ctx: ToolContext) -> ToolResult:
        agent_id = getattr(ctx, "agent_id", "") or ""
        token = _token(agent_id)
        if not token:
            return ToolResult(
                f"Push is not configured: no {_TOKEN_ENV} for this agent in the "
                "Forge's environment. This is an operator setup task — do not "
                "retry; commit your work and say it is ready to push.", is_error=True)

        repo, err = await _resolve_repo(ctx, args.path)
        if err is not None:
            return ToolResult(err, is_error=True)

        push_url, err = await _resolve_remote(repo, args.remote)
        if err is not None:
            return ToolResult(err, is_error=True)

        branch = args.branch
        if not branch:
            code, out, _e = await _git(repo, ["rev-parse", "--abbrev-ref", "HEAD"])
            branch = out.strip()
            if code != 0 or not branch or branch == "HEAD":
                return ToolResult(
                    "Could not determine the current branch (detached HEAD?). Pass "
                    "`branch` explicitly.", is_error=True)
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", branch):
            return ToolResult(f"Refused: implausible branch name {branch!r}.", is_error=True)

        # ── The push: token in this subprocess's env only, hooks off, the
        # repo's own credential helpers reset, pushed to the VALIDATED url (not
        # the remote name, so a config change between check and push cannot
        # redirect it). ──
        push_args = [
            "-c", "credential.helper=",            # reset the inherited list…
            "-c", f"credential.helper={_CRED_HELPER}",  # …then only ours
            "push", "--no-verify", push_url,
            f"HEAD:refs/heads/{branch}",
        ]
        code, out, err = await _git(repo, push_args, token=token)
        combined = (out + "\n" + err).strip()
        if code != 0:
            # Never echo the token; git does not print it, but the helper name
            # could appear — scrub defensively.
            return ToolResult(
                f"Push failed:\n{_scrub(combined)}", is_error=True)
        return ToolResult(
            f"Pushed {branch} to {push_url}.\n{_scrub(combined)}".strip())


class OpenPRArgs(BaseModel):
    path: str = Field(
        default=".",
        description="Repository directory, relative to your workspace. Same "
                    "meaning as git_push's `path`.")
    head: str = Field(
        description="Branch to open the PR from. Must already be pushed — run "
                    "git_push on it first, or GitHub will refuse with "
                    "'no commits between' or similar.")
    title: str = Field(description="Pull request title.")
    body: str = Field(
        default="", description="Pull request description. Markdown, as GitHub renders it.")
    base: str | None = Field(
        default=None,
        description="Branch the PR merges into. Defaults to the repository's "
                    "default branch (usually main).")
    remote: str = Field(
        default="origin",
        description="Which remote identifies the repository to open the PR "
                    "against. Defaults to 'origin'. The URL must be a github.com remote.")
    draft: bool = Field(default=False, description="Open as a draft PR.")


class OpenPR(Tool):
    name = "open_pr"
    description = (
        "Open a pull request on GitHub from a branch you already published with "
        "git_push. Uses the same credential as git_push — do your git work, "
        "push the branch, then call this once to open it for review. Refuses a "
        "remote that is not github.com, the same as git_push, so it can never "
        "reach a repository git_push itself would refuse. Returns the PR's URL "
        "on success; say in one sentence what it is and link it."
    )
    Args = OpenPRArgs
    READ_ONLY = False
    CONCURRENCY_SAFE = False

    async def call(self, args: OpenPRArgs, ctx: ToolContext) -> ToolResult:
        agent_id = getattr(ctx, "agent_id", "") or ""
        token = _token(agent_id)
        if not token:
            return ToolResult(
                f"Opening a PR is not configured: no {_TOKEN_ENV} for this agent "
                "in the Forge's environment. This is an operator setup task — do "
                "not retry; push your work and say it is ready for a PR.", is_error=True)

        repo, err = await _resolve_repo(ctx, args.path)
        if err is not None:
            return ToolResult(err, is_error=True)

        push_url, err = await _resolve_remote(repo, args.remote)
        if err is not None:
            return ToolResult(err, is_error=True)
        owner, name = _owner_repo(push_url)

        base = args.base
        if not base:
            payload, err = await _github_api("GET", f"/repos/{owner}/{name}", token)
            if err:
                return ToolResult(err, is_error=True)
            base = payload.get("default_branch")
            if not base:
                return ToolResult(
                    "Could not determine the repository's default branch. Pass "
                    "`base` explicitly.", is_error=True)

        payload, err = await _github_api(
            "POST", f"/repos/{owner}/{name}/pulls", token,
            json={"title": args.title, "head": args.head, "base": base,
                  "body": args.body, "draft": args.draft})
        if err:
            return ToolResult(err, is_error=True)
        url = payload.get("html_url", "")
        number = payload.get("number")
        return ToolResult(
            f"Opened PR #{number}: {args.head} → {base} in {owner}/{name}.\n{url}".strip())
