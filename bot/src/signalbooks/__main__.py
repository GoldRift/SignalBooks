"""CLI: the pipeline's build/test harness, plus the Signal serve mode."""

import argparse
import asyncio
import sys

from . import abb, config, jobs, messages, signal, torbox

RETRY_SECONDS = 5.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="signalbooks", description="AudiobookBay link in, TorBox download link out."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--abb", metavar="URL", help="AudiobookBay detail-page link")
    source.add_argument(
        "--link", metavar="TORRENT_ID", type=int, help="re-request a link for a stored torrent"
    )
    source.add_argument("--serve", action="store_true", help="run the Signal bot")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="scrape only: title, author, info hash")
    mode.add_argument("--wait", action="store_true", help="add, poll, print the download link")
    args = parser.parse_args(argv)
    if args.abb and not (args.dry_run or args.wait):
        parser.error("--abb needs --dry-run or --wait")
    if (args.link or args.serve) and (args.dry_run or args.wait):
        parser.error("--link and --serve take neither --dry-run nor --wait")
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130
    except (abb.AbbError, torbox.TorboxError, jobs.JobsError, signal.SignalError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


async def _run(args: argparse.Namespace) -> int:
    cfg = config.load()
    if args.serve:
        return await _serve(cfg)
    if args.dry_run:
        book = await abb.fetch_book(args.abb, cfg.abb_cookie)
        print(f"title:     {book.title}")
        print(f"author:    {book.author or '-'}")
        print(f"info hash: {book.info_hash}")
        return 0
    if not cfg.torbox_api_key:
        print("error: TORBOX_API_KEY is not set", file=sys.stderr)
        return 1
    if args.link:
        return await _relink(cfg.torbox_api_key, args.link)

    state = jobs.State()
    book = await abb.fetch_book(args.abb, cfg.abb_cookie)
    added = await jobs.add(cfg.torbox_api_key, state, "cli", book)
    if added.duplicate:
        print(f"already added: {added.title}")
        job = state.get(added.torrent_id, "cli")
        if not args.wait or not job or job.get("sent"):
            return 0
    else:
        print(f"added: {added.title} (torrent {added.torrent_id})")
    while True:
        for delivery in await jobs.poll(cfg.torbox_api_key, state):
            code = _show(delivery)
            state.record_delivery(delivery.torrent_id, delivery.chat)
            state.save()
            if delivery.torrent_id == added.torrent_id:
                return code
        await asyncio.sleep(jobs.POLL_SECONDS)


async def _relink(api_key: str, torrent_id: int) -> int:
    """Manual escape hatch: request a fresh URL without touching the job state,
    so a still-pending job keeps its turn in the poll loop."""
    print(await jobs.fresh_link(api_key, torrent_id))
    return 0


async def _serve(cfg: config.Config) -> int:
    """Phase 1 echo bot: prove receive + send before any routing exists."""
    if not cfg.signal_account or not cfg.allowed_numbers:
        print(
            "error: SIGNAL_ACCOUNT and SIGNAL_ALLOWED_NUMBERS are required for --serve",
            file=sys.stderr,
        )
        return 1
    while True:
        client = signal.Client(cfg.signal_socket, cfg.signal_account)
        try:
            await client.connect()
            print(f"connected to signal-cli at {cfg.signal_socket}", file=sys.stderr)
            while (message := await client.next_message()) is not None:
                if not messages.permitted(message.sender, message.group, cfg.allowed_numbers):
                    print(f"ignoring a message from {message.sender}", file=sys.stderr)
                    continue
                print(f"message from {message.sender} ({len(message.text)} chars)", file=sys.stderr)
                try:
                    await client.send(message.sender, "pong")
                except signal.SignalError as exc:
                    print(str(exc), file=sys.stderr)
        except signal.SignalError as exc:
            print(str(exc), file=sys.stderr)
        finally:
            await client.close()
        await asyncio.sleep(RETRY_SECONDS)


def _show(delivery: jobs.Delivery) -> int:
    if delivery.error:
        print(f"error: {delivery.title}: {delivery.error}", file=sys.stderr)
        return 1
    print(delivery.url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
