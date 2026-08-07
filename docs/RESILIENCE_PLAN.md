# Order of work: closing the gaps the reference sweep found

`REFERENCE_STRENGTHS.md` catalogued the reference and listed five things Forge
does not have. This is the plan for four of them, the argument for demoting the
fifth, and a short list of things that stay unbuilt on purpose.

Two of the five estimates in that document were wrong, and both are corrected
below. Checking where a change actually lands before ranking it is the whole
point of writing a plan rather than a wish list — the same discipline
`RESILIENCE.md` applied when it discovered the file-changed reminder was not
"one rule" after all.

---

## The ranking

| # | Gap | Shape | Cost | Prevents |
|---|---|---|---|---|
| 1 | Truncated turns are invisible | protocol + one guard + a continuation | **L** | corrupted work |
| 2 | No denial streak signal | one field, one rule in `reminders.py` | **S** | a wasted job |
| 3 | Compaction has no pointer back | archive sidecar + a sentence | **M** (was estimated S) | unrecoverable detail |
| 4 | Verify prompt lacks the rationalisation list | prose | **XS** | a false PASS |
| 5 | No prompt maintenance markers / cache boundary | convention; then real work | XS / L | prompt rot |

Item 1 is the only one that can produce a wrong outcome rather than a wasted
call. It goes first despite being the largest, because ordering by effort is how
the correctness item ends up perpetually fourth. Item 4 is an isolated string
and can ride along in the same sitting.

---

## 1. A turn cut off at `max_tokens` is committed as though it finished

### What actually breaks

`engine.py`'s stop condition is:

```python
if not turn.tool_uses:
    ...
    return self._terminal(StopReason.COMPLETED, state)
```

A turn truncated at the output cap produces text and no tool-use blocks. It is
therefore indistinguishable from a turn that finished, and the loop returns
`COMPLETED` with `final_text` holding half a sentence. On the peer path that
half-sentence is persisted as the answer; in the TUI the operator sees prose that
simply stops.

The default cap is 16,384 tokens (`config.py`). A model part-way through writing
a file, or through a long final report, hits it routinely on a big job.

**Verified:** `stop_reason` appears nowhere in `forge/`, and the seam has no
channel for it — `model/base.py` defines `TextDelta`, `ToolUseRequest`, and
`UsageReport`, none of which carries why the turn ended. The fact is not
ignored; it is unrepresentable.

### 1a — Get the fact into `_Turn`

Two sources, in the precedence `model/errors.py` already established for
classifying failures ("prefers typed SDK exceptions, falls back to exception
type, and only then sniffs strings"):

**Authoritative.** `AnthropicModel.stream` already calls
`await stream.get_final_message()`. `final.stop_reason` is right there and is
currently dropped on the floor. Yield it.

**Fallback.** Providers that report nothing still usually yield a `UsageReport`.
`output_tokens >= max_tokens` is a reliable-enough tell, and it is the same
shape of concession `UsageReport.estimated` already makes — a figure that was
inferred rather than reported, marked as such so nothing downstream renders a
guess as a measurement.

The event to add is a new `TurnEnd`, not a field on `UsageReport`. `UsageReport`
is documented as **optional by contract** — "reporting usage is a capability,
not an obligation" — and hanging the truncation signal off an optional event
makes the most dangerous check fail open on exactly the providers least likely
to implement it. A separate event whose absence means *unknown* is honest about
what it does and does not know.

```python
@dataclass
class TurnEnd:
    """Why the turn stopped, when the provider says. Absence means unknown —
    which is not the same as "it finished", and the engine must not read it
    as such."""
    reason: str | None = None        # "end_turn" | "tool_use" | "max_tokens" | ...
    truncated_estimate: bool = False # inferred from output_tokens hitting the cap
```

Touches: `model/base.py` (event + `ModelEvent` union), `model/anthropic_model.py`,
`model/providers.py` (OpenAI-compat `finish_reason == "length"`),
`model/scripted.py` (so tests can drive it), `warden/engine.py` (`_Turn.end`,
and the `_stream_turn` branch that collects it).

### 1b — Refuse to call a truncated turn complete

This is the whole safety payload, and it is one condition:

```python
if not turn.tool_uses and not turn.was_truncated():
    ...COMPLETED
```

A truncated turn with no tool calls falls through to the continuation below
instead of terminating. If 1c is not built yet, it should terminate with
`StopReason.ERROR` and the real reason rather than `COMPLETED` — failing loud
beats failing silent, and `_terminal` already carries an `error` field for
exactly this.

Build 1a and 1b together. They are the fix; 1c is the polish.

### 1c — Resume, with the three wrong moves forbidden

New `ContinueReason.RESUMED_TRUNCATED`, a counter on `LoopState` beside
`retry_attempt`, capped at 3 (the reference's `MAX_OUTPUT_TOKENS_RECOVERY_LIMIT`,
and there is no reason to differ). The injected message follows the reference's
construction — name what the model will otherwise do, forbid each:

> Your previous turn was cut off at the output limit. Resume directly. Do not
> apologise, do not recap what you were doing, and do not restart the paragraph
> — pick up mid-sentence if that is where the cut fell. Break what remains into
> smaller pieces so the next turn fits.

Charge it against `state.retries`, as the transient-retry path already does: a
resumed turn is not work done, and letting truncation quietly extend the
iteration ceiling is the same leak the `retries` field was added to plug.

### 1d — Escalating the cap: not doing it

The reference retries the identical request at 64k when the 8k default was hit.
Forge's cap is `FORGE_MAX_TOKENS`, an operator setting. A harness whose entire
permission philosophy is *the operator decides* should not silently quadruple a
number the operator chose. If the cap is wrong the continuation in 1c still
finishes the work, one turn slower, and the journal says `resumed_truncated` so
the operator can see the cap is costing them and raise it themselves.

### The case worth a test rather than code

A turn truncated **mid-`tool_use`** leaves the last block's input JSON
incomplete. Anthropic's SDK accumulates into `final.content`, so the engine
would receive a `ToolUseRequest` with a partial `input` dict. That reaches
`tool.Args.model_validate` and is answered by `format_validation_error` — which
is already the right behaviour, and better than the reference's, since Forge
prints the signature. So this degrades gracefully by accident. Write the test to
confirm the accident holds; do not add code for it.

### Done when

- `tests/test_truncation.py`: a `ScriptedModel` yielding `TurnEnd("max_tokens")`
  with no tool uses does **not** return `COMPLETED`.
- The same, with `TurnEnd` absent but `output_tokens == max_tokens`, is caught
  by the estimate.
- Three consecutive truncations exhaust the cap and terminate with a stated
  reason, not silently.
- `Terminal.transitions` contains `resumed_truncated` — the recovery is
  assertable without reading message bodies, which is what `ContinueReason`
  exists for.

---

## 2. Nothing counts denials

### What actually breaks, restated honestly

The reference escalates at three consecutive or twenty total classifier denials.
Forge has no classifier, and in the TUI a gate stop *is* a question to the
operator, so the reference's exact failure — twenty silent refusals — does not
exist here.

Three places where it does:

- **Plan mode.** `Decision("deny", "plan mode is active: mutating tools are
  disabled for review.")` is deliberately a wall, not a checkpoint. An agent
  that has not understood it is in plan mode can spend its whole iteration
  budget hitting that wall, and no human is in the loop to notice.
- **Peer and non-interactive runs**, where an ASK has nobody to ask.
- **`_repeating` does not cover it.** That rule requires *identical arguments*
  (`repeated_failure: tuple[str, int]`). An agent probing three different routes
  to the same denied goal — `rm -rf`, then `git clean -fd`, then a shell
  redirect — trips nothing, and that is precisely the pattern
  `DENIAL_GUIDANCE` was written to discourage.

### Where it lands

`warden/reminders.py`, not `permissions.py`. `PermissionEngine` is stateless by
design and holds mode plus allowlist; giving it a per-job counter would make it
per-job. The reminders module already owns "observations ABOUT the run rather
than the run itself", already receives `results` in `observe`, and already has
the once-per-job discipline this needs.

A fourth field and a fourth rule:

```python
denials: int = 0
"""Consecutive tool calls refused by the gate, any tool, any target. Distinct
from `repeated_failure`, which needs identical arguments: an agent working
around a refusal by finding a new route is the pattern worth catching, and by
construction each attempt looks different."""
```

Reset by any allowed call, mirroring the reference's `recordSuccess`. Fire once,
at three. The text should say the thing the model cannot see — that these are
one pattern rather than three independent refusals — and point at the exit
`DENIAL_GUIDANCE` already names: stop and say what you are blocked on.

Ordered second in `REMINDERS`, after `repeated_failure`: a gate refusal is
cheaper to discover late than a call that will never work, but more expensive
than unverified changes.

### Done when

- Three denials of three *different* actions fire the rule once.
- An allowed call between the second and third resets the count.
- It fires once per job, like its neighbours.
- A denial streak in a subagent does not fire the parent's rule — `ReminderState`
  is already per-Warden for this reason; assert it stays that way.

---

## 3. Compaction loses detail with no route back — and the fix is bigger than stated

### The correction

`REFERENCE_STRENGTHS.md` called this "the smallest item here… a string and a
path lookup". That was wrong, and checking the persistence layer is what shows
it.

`persistence.save` writes `session.messages`. `repl.py:395` sets
`session.messages = list(terminal.messages)` after each run, and `_compact`
rebinds `state.messages` to the rebuilt, post-cut list. So the session file
holds the transcript *after* compaction. A pointer to it would point at a file
that has lost exactly the detail the summary replaced — a pointer to the
absence, which is worse than no pointer, because the model would burn a
`read_file` discovering that.

The reference does not have this problem because its transcript is an
append-only JSONL written per message; compaction changes what is *sent*, not
what is *recorded*.

### So the work is an archive, then a sentence

At the cut, before `rebuild` returns, write `messages[1:cut]` to a sidecar —
`sessions/<id>.compacted.<n>.json` — and thread the path into the rebuilt
message. `find_cut` already computes the boundary and `rebuild` already has the
slice; nothing new needs to be discovered, but something new needs to be
written to disk, and it needs to survive `_prune` (which currently deletes by
mtime and would happily orphan a sidecar from a live session).

The sentence, appended to `rebuild`'s block, in its existing register:

> The messages this summary replaced are on disk at `{path}`. If you need
> something exact from before this point — a command's output, a file as it
> was, the wording of an error — read it rather than reconstructing it.

That last clause is the load-bearing one. The failure this prevents is not the
model *lacking* a detail; it is the model *inventing* one because retrieval
never occurred to it.

### Second-order

Compaction currently calls `_forget_files()`, invalidating read-before-write
grounding because "your memory of its contents is this summary's, not the
file's." An archive does not change that and must not be read as changing it —
a file's *contents at the time it was read* are in the archive; what is on disk
now is not. Worth a sentence in the code so a later reader does not connect the
two and weaken the refusal. This is the same trap `FileState.reported_change`
was split off to avoid.

### Done when

- After a compaction the archive exists, parses, and contains the replaced slice.
- The rebuilt head names it.
- `_prune` does not orphan a live session's sidecars.
- Two successive compactions produce two archives and the second head names the
  second — `rebuild`'s existing dedup already stops the operator block stacking;
  confirm the pointer does not stack either.

---

## 4. The verify prompt's missing half

The cheapest item and the one with the clearest target. Forge's `_VERIFY_PROMPT`
is at parity with the reference on structure, and ahead of it on opening — the
real 22-vs-175 incident is a stronger prior than a generic warning. It is
missing two things, both prose:

**The enumerated rationalisations.** The reference quotes six excuses in the
verifier's own voice with the counter attached: *"The code looks correct based
on my reading" — reading is not verification. Run it.* Naming the sentence the
model is about to write is a different intervention from telling it to be
thorough, and a cheaper one than any mechanism.

**The `Bad (rejected)` / `Good` worked pair.** A plausible PASS backed by code
reading, labelled as rejected, next to a real one. Forge has the
CHECK/RAN/SAW/RESULT skeleton but nothing showing what a convincing violation
looks like — and a convincing violation is the only kind that gets through.

Both target `audit_verification`'s blind spot precisely. It rejects a PASS with
no `RAN` line. It cannot reject a `RAN` line that ran the wrong thing, and
"start the server and check the code" is exactly the shape of a command that
runs, produces output, and verifies nothing.

**Done when:** the prompt contains both, and `test_verify_subagent.py` still
passes — the additions must not perturb the `VERDICT:` line the parser reads.

---

## 5. Prompt maintenance markers, and the cache boundary

Split, because the two halves have very different costs.

**Markers: do it.** A `@[MODEL LAUNCH]`-equivalent convention in
`agents/prompt.py`, on any instruction that exists to counterweight a specific
model's tendency, with an expiry note. Near-free, and it stops the slow
accumulation of instructions aimed at models nobody runs any more. Forge already
carries a few of these without labelling them.

**Cache boundary: defer, and say why.** The reference splits its system prompt
at `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` and names its escape hatch
`DANGEROUS_uncachedSystemPromptSection` because each runtime boolean on the
wrong side doubles the cache-key surface — "2^N". That maths bites at their
prompt size and section count. Forge's system prompt is a fraction of it and has
few runtime conditionals, so the same discipline buys much less. Revisit when
`agents/prompt.py` grows conditionals, not before. Building the machinery first
is the speculative abstraction the prompt itself warns against.

---

## Deliberately not building

- **The reference's five compaction subsystems.** snip, microcompact, cached
  microcompact, context collapse, reactive compact — each flagged, several
  mutually redundant. That is an experiment surface, not an architecture. Forge's
  three stages plus the `RECOVERABLE` backstop cover the same ground.
- **`bashSecurity.ts`-scale command analysis.** 2,593 lines and 23 numbered
  checks defend a shell running on the user's machine. Forge runs commands in a
  Cell with an output cap. Different threat model; importing the surface would
  import maintenance without importing the threat.
- **Tool-name aliases.** The reference needs them to replay old transcripts.
  Forge's unknown-tool error lists the tools that exist, which is better for a
  live session and is the only session Forge has.
- **Cross-message `tool_use` ID dedup.** The reference added it after a
  session-deadlocking bug (CC-1212) caused by an orphan handler Forge does not
  have. Write a test asserting `repair_transcript` handles a duplicated id;
  add code only if it fails.
- **Telemetry density and feature-flag gating.** Both are artefacts of running
  A/B infrastructure at scale. The `_CACHED_MAY_BE_STALE` suffix in the
  reference is doing real work precisely because several of its bugs come from a
  gate flipping mid-turn. Adopting the pattern inherits the bug class for none
  of the benefit.

---

## Sequence

**1a + 1b + 4** in one sitting. The truncation guard and the prompt additions
touch nothing in common, so a failure in either does not block the other, and it
gets the corruption case closed on day one.

**1c**, once 1a's protocol change has survived a real job.

**2**, which is an afternoon and lands entirely inside a module built for it.

**3**, which needs the archive designed before anything is written — in
particular how sidecars interact with `_prune`.

**5's markers** whenever `agents/prompt.py` is next open for another reason.

---

## Shipped — 2026-08-07: items 1 and 4

**Item 1 landed whole, including 1c, against this plan's own sequencing.** The
plan said to defer the continuation until the protocol change had survived a
real job. That was wrong, and the reason is visible as soon as 1b exists on its
own: shipping the guard without the resume converts a silent wrong answer into a
hard `StopReason.ERROR`. Correct, and strictly worse to use — a job that
previously finished with a bad last paragraph now dies. 1c is what makes the
guard a recovery instead of a new way to fail, and thirty lines is not worth a
release where truncation is fatal.

What exists now:

| Piece | Where |
|---|---|
| `TurnEnd` event; absence means unknown, never "finished" | `model/base.py` |
| `stop_reason` from the final message + cap-vs-output estimate | `model/anthropic_model.py` |
| `finish_reason` on chat-completions; `incomplete_details` on `/v1/responses` | `model/providers.py` |
| `ends=[...]` so a test can script a truncated turn | `model/scripted.py` |
| `_Turn.truncated()`, failing open when the provider said nothing | `warden/engine.py` |
| The guard, checked before the verification nudge | `warden/engine.py` |
| Bounded resume, charged against `retries`, reset when tools run | `warden/engine.py`, `warden/state.py` |
| `ContinueReason.RESUMED_TRUNCATED` | `warden/state.py` |

Two decisions worth recording because they went against the obvious choice:

- **`TurnEnd` is its own event, not a field on `UsageReport`.** `UsageReport` is
  optional by contract. Hanging truncation off it would fail open on exactly the
  providers least likely to implement it — wrong direction for the one signal
  that can corrupt work.
- **`truncated()` returns False when the provider reported nothing.** Fail-open,
  deliberately, and the docstring says so: the alternative treats every silent
  provider's every turn as truncated and resumes forever. The fix for a silent
  provider is to make it report, not to guess in the engine.

`test_truncation.py` — nine tests: the fragment is not the final answer; a normal
end still completes; a silent provider is unchanged; the estimate fires at the
cap and not below it; repeated truncation terminates loudly at the bound;
resumes are charged to the budget; the streak resets when tools run; and
truncation mid-`tool_use` still falls through to schema validation rather than
being intercepted.

That last one is the test the plan asked for instead of code, and it holds — a
tool call whose name was truncated away is answered by
`format_validation_error`, which is a better message than any generic resume.

**Item 4 landed as written** — the six rationalisations in the verifier's own
voice, and the rejected/accepted pair. The rejected example is annotated with
what reading failed to establish (the route may not be registered), because the
point is not that commands are mandatory but that reading cannot see the thing
that breaks.

Suite: 783 passing, 3 skipped. Demo end-to-end: passes.

**Still open: items 2, 3, and 5.** Nothing about this sitting changed their
ranking or their estimates.

## What this does not fix

Gap 5 of `RESILIENCE.md` — the unexamined number — is still not on the list, and
none of the above moves it. Item 1 prevents a wrong outcome, but it does so by
reading a fact about the response envelope, not about the reasoning inside it.
Everything here still lives on the harness side of the line that document drew,
and the honest next measurement remains the one it named: how often an agent
holding `graph_overview` reaches for it unprompted.
