#!/bin/bash
set -eu

watchdog_launcher="$HOME/Library/Application Support/CodexWatchdog/bin/watchdog-macos.sh"
if [[ ! -x "$watchdog_launcher" ]]; then
    printf '%s\n' 'Codex WatchDog is not installed for this macOS account.' >&2
    exit 1
fi

printf '%s\n' 'Starting Codex WatchDog (Slack only, every 30 seconds).' \
    'Keep this window open. Press Control-C to stop.'
exec "$watchdog_launcher" --slack-only --shared-slack-app -- --interval 30
