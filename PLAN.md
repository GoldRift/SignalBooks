# SignalBooks — Plan

A Signal bot: send it `/abb` with an AudiobookBay link, it adds the torrent to TorBox,
waits for the download, and replies with a TorBox download link.

## Decisions (locked)

| Topic | Choice |
| --- | --- |
| Language | **Python 3.12** — this is HTTP + HTML parsing + a socket loop |
| Signal | **signal-cli JSON-RPC daemon** over a Unix socket. No better maintained option exists for a personal Signal account; `signal-cli-rest-api` is only a wrapper around the same thing. |
| TorBox input | Scrape the ABB page, read the info hash, `POST` a built magnet to `createtorrent`. |
| Delivery | `requestdl` link, sent back as a Signal text message. Link must be opened within ~3 h; after the download starts it is not time-limited. |
| Hosting | Docker Compose on an always-on box: `signal-cli` service + `bot` service, shared socket volume. |
| Access | Allowlist of sender numbers for 1:1 chats, plus one configured group whose members are all trusted. |
| Prereqs | TorBox paid plan + API key, Signal account to link. No ABB account: the info hash is readable anonymously (verified 2026-11-12), so `ABB_COOKIE` is optional and only set if a login gate returns. |
| Build order | **Pipeline first, bot second.** `abb → torbox → jobs` must run end-to-end from a CLI against live ABB/TorBox before any Signal code is written. The bot is then a thin adapter over the same modules — no throwaway scripts, no rework. |

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
Only messages that start with a command are answered. Plain chatter and bare links
are ignored, so a group conversation stays quiet and the bot never fetches a page
it was not asked for.

| Command | Behavior |
| --- | --- |
| `/help` | Short guide: what the bot does, the commands, and the link-expiry rule. |
| `/abb <link>` | Add the torrent for that book page, wait, reply with the download link. |
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
      __main__.py         # CLI (--abb/--link/--dry-run) and the Signal serve loop
      config.py           # env parsing + validation
      signal.py           # JSON-RPC client: receive notifications, send
      abb.py              # URL validation, page fetch, scrape → Book
      torbox.py           # createtorrent / mylist / requestdl
      jobs.py             # pending queue, 30 s poll loop, JSON state file
      messages.py         # reply templates
    tests/
```

The CLI is the build/test harness, not a debug afterthought — Phase 4 is proven here,
not in a chat window. The bot then calls the same functions:

```sh
cd bot
python -m signalbooks --abb <url> --dry-run   # scrape only: title, author, info hash
python -m signalbooks --abb <url> --wait      # add, poll, print the download link
python -m signalbooks --link <torrent_id>     # re-request a link for a stored torrent
```

- `signal.py`: connect to the Unix socket with `asyncio.open_unix_connection`,
  newline-delimited JSON. Incoming messages arrive as `receive` notifications.
  Ignore `syncMessage`, and ignore `dataMessage` where `sourceNumber == account`
  (your own messages from the phone come back as sync traffic).
- `abb.py`: accept only hosts in a configured ABB-domain allowlist (blocks SSRF);
  the single supported mirror is `https://audiobookbay.lu/`. Accepted link forms:
  `/abss/<slug>/` (current, and what `/audio-books/<slug>/` is rewritten to),
  `/audio-books/<slug>/`. The old `?p=<id>` form is dead on `.lu` (empty 301), so
  it is rejected at parse time rather than failing at fetch. Fetch the detail page
  with a browser UA; send `ABB_COOKIE` when set but do not require it — the hash is
  served anonymously. Redirects are not followed, so the cookie can never leave the
  allowlist.
  Extract title, author, info hash. Build `magnet:?xt=urn:btih:<hash>&dn=<title>`.
  Selectors are the fragile part — cover them with a saved-HTML fixture test.
- `torbox.py`: base `https://api.torbox.app/v1/api`, `Authorization: Bearer $TORBOX_API_KEY`.
  All three calls live under `/torrents/`: `POST /torrents/createtorrent` is
  form-encoded (`magnet=...`); `GET /torrents/mylist` and
  `GET /torrents/requestdl` are GETs (dropping the `torrents` segment returns 404
  `Not Found`).
  `requestdl` also needs the API key as the `token` query param.
  Shapes confirmed from the published collection (see Phase 0): `createtorrent`
  returns `{hash, torrent_id}`; `mylist?files=true` items carry `download_finished`,
  `download_state`, `cached`, `size`, `files[{id,size,name,zipped}]`; `requestdl`
  takes `torrent_id`, `file_id`, `zip_link`.
- `jobs.py`: **one** `mylist` call per poll for all pending jobs, not one per job.
  Idle → no polling at all. `mylist` data is cached up to 600 s, so polls pass
  `bypass_cache=true` (and `files=true` when the file list is needed). `mylist`
  pages at 100 items, so walk pages until every pending id is seen. Persist
  `{"<chat>:<torrent_id>": {chat, title, url, added_at, sent, misses}}` to
  `state.json` with atomic write (tmp + rename) so a restart doesn't lose replies.
  Jobs are keyed per chat so the same torrent requested in two chats still gets two
  replies. A reply is recorded only after it is sent: a crash repeats a reply rather
  than losing it. Track delivery order per chat so `/relink` can find the most recent
  torrent.
- Dedup: ignore the same URL from the same chat within 10 minutes.
  Reuse TorBox's existing torrent id instead of re-adding (docs don't say
  `createtorrent` reports a duplicate, so look the hash up in `mylist` first).
- Multi-file audiobooks: if the torrent has several files, request with
  `zip_link=true` and send the zip URL; if it's one file, link it directly.
  Confirmed: `zip_link=true` returns a `/zip/...` URL for a 7-file torrent.
- Delivery does not delete the torrent: it stays on TorBox so a link can be
  re-requested after the ~3 h window. TorBox's slot limit decides throughput.
- Excess adds are queued by TorBox itself (`queued_id`) and started later by its
  scheduler; the bot keeps no queue of its own. A queued response is reported in one
  line, never silently dropped.
- Replies are minimal but specific. An add is `Added: <title>` plus one line saying
  the link is coming; a delivery is the title, the URL, a file/zip+size line, and one
  start-by/`/relink` hint; `/relink` is titled and repeats the start-by window; errors
  are one line with a recovery step.
  An uncached book gets one live progress message, edited at each quarter of the
  download and finally replaced by the delivery text; a cached book never sees one.
  Errors surfaced to the user:
  bad/unsupported link, page fetch failed, TorBox rejected it, torrent errored/stalled,
  link request failed.

## Phases

Phases run in number order except Phase 1: Signal plumbing comes **after** Phase 4.
Everything before it is validated from the CLI, so a signal-cli surprise only touches
`signal.py` and never blocks the pipeline.

### 0. Preflight

- [x] ABB base is `https://audiobookbay.lu/`. Live check 2026-11-12: detail pages are
      `/abss/<slug>/`, the info hash sits in a plain `<td>` and is visible without
      login; `/audio-books/<slug>/` and `?p=<id>` answer with an empty 301. So no
      ABB account is needed and `ABB_COOKIE` is optional — keep it supported in case
      a login gate or Cloudflare challenge returns.
- [x] Keep completed torrents on TorBox so links can be re-requested; TorBox's slots
      decide throughput, with excess requests queued server-side.
- [x] TorBox endpoint shapes read off the published API collection: `createtorrent`
      (form-encoded) → `{hash, torrent_id}`; `mylist?files=true` → `download_finished`,
      `download_state`, `cached`, `files[{id,size,zipped}]`; `requestdl?token=`
      → a single CDN URL, 3 h to start the download.
- [x] TorBox plan confirmed: **Essential (plan 1)**, 3 concurrent slots, 200 GB max
      per download, 0 extra slots. Limits that matter: 300 req/min, 60 uncached
      torrents/hour, and `mylist` pages at 100 items (dedup scans must paginate).
- [x] `createtorrent` against an already-cached torrent returns
      `"Found Cached Torrent. Using Cached Torrent."` with a fresh `torrent_id`, so
      cached adds can go straight to delivery without waiting on the poll loop.
- [x] Pick the private group id: **Audiobooks** (2 members), set as `SIGNAL_GROUP_ID`
      2026-11-14; group replies work live and every member is trusted.
- [x] All three TorBox calls verified against the live account: `createtorrent`
      (cached add returns a `torrent_id` immediately), `mylist?bypass_cache=true`
      (cached item: `download_state: cached`, `progress: 1`, `files: 7`), and
      `requestdl` with `zip_link=true` on a 7-file torrent → a `/zip/...` CDN URL.
      So the zip branch works well below the 100-file auto-zip threshold.
- [x] Slot-full adds, answered from the SDK response model 2026-11-12 (not reproducible
      on this account: cached audiobooks never occupy a slot): `createtorrent` returns
      `queued_id` plus `active_limit`/`current_active_downloads` instead of `torrent_id`,
      and TorBox's background process starts queued downloads later (up to a few hours)
      without an `as_queued` call. So `jobs.py` keeps no queue of its own; a queued add
      is a one-line error and writes no state.

### 1. Skeleton + Signal plumbing (after Phase 4)

- [x] `docker-compose.yml`: `signal-cli` service (pinned version, data volume) and `bot`
      service (shared `/run/signal-cli` socket volume, `restart: unless-stopped`).
- [x] Link the device: `docker compose run --rm signal-cli link -n SignalBooks`, scan QR
      from Signal → Settings → Linked devices. (Linking needs no spare number; it becomes
      a linked device on your existing account. A separate number would have to be registered.)
- [x] Echo bot: log incoming messages, reply "pong". Proved in a 1:1 chat 2026-11-14;
      routing end-to-end followed in Phase 4.
- [x] `.env.example`, `.gitignore`, README with the link procedure.

### 2. ABB → magnet

- [x] URL parser for `/abss/<slug>/` and `/audio-books/<slug>/` (rewritten to
      `/abss/`); rejects everything else, including the dead `?p=<id>` form.
- [x] Scraper + fixture test from a saved page (`bot/tests/fixtures/abb_detail.html`,
      a real logged-out detail page).
- [x] Magnet builder test (hash casing, URL-encoded title).

### 3. TorBox + job loop

- [x] `createtorrent`, handle cached/immediate completion.
- [x] Poll loop, state file, restart resume.
- [x] Wire `--abb <url> [--dry-run|--wait]` and the manual `--link <torrent_id>` CLI
      escape hatch for stuck jobs. These are the only surfaces used to test Phases 3–4.
- [x] Live check settled in Phase 0: TorBox queues an add itself when all slots are
      busy (`queued_id` in the response), so `jobs.py` keeps no queue and reports a
      queued response as one line instead of failing obscurely.

### 4. Delivery + error paths

- [x] `requestdl` with single-file vs zip branch, covered by respx tests
      (`jobs.link_for`).
- [x] Live check 2026-11-12: both a single-file link and a multi-file zip link
      download.
- [x] `/help`, `/abb`, and `/relink` parsing in `messages.py`; bare `/relink` uses
      the chat's last delivery, `/relink <torrent_id>` picks a specific one
      (`jobs.relink`), and both commands are routed from chat in `__main__._serve`.
- [x] Friendly failure messages: expected failures are one-line exceptions
      (`AbbError`/`TorboxError`/`JobsError`), caught at the edge; no stack traces.
- [x] End-to-end from the CLI with a real audiobook under ~50 MB, then a multi-file one.
      Keep the first live ABB page as `tests/fixtures/abb_detail.html`.
- [x] Phase 1 is done (device linked, echo bot answers in a 1:1 chat, 2026-11-14).
- [x] Route `/help`, `/abb`, and `/relink` from chat through the Phase 1 loop
      (`__main__._serve`): commands share `messages`/`jobs` with the CLI, one poll
      cycle serves every pending job, and each reply is recorded only after it is sent.
      Covered by tests that drive the real serve loop against a stand-in daemon, and
      live on 2026-11-14 (a real ABB link was added from chat and its link delivered).

### 5. Hardening

- [x] Allowlist and group enforcement tests (foreign sender, wrong group, self-sync).
- [x] Command tests: unknown command, `/relink` with nothing stored, malformed `/abb`.
- [x] Revisit only if uncached bursts show up: not triggered — audiobooks are cached,
      so a slot is never occupied and `queued_id` never appears. No queue code, as
      designed; `torrents/getqueued` stays the upgrade path if it ever shows up.
- [x] Log redaction: never log message bodies, API keys, or presigned URLs. Only the
      sender and command kind are logged; covered by `test_serve_logs_no_message_body_link_or_key`.
- [x] Pin `signal-cli`; document the quarterly update (Signal breaks old clients):
      digest-pinned in `docker-compose.yml`, update steps in README → Maintenance.

### 6. Deploy + runbook

- [x] Compose up on the server, volume backup note for signal-cli keys (README →
      Maintenance).
- [x] Runbook: re-link device, rotate API key, regenerate a link, inspect state.json
      (README → Maintenance).

### 7. Chat interface polish

- [x] Rewrite the command copy: a short help guide, actionable usage and failure
      text, a titled `/relink`, and a delivery that names the book, the file/zip shape,
      the size, when the download must be started, and the torrent id for `/relink`.
- [x] Nudge the poll loop after an add so a cached book does not wait out the 30 s
      interval before it can be delivered.
- [x] Cover every chat state with exact-string tests; no new dependencies and no
      `state.json` schema change.
- [x] Confirm the copy in live Signal (wrapping, `·`/`—`, no Markdown assumptions),
      then close this phase. Verified 2026-11-15.

### 8. Progress message for an uncached book

- [x] A cached book is untouched: `Added: <title>`, then the delivery, no bar.
- [x] A fresh download gets one live message: the book, a ten-cell plain-text bar,
      the percent, and `downloaded of total`. It is edited at each quarter, and its
      last edit is the delivery text, so the bar becomes the link. Signal allows ten
      edits per message in 24 h; this flow uses four.
- [x] The live message's timestamp and the percent last shown are stored
      (`progress_ts`, `progress_pct`), so a restart keeps editing the same bubble.
      A failed send or edit is logged and retried on the next poll, never fatal; a
      failed finishing edit falls back to a normal delivery message.
- [x] The bar and its quarter edits are `--no-urgent`; the last edit keeps the
      default urgency, but Signal clients do not push for an edit, so the bubble the
      user is already watching is the only cue.
- [x] Live check 2026-11-15: an uncached book showed the bar, its quarter edits, and
      then the link in the same bubble; the final edit did not ring the phone.

### 9. Retire a dead download

- [x] A torrent TorBox reports as `stalled` or `error` is deleted from the account
      (`POST /torrents/controltorrent`, `operation: delete`) before the reply, so it
      stops holding a slot. A delivered torrent stays, so `/relink` keeps working.
- [x] The reply names the reason and invites a retry: "The download stalled (no seeds,
      usually). Removed it from TorBox so it doesn't hold a slot. Send /abb with the
      book link again in 10 minutes."
- [x] If TorBox refuses the delete, the reply says the torrent may still hold a slot
      and the poll carries on.
- [ ] Live check: a no-seed torrent disappears from the TorBox dashboard and the chat
      says why. Needs a deliberately dead torrent.

## Not doing (unless asked)

- Web UI, multi-user accounts, per-user TorBox keys.
- Downloading or seeding anything locally.
- Uploading to third-party file hosters.
- Accepting raw magnets or `.torrent` files (ABB links only).
- Auto-deleting a *delivered* torrent from TorBox — it would free slots but kills link
  regeneration. Only a dead download (stalled or errored) is removed; it has no link
  worth keeping.
- A bar that updates more often than each quarter: Signal's ten-edit limit in 24 h
  rules out a live one, and deleting the bar leaves a tombstone in the chat.

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
- **A delivered link arrives as a silent edit** of the progress bubble: Signal clients
  do not push for an edit (confirmed 2026-11-15), so a user who has left the chat is
  not pinged. The link is in the bubble they were watching, and `/relink` recovers.
- **TorBox slot limits** mean a burst of links queues rather than fails; keep the
  server-side queue explicit and drain it as slots free.
