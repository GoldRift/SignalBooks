"""Tests for the TorBox calls, against mocked HTTP."""

import asyncio
import json
import urllib.parse

import httpx
import pytest
import respx

from signalbooks import torbox

API = "https://api.torbox.app/v1/api/torrents"
KEY = "test-key"
HASH = "0123456789abcdef0123456789abcdef01234567"


def item(torrent_id: int, **overrides) -> dict:
    base = {
        "id": torrent_id,
        "hash": f"{torrent_id:040x}",
        "name": f"book-{torrent_id}",
        "download_state": "downloading",
        "download_finished": False,
        "cached": False,
        "files": [],
    }
    base.update(overrides)
    return base


def mylist(data: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "error": None, "data": data})


def test_create_torrent_posts_the_magnet_form_encoded():
    with respx.mock:
        route = respx.post(f"{API}/createtorrent").mock(
            return_value=httpx.Response(
                200,
                json={
                    "success": True,
                    "detail": "Found Cached Torrent. Using Cached Torrent.",
                    "data": {"hash": HASH.upper(), "torrent_id": 42},
                },
            )
        )
        torrent_id, info_hash = asyncio.run(torbox.create_torrent(KEY, "magnet:?xt=urn:btih:abc"))

    assert (torrent_id, info_hash) == (42, HASH)
    request = route.calls[0].request
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert dict(urllib.parse.parse_qsl(request.content.decode())) == {
        "magnet": "magnet:?xt=urn:btih:abc"
    }


def test_a_queued_torrent_says_so_instead_of_looking_broken():
    with respx.mock:
        respx.post(f"{API}/createtorrent").mock(
            return_value=httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "queued_id": 7,
                        "hash": HASH,
                        "active_limit": 3,
                        "current_active_downloads": 3,
                    },
                },
            )
        )
        with pytest.raises(
            torbox.TorboxError,
            match=r"download limit \(3/3\) and queued this book\. Send /abb with the book link again once a slot frees up",
        ):
            asyncio.run(torbox.create_torrent(KEY, "magnet:?xt=urn:btih:abc"))


def test_a_queued_torrent_without_slot_counts_still_says_why():
    with respx.mock:
        respx.post(f"{API}/createtorrent").mock(
            return_value=httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"queued_id": 7, "hash": HASH},
                },
            )
        )
        with pytest.raises(
            torbox.TorboxError,
            match="download limit and queued this book. Send /abb with the book link again once a slot frees up",
        ):
            asyncio.run(torbox.create_torrent(KEY, "magnet:?xt=urn:btih:abc"))


def test_create_torrent_without_a_torrent_or_queued_id_is_an_error():
    with respx.mock:
        respx.post(f"{API}/createtorrent").mock(
            return_value=httpx.Response(200, json={"success": True, "data": {"hash": HASH}})
        )
        with pytest.raises(torbox.TorboxError, match="torrent id"):
            asyncio.run(torbox.create_torrent(KEY, "magnet:?xt=urn:btih:abc"))


def test_find_by_hash_stops_at_the_first_matching_page():
    page = [item(i) for i in range(100)]
    page[50]["hash"] = HASH
    with respx.mock:
        route = respx.get(f"{API}/mylist").mock(return_value=mylist(page))
        found = asyncio.run(torbox.find_by_hash(KEY, HASH.upper()))

    assert found is not None and found.id == 50
    assert route.call_count == 1
    params = route.calls[0].request.url.params
    assert params["bypass_cache"] == "true"
    assert params["offset"] == "0"


def test_find_by_hash_walks_pages_until_it_finds_the_hash():
    with respx.mock:
        route = respx.get(f"{API}/mylist").mock(
            side_effect=[mylist([item(i) for i in range(100)]), mylist([item(100, hash=HASH)])]
        )
        found = asyncio.run(torbox.find_by_hash(KEY, HASH))

    assert found is not None and found.id == 100
    assert route.call_count == 2
    assert route.calls[1].request.url.params["offset"] == "100"


def test_find_many_walks_pages_until_every_id_is_seen():
    with respx.mock:
        route = respx.get(f"{API}/mylist").mock(
            side_effect=[
                mylist([item(i) for i in range(100)]),
                mylist([item(100, files=[{"id": 1, "name": "a.mp3", "size": 10}])]),
            ]
        )
        found = asyncio.run(torbox.find_many(KEY, {5, 100}, files=True))

    assert set(found) == {5, 100}
    assert route.call_count == 2
    assert found[100].files == (torbox.File(id=1, name="a.mp3", size=10),)
    assert route.calls[0].request.url.params["files"] == "true"


def test_find_many_leaves_absent_ids_out():
    with respx.mock:
        respx.get(f"{API}/mylist").mock(return_value=mylist([item(1)]))

        found = asyncio.run(torbox.find_many(KEY, {99}))

    assert found == {}


def test_torrent_flags_cover_cached_and_failed_states():
    cached = torbox.Torrent(
        id=1, hash=HASH, name="a", state="cached", finished=False, cached=True, files=()
    )
    stalled = torbox.Torrent(
        id=2, hash=HASH, name="b", state="stalled (no seeds)", finished=False, cached=False, files=()
    )
    assert cached.done and not cached.failed
    assert stalled.failed and not stalled.done


def test_find_by_hash_skips_a_failed_torrent():
    failed = item(1, hash=HASH, download_state="error", download_finished=False)
    with respx.mock:
        respx.get(f"{API}/mylist").mock(return_value=mylist([failed]))
        found = asyncio.run(torbox.find_by_hash(KEY, HASH))

    assert found is None


def test_mylist_with_an_unexpected_payload_is_an_error():
    with respx.mock:
        respx.get(f"{API}/mylist").mock(
            return_value=httpx.Response(200, json={"success": True, "data": {"not": "a list"}})
        )
        with pytest.raises(torbox.TorboxError, match="unexpected torrent list"):
            asyncio.run(torbox.find_many(KEY, {1}))


@pytest.mark.parametrize(
    "bad",
    [
        [{"id": None}],
        [{"name": "no id"}],
        ["not a dict"],
        [{"id": 1, "files": [{"name": "missing id"}]}],
    ],
)
def test_malformed_torrent_entries_are_errors(bad):
    with respx.mock:
        respx.get(f"{API}/mylist").mock(return_value=mylist(bad))
        with pytest.raises(torbox.TorboxError, match="unexpected reply"):
            asyncio.run(torbox.find_many(KEY, {1}))


def test_request_link_for_a_zip_sends_zip_link():
    with respx.mock:
        route = respx.get(f"{API}/requestdl").mock(
            return_value=httpx.Response(200, json={"success": True, "data": "https://cdn/zip/x"})
        )
        url = asyncio.run(torbox.request_link(KEY, 42, zip_link=True))

    assert url == "https://cdn/zip/x"
    params = route.calls[0].request.url.params
    assert params["token"] == KEY
    assert params["torrent_id"] == "42"
    assert params["zip_link"] == "true"
    assert "file_id" not in params


def test_request_link_for_one_file_sends_file_id():
    with respx.mock:
        route = respx.get(f"{API}/requestdl").mock(
            return_value=httpx.Response(200, json={"success": True, "data": "https://cdn/one.mp3"})
        )
        url = asyncio.run(torbox.request_link(KEY, 42, file_id=3))

    assert url == "https://cdn/one.mp3"
    params = route.calls[0].request.url.params
    assert params["file_id"] == "3"
    assert "zip_link" not in params


def test_delete_torrent_posts_the_controltorrent_operation():
    with respx.mock:
        route = respx.post(f"{API}/controltorrent").mock(
            return_value=httpx.Response(200, json={"success": True, "data": {}})
        )
        asyncio.run(torbox.delete_torrent(KEY, 42))

    request = route.calls[0].request
    assert json.loads(request.content) == {"torrent_id": 42, "operation": "delete"}
    assert request.headers["Authorization"] == f"Bearer {KEY}"


def test_failed_envelope_uses_the_detail_line():
    with respx.mock:
        respx.get(f"{API}/mylist").mock(
            return_value=httpx.Response(
                200, json={"success": False, "error": "BAD_TOKEN", "detail": "API key is invalid"}
            )
        )
        with pytest.raises(torbox.TorboxError, match="API key is invalid"):
            asyncio.run(torbox.find_many(KEY, {1}))


def test_non_json_response_reports_the_status():
    with respx.mock:
        respx.get(f"{API}/mylist").mock(return_value=httpx.Response(502, text="bad gateway"))
        with pytest.raises(torbox.TorboxError, match="HTTP 502"):
            asyncio.run(torbox.find_many(KEY, {1}))


def test_transport_errors_are_wrapped():
    with respx.mock:
        respx.get(f"{API}/mylist").mock(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(torbox.TorboxError, match="couldn't reach TorBox"):
            asyncio.run(torbox.find_many(KEY, {1}))


def test_missing_link_in_the_response_is_an_error():
    with respx.mock:
        respx.get(f"{API}/requestdl").mock(
            return_value=httpx.Response(200, json={"success": True, "data": {"nope": 1}})
        )
        with pytest.raises(torbox.TorboxError, match="download link"):
            asyncio.run(torbox.request_link(KEY, 42, file_id=1))
