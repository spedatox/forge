# Optimus's Cell image — a polyglot DEV lab baked at BUILD time so a live Cell
# pays nothing at job time for the routine toolchain. Same idea, and now the same
# posture, as cell-centurion.Dockerfile: the Cell is a throwaway, per-job
# container isolated from the host, and the profile runs it as root — so the bake
# only needs to cover the ROUTINE toolchain, and anything beyond it a job simply
# `apt-get install`s (or `npm i -g`, `go install`, `pip install`) at job time,
# exactly as Centurion does with the long tail of Kali tools.
#
# forge/agents/optimus/profile.toml [cell].image points at the tag this builds.
# The per-agent image wins over the global FORGE_CELL_IMAGE, so no .env override
# is needed. Under the subprocess backend (a Docker-less host) the image is
# ignored entirely — this matters only with the Docker Cell.
#
# Build (on the host, context is deploy/ to keep the .venv out of the daemon):
#   cd /opt/forge-mk1
#   docker build -f deploy/cell-optimus.Dockerfile -t forge-cell-optimus:latest deploy/

# Node comes from its own official image and is copied in, rather than piped
# from a remote setup script at build time: both bases are Debian bookworm, so
# the glibc matches, and the version is pinned by the tag instead of by whatever
# a `curl | bash` resolves on the day of the build.
FROM node:22-bookworm-slim AS node

FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="forge-cell-optimus" \
      org.opencontainers.image.description="Polyglot dev toolchain (Python/Node/Go + build tools) baked for the Forge's Optimus agent" \
      maintainer="spedatox"

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PIP_NO_CACHE_DIR=1

# 1) System toolchain: a C/C++ build environment (native npm/pip modules compile
#    against it), git and the network CLIs, the fast search tools a coding agent
#    reaches for constantly (ripgrep/fd), Go, and SQLite. A failure here MUST
#    break the build.
RUN apt-get update \
 && apt-get -y upgrade \
 && apt-get -y install --no-install-recommends \
        build-essential pkg-config \
        git curl wget ca-certificates openssh-client \
        jq unzip zip ripgrep fd-find \
        make gcc g++ \
        golang-go \
        sqlite3 libsqlite3-dev \
        python3-venv \
 && ln -sf "$(command -v fdfind)" /usr/local/bin/fd \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# 2) Node + npm, copied from the official image (see the top comment) — the
#    WHOLE of /usr/local/bin, not individual named files. corepack's shim
#    requires a sibling `lib/corepack.cjs` that lives beside it in that
#    directory, not inside node_modules; picking files by name (node, npm, npx,
#    corepack) missed it and broke `corepack enable`. Copying the directory
#    wholesale is also the fix for the failure mode that caused THAT bug: this
#    is the second time Node's own internal layout under /usr/local/bin turned
#    out to have one more piece than assumed, and a wholesale copy of the
#    source of truth is what stops there being a third. No collision risk with
#    the Python base image: it owns /usr/local/bin/python3*, pip*, none of
#    which Node's directory contains. corepack is enabled so `yarn`/`pnpm` are
#    available on demand without a separate global install.
COPY --from=node /usr/local/bin/ /usr/local/bin/
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN corepack enable && node --version && npm --version

# 3) Python developer tooling, baked so the common loop (make a venv, lint, test)
#    pays nothing at job time. uv is included because it is the fast path for the
#    "install these deps" step a job hits first; ruff/pytest are the lint+test
#    pair. Installed into the system interpreter so any Cell uid can run them.
RUN pip install --no-cache-dir \
        uv ruff pytest pytest-cov virtualenv pipx

# The Cell is launched with `--workdir /workspace` and an explicit
# `sleep infinity` by the Docker Cell, so this CMD is only a sensible default for
# a manual `docker run`. /workspace is created and chowned to the Cell uid by
# DockerCell.start(); nothing here needs to pre-create it.
WORKDIR /workspace
CMD ["sleep", "infinity"]
