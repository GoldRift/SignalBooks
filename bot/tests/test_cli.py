"""Tests for the CLI surfaces used to exercise the pipeline."""

import asyncio
import contextlib
import json
import pathlib

import httpx
import pytest
import respx

from signalbooks import __main__ as cli
from signalbooks import config, jobs, messages, signal

API = "https://api.torbox.app/v1/api/torrents"
ABB_URL = "https://audiobookbay.lu/abss/test-book-ada-example/"
HASH = "0123456789abcdef0123456789abcdef01234567"
FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "abb_detail.html"


def item(**overrides) -> dict:
    base = {
        "id": 42,
        "hash": HASH,
        "name": "Test Book - Ada Example",
        "download_state": "cached",
        "download_finished": False,
        "cached": True,
        "files": [{"id": 1, "name": "test-book.mp3", "size": 1000}],
    }
    base.update(overrides)
    return base


def mylist(data: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "data": data})


@pytest.fixture(autouse=True)
def in_tmp_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TORBOX_API_KEY", "test-key")
    return tmp_path


def test_dry_run_prints_the_scraped_fields(capsys):
    with respx.mock:
        respx.get(ABB_URL).mock(return_value=httpx.Response(200, text=FIXTURE.read_text()))
        assert cli.main(["--abb", ABB_URL, "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "Test Book - Ada Example" in out
    assert "Ada Example" in out
    assert HASH in out


def test_wait_adds_and_prints_the_link_without_sleeping_for_a_cached_torrent(capsys):
    with respx.mock:
        respx.get(ABB_URL).mock(return_value=httpx.Response(200, text=FIXTURE.read_text()))
        respx.get(f"{API}/mylist").mock(return_value=mylist([item()]))
        respx.get(f"{API}/requestdl").mock(
            return_value=httpx.Response(200, json={"success": True, "data": "https://cdn/x.zip"})
        )
        assert cli.main(["--abb", ABB_URL, "--wait"]) == 0

    out = capsys.readouterr().out
    assert "added: Test Book - Ada Example (torrent 42)" in out
    assert "https://cdn/x.zip" in out
    state = json.loads(pathlib.Path("state.json").read_text())
    assert state["cli:42"]["sent"] is True


def test_a_second_identical_link_is_ignored(capsys):
    with respx.mock:
        respx.get(ABB_URL).mock(return_value=httpx.Response(200, text=FIXTURE.read_text()))
        respx.get(f"{API}/mylist").mock(return_value=mylist([item()]))
        respx.get(f"{API}/requestdl").mock(
            return_value=httpx.Response(200, json={"success": True, "data": "https://cdn/x.zip"})
        )
        assert cli.main(["--abb", ABB_URL, "--wait"]) == 0
        capsys.readouterr()
        assert cli.main(["--abb", ABB_URL, "--wait"]) == 0

    assert "already added: Test Book - Ada Example" in capsys.readouterr().out


def test_link_requests_a_fresh_url_for_a_stored_torrent(capsys):
    with respx.mock:
        respx.get(f"{API}/mylist").mock(return_value=mylist([item()]))
        respx.get(f"{API}/requestdl").mock(
            return_value=httpx.Response(200, json={"success": True, "data": "https://cdn/fresh"})
        )
        assert cli.main(["--link", "42"]) == 0

    assert "https://cdn/fresh" in capsys.readouterr().out
    # A manual link request must not suppress a pending job's chat reply.
    assert not pathlib.Path("state.json").exists()


def test_link_for_an_unknown_torrent_fails_cleanly(capsys):
    with respx.mock:
        respx.get(f"{API}/mylist").mock(return_value=mylist([]))
        assert cli.main(["--link", "42"]) == 1

    assert "not on TorBox" in capsys.readouterr().err


def test_link_refuses_a_torrent_that_is_not_ready(capsys):
    with respx.mock:
        respx.get(f"{API}/mylist").mock(
            return_value=mylist(
                [item(download_state="downloading", download_finished=False, cached=False)]
            )
        )
        assert cli.main(["--link", "42"]) == 1

    assert "still downloading" in capsys.readouterr().err


def test_link_works_even_when_the_state_file_is_unreadable(capsys):
    pathlib.Path("state.json").write_text("{broken")
    with respx.mock:
        respx.get(f"{API}/mylist").mock(return_value=mylist([item()]))
        respx.get(f"{API}/requestdl").mock(
            return_value=httpx.Response(200, json={"success": True, "data": "https://cdn/fresh"})
        )
        assert cli.main(["--link", "42"]) == 0

    assert "https://cdn/fresh" in capsys.readouterr().out


def test_wait_resumes_a_pending_duplicate_instead_of_exiting(capsys):
    state = jobs.State()
    state.add(42, "cli", "Test Book - Ada Example", ABB_URL)
    state.save()
    with respx.mock:
        respx.get(ABB_URL).mock(return_value=httpx.Response(200, text=FIXTURE.read_text()))
        respx.get(f"{API}/mylist").mock(return_value=mylist([item()]))
        respx.get(f"{API}/requestdl").mock(
            return_value=httpx.Response(200, json={"success": True, "data": "https://cdn/x.zip"})
        )
        assert cli.main(["--abb", ABB_URL, "--wait"]) == 0

    out = capsys.readouterr().out
    assert "already added: Test Book - Ada Example" in out
    assert "https://cdn/x.zip" in out


def test_wait_without_an_api_key_fails_cleanly(monkeypatch, capsys):
    monkeypatch.delenv("TORBOX_API_KEY")
    assert cli.main(["--abb", ABB_URL, "--wait"]) == 1

    assert "TORBOX_API_KEY" in capsys.readouterr().err


def test_abb_needs_a_mode():
    with pytest.raises(SystemExit) as exc:
        cli.main(["--abb", ABB_URL])

    assert exc.value.code == 2


def test_serve_refuses_to_start_without_an_account_and_torbox_key(monkeypatch, capsys):
    monkeypatch.delenv("SIGNAL_ACCOUNT", raising=False)
    monkeypatch.delenv("SIGNAL_ALLOWED_NUMBERS", raising=False)
    monkeypatch.delenv("SIGNAL_GROUP_ID", raising=False)
    monkeypatch.setenv("TORBOX_API_KEY", "test-key")
    assert cli.main(["--serve"]) == 1

    assert "SIGNAL_ACCOUNT and TORBOX_API_KEY" in capsys.readouterr().err


def test_serve_refuses_to_start_with_no_chat_it_could_answer(monkeypatch, capsys):
    monkeypatch.setenv("SIGNAL_ACCOUNT", "+15550000001")
    monkeypatch.delenv("SIGNAL_ALLOWED_NUMBERS", raising=False)
    monkeypatch.delenv("SIGNAL_GROUP_ID", raising=False)
    monkeypatch.setenv("TORBOX_API_KEY", "test-key")
    assert cli.main(["--serve"]) == 1

    assert "SIGNAL_ALLOWED_NUMBERS or SIGNAL_GROUP_ID" in capsys.readouterr().err


def test_serve_refuses_to_start_without_a_torbox_key(monkeypatch, capsys):
    monkeypatch.setenv("SIGNAL_ACCOUNT", "+15550000001")
    monkeypatch.setenv("SIGNAL_ALLOWED_NUMBERS", "+15550000002")
    monkeypatch.delenv("TORBOX_API_KEY")
    assert cli.main(["--serve"]) == 1

    assert "TORBOX_API_KEY" in capsys.readouterr().err


def test_serve_takes_neither_dry_run_nor_wait():
    with pytest.raises(SystemExit) as exc:
        cli.main(["--serve", "--wait"])

    assert exc.value.code == 2


ALLOWED = "+15550000002"
STRANGER = "+15559999999"
BOT_ACCOUNT = "+15550000001"


def run_serve(
    tmp_path,
    monkeypatch,
    *texts,
    sender=ALLOWED,
    replies=1,
    wait=3.0,
    group=None,
    group_id=None,
    poll_seconds=0.01,
):
    """Drive the real serve loop against a fake daemon and return its send requests."""
    socket_path = tmp_path / "signal.sock"
    sent: list[dict] = []

    async def daemon(reader, writer):
        for text in texts:
            data: dict = {"message": text}
            if group:
                data["groupInfo"] = {"groupId": group}
            envelope = {"sourceNumber": sender, "dataMessage": data}
            notification = {"jsonrpc": "2.0", "method": "receive", "params": {"envelope": envelope}}
            writer.write(json.dumps(notification).encode() + b"\n")
        await writer.drain()
        try:
            while len(sent) < replies:
                request = json.loads(await asyncio.wait_for(reader.readline(), wait))
                sent.append(request)
                response = {"jsonrpc": "2.0", "id": request.get("id"), "result": {"timestamp": 1}}
                writer.write(json.dumps(response).encode() + b"\n")
                await writer.drain()
        except TimeoutError:
            pass
        writer.close()

    async def scenario():
        server = await asyncio.start_unix_server(daemon, path=str(socket_path))
        cfg = config.Config(
            torbox_api_key="test-key",
            abb_cookie=None,
            signal_account=BOT_ACCOUNT,
            signal_socket=socket_path,
            allowed_numbers=frozenset({ALLOWED}),
            signal_group_id=group_id,
        )
        task = asyncio.create_task(cli._serve(cfg))
        try:
            deadline = asyncio.get_running_loop().time() + wait
            while len(sent) < replies and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)  # let a delivery be recorded after its reply
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            server.close()
            await server.wait_closed()

    monkeypatch.setattr(jobs, "POLL_SECONDS", poll_seconds)
    asyncio.run(asyncio.wait_for(scenario(), wait + 5))
    return sent


def test_serve_answer_only_an_allowlisted_direct_message(tmp_path, monkeypatch):
    assert run_serve(tmp_path, monkeypatch, "hello", sender=STRANGER, replies=0, wait=0.3) == []


def test_serve_replies_to_help(tmp_path, monkeypatch):
    sent = run_serve(tmp_path, monkeypatch, "/help")

    assert sent[0]["params"] == {"recipient": [ALLOWED], "message": messages.HELP}


def test_serve_replies_usage_to_an_unknown_command(tmp_path, monkeypatch):
    sent = run_serve(tmp_path, monkeypatch, "/nope")

    assert sent[0]["params"]["message"] == messages.usage("")


def test_serve_relink_with_nothing_stored_is_one_line(tmp_path, monkeypatch):
    sent = run_serve(tmp_path, monkeypatch, "/relink")

    assert sent[0]["params"]["message"] == (
        "Nothing has been sent in this chat yet. Use /abb with a book link."
    )


def test_serve_relink_with_a_pending_job_says_it_is_still_downloading(tmp_path, monkeypatch):
    state = jobs.State()
    state.add(42, ALLOWED, "Test Book - Ada Example", ABB_URL)
    state.save()
    with respx.mock:
        # The background poll walks mylist for the pending job; relink itself
        # must not be the thing that asks for a link. Nothing is listed yet, so
        # the poll sends no progress message to race with the reply.
        respx.get(f"{API}/mylist").mock(return_value=mylist([]))
        sent = run_serve(tmp_path, monkeypatch, "/relink")

    assert sent[0]["params"]["message"] == (
        "The last book in this chat is still downloading. Its link will arrive when it's ready."
    )


def test_serve_usage_for_abb_without_a_link_makes_no_request(tmp_path, monkeypatch):
    with respx.mock:  # no routes: the parser must not reach the network
        sent = run_serve(tmp_path, monkeypatch, "/abb")

    assert sent[0]["params"]["message"] == messages.usage("abb")


def test_serve_usage_for_a_malformed_relink_makes_no_request(tmp_path, monkeypatch):
    with respx.mock:  # no routes: the parser must not reach the network
        sent = run_serve(tmp_path, monkeypatch, "/relink abc")

    assert sent[0]["params"]["message"] == messages.usage("relink")


def test_serve_malformed_abb_link_is_one_line_without_a_fetch(tmp_path, monkeypatch):
    with respx.mock:  # no routes: the link is rejected before fetching
        sent = run_serve(tmp_path, monkeypatch, "/abb not-a-link")

    assert sent[0]["params"]["message"] == (
        "That isn't an AudiobookBay link. Send /abb with the book's page link."
    )


def test_serve_relink_reports_a_torrent_torbox_no_longer_lists(tmp_path, monkeypatch):
    state = jobs.State()
    state.add(42, ALLOWED, "Test Book - Ada Example", ABB_URL)
    state.record_delivery(42, ALLOWED)
    state.save()
    with respx.mock:
        respx.get(f"{API}/mylist").mock(return_value=mylist([]))
        sent = run_serve(tmp_path, monkeypatch, "/relink")

    assert sent[0]["params"]["message"] == (
        "Torrent 42 is not on TorBox. Send /abb with the book link to add it again."
    )


def test_serve_ignores_a_bare_link_and_plain_chatter(tmp_path, monkeypatch):
    sent = run_serve(tmp_path, monkeypatch, ABB_URL, "just talking", replies=0, wait=0.4)

    assert sent == []


GROUP = "r2V4group=="


def test_serve_answers_the_configured_group_and_replies_into_it(tmp_path, monkeypatch):
    sent = run_serve(tmp_path, monkeypatch, "/help", group=GROUP, group_id=GROUP)

    assert sent[0]["params"] == {"groupId": GROUP, "message": messages.HELP}


def test_serve_ignores_a_group_it_was_not_configured_for(tmp_path, monkeypatch):
    sent = run_serve(
        tmp_path, monkeypatch, "/help", group=GROUP, group_id="other==", replies=0, wait=0.3
    )

    assert sent == []


def test_serve_trusts_every_member_of_the_configured_group(tmp_path, monkeypatch):
    sent = run_serve(
        tmp_path, monkeypatch, "/help", sender=STRANGER, group=GROUP, group_id=GROUP
    )

    assert sent[0]["params"] == {"groupId": GROUP, "message": messages.HELP}


def test_serve_still_replies_to_a_direct_message_when_a_group_is_configured(tmp_path, monkeypatch):
    sent = run_serve(tmp_path, monkeypatch, "/help", group_id=GROUP)

    assert sent[0]["params"] == {"recipient": [ALLOWED], "message": messages.HELP}


def test_serve_adds_a_link_and_delivers_its_url(tmp_path, monkeypatch):
    with respx.mock:
        respx.get(ABB_URL).mock(return_value=httpx.Response(200, text=FIXTURE.read_text()))
        respx.get(f"{API}/mylist").mock(return_value=mylist([item()]))
        respx.get(f"{API}/requestdl").mock(
            return_value=httpx.Response(200, json={"success": True, "data": "https://cdn/x.zip"})
        )
        sent = run_serve(tmp_path, monkeypatch, f"/abb {ABB_URL}", replies=2)

    texts = [request["params"]["message"] for request in sent]
    assert "Added: Test Book - Ada Example\nLink coming when it's ready." in texts
    assert any("https://cdn/x.zip" in text for text in texts)
    assert any("1 file · 1 KB" in text for text in texts)
    assert all(request["params"]["recipient"] == [ALLOWED] for request in sent)
    assert json.loads(pathlib.Path("state.json").read_text())["+15550000002:42"]["sent"] is True


def test_serve_delivers_a_cached_book_right_after_added(tmp_path, monkeypatch):
    with respx.mock:
        respx.get(ABB_URL).mock(return_value=httpx.Response(200, text=FIXTURE.read_text()))
        respx.get(f"{API}/mylist").mock(return_value=mylist([item()]))
        respx.get(f"{API}/requestdl").mock(
            return_value=httpx.Response(200, json={"success": True, "data": "https://cdn/x.zip"})
        )
        sent = run_serve(tmp_path, monkeypatch, f"/abb {ABB_URL}", replies=2, poll_seconds=30)

    texts = [request["params"]["message"] for request in sent]
    assert texts[0].startswith("Added: Test Book - Ada Example")
    assert "https://cdn/x.zip" in texts[1]


def test_serve_turns_one_progress_message_into_the_delivery(tmp_path, monkeypatch):
    title = "Test Book - Ada Example"
    with respx.mock:
        respx.get(ABB_URL).mock(return_value=httpx.Response(200, text=FIXTURE.read_text()))
        respx.get(f"{API}/mylist").mock(
            side_effect=[
                mylist([]),  # the dedup lookup in jobs.add
                mylist(
                    [
                        item(
                            download_state="downloading",
                            download_finished=False,
                            cached=False,
                            progress=0.1,
                            size=2000,
                        )
                    ]
                ),
                mylist(
                    [
                        item(
                            download_state="downloading",
                            download_finished=False,
                            cached=False,
                            progress=0.3,
                            size=2000,
                        )
                    ]
                ),
                mylist([item(download_state="completed", download_finished=True, progress=1)]),
            ]
        )
        respx.post(f"{API}/createtorrent").mock(
            return_value=httpx.Response(
                200, json={"success": True, "data": {"torrent_id": 42, "hash": HASH}}
            )
        )
        respx.get(f"{API}/requestdl").mock(
            return_value=httpx.Response(200, json={"success": True, "data": "https://cdn/x.zip"})
        )
        sent = run_serve(tmp_path, monkeypatch, f"/abb {ABB_URL}", replies=4)

    added, bar, stepped, finished = [request["params"] for request in sent]
    assert added["message"].startswith("Added: Test Book - Ada Example")
    assert bar["message"] == messages.progress(title, 10, 2000)
    assert "editTimestamp" not in bar and bar["noUrgent"] is True
    assert stepped["message"] == messages.progress(title, 30, 2000)
    assert stepped["editTimestamp"] == 1 and stepped["noUrgent"] is True
    assert finished["message"] == messages.delivery(
        jobs.Delivery(42, ALLOWED, title, url="https://cdn/x.zip", files=1, size=1000)
    )
    assert finished["editTimestamp"] == 1
    assert "noUrgent" not in finished  # the link is worth a notification
    state = json.loads(pathlib.Path("state.json").read_text())
    assert state["+15550000002:42"]["sent"] is True


def test_a_failed_finishing_edit_still_sends_the_link(tmp_path):
    title = "Test Book - Ada Example"
    state = jobs.State(tmp_path / "state.json")
    state.add(42, ALLOWED, title, ABB_URL)
    state.set_progress(42, ALLOWED, 99, 75)
    delivery = jobs.Delivery(42, ALLOWED, title, url="https://cdn/x.zip", files=1, size=1000)

    class FlakyClient:
        def __init__(self):
            self.edits = 0
            self.sent: list[tuple[str, str]] = []

        async def edit(self, chat: str, timestamp: int, text: str, urgent: bool = True) -> None:
            self.edits += 1
            raise signal.SignalError("edit refused")

        async def send(self, chat: str, text: str, urgent: bool = True) -> int:
            self.sent.append((chat, text))
            return 1

    async def scenario():
        client = FlakyClient()
        await cli._deliver(client, state, delivery)
        return client

    client = asyncio.run(scenario())

    assert client.edits == 1
    assert client.sent == [(ALLOWED, messages.delivery(delivery))]
    assert state.get(42, ALLOWED)["sent"] is True


def test_serve_relink_names_the_last_book(tmp_path, monkeypatch):
    state = jobs.State()
    state.add(42, ALLOWED, "Test Book - Ada Example", ABB_URL)
    state.record_delivery(42, ALLOWED)
    state.save()
    with respx.mock:
        respx.get(f"{API}/mylist").mock(return_value=mylist([item()]))
        respx.get(f"{API}/requestdl").mock(
            return_value=httpx.Response(200, json={"success": True, "data": "https://cdn/fresh"})
        )
        sent = run_serve(tmp_path, monkeypatch, "/relink")

    assert sent[0]["params"]["message"] == (
        "Fresh link for Test Book - Ada Example\n"
        "https://cdn/fresh\n"
        "Start the download within about 3 hours."
    )


def test_serve_duplicate_add_points_at_relink(tmp_path, monkeypatch):
    state = jobs.State()
    state.add(42, ALLOWED, "Test Book - Ada Example", ABB_URL)
    state.record_delivery(42, ALLOWED)
    state.save()
    with respx.mock:
        respx.get(ABB_URL).mock(return_value=httpx.Response(200, text=FIXTURE.read_text()))
        sent = run_serve(tmp_path, monkeypatch, f"/abb {ABB_URL}")

    assert sent[0]["params"]["message"] == (
        "Already added: Test Book - Ada Example\n/relink 42 for a fresh one."
    )


def test_poll_loop_delivers_a_job_added_after_it_started(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "POLL_SECONDS", 0.02)
    state = jobs.State(tmp_path / "state.json")

    class FakeClient:
        def __init__(self):
            self.sent: list[tuple[str, str]] = []

        async def send(self, chat: str, text: str) -> None:
            self.sent.append((chat, text))

    cfg = config.Config(
        torbox_api_key="test-key",
        abb_cookie=None,
        signal_account="",
        signal_socket=tmp_path / "unused.sock",
        allowed_numbers=frozenset(),
        signal_group_id=None,
    )

    async def scenario():
        with respx.mock:
            respx.get(f"{API}/mylist").mock(return_value=mylist([item()]))
            respx.get(f"{API}/requestdl").mock(
                return_value=httpx.Response(200, json={"success": True, "data": "https://cdn/tick"})
            )
            client = FakeClient()
            task = asyncio.create_task(cli._poll_loop(client, cfg, state, asyncio.Event()))
            try:
                await asyncio.sleep(0.05)  # let the loop take its first idle turn
                state.add(42, "cli", "Test Book - Ada Example", ABB_URL)  # deliberately no wake
                deadline = asyncio.get_running_loop().time() + 2
                while not client.sent and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(0.01)
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        return client.sent

    sent = asyncio.run(scenario())
    assert len(sent) == 1
    assert "https://cdn/tick" in sent[0][1]


def test_serve_logs_no_message_body_link_or_key(tmp_path, monkeypatch, capsys):
    with respx.mock:
        respx.get(ABB_URL).mock(return_value=httpx.Response(200, text=FIXTURE.read_text()))
        respx.get(f"{API}/mylist").mock(return_value=mylist([item()]))
        respx.get(f"{API}/requestdl").mock(
            return_value=httpx.Response(200, json={"success": True, "data": "https://cdn/secret"})
        )
        run_serve(tmp_path, monkeypatch, f"/abb {ABB_URL}", replies=2)

    err = capsys.readouterr().err
    assert ALLOWED in err  # who, and which command, is all that is logged
    assert ABB_URL not in err
    assert "https://cdn/secret" not in err
    assert "test-key" not in err
