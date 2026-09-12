"""Presentation-only identity of the WatchDog sending a Slack message."""

from socket import gethostname
from sys import platform as host_platform


def slack_message_with_host(text: str) -> str:
    # macOS keeps its existing presentation; this feature is Windows/Linux only.
    if host_platform not in ("win32", "linux"):
        return text
    try:
        host = gethostname().strip()
    except OSError:
        host = ""
    # OS hostnames are normally DNS/NetBIOS names. Keep the label one line and
    # prevent unusual local names from becoming Slack markup or mentions.
    host = "".join(c if c.isalnum() or c in ".-_" else "_" for c in host)[:255]
    return f"WatchDog host: {host or 'unavailable'}\n{text}"
