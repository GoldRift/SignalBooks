"""Tests for the CLI surfaces used to exercise the pipeline."""

import asyncio
import contextlib
import json
import pathlib

import httpx
import pytest
import respx

from signalbooks import __main__ as cli
from signalbooks import config, jobs

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

    assert "not ready yet (downloading)" in capsys.readouterr().err


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


def test_serve_refuses_to_start_without_an_account_and_allowlist(monkeypatch, capsys):
    monkeypatch.delenv("SIGNAL_ACCOUNT", raising=False)
    monkeypatch.delenv("SIGNAL_ALLOWED_NUMBERS", raising=False)
    assert cli.main(["--serve"]) == 1

    assert "SIGNAL_ACCOUNT and SIGNAL_ALLOWED_NUMBERS" in capsys.readouterr().err


def test_serve_takes_neither_dry_run_nor_wait():
    with pytest.raises(SystemExit) as exc:
        cli.main(["--serve", "--wait"])

    assert exc.value.code == 2


def test_serve_answers_only_an_allowlisted_direct_message(tmp_path):
    allowed, stranger = "+15550000002", "+15559999999"
    seen: list[dict] = []

    async def daemon(reader, writer):
        for sender in (stranger, allowed):
            envelope = {"sourceNumber": sender, "dataMessage": {"message": "hi"}}
            notification = {"jsonrpc": "2.0", "method": "receive", "params": {"envelope": envelope}}
            writer.write(json.dumps(notification).encode() + b"\n")
        await writer.drain()
        seen.append(json.loads(await asyncio.wait_for(reader.readline(), 5)))
        await reader.read()
        writer.close()

    async def scenario():
        server = await asyncio.start_unix_server(daemon, path=str(tmp_path / "signal.sock"))
        cfg = config.Config(
            torbox_api_key="",
            abb_cookie=None,
            signal_account="+15550000001",
            signal_socket=tmp_path / "signal.sock",
            allowed_numbers=frozenset({allowed}),
        )
        task = asyncio.create_task(cli._serve(cfg))
        try:
            while not seen:  # the stranger must not produce a second reply
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            server.close()
            await server.wait_closed()

    asyncio.run(asyncio.wait_for(scenario(), 10))

    assert len(seen) == 1
    assert seen[0]["method"] == "send"
    assert seen[0]["params"] == {"recipient": [allowed], "message": "pong"}
