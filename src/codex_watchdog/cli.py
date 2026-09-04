from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Optional, Sequence

from . import __version__
from .hook_config import (
    build_packaged_hooks_document,
    install_hooks,
    installation_result,
    render_hooks_document,
)
from .models import sha256_text, validate_instruction_id
from .mvp_service import MvpWatchdogService
from .notifications import EnvironmentNotifier, NotificationConfig, NotificationEvent
from .queue_wake import QueueWakeDispatcher
from .service import RunOnceService
from .slack_mapping import SlackRelayTarget
from .stop_hook import HookSettings, run_hook
from .storage import InstructionStore
from .workspace_discovery import EffectiveWorkspaceCatalog
from .workspace_registry import REGISTRY_SCHEMA_VERSION, WorkspaceRegistry


def _path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def _positive_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if seconds <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return seconds


def _safe_id(value: str) -> str:
    try:
        return validate_instruction_id(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-watchdog")
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument("--runtime", type=_path, default=_path(".codex-watchdog"))
    parser.add_argument(
        "--codex-home", type=_path, help="exact Codex home shared with queue state"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    hook = commands.add_parser(
        "hook", help="handle one native Codex hook event from stdin"
    )
    hook.add_argument("--grace-seconds", type=float, default=600.0)
    hook.add_argument("--poll-seconds", type=float, default=0.5)
    hook.add_argument("--test-mode", action="store_true", help=argparse.SUPPRESS)

    submit = commands.add_parser("submit", help="publish one short-stop instruction")
    submit.add_argument("--id", required=True)
    submit.add_argument(
        "--thread", required=True, help="target Codex session/thread id"
    )
    submit.add_argument("--source", default="manual")
    prompt = submit.add_mutually_exclusive_group(required=True)
    prompt.add_argument("--message")
    prompt.add_argument("--prompt-file", type=_path)

    queue = commands.add_parser(
        "queue", help="wake an existing thread with codex queue"
    )
    queue.add_argument("--thread", required=True)
    queue.add_argument("--id", required=True)
    queue.add_argument("--source", default="manual")
    queue.add_argument("--queue-db", type=_path)
    queue_prompt = queue.add_mutually_exclusive_group(required=True)
    queue_prompt.add_argument("--message")
    queue_prompt.add_argument("--prompt-file", type=_path)

    queue_observe = commands.add_parser(
        "queue-observe", help="observe whether an enqueued message started"
    )
    queue_observe.add_argument("--id", required=True)
    queue_observe.add_argument("--queue-db", type=_path)

    remote = commands.add_parser("queue-remote-update")
    remote.add_argument("--thread", required=True)
    remote.add_argument("--oid", required=True)
    remote.add_argument(
        "--workspace", help="stable workspace identity for deduplication"
    )
    remote.add_argument("--queue-db", type=_path)

    resume = commands.add_parser("queue-resume-prompt")
    resume.add_argument("--thread", required=True)
    resume.add_argument("--queue-db", type=_path)

    workspace_add = commands.add_parser(
        "workspace-add", help="register one process-local workspace"
    )
    workspace_add.add_argument("--workspace", required=True)
    workspace_add.add_argument("--repo", required=True)
    workspace_add.add_argument("--thread", required=True)

    workspace_remove = commands.add_parser(
        "workspace-remove", help="remove one durable manual workspace override"
    )
    workspace_remove.add_argument("--workspace", required=True, type=_safe_id)

    commands.add_parser("workspace-list", help="list registered workspaces")
    commands.add_parser(
        "workspace-discover",
        help="read all open VS Code windows and resolve safe effective workspaces",
    ).add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="REPO",
        help="exclude an exact repository name, path, or VS Code workspace URI",
    )
    service_once = commands.add_parser(
        "service-once", help="persist one deterministic Git sensor pass"
    )
    service_once.add_argument(
        "--manual-only",
        action="store_true",
        help="disable live VS Code discovery and use only explicit registrations",
    )
    service_once.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="REPO",
        help="exclude an exact auto-discovered repository name, path, or URI",
    )
    run = commands.add_parser(
        "run", help="run the foreground Stop, Git, notification, and wake loop"
    )
    run.add_argument("--interval", type=_positive_seconds, default=300.0)
    run.add_argument(
        "--once", action="store_true", help="run one immediate cycle and exit"
    )
    run.add_argument(
        "--replay-latest-stop",
        action="store_true",
        help=(
            "on first use of a runtime, process only the latest matching completed "
            "Stop audit"
        ),
    )
    run.add_argument(
        "--manual-only",
        action="store_true",
        help="disable live VS Code discovery and use only explicit registrations",
    )
    run.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="REPO",
        help="exclude an exact auto-discovered repository name, path, or URI",
    )
    notify_test = commands.add_parser(
        "notify-test", help="send one direct, nonsecret watchdog test notification"
    )
    notify_test.add_argument(
        "--id",
        required=True,
        type=_safe_id,
        help="unique test id; a new id deliberately bypasses notification debounce",
    )
    notify_test.add_argument("--workspace", type=_safe_id, default="notification-test")
    relay_test = commands.add_parser(
        "slack-relay-test",
        help="post one reply-enabled test for an auto-discovered local workspace",
    )
    relay_test.add_argument("--id", required=True, type=_safe_id)
    relay_test.add_argument(
        "--workspace",
        help="exact workspace id, repository name, or canonical local path",
    )
    commands.add_parser(
        "outlook-login",
        help="authorize personal Outlook SMTP using a one-time Microsoft device code",
    )
    user_hooks = commands.add_parser(
        "install-user-hooks",
        help="render or conservatively install hooks that invoke the packaged executable",
    )
    user_hooks.add_argument("--grace-seconds", type=float, default=600.0)
    user_hooks.add_argument("--poll-seconds", type=float, default=0.5)
    user_hooks.add_argument("--test-mode", action="store_true", help=argparse.SUPPRESS)
    user_hooks.add_argument("--executable", type=_path, help=argparse.SUPPRESS)
    user_hooks.add_argument(
        "--install",
        action="store_true",
        help="write hooks.json only when it is missing or already equivalent",
    )
    return parser


def _prompt(message: Optional[str], prompt_file: Optional[Path]) -> str:
    if message is not None:
        return message
    assert prompt_file is not None
    return prompt_file.read_text(encoding="utf-8")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "install-user-hooks":
        executable = args.executable
        if executable is None:
            if not getattr(sys, "frozen", False):
                print(
                    json.dumps(
                        {
                            "status": "packaged_executable_required",
                            "reason": (
                                "use tools/install_user_hooks.py for a source checkout"
                            ),
                        },
                        sort_keys=True,
                    )
                )
                return 1
            executable = Path(sys.executable).resolve()
        document = build_packaged_hooks_document(
            executable,
            args.runtime,
            args.grace_seconds,
            args.poll_seconds,
            args.test_mode,
        )
        rendered = render_hooks_document(document)
        if not args.install:
            sys.stdout.write(rendered)
            return 0
        codex_home = args.codex_home or _path("~/.codex")
        status, path = install_hooks(codex_home, document)
        print(
            json.dumps(
                installation_result(status, path, rendered),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "hook":
        return run_hook(
            HookSettings(
                runtime=args.runtime,
                grace_seconds=args.grace_seconds,
                poll_seconds=args.poll_seconds,
                test_mode=args.test_mode,
            )
        )
    if args.command == "submit":
        result = InstructionStore(args.runtime).submit(
            args.id,
            args.source,
            _prompt(args.message, args.prompt_file),
            target_session_id=args.thread,
        )
        print(
            json.dumps(
                {
                    "instruction_id": result.instruction.instruction_id,
                    "status": result.status,
                    "path": str(result.path),
                }
            )
        )
        return 0
    if args.command == "workspace-add":
        result = WorkspaceRegistry(args.runtime).add(
            args.workspace, args.repo, args.thread
        )
        print(
            json.dumps(
                {"status": result.status, "workspace": result.workspace.to_dict()},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "workspace-list":
        workspaces = WorkspaceRegistry(args.runtime).list_workspaces()
        print(
            json.dumps(
                {
                    "schema_version": REGISTRY_SCHEMA_VERSION,
                    "workspaces": [workspace.to_dict() for workspace in workspaces],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "workspace-remove":
        result = WorkspaceRegistry(args.runtime).remove(args.workspace)
        print(
            json.dumps(
                {
                    "status": result.status,
                    "workspace": (
                        result.workspace.to_dict()
                        if result.workspace is not None
                        else None
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "workspace-discover":
        snapshot = EffectiveWorkspaceCatalog(
            args.runtime, codex_home=args.codex_home, exclude=args.exclude
        ).snapshot(persist=False)
        print(json.dumps(snapshot.to_dict(), ensure_ascii=False, sort_keys=True))
        return 0 if snapshot.status in ("ok", "partial") else 1
    if args.command == "service-once":
        registry = (
            WorkspaceRegistry(args.runtime)
            if args.manual_only
            else EffectiveWorkspaceCatalog(
                args.runtime,
                codex_home=args.codex_home,
                exclude=args.exclude,
            )
        )
        result = RunOnceService(args.runtime, registry=registry).run_once()
        print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
        return 0 if result.ok else 1
    if args.command == "notify-test":
        result = EnvironmentNotifier(args.runtime).notify(
            NotificationEvent(
                workspace_id=args.workspace,
                event_type="notification_test",
                transition_fingerprint=sha256_text(f"notification_test\0{args.id}"),
                subject="[Codex Watchdog TEST] Notification delivery check",
                message=(
                    "This is a direct Codex Watchdog test notification. "
                    "No action is required."
                ),
            )
        )
        print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
        return 0 if result.status in ("sent", "sent_fallback") else 1
    if args.command == "slack-relay-test":
        notifier = EnvironmentNotifier(args.runtime)
        if not notifier.config.slack_relay_configured:
            print(
                json.dumps(
                    {
                        "status": "configuration_error",
                        "configuration_issues": list(
                            notifier.config.configuration_issues
                        )
                        or ["slack_relay_not_configured"],
                    },
                    sort_keys=True,
                )
            )
            return 1
        catalog = EffectiveWorkspaceCatalog(args.runtime, codex_home=args.codex_home)
        workspaces = catalog.list_workspaces()
        selector = args.workspace.casefold() if args.workspace else None
        matches = [
            workspace
            for workspace in workspaces
            if selector is None
            or selector
            in {
                workspace.workspace_id.casefold(),
                workspace.repo_root.name.casefold(),
                str(workspace.repo_root).casefold(),
            }
        ]
        if len(matches) != 1:
            print(
                json.dumps(
                    {
                        "status": "workspace_ambiguous",
                        "match_count": len(matches),
                    },
                    sort_keys=True,
                )
            )
            return 1
        workspace = matches[0]
        relay_event = NotificationEvent(
            workspace_id=workspace.workspace_id,
            event_type="slack_relay_test",
            transition_fingerprint=sha256_text(f"slack_relay_test\0{args.id}"),
            subject=f"[Codex Watchdog RELAY TEST] {args.id}",
            message=(
                "Reply in this Slack thread. WatchDog will relay your text "
                "without interpreting it to the exact existing VS Code Codex "
                "thread shown by this workspace."
            ),
            relay_target=SlackRelayTarget(
                workspace_id=workspace.workspace_id,
                thread_id=workspace.session_id,
                execution_locality="process_local",
            ),
        )
        result = notifier.notify(relay_event)
        mapping_created = notifier.slack_thread_store.has_notification_mapping(
            relay_event.event_fingerprint()
        )
        output = result.to_dict()
        output["relay_mapping"] = "created" if mapping_created else "missing"
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return (
            0
            if result.status == "sent" and result.channel == "slack" and mapping_created
            else 1
        )
    if args.command == "outlook-login":
        from .outlook_oauth import OutlookOAuthError, OutlookOAuthTokenProvider

        try:
            config = NotificationConfig.from_environment()
        except ValueError:
            print(
                json.dumps(
                    {
                        "status": "configuration_error",
                        "configuration_issues": ["invalid_notification_environment"],
                    },
                    sort_keys=True,
                )
            )
            return 1
        if config.smtp_auth != "outlook_oauth2" or not config.smtp_configured:
            print(
                json.dumps(
                    {
                        "status": "configuration_error",
                        "configuration_issues": list(config.configuration_issues)
                        or ["outlook_oauth_configuration_incomplete"],
                    },
                    sort_keys=True,
                )
            )
            return 1
        assert config.outlook_client_id is not None
        assert config.smtp_username is not None

        def display_device_code(prompt) -> None:
            print(
                json.dumps(
                    {
                        "status": "authorization_required",
                        "verification_uri": prompt.verification_uri,
                        "user_code": prompt.user_code,
                        "expires_in_seconds": prompt.expires_in_seconds,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                flush=True,
            )

        try:
            provider = OutlookOAuthTokenProvider(
                client_id=config.outlook_client_id,
                username=config.smtp_username,
                timeout_seconds=config.timeout_seconds,
            )
            login_result = provider.login_device_code(display_device_code)
        except KeyboardInterrupt:
            print(json.dumps({"status": "cancelled"}, sort_keys=True))
            return 130
        except OutlookOAuthError as exc:
            print(
                json.dumps(
                    {"status": "authorization_failed", "reason": exc.code},
                    sort_keys=True,
                )
            )
            return 1
        print(json.dumps(login_result.to_dict(), sort_keys=True))
        return 0
    if args.command == "run":
        service_options = {
            "codex_home": args.codex_home,
            "replay_latest_stop": args.replay_latest_stop,
        }
        if args.manual_only:
            service_options["auto_discovery"] = False
        elif args.exclude:
            service_options["discovery_exclude"] = tuple(args.exclude)
        service = MvpWatchdogService(args.runtime, **service_options)
        if args.once:
            result = service.run_once()
            print(
                json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True),
                flush=True,
            )
            return 0 if result.ok else 1
        return service.run(
            interval_seconds=args.interval,
            emit=lambda value: print(
                json.dumps(value, ensure_ascii=False, sort_keys=True), flush=True
            ),
        )

    dispatcher = QueueWakeDispatcher(
        args.runtime,
        codex_home=args.codex_home,
        queue_database=getattr(args, "queue_db", None),
    )
    if args.command == "queue-observe":
        receipt = dispatcher.observe_delivery(args.id, queue_database=args.queue_db)
    elif args.command == "queue":
        receipt = dispatcher.dispatch(
            args.thread,
            args.id,
            _prompt(args.message, args.prompt_file),
            args.source,
        )
    elif args.command == "queue-remote-update":
        receipt = dispatcher.dispatch_remote_update(
            args.thread, args.oid, workspace_id=args.workspace
        )
    else:
        receipt = dispatcher.claim_and_dispatch_resume_prompt(args.thread)
        if receipt is None:
            print(json.dumps({"status": "no_resume_prompt"}))
            return 0
    print(json.dumps(receipt.__dict__, ensure_ascii=False))
    return 0 if receipt.status in ("enqueued", "consumed_or_started", "started") else 1


if __name__ == "__main__":
    sys.exit(main())
