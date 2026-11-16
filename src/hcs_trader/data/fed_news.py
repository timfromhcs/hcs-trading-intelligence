"""Read-only ingestion for Federal Reserve monetary-policy press releases."""

import asyncio
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from xml.etree.ElementTree import ParseError

import httpx2
from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring

from hcs_trader.data.provider import SourceRateLimited, parse_retry_after
from hcs_trader.domain.news import NewsItem

FEED_URL = "https://www.federalreserve.gov/feeds/press_monetary.xml"
SOURCE = "federal-reserve-monetary-policy"
_ARTICLE_URL = re.compile(
    r"https://www\.federalreserve\.gov/newsevents/pressreleases/monetary[0-9]{8}[a-z]+\.htm"
)
_MAX_BYTES = 512_000


class NewsUnavailable(Exception):
    """The feed could not supply a usable batch."""


class NewsRateLimited(NewsUnavailable, SourceRateLimited):
    """The feed answered 429/503; ``retry_after`` carries the requested pause."""

    def __init__(self, retry_after: float | None) -> None:
        super().__init__("Federal Reserve feed is rate limited")
        self.retry_after = retry_after


async def fetch_fed_news(client: httpx2.AsyncClient) -> list[NewsItem]:
    try:
        async with (
            asyncio.timeout(5),
            client.stream("GET", FEED_URL, follow_redirects=False) as response,
        ):
            if response.status_code in (429, 503):
                raise NewsRateLimited(
                    parse_retry_after(response.headers.get("retry-after"))
                )
            response.raise_for_status()
            payload = bytearray()
            async for chunk in response.aiter_bytes():
                if len(payload) + len(chunk) > _MAX_BYTES:
                    raise NewsUnavailable(
                        "Federal Reserve feed exceeded the size limit"
                    )
                payload.extend(chunk)
    except (httpx2.HTTPError, TimeoutError) as error:
        raise NewsUnavailable("Federal Reserve feed request failed") from error

    now = datetime.now(UTC)
    try:
        root = fromstring(bytes(payload), forbid_dtd=True)
        channel = root.find("channel")
        if root.tag != "rss" or channel is None:
            raise NewsUnavailable("Unexpected Federal Reserve RSS envelope")
        entries = channel.findall("item")
        if len(entries) > 200:
            raise NewsUnavailable("Too many RSS items")
        result = []
        for entry in entries:
            url = (entry.findtext("link") or "").strip()
            if not _ARTICLE_URL.fullmatch(url):
                continue
            title = " ".join((entry.findtext("title") or "").split())
            if not 1 <= len(title) <= 512:
                continue
            try:
                published_at = parsedate_to_datetime(entry.findtext("pubDate") or "")
            except (ValueError, OverflowError):
                continue
            if published_at.utcoffset() is None or published_at > now:
                continue
            result.append(
                NewsItem(SOURCE, url, title, published_at.astimezone(UTC), now)
            )
        if entries and not result:
            raise NewsUnavailable("Federal Reserve feed contained no valid entries")
        return result
    except (ParseError, DefusedXmlException, ValueError) as error:
        raise NewsUnavailable("Invalid Federal Reserve RSS document") from error
