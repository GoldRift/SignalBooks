# SignalBooks

Send the bot `/abb` with an AudiobookBay (ABB) link, get back a TorBox download link.
No torrent data passes through this box and nothing is re-hosted.

`PLAN.md` is the spec; this README is the quick start.

## Status

Live: 1:1 chats from allowlisted numbers, plus the configured private group, where
every member is trusted. `/help`, `/abb`, and `/relink` are answered from chat, and a
real ABB link was added and delivered. Replies name the book, the link, the file/zip
shape, and the expiry; an add triggers a poll immediately, so a cached book does not
wait for the 30 s interval. Single-file and zip links are confirmed against live
TorBox. The same pipeline runs from the CLI.

## How it works

```
Signal msg → signal-cli daemon → bot
  allowlist + group check → extract ABB link → scrape page → info hash → magnet
  → TorBox createtorrent → poll mylist → requestdl → reply with the download URL
```

The pipeline below the bot runs today from the CLI; the bot is a thin adapter
over the same modules.

## Commands

1:1 chats from allowlisted numbers, plus one optional group whose members are all
trusted. Only messages that start with a command are answered; anything else is
ignored.

| Message | What happens |
| --- | --- |
| `/help` | A short guide with the commands and the link-expiry rule. |
| `/abb <link>` | Adds the torrent and replies `Added: <title>` with a note that the link is coming. The download message follows when it is ready. |
| `/relink` | A fresh titled link for the last book delivered in this chat. |
| `/relink <torrent_id>` | A fresh link for that specific torrent; never re-adds or consumes a TorBox slot. |

An add triggers a poll immediately, so a book TorBox already has cached is usually
delivered right after "Added"; a fresh download arrives when a later poll sees it
finish. A fresh download also gets one live progress message — a bar that moves at
each quarter — and its last edit is the delivery, so the bar becomes the link. After
"Added: Test Book - Ada Example", the delivery looks like this:

```text
Test Book - Ada Example
https://… (the presigned TorBox link)
1 file · 438 MB
Start the download within about 3 hours · /relink 42 for a new one
```

Bad links, unknown commands, and TorBox problems get a plain one-line explanation,
never a stack trace.

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

After a code change, rebuild **and** recreate — podman-compose in particular
rebuilds the image without replacing the running container, which keeps old code
answering:

   ```sh
   docker compose up -d --build --force-recreate bot
   ```

`signal-cli` and `bot` talk over a Unix socket on a shared named volume; no port is
published. The linked-device keys and `state.json` live in named volumes
(`signal-cli-data`, `bot-state`), so nothing lands on the host and nothing is
committed.

Send `/help`, `/relink`, or `/abb <link>` in a 1:1 chat to use it. A group works
too once `SIGNAL_GROUP_ID` names it; replies land back in that group.

## Maintenance

The linked-device keys are the one thing you cannot recreate: they live in the
`signal-cli-data` volume. The bot's `bot-state` volume holds `state.json` (pending
jobs and delivered replies).

Back up both before upgrades or when moving the box:

```sh
docker volume ls | grep -E 'signal-cli-data|bot-state'   # exact names first
docker run --rm -v signalbooks_signal-cli-data:/data:ro -v "$PWD":/backup alpine \
  tar -C /data -czf /backup/signal-cli-data.tgz .
docker run --rm -v signalbooks_bot-state:/data:ro -v "$PWD":/backup alpine \
  tar -C /data -czf /backup/bot-state.tgz .
```

Restore into empty volumes with the same commands using `-xzf` and no `:ro`, after
`docker compose down`. Keep the backups private: the keys are the account.

**Re-link the device** (new box, lost keys, or a `signal-cli` that no longer starts):
stop the stack, link, then start it; remove the stale `SignalBooks` entry from
Signal → Linked devices afterwards.

```sh
docker compose down
docker compose run --rm signal-cli link -n SignalBooks
docker compose up -d
```

**Rotate the TorBox API key**: edit `TORBOX_API_KEY` in `.env`, then recreate the bot
only — no rebuild. Pending jobs keep their place.

```sh
docker compose up -d --force-recreate bot
```

**Regenerate a download link** without touching the queue: `/relink` in chat (last
book delivered there), `/relink <torrent_id>`, or from the host:

```sh
docker compose exec bot python -m signalbooks --link <torrent_id>
```

The torrent stays on TorBox, so this works after the ~3 h presigned window. It only
calls `requestdl`; it never re-adds anything.

**Inspect `state.json`** (titles, chats, and short-lived presigned URLs — do not
paste it publicly, and do not hand-edit while the bot runs):

```sh
docker compose exec bot cat /state/state.json
```

**Update `signal-cli` quarterly** — Signal drops old clients. The version is pinned
by digest in `docker-compose.yml`; bump the digest deliberately, keep the version
comment current, then rebuild and watch for the JSON-RPC server line in the logs:

```sh
docker compose up -d --build --force-recreate
docker compose logs --tail 20 signal-cli
```

## Configuration

All config comes from env vars (see `PLAN.md`).

| Variable | Required | Meaning |
| --- | --- | --- |
| `SIGNAL_ACCOUNT` | for `--serve` | The Signal number the linked device belongs to. |
| `SIGNAL_ALLOWED_NUMBERS` | for `--serve`, unless `SIGNAL_GROUP_ID` is set | Comma-separated numbers allowed to use the bot in 1:1 chats. Everything else is ignored. |
| `SIGNAL_GROUP_ID` | no | One Signal group's base64 id (`signal-cli listGroups`). Every member of that group may use the bot; no other group is answered. |
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
