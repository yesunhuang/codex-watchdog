#!/usr/bin/env bash

set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  ./watchdog-macos.sh [--runtime PATH] [--slack-only] [--shared-slack-app] [--dry-run] [--] [RUN_OPTIONS...]
  ./watchdog-macos.sh [--runtime PATH] [--slack-only] [--shared-slack-app] --relay-test WORKSPACE

Loads Slack reply-relay credentials from the current user's macOS Keychain.
Use --slack-only to remove SMTP and Outlook settings from the child process.
Use --shared-slack-app to poll this Mac's mapped replies when other machines use the same Slack app.
Arguments after -- are passed to `codex-watchdog run`.
EOF
}

die() {
    printf '%s\n' "$*" >&2
    exit 1
}

if [[ "$(uname -s)" != "Darwin" && "${CODEX_WATCHDOG_MACOS_TEST:-}" != "1" ]]; then
    die "This launcher requires macOS Keychain."
fi

script_dir=$(cd "$(dirname "$0")" && pwd -P)
runtime_path="${script_dir}/.codex-watchdog"
packaged_executable="${script_dir}/codex-watchdog"
if [[ -x $packaged_executable ]]; then
    runtime_path=""
fi
slack_only=0
dry_run=0
relay_workspace=""

while (($#)); do
    case "$1" in
        --runtime)
            (($# >= 2)) || die "--runtime requires a value."
            runtime_path=$2
            shift 2
            ;;
        --slack-only)
            slack_only=1
            shift
            ;;
        --shared-slack-app)
            export CODEX_WATCHDOG_SLACK_REPLY_MODE=poll
            shift
            ;;
        --dry-run)
            dry_run=1
            shift
            ;;
        --relay-test)
            (($# >= 2)) || die "--relay-test requires a workspace selector."
            relay_workspace=$2
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        --)
            shift
            break
            ;;
        *)
            die "Unknown launcher option before --: $1"
            ;;
    esac
done
run_arguments=("$@")

if [[ -n $relay_workspace && ${#run_arguments[@]} -gt 0 ]]; then
    die "--relay-test cannot be combined with run options."
fi
if [[ -n $relay_workspace && $dry_run -eq 1 ]]; then
    die "--relay-test cannot be combined with --dry-run."
fi

if [[ -x $packaged_executable ]]; then
    if [[ -z $runtime_path ]]; then
        runtime_path=$("$packaged_executable" _macos-runtime)
    fi
    python_bin=""
elif [[ -n ${CODEX_WATCHDOG_PYTHON:-} ]]; then
    python_bin=$CODEX_WATCHDOG_PYTHON
elif [[ -x "${script_dir}/.venv/bin/python" ]]; then
    python_bin="${script_dir}/.venv/bin/python"
else
    python_bin=$(command -v python3 || true)
fi
[[ -x $packaged_executable || ( -n ${python_bin:-} && -x $python_bin ) ]] || die "Python 3 is unavailable."

security_bin=${CODEX_WATCHDOG_MACOS_SECURITY_BIN:-/usr/bin/security}
[[ -x $security_bin ]] || die "macOS security executable is unavailable."
config_dir=${CODEX_WATCHDOG_MACOS_CONFIG_DIR:-"${HOME}/Library/Application Support/CodexWatchdog"}
config_path="${config_dir}/slack-relay.json"
service_name="org.localcodexwatchdog.slack"

relay_names=(
    CODEX_WATCHDOG_SLACK_BOT_TOKEN
    CODEX_WATCHDOG_SLACK_APP_TOKEN
    CODEX_WATCHDOG_SLACK_CHANNEL_ID
    CODEX_WATCHDOG_SLACK_ALLOWED_USER_IDS
)
initially_set=0
for name in "${relay_names[@]}"; do
    [[ -n ${!name:-} ]] && ((initially_set += 1))
done

if [[ -z ${CODEX_WATCHDOG_SLACK_CHANNEL_ID:-} || -z ${CODEX_WATCHDOG_SLACK_ALLOWED_USER_IDS:-} ]]; then
    if [[ -f $config_path ]]; then
        if [[ -x $packaged_executable ]]; then
            config_values=$("$packaged_executable" _macos-relay-config "$config_path") ||
                die "The saved Slack relay configuration is invalid."
        elif ! config_values=$("$python_bin" - "$config_path" <<'PY'
import json
import re
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    value = json.load(handle)
channel = value.get("channel_id")
users = value.get("allowed_user_ids")
if (
    value.get("schema_version") != 1
    or not isinstance(channel, str)
    or re.fullmatch(r"[CG][A-Z0-9]{8,}", channel) is None
    or not isinstance(users, list)
    or not users
    or any(
        not isinstance(user, str)
        or re.fullmatch(r"[UW][A-Z0-9]{8,}", user) is None
        for user in users
    )
):
    raise SystemExit("invalid Slack relay configuration")
print(channel + "\t" + ",".join(dict.fromkeys(users)))
PY
        ); then
            die "The saved Slack relay configuration is invalid."
        fi
        saved_channel=${config_values%%$'\t'*}
        saved_users=${config_values#*$'\t'}
        if [[ -z ${CODEX_WATCHDOG_SLACK_CHANNEL_ID:-} ]]; then
            export CODEX_WATCHDOG_SLACK_CHANNEL_ID=$saved_channel
        fi
        if [[ -z ${CODEX_WATCHDOG_SLACK_ALLOWED_USER_IDS:-} ]]; then
            export CODEX_WATCHDOG_SLACK_ALLOWED_USER_IDS=$saved_users
        fi
        unset config_values saved_channel saved_users
    fi
fi

load_keychain_secret() {
    local account=$1
    local prefix=$2
    local value
    if ! value=$("$security_bin" find-generic-password -s "$service_name" -a "$account" -w); then
        return 1
    fi
    [[ $value == "$prefix"* ]] || {
        unset value
        return 1
    }
    printf '%s' "$value"
    unset value
}

loaded_from_keychain=0
if [[ -z ${CODEX_WATCHDOG_SLACK_BOT_TOKEN:-} ]]; then
    if "$security_bin" find-generic-password -s "$service_name" -a slack-bot-token >/dev/null 2>&1; then
        if ! bot_value=$(load_keychain_secret slack-bot-token xoxb-); then
            die "The Slack bot token in Keychain is unreadable or invalid."
        fi
        export CODEX_WATCHDOG_SLACK_BOT_TOKEN=$bot_value
        unset bot_value
        ((loaded_from_keychain += 1))
    fi
fi
if [[ -z ${CODEX_WATCHDOG_SLACK_APP_TOKEN:-} ]]; then
    if "$security_bin" find-generic-password -s "$service_name" -a slack-app-token >/dev/null 2>&1; then
        if ! app_value=$(load_keychain_secret slack-app-token xapp-); then
            die "The Slack app token in Keychain is unreadable or invalid."
        fi
        export CODEX_WATCHDOG_SLACK_APP_TOKEN=$app_value
        unset app_value
        ((loaded_from_keychain += 1))
    fi
fi

configured_count=0
for name in "${relay_names[@]}"; do
    [[ -n ${!name:-} ]] && ((configured_count += 1))
done
if ((configured_count > 0 && configured_count < ${#relay_names[@]})); then
    die "Slack reply relay configuration is incomplete. Run setup-slack-relay-macos.sh."
fi

relay_source="not_configured"
if ((configured_count == ${#relay_names[@]})); then
    [[ ${CODEX_WATCHDOG_SLACK_BOT_TOKEN} == xoxb-* ]] || die "Slack bot token is invalid."
    [[ ${CODEX_WATCHDOG_SLACK_APP_TOKEN} == xapp-* ]] || die "Slack app token is invalid."
    [[ ${CODEX_WATCHDOG_SLACK_CHANNEL_ID} =~ ^[CG][A-Z0-9]{8,}$ ]] || die "Slack channel ID is invalid."
    IFS=',; ' read -r -a allowed_users <<<"${CODEX_WATCHDOG_SLACK_ALLOWED_USER_IDS}"
    ((${#allowed_users[@]} > 0)) || die "Slack allowed user list is empty."
    for user_id in "${allowed_users[@]}"; do
        [[ $user_id =~ ^[UW][A-Z0-9]{8,}$ ]] || die "Slack allowed user ID is invalid."
    done
    unset allowed_users user_id
    if ((initially_set == ${#relay_names[@]})); then
        relay_source="environment"
    elif ((initially_set > 0)); then
        relay_source="mixed"
    elif ((loaded_from_keychain == 2)); then
        relay_source="macos_keychain"
    else
        relay_source="saved_config"
    fi
fi

if ((slack_only == 1)); then
    unset CODEX_WATCHDOG_SMTP_HOST
    unset CODEX_WATCHDOG_SMTP_PORT
    unset CODEX_WATCHDOG_SMTP_AUTH
    unset CODEX_WATCHDOG_SMTP_USERNAME
    unset CODEX_WATCHDOG_SMTP_PASSWORD
    unset CODEX_WATCHDOG_SMTP_FROM
    unset CODEX_WATCHDOG_SMTP_TO
    unset CODEX_WATCHDOG_SMTP_SECURITY
    unset CODEX_WATCHDOG_OUTLOOK_CLIENT_ID
fi

smtp_configured=false
if [[ -n ${CODEX_WATCHDOG_SMTP_HOST:-} && -n ${CODEX_WATCHDOG_SMTP_FROM:-} && -n ${CODEX_WATCHDOG_SMTP_TO:-} ]]; then
    smtp_configured=true
fi

if ((dry_run == 1)); then
    if [[ -x $packaged_executable ]]; then
        exec "$packaged_executable" _macos-launcher-summary "$runtime_path" "$relay_source" "$smtp_configured" "$slack_only"
    fi
    "$python_bin" - "$runtime_path" "$relay_source" "$smtp_configured" "$slack_only" <<'PY'
import json
import os
import sys

print(
    json.dumps(
        {
            "status": "ready",
            "runtime": sys.argv[1],
            "slack_reply": sys.argv[2],
            "slack_reply_mode": os.environ.get("CODEX_WATCHDOG_SLACK_REPLY_MODE", "socket"),
            "smtp_configured": sys.argv[3] == "true",
            "slack_only": sys.argv[4] == "1",
        },
        sort_keys=True,
    )
)
PY
    exit 0
fi

((configured_count == ${#relay_names[@]})) ||
    die "Slack reply relay is not configured. Run setup-slack-relay-macos.sh first."

if [[ -x $packaged_executable ]]; then
    launcher_command=("$packaged_executable")
else
    launcher="${script_dir}/tools/codex_watchdog.py"
    [[ -f $launcher ]] || die "WatchDog Python launcher is unavailable."
    launcher_command=("$python_bin" "$launcher")
fi

printf 'status: ready\nruntime: %s\nslack_reply: %s\nsmtp_configured: %s\n' \
    "$runtime_path" "$relay_source" "$smtp_configured"

if [[ -n $relay_workspace ]]; then
    test_id="slack-relay-$(date -u +%Y%m%dT%H%M%SZ)-$$"
    exec "${launcher_command[@]}" --runtime "$runtime_path" slack-relay-test \
        --id "$test_id" --workspace "$relay_workspace"
fi

exec "${launcher_command[@]}" --runtime "$runtime_path" run "${run_arguments[@]}"
