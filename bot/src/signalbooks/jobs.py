"""Pending queue, poll loop, and the state file.

One mylist walk per poll serves every pending job; idle means no HTTP at all.
Completed torrents stay on TorBox, so delivery only stores that a reply was sent.
"""

import json
import os
import pathlib
import time
from dataclasses import dataclass

from . import abb, torbox

POLL_SECONDS = 30.0
DEDUP_SECONDS = 600
# A torrent TorBox no longer lists is retried for a couple of minutes before it
# is reported as gone; that keeps a transient pagination miss from ending a job.
MISS_LIMIT = 5
DEFAULT_STATE = pathlib.Path("state.json")


class JobsError(Exception):
    """Expected failure: unreadable or malformed state file."""


@dataclass(frozen=True)
class Added:
    torrent_id: int
    title: str
    duplicate: bool = False


@dataclass(frozen=True)
class Delivery:
    torrent_id: int
    chat: str
    title: str
    url: str = ""
    error: str = ""


def _key(chat: str, torrent_id: int) -> str:
    return f"{chat}:{torrent_id}"


class State:
    """``{"<chat>:<torrent_id>": {chat, title, url, added_at, sent, misses}}``.

    Jobs are per chat, so the same torrent requested in two chats still gets two
    replies. Dict order is add/delivery order, which is what bare ``/relink``
    leans on.
    """

    def __init__(self, path: pathlib.Path = DEFAULT_STATE):
        self.path = pathlib.Path(path)
        self.jobs: dict[str, dict] = self._read()

    def _read(self) -> dict[str, dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise JobsError(f"cannot read {self.path}: {exc}") from exc
        if not isinstance(data, dict):
            raise JobsError(f"{self.path} is not a job map")
        for key, job in data.items():
            chat, _, torrent_id = key.rpartition(":")
            if not (
                chat
                and torrent_id.isdigit()
                and isinstance(job, dict)
                and job.get("chat") == chat
                and "title" in job
            ):
                raise JobsError(f"{self.path}: bad job entry {key!r}")
        return data

    def save(self) -> None:
        """Write via a sibling temp file and rename, so a crash mid-write can
        never truncate the replies already recorded."""
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(self.jobs, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def pending(self) -> list[tuple[str, int, dict]]:
        return [
            (job["chat"], int(key.rpartition(":")[2]), job)
            for key, job in self.jobs.items()
            if not job.get("sent")
        ]

    def get(self, torrent_id: int, chat: str) -> dict | None:
        return self.jobs.get(_key(chat, torrent_id))

    def add(self, torrent_id: int, chat: str, title: str, url: str, now: float | None = None) -> None:
        key = _key(chat, torrent_id)
        self.jobs.pop(key, None)
        self.jobs[key] = {
            "chat": chat,
            "title": title,
            "url": url,
            "added_at": int(time.time() if now is None else now),
            "sent": False,
            "misses": 0,
        }

    def record_delivery(self, torrent_id: int, chat: str, title: str = "") -> None:
        key = _key(chat, torrent_id)
        job = self.jobs.pop(key, None) or {
            "chat": chat,
            "title": title,
            "url": "",
            "added_at": int(time.time()),
            "misses": 0,
        }
        job["sent"] = True
        self.jobs[key] = job

    def recent(self, chat: str, url: str, now: float | None = None) -> int | None:
        """The same link from the same chat inside the dedup window."""
        cutoff = (time.time() if now is None else now) - DEDUP_SECONDS
        for key, job in self.jobs.items():
            if (
                job.get("chat") == chat
                and job.get("url") == url
                and job.get("added_at", 0) >= cutoff
            ):
                return int(key.rpartition(":")[2])
        return None

    def last_sent(self, chat: str) -> int | None:
        for key in reversed(self.jobs):
            job = self.jobs[key]
            if job.get("chat") == chat and job.get("sent"):
                return int(key.rpartition(":")[2])
        return None


async def add(
    api_key: str, state: State, chat: str, book: abb.Book, now: float | None = None
) -> Added:
    """Dedup, then reuse TorBox's torrent for this hash or add it."""
    duplicate = state.recent(chat, book.url, now)
    if duplicate is not None:
        job = state.get(duplicate, chat) or {}
        return Added(duplicate, job.get("title") or book.title, duplicate=True)
    existing = await torbox.find_by_hash(api_key, book.info_hash)
    if existing:
        torrent_id = existing.id
    else:
        magnet = abb.build_magnet(book.info_hash, book.title)
        torrent_id, _ = await torbox.create_torrent(api_key, magnet)
    state.add(torrent_id, chat, book.title, book.url, now)
    state.save()
    return Added(torrent_id, book.title)


async def link_for(api_key: str, torrent: torbox.Torrent) -> str:
    """Multi-file audiobooks come back as one zip; a single file is linked
    directly."""
    if not torrent.files:
        raise torbox.TorboxError("TorBox has no files for this torrent yet")
    if len(torrent.files) > 1:
        return await torbox.request_link(api_key, torrent.id, zip_link=True)
    return await torbox.request_link(api_key, torrent.id, file_id=torrent.files[0].id)


async def poll(api_key: str, state: State) -> list[Delivery]:
    """Refresh every pending job with one mylist walk and report the ones that
    are ready.

    Nothing is marked delivered here: the caller records each reply after it is
    actually sent, so a crash in between repeats a reply instead of losing it.
    """
    pending = state.pending()
    if not pending:
        return []
    items = await torbox.find_many(api_key, {torrent_id for _, torrent_id, _ in pending}, files=True)
    deliveries: list[Delivery] = []
    for chat, torrent_id, job in pending:
        item = items.get(torrent_id)
        if item is None:
            job["misses"] = job.get("misses", 0) + 1
            if job["misses"] >= MISS_LIMIT:
                deliveries.append(
                    Delivery(torrent_id, chat, job["title"], error="torrent is gone from TorBox")
                )
            continue
        job["misses"] = 0
        if item.failed:
            deliveries.append(
                Delivery(torrent_id, chat, job["title"], error=f"torrent is {item.state}")
            )
        elif item.done and item.files:
            try:
                url = await link_for(api_key, item)
            except torbox.TorboxError as exc:
                deliveries.append(Delivery(torrent_id, chat, job["title"], error=str(exc)))
                continue
            deliveries.append(Delivery(torrent_id, chat, job["title"], url=url))
    state.save()
    return deliveries
