# What makes a harness resilient to the model inside it

## The experiment that frames this

The owner wired DeepSeek into Claude Code, and it behaved almost identically to
Claude Code. Same model, different harness, different quality of work.

That retires a question this project kept asking. Optimus rationalising an
anomalous number, retrying an identical failing call three times, reporting
untested work as done — those were repeatedly explained away as "DeepSeek's
weakness". They are not. They are the absence of mechanisms that a harness can
supply and this one did not.

This document is the list of those mechanisms, what Forge has, what it lacks,
and what the gap actually costs. Everything marked VERIFIED was tested against
the running code on 2026-08-06; everything else says what it is.

---

## The principle

A capable model needs a harness that stays out of the way. A weaker model needs
a harness that **closes the loop it cannot close itself**.

Every mechanism below is one of three shapes:

1. **The harness observes what the model cannot see about itself.** A model has
   no memory of having *not* run something. The loop watched.
2. **The harness makes the failure legible.** An error that names the fix is
   worth more than an error that names the exception.
3. **The harness refuses to accept an unsupported claim.** Not "please verify" —
   a PASS with no command behind it does not count as a PASS.

Prose belongs in the first draft of each of these. None of them should *stay*
prose, because a rule the model can decline is not a mechanism.

---

## Where Forge already stands up

Worth stating, because the gaps below are not a verdict on the whole thing.

| Mechanism | Status |
|---|---|
| Result truncation spills the full output to a file and gives the path | **Better than the reference.** The model is told where the rest is, not just that there was more. |
| Unknown tool name lists the tools that do exist | VERIFIED — `Unknown tool 'reed_file'. Available tools: read_file.` |
| Dangling `tool_use` repaired before replay | `repair_transcript`, so a turn that died mid-call does not poison the next one |
| Retry with error classification and capped backoff | Four attempts, doubling from 2s |
| A context-overflow 400 is treated as RECOVERABLE, not permanent | Matches the reference, which parses the same error's numbers out of its message text. Both refuse to let the one recoverable 400 be classified with the fatal ones. |
| Read-before-edit enforced by the harness | Not advice — the edit fails |
| Iteration ceiling ends with a handover, not a severed head | Added 2026-08-06 |
| Per-turn tool refresh | A tool that appears mid-job becomes available |

---

## Gap 1 — Errors that name the exception instead of the fix

The reference states the premise in a code comment, above the validation call:

```ts
// Validate input types with zod (surprisingly, the model is not great at
// generating valid input)
```

The harness is built on the assumption that the model gets tool calls wrong.
`formatZodValidationError` (utils/toolErrors.ts) then sorts the failure into
three kinds and writes a sentence for each:

```
read_file failed due to the following issue:
The required parameter `path` is missing

grep failed due to the following issues:
The parameter `pattern` type is expected as `string` but provided as `number`
An unexpected parameter `lines` was provided
```

Missing, unexpected, mistyped — named in the tool's own vocabulary, with the
raw error kept only as a fallback when none of the three apply.

There is a second layer worth stealing whole. `buildSchemaNotSentHint`
recognises one specific mechanical cause and hands back the exact recovery:

> This tool's schema was not sent to the API… Without the schema in your
> prompt, typed parameters (arrays, numbers, booleans) get emitted as strings
> and the client-side parser rejects them. Load the tool first: call
> ToolSearch with query "select:<tool>", then retry this call.

That is the shape to copy — not "invalid input", but *why the call came out
malformed* and *the command that fixes it*.

**VERIFIED against Forge, 2026-08-06.** Four malformed calls, and what our model
received before this was closed:

```
missing required arg  -> Invalid input for 'read_file': 1 validation error for
                         ReadFileArgs  path  Field required
                         [type=missing, input_value={}, input_type=dict]
                         For furth…                    ← truncated mid-URL

wrong type            -> Input should be a valid string
                         [type=string_type, input_value=123, input_type=int]

tool raised           -> <tool_error>AttributeError: 'NoneType' object has no
                         attribute 'read'</tool_error>
```

This is a Python internal handed to a language model. It names a class the model
has never seen (`ReadFileArgs`), a Pydantic error code (`type=missing`), and
trails off mid-sentence into a documentation URL.

Nowhere does it say **what the tool wants**. A weaker model has to infer the
correct call from a stack of implementation detail, and the most likely
inference is to try again with the same shape.

This matters more here than almost anywhere else, because of a documented
provider behaviour: **DeepSeek and Gemini ignore a tool schema's `required`.**
Missing arguments are not an edge case on this deployment — they are the normal
failure, and the message that greets them teaches nothing.

**Cost of the gap:** one wasted call per malformed invocation, minimum, and an
unknown number of retries when the model guesses the same wrong shape twice.

### Closed — 2026-08-07 (`forge/warden/toolerrors.py`)

The same three kinds, in the tool's own vocabulary, plus one thing the reference
does not do: the signature is printed. The doc above says the work is writing
the sentences rather than extracting the facts, and that was right — but it
missed that the sentences alone still leave the model to *infer* the correct
call. Given that the failing provider is the one which ignores `required`, the
shape is exactly what it does not have. So it is stated:

```
read_file failed due to the following issue with its arguments:

  - The required parameter `path` is missing.

read_file takes: path (string, required), offset (integer, optional), limit (integer, optional).

Nothing ran. Call it again with the arguments corrected — an identical retry
will fail identically.
```

Types are named the way the schema names them, not the way Python does: the
model was handed JSON Schema and sent JSON, so `object` and `integer`, never
`dict` and `int`. Constraint failures keep Pydantic's own sentence, which reads
well already ("Input should be greater than or equal to 1") — only the framing
around it changes. Nothing that leaked before survives: no `ReadFileArgs`, no
`type=missing`, no truncated URL.

The tool-raised case is answered too, and differently, because it is a different
problem. `<tool_error>` now says whose fault it is — the arguments were valid,
the tool broke, and a reshaped retry cannot help. Without that sentence a
traceback class name reads to the model as "you called it wrong", and the
correction it invents is the one thing guaranteed not to work.

---

## Gap 2 — Compaction that keeps the wrong things

Forge compacts by summarising. The reference summarises against a **nine-section
structure**, and two of those sections are the reason it works:

- **"All user messages"** — every non-tool-result message the operator sent.
  Intent lives there, and paraphrase loses it.
- **"Optional next step, with direct quotes… verbatim to ensure there's no
  drift in task interpretation."**

Forge's version is much thinner. This is the highest-value unclaimed item,
because compaction failure is invisible: the session does not error, it
gradually starts working on a slightly different problem than the one asked
for. On a six-hour run that is the difference between finishing and drifting.

### Closed — 2026-08-07, and not the way this section proposed

Re-reading Forge's `SUMMARY_INSTRUCTION` against the reference: it already had
nine sections, including "every instruction and correction from the operator,
verbatim". The first draft of this document was written from greps, and this is
one of the places that showed. The prompt was not thinner.

The gap was real anyway, and it was one level down. **Asking for verbatim is not
a mechanism.** It is a request, made to the model, about the model's own output,
and the one case where declining it is undetectable — the summary still reads
fluent and complete. That is this document's own test applied to this document's
own recommendation, and the recommendation fails it.

So the operator's turns are now carried by the harness, which cannot paraphrase
because it never reads them (`compaction.operator_turns`, and `rebuild`'s
`said` argument). They are captured once, from the transcript a run is seeded
with — exact rather than heuristic, because every user-role message the loop
appends after that point is the harness talking to itself. The block is dropped
from the oldest end under a size ceiling, since a later instruction usually
supersedes an earlier one, and it says so when it drops something.

The prompt still asks. Two mechanisms of different kinds covering one failure is
not redundancy here: the model's rendering has context the copy does not, and
the copy has a guarantee the rendering does not.

---

## Gap 3 — Nothing notices when a file changes underneath

Forge tracks read-before-edit. It says nothing when a file the model has already
read is modified by something else — a linter, a formatter, the operator, a
concurrent subagent.

The reference injects a reminder naming the file. Without it the model edits
against a version it read ten minutes ago, and the edit either fails on a stale
match or silently reverts someone else's change.

Forge now has the injection channel (`forge/warden/reminders.py`, added
2026-08-06). This is a rule to add to it, not new machinery.

### Closed — 2026-08-07, and it needed a little machinery after all

A rule was not enough, for two reasons the channel's design made visible.

**It is not a rule.** Every reminder in that module fires once per job, because
a repeated judgement is nagging and teaches the reader to skip the block. A file
moving underneath the model is not a judgement, it is a fact, and the second
formatter run is exactly when an edit lands on text nobody has looked at. So
`file_changed_notice` sits outside `REMINDERS` and outside the once-each rule,
and it outranks any nudge that was due the same turn.

**Nothing was watching.** The channel injects; it does not observe. The loop now
sweeps the file cache after any turn that ran a tool which is not declared
read-only, capped at the twenty most recently used entries. The condition is an
over-approximation on purpose: `run_command` answers `is_read_only` per command,
so `git status` would skip the sweep and would usually be right — but the cost
of being wrong that way is a stale edit, and the cost of being wrong the other
way is one extra read.

The part worth stating, because getting it backwards would be silent: detecting
a change does **not** update the recorded digest. `FileState.reported_change` is
a second field, so read-before-write keeps refusing the edit while the notice
stops repeating. Folding the two would have silenced the announcement and the
refusal together, which is precisely the combination that lets a blind edit
through — a fix that opens the hole it was written to close.

---

## Gap 4 — Authorisation has no scope

From the reference's risk section:

> A user approving an action once does NOT mean that they approve it in all
> contexts… Authorization stands for the scope specified, not beyond.

Forge's gate asks per action and records the exact action string on "always",
which is good. What it does not do is tell the model that **an approval it
received earlier does not extend to a similar action later**. Nothing stops an
agent reasoning "they let me force-push yesterday" into force-pushing today.

Cheap to add, and it belongs beside the denial guidance shipped today.

### Closed — 2026-08-07 (`dispatch.APPROVAL_SCOPE`)

It went beside the denial guidance, as predicted, and it is the more important
of the two. A refusal is self-limiting: it stops one call and the model has to
think again. An approval is what generalises on its own — "they let me
force-push" quietly becomes a belief about force-pushing, and the second one is
never put to anybody.

Attached to the result of the call the operator personally cleared, and only
that one. Not to standing allow-list hits, which are a decision already on file,
and not to ordinary calls, which is how a note becomes furniture. Appended after
the result cap, so the scope statement cannot be the part that gets truncated.

---

## Gap 5 — No plausibility check on a number

The failure that produced the whole investigation: a tool reported 22 import
edges across 76 modules where the true figure was 175, and the agent wrote a
paragraph explaining why 22 made sense.

The `verify` subagent's prompt now tells it to sanity-check counts against an
independent measure, and that is the right first draft — but it is still prose,
and prose is what the model can decline. There is no mechanism here yet.

Whether one is even possible is an open question. "Is this number plausible"
requires knowing what plausible means for that number, which the harness
generally does not. A narrower version might: flag when a count-shaped result
changes by more than an order of magnitude between runs of the same tool.

**Flagged as unsolved rather than pending.** It is the hardest item on the list
and the one most worth thinking about properly.

### Still open — 2026-08-07, deliberately

Gaps 1–4 shipped. This one was designed and then not built, which needs
justifying rather than announcing.

The narrower version proposed above was written out and tested against the
incident that motivated it. **It would not have caught it.** The agent ran the
tool once. There was no second run to differ from by an order of magnitude, so a
between-runs comparison has no signal to read. It is a mechanism with near-zero
false positives and near-zero recall, which is code that almost never fires.

Two other candidates were considered and rejected for the same test:

- *Fire when the agent runs a script it wrote this session.* Detectable, cheap,
  and describes the incident exactly — and also describes writing a test and
  running it, which is most of the work most of the time. It would fire on
  nearly every job. That is the wallpaper failure the reminders module was
  built to avoid, and it would cost the three rules that do work their
  credibility.
- *Fire when the final report contains a number no tool result contained.* Also
  mechanical. But the reported figure was 22, and 22 was in the tool output. The
  failure was not a fabricated number, it was an unquestioned one.

That third attempt is the useful one, because it names the thing precisely.
**The failure mode is not "wrong number", it is "unexamined number", and the
harness cannot tell them apart without a second measurement.** No amount of
inspecting the first measurement gets there.

### Two things checked against the code, which moved the conclusion

The paragraph that stood here claimed the missing piece was *knowing when to
spend a subagent*. Half of that is true and the half that matters is not.

**True: nothing but the model can spawn a verifier.** `verify` is reachable only
through the `task` tool (`warden/subagents.py`, `BUILT_INS["verify"]`); there is
no harness call site. So the decision to get a second opinion is entirely the
model's, and a model that has spent an hour becoming convinced is the one making
it.

**But the incident passes every structural test the harness could apply.** Walk
it against `Warden._note_tools` and `_unverified`: files were written, a command
was run afterwards, it exited clean, the result was reported. `checked_at >
wrote_at`, so the end-of-run verification nudge correctly does not fire. A
harness that auto-spawned `verify` on the existing trigger would still have
missed this one, because by every signal available the job went fine. Nothing
went wrong structurally. One number was wrong semantically, and semantics is the
thing the harness does not have.

**And the second measurement was already free.** `graph_overview` "returns
counts" (`tools/graph.py`), over the same import graph the agent was hand-rolling
a counter for. The correct figure was one read-only, parallel-safe tool call
away, in a tool the agent already had. So cost was never the obstacle, which
means "when to spend a subagent" is not the question either — nothing needed
spending.

### What that leaves

The obstacle was not availability, not cost, and not a missing check. It was
that nothing made the agent *seek* a second number when it had one it liked.
That is a disposition rather than a decision point, and a harness cannot install
a disposition — it can only make the second measurement cheap (graph tools, and
they were), make a context available that has not already rationalised anything
(the `verify` subagent, and it is), and refuse an unsupported claim when one is
finally made (`audit_verification`, and it does).

All three exist. **This gap is therefore not an unbuilt mechanism; it is the
residue** — and the residue has a section of its own at the bottom of this
document, which says that none of this makes a weaker model reason better. Gap 5
is where that limit shows up first, and filing it as pending work implies a fix
that the rest of the document argues cannot exist.

Left open, but reclassified: not "hardest item on the list", **the item that is
not on the list**. If it moves, it will move because a second measurement became
something the loop takes rather than something the model chooses — and the
honest next step is not to build that, it is to find out how often an agent with
`graph_overview` in its toolset reaches for it unprompted. That is a measurement
this document does not have and should not guess at.

---

## What shipped on 2026-08-07

| Mechanism | Shape |
|---|---|
| A rejected call is answered with the call it should have made, signature included | failure made legible |
| `<tool_error>` says the arguments were valid and the tool broke | failure made legible |
| An approval states that it does not extend to the next action of the same kind | failure made legible |
| Operator instructions copied through compaction by the harness, not the summarizer | claim refused |
| The loop sweeps for files that moved underneath it, and announces each change | harness observes |
| Detecting a change does not forgive the stale edit — two fields, on purpose | failure made impossible |

---

## What shipped on 2026-08-06

For completeness, since these were the same investigation.

| Mechanism | Shape |
|---|---|
| Loop notices "wrote code, ran nothing" and asks once, at the end | harness observes |
| Mid-loop reminders: repeated identical failure, unverified edits, no plan | harness observes |
| `verify` subagent — fresh context, cannot edit, must show commands | claim refused |
| A PASS with no command behind it is rejected before reaching the parent | claim refused |
| Denial says what is legitimate next, and that a shell is not a way around a refusal | failure made legible |
| Permission prompt states the scope it grants, and refusal can carry an instruction | failure made legible |
| Graph tools withheld when there is no graph | failure made impossible |
| POSIX shell everywhere, so one dialect works on every machine | failure made impossible |

---

## Order of work

Ranked by cost of the gap, not by effort. Items 1–4 shipped on 2026-08-07 in
this order; the estimates are left as written so the next list can be read
against how this one went.

1. ~~**Errors that teach the correct call.**~~ Verified broken, hit constantly
   on a provider that ignores `required`, and the smallest change on the list.
   *Estimate held. It was the smallest and it needed one new module.*
2. ~~**Compaction structure.**~~ Highest value, invisible when it fails, and the
   thing that decides whether a long session holds together. *Estimate wrong in
   an instructive way: the prompt was already right, and the gap was that a
   prompt is not a mechanism.*
3. ~~**File-changed reminder.**~~ The channel exists; this is one rule.
   *Estimate wrong: the channel injects, but nothing was watching, so it needed
   a sweep and a second field on the cache. Not one rule.*
4. ~~**Authorisation scope.**~~ A paragraph, beside the denial guidance.
   *Estimate held exactly.*
5. **Plausibility checking.** Removed from the list rather than deferred on it.
   Checked against the code: the incident passes every structural test the
   harness has, and the independent measure it needed was already one free tool
   call away. There is no unbuilt mechanism here — see the section for the walk
   through, and *The honest limit* for where it actually belongs.

---

## What was actually read

Stated so the next person knows how much of this is evidence and how much is
inference. The first draft of this document was written from greps and memory,
and its reference side was thin.

Read in full: `verificationAgent.ts`, `utils/toolErrors.ts`,
`bashToolUseOptions.tsx`, `FallbackPermissionRequest.tsx`, `builtInAgents.ts`,
`exploreAgent.ts`, `DENIAL_WORKAROUND_GUIDANCE`.

Read in part: `services/tools/toolExecution.ts` (the validation and permission
path), `services/api/withRetry.ts` (classification and overflow parsing),
`services/compact/prompt.ts` (`BASE_COMPACT_PROMPT`), `constants/prompts.ts`
(structure and the risk block), `utils/messages.ts` (`wrapInSystemReminder`).

Not read, and each may hold something: the query loop itself (`query.ts`),
`Tool.ts` in full, the memory subsystem (`memdir/`), hooks, and the ~30 other
named prompt constants enumerated but not opened.

**Added 2026-08-07**, on the Forge side, since two of this document's claims
turned out to rest on greps. Read in full while closing the gaps:
`warden/compaction.py`, `warden/dispatch.py`, `warden/engine.py`,
`warden/filestate.py`, `warden/permissions.py`, `warden/reminders.py`,
`warden/state.py`, `warden/subagents.py`, `warden/tool.py`, `tools/files.py`,
`cell/base.py`. That is what corrected Gap 2 (the summary prompt was already
nine sections) and Gap 5 (the second measurement was already free). Both had
been asserted from search results, and both were wrong in the same direction —
assuming absent what was present.

## The honest limit

None of this makes a weaker model reason better. It makes the loop around it
fail loudly, early, and legibly instead of quietly, late, and confidently.

That is the whole of what the reference does, and the reason the same model
behaves differently inside it.

Gap 5 is where that limit becomes visible, and it is worth keeping in view now
that the other four are closed. An agent that writes its own instrument, runs
it, reads one number off it and believes it has done nothing the loop can catch:
it wrote, it ran, it succeeded, it reported. Every mechanism in this document
would pass that job. The number was wrong and the harness has no way to know,
because knowing would require understanding what was being counted.

So the list ends where it should. Four gaps were mechanisms that were missing.
The fifth was never a mechanism, and treating it as one for two drafts was the
most useful mistake in this investigation — it is what forced the distinction
between a failure a loop can observe and a failure only a reader can.
