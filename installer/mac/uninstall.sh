#!/bin/bash
# Removes The Den from this Mac. It asks before deleting your own data (tasks, projects, logins, notes).
# Options: --prefix DIR (where it was installed; default ~/Applications/TheDen), --delete-data, --keep-data,
#          --dry-run (changes nothing; only says what it would do)

set -u
LABEL="com.theden.app"
APP="$HOME/Applications/TheDen"
LOGS="$HOME/Library/Logs/TheDen"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
CHOICE=""
DRY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) APP="$2"; shift 2 ;;
        --delete-data) CHOICE=delete; shift ;;
        --keep-data) CHOICE=keep; shift ;;
        --dry-run) DRY=1; shift ;;
        *) echo "I don't know the option $1"; exit 2 ;;
    esac
done
do_it() { if [ "$DRY" = 1 ]; then echo "      (dry run) would run: $*"; else "$@"; fi; }

echo
echo "  Removing The Den"
echo
echo "  Step 1 of 3: stopping The Den"
do_it launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
[ "$DRY" = 1 ] || sleep 2
do_it rm -f "$PLIST"
echo "  OK  Stopped"

echo
echo "  Step 2 of 3: your data"
echo "  Your data is everything you made in The Den: tasks, projects, notes, files and logins."
if [ -z "$CHOICE" ]; then
    if [ -t 0 ]; then
        read -r -p "  Delete your data too? (Choose No to keep it, for example to install The Den again later.) [y/N] " a
        case "$(echo "$a" | tr '[:upper:]' '[:lower:]')" in y|yes) CHOICE=delete ;; *) CHOICE=keep ;; esac
    else
        CHOICE=keep
    fi
fi

echo
echo "  Step 3 of 3: removing the program files"
if [ ! -d "$APP" ]; then
    echo "  I can't find The Den in $APP. Nothing to remove there."
elif [ "$CHOICE" = delete ]; then
    do_it rm -rf "$APP"
    do_it rm -rf "$LOGS"
    echo "  OK  Everything was removed, including your data"
else
    # Delete everything except the person's own files and folders (same list as launcher.py).
    for item in "$APP"/* "$APP"/.[!.]*; do
        [ -e "$item" ] || continue
        case "$(basename "$item")" in
            queue.db*|accounts.json|login.json|api_keys.json|restart_held.json|restart_requested.json|den.env|den-keys.json|mcp-config.local.json) continue ;;
            workspace|vault|backups|releases|.den-update) continue ;;
        esac
        do_it rm -rf "$item"
    done
    echo "  OK  The program was removed. Your data is still in $APP"
    echo "  (Delete that folder yourself whenever you no longer want it.)"
fi
echo
echo "  The Den has been removed. Python and Claude Code were left alone, because other programs may use them."
echo
