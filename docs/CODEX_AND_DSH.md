# Two more references: Codex and the DeepSeek Harness

`REFERENCE_STRENGTHS.md` swept one reference (Claude Code) for everything that
makes a loop survive the model inside it. This document does the same for two
others, chosen because they fail differently:

- **Codex** (`openai/codex`, `bc7a487`, 2026-08-18) — a Rust monorepo, ~194k
  lines in `core` alone. Its centre of gravity is **the boundary between the
  agent and the machine**: sandboxing, escalation, exec policy, patch
  application.
- **DeepSeek Harness** (`deepseek-ai/deepseek-harness`, `99f6f02`, dsh-0.1.0-rc.7)
  — a TypeScript plugin architecture over Cordis, ~50 packages. Its centre of
  gravity is **composition**: every behaviour is a plugin against an extension
  point, and each one is specified in prose before it is written.

## Coverage, stated honestly

This is not a full read of either. Codex `core` alone is twelve times Forge's
entire source. Read in full for this pass: `apply-patch/src/seek_sequence.rs`,
`sandboxing/src/denial.rs`, `core/src/guardian/{mod.rs,policy.md}` (header and
policy), `core/src/responses_retry.rs`, `core/src/unified_exec/mod.rs` (header)
and `head_tail_buffer.rs`, plus crate-level structure for `execpolicy`,
`rollout`, `hooks`, `turn_diff_tracker`. On the DSH side the package READMEs are
unusually load-bearing — they specify behaviour to a level most projects put in
code — so `guard/repeat-tool-reminder`, `guard/timeout-policy`,
`core/agent-loop`, `lsp/tool-lsp` and `docs/tool-catalog.md` were read as
specifications rather than skimmed.

Every Forge claim below was checked against this repository, not recalled.

---

## The organising observation

Claude Code's mechanisms cluster around **the model being wrong about itself**.
Codex's and DSH's cluster somewhere else, and the difference is the point of
reading them:

- **Codex assumes the machine will refuse.** Its recurring shape is: attempt
  under restriction, detect the refusal, escalate deliberately, retry. Sandbox
  denial, patch context mismatch, network policy, connection loss — each has a
  named detector and a staged escalation rather than an error.
- **DSH assumes the harness will be extended by someone who is not you.** Its
  recurring shape is: state the contract, state what fails loud, state what is
  deliberately transparent. Its guards are advisory by construction — they
  never veto, because a veto in a plugin system is a footgun.

Forge sits closer to Claude Code than to either. Its strongest machinery is
about the model (`reminders.py`, `_unverified`, `_RESUME_TRUNCATED`,
read-before-write grounding), and its weakest surface is exactly Codex's
strongest: **what happens when the machine says no.**

---

## A. Where Codex is ahead

### A1. Patch context matching degrades in four named stages

`apply-patch/src/seek_sequence.rs` locates a context block by trying, in order:

1. exact match
2. ignoring **trailing** whitespace (`trim_end`)
3. ignoring leading **and** trailing whitespace (`trim`)
4. after normalising Unicode punctuation to ASCII — the six dash code points,
   fancy quotes, and eight varieties of non-breaking space

Stage 4 carries its justification in a comment: it "mirrors the fuzzy behaviour
of `git apply`", and exists because a model authors diffs in plain ASCII against
source files containing typographic dashes. Two defensive cases are handled
explicitly, one with the date of the panic it fixed: empty pattern → no-op
match, and `pattern.len() > lines.len()` → `None` rather than an out-of-bounds
slice ("previously caused a panic when...").

**Forge:** `edit_file` (`forge/tools/files.py`) is exact-substring only.
`current.count(args.old_string)`, and 0 occurrences is a flat rejection:
"old_string not found in {path}. Read the file and retry."

This is the single clearest coding-reliability gap in Forge. The failure is
common and cheap to trigger — the model reproduces a line with one space
different, or the file uses `–` where the model typed `-` — and the current
answer costs a full read-and-retry cycle each time. Note that Forge's rejection
is *correct*, just expensive; the fix is not to guess, it is to widen the match
in declared stages and say which stage matched.

### A2. Sandbox denial is detected, not surfaced

`sandboxing/src/denial.rs` — `is_likely_sandbox_denied` decides whether a
non-zero exit was the sandbox refusing rather than the command failing:

- exit 0 or no sandbox → not a denial, immediately
- a keyword sweep over output: "operation not permitted", "permission denied",
  "read-only file system", and four more
- **quick-reject exit codes `[2, 126, 127]`** — these mean bad usage, not
  executable, command not found; a denial they are not
- on Linux with seccomp, exit code `128 + SIGSYS` is a denial with certainty

`unified_exec/mod.rs` states the flow this feeds: "on sandbox denial, retries
without sandbox when policy allows (no re-prompt thanks to caching)."

The interesting part is the quick-reject list. Without it, every `command not
found` would be read as a sandbox denial and escalate to an approval prompt —
the mechanism would fire constantly on the most common shell error there is.

**Forge:** nothing equivalent. `RunCommand.call` returns exit code and streams
straight through. A Docker Cell that denies a write produces "permission denied"
in stderr, `is_error=True`, and the model is left to work out whether the
sandbox refused or the code is wrong. Those two require opposite responses.

### A3. Connection loss and API failure have separate retry budgets

`core/src/responses_retry.rs` keeps two counters on one state struct: `retries`
and `connection_retries`. A pure `ConnectionFailed` (network down, not a server
error) takes a different path — a delay starting at **5 s**, doubling to a
**60 s** cap, and **not bounded by `max_retries`**. The operator sees
"Reconnecting... waiting for network". Only when the ordinary retry budget is
exhausted does it try `try_switch_fallback_transport`.

Guarded behind an `UnboundedConnectionRetries` feature flag, and excluded for
internal sessions and Bedrock — so it is a considered posture, not a default.

**Forge:** one budget. `RETRY_ATTEMPTS = 4`, `RETRY_BASE_DELAY_S = 2.0`, cap
30 s (`forge/warden/engine.py`). Every `ErrorClass.TRANSIENT` gets the same four
laps. Four laps at 2 s doubling is ~30 s of tolerance, which the comment
correctly identifies as the shape of a 529 spike — but a laptop losing wifi for
two minutes is also transient, and it kills the job. The distinction Codex draws
is real: *a 529 means the server is struggling and more requests make it worse;
a dropped socket means nothing is happening and waiting costs nothing.*

### A4. Approval escalation has a model in it, with a denial ceiling

`core/src/guardian/` is a second model session that decides whether an
`on-request` approval can be granted without asking the human. Its header states
the discipline in four lines: reconstruct a compact transcript preserving user
intent; ask a dedicated session for strict JSON; **fail closed on timeout,
execution failure, or malformed output**; apply the explicit outcome.

Two things are worth taking regardless of whether the guardian itself is:

- **Every input is token-budgeted separately**:
  `GUARDIAN_MAX_MESSAGE_TRANSCRIPT_TOKENS = 10_000`,
  `GUARDIAN_MAX_TOOL_TRANSCRIPT_TOKENS = 10_000`, per-entry caps of 2,000 and
  1,000, `GUARDIAN_RECENT_ENTRY_LIMIT = 40`. A reviewer that can be flooded is
  a reviewer that can be talked around.
- **Denials are rate-limited per turn**:
  `MAX_CONSECUTIVE_GUARDIAN_DENIALS_PER_TURN = 3`, and `1` for the cyber
  category. Past that it stops asking. This is the same class of guard as the
  death-spiral comments in Claude Code's `query.ts` — a reviewer that denies
  forever is an infinite loop with a model call in it.

`guardian/policy.md` is ~60 lines of written policy with an explicit risk
taxonomy (exfiltration, credential probing, persistent security weakening,
destructive actions) and per-category "outcome rule:" lines. One line is worth
quoting for its accuracy about the thing being guarded: agents "can make
mistakes, especially in complicated inline commands."

**Forge:** `PermissionEngine` + `oracle.py` cover the same territory with a
human in the loop rather than a model, and `oracle.py`'s degradation is already
correct and better stated ("an unanswered question is a no, a lost socket is a
no, a timeout is a no"). The gap is not the guardian — it is that Forge has **no
per-turn denial ceiling**. An agent that keeps proposing a gated command gets
asked, denied, and asked again, indefinitely, for as long as the iteration
budget lasts.

### A5. Command output keeps head *and* tail

`unified_exec/head_tail_buffer.rs`: a capped buffer splitting its budget 50/50
between a stable prefix and suffix, dropping the **middle** and recording
`omitted_bytes`.

**Forge:** already had this, and it was already broken. `warden/results.py`
implements head+tail correctly at both the per-result and batch stages, with the
reasoning written down — "a failing build puts the error at the end, and
head-only truncation is how you lose exactly the line you needed" — and a named
regression test behind it.

But `Cell._cap` runs **first**, at the sandbox boundary, capping each stream at
`max_output_bytes = 100_000`, and it was head-only:

```python
return text[:limit] + f"\n…[truncated {len(text) - limit} bytes]"
```

So on any command noisy enough to exceed 100 KB — a verbose test run, a failing
build, exactly the case both layers name — the tail was discarded before the
layer that exists to preserve it ever saw the text. The downstream head+tail
preview was faithfully preserving a tail that was already gone.

**Fixed in this pass**, along with the regression test that was missing at that
layer (`test_the_cell_cap_keeps_the_tail_too`). Recorded here rather than
quietly, because the interesting part is not the bug — it is that a correct
mechanism with its reasoning written down and a test guarding it was silently
defeated by an older, cruder version of itself one layer upstream. Codex's
`HeadTailBuffer` is not smarter than what Forge already had; it is just the only
implementation in its stack, applied at the point of capture.

---

## B. Where the DeepSeek Harness is ahead

### B1. Repeat detection counts successes, and cannot be laundered

`guard/repeat-tool-reminder` watches every agent's tool-call stream, counts runs
of consecutive calls with **identical canonicalized arguments** (deep key-sort +
`JSON.stringify`, so property order does not matter), and injects an escalating
advisory at thresholds `[3, 5, 8]`. The first threshold is a short generic
nudge; later ones name the tool, the run length, and the arguments.

Four design notes, each of which Forge gets differently:

- **It is advisory by construction.** "never vetoes or rewrites a call... The
  decision stays entirely with the model."
- **Untracked calls are transparent to the chain.** A call excluded by config
  neither increments nor resets: `grep X → todo_write → grep X` counts as two
  consecutive `grep X`. The README states the reason plainly — "bookkeeping
  tools interleaved into a loop must not launder it."
- **Denied calls count.** Detection sits on `post-execute`, which runs for calls
  a `pre-execute` listener denied. "A model hammering a denied call is exactly
  the loop worth breaking."
- **The full argument string is always the chain key**; the preview cap
  (`argumentsPreviewChars: 500`) bounds only the reminder text. A looping `write`
  cannot ride its payload into the next request.

**Forge:** `forge/warden/reminders.py` (`observe`) has this, narrower on two axes
and with one hole:

1. **Failure-only.** The docstring defends this: "a tool called twice the same
   way that worked twice is a loop doing its job." That is true of a `grep` and
   false of a model that has read the same file eight times, or run the same
   passing test after every edit without changing anything.
2. **Any success clears the streak.** `state._last_error = (None, 0)` runs for
   every non-error result in the batch. So `grep X (fail) → todo_write (ok) →
   grep X (fail)` never trips — the exact laundering DSH designed against.
3. **Fires once, ever** (the `fired` set). DSH escalates three times.

Constraint 1 in Forge's own docstring — "once each, per job" — is well argued
for judgement-shaped reminders. It is the wrong shape for a repetition counter,
where the second and third occurrence *are* the new information.

### B2. Tool timeouts are declared by the tool and enforced centrally

`guard/timeout-policy` is a single `tools/execute` around-dispatch listener,
**zero-config**. It reads `timeoutMs` from the tool's own registry declaration,
arms a deadline fusing the caller's abort signal with its own timer, swaps the
derived signal in for dispatch, **restores the caller's signal afterward** so
`post-execute` sees the original, and on its own timer firing replaces the
result with a structured `TOOL_TIMEOUT`: `{ isError: true, error: { code:
'TOOL_TIMEOUT' }, content: 'Error: tool call timed out after <ms>ms' }`.

The README names why the budget lives on the tool: "a mistyped tool name is not
possible."

**Forge:** timeouts exist only for shell (`CellPolicy.default_timeout_s = 60`,
`max_timeout_s = 600`). Every other tool can hang indefinitely — `web`,
`graph_index` on a large repo, an MCP tool over a wedged stdio pipe, a subagent.
`dispatch_tool` has no deadline. A hung MCP server hangs the loop, and the only
way out is the operator's interrupt.

### B3. Persistent terminals, as six small tools

`dsh-tool-terminal`: `terminal_open` / `send` / `read` / `signal` / `list` /
`close`. Codex has the same capability as `unified_exec`. The schemas encode
most of the design:

- `terminal_send` waits for **"a prompt, stdin wait, output silence, timeout, or
  session exit"** — five distinct completion signals, which is what makes an
  interactive process usable by a model at all.
- `submit: false` for control characters and incomplete REPL input.
- `run_in_background: true` returns a job id for `job_output`/`job_kill`.
- `terminal_signal` allows SIGINT/TERM/KILL/TSTP/HUP but **rejects
  shell-targeted SIGKILL** — "use terminal_close" — so the model cannot orphan
  a process tree.
- `terminal_close` "wait[s] until its captured owned process tree is gone."

**Forge:** `Cell.run` is one-shot with a wall clock (`forge/cell/base.py`).
There is no way to start a dev server and then interact with the app, run a long
build and check on it, drive a REPL or debugger, or answer an interactive
prompt. A command that asks a question hangs until the timeout kills it. For a
harness aiming at industrial coding work this is the largest missing
*capability*, as distinct from the largest missing *safeguard*.

### B4. Config that fails loud

`repeat-tool-reminder`'s `thresholds` "fails loud at plugin load: an empty list,
a non-integer, a value below 2, or a duplicate throws, never a silent fall-back
to defaults." Contrast the same package's `include`/`exclude`, where a pattern
matching no registered tool is explicitly **not** an error, with the reason
given: `exclude: [mcp_*]` must stay valid in a deployment that loads no MCP
tools.

That pairing — validate the values, do not validate the referents — is the
generalisable idea, and it is a better articulation than "validate your config".

---

## C. Where Forge is already ahead, or has chosen differently

Recording these so the list above is not read as a to-do.

- **`_unverified` / `_VERIFY_PROMPT`.** Neither reference has an equivalent of
  Forge noticing "wrote code, ran nothing" and refusing to finish on it. Codex
  tracks a turn diff (`turn_diff_tracker.rs`) but for display, not for a
  challenge.
- **`_wind_down`.** The iteration ceiling spends its last turn on a handover
  with the tools removed. Codex ends turns; DSH ends turns. Neither asks for the
  one thing only the agent knows.
- **Read-before-write grounding by content hash**, and `_forget_files` clearing
  it after *any* reclamation including elision — because a `read_file` result is
  a tool result, so elision can leave the cache asserting a file was read whose
  contents are gone. That reasoning is sharper than anything comparable read in
  either reference.
- **LSP, deliberately declined.** `forge/tools/diagnostics.py` argues the case
  in its docstring: run the project's own checker for the half that matters
  (is this valid?), and let the graph tools answer the other half (what is
  coupled to what?), across languages at once. DSH's `tool-lsp` is a real
  capability Forge lacks, but its own stated scope — "when textual matches are
  ambiguous or before a change requires precise definitions" — is largely what
  `graph_query`/`graph_path` cover. **Not a gap.** Worth revisiting only if the
  graph proves to miss things in practice.
- **`oracle.py`'s degradation rule** is better stated than Codex's fail-closed
  comment, and covers more cases.

---

## D. Ranked

Ordered by (damage prevented or capability gained) ÷ (effort). Effort is
Forge-sized: this repository is 16k lines and its modules are small.

| # | Gap | Where | Effort | Why here |
|---|-----|-------|--------|----------|
| ~~1~~ | ~~Tool-call deadlines~~ | `warden/tool.py`, `warden/dispatch.py` | — | **Done in this pass.** See below. B2. |
| 2 | **Staged edit matching** | `tools/files.py` | S–M | Four declared stages, and the result says which matched. Removes the most common wasted round-trip in coding work. A1. |
| ~~3~~ | ~~Repeat counter~~ | `plugins/builtin/repeat_reminder.py` | — | **Done in this pass**, as a plugin rather than as more of `reminders.py`. B1. |
| 4 | **Per-turn denial ceiling** | `warden/permissions.py`, `warden/oracle.py` | S | A counter and a stop. Prevents an ask/deny/ask spiral burning the iteration budget. A4. |
| 5 | **Split the retry budget** | `warden/engine.py`, `model/errors.py` | S–M | A new disconnected class with its own escalating, generously-bounded budget. A3. |
| 6 | **Sandbox-denial detection** | `tools/shell.py`, `cell/` | M | Keyword sweep + the `[2, 126, 127]` quick-reject, then tell the model *which* kind of failure it hit. A2. |
| 7 | **Persistent terminals** | new `tools/terminal.py`, `cell/base.py` | L | The one genuine capability gap. Needs a new Cell method and a completion heuristic; the five-signal list in B3 is the design. |
| ~~8~~ | ~~Head+tail output retention~~ | `cell/base.py` | — | **Done in this pass.** Was a live defect, not a gap: the Cell's head-only cap ran upstream of the correct head+tail logic and defeated it. A5. |

Items 1–5 are each an afternoon and together close most of the distance. Item 7
is a week and changes what the harness can be pointed at.

## A note on how to read this list

The one item that turned out to be urgent was the one filed as "check first,
Forge may already do the right thing" — and Forge *did* do the right thing, in
the module where you would look for it, with the reasoning and a test. The
defect was in the other place that also truncates, which nothing pointed at.

That is worth carrying into the rest of the list. The question each reference
actually answers is not "does Forge have this mechanism" but "does Forge have it
everywhere the condition arises". Item 1 turned out to be the same shape:
`run_command` had a timeout, and the answer to "which other tools can block
forever" was "all of them".

---

## E. What item 1 turned into

`Tool.TIMEOUT_S` (default 300s) with a `timeout_s(args, ctx)` method beside the
three existing safety methods, enforced once in `dispatch_tool`. Taken from
DSH's timeout-policy: **the tool declares the budget, one central listener
enforces it**, so a new tool inherits a bound by existing rather than by
remembering. `SELF_BOUNDED` is the opt-out, and `task` is currently the only
user of it — a subagent's `max_iterations` and its `_wind_down` handover are a
better stopping condition than a wall clock, and cancelling it mid-flight
destroys the handover specifically.

Four things that were not obvious going in:

**The ordering is the fragile part, not the mechanism.** A backstop that fires
before the real timeout is worse than none: it stops a command the Cell was
about to stop correctly, and reports the harness as the cause. So `run_command`
reads `ctx.cell.policy.max_timeout_s` per call rather than declaring a constant
— an agent profile that raises its Cell's ceiling would otherwise invert the two
silently. `claude_code` derives from its own `FCC_TIMEOUT_S` the same way.

**A timeout has to be distinguished by identity, not by type.** On 3.11+
`asyncio.TimeoutError` *is* `TimeoutError`, so a tool letting its own inner
deadline escape — the graph sidecar at 30s, an MCP call at 120 — is
indistinguishable from the dispatcher's by exception type. Reported as ours it
would state a limit that never applied. `_DeadlineExceeded` is raised only when
this layer's own clock won, which is the same check DSH's timeout-policy makes
(`timeoutOf(d.signal, 'TOOL_TIMEOUT')`) for the same reason.

**An interrupt must not be reported as a timeout.** When the deadline fires, the
tool's task is cancelled and awaited, and that await can itself be interrupted
by the operator. `Task.cancelling()` separates the two, so ctrl+c during cleanup
still reads as an abort rather than as the harness giving up.

**The message matters as much as the cancellation.** A model told only "timed
out" retries with a narrower argument — which cannot help, the arguments already
passed validation — and pays another full deadline to find that out. It is also
told that whatever the tool had already done is still done, because a cancelled
`write_file` is not a no-op and an agent that assumes otherwise builds on a file
it never looked at.

### Bundled with it: a timeout that says what it saw

`SubprocessCell` collected output with `communicate()`, which returns its pair
only on success — so a command killed at the deadline reported `stdout=""` and
one line of stderr. The output before the wedge is the entire diagnosis: a
hanging `pytest` names the test it stopped on, a hanging build names the file.
`DockerCell` never had the problem, because its `timeout -s KILL` fires inside
the container and `docker exec` returns normally with the output — so the two
backends disagreed about what a timeout tells you.

Now both drain into a bounded head+tail buffer that survives cancellation. The
bound is applied *while* reading rather than after, because a buffer filled for
the length of a timeout is exactly the unbounded growth `max_output_bytes`
exists to prevent, and the reader never stops consuming, because a full pipe
blocks the writer and a command blocked on a pipe nobody drains is a hang the
harness caused.

### What that turned out to be a fix for

Reported from use: *"I keep hitting the 300 timeout and the turn ends like it
gets confused."*

The 300 is not the tool backstop. It is
[`forge/agents/centurion/profile.toml:44`](../forge/agents/centurion/profile.toml)
— `timeout_s = 300`, the Cell's per-command wall clock — and Centurion is the
default agent (`FORGE_AGENT` falls back to it). So a full test suite, a cold
build or a wide scan is killed at five minutes, correctly and by design.

The confusion was downstream of that, and there were three causes, none of them
the timeout itself:

1. **The result carried no evidence.** `communicate()` yields nothing when
   cancelled, so the model got `exit_code: 124` and one line. It could not tell
   whether the suite was passing, failing, or had never started.
2. **The lever was invisible.** `run_command` has a `timeout` argument that
   accepts up to `max_timeout_s` (600), and nothing in the tool's description,
   the field's description, or the timeout result said so. A model with no
   visible lever has two moves: re-run the identical command into the identical
   wall, or stop and report the work blocked. Both read as confusion from
   outside; neither is.
3. **The number collided.** `DEFAULT_TOOL_TIMEOUT_S` was also 300, so once the
   backstop shipped, two unrelated clocks would have reported the same figure
   with nothing in the message to tell them apart. Moved to 420.

Fixed by (1) the buffer above, (2) `_timeout_advice`, which reads the live
policy and names both the limit that applied and the ceiling it can be raised
to — and at the ceiling stops offering more time and asks for the work to be
split instead, because advice to ask for longer when longer does not exist just
sends the model round again with a bigger number.

**And it exposed two ordering bugs in the backstop itself.** `diagnostics` runs
each checker in `_CHECKERS` in turn until one answers, every one of them a Cell
command inheriting the profile's clock — so on Centurion its honest worst case
is several times 300s against a 300s backstop, an exact tie. `ask_operator`
parks on `FORGE_ASK_TIMEOUT_S`, which an operator with a Telegram channel
reasonably sets to many minutes. Both now derive their deadline from the bound
they actually sit above, via `cell_backed_timeout(ctx, runs=...)`.

The general rule, which the first pass got only half right: **a tool whose inner
bound is operator-configurable cannot inherit a fixed backstop.** Two tools were
fixed by hand in that pass because they were the two I happened to open. The
helper exists so the next one is not found the same way.

---

## F. The plugin architecture

DSH's headline claim is that everything is a plugin. Reading it, the load-bearing
idea turns out not to be "plugins" — Forge already had a provider seam and a hook
seam — but **`next()`**.

`warden/hooks.py` gives a plugin two places to stand, `pre_tool` and `post_tool`,
and they are two separate calls with nothing joining them. That is enough to
inspect, veto or rewrite. It is not enough to **wrap**, and wrapping is what most
cross-cutting behaviour actually is:

- a timeout arms a clock, calls through, disarms it
- a retry calls through more than once
- a tracer holds a start time across the call
- a cache is able to not call through at all

None of those decompose into a before and an after. Forge's own tool deadline is
the proof: it is written *into* `dispatch_tool` because no seam had that shape.
DSH ships the same behaviour as `dsh-tool-call-timeout-policy`, an ordinary
plugin, and the only difference is the waterfall.

### What was built

`forge/plugins/`, in four parts:

- **`waterfall.py`** — named events whose listeners compose into an onion around
  the real work. Registration order is outermost-first. A listener may run code
  before, after, both, or instead of `next()`.
- **`context.py`** — the `ctx` a plugin receives: services it may reach, the
  registration verbs, and a `Scope` that records every registration's undo.
- **`loader.py`** — resolve, validate, apply, unload.
- **`builtin/repeat_reminder.py`** — the first plugin, and item 3 above.

A plugin is a module with `name`, optional `Config` (pydantic) and `inject`, and
`apply(ctx, config)`. That is the whole contract.

### The four decisions worth recording

**Disposal is recorded, not written.** `ctx.on(...)`, `ctx.tool(...)`,
`ctx.provide(...)` each hand their undo to the plugin's scope. A plugin author
never writes teardown and therefore never writes it wrong — teardown maintained
in parallel with setup is teardown that is already out of date. A plugin you
cannot remove is a patch, and a system that accumulates patches is one where
nobody dares turn anything off.

**Config fails loud on values and stays quiet about referents.** DSH draws this
line and it is the right one. An empty threshold list or a negative count is a
mistake with no sensible fallback, and defaulting silently discards the
operator's intent without telling them. But a pattern matching nothing —
`exclude: ["mcp_*"]` in a deployment running no MCP servers — is not a mistake;
it is a config that stays correct across deployments. Validate what the value
*is*; never validate what it points at.

**Plugins load by name, not by import.** Cordis self-registers; Forge's manifest
is an explicit ordered list in `.forge/extensions.json`. This was Law 2 of
`MARK2_SEAMS`, and it is kept here on its merits rather than its authority:
import-side-effect registration is how load order becomes significant in ways
nobody can see, and an ordered manifest costs one line per plugin. Law 1 —
dependency direction — is untouched and verified: nothing in `warden/`, `cell/`
or `model/` imports `plugins/`.

**One plugin's failure is one plugin's failure.** A plugin that will not import,
will not validate, or throws in `apply` is unloaded, logged, and skipped; the
rest load. A plugin that throws halfway through `apply` is *fully* unloaded
first, because half-applied is the worst state available — its listeners run
while its author believes it never loaded.

### What it changes about the rest of this list

Items 4 (denial ceiling), 5 (retry budget) and 6 (sandbox-denial detection) were
all filed as core edits. Two of the three are now better as plugins:

- **Denial ceiling** is a counter around dispatch — a `tools/execute` listener,
  no core change at all.
- **Sandbox-denial detection** is a post-processing step on a `run_command`
  result, which is the same shape.
- **Retry budget** stays core: it lives in the model stream loop, not the tool
  path, and there is no waterfall there yet. Adding `model/stream` as an event
  would be the way in, and is the obvious next extension point.

Item 7 (persistent terminals) stays core — it needs a new Cell method, which is
a contract change rather than a behaviour addition.
