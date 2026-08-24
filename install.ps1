<#
.SYNOPSIS
    Install the Forge on a Windows machine.

.DESCRIPTION
    One command from a fresh clone to a working `forge`:

        .\install.ps1

    Finds a Python 3.11+, builds a .venv beside the repo, installs the Forge
    into it with the extras that make it usable (tui, providers, dev), seeds a
    .env from .env.example, and proves the install by listing the agents.

    Nothing here is magic — it is the README's manual steps with the failure
    modes named. Rerunning it is safe.

.PARAMETER Python
    Interpreter to build the environment from. Default: the newest 3.11+ found
    via the py launcher or PATH.

.PARAMETER Extras
    Comma-separated optional-dependency groups. Default "tui,providers,dev".
    Pass "" for the bare install.

.PARAMETER NoVenv
    Install into the interpreter itself rather than a .venv. What you want when
    the machine already has a dedicated Python for this, or on a server where
    the unit file calls the interpreter directly.

.PARAMETER AddToPath
    Append the resulting Scripts directory to your *user* PATH, so `forge`
    works from any terminal without activating anything. Off by default because
    it edits your environment; the script tells you when you need it.

.EXAMPLE
    .\install.ps1
.EXAMPLE
    .\install.ps1 -NoVenv -AddToPath
.EXAMPLE
    .\install.ps1 -Python "C:\Python312\python.exe" -Extras "providers"
#>
[CmdletBinding()]
param(
    [string]$Python = "",
    [string]$Extras = "tui,providers,dev",
    [switch]$NoVenv,
    [switch]$AddToPath
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path

function Say  ($m) { Write-Host "  $m" }
function Step ($m) { Write-Host "" ; Write-Host "==> $m" -ForegroundColor Cyan }
function Die  ($m) { Write-Host "" ; Write-Host "ERROR: $m" -ForegroundColor Red ; exit 1 }

Write-Host ""
Write-Host "F.O.R.G.E. installer" -ForegroundColor White
Write-Host "  $repo"

# ── 1. Find an interpreter ───────────────────────────────────────────────────
# The trap on Windows is C:\...\WindowsApps\python.exe: a 0-byte stub that
# opens the Microsoft Store instead of running Python. It is on PATH by
# default, it answers `Get-Command python`, and it fails only once you try to
# use it — so check the version output, not the name.
#
# A candidate is a command line — @("py","-3.12") or @("C:\...\python.exe") —
# because the py launcher needs its version selector. Asking the candidate for
# sys.executable resolves it to a real interpreter path either way, and a stub
# never gets that far.
function Test-Interpreter ($argv) {
    if (-not $argv) { return $null }
    $head = $argv[0]
    $rest = @()
    if ($argv.Count -gt 1) { $rest = $argv[1..($argv.Count - 1)] }
    try {
        $out = & $head @rest -c "import sys; print('%d.%d %s' % (sys.version_info[0], sys.version_info[1], sys.executable))" 2>$null
    } catch { return $null }
    if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
    $fields = ($out | Select-Object -Last 1).Trim() -split ' ', 2
    if ($fields.Count -lt 2) { return $null }
    $parts = $fields[0] -split '\.'
    $major = [int]$parts[0]; $minor = [int]$parts[1]
    if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 11)) { return $null }
    return @{ Exe = $fields[1].Trim(); Version = "$major.$minor" }
}

Step "Locating Python 3.11+"
$found = $null
if ($Python) {
    $found = Test-Interpreter @($Python)
    if (-not $found) { Die "$Python is not a working Python 3.11 or later." }
} else {
    $candidates = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($v in @("-3.14", "-3.13", "-3.12", "-3.11", "-3")) {
            $candidates += ,@("py", $v)
        }
    }
    foreach ($n in @("python", "python3")) {
        $c = Get-Command $n -ErrorAction SilentlyContinue
        if ($c) { $candidates += ,@($c.Source) }
    }
    foreach ($dir in (Get-ChildItem "$env:LOCALAPPDATA\Programs\Python", "$env:ProgramFiles\Python*", "C:\Python*" -Directory -ErrorAction SilentlyContinue)) {
        $exe = Join-Path $dir.FullName "python.exe"
        if (Test-Path $exe) { $candidates += ,@($exe) }
    }
    foreach ($cand in $candidates) {
        $probe = Test-Interpreter $cand
        if ($probe) { $found = $probe; break }
    }
}

if (-not $found) {
    Write-Host ""
    Write-Host "No usable Python 3.11+ found." -ForegroundColor Red
    Say "Install one, then rerun this script:"
    Say "    winget install --id Python.Python.3.12 --source winget"
    Say "(the python.exe in WindowsApps is a Store stub, not an interpreter)"
    exit 1
}
Say "Python $($found.Version) — $($found.Exe)"

# ── 2. Environment to install into ───────────────────────────────────────────
if ($NoVenv) {
    Step "Installing into the interpreter itself (-NoVenv)"
    $py = $found.Exe
} else {
    $venv = Join-Path $repo ".venv"
    $py = Join-Path $venv "Scripts\python.exe"
    if (Test-Path $py) {
        Step "Reusing the existing .venv"
    } else {
        Step "Creating .venv"
        & $found.Exe -m venv $venv
        if ($LASTEXITCODE -ne 0) { Die "venv creation failed. On a stripped Python, install the 'venv' module." }
    }
    Say $py
}

# Under -NoVenv, $py is whichever candidate Test-Interpreter resolved — on
# Windows that can legitimately differ from what a bare `python` means in the
# NEXT shell (the runner image ships several 3.x installs, and the `py`
# launcher's version table does not always agree with PATH order). A later CI
# step that assumes `python -m forge demo` means the same interpreter this
# script just installed into is exactly how "ModuleNotFoundError: pydantic"
# happened with a green install log two steps above it. Recording the exact
# path lets a workflow step ask for THIS interpreter instead of guessing.
if ($env:GITHUB_ENV) {
    Add-Content -Path $env:GITHUB_ENV -Value "FORGE_PY=$py"
}

# ── 3. Install ───────────────────────────────────────────────────────────────
Step "Installing dependencies"
& $py -m pip install --upgrade --quiet pip
if ($LASTEXITCODE -ne 0) { Die "could not upgrade pip in $py" }

if ($Extras.Trim()) { $target = ".[$($Extras.Trim())]" } else { $target = "." }
Say "pip install -e `"$target`""
Push-Location $repo
try {
    & $py -m pip install -e $target
    $rc = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($rc -ne 0) { Die "pip install failed. The output above says why." }

# ── 4. Configuration ─────────────────────────────────────────────────────────
# Keys live in a file the agent never commits. A repo-local .env covers this
# clone; ~/.forge/.env covers every project on the machine. Seed the local one
# only when neither exists, so a rerun never clobbers real keys.
Step "Configuration"
$localEnv = Join-Path $repo ".env"
if ($env:FORGE_HOME) { $forgeHome = $env:FORGE_HOME } else { $forgeHome = Join-Path $env:USERPROFILE ".forge" }
$homeEnv  = Join-Path $forgeHome ".env"
if ((Test-Path $localEnv) -or (Test-Path $homeEnv)) {
    if (Test-Path $localEnv) { Say ".env already present — left alone" }
    if (Test-Path $homeEnv)  { Say "$homeEnv already present — left alone" }
} else {
    Copy-Item (Join-Path $repo ".env.example") $localEnv
    Say "wrote .env from .env.example — put your API key in it"
}

# ── 5. Prove it ──────────────────────────────────────────────────────────────
Step "Verifying"
& $py -m forge agents
if ($LASTEXITCODE -ne 0) { Die "the Forge is installed but will not start. See above." }

# Ask the interpreter where its console scripts land rather than assuming they
# sit beside python.exe. In a venv they do; in a system install forge.exe goes
# to <prefix>\Scripts, and guessing puts a directory on PATH that holds nothing.
$scripts = (& $py -c "import sysconfig; print(sysconfig.get_path('scripts'))").Trim()
if ($AddToPath) {
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if (-not $userPath) { $userPath = "" }
    # Compare without the trailing slash: PATH entries written by the Python
    # installer end in one, and a naive match would append a second copy of a
    # directory that is already there on every run.
    $normalised = @()
    foreach ($e in ($userPath -split ';')) {
        if ($e) { $normalised += $e.TrimEnd('\').ToLower() }
    }
    if ($normalised -contains $scripts.TrimEnd('\').ToLower()) {
        Say "$scripts is already on your user PATH"
    } else {
        $joined = ($userPath.TrimEnd(';') + ';' + $scripts).TrimStart(';')
        [Environment]::SetEnvironmentVariable("Path", $joined, "User")
        $env:Path = $env:Path + ';' + $scripts
        Say "added $scripts to your user PATH (new terminals pick it up)"
    }
}

Write-Host ""
Write-Host "Done." -ForegroundColor Green
# The persisted PATH, not just this process's copy: a terminal opened before
# the directory was added still has the old one, and reporting from it would
# tell a correctly-installed machine to go add PATH again.
$onPath = @()
foreach ($scope in @("Process", "User", "Machine")) {
    $v = [Environment]::GetEnvironmentVariable("Path", $scope)
    if ($v) { foreach ($e in ($v -split ';')) { if ($e) { $onPath += $e.TrimEnd('\').ToLower() } } }
}
if ($onPath -contains $scripts.TrimEnd('\').ToLower()) {
    Say "cd into any project and run:  forge"
} else {
    Say "Run it with:            $scripts\forge.exe"
    Say "Or put it on PATH with: .\install.ps1 -AddToPath"
}
Say ("Offline end-to-end check (needs no API key):  " + $py + " -m forge demo")
Write-Host ""
