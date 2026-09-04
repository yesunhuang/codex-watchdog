"""PyInstaller entry point for the Windows beta executable."""

import sys

from codex_watchdog.cli import main


if len(sys.argv) == 1:
    from codex_watchdog.windows_launcher import one_click_main

    raise SystemExit(one_click_main())

from codex_watchdog.windows_launcher import packaged_cli_arguments

raise SystemExit(
    main(packaged_cli_arguments(sys.argv[1:], executable=sys.executable))
)
