"""The Forge's curated tool set.

A small, fixed toolset, so every schema is sent on turn 1 — no deferred-loading /
ToolSearch machinery (study §2 open question 5: only needed when the tool list
strains the prompt, which a curated set does not). Each tool declares its own
harness-side safety flags; the loop and permission engine read those, the model
never sees them.
"""
from forge.tools.shell import RunCommand
from forge.tools.ask import AskOperator
from forge.tools.claude_code import ClaudeCode
from forge.tools.diagnostics import Diagnostics
from forge.tools.files import ReadFile, WriteFile, EditFile
from forge.tools.graph import GraphQuery, GraphPath, GraphOverview, GraphIndex
from forge.tools.hisar import HisarDeposit, HisarList, HisarRead
from forge.tools.memory import Memory
from forge.tools.search import Grep, Glob
from forge.tools.task import TaskTool
from forge.tools.telegram import TelegramSend
from forge.tools.todo import TodoWrite
from forge.tools.web import WebFetch, WebSearch
from forge.tools.worktree import EnterWorktree, ExitWorktree

# Navigation — how an agent orients in a repo it did not write. Shared by every
# profile: the alternative is reading whole files, which fills the window before
# the work starts.
NAV_TOOLS = [ReadFile, Grep, Glob]

# Research — what the repo cannot answer. Kept as its own group so a profile can
# take navigation without taking the open internet; see web.py on why this is
# independent of the Cell's allow_network posture.
WEB_TOOLS = [WebSearch, WebFetch]

# The vault. Its own group because it is the owner's filesystem rather than a
# capability of the repo: a profile can take coding tools without being handed a
# door into the owner's documents. Withheld at dispatch when no machine token is
# configured (see toolsource.without_hisar_tools), so an agent whose profile
# allows them still never sees a door it has no key to.
HISAR_TOOLS = [HisarList, HisarRead, HisarDeposit]

# Reaching the owner mid-job. Its own group for the same reason the vault is:
# messaging a person is not a capability of working on a repo, and a profile
# should be able to take coding tools without one. Withheld at dispatch when
# no bot is configured.
NOTIFY_TOOLS = [TelegramSend]

# Asking the owner a question mid-job. In CODING_TOOLS rather than its own
# group: reaching a fork you should not pick alone is not an optional extra
# for real work, it is the alternative to guessing silently. Degrades on its
# own when no operator is reachable, so it needs no dispatch-time gate.
ASK_TOOLS = [AskOperator]

# The owner's memory, which lives in Mark VI. Its own group because it is the
# owner's, not the repository's — the same line the vault is on — and because a
# profile should be able to take coding tools without being handed the power to
# rewrite what every other agent believes about him. Withheld at dispatch when
# there is no channel to Mark VI, so the standalone TUI never sees it.
MEMORY_TOOLS = [Memory]

# Reusable tool groups, referenced by agent configs via their allowlist (§2).
# todo_write is in the coding group rather than its own: a plan is not an
# optional capability for multi-step work, it is what keeps it coherent.
# `task` is here rather than in a group of its own: delegating is not an
# optional extra for real work, it is how a long job avoids drowning its own
# context. A profile that omits it simply never spawns subagents.
CODING_TOOLS = [*NAV_TOOLS, WriteFile, EditFile, RunCommand,
                GraphQuery, GraphPath, GraphOverview, GraphIndex, Diagnostics, TodoWrite,
                EnterWorktree, ExitWorktree, TaskTool, ClaudeCode, AskOperator]

# Centurion's group: run security tooling in the Cell (RunCommand) and read/write
# scan output and engagement reports (files). No graph — its subject is a live
# target's posture, not a codebase's structure. The Cell policy (allow_network)
# and the operator's authorization are the real boundary, not this list.
SECURITY_TOOLS = [*NAV_TOOLS, RunCommand, WriteFile, EditFile]

ALL_TOOLS = {cls.name: cls for cls in [
    RunCommand, ReadFile, WriteFile, EditFile, Grep, Glob,
    GraphQuery, GraphPath, GraphOverview, GraphIndex, Diagnostics, WebSearch, WebFetch, TodoWrite,
    EnterWorktree, ExitWorktree, TaskTool, ClaudeCode,
    HisarList, HisarRead, HisarDeposit, TelegramSend, AskOperator,
    Memory,
]}

__all__ = ["ALL_TOOLS", "NAV_TOOLS", "CODING_TOOLS", "SECURITY_TOOLS", "WEB_TOOLS",
           "HISAR_TOOLS", "NOTIFY_TOOLS", "ASK_TOOLS", "MEMORY_TOOLS", "Memory",
           "RunCommand", "ReadFile", "WriteFile", "EditFile", "Grep", "Glob",
           "GraphQuery", "GraphPath", "GraphOverview", "GraphIndex", "Diagnostics", "WebSearch", "WebFetch",
           "TodoWrite", "EnterWorktree", "ExitWorktree", "TaskTool", "ClaudeCode",
           "HisarList", "HisarRead", "HisarDeposit", "TelegramSend", "AskOperator"]
