"""TorBox API: add a torrent, list torrents, request a download link.

Only these three calls exist; the API shapes are pinned in PLAN.md. Every
response is wrapped in ``{success, error, detail, data}``.
"""

from dataclasses import dataclass

import httpx

BASE = "https://api.torbox.app/v1/api"
# Every torrent call lives under /torrents/ (create, list, requestdl).
TORRENTS = "torrents"
TIMEOUT = 30
PAGE_SIZE = 100
# TorBox pages mylist at 100 items. Ten thousand torrents is far past a personal
# account; the cap only stops a misbehaving API from looping forever.
MAX_PAGES = 100


class TorboxError(Exception):
    """Expected failure: rejected request, bad response, or missing data."""


@dataclass(frozen=True)
class File:
    id: int
    name: str
    size: int


@dataclass(frozen=True)
class Torrent:
    id: int
    hash: str
    name: str
    state: str
    finished: bool
    cached: bool
    files: tuple[File, ...]
    progress: float = 0.0
    size: int = 0  # total bytes; 0 when TorBox has not measured it yet

    @property
    def done(self) -> bool:
        """The torrent's data is ready to link, whether it just finished or was
        already cached."""
        return self.finished or self.cached

    @property
    def failed(self) -> bool:
        return self.state.lower().startswith(("error", "stalled"))


def _torrent(item: dict) -> Torrent:
    try:
        files = tuple(
            File(id=int(f["id"]), name=f.get("name") or "", size=int(f.get("size") or 0))
            for f in item.get("files") or []
        )
        return Torrent(
            id=int(item["id"]),
            hash=(item.get("hash") or "").lower(),
            name=item.get("name") or "",
            state=item.get("download_state") or "",
            finished=bool(item.get("download_finished")),
            cached=bool(item.get("cached")),
            files=files,
            progress=float(item.get("progress") or 0),
            size=int(item.get("size") or 0),
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        raise TorboxError("TorBox sent an unexpected reply. Try again in a minute") from None


def _data(response: httpx.Response) -> object:
    try:
        body = response.json()
    except ValueError:
        raise TorboxError(f"TorBox returned HTTP {response.status_code}. Try again later") from None
    if not isinstance(body, dict):
        raise TorboxError("TorBox sent an unexpected reply. Try again in a minute")
    if not body.get("success"):
        detail = body.get("detail") or body.get("error") or f"HTTP {response.status_code}"
        raise TorboxError(f"TorBox: {detail}")
    return body.get("data")


async def _request(
    client: httpx.AsyncClient, method: str, path: str, api_key: str, **kwargs
) -> object:
    try:
        response = await client.request(
            method,
            f"{BASE}/{path}",
            headers={"Authorization": f"Bearer {api_key}"},
            **kwargs,
        )
    except httpx.HTTPError as exc:
        raise TorboxError("couldn't reach TorBox. Try again in a minute") from exc
    return _data(response)


async def create_torrent(api_key: str, magnet: str) -> tuple[int, str]:
    """Add a magnet and return ``(torrent_id, info_hash)``.

    An already-cached torrent comes back with its own fresh ``torrent_id`` and
    ``detail`` saying so, so callers can deliver without waiting.
    """
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        data = await _request(client, "POST", f"{TORRENTS}/createtorrent", api_key, data={"magnet": magnet})
    if not isinstance(data, dict):
        raise TorboxError("TorBox sent an unexpected reply. Try again in a minute")
    if not data.get("torrent_id"):
        if data.get("queued_id"):
            active = data.get("current_active_downloads")
            limit = data.get("active_limit")
            counts = f" ({active}/{limit})" if active is not None and limit is not None else ""
            raise TorboxError(
                "TorBox is at its download limit"
                f"{counts} and queued this book. Send /abb with the book link again once a slot frees up"
            )
        raise TorboxError("TorBox accepted the book but returned no torrent id")
    return int(data["torrent_id"]), (data.get("hash") or "").lower()


async def _scan(api_key: str, files: bool, keep, stop) -> list[Torrent]:
    """Walk mylist pages once, collecting matches, until ``stop`` says enough."""
    found: list[Torrent] = []
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        for page in range(MAX_PAGES):
            params = {
                "limit": PAGE_SIZE,
                "offset": page * PAGE_SIZE,
                "bypass_cache": "true",
            }
            if files:
                params["files"] = "true"
            items = await _request(client, "GET", f"{TORRENTS}/mylist", api_key, params=params) or []
            if not isinstance(items, list):
                raise TorboxError("TorBox sent an unexpected torrent list. Try again in a minute")
            for item in items:
                torrent = _torrent(item)
                if keep(torrent):
                    found.append(torrent)
            if len(items) < PAGE_SIZE or stop(found):
                return found
    return found


async def find_by_hash(api_key: str, info_hash: str, files: bool = False) -> Torrent | None:
    """Find an existing torrent for dedup. TorBox may report duplicates itself
    only after the fact, so the hash is looked up before adding.

    Failed torrents are not reused: they would be adopted and reported as failed
    again, so the caller gets to add a fresh magnet instead.
    """
    wanted = info_hash.lower()
    found = await _scan(api_key, files, lambda t: t.hash == wanted and not t.failed, bool)
    return found[0] if found else None


async def find_many(api_key: str, ids: set[int], files: bool = False) -> dict[int, Torrent]:
    """One mylist walk for all pending ids. Stops early once every id is seen;
    ids that no longer exist on TorBox are simply absent from the result."""
    found = await _scan(
        api_key,
        files,
        lambda t: t.id in ids,
        lambda f: len({t.id for t in f}) == len(ids),
    )
    return {t.id: t for t in found}


async def delete_torrent(api_key: str, torrent_id: int) -> None:
    """Remove a torrent from TorBox. Used for dead downloads only: they have no
    link to regenerate and keep holding one of the account's slots."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        await _request(
            client,
            "POST",
            f"{TORRENTS}/controltorrent",
            api_key,
            json={"torrent_id": torrent_id, "operation": "delete"},
        )


async def request_link(
    api_key: str, torrent_id: int, file_id: int | None = None, zip_link: bool = False
) -> str:
    """Ask for a presigned download URL. ``requestdl`` needs the API key in the
    query as well as the header; zip links are for multi-file torrents."""
    params: dict[str, object] = {"token": api_key, "torrent_id": torrent_id}
    if zip_link:
        params["zip_link"] = "true"
    if file_id is not None:
        params["file_id"] = file_id
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        data = await _request(client, "GET", f"{TORRENTS}/requestdl", api_key, params=params)
    if isinstance(data, str) and data.startswith("http"):
        return data
    if isinstance(data, dict) and isinstance(data.get("url"), str):
        return data["url"]
    raise TorboxError("TorBox didn't return a download link")
