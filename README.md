# SignalBooks

Send a Signal message with an AudiobookBay (ABB) link, get back a TorBox download
link. No torrent data passes through this box and nothing is re-hosted.

`PLAN.md` is the spec; this README is the quick start.

## Status

Phase 2 of `PLAN.md` is done: ABB link parsing, page scraping, and magnet building.
Not built yet: TorBox calls, the job/poll loop, the CLI, and the Signal bot.

## How it works

```
Signal msg → signal-cli daemon → bot
  allowlist + group check → extract ABB link → scrape page → info hash → magnet
  → TorBox createtorrent → poll mylist → requestdl → reply with the download URL
```

## Layout

```
bot/
  pyproject.toml        # httpx + selectolax, pytest + respx (dev)
  src/signalbooks/
    abb.py              # link validation (SSRF guard), scraping, magnet building
  tests/
    test_abb.py
    fixtures/abb_detail.html   # saved real detail page
```

## Development

Python 3.12, pinned in `bot/.python-version`.

```sh
cd bot
uv sync
uv run pytest                              # full suite
uv run pytest tests/test_abb.py -q         # one file
uv run pytest tests/test_abb.py -k magnet  # one test
```

Tests never hit live ABB, TorBox, or Signal: scraping is covered by saved HTML
fixtures and HTTP is mocked with `respx`.

## Configuration

All config comes from env vars (see `PLAN.md`). Only one exists so far:

| Variable | Required | Meaning |
| --- | --- | --- |
| `ABB_COOKIE` | no | Session cookie for ABB, sent only when set. The info hash is served anonymously, so this is only for the day a login gate or Cloudflare challenge returns. |

`TORBOX_API_KEY` and the allowlist/group settings arrive with Phases 3–5.

## ABB notes

- Only `https://audiobookbay.lu/` is supported. Accepted link forms:
  `/abss/<slug>/` and `/audio-books/<slug>/` (rewritten to `/abss/`).
  The old `?p=<id>` form is dead on `.lu` and is rejected.
- The info hash is in the `Info Hash` table row and needs no login.
- Selectors live only in `bot/src/signalbooks/abb.py`. When ABB's DOM changes, the
  fix is that one file plus a fresh `tests/fixtures/abb_detail.html` saved from the
  live page.
- Redirects are not followed, so a redirect can never carry the cookie off-host.

## Not doing

Web UI, per-user accounts, local downloads or seeding, file hosters, raw magnets or
`.torrent` uploads, auto-deleting torrents from TorBox, progress updates in chat.
See `PLAN.md`.
