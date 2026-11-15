"""Chat command parsing and the one-line reply templates.

Parsing lives here so the bot loop and the tests share it; nothing in this module
touches the network or the state file.
"""

from dataclasses import dataclass

HELP = "commands: /help, /abb <link>, /relink [torrent_id]"
USAGE = f"unknown command. {HELP}"


@dataclass(frozen=True)
class Command:
    kind: str  # "help" | "abb" | "relink" | "usage" | "ignore"
    argument: str = ""


def added(title: str, duplicate: bool = False) -> str:
    return f"{'already added' if duplicate else 'added'}: {title}"


def permitted(
    sender: str,
    group: str | None,
    allowed: frozenset[str],
    group_id: str | None = None,
) -> bool:
    """Direct messages need an allowlisted number; the one configured group is
    trusted whole, so every member of it can use the bot."""
    if group is not None:
        return group == group_id
    return sender in allowed


def parse(text: str) -> Command:
    """Classify one message. Only slash commands are answered; anything else —
    plain chatter, bare links — is ignored so a group's conversation stays quiet."""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return Command("ignore")
    parts = stripped.split(maxsplit=1)
    head = parts[0].lower()
    argument = parts[1] if len(parts) > 1 else ""
    if head == "/relink" and argument and not (argument.isascii() and argument.isdigit()):
        return Command("usage")
    if head in ("/help", "/abb", "/relink"):
        return Command(head[1:], argument)
    return Command("usage")
