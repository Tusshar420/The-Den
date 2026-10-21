#!/bin/bash
# Installs (or updates) The Den on a Mac. Started by "Install The Den.command".
#
# No administrator password is needed: everything goes into your own folders.
#   The program:   ~/Applications/TheDen         (change it with --prefix)
#   Logs:          ~/Library/Logs/TheDen
#   Background job: ~/Library/LaunchAgents/com.theden.app.plist   (label com.theden.app)
# Safe to run again: it then updates the program files and keeps your tasks, logins and settings.
#
# Options:
#   --prefix DIR   install somewhere else
#   --port N       the port it listens on (default 8787)
#   --network      let other computers on your network reach this Den (needed for sharing)
#   --no-open      don't open the browser at the end
#   --dry-run      only try it out: copy files and write the background-job file inside --prefix,
#                  but install no packages, register nothing with macOS and start nothing
# For unattended runs: DEN_LOGIN_USER and DEN_LOGIN_PASSWORD set the first login; DEN_INSTALL_YES=1 stops all questions.

set -u

LABEL="com.theden.app"
SRC="$(cd "$(dirname "$0")/../.." && pwd)"
APP="$HOME/Applications/TheDen"
PORT=8787
NETWORK=0
OPEN=1
DRY=0
TOTAL=7
LOGS="$HOME/Library/Logs/TheDen"
PLIST_DIR="$HOME/Library/LaunchAgents"

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) APP="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --network) NETWORK=1; shift ;;
        --no-open) OPEN=0; shift ;;
        --dry-run) DRY=1; shift ;;
        *) echo "I don't know the option $1"; exit 2 ;;
    esac
done
case "$APP" in /*) ;; *) APP="$(pwd)/$APP" ;; esac
if [ "$DRY" = 1 ]; then       # a dry run touches nothing outside the --prefix folder
    LOGS="$APP/logs"
    PLIST_DIR="$APP/launchd"
fi
PLIST="$PLIST_DIR/$LABEL.plist"
INTERACTIVE=0
if [ -t 0 ] && [ -z "${DEN_INSTALL_YES:-}" ]; then INTERACTIVE=1; fi

step() { echo; echo "Step $1 of $TOTAL: $2"; }
good() { echo "  OK  $1"; }
note() { echo "      $1"; }
warn() { echo "  !   $1"; }
fail() {
    echo
    echo "  The install stopped: $1"
    echo "  Nothing is broken - you can run it again once this is fixed."
    exit 1
}
ask_yes_no() {      # ask_yes_no "question" default(y/n)
    if [ "$INTERACTIVE" = 0 ]; then [ "$2" = y ]; return; fi
    local hint="[y/N]"; [ "$2" = y ] && hint="[Y/n]"
    local a; read -r -p "$1 $hint " a
    a="$(echo "$a" | tr '[:upper:]' '[:lower:]')"
    [ -z "$a" ] && a="$2"
    [ "$a" = y ] || [ "$a" = yes ]
}

echo
echo "  =========================="
echo "    Installing The Den"
echo "  =========================="
echo "  This takes about 5 to 10 minutes. You can leave this window open and wait."
[ "$DRY" = 1 ] && echo "  (Dry run: nothing is registered or started, and nothing leaves $APP.)"
[ -f "$SRC/main.py" ] || fail "I can't find The Den's files next to this installer. Unzip the whole download first, then double-click \"Install The Den.command\" from the unzipped folder."
UPDATING=0
[ -f "$APP/queue.db" ] && UPDATING=1
[ "$UPDATING" = 1 ] && echo "  The Den is already installed here, so this will update it. Your tasks, logins and settings are kept."

# ---------------------------------------------------------------- 1. Python
step 1 "Checking for Python (the language The Den is written in)"
PYTHON=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
    for dir in "" /opt/homebrew/bin/ /usr/local/bin/ /Library/Frameworks/Python.framework/Versions/Current/bin/; do
        p="${dir}${c}"
        if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
            PYTHON="$(command -v "$p")"; break 2
        fi
    done
done
[ -n "$PYTHON" ] || fail "Python 3.10 or newer is needed. Please install it from https://www.python.org/downloads/macos/ (the normal installer, just click Continue), then run this installer again."
good "Python is ready ($PYTHON)"

# ---------------------------------------------------------------- 2. Claude Code
step 2 "Checking for Claude Code (the AI that does the work)"
find_claude() {
    if command -v claude >/dev/null 2>&1; then command -v claude; return; fi
    for p in "$HOME/.local/bin/claude" "$HOME/.claude/local/claude" /opt/homebrew/bin/claude /usr/local/bin/claude; do
        [ -x "$p" ] && { echo "$p"; return; }
    done
}
CLAUDE="$(find_claude)"
if [ -z "$CLAUDE" ]; then
    if [ "$DRY" = 1 ]; then
        note "(dry run) Claude Code is not installed; the real install would run Anthropic's installer here."
    else
        note "Claude Code was not found. Installing it with its official installer..."
        curl -fsSL https://claude.ai/install.sh | bash >/dev/null 2>&1
        CLAUDE="$(find_claude)"
        [ -n "$CLAUDE" ] || fail "Claude Code could not be installed automatically. Open https://docs.claude.com/en/docs/claude-code/setup, follow the Mac steps there, then run this installer again."
    fi
fi
[ -n "$CLAUDE" ] && good "Claude Code is ready ($CLAUDE)"
CLAUDE_SIGNED_IN=0
if [ -n "$CLAUDE" ] && "$CLAUDE" auth status 2>/dev/null </dev/null | grep -q '"loggedIn": *true'; then CLAUDE_SIGNED_IN=1; fi
if [ "$CLAUDE_SIGNED_IN" = 1 ]; then good "You are already signed in to Claude"; else note "You are not signed in to Claude yet (this is asked again at the end)."; fi

# ---------------------------------------------------------------- 3. Files
step 3 "Copying The Den's files to $APP"
if [ "$DRY" = 0 ] && [ -f "$PLIST" ]; then
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
    sleep 2
fi
mkdir -p "$APP" "$LOGS" "$PLIST_DIR" || fail "I couldn't create $APP."
# rsync without --delete never removes anything from the install folder.
# These are the person's own files and folders: the same list as KEEP_NAMES / KEEP_DIRS in launcher.py.
rsync -a \
    --exclude 'installer/' --exclude '/README-FIRST.txt' --exclude '/Install The Den.command' --exclude '/Uninstall The Den.command' --exclude '__pycache__/' --exclude '.DS_Store' \
    --exclude '/.venv/' --exclude '/.approval-venv/' --exclude '/.den-update/' --exclude '/.git/' \
    --exclude '/workspace/' --exclude '/vault/' --exclude '/backups/' --exclude '/releases/' --exclude '/static/project-images/' \
    --exclude 'queue.db*' --exclude '/accounts.json' --exclude '/login.json' --exclude '/api_keys.json' \
    --exclude '/restart_held.json' --exclude '/restart_requested.json' --exclude '/den.env' --exclude '/den-keys.json' \
    --exclude '/mcp-config.local.json' \
    "$SRC/" "$APP/" || fail "Copying the files failed."
good "Files are in place"

# ---------------------------------------------------------------- 4. Packages
step 4 "Setting up The Den (installing its Python packages; this is the slow part)"
if [ ! -x "$APP/.venv/bin/python3" ]; then
    "$PYTHON" -m venv "$APP/.venv" || fail "Could not create The Den's Python environment."
fi
if [ "$DRY" = 1 ]; then
    note "(dry run) would install: $(grep -v '^#' "$APP/requirements.txt" | grep -v '^$' | tr '\n' ' ') and mcp<2"
else
    "$APP/.venv/bin/python3" -m pip install -q --disable-pip-version-check --upgrade pip >/dev/null 2>&1
    "$APP/.venv/bin/python3" -m pip install -q --disable-pip-version-check -r "$APP/requirements.txt" \
        || fail "Installing the Python packages failed. Check your internet connection and run the installer again."
    # the helper that asks you before Claude runs something risky
    "$APP/.venv/bin/python3" -m pip install -q --disable-pip-version-check 'mcp<2' \
        || fail "Installing the approval helper failed. Check your internet connection and run the installer again."
    good "Packages are installed"
fi

# ---------------------------------------------------------------- 5. Settings
step 5 "Writing your settings"
API_KEY=""
HOST_NOW=""
if [ -f "$PLIST" ]; then       # an update keeps the key and the network choice it already has
    API_KEY="$("$PYTHON" - "$PLIST" <<'PY'
import plistlib, sys
try:
    print(plistlib.load(open(sys.argv[1], "rb"))["EnvironmentVariables"].get("API_KEY", ""))
except Exception:
    print("")
PY
)"
    HOST_NOW="$("$PYTHON" - "$PLIST" <<'PY'
import plistlib, sys
try:
    print(plistlib.load(open(sys.argv[1], "rb"))["EnvironmentVariables"].get("HOST", ""))
except Exception:
    print("")
PY
)"
fi
if [ -z "$API_KEY" ]; then
    API_KEY="$("$PYTHON" -c 'import secrets; print(secrets.token_hex(24))')"
    good "Made a private key for this Den (it stays on this Mac)"
fi
HOST="127.0.0.1"
if [ "$NETWORK" = 1 ]; then
    HOST="0.0.0.0"
elif [ -n "$HOST_NOW" ]; then
    HOST="$HOST_NOW"
else
    echo
    echo "  Should other computers on your network be able to reach this Den?"
    echo "  Say No unless you plan to share it with a colleague. You can change this later."
    ask_yes_no "  Let other computers connect?" n && HOST="0.0.0.0"
fi
PATH_VALUE="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
"$PYTHON" - "$SRC/installer/mac/com.theden.app.plist.template" "$PLIST" "$LABEL" "$APP" "$PATH_VALUE" "$HOST" "$PORT" "$API_KEY" "${CLAUDE:-claude}" "$LOGS" <<'PY' || fail "Could not write the background-job file."
import os, sys
from xml.sax.saxutils import escape
tpl, out, label, app, path, host, port, key, claude, logs = sys.argv[1:11]
text = open(tpl, encoding="utf-8").read()
for k, v in {"@LABEL@": label, "@APP@": app, "@PATH@": path, "@HOST@": host, "@PORT@": port,
             "@API_KEY@": key, "@CLAUDE_BIN@": claude, "@LOGS@": logs}.items():
    text = text.replace(k, escape(v))
fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)     # it holds the key: only you can read it
with os.fdopen(fd, "w", encoding="utf-8") as f:
    f.write(text)
os.chmod(out, 0o600)
PY
if [ "$HOST" = "127.0.0.1" ]; then good "The Den will only be reachable from this Mac"; else good "The Den will be reachable from your network"; fi

# ---------------------------------------------------------------- 6. Start
step 6 "Starting The Den"
if [ "$DRY" = 1 ]; then
    note "(dry run) would register $PLIST with macOS and start The Den"
else
    chmod 600 "$PLIST"
    UIDN="$(id -u)"
    launchctl bootstrap "gui/$UIDN" "$PLIST" 2>/dev/null || { launchctl bootout "gui/$UIDN/$LABEL" 2>/dev/null; sleep 1; launchctl bootstrap "gui/$UIDN" "$PLIST" 2>/dev/null; } \
        || fail "macOS would not register the background job."
    launchctl enable "gui/$UIDN/$LABEL" 2>/dev/null
    launchctl kickstart -k "gui/$UIDN/$LABEL" 2>/dev/null
    good "The Den will now start by itself whenever you sign in to your Mac"
    note "Waiting for The Den to wake up (the first start can take a minute)..."
    UP=0
    for i in $(seq 1 75); do
        if curl -fsS -m 3 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then UP=1; break; fi
        sleep 2
    done
    [ "$UP" = 1 ] || fail "The Den did not start. Its log is in $LOGS/den-stderr.log - please send that file to whoever gave you The Den."
    good "The Den is running"
fi

# ---------------------------------------------------------------- 7. First login
step 7 "Your login"
MADE_LOGIN=0
if [ "$DRY" = 1 ]; then
    note "(dry run) would ask you to choose a name and password for signing in"
else
    NEED_LOGIN="$("$APP/.venv/bin/python3" - "$PORT" "$API_KEY" <<'PY'
import json, sys, urllib.request
req = urllib.request.Request(f"http://127.0.0.1:{sys.argv[1]}/auth/state", headers={"X-API-Key": sys.argv[2]})
try:
    print("no" if json.load(urllib.request.urlopen(req, timeout=10)).get("configured") else "yes")
except Exception:
    print("no")
PY
)"
    if [ "$NEED_LOGIN" = yes ]; then
        echo "  Choose how you will sign in to The Den. Pick any name and a password of 8 or more characters."
        for attempt in 1 2 3; do
            U="${DEN_LOGIN_USER:-}"; P="${DEN_LOGIN_PASSWORD:-}"
            if [ -z "$U" ] || [ -z "$P" ]; then
                [ "$INTERACTIVE" = 1 ] || break
                read -r -p "  Your name for signing in: " U
                if [ -z "$U" ]; then note "Skipped. The Den will ask you to create one when it opens."; break; fi
                read -r -s -p "  Your password: " P; echo
            fi
            MSG="$(DEN_U="$U" DEN_P="$P" "$APP/.venv/bin/python3" - "$PORT" "$API_KEY" <<'PY'
import json, os, sys, urllib.error, urllib.request
body = json.dumps({"username": os.environ["DEN_U"], "password": os.environ["DEN_P"], "remember": True}).encode()
req = urllib.request.Request(f"http://127.0.0.1:{sys.argv[1]}/auth/setup", data=body, method="POST",
                             headers={"X-API-Key": sys.argv[2], "Content-Type": "application/json"})
try:
    urllib.request.urlopen(req, timeout=20)
    print("OK")
except urllib.error.HTTPError as e:
    try:
        print(json.loads(e.read()).get("detail", "that did not work"))
    except Exception:
        print("that did not work")
except Exception:
    print("that did not work")
PY
)"
            if [ "$MSG" = OK ]; then MADE_LOGIN=1; good "Your login is ready. Sign in as: $U"; break; fi
            warn "Could not save it: $MSG"
            DEN_LOGIN_USER=""; DEN_LOGIN_PASSWORD=""
        done
    else
        MADE_LOGIN=1; good "You already have a login"
    fi
    if [ "$CLAUDE_SIGNED_IN" = 0 ] && [ "$INTERACTIVE" = 1 ] && [ -n "$CLAUDE" ]; then
        echo
        if ask_yes_no "  Sign in to Claude now? (a browser window opens)" y; then
            "$CLAUDE" auth login || warn "That did not start. The steps below show another way."
            "$CLAUDE" auth status 2>/dev/null </dev/null | grep -q '"loggedIn": *true' && CLAUDE_SIGNED_IN=1
        fi
    fi
fi

echo
echo "  =========================="
echo "    The Den is installed"
echo "  =========================="
echo
echo "  What to do next:"
echo "   1. The Den opens in your browser. If it does not, go to http://127.0.0.1:$PORT/dashboard"
if [ "$MADE_LOGIN" = 1 ]; then
    echo "   2. Sign in with the name and password you chose."
else
    echo "   2. It will ask you to create a login. It also asks once for your private key: it is the API_KEY line in"
    echo "      $PLIST  (open it with TextEdit)."
fi
if [ "$CLAUDE_SIGNED_IN" = 0 ]; then
    echo "   3. Sign in to Claude, or the AI can not work: open the Terminal app, type  claude  and press Return,"
    echo "      then follow the steps on screen. Do this once."
fi
echo
echo "  The Den runs quietly in the background and starts by itself each time you sign in to your Mac."
echo "  Its files:  $APP"
echo "  Its logs:   $LOGS"
echo "  To remove it later, double-click \"Uninstall The Den.command\" (in the folder you unzipped)."
echo
if [ "$DRY" = 0 ] && [ "$OPEN" = 1 ]; then open "http://127.0.0.1:$PORT/dashboard"; fi
exit 0
