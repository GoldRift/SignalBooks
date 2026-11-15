"""AudiobookBay link validation and detail-page scraping.

Selectors live here and nowhere else: ABB's DOM is the fragile part, so a break
should be a one-file fix plus a fixture test.
"""

import re
from dataclasses import dataclass
from urllib.parse import quote, urlparse

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser

BASE = "https://audiobookbay.lu"
ABB_HOSTS = frozenset({"audiobookbay.lu", "www.audiobookbay.lu"})
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_SLUG = re.compile(r"/(?:abss|audio-books)/([a-z0-9][a-z0-9-]*)/?")
_HEX40 = re.compile(r"[0-9a-fA-F]{40}")


class AbbError(Exception):
    """Expected failure: bad link, fetch problem, or unparseable page."""


@dataclass(frozen=True)
class Book:
    url: str
    title: str
    author: str
    info_hash: str


def parse_url(text: str) -> str:
    """Validate an ABB link and return the canonical URL to fetch.

    Only allowlisted hosts pass, so this is the SSRF guard: nothing is fetched
    before it succeeds.
    """
    parts = urlparse(text.strip())
    if parts.hostname not in ABB_HOSTS:
        raise AbbError(
            "that isn't an AudiobookBay link. Send /abb with the book's page link"
        )
    if parts.scheme not in ("http", "https"):
        raise AbbError("that link needs to start with http:// or https://")
    slug = _SLUG.fullmatch(parts.path or "")
    if slug:
        return f"{BASE}/abss/{slug.group(1)}/"
    raise AbbError(
        "that AudiobookBay link isn't a book page. Send /abb with the book's page link"
    )


def build_magnet(info_hash: str, title: str) -> str:
    if not _HEX40.fullmatch(info_hash):
        raise AbbError("missing or malformed info hash")
    return f"magnet:?xt=urn:btih:{info_hash.lower()}&dn={quote(title)}"


def parse_book(html: str, url: str = "") -> Book:
    tree = HTMLParser(html)
    title = _text(tree, "h1[itemprop=name]") or _text(tree, "div.postTitle h1")
    if not title:
        raise AbbError("couldn't read the book's title from that AudiobookBay page. Try another link")
    authors = [n.text(strip=True) for n in tree.css("span.author") if n.text(strip=True)]
    info_hash = _info_hash(tree)
    if not info_hash:
        raise AbbError("couldn't find the info hash on that AudiobookBay page. Try another link")
    return Book(url=url, title=title, author=", ".join(authors), info_hash=info_hash)


def _text(tree: HTMLParser, selector: str) -> str:
    node = tree.css_first(selector)
    return " ".join(node.text().split()) if node else ""


def _info_hash(tree: HTMLParser) -> str:
    for cell in tree.css("td"):
        if cell.text(strip=True).rstrip(":") == "Info Hash":
            row = cell.parent
            cells = row.css("td") if row else []
            if len(cells) > 1 and _HEX40.fullmatch(cells[1].text(strip=True)):
                return cells[1].text(strip=True).lower()
    return ""


async def fetch_book(url: str, cookie: str | None = None) -> Book:
    """Fetch and parse one ABB detail page. Redirects are refused, not followed:
    a redirect would otherwise carry the session cookie to another host."""
    canonical = parse_url(url)
    headers = {"User-Agent": UA}
    if cookie:
        headers["Cookie"] = cookie
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        try:
            response = await client.get(canonical, headers=headers)
        except httpx.HTTPError as exc:
            raise AbbError(
                "couldn't reach AudiobookBay. Try again in a minute"
            ) from exc
    if response.status_code != 200:
        raise AbbError(
            f"AudiobookBay returned HTTP {response.status_code}. Try again later"
        )
    return parse_book(response.text, canonical)
