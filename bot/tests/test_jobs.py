"""Tests for dedup, the poll loop, and the state file."""

import asyncio
import json
import urllib.parse

import httpx
import pytest
import respx

from signalbooks import abb, jobs

API = "https://api.torbox.app/v1/api/torrents"
KEY = "test-key"
HASH = "0123456789abcdef0123456789abcdef01234567"
URL = "https://audiobookbay.lu/abss/test-book-ada-example/"


def book(url: str = URL) -> abb.Book:
    return abb.Book(url=url, title="Test Book - Ada Example", author="Ada Example", info_hash=HASH)


def item(torrent_id: int = 42, **overrides) -> dict:
    base = {
        "id": torrent_id,
        "hash": HASH,
        "name": "Test Book - Ada Example",
        "download_state": "completed",
        "download_finished": True,
        "cached": False,
        "files": [{"id": 1, "name": "test-book.mp3", "size": 1000}],
    }
    base.update(overrides)
    return base


def mylist(data: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "error": None, "data": data})


def link(url: str = "https://cdn.example/book") -> httpx.Response:
    return httpx.Response(200, json={"success": True, "data": url})


def pending_state(tmp_path, torrent_id: int = 42, chat: str = "cli") -> jobs.State:
    state = jobs.State(tmp_path / "state.json")
    state.add(torrent_id, chat, "Test Book - Ada Example", URL)
    return state


def test_state_round_trips_without_leaving_a_temp_file(tmp_path):
    state = pending_state(tmp_path)
    state.save()

    reread = jobs.State(tmp_path / "state.json")

    assert reread.pending() == [
        (
            "cli",
            42,
            {
                "chat": "cli",
                "title": "Test Book - Ada Example",
                "url": URL,
                "added_at": state.get(42, "cli")["added_at"],
                "sent": False,
                "misses": 0,
            },
        )
    ]
    assert not (tmp_path / "state.json.tmp").exists()


def test_state_moves_a_reply_to_the_end_so_relink_finds_it(tmp_path):
    state = jobs.State(tmp_path / "state.json")
    state.add(1, "cli", "one", URL)
    state.add(2, "cli", "two", URL)

    state.record_delivery(1, "cli")
    state.record_delivery(2, "cli")
    assert state.last_sent("cli") == 2

    state.record_delivery(1, "cli")
    assert state.last_sent("cli") == 1
    assert state.last_sent("other") is None


def test_state_keeps_one_job_per_chat_for_the_same_torrent(tmp_path):
    state = jobs.State(tmp_path / "state.json")
    state.add(42, "alice", "Test Book", URL)
    state.add(42, "group", "Test Book", URL)

    state.record_delivery(42, "alice")

    assert [chat for chat, _, _ in state.pending()] == ["group"]
    assert state.last_sent("alice") == 42
    assert state.get(42, "group")["sent"] is False


def test_state_accepts_a_chat_id_containing_a_colon(tmp_path):
    state = jobs.State(tmp_path / "state.json")
    state.add(42, "group:abc", "Test Book", URL)
    state.save()

    reread = jobs.State(tmp_path / "state.json")
    assert reread.pending()[0][:2] == ("group:abc", 42)

    reread.record_delivery(42, "group:abc")
    assert reread.last_sent("group:abc") == 42


def test_corrupt_state_is_reported_not_silently_reset(tmp_path):
    (tmp_path / "state.json").write_text("{not json")

    with pytest.raises(jobs.JobsError, match="state.json"):
        jobs.State(tmp_path / "state.json")


def test_state_rejects_an_entry_that_does_not_match_its_key(tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({"42": {"chat": "cli", "title": "x"}}))

    with pytest.raises(jobs.JobsError, match="bad job entry"):
        jobs.State(tmp_path / "state.json")


def test_add_reuses_the_existing_torrent_for_a_known_hash(tmp_path):
    state = jobs.State(tmp_path / "state.json")
    with respx.mock:
        respx.get(f"{API}/mylist").mock(return_value=mylist([item(42)]))
        added = asyncio.run(jobs.add(KEY, state, "cli", book()))

    assert added == jobs.Added(torrent_id=42, title="Test Book - Ada Example")
    assert jobs.State(tmp_path / "state.json").get(42, "cli")["url"] == URL


def test_add_creates_a_torrent_when_the_hash_is_new(tmp_path):
    state = jobs.State(tmp_path / "state.json")
    with respx.mock:
        respx.get(f"{API}/mylist").mock(return_value=mylist([]))
        route = respx.post(f"{API}/createtorrent").mock(
            return_value=httpx.Response(200, json={"success": True, "data": {"torrent_id": 7, "hash": HASH}})
        )
        added = asyncio.run(jobs.add(KEY, state, "cli", book()))

    assert added.torrent_id == 7
    body = urllib.parse.parse_qsl(route.calls[0].request.content.decode())
    assert ("magnet", f"magnet:?xt=urn:btih:{HASH}&dn=Test Book%20-%20Ada%20Example") in body
    assert state.get(7, "cli")["title"] == "Test Book - Ada Example"


def test_add_does_not_reuse_a_failed_torrent(tmp_path):
    state = jobs.State(tmp_path / "state.json")
    with respx.mock:
        respx.get(f"{API}/mylist").mock(
            return_value=mylist([item(42, download_state="error", download_finished=False)])
        )
        respx.post(f"{API}/createtorrent").mock(
            return_value=httpx.Response(200, json={"success": True, "data": {"torrent_id": 7, "hash": HASH}})
        )
        added = asyncio.run(jobs.add(KEY, state, "cli", book()))

    assert added.torrent_id == 7


def test_add_ignores_the_same_link_from_the_same_chat_inside_ten_minutes(tmp_path):
    state = jobs.State(tmp_path / "state.json")
    state.add(42, "cli", "Test Book - Ada Example", URL, now=1000)
    with respx.mock:  # no routes: any HTTP call fails the test
        added = asyncio.run(jobs.add(KEY, state, "cli", book(), now=1000 + 599))

    assert added.duplicate and added.torrent_id == 42
    assert added.sent is False


def test_add_reports_a_duplicate_that_was_already_delivered(tmp_path):
    state = jobs.State(tmp_path / "state.json")
    state.add(42, "cli", "Test Book - Ada Example", URL)
    state.record_delivery(42, "cli")
    with respx.mock:  # dedup returns before any HTTP call
        added = asyncio.run(jobs.add(KEY, state, "cli", book()))

    assert added == jobs.Added(42, "Test Book - Ada Example", duplicate=True, sent=True)


def test_add_accepts_the_same_link_after_the_dedup_window(tmp_path):
    state = jobs.State(tmp_path / "state.json")
    state.add(42, "cli", "Test Book - Ada Example", URL, now=1000)
    with respx.mock:
        respx.get(f"{API}/mylist").mock(return_value=mylist([item(42)]))
        added = asyncio.run(jobs.add(KEY, state, "cli", book(), now=1000 + 601))

    assert not added.duplicate


def test_poll_makes_no_request_when_nothing_is_pending(tmp_path):
    with respx.mock:  # no routes: any HTTP call fails the test
        deliveries = asyncio.run(jobs.poll(KEY, jobs.State(tmp_path / "state.json")))

    assert deliveries == []


def test_poll_uses_one_mylist_walk_for_all_pending_jobs(tmp_path):
    state = jobs.State(tmp_path / "state.json")
    state.add(42, "cli", "one", URL)
    state.add(43, "cli", "two", URL)
    with respx.mock:
        route = respx.get(f"{API}/mylist").mock(return_value=mylist([item(42), item(43)]))
        respx.get(f"{API}/requestdl").mock(return_value=link())
        deliveries = asyncio.run(jobs.poll(KEY, state))

    assert {d.torrent_id for d in deliveries} == {42, 43}
    assert route.call_count == 1


def test_poll_delivers_a_single_file_with_a_direct_link(tmp_path):
    state = pending_state(tmp_path)
    with respx.mock:
        respx.get(f"{API}/mylist").mock(return_value=mylist([item(42)]))
        route = respx.get(f"{API}/requestdl").mock(return_value=link("https://cdn.example/test-book.mp3"))
        deliveries = asyncio.run(jobs.poll(KEY, state))

    assert deliveries == [
        jobs.Delivery(
            42,
            "cli",
            "Test Book - Ada Example",
            url="https://cdn.example/test-book.mp3",
            files=1,
            size=1000,
            zipped=False,
        )
    ]
    params = route.calls[0].request.url.params
    assert params["file_id"] == "1"
    assert "zip_link" not in params
    # The caller records the reply after sending it, not before.
    assert state.get(42, "cli")["sent"] is False


def test_poll_delivers_a_multi_file_torrent_as_one_zip(tmp_path):
    state = pending_state(tmp_path)
    files = [
        {"id": 1, "name": "01.mp3", "size": 1000},
        {"id": 2, "name": "02.mp3", "size": 2000},
    ]
    with respx.mock:
        respx.get(f"{API}/mylist").mock(return_value=mylist([item(42, files=files)]))
        route = respx.get(f"{API}/requestdl").mock(return_value=link("https://cdn.example/zip/abc"))
        deliveries = asyncio.run(jobs.poll(KEY, state))

    assert deliveries[0].url == "https://cdn.example/zip/abc"
    assert deliveries[0].files == 2
    assert deliveries[0].size == 3000
    assert deliveries[0].zipped is True
    assert route.calls[0].request.url.params["zip_link"] == "true"


def test_poll_delivers_a_cached_torrent_without_waiting_for_a_download(tmp_path):
    state = pending_state(tmp_path)
    with respx.mock:
        respx.get(f"{API}/mylist").mock(
            return_value=mylist([item(42, download_state="cached", download_finished=False, cached=True)])
        )
        respx.get(f"{API}/requestdl").mock(return_value=link())
        deliveries = asyncio.run(jobs.poll(KEY, state))

    assert deliveries and deliveries[0].url


def test_poll_waits_while_the_torrent_is_downloading(tmp_path):
    state = pending_state(tmp_path)
    with respx.mock:
        respx.get(f"{API}/mylist").mock(
            return_value=mylist([item(42, download_state="downloading", download_finished=False)])
        )
        deliveries = asyncio.run(jobs.poll(KEY, state))

    assert deliveries == []
    assert state.get(42, "cli")["sent"] is False


def test_poll_waits_when_a_done_torrent_has_no_file_list_yet(tmp_path):
    state = pending_state(tmp_path)
    with respx.mock:
        respx.get(f"{API}/mylist").mock(return_value=mylist([item(42, files=[])]))
        deliveries = asyncio.run(jobs.poll(KEY, state))

    assert deliveries == []


def test_poll_reports_a_failed_torrent_only_once_after_it_is_recorded(tmp_path):
    state = pending_state(tmp_path)
    with respx.mock:
        respx.get(f"{API}/mylist").mock(
            return_value=mylist([item(42, download_state="error", download_finished=False)])
        )
        first = asyncio.run(jobs.poll(KEY, state))
    assert first[0].error == (
        "TorBox reported a download error. Send /abb with the book link again in 10 minutes."
    )

    state.record_delivery(42, "cli")
    state.save()
    with respx.mock:  # delivered jobs are not polled again
        assert asyncio.run(jobs.poll(KEY, state)) == []


def test_poll_reports_a_stalled_torrent_as_a_stall(tmp_path):
    state = pending_state(tmp_path)
    with respx.mock:
        respx.get(f"{API}/mylist").mock(
            return_value=mylist([item(42, download_state="stalled (no seeds)", download_finished=False)])
        )
        deliveries = asyncio.run(jobs.poll(KEY, state))

    assert deliveries[0].error == (
        "The download stalled. Send /abb with the book link again in 10 minutes."
    )


def test_poll_reports_a_failed_link_request(tmp_path):
    state = pending_state(tmp_path)
    with respx.mock:
        respx.get(f"{API}/mylist").mock(return_value=mylist([item(42)]))
        respx.get(f"{API}/requestdl").mock(
            return_value=httpx.Response(200, json={"success": False, "detail": "no slots"})
        )
        deliveries = asyncio.run(jobs.poll(KEY, state))

    assert deliveries[0].error == "TorBox: no slots. Try /relink 42 in a minute."


def test_poll_does_not_stack_a_relink_hint_on_a_detail_that_has_one(tmp_path):
    state = pending_state(tmp_path)
    with respx.mock:
        respx.get(f"{API}/mylist").mock(return_value=mylist([item(42)]))
        respx.get(f"{API}/requestdl").mock(
            return_value=httpx.Response(
                200, json={"success": False, "detail": "temporary failure. Try again later"}
            )
        )
        deliveries = asyncio.run(jobs.poll(KEY, state))

    assert deliveries[0].error == "TorBox: temporary failure. Try again later."


def test_poll_keeps_retrying_a_torrent_torbox_does_not_list_yet(tmp_path):
    state = pending_state(tmp_path)
    with respx.mock:
        respx.get(f"{API}/mylist").mock(return_value=mylist([]))
        assert asyncio.run(jobs.poll(KEY, state)) == []

    assert state.get(42, "cli")["sent"] is False


def test_poll_gives_up_after_repeated_misses(tmp_path):
    state = pending_state(tmp_path)
    with respx.mock:
        respx.get(f"{API}/mylist").mock(return_value=mylist([]))
        for _ in range(jobs.MISS_LIMIT - 1):
            assert asyncio.run(jobs.poll(KEY, state)) == []
        deliveries = asyncio.run(jobs.poll(KEY, state))

    assert deliveries[0].error == (
        "It's no longer on TorBox. Send /abb with the book link again in 10 minutes."
    )


def test_a_seen_torrent_resets_the_miss_count_and_the_count_survives_a_restart(tmp_path):
    state = pending_state(tmp_path)
    with respx.mock:
        respx.get(f"{API}/mylist").mock(return_value=mylist([]))
        for _ in range(jobs.MISS_LIMIT - 1):
            asyncio.run(jobs.poll(KEY, state))

    assert jobs.State(tmp_path / "state.json").get(42, "cli")["misses"] == jobs.MISS_LIMIT - 1

    with respx.mock:
        respx.get(f"{API}/mylist").mock(
            return_value=mylist([item(42, download_state="downloading", download_finished=False)])
        )
        assert asyncio.run(jobs.poll(KEY, state)) == []

    assert state.get(42, "cli")["misses"] == 0


def delivered_state(tmp_path, *torrent_ids: int, chat: str = "cli") -> jobs.State:
    state = jobs.State(tmp_path / "state.json")
    for torrent_id in torrent_ids:
        state.add(torrent_id, chat, f"book {torrent_id}", URL)
        state.record_delivery(torrent_id, chat)
    return state


def test_relink_finds_the_link_for_the_last_delivered_torrent(tmp_path):
    state = delivered_state(tmp_path, 42, 43)
    with respx.mock:
        respx.get(f"{API}/mylist").mock(return_value=mylist([item(42), item(43)]))
        route = respx.get(f"{API}/requestdl").mock(return_value=link("https://cdn.example/relinked"))
        url = asyncio.run(jobs.relink(KEY, state, "cli"))

    assert url == "https://cdn.example/relinked"
    assert route.call_count == 1
    assert route.calls[0].request.url.params["torrent_id"] == "43"


def test_relink_with_an_explicit_torrent_id_ignores_the_last_delivery(tmp_path):
    state = delivered_state(tmp_path, 42, 43)
    with respx.mock:
        respx.get(f"{API}/mylist").mock(return_value=mylist([item(42), item(43)]))
        route = respx.get(f"{API}/requestdl").mock(return_value=link())
        asyncio.run(jobs.relink(KEY, state, "cli", 42))

    assert route.calls[0].request.url.params["torrent_id"] == "42"


def test_relink_with_nothing_sent_makes_no_request(tmp_path):
    state = jobs.State(tmp_path / "state.json")
    # no routes: any HTTP call fails the test
    with respx.mock, pytest.raises(jobs.JobsError, match="nothing has been sent in this chat yet"):
        asyncio.run(jobs.relink(KEY, state, "cli"))


def test_relink_while_that_chat_still_has_a_pending_job_makes_no_request(tmp_path):
    state = pending_state(tmp_path)
    # no routes: any HTTP call fails the test
    with respx.mock, pytest.raises(jobs.JobsError, match="still downloading"):
        asyncio.run(jobs.relink(KEY, state, "cli"))


def test_relink_sees_no_pending_job_when_only_another_chat_is_waiting(tmp_path):
    state = pending_state(tmp_path, chat="group")
    # no routes: any HTTP call fails the test
    with respx.mock, pytest.raises(jobs.JobsError, match="nothing has been sent in this chat yet"):
        asyncio.run(jobs.relink(KEY, state, "cli"))


def test_relink_reports_a_torrent_torbox_no_longer_lists(tmp_path):
    state = delivered_state(tmp_path, 42)
    with respx.mock:
        respx.get(f"{API}/mylist").mock(return_value=mylist([]))
        with pytest.raises(
            jobs.JobsError,
            match="torrent 42 is not on TorBox. Send /abb with the book link to add it again",
        ):
            asyncio.run(jobs.relink(KEY, state, "cli"))


def test_relink_waits_for_a_torrent_that_is_still_downloading(tmp_path):
    state = delivered_state(tmp_path, 42)
    with respx.mock:
        respx.get(f"{API}/mylist").mock(
            return_value=mylist([item(42, download_state="downloading", download_finished=False)])
        )
        with pytest.raises(
            jobs.JobsError, match="torrent 42 is still downloading. Try again when it's done"
        ):
            asyncio.run(jobs.relink(KEY, state, "cli"))


def test_relink_reports_a_failed_torrent_before_waiting_on_it(tmp_path):
    state = delivered_state(tmp_path, 42)
    with respx.mock:
        respx.get(f"{API}/mylist").mock(
            return_value=mylist([item(42, download_state="stalled", download_finished=False)])
        )
        with pytest.raises(
            jobs.JobsError,
            match="torrent 42 failed on TorBox. Send /abb with the book link again in 10 minutes",
        ):
            asyncio.run(jobs.relink(KEY, state, "cli"))


def test_relink_only_sees_the_last_delivery_from_that_chat(tmp_path):
    state = jobs.State(tmp_path / "state.json")
    state.add(42, "alice", "book 42", URL)
    state.record_delivery(42, "alice")
    state.add(43, "group", "book 43", URL)
    state.record_delivery(43, "group")

    with respx.mock:
        respx.get(f"{API}/mylist").mock(return_value=mylist([item(42), item(43)]))
        route = respx.get(f"{API}/requestdl").mock(return_value=link())
        asyncio.run(jobs.relink(KEY, state, "alice"))

    assert route.call_count == 1
    assert route.calls[0].request.url.params["torrent_id"] == "42"


def test_relink_leaves_the_state_untouched(tmp_path):
    state = delivered_state(tmp_path, 42)
    state.add(43, "cli", "book 43", URL)
    before = json.loads(json.dumps(state.jobs))

    with respx.mock:
        respx.get(f"{API}/mylist").mock(return_value=mylist([item(42), item(43)]))
        respx.get(f"{API}/requestdl").mock(return_value=link())
        asyncio.run(jobs.relink(KEY, state, "cli"))
        # An explicit relink of a still-pending torrent must not mark it sent.
        asyncio.run(jobs.relink(KEY, state, "cli", 43))

    assert state.jobs == before
    assert [torrent_id for _, torrent_id, _ in state.pending()] == [43]
    assert state.get(43, "cli")["sent"] is False
