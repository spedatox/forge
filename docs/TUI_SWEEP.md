# The reference's terminal UI, swept — and what Forge's is missing

A full pass over the reference's UI layer (144 components, `screens/REPL.tsx` at
5,006 lines, the `ink/` renderer, `PromptInput/`, `Spinner/`) against Forge's
`forge/tui/` (3,481 lines across eleven modules).

The two are architecturally incomparable and that is the first thing to say. The
reference is a React application on a custom Ink reconciler with a virtual DOM,
a 50 ms animation clock, a layout engine, and a hit-tester. Forge writes ANSI
escapes to stdout with the standard library. Almost none of the reference's
*code* is transferable.

Its **behaviours** are, and those are what this documents. The filter applied
throughout: does this survive the translation to a line-based renderer that owns
one transient row and otherwise appends to scrollback?

---

## Where the two agree, arrived at separately

Worth stating first, because it calibrates everything below.

**Neither takes the alternate screen buffer.** This is the single biggest UI
decision and both made it the same way. Completed turns commit to the terminal's
own scrollback, so scroll, select, copy, and shell-history interleaving all keep
working, and the transcript survives the session. Forge's `input.py` says so
explicitly and cites the reference; the reference's Ink renderer maintains a
static region above a dynamic one for the same reason.

**One live row, redrawn in place.** Reference: Ink's dynamic region. Forge:
`ansi.transient()` plus a strict clear-before-write contract. Same shape.

**The distinction the layout protects is model-said versus harness-did.** The
reference indents and dims tool activity under a bullet; Forge uses a margin
bullet and a `⎿`-style gutter. Same principle, near-identical output.

Forge additionally has several things the reference does not need but that are
right for its environment: ASCII fallbacks for every glyph (`ansi.GLYPHS`),
because Windows consoles still default to cp1252 and an `UnicodeEncodeError`
while drawing a banner would kill the session before anything was typed; and a
**loud** degradation path when `prompt_toolkit` fails to construct, because a
silently-degraded prompt looks identical and reads as a completion bug.

---

## A. The live line

### A1. Stall detection — the spinner turns red

`Spinner/useStalledAnimation.ts`. The hook tracks time since the response length
last increased. After **3 seconds with no new tokens and no active tools** it
begins interpolating the spinner glyph and its message toward `ERROR_RED`, over
a further 2 seconds:

```ts
const isStalled = timeSinceLastToken > 3000 && !hasActiveTools
const intensity = isStalled
  ? Math.min((timeSinceLastToken - 3000) / 2000, 1)
  : 0
```

The glimmer animation is also killed while stalled (`glimmerIndex = -100`), so a
stalled spinner stops shimmering as well as going red. Two independent signals
pointing the same way.

`hasActiveTools` suppresses it entirely — a two-minute test run is not a stall,
and colouring it red would train the operator to ignore the colour.

**Why it matters more than it looks.** Every spinner answers "am I alive". Only
this one answers "is anything actually arriving". A rotating verb proves the
event loop is turning, which is precisely what remains true when the provider
has silently stopped sending. The operator's decision — keep waiting, or ctrl-C
and retry — depends on exactly that distinction.

**Forge: gap.** `spinner.py` rotates a verb every 6 s and counts elapsed seconds.
A dead connection and a thinking model render identically.

### A2. The token counter appears late, and animates

`SHOW_TOKENS_AFTER_MS = 30_000` — no token count for the first thirty seconds. A
counter on a short turn is noise; on a long one it is the main evidence of
progress. The counter also eases toward its true value rather than jumping
(increment 3 when close, 50 when far), so it reads as flowing rather than
stepping.

**Forge:** shows the count once it passes 200 tokens, which is a different rule
solving the same problem. Not a gap — arguably better, since Forge's proxy is
character-derived and available immediately.

### A3. Thinking has its own state machine with a minimum display time

Thinking status shows for **at least 2 s** even if the model thought for 200 ms,
then shows the duration for 2 s, then clears. Explicitly "to avoid UI jank".

A general principle worth naming: **a status that can change faster than it can
be read needs a floor, not just a value.**

---

## B. The input

### B1. Large pastes are truncated with a placeholder, not sent whole

`inputPaste.ts`. Over `TRUNCATION_THRESHOLD = 10_000` characters, the input keeps
500 characters of head and 500 of tail and replaces the middle with:

```
[...Truncated text #1 +4,182 lines...]
```

The excised text is stashed under that id in `pastedContents` rather than
discarded, so the reference can re-attach it later.

**Forge: gap.** A paste of any size goes to the model whole. Pasting a stack
trace, a log file, or a CSV is a normal thing to do at a prompt, and Forge's only
defence is the context ledger noticing afterwards.

### B2. The placeholder is a teaching surface

`usePromptInputPlaceholder.ts` — the empty-input placeholder is chosen by state:
`Press up to edit queued messages` (shown at most three times, counted in global
config), an example command before the first submit, `Message @name…` when
viewing a teammate. Discoverability delivered at the moment it applies, then
retired so it does not become furniture.

**Forge:** has the equivalent as a static hint line under the input
(`_hint_line`), with the same reasoning written in its docstring — "only what
changes with state or is otherwise undiscoverable". Present in spirit; the
"retire after N showings" counter is the part Forge lacks, and it is minor.

### B3. Double-press to exit

`useDoublePress.ts`, `DOUBLE_PRESS_TIMEOUT_MS = 800`. First ctrl-C shows
`Press Ctrl-C again to exit` in the footer; a second within 800 ms exits; the
message clears on timeout.

**Forge: gap, and there is a doc/code mismatch behind it.** `input.py`'s `read()`
docstring says "Ctrl-D (or ctrl+C at an empty line) ends the session". The code
returns `Submission("prompt", "")` on `KeyboardInterrupt` unconditionally, and
`_loop` does `if not entry.text: continue`. So ctrl-C at the prompt silently
redraws it and there is no ctrl-C exit at all. Ctrl-D works. Since ctrl-C ctrl-C
is most terminal users' reflex for leaving, the current behaviour is a small
dead end that also makes the docstring wrong.

### B4. Queued input while the model works

The reference accepts typing during a turn, shows the queue above the prompt
rendered as real messages, caps task notifications at three visible lines with a
`+N more` synthetic overflow row, and lets ↑ pull a queued message back for
editing.

**Forge:** not applicable in the same form — `_run_turn` awaits the whole turn
and the prompt is not accepting input during it. This is a genuine capability
gap but a large one (it needs the input bar reading concurrently with the
engine), and it is a feature rather than a resilience mechanism. Recorded, not
recommended.

---

## C. What happens when a turn ends

### C1. Completion notifications

`services/notifier.ts` plus `ink/useTerminalNotification.ts`. Channels, in
preference order: iTerm2 OSC, Kitty OSC 99, Ghostty OSC 777, and plain `BEL`.
The bell is written **raw and unwrapped**, with the reason in a comment:

> Raw BEL — inside tmux this triggers tmux's bell-action (window flag).
> Wrapping would make it opaque DCS payload and lose that fallback.

**Forge: gap.** A ten-minute turn finishes in silence. The operator either
watches the whole thing or discovers it finished later. A terminal bell is four
bytes and turns Forge into something you can leave running.

### C2. A past-tense completion verb

`TURN_COMPLETION_VERBS` — `Baked`, `Brewed`, `Churned`, `Cogitated`, `Worked` —
chosen to read naturally with a duration: "Worked for 5s".

**Forge: gap in substance, not in whimsy.** Forge prints iteration and token
counts only when `verbose` is on or the turn did *not* complete. A normal
successful turn ends with no duration and no cost signal at all. The operator
learns what a turn cost by running `/cost` afterwards, which means in practice
they do not.

### C3. Context-pressure warning

`TokenWarning.tsx` fires at `threshold - 20_000` tokens — before autocompact, not
when it happens — and includes the remaining headroom.

**Forge: gap.** `status.py` renders `18% ctx` on every prompt, which is passive
and reads as furniture at every value. Nothing marks the crossing into the region
where the next turn will compact. A threshold crossing is an event; a percentage
is a gauge, and the two are not substitutes.

---

## D. Deliberately not adopting

- **The Ink renderer.** A virtual DOM, reconciler, layout engine, hit-tester,
  and 50 ms animation clock for a terminal. Correct for an app with dialogs,
  pickers, and a fullscreen mode; catastrophic overkill for an appending
  transcript, and it would cost Forge the stdlib-only install its `ansi.py`
  docstring defends.
- **Shimmer, glimmer, and colour interpolation.** The reference sweeps a
  brightness gradient along the spinner text at 50 ms. It is pretty. It also
  requires an animation clock and truecolour, and it communicates nothing that
  the frame rotation does not. The *stall* interpolation is different — that one
  carries information, and it is the one being adopted.
- **144 components' worth of dialogs.** Model pickers, theme pickers, onboarding
  flows, IDE dialogs, marketplace browsers. Forge has one dialog shape (the
  arrow-selectable permission prompt) and slash commands for the rest.
- **The 30-second token gate.** Forge's 200-token floor answers the same
  question earlier and with a signal it already has.

---

## What to implement, ranked

| # | Gap | Where | Cost |
|---|---|---|---|
| 1 | Spinner does not distinguish stalled from working | `tui/spinner.py` | S |
| 2 | No completion notification | new `tui/notify.py` | S |
| 3 | A completed turn reports nothing | `tui/repl.py` `_report` | XS |
| 4 | No context-pressure warning, only a gauge | `tui/status.py` | S |
| 5 | ctrl-C at the prompt does nothing, docstring says otherwise | `tui/input.py`, `tui/repl.py` | S |
| 6 | Large pastes go to the model whole | `tui/input.py` | S |

All six are small. Ranked by how often the absence is felt: 1 and 2 change what
it is like to run a long job at all; 3 and 4 are information the operator
currently has to go and ask for; 5 is a papercut with a wrong docstring attached;
6 is a rarely-hit cliff with an expensive landing.

---

## Shipped — 2026-08-07: all six

| # | What landed | Where |
|---|---|---|
| 1 | `STALL_AFTER_S = 4.0`; the line turns yellow and says `nothing received for Ns` | `tui/spinner.py`, `tui/render.py` |
| 2 | iTerm2 / kitty / Ghostty OSC, raw `BEL` fallback, only past 30 s | new `tui/notify.py` |
| 3 | `_turn_summary` — duration, steps, output tokens on every completed turn | `tui/repl.py` |
| 4 | `pressure_warning` at 75 %, once per crossing | `tui/status.py`, `tui/session.py` |
| 5 | ctrl-c twice within 1.5 s exits; one press says so | `tui/input.py` |
| 6 | `fold_paste` over 10 k chars, keeping 500 each end, spilling the rest to a file | `tui/input.py`, `tui/repl.py` |

Four decisions where the reference was not copied straight:

- **Stall is yellow, not red, and it says so in words.** Red is already the
  error colour in `ansi.py`; a stall is not an error, it is an absence. And the
  tail carries `nothing received for 12s` as text, because colour is off under
  `NO_COLOR`, in a pipe, and on a dumb TERM — which are the same places a hung
  connection is hardest to diagnose.

- **The tool flag is cleared when the batch empties, not when the next token
  arrives.** `render.py` sends `set_status("Thinking")` once `_in_flight` drains.
  Without that, the wait for the next model response would still be credited to
  a tool that had already finished — which is precisely the window a stall
  happens in.

- **The paste spill writes the whole text to a file and names it.** The
  reference stashes the elided middle in memory under an id. Forge writes the
  full paste to `.forge/pastes/` and puts the path in the placeholder, matching
  the contract oversized tool results already have. A fold that says nothing
  about where the rest went is a quiet lie about what was provided.

- **The already-warned flag lives on `Session`, not in a module-level set.** The
  first implementation keyed it on `id(session)` and a test caught it
  immediately: CPython reuses the address of a collected object, so a later
  session silently inherited the suppression. That would never have reproduced
  by hand and would have surfaced as "the warning sometimes doesn't fire".

`tests/test_tui_ux.py` — 22 tests, including the three cases that are the whole
point of the respective mechanism: a running tool is never a stall, a slow second
ctrl-c is a fresh first, and an unwritable spill still folds.

Suite: 805 passing, 3 skipped. Demo end-to-end: passes.

**Not built, and still not recommended:** queued input during a turn (B4). It
needs the input bar reading concurrently with the engine, which is a real
architectural change, and it is a feature rather than a mechanism that keeps a
job honest.

---

## The render engine, revisited — 2026-08-07

Section D above called an Ink-equivalent "catastrophic overkill". That was
answering the wrong question. It argued against cloning Ink's *reconciler*; the
question worth asking was whether Forge should have the *capability*, and the
answer is yes, because Forge already had the pieces:

- `ui.py`'s own docstring says "Rich is the equivalent here", and Rich is
  already a declared dependency (`tui = [...]`, rich 15.0.0 installed).
- Rich renders **inline**, the same property both codebases protect.

So the gap was never a missing engine. Of Ink's three contributions — flexbox
layout, inline rendering, and a **multi-row dynamic region** — Forge had the
first two and only ever used one row.

That last one was costing real visibility:

```
MAX_TOOL_CONCURRENCY = 10      warden/engine.py
MAX_CONCURRENT       = 4       warden/subagents.py
```

A parallel batch rendered as `Running grep` while nine calls were in flight and
unnamed. Subagents were worse: `render.py` had **no `_on_subagent` handler at
all**, so those events fell through the `_on_{kind}` lookup and vanished. Four
children could run for minutes and produce nothing on screen.

### What landed

`forge/tui/live.py` — `LiveRegion`, a drop-in for `Spinner` (same surface, so
`render.py`, `repl.py` and the oracle are unchanged) that draws a header plus a
row per in-flight call:

```
  ◒ Chewing…  (ctrl-c to interrupt · 47s · ↓ 3.1k tokens)
  ├─ ◆ verify  check the truncation guard                     48s
  ├─ ● Run(pytest -q tests/test_truncation.py)                 32s
  ├─ ● Grep(retry_attempt)                                      4s
  └─ ● Read(forge/warden/engine.py)                             3s
```

Claude Code's shape — `├─`/`└─`, the `●` bullet, `Name(target)` — in Forge's
greys. Plus `_on_subagent` in `render.py`, which now exists.

### Four decisions

- **`rich.Live` is not used; Rich is the layout engine only.** It composes rows
  to strings and `ansi.rewind` draws and erases them. `rich.Live` wants to own
  stdout, and stdout is already shared with prompt_toolkit's `patch_stdout` and
  every direct `ansi.write` in the renderer. Two owners is a corruption bug that
  would surface during a parallel batch. Keeping the clear-before-write contract
  the renderer already honours means the region generalised from one row to N
  and nothing else changed.

- **The header went grey too.** A cyan header over grey rows is two designs in
  one object. It also restores the palette rule the rest of the TUI keeps —
  structure grey, colour spent only on meaning. A permanently-cyan spinner
  spends an accent on the least surprising fact on screen. The stall state stays
  yellow, because that one means something.

- **Finished rows leave immediately.** The renderer already commits every call
  and result to scrollback; a region that also showed them would print each call
  in a batch twice.

- **Longest-running leads, and the tail is capped at 8.** With ten in flight the
  only question worth asking at a glance is which one is not coming back. Past
  the cap the tail collapses to `└─ … +4 more running`, because a rewind taller
  than the terminal eats scrollback that was never ours.

`tests/test_live_region.py` — 19 tests. The load-bearing one is
`test_a_frame_is_erased_before_the_next_is_drawn`, which runs the real tick loop
and asserts net committed rows is exactly zero: leak one row per frame and a
five-minute turn buries the conversation.

Suite: 824 passing, 3 skipped. Demo end-to-end: passes.
