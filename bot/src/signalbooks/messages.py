"""Chat command parsing and the reply templates.

Parsing lives here so the bot loop and the tests share it; nothing in this module
touches the network or the state file. Replies are plain text: Signal renders no
Markdown typed into a message body, so the layout carries the hierarchy.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .jobs import Delivery

HELP = (
    "SignalBooks: send /abb with a book page link. A bare link is ignored.\n"
    "\n"
    "/abb <link> — add a book from audiobookbay.lu\n"
    "/relink — fresh link for the last book in this chat\n"
    "/relink <torrent id> — fresh link for a specific torrent\n"
    "/help — this message\n"
    "\n"
    "Start the download within about 3 hours; /relink sends a new link."
)


@dataclass(frozen=True)
class Command:
    kind: str  # "help" | "abb" | "relink" | "usage" | "ignore"
    argument: str = ""


def usage(context: str = "") -> str:
    """The shortest correct recovery for a malformed command."""
    if context == "abb":
        return "Usage: /abb <link>"
    if context == "relink":
        return "Usage: /relink (last book in this chat) or /relink <torrent id>"
    return "Unknown command. /help lists what I can do."


def added(
    title: str, duplicate: bool = False, torrent_id: int = 0, sent: bool = False
) -> str:
    if duplicate:
        if sent:
            hint = f"/relink {torrent_id}" if torrent_id else "/relink"
            return f"Already added: {title}\n{hint} for a fresh one."
        return f"Already added: {title}\nThe link is still coming."
    return f"Added: {title}\nLink coming when it's ready."


def delivery(job: "Delivery") -> str:
    """One finish message: which book, the link, what the file looks like, and
    how to start over once the presigned link expires."""
    if job.error:
        reason = job.error.strip()
        if not reason:
            return f"Couldn't get {job.title}."
        if reason[-1] not in ".!?)":
            reason += "."
        return f"Couldn't get {job.title}. {reason}"
    lines = [job.title, job.url]
    facts = _file_facts(job.files, job.size, job.zipped)
    if facts:
        lines.append(facts)
    lines.append(
        f"Start the download within about 3 hours · /relink {job.torrent_id} for a new one"
    )
    return "\n".join(lines)


def relink(url: str, title: str = "", torrent_id: int = 0) -> str:
    label = title or (f"torrent {torrent_id}" if torrent_id else "that book")
    return f"Fresh link for {label}\n{url}\nStart the download within about 3 hours."


def failure(exc: Exception) -> str:
    """Turn an expected one-line exception into a chat sentence, so the reply
    reads like a message rather than a log line."""
    text = str(exc).strip()
    if not text:
        return "Something went wrong. Try again."
    text = text[0].upper() + text[1:]
    if text[-1] not in ".!?)":
        text += "."
    return text


def size_text(size: int) -> str:
    if size <= 0:
        return ""
    if size >= 999_500_000:
        value = f"{size / 1_000_000_000:.1f}".removesuffix(".0")
        return f"{value} GB"
    if size >= 999_500:
        return f"{size / 1_000_000:.0f} MB"
    if size >= 1_000:
        return f"{size / 1_000:.0f} KB"
    return f"{size} B"


def _file_facts(files: int, size: int, zipped: bool) -> str:
    if files <= 0:
        return ""
    parts = ["Zip"] if zipped else []
    if zipped:
        parts.append(f"{files} files")
    else:
        parts.append("1 file" if files == 1 else f"{files} files")
    if size > 0:
        parts.append(size_text(size))
    return " · ".join(parts)


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
        return Command("usage", "relink")
    if head == "/abb" and not argument:
        return Command("usage", "abb")
    if head in ("/help", "/abb", "/relink"):
        return Command(head[1:], argument)
    return Command("usage", "")
