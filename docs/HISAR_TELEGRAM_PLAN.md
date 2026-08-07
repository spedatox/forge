# Hisar tools, and Telegram notification

Two capabilities Forge is missing, both of which already exist on the other side
of a contract Forge does not call.

---

## Part 1 — Hisar

### What is already true

The vault server has **already been widened** for machines. `hisar/server/auth.py`
makes the argument itself:

> an agent that can only WRITE is close to useless: it cannot find the report it
> filed last week, check whether a folder already exists, or read a document the
> owner asked it to work from. Deposit-only access made the vault a drop box
> rather than a shared workspace.

The permission model is three tiers, and it is not ours to redesign:

| Operation | Endpoint | Machine (Forge) | Guard |
|---|---|---|---|
| list, download | `/files/list`, `/files/download` | **anywhere** | `require_reader` |
| deposit, upload, mkdir | `/deposit`, `/files/upload`, `/files/mkdir` | **`/SPEDA` and `/Forge` only** | `authorize_write` |
| delete, rename | `/files/delete`, `/files/rename` | **never** | `require_owner` |

Auth is `X-Hisar-Token`. `MACHINE_WRITE_SCOPES = ("/SPEDA", "/Forge")`.

### What is missing

Forge has **no Hisar tools at all**. The only artifact anywhere is
`hisar/integrations/speda_hisar_skill.py`, which is a reference implementation
targeting speda-mark6 (Igor) and explicitly says it does not run where it lives.
Its description still says the vault is "write-only for agents", which the
server has since stopped being — the doc drifted from the contract, which is
exactly what that file was written to prevent.

So "operate freely" needs no server change. It needs three tools.

### The tools

`forge/tools/hisar.py`, modelled on `tools/web.py` — networked, key-gated,
degrading to a legible `is_error` rather than raising.

- **`hisar_list(path="/")`** — what is in the vault. Reads anywhere.
- **`hisar_read(path)`** — download a file's text. Reads anywhere. Capped like
  `web_fetch`, spilling oversize content to the Cell workspace rather than the
  context window.
- **`hisar_deposit(source, folder="/Forge", filename=None)`** — put a file in.
  Writes under `/SPEDA` or `/Forge` only.

**No delete and no rename tool.** The server answers 403 to a machine on both,
always. A tool that can only ever fail is worse than an absent one: it costs a
call to discover, and it teaches the model that Hisar tools are unreliable.
Forge already holds this line — graph tools are withheld when there is no graph
rather than offered and failing.

**The write scope goes in the description.** `/SPEDA` and `/Forge`, named, so
the model does not spend a call finding the boundary. Reading is anywhere and
the description says that too, because the asymmetry is surprising and an agent
that assumes symmetry will either not look or not try.

**Withheld entirely when `HISAR_MACHINE_TOKEN` is unset.** Same rule as the
graph tools and for the same reason.

**Warden-side, not in the Cell.** `web.py` draws this line already: the Cell's
network posture governs code the model *writes and runs*; the harness's own
instruments are separate. The vault holds the owner's documents and must not be
reachable from generated code that happens to be running with network on.

---

## Part 2 — Telegram

### What is already true

Mark VI has a full Telegram stack (`packages/igor/app/telegram/`, documented in
`docs/TELEGRAM_ARCHITECTURE.md`): one bot per agent, tokens keyed by `agent_id`,
inbound chat, outbound delivery, notifications. It is live.

And it already knows about Forge. From that document:

> **Optimus stays external.** Its bot's webhook lands on this backend, but the
> turn is proxied through the existing `core/external_proxy.py` path — identical
> to how `/chat/optimus` already behaves.

So **inbound already works**: you can message Optimus on Telegram today and it
reaches Forge through Mark VI.

### What is missing

**Outbound, initiated by Forge.** Nothing lets a Forge peer say something the
owner did not ask for — which is precisely "let me know when it's finished
prototyping". Today a `forge connect` job finishes and the only trace is a
journal line.

This is the same gap `tui/notify.py` closed last week, one surface over:

| Surface | Operator is | Answer |
|---|---|---|
| `forge chat` | at the terminal, looked away | terminal bell / OSC (**built**) |
| `forge connect` | not at the terminal at all | Telegram (**this**) |

Stating the pair matters because it fixes the policy: the bell already decided
*when* to fire (past 30 s, opt-out via env, silent when not a tty). Telegram
should reuse that judgement rather than invent a second one.

### The shape

- **`forge/notify/telegram.py`** — a thin Bot API client. `sendMessage`, and
  `sendDocument` for a deliverable. Persistent `httpx` client, chunked at 4096,
  never raises into a turn.
- **Token per agent**, `FORGE_TELEGRAM_TOKEN_<AGENT>` falling back to
  `FORGE_TELEGRAM_TOKEN`, plus `FORGE_TELEGRAM_CHAT_ID`. Config, not profile —
  the same split Mark VI uses (identity in the profile, secrets in env).
- **Fires on job completion in the peer path** (`gate/runner.py`), carrying the
  same summary line `_turn_summary` already builds for the TUI.
- **A `telegram_send` tool**, so an agent can deliberately message mid-job —
  Mark VI has exactly this as a Tier-1 skill and the parity is the point.

### Two things to be careful about

**Sending is outward-facing.** A tool that messages the owner is not a local
side effect. It gets the same treatment as any other: withheld when
unconfigured, and never fired by a test.

**Don't duplicate Igor's bot.** If `FORGE_TELEGRAM_TOKEN` is the same token
Igor uses, both processes long-poll or webhook the same bot and messages get
delivered twice or stolen. Forge only ever *sends* — no `getUpdates`, no
webhook — which sidesteps it entirely, and that restriction should be written
into the module rather than assumed.

---

---

## Part 3 — Asking, not just telling

Added after the plan was written, because notification turned out to be half the
requirement: a server-side job that is *prototyping* hits forks it should not
pick alone.

### What was already there

More than expected. `ChannelOracle` (Seam 2) already parks a question inside one
tool dispatch, ships a `permission_request` frame over the Mark VI socket, and
resolves when the answer returns — and the frame already carries a `chat_id`. So
**approval asks already work server-side.** Nothing needed building for those.

### What was missing

`Answer` is `(approved: bool, remember: bool, note: str)` — a verdict. There was
no way for an agent to ask *"REST or WebSocket?"* and get prose back. That is
the "or idea" half of the request, and it needed a second shape.

### The asymmetry that drove the design

The two kinds of ask fail in **opposite** directions, and flattening them would
get one wrong:

| | unanswered means | why |
|---|---|---|
| `ask` → `Answer` | **denied** | absence of an operator must never become consent |
| `consult` → `Reply` | **unanswered, proceed** | there is nothing to refuse; blocking on nobody's opinion is a deadlock, not a safeguard |

So `Reply.guidance` tells the model to choose, proceed, and *say in its report
that it decided alone*. It also says "do not ask again" — a model that reads
silence as "retry" turns one unanswered question into a loop against someone
asleep.

`abandon_all` follows the same split: permissions resolve to DENIED, questions
to UNANSWERED.

### Shipped

| Piece | Where |
|---|---|
| `Reply`, `UNANSWERED`, `has_consult` | `warden/oracle.py` |
| `ChannelOracle.consult` / `.answer`, `question` frame | `warden/oracle.py` |
| `AutoDenyOracle.consult` → unanswered, never denied | `warden/oracle.py` |
| `question_response` frame routing | `gate/peer.py` |
| `TerminalOracle.consult` — numbered options, enter to decline | `tui/session.py` |
| `ask_operator` tool, rationed by description | `tools/ask.py` |

`ask_operator` is `READ_ONLY = True` so plan mode permits it — asking what to
build is exactly what a review pass is for — and `CONCURRENCY_SAFE = False`,
because two questions arriving at once is the shape that trains an owner to stop
reading them.

### The one piece not in this repo

Mark VI has to render the new `question` frame and reply with
`question_response: {ask_id, text}`. Forge's side is complete and degrades
correctly without it — an unrecognised frame means no answer, which means the
agent decides for itself and says so. Nothing hangs.

---

## Order

1. **Hisar tools.** Fully specified by a contract that already exists, no
   secrets to invent, and testable against a fake server.
2. **Telegram client + completion notification.** Needs a bot token to test
   live; the client and the policy are testable without one.
3. **`telegram_send` tool.** Last, because it is the only piece that lets the
   model initiate an outward action, and it should land on top of a delivery
   path already known to work.
