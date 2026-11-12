"""Tests for link parsing, magnet building, and scraping a saved ABB page."""

import asyncio
import pathlib
import urllib.parse

import httpx
import pytest
import respx

from signalbooks import abb

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "abb_detail.html"
HASH = "0123456789abcdef0123456789abcdef01234567"


def test_parse_slug_form():
    url = "https://audiobookbay.lu/abss/test-book-ada-example/"
    assert abb.parse_url(url) == url


def test_parse_audio_books_form_is_canonicalised():
    assert (
        abb.parse_url("http://www.audiobookbay.lu/audio-books/test-book-ada-example")
        == "https://audiobookbay.lu/abss/test-book-ada-example/"
    )


def test_parse_post_id_form_is_rejected():
    # The ?p=<id> form is dead on .lu (empty 301), so it fails at parse time
    # instead of surfacing as a confusing fetch error.
    with pytest.raises(abb.AbbError):
        abb.parse_url("https://audiobookbay.lu/?p=100000")


@pytest.mark.parametrize(
    "url",
    [
        "https://audiobookbay.lu.evil.com/abss/test-book-ada-example/",
        "https://evil.com/abss/test-book-ada-example/",
        "https://audiobookbay.lu/audio-books/tag/dutch/",
        "https://audiobookbay.lu/",
        "https://audiobookbay.lu/?s=test-book",
        "ftp://audiobookbay.lu/abss/test-book-ada-example/",
        "test-book-ada-example",
        "https://audiobookbay.lu/abss/../../etc/passwd",
    ],
)
def test_parse_rejects_everything_else(url):
    with pytest.raises(abb.AbbError):
        abb.parse_url(url)


def test_build_magnet_lowercases_hash_and_encodes_title():
    magnet = abb.build_magnet(HASH.upper(), 'AC/DC "Live" + Ünïcødé & more')
    parsed = urllib.parse.urlparse(magnet)
    query = urllib.parse.parse_qs(parsed.query)
    assert parsed.scheme == "magnet"
    assert query["xt"] == [f"urn:btih:{HASH}"]
    assert query["dn"] == ['AC/DC "Live" + Ünïcødé & more']


def test_build_magnet_rejects_bad_hash():
    with pytest.raises(abb.AbbError):
        abb.build_magnet("not-a-hash", "Test Book")


def test_parse_book_from_saved_page():
    book = abb.parse_book(FIXTURE.read_text(), "https://audiobookbay.lu/abss/test-book-ada-example/")
    assert book.title == "Test Book - Ada Example"
    assert book.author == "Ada Example"
    assert book.info_hash == HASH


def test_parse_book_without_info_hash_errors():
    html = '<div class="postTitle"><h1 itemprop="name">Test Book</h1></div>'
    with pytest.raises(abb.AbbError, match="info hash"):
        abb.parse_book(html)


def test_fetch_book_sends_cookie_and_parses():
    url = "https://audiobookbay.lu/abss/test-book-ada-example/"
    with respx.mock:
        route = respx.get(url).mock(
            return_value=httpx.Response(200, text=FIXTURE.read_text())
        )
        book = asyncio.run(abb.fetch_book(url, cookie="session=abc"))
    assert book.info_hash == HASH
    assert route.calls[0].request.headers["cookie"] == "session=abc"


def test_fetch_book_refuses_non_200():
    url = "https://audiobookbay.lu/abss/test-book-ada-example/"
    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(403))
        with pytest.raises(abb.AbbError, match="403"):
            asyncio.run(abb.fetch_book(url))


def test_fetch_book_does_not_follow_redirects():
    url = "https://audiobookbay.lu/abss/test-book-ada-example/"
    with respx.mock:
        respx.get(url).mock(
            return_value=httpx.Response(302, headers={"location": "https://evil.com/"})
        )
        with pytest.raises(abb.AbbError, match="302"):
            asyncio.run(abb.fetch_book(url, cookie="session=abc"))


def test_fetch_book_wraps_transport_errors():
    url = "https://audiobookbay.lu/abss/test-book-ada-example/"
    with respx.mock:
        respx.get(url).mock(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(abb.AbbError, match="ConnectError"):
            asyncio.run(abb.fetch_book(url))
