# F.O.R.G.E.
### **F**ramework of **O**perational **R**untime & **G**ated **E**xecution.

A coding agent that runs on your machine or on a server, with sandboxed
execution and a permission gate in front of anything irreversible.

Forge provides a terminal interface for interactive work and a WebSocket peer
interface for accepting jobs from SPEDA Mark VI. Both run the same engine.

```bash
pip install -e ".[tui,providers]"
cd your-project
forge chat
```

---

## Contents

- [What it does](#what-it-does)
- [Requirements](#requirements)
- [Installation](#installation)
- [Terminal interface](#terminal-interface)
- [Tools](#tools)
- [Execution model](#execution-model)
- [Sandboxing](#sandboxing)
- [Permissions](#permissions)
- [Project conventions](#project-conventions)
- [Sessions](#sessions)
- [Commit attribution](#commit-attribution)
- [Context management](#context-management)
- [Model providers](#model-providers)
- [Agent configuration](#agent-configuration)
- [Running as a peer](#running-as-a-peer)
- [Configuration reference](#configuration-reference)
- [Project layout](#project-layout)
- [Testing](#testing)

---

## What it does

Forge runs an agent against a codebase. The agent reads files, searches, edits,
runs commands, and iterates on the results until the task is complete or a
limit is reached.

Two agents ship with it. They use the same engine and differ only in
configuration:

| Agent | Purpose | Tools |
|---|---|---|
| `optimus` | Software development | 14 |
| `centurion` | Security assessment | 6 |

Adding an agent means adding a configuration directory. No engine changes are
required.

Capabilities:

- File reading, writing and editing, with staleness detection on edits
- Repository search (`grep`, `glob`)
- Shell execution inside a sandbox
- Web search and page retrieval
- Git worktree isolation, so edits land on a branch rather than in your working
  copy
- A task list that survives context compaction
- Project conventions from AGENTS.md loaded into every turn
- Conversations saved per workspace and resumable after the terminal closes
- Commits attributed to the agent that wrote the code
- Codebase structure queries via a Graphify sidecar (optional)
- Model routing across six providers

---

## Requirements

- Python 3.11 or later
- Docker — optional; without it Forge uses a subprocess sandbox
- Git — required for worktree isolation and the git commands
- An API key for at least one model provider

---

## Installation

```bash
git clone https://github.com/spedatox/forge-mark1.git
cd forge-mark1
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[tui,providers,dev]"
```

Optional extras:

| Extra | Provides |
|---|---|
| `tui` | Input history, completion and reverse search in the terminal interface |
| `providers` | OpenAI, Gemini, z.ai, DeepSeek and Ollama support |
| `graph` | Codebase structure queries |
| `dev` | Test dependencies |

Forge reads configuration from the environment rather than loading a `.env`
file itself. Export the variables directly, or source a file first:

```bash
set -a && . ./.env && set +a
```

Verify the installation:

```bash
forge agents          # lists configured agents and their models
forge chat            # then type /doctor
```

`/doctor` reports missing API keys, an unreachable sandbox, and anything else
that would otherwise fail partway through a task.

---

## Terminal interface

```bash
cd your-project
forge chat                          # works in the current directory
forge chat --agent centurion
forge chat --cwd /path/to/repo
forge chat --model openai:gpt-5.1
```

### Input modes

The first character of a line determines how it is handled:

| Input | Behaviour |
|---|---|
| `text` | Sent to the model |
| `!command` | Runs in the sandbox immediately — no model call, no token cost. Not added to the conversation. |
| `/command` | Handled by the interface |

### Attaching an image

Name a picture anywhere in a line and it is sent with the turn:

```
what is wrong with this layout? @screenshot.png
```

`@` is optional — dragging a file onto the terminal pastes its path, quoted if
it contains a space, and that works too. A token is treated as an attachment
only if it has an image suffix, resolves against the workspace, and exists, so
`email @bob about the .png pipeline` attaches nothing.

PNG, JPEG, GIF and WebP, up to 3.75MB each and eight per turn. The path stays in
the text where you typed it — it is how you refer to the picture in the next
sentence. Anything that looked like an image and could not be read is reported
and the turn goes ahead without it.

A turn carrying an image runs on the agent's `vision_model` if its default
cannot see; the swap is printed when it happens.

### Keys

| Key | Action |
|---|---|
| `↑` `↓` | Previous inputs, stored per workspace |
| `ctrl+r` | Search input history |
| `tab` | Complete a `/command` or an `@path` |
| `esc` then `enter` | Insert a newline instead of submitting |
| `shift+tab` | Switch between `act` and `plan` mode |
| `ctrl+o` | Reprint the last truncated tool output in full |
| `ctrl+c` | Interrupt the running task |
| `ctrl+d` | End the session |

### Commands

| Command | Description |
|---|---|
| `/help` | List commands |
| `/status` | Session state and git status |
| `/diff` | Uncommitted changes in the working tree |
| `/branch [name]` | Show the current branch, or switch to another |
| `/doctor` | Check that this environment can run a task |
| `/cost` | Token usage for this session |
| `/context` | Context window utilisation |
| `/compact` | Summarise the conversation to free context |
| `/clear` | Discard the conversation, keep the session |
| `/tools` | Tools available to this agent |
| `/model` | Model in use |
| `/agent` | Agent profile in use |
| `/approved` | Operations approved for this session |
| `/transcript` | Print the raw conversation |
| `/export [file]` | Write the conversation to a file |
| `/keybindings` | Key reference |
| `/cwd` | Current working directory |
| `/commit <message>` | Stage everything and commit |
| `/review [focus]` | Have the agent read its own uncommitted changes |
| `/sessions` | Conversations that can be resumed here |
| `/resume [n\|id]` | Continue an earlier conversation |
| `/vim [on\|off]` | Vi keys in the input line |
| `/copy` | Last reply to the clipboard |
| `/mcp` | MCP servers and their tools |
| `/permissions` | What is gated and what you have allowed |
| `/init` | Write an AGENTS.md describing this project's conventions |
| `/exit` | End the session |

### Status line

A summary is printed above each prompt:

```
optimus · deepseek-v4-pro · 18% ctx · 12.4k in / 3.1k out · main · my-project
```

Fields appear only when they apply; permission mode is shown when it is not
`act`. Token counts are always shown because they are measured. Currency
figures appear only if per-model prices are configured in
`forge/tui/status.py`, since a built-in price table would go out of date
without any indication that it had.

---

## Tools

Tools are organised into groups. An agent's configuration lists the groups it
may use, and that list is the security boundary — a tool absent from it cannot
be called.

**Navigation** — `read_file`, `grep`, `glob`

**Editing** — `write_file`, `edit_file`

`edit_file` requires that the file has been read and has not changed since.
Content is compared by hash, so an edit against a file modified by another
process is rejected rather than applied to unexpected content. Edits are
reported to the operator as a diff.

**Execution** — `run_command`

Runs in the sandbox and returns stdout, stderr and the exit code. Read-only
commands are identified per invocation and may run in parallel; anything that
could write runs alone.

**Version control** — `enter_worktree`, `exit_worktree`

`enter_worktree` creates a git worktree on a new branch and moves the agent into
it. While it is active the sandbox refuses writes outside the worktree, so the
working copy open in your editor is unaffected. `exit_worktree` returns to the
workspace root and reports anything left uncommitted. Neither deletes the
branch — it remains for review.

**Research** — `web_search`, `web_fetch`

Requires `TAVILY_API_KEY`. These run in the Forge process rather than in the
sandbox, so an agent can consult documentation while the sandbox itself remains
without network access.

**Planning** — `todo_write`

Holds the task list outside the conversation so it survives context compaction.
One item may be in progress at a time.

**Codebase structure** — `graph_query`, `graph_path`, `graph_overview`

Requires the `graph` extra. Returns an error result when unavailable rather
than failing the task.

**Vault** — `hisar_list`, `hisar_read`, `hisar_deposit`

Requires `HISAR_MACHINE_TOKEN`. The owner's file store, reached from the Forge
process rather than the sandbox. Reads are unrestricted; writes are confined to
`/SPEDA` and `/Forge`; deleting and renaming are owner-only and have no tool.
Its own group rather than part of `coding`, so an agent is given a coding
toolset without also being given the owner's documents.

**Notification** — `telegram_send`

Requires `FORGE_TELEGRAM_TOKEN` and `FORGE_TELEGRAM_CHAT_ID`. Messages the owner
mid-job — a peer run has nobody watching the terminal. Job completion is
reported automatically and needs no tool; this is for the case worth raising
before the job ends. Its own group, for the same reason as the vault.

Both are withheld from the toolset entirely when their credentials are unset,
rather than being offered and failing: an agent has no way to tell a missing
credential from a transient fault, and will retry a door that was never going to
open.

---

## Execution model

Each task runs one loop:

1. Call the model with the conversation and the available tools.
2. Execute the tools it requests. Consecutive tools that declare themselves
   safe to parallelise run together; everything else runs one at a time.
3. Append the results and repeat.

The loop ends with a typed outcome: completed, iteration limit reached,
interrupted, or failed. The default ceiling is 30 iterations for `optimus`.

Failure handling:

- Tool failures return a result marked as an error rather than raising. The
  model reads it and adapts.
- Transient provider errors are retried with exponential backoff.
- Interrupts are honoured at defined points in the loop. Pending tool results
  are filled in first so the conversation stays valid for the next task.

---

## Sandboxing

Commands and file operations run inside a Cell. Two implementations are
available, selected by `FORGE_CELL_BACKEND`.

**Docker** (`docker`)

- Non-root user, all Linux capabilities dropped
- CPU, memory and process limits
- `--network none` unless the agent configuration or the job enables it
- Only the workspace is mounted

**Subprocess** (`subprocess`)

Used when Docker is unavailable. Commands run as child processes with the
working directory pinned to the workspace and path traversal rejected.

This is a workspace boundary, not a security boundary. Network access cannot be
reliably removed from a plain subprocess; when configuration disallows it,
proxy variables are set as a deterrent only. Use the Docker backend when the
code being executed is not trusted.

`auto` selects Docker when the daemon is reachable and subprocess otherwise.

---

## Permissions

Every tool call passes through a permission engine before it runs.

| Mode | Behaviour |
|---|---|
| `act` | Operations proceed; gated operations prompt |
| `plan` | Every mutating operation is denied |

High-impact operations — force pushes, recursive deletes, piping a download
into a shell — are gated regardless of mode and cannot be pre-approved through
configuration.

At a prompt the operator can allow once, allow for the session, or deny.
Session approvals are recorded and listed by `/approved`. If no operator is
reachable, the request is denied.

---

## Project conventions

If the workspace contains `AGENTS.md` — or `CLAUDE.md`, since many
repositories already have one — it is loaded into the system prompt on
every turn, labelled with its filename so the agent can tell a project's
instructions from its own.

Put the things someone working in the repository has to know and could not
infer from a single file: the real build and test commands, where a new
module of each kind belongs, conventions the code follows consistently, and
anything the project deliberately avoids.

`/init` has the agent survey the repository and write one. It refuses if a
file already exists; `/init force` rewrites it.

Keep it short. It is sent on every turn, so a sentence that would not change
what an agent does is paying to say nothing. Files over 12,000 characters
are truncated with a note.

---

## Sessions

Each conversation is written to `.forge/sessions/` in the workspace after
every completed turn, and can be picked up later:

```
/sessions          list what can be resumed here
/resume 1          continue the most recent
/resume 20260805-133421
```

A turn is the unit because it is the point at which the transcript is known
to be replayable — mid-turn there can be a tool call with no result, which
cannot be sent back to a provider.

Resuming restores the conversation only. The sandbox, tools and permissions
are rebuilt from the current profile, and file read-tracking starts empty,
so the first edit after a resume re-reads the file. A conversation from last
week describes files as they were.

The 40 most recent sessions per workspace are kept.

---

## Commit attribution

Work an agent does is committed under that agent's name, declared in its
profile:

```toml
[git]
name  = "Optimus Mark II"
email = "optimus@spedatox.systems"
```

This is applied as environment on the sandbox, so it covers every route to a
commit including `run_command git commit` — not only the `/commit` command.
Author and committer are both set; setting only the author would record the
operator as having applied a patch they never saw. The repository's own
`user.name` is left alone.

No account is required. Git stores a name and an address; a host such as
GitHub links that to a profile only if the address belongs to a registered
account.

---

## Context management

A token ledger tracks utilisation. As the conversation approaches the model's
limit, Forge reclaims space in two stages:

1. **Elision** — the contents of older tool results are dropped, leaving the
   surrounding reasoning intact. Frequently sufficient on its own.
2. **Summarisation** — a section of the conversation is replaced by a summary.
   The original task and the most recent exchanges are preserved verbatim.

After either, read-tracking is cleared: the model's knowledge of file contents
is now a summary's, so an edit must read the file again first.

The `todo_write` task list is restated into the summary, because tasks long
enough to require compaction are the ones with a plan worth keeping.

---

## Model providers

| Provider | Reference format | Key |
|---|---|---|
| Anthropic | `claude-sonnet-4-6` | `ANTHROPIC_API_KEY` |
| OpenAI | `openai:gpt-5.1` | `OPENAI_API_KEY` |
| Gemini | `gemini:gemini-2.5-pro` | `GEMINI_API_KEY` |
| z.ai | `zai:glm-4.6` | `ZAI_API_KEY` |
| DeepSeek | `deepseek:deepseek-v4-pro` | `DEEPSEEK_API_KEY` |
| Ollama | `ollama:llama3.1:8b` | `OLLAMA_BASE_URL` |

A bare reference is treated as Anthropic. Non-Anthropic providers require the
`providers` extra.

Selection order, highest priority first:

1. `--model` on the command line, or the model named in a job request
2. The agent's `vision_model`, when the turn carries an image
3. `FORGE_LLM_FALLBACK_CHAIN`, tried in order when a provider fails
4. The agent's `profile.toml`

An explicit model outranks `vision_model` deliberately: an operator who named a
model meant it, and being silently overruled is the worse surprise. The picture
is sent to what they picked and refused out loud if that model cannot see it.

Providers differ in what they accept beyond text. An image is carried as an
Anthropic content block internally and translated at the request boundary — to
`image_url` for the chat-completions family, `input_image` for OpenAI's
Responses API. A model without vision returns an error rather than answering
about a picture it never received.

Providers differ in how strictly they enforce tool schemas. Anthropic enforces
required parameters; others may omit them or substitute different names. Tools
should reject incomplete input explicitly rather than applying a default.

---

## Agent configuration

An agent is a directory under `forge/agents/` containing `profile.toml` and
`system_prompt.md`.

```toml
agent_id = "optimus"
name = "Optimus"
domain = "systems, code & infrastructure"

model = "deepseek:deepseek-v4-pro"
vision_model = "claude-sonnet-4-6"
tools = ["coding", "web"]
permission_mode = "act"
max_iterations = 30

[cell]
allow_network = false
cpus = 2.0
memory_mb = 2048
timeout_s = 120
```

| Field | Purpose |
|---|---|
| `agent_id` | Identifier used on the wire and in job requests |
| `model` | Model reference. Model identifiers live only in profiles. |
| `vision_model` | Where a turn carrying an image goes instead. Omit if `model` can already see. |
| `tools` | Tool groups or individual tool names — see below |
| `permission_mode` | `act` or `plan` |
| `max_iterations` | Loop ceiling for a single task |
| `[cell]` | Sandbox resource and network policy |

`vision_model` is a second model reference rather than a `supports_vision` flag
because a flag would need a table, in code, of which model identifiers can see —
going stale silently, the same objection this project makes to a built-in price
table. An operator naming the model they want is a fact; a table is a guess with
a shelf life.

`tools` takes any of these group names, individual tool names, or a mix:

| Group | Contains |
|---|---|
| `coding` | Navigation, editing, execution, worktrees, planning, graph, delegation |
| `security` | Navigation, editing and execution only — no graph |
| `web` | `web_search`, `web_fetch` |
| `memory` | `memory` — the owner's memory, held by Mark VI |
| `hisar` | `hisar_list`, `hisar_read`, `hisar_deposit` |
| `notify` | `telegram_send` |

The last three are separate groups deliberately. They reach the owner rather
than the repository, so an agent is given a coding toolset without also being
given their documents, their memory, or a line to their phone.

To add an agent, create the directory, write the two files, and run
`forge agents` to confirm it loads.

---

## Running as a peer

```bash
forge connect --agent optimus
```

Forge opens an outbound WebSocket to SPEDA Mark VI and registers. Because the
connection is outbound, no inbound firewall rule or public address is required.

Requires `SPEDA_WS_URL` and `SPEDA_API_KEY`.

The peer accepts chat requests and job dispatches, streams tool activity and
output back, and relays permission prompts to the operator's client. A task
that fails still sends a terminating frame, so a caller is never left waiting.

`forge serve` exposes the same job interface as a standalone WebSocket server
without connecting to Mark VI.

### More than one peer for the same agent

One agent may run in several places at once — on the server and on a
workstation. Mark VI keys connections by `(agent_id, host)`, so those coexist
only if each peer says which machine it is:

| Variable | Purpose |
|---|---|
| `FORGE_HOST` | A stable name for this machine. Blank → the bare hostname. |
| `FORGE_ROOTS` | Directories this peer will work in, `os.pathsep`-separated. Blank → anything well-formed for its platform. |

`FORGE_ROOTS` is how the routing is steered: the owner picks a folder and the
folder identifies the machine. Leave it blank on the server, which should take
anything, and set it on a workstation so only that machine's own projects come
to it.

Advertising is not enforcement. These are routing hints; what a peer will
actually do is bounded on the peer, by its own tools and permission gate.

**Why this matters for working locally.** A workstation peer runs the code in
your working tree while everything Mark VI provides — the owner's memory, the
model picker, images, permission prompts in the operator's client — arrives over
the socket exactly as it does in production. That is the loop that does not
require a deploy to test a change. Set `FORGE_HOST` before doing it: until each
peer named its machine, every one registered as `default` and the second to
connect silently displaced the first.

---

## Configuration reference

| Variable | Default | Purpose |
|---|---|---|
| `FORGE_CELL_BACKEND` | `auto` | `docker`, `subprocess` or `auto` |
| `FORGE_CELL_IMAGE` | `python:3.12-slim` | Docker image for the sandbox |
| `FORGE_WORKSPACE_ROOT` | `./.forge/workspaces` | Root for per-agent workspaces |
| `FORGE_ALLOWLIST_PATH` | `./.forge/allowlist.json` | Persisted operation approvals |
| `FORGE_LLM_FALLBACK_CHAIN` | unset | Comma-separated model references |
| `FORGE_GRAPHIFY_BIN` | PATH lookup | Path to the Graphify executable |
| `FORGE_AGENT` | `optimus` | Default agent |
| `FORGE_LOG_LEVEL` | `INFO` | Logging level |
| `FORGE_NO_STATUS` | unset | Suppress the status line |
| `SPEDA_WS_URL` | `ws://127.0.0.1:8000/agents/ws/optimus` | Mark VI endpoint |
| `SPEDA_API_KEY` | unset | Mark VI authentication |
| `FORGE_HOST` | hostname | Which machine this peer speaks for |
| `FORGE_ROOTS` | unset | Directories this peer will work in, `os.pathsep`-separated |
| `TAVILY_API_KEY` | unset | Required by `web_search` |
| `HISAR_MACHINE_TOKEN` | unset | Required by the `hisar` tools |
| `HISAR_URL` | `https://hisar.spedatox.systems` | Vault endpoint |
| `FORGE_TELEGRAM_TOKEN` | unset | Bot token; `_<AGENT>` suffix overrides per agent |
| `FORGE_TELEGRAM_CHAT_ID` | unset | Destination chat, required with the token |
| `FORGE_NO_TELEGRAM` | unset | Suppress Telegram entirely |

Provider keys are listed under [Model providers](#model-providers).
See [`.env.example`](.env.example) for the annotated list.

### Commands

| Command | Purpose |
|---|---|
| `forge chat` | Interactive session in the current directory |
| `forge connect` | Run as a Mark VI peer |
| `forge serve` | Standalone job server |
| `forge agents` | List configured agents |
| `forge demo` | Offline demonstration; no API key required |

---

## Project layout

```
forge/
├── agents/     Agent profiles and system prompts
├── cell/       Sandbox backends (docker, subprocess)
├── gate/       Peer connection and job protocol
├── graph/      Graphify sidecar
├── mcp/        MCP client and tool provider
├── model/      Provider routing
├── skills/     Loadable skill definitions
├── tools/      Tool implementations
├── tui/        Terminal interface
└── warden/     Execution engine, permissions, context management
tests/
```

---

## Testing

```bash
pytest -q
```

966 tests. No network access or API key is required; provider calls and
sandboxes are substituted.

---

## License

Private project. Not licensed for redistribution.
