"""Environment configuration. Everything is optional at parse time; the command
that actually needs a value is the one that rejects it."""

import os
import pathlib
from dataclasses import dataclass

DEFAULT_SOCKET = pathlib.Path("/run/signal-cli/socket")


@dataclass(frozen=True)
class Config:
    torbox_api_key: str
    abb_cookie: str | None
    signal_account: str
    signal_socket: pathlib.Path
    allowed_numbers: frozenset[str]
    signal_group_id: str | None


def load(env: dict[str, str] | None = None) -> Config:
    values = os.environ if env is None else env
    socket = values.get("SIGNAL_SOCKET", "").strip()
    return Config(
        torbox_api_key=values.get("TORBOX_API_KEY", "").strip(),
        abb_cookie=values.get("ABB_COOKIE", "").strip() or None,
        signal_account=values.get("SIGNAL_ACCOUNT", "").strip(),
        signal_socket=pathlib.Path(socket) if socket else DEFAULT_SOCKET,
        allowed_numbers=frozenset(
            number.strip()
            for number in values.get("SIGNAL_ALLOWED_NUMBERS", "").split(",")
            if number.strip()
        ),
        signal_group_id=values.get("SIGNAL_GROUP_ID", "").strip() or None,
    )
