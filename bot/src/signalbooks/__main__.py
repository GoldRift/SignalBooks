"""CLI: the pipeline's build/test harness. Phases 3–4 are proven here before
any Signal code exists."""

import argparse
import asyncio
import sys

from . import abb, config, jobs, torbox


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="signalbooks", description="AudiobookBay link in, TorBox download link out."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--abb", metavar="URL", help="AudiobookBay detail-page link")
    source.add_argument(
        "--link", metavar="TORRENT_ID", type=int, help="re-request a link for a stored torrent"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="scrape only: title, author, info hash")
    mode.add_argument("--wait", action="store_true", help="add, poll, print the download link")
    args = parser.parse_args(argv)
    if args.abb and not (args.dry_run or args.wait):
        parser.error("--abb needs --dry-run or --wait")
    if args.link and (args.dry_run or args.wait):
        parser.error("--link takes neither --dry-run nor --wait")
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130
    except (abb.AbbError, torbox.TorboxError, jobs.JobsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


async def _run(args: argparse.Namespace) -> int:
    cfg = config.load()
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
    items = await torbox.find_many(api_key, {torrent_id}, files=True)
    item = items.get(torrent_id)
    if item is None:
        print(f"error: torrent {torrent_id} is not on TorBox", file=sys.stderr)
        return 1
    if not item.done:
        print(
            f"error: torrent {torrent_id} is not ready yet ({item.state or 'unknown'})",
            file=sys.stderr,
        )
        return 1
    print(await jobs.link_for(api_key, item))
    return 0


def _show(delivery: jobs.Delivery) -> int:
    if delivery.error:
        print(f"error: {delivery.title}: {delivery.error}", file=sys.stderr)
        return 1
    print(delivery.url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
