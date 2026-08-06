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
| Read-before-edit enforced by the harness | Not advice — the edit fails |
| Iteration ceiling ends with a handover, not a severed head | Added 2026-08-06 |
| Per-turn tool refresh | A tool that appears mid-job becomes available |

---

## Gap 1 — Errors that name the exception instead of the fix

**VERIFIED.** Four malformed calls, and what the model receives:

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

**What it should say:** what the tool needs, what arrived, and a correct
example. `read_file needs "path" (string). You sent no arguments. Example:
{"path": "src/main.py"}`.

**Cost of the gap:** one wasted call per malformed invocation, minimum, and an
unknown number of retries when the model guesses the same wrong shape twice.

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

Ranked by cost of the gap, not by effort.

1. **Errors that teach the correct call.** Verified broken, hit constantly on a
   provider that ignores `required`, and the smallest change on the list.
2. **Compaction structure.** Highest value, invisible when it fails, and the
   thing that decides whether a long session holds together.
3. **File-changed reminder.** The channel exists; this is one rule.
4. **Authorisation scope.** A paragraph, beside the denial guidance.
5. **Plausibility checking.** Unsolved. Worth designing before building.

---

## The honest limit

None of this makes a weaker model reason better. It makes the loop around it
fail loudly, early, and legibly instead of quietly, late, and confidently.

That is the whole of what the reference does, and the reason the same model
behaves differently inside it.
