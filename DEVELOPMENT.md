# Development Workflow

## First-time setup (do this ONCE, ever)

Open PowerShell, anywhere:

```powershell
irm https://raw.githubusercontent.com/MuaazEid/orchestra/main/scripts/setup.ps1 | iex
```

That clones the repo to `C:\dev\orchestra`, creates a `.venv`, and installs
everything. Total time ~1 minute.

## Daily loop (this is it, literally two lines)

```powershell
cd C:\dev\orchestra
.\dev.ps1
```

### One-click launcher (optional, do it once)

```powershell
cd C:\dev\orchestra
powershell -ExecutionPolicy Bypass -File scripts\install-shortcut.ps1
```

Creates an **Orchestra** shortcut on your desktop with a distinctive amber
icon. Double-click it and you're running — same `dev.ps1` under the hood,
just no typing.

`dev.ps1` does three things automatically:

1. `git pull` — grabs anything new I pushed
2. Reinstalls deps **only** if `requirements.txt` actually changed
3. Starts the server with **hot reload** on `http://localhost:8765`

Hot reload means: edit a `.py` file → save → the server restarts by
itself. Edit an `.html` file → save → refresh the browser tab. No CMD
gymnastics, no re-activating venvs, no re-running commands.

Leave `dev.ps1` running in one window; do everything else in another.

## When I push a new change

You don't do anything. Next time you run `.\dev.ps1`, it pulls before
starting. If the server is already running, just stop it (Ctrl+C) and
start it again — it'll pull first.

## Where things live

```
C:\dev\orchestra\               ← the ONE directory. Nothing else matters.
├── .venv\                      ← Python environment. Auto-created.
├── .git\                       ← version control. Auto.
├── .env                        ← YOUR local secrets (Tavily key, etc).
│                                 Never committed. See .env.example.
├── orchestra\                  ← the actual source code
│   ├── agents\
│   ├── engine\
│   ├── web\
│   └── ...
├── tests\                      ← run with:  pytest -q
├── requirements.txt
├── dev.ps1                     ← ← ← what you run daily
└── scripts\
    └── setup.ps1               ← one-time bootstrap
```

## Rules that keep this clean

1. **`Downloads` is not a workspace.** Never work there. If a zip lands
   there, delete it after unpacking. The only workspace is
   `C:\dev\orchestra`.
2. **`.env` never leaves your machine.** It's in `.gitignore`. Keep your
   Tavily key and any other secrets there.
3. **Never edit files by pasting into Notepad again.** Editing happens
   either in an editor (VS Code recommended — `code .` from the folder)
   or through a `git pull`. Never both mixed.
4. **If it's broken, `git status` first.** It tells you what changed and
   what state the repo is in. 90% of "weird" problems become obvious.

## Quick commands, for reference

```powershell
# See what changed locally
git status

# Discard a local edit you don't want anymore
git checkout -- path\to\file.py

# See the last few commits
git log --oneline -10

# Run tests
.\.venv\Scripts\python.exe -m pytest -q
```

That's the whole workflow. Everything else is noise.
