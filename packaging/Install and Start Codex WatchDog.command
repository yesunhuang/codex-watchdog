#!/bin/bash
set -euo pipefail

wd_finish() {
    wd_exit_code=$?
    if ((wd_exit_code != 0 && wd_exit_code != 130)); then
        printf '\nWatchDog stopped with code %s. Keep the message above for diagnosis.\n' "$wd_exit_code"
        if [[ -t 0 && -t 1 ]]; then
            read -r -p 'Press Return to close this launcher.' wd_reply || true
        fi
    fi
}
trap wd_finish EXIT

wd_package=$(cd "$(dirname "$0")" && pwd -P)
wd_executable="$wd_package/codex-watchdog"
wd_launcher="$wd_package/watchdog-macos.sh"
if [[ ! -x "$wd_executable" || ! -x "$wd_launcher" ]]; then
    printf '%s\n' 'Keep this launcher beside the other files from the extracted Mac ZIP.' >&2
    exit 1
fi

printf '%s\n' 'Installing Codex WatchDog for this Mac account...'
"$wd_executable" macos-install
"$wd_executable" macos-hooks --install

printf '\n%s\n' 'Starting WatchDog. Follow the messaging prompts if setup is needed.' \
    'Review the WatchDog hooks in Codex if it requests trust.' \
    'Keep this window open while WatchDog runs. Press Control-C to stop.'
"$wd_launcher" -- --interval 30
