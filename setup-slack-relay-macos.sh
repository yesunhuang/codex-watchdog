#!/usr/bin/env bash

set -euo pipefail
umask 077

usage() {
    cat <<'EOF'
Usage: ./setup-slack-relay-macos.sh --channel-id ID --allowed-user-id ID [--allowed-user-id ID ...] [--force]

Stores Slack bot and app tokens in the current user's macOS login Keychain.
The security(1) prompts accept the tokens without placing them in shell history
or command-line arguments. Channel and allowlisted member IDs are nonsecret.
EOF
}

die() {
    printf '%s\n' "$*" >&2
    exit 1
}

channel_id=""
force=0
allowed_user_ids=()

while (($#)); do
    case "$1" in
        --channel-id)
            (($# >= 2)) || die "--channel-id requires a value."
            channel_id=$2
            shift 2
            ;;
        --allowed-user-id)
            (($# >= 2)) || die "--allowed-user-id requires a value."
            allowed_user_ids+=("$2")
            shift 2
            ;;
        --force)
            force=1
            shift
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            die "Unknown option: $1"
            ;;
    esac
done

if [[ "$(uname -s)" != "Darwin" && "${CODEX_WATCHDOG_MACOS_TEST:-}" != "1" ]]; then
    die "This setup helper requires macOS Keychain."
fi
[[ $channel_id =~ ^[CG][A-Z0-9]{8,}$ ]] ||
    die "Slack channel ID must begin with C or G and contain only uppercase letters and digits."
((${#allowed_user_ids[@]} > 0)) ||
    die "At least one --allowed-user-id is required."

unique_user_ids=()
for user_id in "${allowed_user_ids[@]}"; do
    [[ $user_id =~ ^[UW][A-Z0-9]{8,}$ ]] ||
        die "Slack member IDs must begin with U or W and contain only uppercase letters and digits."
    duplicate=0
    if ((${#unique_user_ids[@]} > 0)); then
        for existing_user_id in "${unique_user_ids[@]}"; do
            if [[ $existing_user_id == "$user_id" ]]; then
                duplicate=1
                break
            fi
        done
    fi
    ((duplicate == 1)) || unique_user_ids+=("$user_id")
done

security_bin=${CODEX_WATCHDOG_MACOS_SECURITY_BIN:-/usr/bin/security}
[[ -x $security_bin ]] || die "macOS security executable is unavailable."

config_dir=${CODEX_WATCHDOG_MACOS_CONFIG_DIR:-"${HOME}/Library/Application Support/CodexWatchdog"}
config_path="${config_dir}/slack-relay.json"
service_name="org.localcodexwatchdog.slack"
bot_account="slack-bot-token"
app_account="slack-app-token"

existing=0
[[ -e $config_path ]] && existing=1
"$security_bin" find-generic-password -s "$service_name" -a "$bot_account" >/dev/null 2>&1 && existing=1
"$security_bin" find-generic-password -s "$service_name" -a "$app_account" >/dev/null 2>&1 && existing=1
if ((existing == 1 && force == 0)); then
    die "Slack relay configuration already exists. Re-run with --force to replace it."
fi

mkdir -p "$config_dir"
chmod 700 "$config_dir"

printf '%s\n' "Enter the Slack bot token (xoxb-...) at the Keychain prompt."
"$security_bin" add-generic-password -U -a "$bot_account" -s "$service_name" \
    -l "Codex WatchDog Slack bot token" -w
printf '%s\n' "Enter the Slack app token (xapp-...) at the Keychain prompt."
"$security_bin" add-generic-password -U -a "$app_account" -s "$service_name" \
    -l "Codex WatchDog Slack app token" -w

if ! bot_value=$("$security_bin" find-generic-password -s "$service_name" -a "$bot_account" -w); then
    die "The Slack bot token could not be read back from Keychain."
fi
if ! app_value=$("$security_bin" find-generic-password -s "$service_name" -a "$app_account" -w); then
    unset bot_value
    die "The Slack app token could not be read back from Keychain."
fi
[[ $bot_value == xoxb-* ]] || {
    unset bot_value app_value
    die "The saved Slack bot token does not begin with xoxb-. Re-run with --force."
}
[[ $app_value == xapp-* ]] || {
    unset bot_value app_value
    die "The saved Slack app token does not begin with xapp-. Re-run with --force."
}
unset bot_value app_value

config_tmp=$(mktemp "${config_dir}/.slack-relay.XXXXXX")
cleanup() {
    if [[ -n ${config_tmp:-} && -e $config_tmp ]]; then
        rm -f "$config_tmp"
    fi
}
trap cleanup EXIT HUP INT TERM

{
    printf '{\n'
    printf '  "schema_version": 1,\n'
    printf '  "channel_id": "%s",\n' "$channel_id"
    printf '  "allowed_user_ids": ['
    separator=""
    for user_id in "${unique_user_ids[@]}"; do
        printf '%s"%s"' "$separator" "$user_id"
        separator=", "
    done
    printf ']\n}\n'
} >"$config_tmp"
chmod 600 "$config_tmp"
mv -f "$config_tmp" "$config_path"
config_tmp=""
trap - EXIT HUP INT TERM

printf '{"status":"saved","allowed_user_count":%d,"secret_protection":"macos_keychain_current_user"}\n' \
    "${#unique_user_ids[@]}"
