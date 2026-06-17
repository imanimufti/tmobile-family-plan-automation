#!/bin/bash
# Install (or reinstall) the launchd agent that runs the pipeline a few times a day.
# Resolves the python interpreter and project paths at install time and loads the job.
set -euo pipefail

LABEL="com.imani.tmobile-pipeline"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$ROOT/launchd/$LABEL.plist"
DEST_DIR="$HOME/Library/LaunchAgents"
DEST="$DEST_DIR/$LABEL.plist"

PYTHON="$(command -v python3)"
if [ -z "$PYTHON" ]; then
    echo "python3 not found on PATH" >&2
    exit 1
fi

# launchd starts with a minimal PATH; include node's dir (Homebrew) so the
# WhatsApp Web sender is found during the announce stage.
NODE_DIR="$(dirname "$(command -v node 2>/dev/null || echo /opt/homebrew/bin/node)")"
RUN_PATH="$NODE_DIR:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

mkdir -p "$DEST_DIR" "$ROOT/logs" "$ROOT/state"

# Substitute placeholders into the installed copy.
sed -e "s|__PYTHON__|$PYTHON|g" -e "s|__ROOT__|$ROOT|g" -e "s|__PATH__|$RUN_PATH|g" "$TEMPLATE" > "$DEST"

# Reload cleanly if already present.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$DEST"
launchctl enable "gui/$(id -u)/$LABEL"

echo "Installed $DEST"
echo "Interpreter: $PYTHON"
echo
echo "Grant this interpreter Full Disk Access AND Accessibility in System Settings"
echo "  -> Privacy & Security  (needed for chat.db reads and WhatsApp keystrokes):"
echo "     $PYTHON"
echo
echo "Run it now with:   launchctl kickstart -k gui/$(id -u)/$LABEL"
echo "Watch logs with:   tail -f \"$ROOT/logs/pipeline.log\""
