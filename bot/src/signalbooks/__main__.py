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
    """Answer allowlisted chats through the same modules the CLI uses, and deliver
    finished jobs on the poll loop while the socket is up."""
    if not cfg.signal_account or not cfg.torbox_api_key:
        print(
            "error: SIGNAL_ACCOUNT and TORBOX_API_KEY are required for --serve",
            file=sys.stderr,
        )
        return 1
    if not cfg.allowed_numbers and not cfg.signal_group_id:
        print(
            "error: --serve needs SIGNAL_ALLOWED_NUMBERS or SIGNAL_GROUP_ID, else it answers nobody",
            file=sys.stderr,
        )
        return 1
    state = jobs.State()
    while True:
        client = signal.Client(cfg.signal_socket, cfg.signal_account)
        try:
            await client.connect()
            print(f"connected to signal-cli at {cfg.signal_socket}", file=sys.stderr)
            # A new add nudges the poll loop instead of waiting out the interval,
            # so an already-cached audiobook is delivered right after "Added".
            wake = asyncio.Event()
            tasks = {
                asyncio.create_task(_receive_loop(client, cfg, state, wake)),
                asyncio.create_task(_poll_loop(client, cfg, state, wake)),
            }
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            for task in done:
                task.result()  # a SignalError tears the connection down for a retry
        except signal.SignalError as exc:
            print(str(exc), file=sys.stderr)
        finally:
            await client.close()
        await asyncio.sleep(RETRY_SECONDS)


async def _receive_loop(
    client: signal.Client, cfg: config.Config, state: jobs.State, wake: asyncio.Event
) -> None:
    while (message := await client.next_message()) is not None:
        if not messages.permitted(
            message.sender, message.group, cfg.allowed_numbers, cfg.signal_group_id
        ):
            print(f"ignoring a message from {message.sender}", file=sys.stderr)
            continue
        command = messages.parse(message.text)
        if command.kind == "ignore":
            continue
        print(f"serving {command.kind} for {message.sender}", file=sys.stderr)
        try:
            queued = await _answer(client, cfg, state, message.chat, command)
        except (abb.AbbError, torbox.TorboxError, jobs.JobsError) as exc:
            await client.send(message.chat, messages.failure(exc))
            continue
        if queued:
            wake.set()


async def _answer(
    client: signal.Client,
    cfg: config.Config,
    state: jobs.State,
    chat: str,
    command: messages.Command,
) -> bool:
    """Answer one command and report whether it was an add, so the poll loop can
    check immediately (a duplicate costs at most one idle cycle)."""
    if command.kind == "help":
        await client.send(chat, messages.HELP)
    elif command.kind == "usage":
        await client.send(chat, messages.usage(command.argument))
    elif command.kind == "abb":
        book = await abb.fetch_book(command.argument, cfg.abb_cookie)
        added = await jobs.add(cfg.torbox_api_key, state, chat, book)
        await client.send(
            chat, messages.added(added.title, added.duplicate, added.torrent_id, added.sent)
        )
        return True
    elif command.kind == "relink":
        torrent_id = int(command.argument) if command.argument else state.last_sent(chat)
        url = await jobs.relink(cfg.torbox_api_key, state, chat, torrent_id)
        title = (state.get(torrent_id, chat) or {}).get("title", "") if torrent_id else ""
        await client.send(chat, messages.relink(url, title, torrent_id or 0))
    return False


async def _poll_loop(
    client: signal.Client, cfg: config.Config, state: jobs.State, wake: asyncio.Event
) -> None:
    """One mylist walk per cycle for every pending job; no HTTP at all when idle.
    The wake event makes a fresh add poll now instead of on the next interval."""
    while True:
        wake.clear()
        if state.pending():
            try:
                deliveries = await jobs.poll(cfg.torbox_api_key, state)
            except torbox.TorboxError as exc:
                print(f"poll failed: {exc}", file=sys.stderr)
                deliveries = []
            for delivery in deliveries:
                await client.send(delivery.chat, messages.delivery(delivery))
                state.record_delivery(delivery.torrent_id, delivery.chat)
                state.save()
        try:
            await asyncio.wait_for(wake.wait(), jobs.POLL_SECONDS)
        except TimeoutError:
            pass


def _show(delivery: jobs.Delivery) -> int:
    if delivery.error:
        print(f"error: {delivery.title}: {delivery.error}", file=sys.stderr)
        return 1
    print(delivery.url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
