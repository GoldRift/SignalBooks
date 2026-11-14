# SignalBooks

Send a Signal message with an AudiobookBay (ABB) link, get back a TorBox download
link. No torrent data passes through this box and nothing is re-hosted.

`PLAN.md` is the spec; this README is the quick start.

## Status

Phase 1 is proven: the device is linked, `signal-cli` and the bot run under compose,
and an allowlisted number gets a reply in a 1:1 chat. The bot is still an echo bot
(it replies "pong"); routing `/help`, `/abb`, and `/relink` comes next. On the
pipeline side, the single-file and zip links are confirmed working against live
TorBox, and command parsing plus chat-scoped relink are implemented and unit-tested.

## How it works

```
Signal msg → signal-cli daemon → bot
  allowlist + group check → extract ABB link → scrape page → info hash → magnet
  → TorBox createtorrent → poll mylist → requestdl → reply with the download URL
```

The pipeline below the bot runs today from the CLI; the bot is a thin adapter
over the same modules.

## Layout

```
docker-compose.yml      # signal-cli + bot on a shared Unix socket
.env.example            # copy to .env, never commit .env
bot/
  Dockerfile
  pyproject.toml        # httpx + selectolax, pytest + respx (dev)
  src/signalbooks/
    __main__.py         # CLI: scrape, add + wait, relink
    config.py           # env parsing
    abb.py              # link validation (SSRF guard), scraping, magnet building
    torbox.py           # createtorrent / mylist / requestdl
    jobs.py             # pending queue, poll loop, state file, relink
    messages.py         # command parsing, reply templates, access policy
    signal.py           # signal-cli JSON-RPC client over the Unix socket
  tests/
    test_abb.py
    test_torbox.py
    test_jobs.py
    test_cli.py
    test_config.py
    test_messages.py
    test_signal.py
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

## CLI

The CLI is the pipeline's test harness: the same modules the bot will call.

```sh
cd bot
export TORBOX_API_KEY=...                  # required for everything except --dry-run
uv run python -m signalbooks --abb <url> --dry-run   # scrape only
uv run python -m signalbooks --abb <url> --wait      # add, poll, print the link
uv run python -m signalbooks --link <torrent_id>     # re-request a link
```

`--wait` polls `mylist` every 30 s and exits when the link is printed. A cached
torrent is delivered on the first poll. Completed jobs are written to
`state.json` in the working directory (atomic write, never commit it); the
torrent itself stays on TorBox so `--link` can produce a fresh URL after the
~3 h presigned window.

## Deploy

Preconditions: a TorBox paid plan and API key, a Signal account to link (linking
adds a device to an existing account — no spare number needed), and Docker with
Compose.

1. Create the config file and fill it in (the compose project reads it):

   ```sh
   cp .env.example .env
   ```

2. Link the device once, then scan the QR from Signal → Settings → Linked devices:

   ```sh
   docker compose run --rm signal-cli link -n SignalBooks
   ```

3. Start it:

   ```sh
   docker compose up -d --build
   ```

`signal-cli` and `bot` talk over a Unix socket on a shared named volume; no port is
published. The linked-device keys and `state.json` live in named volumes
(`signal-cli-data`, `bot-state`), so nothing lands on the host and nothing is
committed.

The Signal bot answers in a 1:1 chat today, but only with "pong": command routing is
the next step. The pipeline from the CLI (see above) is what actually adds and
delivers audiobooks for now.

## Configuration

All config comes from env vars (see `PLAN.md`).

| Variable | Required | Meaning |
| --- | --- | --- |
| `SIGNAL_ACCOUNT` | for `--serve` | The Signal number the linked device belongs to. |
| `SIGNAL_ALLOWED_NUMBERS` | for `--serve` | Comma-separated numbers allowed to use the bot. Everything else is ignored. |
| `SIGNAL_SOCKET` | no | signal-cli's Unix socket; defaults to `/run/signal-cli/socket`, which compose sets. |
| `TORBOX_API_KEY` | for `--abb --wait` and `--link` | TorBox API key, sent as a bearer token (`requestdl` also takes it as the `token` query param). |
| `ABB_COOKIE` | no | Session cookie for ABB, sent only when set. The info hash is served anonymously, so this is only for the day a login gate or Cloudflare challenge returns. |

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
