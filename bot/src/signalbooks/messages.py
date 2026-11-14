"""Chat command parsing and the one-line reply templates.

Parsing lives here so the bot loop and the tests share it; nothing in this module
touches the network or the state file.
"""

from dataclasses import dataclass

HELP = "commands: /help, /abb <link>, /relink [torrent_id]"
USAGE = f"unknown command. {HELP}"


@dataclass(frozen=True)
class Command:
    kind: str  # "help" | "abb" | "relink" | "usage"
    argument: str = ""


def added(title: str, duplicate: bool = False) -> str:
    return f"{'already added' if duplicate else 'added'}: {title}"


def permitted(sender: str, group: str | None, allowed: frozenset[str]) -> bool:
    """Chat access policy: direct messages from an allowlisted number only. The
    private group opens after the 1:1 flow is proven."""
    return group is None and sender in allowed


def parse(text: str) -> Command:
    """Classify one message. A bare link is shorthand for ``/abb``; anything that
    is not a known command comes back as a usage reply, never an exception."""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return Command("abb", stripped)
    parts = stripped.split(maxsplit=1)
    head = parts[0].lower()
    argument = parts[1] if len(parts) > 1 else ""
    if head == "/relink" and argument and not (argument.isascii() and argument.isdigit()):
        return Command("usage")
    if head in ("/help", "/abb", "/relink"):
        return Command(head[1:], argument)
    return Command("usage")
