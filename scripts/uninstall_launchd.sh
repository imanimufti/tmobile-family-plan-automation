#!/bin/bash
# Stop and remove the pipeline launchd agent.
set -euo pipefail

LABEL="com.imani.tmobile-pipeline"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$DEST"
echo "Removed $LABEL"
