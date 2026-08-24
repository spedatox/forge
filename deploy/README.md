# Deploying the Forge (placement plan, phase H4)

The Forge runs as a **systemd unit on the host**, not as a container. That is
forced by the Cell design: `DockerCell` bind-mounts the job workspace into each
throwaway container, so the paths the Forge hands to `docker run` must be *host*
paths. Running the Forge itself inside a container would mean giving that
container the Docker socket — handing host root to whatever the agent generates,
which is the exact thing the Cell exists to prevent.

```
systemd: forge@<agent>.service              (host, root — needs the docker socket)
  └─ python -m forge connect --agent <agent>
       └─ docker run --rm --network none --cap-drop ALL --user 1000  ← one per job
            └─ /workspace  ⇄  /opt/hisar/vault/Forge/workspaces/<agent>
```

---

## 1. Install

```bash
git clone https://github.com/spedatox/forge-mark1.git /opt/forge-mk1
cd /opt/forge-mk1
./install.sh --extras providers       # .venv + deps, then verifies
docker pull python:3.12-slim          # the Cell image
```

The installer is the same script a developer runs on a laptop; `--extras
providers` is the only difference, because a server has no terminal UI to
install for. It ends by listing the agents, so a box that cannot start the
Forge says so here rather than at the first `systemctl start`. By hand it is
`python3 -m venv .venv && ./.venv/bin/pip install -e ".[providers]"` — the unit
file calls `.venv/bin/python` either way.

The `providers` extra is **not** optional in practice on this deployment. It
pulls the OpenAI client, which every non-Anthropic provider shares (OpenAI,
Gemini, z.ai, DeepSeek, Ollama). Install without it and a plain `pip install -e .`
looks fine until a model pin resolves to one of them, then dies with
`ModuleNotFoundError: No module named 'openai'` mid-job. Anthropic-only
deployments can skip it; this one cannot, because Igor's model pins can route to
any configured provider.

Providers verified against Igor's env: Anthropic, OpenAI, z.ai. Gemini has no
`GEMINI_API_KEY` anywhere on the box — add it to Igor's `.env` before pinning
anything to Gemini.

## 2. Configure

**Credentials are not duplicated here.** The provider keys and `SPEDA_API_KEY`
are the same values Igor uses, so the unit loads Igor's `.env` first and this
file second. Copying them instead produced exactly the failure you would expect:
a model pin resolved to `openai:…` and the peer died with *"openai model
requested but its API key is not set"*, because only the Anthropic key had been
copied across. Nothing is widened by reading Igor's file — the Forge runs as
root on the same host and could always read it.

`/opt/forge-mk1/.env` therefore holds only Forge-specific settings, mode 600:

```ini
FORGE_CELL_BACKEND=docker
FORGE_CELL_IMAGE=python:3.12-slim
FORGE_WORKSPACE_ROOT=/opt/hisar/vault/Forge/workspaces
```

`SPEDA_WS_URL` is not set here either — the unit derives it per agent.

Per-agent overrides go in `.env.<agent>` (optional, loaded last) for env-level
settings. The Cell IMAGE, though, is a profile field (`[cell].image`), and a
value there wins over the global `FORGE_CELL_IMAGE` — so an agent that needs its
own toolchain names its image in its profile and needs no `.env.<agent>` at all.

Two agents ship a baked lab image, each built from a Dockerfile in this folder.
Both run their Cell as root (a throwaway, per-job, host-isolated container), so
the bake only covers the routine toolchain — anything else the job `apt-get`s at
job time:

```bash
cd /opt/forge-mk1
# Centurion — headless Kali security toolbox
docker build -f deploy/cell-centurion.Dockerfile -t forge-cell-centurion:latest deploy/
# Optimus — polyglot dev lab (Python/Node/Go + build tools)
docker build -f deploy/cell-optimus.Dockerfile   -t forge-cell-optimus:latest   deploy/
```

The tags match what each profile's `[cell].image` already points at, so once
built they are picked up with no further config. Rebuild after changing a
Dockerfile; under the subprocess backend (Docker-less host) the images are
ignored and commands use the host's own tools.

The workspace root inside the Hisar vault is the whole of the placement plan's
passive H4 layer: live Cell workspaces are browsable on the web desktop with no
code at all.

## 3. Vault permissions

Hisar's container runs as uid 10001 / **gid 999**. Group-own the Forge subtree to
that gid and set the setgid bit, so everything created under it stays manageable
from the file desktop:

```bash
chgrp -R 999 /opt/hisar/vault/Forge
chmod -R g+rwX /opt/hisar/vault/Forge
find /opt/hisar/vault/Forge -type d -exec chmod g+s {} +
```

The Cell runs as uid 1000 and drops every capability, so it cannot chown its own
bind mount — `DockerCell` does that from the host side at start, and probes
writability before reporting the Cell started. A job that cannot write its
workspace now fails loudly at startup instead of on its first write.

## 4. Service

The forge unit is **templated on the agent id**, so each agent is one more
instance rather than one more file:

```bash
cp deploy/forge@.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now forge@optimus forge@centurion
journalctl -u 'forge@*' -f      # expect: peer_registered, once per agent
```

The free-claude-code proxy is a separate unit — one instance serves every Cell:

```bash
cp deploy/fcc-server.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now fcc-server
```

`EnvironmentFile` is load-bearing: `forge/__main__.py` reads `os.environ`
directly and loads no `.env` itself.

## 5. Verify

```bash
systemctl is-active forge@optimus forge@centurion fcc-server
journalctl -u 'forge@*' -n 10 | grep peer_registered
journalctl -u fcc-server -n 5 | grep -i 'listening\|started'
curl -sS -H "X-API-Key: $SPEDA_API_KEY" localhost:8000/agents   # forge peers online
curl -sS http://localhost:8082/health                            # proxy reachable
```

A restart should take well under a second. If it takes 20s and the journal says
`stop-sigterm timed out`, the peer is ignoring SIGTERM — that was a real bug
(the stop event went unobserved while the connection was healthy) and it is
covered by `test_stop_request_ends_an_idle_connection`.

## 6. Agents

| Agent | Cell network | Notes |
|---|---|---|
| `optimus` | **off** (`--network none`) | coding; the default posture |
| `centurion` | **on** | recon and scanning need it — declared in its `profile.toml`, not in any env file |

Centurion's cells reach the internet. That is deliberate and profile-declared,
but it is the one agent whose sandbox is not network-isolated, so it is worth
knowing before dispatching to it. Its profile also expects security tooling
(nmap, nikto, …) in the Cell image; until one is built it shares Optimus's
`python:3.12-slim`, which carries none of it — see the `.env.centurion` seam
in §2.

## 7. Claude Code proxy (free-claude-code)

The `claude_code` tool delegates to the real Claude Code CLI, which must be
installed in the Cell image alongside Node.js. Its API calls route through the
`fcc-server` proxy on the host, reachable from inside a container at the Docker
bridge gateway (`172.17.0.1:8082`).

### Host setup

```bash
# Install the proxy server
npm install -g free-claude-code

# Start it
systemctl enable --now fcc-server
```

The proxy reads provider keys from Igor's `.env` (same `EnvironmentFile` the
forge unit uses). Which provider Claude Code actually hits depends on how the
proxy's admin UI is configured — point it at DeepSeek (key already in Igor's
env) or whichever provider you want the delegated work to use.

### Cell image requirements

The default `python:3.12-slim` image has no Node.js, so the `claude_code` tool
reports "fcc-claude is not installed" and the model falls back to the `task`
tool. To make it available, either:

**A. Install at runtime** (the agent does this once per session):
```bash
apt-get update && apt-get install -y nodejs npm
npm install -g @anthropic-ai/claude-code free-claude-code
```

**B. Build a custom image** that bakes it in:
```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y nodejs npm \
    && npm install -g @anthropic-ai/claude-code free-claude-code \
    && rm -rf /var/lib/apt/lists/*
```
Then set `FORGE_CELL_IMAGE=forge-cell-claude:latest` in `/opt/forge-mk1/.env`.

Option B is strongly preferred: installing every session adds minutes of
wall-clock to every `claude_code` call and burns tokens on setup the model
has to remember to do.

### Network posture

The Cell reaches the proxy over the Docker bridge. That means the agent's
profile MUST set `allow_network = true` for `claude_code` to work — the proxy
is on the host, not inside the Cell, and without network the connection is
refused at the TCP level. The `claude_code` tool itself does not bypass this;
it fails with a clear error if the Cell cannot reach the proxy.

This is the same posture Centurion already uses (its profile declares network),
but it is worth flagging because Optimus's default is `--network none`. To give
Optimus access to Claude Code, add `allow_network = true` to its profile.
The proxy only listens on localhost, so the risk surface is the Cell itself —
an agent that already owns the Cell can spend the same provider keys directly.

### .env additions

```ini
# The proxy URL from inside the Cell. 172.17.0.1 is the Docker bridge gateway —
# the host as seen from a container on the default bridge network.
CLAUDE_CODE_SERVER_URL=http://172.17.0.1:8082
```

## 8. Updating

**CI deploys this.** `.github/workflows/ci.yml` runs the suite and the demo on
every branch, and on a green push to `main` it SSHes in and does everything
below. Pushing to main is the update procedure; the manual commands here are for
when the pipeline is unavailable.

That paragraph used to say "There is no CI for this repo", which stopped being
true and stayed on the page — the same drift that made the Hisar skill file
describe a write-only vault months after reads were opened.

### What the deploy account needs

The clone is owned by **root** (§1 installs it as root, and `forge@.service` has
no `User=` so systemd runs it as root), while CI connects as `SSH_USER`. That
split is deliberate: the service needs the docker socket, and the deploy account
gets no general sudo — `/etc/sudoers.d/forgedeploy` matches the exact command
lines the workflow uses, which is why those are spelled out verbatim there and
the unit path is not a variable.

Git ≥ 2.35.2 refuses to touch a tree owned by another user, so a fresh host
fails its first deploy with:

```
fatal: detected dubious ownership in repository at '***'
```

The workflow now declares the exception itself, idempotently, before its first
git command — so a rebuilt host works on the first run rather than needing a
step nobody wrote down. Doing it by hand is the same line, as the deploy user:

```bash
git config --global --add safe.directory /opt/forge-mk1
```

Do **not** "fix" this by chowning the tree to the deploy user. It only inverts
the mismatch: the root service writes `__pycache__` back into the same tree, and
the next `git status` complains from the other side.

### By hand, when CI cannot

```bash
cd /opt/forge-mk1 && git fetch origin main && git reset --hard origin/main
./.venv/bin/pip install -e ".[providers]"   # only when dependencies changed
systemctl restart forge@optimus forge@centurion
```
