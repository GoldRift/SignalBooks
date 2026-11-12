"""Environment configuration. Everything is optional at parse time; the CLI
decides what a given command actually needs."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    torbox_api_key: str
    abb_cookie: str | None


def load(env: dict[str, str] | None = None) -> Config:
    values = os.environ if env is None else env
    return Config(
        torbox_api_key=values.get("TORBOX_API_KEY", "").strip(),
        abb_cookie=values.get("ABB_COOKIE", "").strip() or None,
    )
