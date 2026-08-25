"""`git_push` — publishing the work, with the credential kept out of the Cell.

Optimus does all its git work INSIDE the Cell: clone, branch, add, commit — none
of which need a secret, because the commit identity rides in on env vars
(`cfg.git.env()`) and everything else is local. Only the final `git push` needs
a credential, and that is the one operation this tool takes Warden-side.

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
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from forge.warden.tool import Tool, ToolContext, ToolResult

_TOKEN_ENV = "FORGE_GIT_TOKEN"
_GIT_TIMEOUT_S = 120.0

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

        # The repo has to be reachable from the HOST for the push to run outside
        # the Cell. An ephemeral Cell with no host mount cannot be pushed from
        # here — say so rather than failing obscurely.
        cell = getattr(ctx, "cell", None)
        host_root = getattr(cell, "host_path", None) if cell is not None else None
        if host_root is None:
            return ToolResult(
                "This workspace has no host-visible path, so the push cannot run "
                "outside the sandbox. (An ephemeral Cell — deposit the work or "
                "push from a workspace-backed run instead.)", is_error=True)

        from pathlib import Path
        root = Path(host_root).resolve()
        repo = (root / args.path).resolve()
        # Containment: the model names the path, so it must not escape the
        # workspace the same way the Cell's own read/write guard enforces.
        if os.path.commonpath([str(repo), str(root)]) != str(root):
            return ToolResult(f"path escapes the workspace: {args.path!r}", is_error=True)
        if not (repo / ".git").exists():
            return ToolResult(f"{args.path!r} is not a git repository.", is_error=True)

        # ── Validate the remote WITHOUT the token (read-only, no creds needed) ──
        code, out, err = await self._git(repo, ["remote", "get-url", args.remote])
        if code != 0:
            return ToolResult(
                f"No remote {args.remote!r} in this repository ({err.strip() or 'not found'}). "
                f"Add it with `git remote add {args.remote} https://github.com/<owner>/<repo>.git` "
                f"first.", is_error=True)
        push_url = _github_https(out.strip())
        if push_url is None:
            return ToolResult(
                f"Refused: remote {args.remote!r} is {out.strip()!r}, which is not a "
                "github.com remote. The push credential is scoped to GitHub and "
                "will not be sent anywhere else.", is_error=True)

        # An insteadOf rewrite could redirect even the URL just validated, so a
        # repo carrying one is refused rather than trusted. A normal working
        # repo has none; one that does is exactly the case worth stopping.
        code, rewrites, _ = await self._git(
            repo, ["config", "--get-regexp", r"^url\..*\.insteadof$"])
        if code == 0 and rewrites.strip():
            return ToolResult(
                "Refused: this repository configures url.*.insteadOf rewrites, "
                "which can redirect a push away from the address just verified. "
                "Remove them before pushing.", is_error=True)

        branch = args.branch
        if not branch:
            code, out, err = await self._git(repo, ["rev-parse", "--abbrev-ref", "HEAD"])
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
        code, out, err = await self._git(repo, push_args, token=token)
        combined = (out + "\n" + err).strip()
        if code != 0:
            # Never echo the token; git does not print it, but the helper name
            # could appear — scrub defensively.
            return ToolResult(
                f"Push failed:\n{self._scrub(combined)}", is_error=True)
        return ToolResult(
            f"Pushed {branch} to {push_url}.\n{self._scrub(combined)}".strip())

    async def _git(self, repo, git_args: list[str], token: str | None = None
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

    @staticmethod
    def _scrub(text: str) -> str:
        """Defence in depth: keep the credential-helper snippet (which names the
        token env var, not its value) out of anything shown to the model."""
        return text.replace(_CRED_HELPER, "<credential-helper>")
