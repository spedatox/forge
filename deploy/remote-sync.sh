#!/usr/bin/env bash
# Runs AS ROOT on the deploy host, invoked by CI through one whitelisted sudoers
# line (deploy/README.md §"What the deploy account needs").
#
# WHY THIS EXISTS. The clone, the venv and the service are all root-owned: §1
# installs the tree as root, and forge@.service has no `User=`, so systemd runs
# the peer as root. CI connects as the deploy account, which has no general sudo.
# The half of the deploy that WRITES into that root-owned state cannot run as the
# deploy user:
#
#   - `git fetch` downloads new objects and then cannot write them into root's
#     .git/objects — it dies with "insufficient permission for adding an object
#     to repository database .git/objects" and exit 128, on every push that
#     carries new commits. `git config safe.directory` silenced git's ownership
#     WARNING; it never granted the write, which is the failure it was mistaken
#     for a fix for.
#   - `pip install -e` into a root-owned .venv fails the same way the moment a
#     dependency actually changes.
#
# So the tree-mutating steps run here, as root, reached through the same
# exact-command sudoers mechanism the systemctl calls in the workflow already
# use. Chowning the tree to the deploy user is NOT the fix (deploy/README.md
# says so at length): the root service writes __pycache__ back into it and the
# ownership mismatch just inverts.
#
# A wrapper rather than three whitelisted command lines on purpose: sudoers
# glob-matches command arguments, and `pip install -e .[providers]` carries a
# literal `[providers]` that sudoers would read as a character class — a landmine
# a single whitelisted script path steps around entirely. It also keeps every
# argument that might change (a pip flag, a fetch refspec) on the root-owned side
# of the trust boundary, where the deploy account cannot alter what runs as root.
#
# The path is hardcoded for the same reason the unit-file path is in the
# workflow: the sudoers rule matches an exact command line, so the location is a
# fact stated in two places that must agree, not a variable.
set -euo pipefail

cd /opt/forge-mk1

# Mirror main exactly. .env and .env.<agent> are gitignored and so survive; the
# venv lives in .venv and is untracked.
git fetch --prune origin main
git reset --hard origin/main

# forge is installed editable, so code is live after the reset — but a new
# dependency in pyproject.toml would not be. Cheap when nothing changed.
.venv/bin/python -m pip install -q -e ".[providers]"
