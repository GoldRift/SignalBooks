# SignalBooks — Plan

A Signal bot: send it an AudiobookBay link, it adds the torrent to TorBox, waits for the
download, and replies with a TorBox download link.

## Decisions (locked)

| Topic | Choice |
| --- | --- |
| Language | **Python 3.12** — this is HTTP + HTML parsing + a socket loop |
| Signal | **signal-cli JSON-RPC daemon** over a Unix socket. No better maintained option exists for a personal Signal account; `signal-cli-rest-api` is only a wrapper around the same thing. |
| TorBox input | Scrape the ABB page, read the info hash, `POST` a built magnet to `createtorrent`. |
| Delivery | `requestdl` link, sent back as a Signal text message. Link must be opened within ~3 h; after the download starts it is not time-limited. |
| Hosting | Docker Compose on an always-on box: `signal-cli` service + `bot` service, shared socket volume. |
| Access | Allowlist of sender numbers. Private chat first; one configured private group only after the private flow is tested. |
| Prereqs | TorBox paid plan + API key, Signal account to link, ABB account (login required to read the magnet hash; session cookie in `ABB_COOKIE`). |

## Flow

```
Signal msg ──► signal-cli daemon ──(JSON-RPC notification)──► bot
                                                               │
       allowlist + group check ◄───────────────────────────────┤
       extract ABB URL                                         │
       scrape page → info hash → magnet                        │
       POST createtorrent ──────────────────────────────► TorBox
       reply "added: <title>"                                  │
       poll mylist every 30 s while jobs pending ◄─────────────┤
       completed/cached → GET requestdl ─────────────────► TorBox
       reply with download URL                                 │
```

No torrent data passes through our box. No file re-hosting.

## Commands

Messages may start with a command. Allowlist and group checks apply to commands too.
Bare ABB links stay accepted as shorthand for `/abb`.

| Command | Behavior |
| --- | --- |
| `/help` | One-line list of supported commands. |
| `/abb <link>` | Same as sending an ABB link: add the torrent, wait, reply with the download link. |
| `/relink` | Re-request a fresh `requestdl` link for the last torrent delivered in this chat. |
| `/relink <torrent_id>` | Re-request a link for a specific stored torrent. |

`/relink` only calls `requestdl`; it never re-adds or consumes a TorBox slot. It leans
on torrents being kept on TorBox. Unknown or malformed commands get a one-line usage
reply, not a stack trace.

## Components

```
SignalBooks/
  PLAN.md
  docker-compose.yml
  .env.example            # all config, no secrets committed
  bot/
    Dockerfile
    pyproject.toml        # httpx, selectolax (or bs4), pytest+respx (dev)
    src/signalbooks/
      __main__.py         # asyncio entrypoint, command routing, wiring
      config.py           # env parsing + validation
      signal.py           # JSON-RPC client: receive notifications, send
      abb.py              # URL validation, page fetch, scrape → Book
      torbox.py           # createtorrent / mylist / requestdl
      jobs.py             # pending queue, 30 s poll loop, JSON state file
      messages.py         # reply templates
    tests/
```

- `signal.py`: connect to the Unix socket with `asyncio.open_unix_connection`,
  newline-delimited JSON. Incoming messages arrive as `receive` notifications.
  Ignore `syncMessage`, and ignore `dataMessage` where `sourceNumber == account`
  (your own messages from the phone come back as sync traffic).
- `abb.py`: accept only hosts in a configured ABB-domain allowlist (blocks SSRF);
  the single supported mirror is `https://audiobookbay.lu/`. Fetch the detail page
  with a browser UA and the logged-in session cookie from `ABB_COOKIE` — ABB requires
  login before the magnet hash is present. Extract title, author, info hash. Build
  `magnet:?xt=urn:btih:<hash>&dn=<title>`.
  Selectors are the fragile part — cover them with a saved-HTML fixture test.
- `torbox.py`: base `https://api.torbox.app/v1/api`, `Authorization: Bearer $TORBOX_API_KEY`.
  `createtorrent` is form-encoded (`magnet=...`); `mylist` and `requestdl` are GETs.
  `requestdl` also needs the API key as the `token` query param.
  Verify exact response field names against `/docs` during implementation
  (`download_finished`, `download_state`, `files[]` are the ones we need).
- `jobs.py`: **one** `mylist` call per poll for all pending jobs, not one per job.
  Idle → no polling at all. Persist `{torrent_id: {chat, title, sent}}` to
  `state.json` with atomic write (tmp + rename) so a restart doesn't lose replies.
  Track delivery order per chat so `/relink` can find the most recent torrent.
- Dedup: ignore the same URL from the same chat within 10 minutes.
  Reuse TorBox's existing torrent id if `createtorrent` reports it already exists.
- Multi-file audiobooks: if the torrent has several files, request with
  `zip_link=true` and send the zip URL; if it's one file, link it directly.
- Delivery does not delete the torrent: it stays on TorBox so a link can be
  re-requested after the ~3 h window. TorBox's slot limit decides throughput.
- Excess requests wait in an explicit server-side queue. Submit up to the available
  TorBox slots and drain the queue as jobs finish, so a burst of links never fails.
- Replies are minimal: "added", final link, or a one-line error. No progress spam.
  Errors surfaced to the user: bad/unsupported link, page fetch failed, TorBox
  rejected it, torrent errored/stalled, link request failed.

## Phases

### 0. Preflight

- [x] ABB base is `https://audiobookbay.lu/`; login is required to read the magnet
      hash, so a session cookie (`ABB_COOKIE`) is mandatory.
- [x] Keep completed torrents on TorBox so links can be re-requested; TorBox's slots
      decide throughput, with excess requests queued server-side.
- [ ] Confirm TorBox plan's active-download slots and max torrent size.
- [ ] Pick the private group id (base64) only after the 1:1 flow works.
- [ ] Sanity-check TorBox endpoints with a real key (curl): create, mylist, requestdl.

### 1. Skeleton + Signal plumbing

- [ ] `docker-compose.yml`: `signal-cli` service (pinned version, data volume) and `bot`
      service (shared `/run/signal-cli` socket volume, `restart: unless-stopped`).
- [ ] Link the device: `docker compose run --rm signal-cli link -n SignalBooks`, scan QR
      from Signal → Settings → Linked devices. (Linking needs no spare number; it becomes
      a linked device on your existing account. A separate number would have to be registered.)
- [ ] Echo bot: log incoming messages, reply "pong". Prove receive + send in a 1:1
      chat first; add the private group only after the private flow works.
- [ ] `.env.example`, `.gitignore`, README with the link procedure.

### 2. ABB → magnet

- [ ] URL parser for both `/audio-books/<slug>/` and `?p=<id>` forms; reject everything else.
- [ ] Scraper + fixture test from a saved page.
- [ ] Magnet builder test (hash casing, URL-encoded title).

### 3. TorBox + job loop

- [ ] `createtorrent`, handle cached/immediate completion.
- [ ] Poll loop, state file, restart resume.
- [ ] Manual `--link <torrent_id>` CLI escape hatch for stuck jobs (used before the bot
      has its own resend command).

### 4. Delivery + error paths

- [ ] `requestdl` with single-file vs zip branch; verify the returned URL downloads.
- [ ] `/help`, `/abb`, and `/relink` routing; bare `/relink` uses the chat's last
      delivery, `/relink <torrent_id>` picks a specific one.
- [ ] Friendly failure messages; no stack traces to the chat.
- [ ] End-to-end run with a real audiobook under ~50 MB, then a multi-file one.

### 5. Hardening

- [ ] Allowlist and group enforcement tests (foreign sender, wrong group, self-sync).
- [ ] Command tests: unknown command, `/relink` with nothing stored, malformed `/abb`.
- [ ] Server-side queue: submit only what TorBox slots allow, drain queued jobs as
      slots free; no fixed per-user cap.
- [ ] Log redaction: never log message bodies, API keys, or presigned URLs.
- [ ] Pin `signal-cli`; document the quarterly update (Signal breaks old clients).

### 6. Deploy + runbook

- [ ] Compose up on the server, volume backup note for signal-cli keys.
- [ ] Runbook: re-link device, rotate API key, regenerate a link, inspect state.json.

## Not doing (unless asked)

- Web UI, multi-user accounts, per-user TorBox keys.
- Downloading or seeding anything locally.
- Uploading to third-party file hosters.
- Accepting raw magnets or `.torrent` files (ABB links only).
- Auto-deleting torrents from TorBox — it would free slots but kills link regeneration.
- Progress percentage updates in chat.

## Known risks

- **ABB is the fragile part.** Cloudflare challenges and DOM changes are expected.
  Mitigation: cookie env var, saved-page fixture test, clear error message,
  and a one-file selector module that is easy to fix.
- **signal-cli must be kept current.** Signal's server drops old clients; a pinned
  version should still be bumped every few months. If it goes silent, suspect this first.
- **Socket permissions across containers** can bite; both services must use the same UID
  or a permissive socket directory.
- **Presigned links expire for starting a download** after ~3 h. The torrent stays on
  TorBox, so a re-request always works.
- **TorBox slot limits** mean a burst of links queues rather than fails; keep the
  server-side queue explicit and drain it as slots free.
