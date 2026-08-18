#!/usr/bin/env sh
# Install the Forge on macOS or Linux.
#
#     ./install.sh                    venv + tui,providers,dev, then verify
#     ./install.sh --no-venv          install into the interpreter itself
#     ./install.sh --extras providers  server install, no terminal UI
#     ./install.sh --python /usr/bin/python3.12
#
# This is the README's manual steps with the failure modes named. Rerunning it
# is safe: an existing .venv is reused and an existing .env is never touched.
set -eu

repo=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
extras="tui,providers,dev"
python_bin=""
use_venv=1
add_to_path=0

while [ $# -gt 0 ]; do
    case "$1" in
        --python)   python_bin="${2:?--python needs a path}"; shift 2 ;;
        --extras)   extras="${2-}"; shift 2 ;;
        --no-venv)  use_venv=0; shift ;;
        --add-to-path) add_to_path=1; shift ;;
        -h|--help)  sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
    esac
done

say()  { printf '  %s\n' "$1"; }
step() { printf '\n==> %s\n' "$1"; }
die()  { printf '\nERROR: %s\n' "$1" >&2; exit 1; }

printf '\nF.O.R.G.E. installer\n  %s\n' "$repo"

# ── 1. Find an interpreter ───────────────────────────────────────────────────
# Named versions first: on a distro whose `python3` is still 3.9, the 3.11+
# interpreter is usually installed alongside under its own name.
probe() {
    [ -n "${1:-}" ] || return 1
    command -v "$1" >/dev/null 2>&1 || [ -x "$1" ] || return 1
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}

step "Locating Python 3.11+"
if [ -n "$python_bin" ]; then
    probe "$python_bin" || die "$python_bin is not a working Python 3.11 or later."
else
    for c in python3.14 python3.13 python3.12 python3.11 python3 python; do
        if probe "$c"; then python_bin="$c"; break; fi
    done
fi
if [ -z "$python_bin" ]; then
    printf '\nNo usable Python 3.11+ found.\n' >&2
    say "Debian/Ubuntu:  sudo apt install python3.12 python3.12-venv"
    say "Fedora:         sudo dnf install python3.12"
    say "macOS:          brew install python@3.12"
    exit 1
fi
say "Python $("$python_bin" -c 'import sys; print("%d.%d" % sys.version_info[:2])') — $("$python_bin" -c 'import sys; print(sys.executable)')"

# ── 2. Environment to install into ───────────────────────────────────────────
if [ "$use_venv" -eq 0 ]; then
    step "Installing into the interpreter itself (--no-venv)"
    py="$python_bin"
else
    py="$repo/.venv/bin/python"
    if [ -x "$py" ]; then
        step "Reusing the existing .venv"
    else
        step "Creating .venv"
        # Debian splits venv into its own package, and the error it prints
        # when it is missing does not say to install anything.
        "$python_bin" -m venv "$repo/.venv" \
            || die "venv creation failed — on Debian/Ubuntu: sudo apt install python3-venv"
    fi
    say "$py"
fi

# ── 3. Install ───────────────────────────────────────────────────────────────
step "Installing dependencies"
# PEP 668: a distro-managed interpreter refuses to be installed into at all,
# and says so in four lines that do not mention the word venv. Only reachable
# under --no-venv, which is exactly where someone is least expecting it.
"$py" -m pip install --upgrade --quiet pip \
    || die "pip cannot write to $py — drop --no-venv, or pass --python for an interpreter you own."

if [ -n "$extras" ]; then target=".[$extras]"; else target="."; fi
say "pip install -e \"$target\""
( cd "$repo" && "$py" -m pip install -e "$target" ) \
    || die "pip install failed. The output above says why."

# ── 4. Configuration ─────────────────────────────────────────────────────────
# Keys live in a file that is never committed. A repo-local .env covers this
# clone; ~/.forge/.env covers every project on the machine. Seed the local one
# only when neither exists, so a rerun never clobbers real keys.
step "Configuration"
if [ -f "$repo/.env" ]; then
    say ".env already present — left alone"
elif [ -f "${FORGE_HOME:-$HOME/.forge}/.env" ]; then
    say "${FORGE_HOME:-$HOME/.forge}/.env already present — left alone"
else
    cp "$repo/.env.example" "$repo/.env"
    say "wrote .env from .env.example — put your API key in it"
fi

# ── 5. Prove it ──────────────────────────────────────────────────────────────
step "Verifying"
"$py" -m forge agents || die "the Forge is installed but will not start. See above."

# Ask the interpreter where its console scripts land rather than assuming they
# sit beside it. In a venv they do; under --no-venv with a --user install they
# go somewhere else entirely, and guessing puts an empty directory on PATH.
bindir=$("$py" -c "import sysconfig; print(sysconfig.get_path('scripts'))")
if [ "$add_to_path" -eq 1 ]; then
    line="export PATH=\"$bindir:\$PATH\""
    case "${SHELL:-}" in
        *zsh) rc="$HOME/.zshrc" ;;
        *)    rc="$HOME/.bashrc" ;;
    esac
    if [ -f "$rc" ] && grep -Fqx "$line" "$rc"; then
        say "$rc already puts $bindir on PATH"
    else
        printf '\n# added by the Forge installer\n%s\n' "$line" >> "$rc"
        say "appended $bindir to PATH in $rc (open a new shell, or source it)"
    fi
fi

printf '\nDone.\n'
if command -v forge >/dev/null 2>&1 && [ "$(command -v forge)" = "$bindir/forge" ]; then
    say "cd into any project and run:  forge"
else
    say "Run it with:            $bindir/forge"
    say "Or put it on PATH with: ./install.sh --add-to-path"
fi
say "Offline end-to-end check (needs no API key):  \"$py\" -m forge demo"
printf '\n'
