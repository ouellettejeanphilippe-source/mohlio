#!/usr/bin/env python3
"""Build RSS podcast feeds for a list of Radio-Canada OHdio shows.

How the feeds are built
-----------------------
Three sources are combined for every show. None of them is complete on its
own, so they are merged instead of replacing each other:

1. ``feed_<id>.xml`` already in the repository. Upstream only ever exposes a
   rolling window of recent episodes (and, for some shows, a single stale
   one), so the file on disk is the only place the full history lives. It is
   never truncated by a failed or partial upstream answer, and it doubles as
   the cache of media URLs that were already resolved.
2. The OHdio show page (``window._rcState_``). This is the source that sees a
   new episode first, usually minutes after it is published, but it only
   exposes a media id that has to be resolved to an HLS URL.
3. The podcast RSS document served by the GraphQL gateway. It lags behind the
   page, but it carries progressive MP3 URLs, real file sizes and the channel
   metadata, so it is used to upgrade episodes that the page published first.

Guarantees the merge is designed to keep
----------------------------------------
* An episode keeps the same ``<guid>`` for its whole life, even when its
  enclosure is upgraded from HLS to MP3. Subscribers never see a duplicate.
* A feed never loses episodes because a source was slow, partial or down.
* Files are written atomically and only when their content really changed, so
  a crash cannot truncate a feed and an unchanged run produces no commit.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import html
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
from typing import Iterable, Iterator, Sequence
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOG = logging.getLogger("mohlio")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GRAPHQL_URL = "https://services.radio-canada.ca/bff/audio/graphql"
VALIDATION_URL = "https://services.radio-canada.ca/media/validation/v2/"
OHDIO_ROOT = "https://ici.radio-canada.ca/ohdio"
PUBLIC_BASE_URL = "https://ouellettejeanphilippe-source.github.io/mohlio"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://ici.radio-canada.ca/",
    "Origin": "https://ici.radio-canada.ca",
    "Accept-Language": "fr-CA,fr;q=0.9",
}

# (connect, read) timeouts. Every request in this file uses them: a hung
# socket must never be able to hold the whole run hostage.
TIMEOUT = (10, 25)

# Upper bound on the number of episodes kept per feed.
MAX_ITEMS = 500

# Hint for podcast clients, in minutes. The feeds are refreshed far more
# often than that, so there is no point advertising a long TTL.
FEED_TTL_MINUTES = 15

# Used to estimate <enclosure length> when the real size is unknown
# (~128 kbit/s). Better than advertising a constant fake size.
ESTIMATED_BYTES_PER_SECOND = 16_000

EASTERN = ZoneInfo("America/Toronto")

FEED_LANGUAGE = "fr-ca"

NS = {
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "atom": "http://www.w3.org/2005/Atom",
}
for _prefix, _uri in NS.items():
    ET.register_namespace(_prefix, _uri)


@dataclass(frozen=True)
class Show:
    """One OHdio show and the feed it is published as."""

    id: int
    slug: str
    title: str

    @property
    def filename(self) -> str:
        return f"feed_{self.id}.xml"

    @property
    def feed_url(self) -> str:
        return f"{PUBLIC_BASE_URL}/{self.filename}"


SHOWS: tuple[Show, ...] = (
    Show(6108, "explique", "Ça s'explique"),
    Show(9887, "journee", "La journée (est encore jeune)"),
    Show(11099, "decrypteurs", "Décrypteurs : le balado"),
    Show(6327, "betisier", "Le bêtisier"),
    Show(12095, "niquet", "Olivier Niquet 24/7 (en jaquette)"),
    Show(302, "une", "À la une"),
    Show(6056, "recherche", "Moteur de recherche"),
    Show(7791, "question", "Pouvez-vous répéter la question?"),
    Show(6104, "hockey", "Tellement hockey"),
    Show(13061, "changement", "Changement de ligne"),
)

SHOWS_BY_ID = {show.id: show for show in SHOWS}

# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------

_thread_local = threading.local()


def _build_session() -> requests.Session:
    """A session that retries the failures that are worth retrying.

    The previous version only retried connection errors, which let a single
    HTTP 502 or a read timeout drop a whole show from the feeds.
    """
    retry_kwargs = dict(
        total=4,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.8,
        status_forcelist=(408, 425, 429, 500, 502, 503, 504),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    try:
        retry = Retry(allowed_methods=frozenset({"GET", "POST"}), **retry_kwargs)
    except TypeError:  # urllib3 < 1.26
        retry = Retry(method_whitelist=frozenset({"GET", "POST"}), **retry_kwargs)

    session = requests.Session()
    session.headers.update(HEADERS)
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def session() -> requests.Session:
    """One session per thread: requests' sessions are not thread safe."""
    existing = getattr(_thread_local, "session", None)
    if existing is None:
        existing = _build_session()
        _thread_local.session = existing
    return existing


class RateLimiter:
    """Keeps a minimum delay between calls, shared across worker threads."""

    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._next_allowed - now
            if delay > 0:
                time.sleep(delay)
                now = time.monotonic()
            self._next_allowed = now + self._min_interval


# The media validation endpoint is the one that answers 429 under load, and
# it is only called for episodes that are not in the feed yet.
MEDIA_LIMITER = RateLimiter(0.35)


def graphql(query: str, variables: dict) -> dict:
    """Run a GraphQL query and return ``data``; raise on transport errors."""
    response = session().post(
        GRAPHQL_URL,
        json={"query": query, "variables": variables},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    for error in payload.get("errors") or ():
        LOG.debug("GraphQL error: %s", error.get("message"))
    return payload.get("data") or {}


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_WEEKDAY = "lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche"
_MONTH = (
    "janvier|février|mars|avril|mai|juin|juillet|août|septembre|octobre|"
    "novembre|décembre"
)
# "Vendredi 11 septembre 2026 - ", "Jeudi 10 septembre 2026 : ", ...
_DATE_PREFIX_RE = re.compile(
    rf"^(?:(?:{_WEEKDAY})\s+)?\d{{1,2}}(?:er)?\s+(?:{_MONTH})\s+\d{{4}}\s*[-–—:]\s*"
)
_EPISODE_PREFIX_RE = re.compile(
    rf"^(?:épisode|émission)\s+du\s+\d{{1,2}}(?:er)?\s+(?:{_MONTH})\s+\d{{4}}\s*[-–—:]?\s*"
)
# "2026-09-11_05_06_00" as embedded in every media file name. The MP3 and the
# HLS rendition of one broadcast share it, which makes it the strongest key
# available for matching an episode across sources.
_BROADCAST_STAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}_\d{2}_\d{2}_\d{2}")


def clean_text(value: str | None) -> str:
    """Normalise a text field coming from any of the sources.

    The podcast RSS gateway returns descriptions that are already HTML
    escaped (``&lt;p&gt;``) while the page returns raw HTML. Without this,
    RSS-sourced descriptions were escaped a second time on the way out and
    podcast apps displayed the literal markup.
    """
    if not value:
        return ""
    text = value.strip()
    if "&lt;" in text or "&amp;" in text or "&#" in text or "&nbsp;" in text:
        unescaped = html.unescape(text)
        # Only trust the unescaping when it actually yields markup or text,
        # never when it would corrupt a literal ampersand-only string.
        if unescaped:
            text = unescaped
    return text.replace(" ", " ").strip()


def normalize_title(value: str | None) -> str:
    """Key used to recognise the same episode across sources.

    The page prefixes titles with the broadcast date ("Jeudi 10 septembre
    2026 : L'entrevue des cinq chefs") while the podcast RSS does not. Not
    stripping that prefix is what filled the feeds with duplicates.
    """
    if not value:
        return ""
    text = clean_text(value)
    text = _TAG_RE.sub(" ", text)
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("’", "'").replace("ʼ", "'")
    text = text.replace("–", "-").replace("—", "-")
    text = _WS_RE.sub(" ", text).strip().casefold()
    text = _DATE_PREFIX_RE.sub("", text)
    text = _EPISODE_PREFIX_RE.sub("", text)
    return text.strip(" -:–—").strip()


def broadcast_stamp(url: str | None) -> str:
    match = _BROADCAST_STAMP_RE.search(url or "")
    return match.group(0) if match else ""


def is_progressive(url: str, mime: str = "") -> bool:
    """True for a plain downloadable file, False for an HLS playlist.

    Plenty of podcast apps cannot play ``.m3u8``, so a progressive MP3 always
    wins over the HLS rendition of the same episode.
    """
    if url.lower().split("?")[0].endswith(".m3u8"):
        return False
    if "mpegurl" in (mime or "").lower():
        return False
    return bool(url)


def parse_datetime(value: str | None) -> datetime | None:
    """Parse both RFC 2822 (podcast RSS) and ISO 8601 (page) timestamps."""
    if not value:
        return None
    text = value.strip()
    try:
        parsed = parsedate_to_datetime(text)
        if parsed is not None:
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_duration(value) -> int | None:
    """Accept seconds, "MM:SS" and "HH:MM:SS"."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value) or None
    text = str(value).strip()
    if text.isdigit():
        return int(text) or None
    parts = text.split(":")
    if not all(part.strip().isdigit() for part in parts if part != ""):
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part or 0)
    return seconds or None


def format_duration(seconds: int | None) -> str:
    if not seconds or seconds < 0:
        return ""
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


# ---------------------------------------------------------------------------
# Episode model and merging
# ---------------------------------------------------------------------------

# Increasing order of authority. The page publishes first, the podcast RSS
# publishes better (real MP3, real byte size, clean metadata).
ORIGIN_RANK = {"existing": 0, "page": 1, "rss": 2}


@dataclass
class Episode:
    guid: str = ""
    title: str = ""
    description: str = ""
    published: datetime | None = None
    duration: int | None = None
    url: str = ""
    length: int = 0
    mime: str = "audio/mpeg"
    link: str = ""
    media_id: str = ""
    origin: str = "existing"

    def aliases(self) -> Iterator[str]:
        """Keys under which this episode can be recognised again."""
        if self.media_id:
            yield f"media:{self.media_id}"
        stamp = broadcast_stamp(self.url)
        if stamp:
            yield f"stamp:{stamp}"
        if self.url:
            yield f"url:{self.url.split('?')[0]}"
        if self.published:
            # Two episodes of one show never share a broadcast instant, and
            # the page exposes it before the media id has been resolved.
            # This is what lets a steady-state run recognise every episode
            # without spending a single media-validation call.
            yield f"time:{int(self.published.timestamp())}"
        title = normalize_title(self.title)
        if title:
            yield f"title:{title}"

    @property
    def enclosure_length(self) -> int:
        if self.length > 0:
            return self.length
        if self.duration:
            return self.duration * ESTIMATED_BYTES_PER_SECOND
        return 0


def _dates_are_close(left: Episode, right: Episode, days: int = 2) -> bool:
    """Guard against merging two same-titled episodes years apart."""
    if left.published is None or right.published is None:
        return True
    return abs(left.published - right.published) <= timedelta(days=days)


def _merge_into(target: Episode, incoming: Episode) -> None:
    """Fold ``incoming`` into ``target``, keeping the identity of ``target``."""
    at_least_as_authoritative = (
        ORIGIN_RANK.get(incoming.origin, 0) >= ORIGIN_RANK.get(target.origin, 0)
    )

    if incoming.title and (at_least_as_authoritative or not target.title):
        target.title = incoming.title
    if incoming.description and (at_least_as_authoritative or not target.description):
        target.description = incoming.description
    if incoming.published and (at_least_as_authoritative or target.published is None):
        target.published = incoming.published
    if incoming.duration and (at_least_as_authoritative or not target.duration):
        target.duration = incoming.duration
    if incoming.link and (at_least_as_authoritative or not target.link):
        target.link = incoming.link
    if incoming.media_id and not target.media_id:
        target.media_id = incoming.media_id

    if incoming.url and _prefer_enclosure(incoming, target):
        target.url = incoming.url
        target.mime = incoming.mime or target.mime
        target.length = incoming.length or 0
    elif incoming.url == target.url and incoming.length > 0:
        target.length = incoming.length

    # The GUID is what subscribers' apps key on: it is set once and kept.
    if not target.guid and incoming.guid:
        target.guid = incoming.guid

    if ORIGIN_RANK.get(incoming.origin, 0) > ORIGIN_RANK.get(target.origin, 0):
        target.origin = incoming.origin


def _prefer_enclosure(incoming: Episode, target: Episode) -> bool:
    if not target.url:
        return True
    if incoming.url == target.url:
        return False
    incoming_progressive = is_progressive(incoming.url, incoming.mime)
    target_progressive = is_progressive(target.url, target.mime)
    if incoming_progressive != target_progressive:
        return incoming_progressive
    return ORIGIN_RANK.get(incoming.origin, 0) > ORIGIN_RANK.get(target.origin, 0)


class EpisodeIndex:
    """Collects episodes coming from several sources, without duplicates."""

    def __init__(self) -> None:
        self._episodes: list[Episode] = []
        self._by_alias: dict[str, Episode] = {}

    def __len__(self) -> int:
        return len(self._episodes)

    def find(self, candidate: Episode) -> Episode | None:
        weak: Episode | None = None
        for alias in candidate.aliases():
            known = self._by_alias.get(alias)
            if known is None or known is candidate:
                continue
            if alias.startswith("title:"):
                if weak is None and _dates_are_close(candidate, known):
                    weak = known
                continue
            return known
        return weak

    def add(self, candidate: Episode) -> Episode:
        known = self.find(candidate)
        if known is None:
            self._episodes.append(candidate)
            self._register(candidate)
            return candidate
        _merge_into(known, candidate)
        self._register(known)
        return known

    def _register(self, episode: Episode) -> None:
        for alias in episode.aliases():
            self._by_alias.setdefault(alias, episode)

    def sorted_episodes(self) -> list[Episode]:
        """Newest first; episodes without a date keep a stable position."""
        oldest = datetime(1970, 1, 1, tzinfo=timezone.utc)
        return sorted(
            self._episodes,
            key=lambda ep: (ep.published or oldest),
            reverse=True,
        )


# ---------------------------------------------------------------------------
# Source 1 - the feed already on disk
# ---------------------------------------------------------------------------


@dataclass
class ChannelMeta:
    title: str = ""
    description: str = ""
    link: str = ""
    image: str = ""
    language: str = FEED_LANGUAGE
    author: str = ""
    copyright: str = ""
    explicit: str = "no"

    def merge(self, other: "ChannelMeta") -> None:
        for field_name in (
            "title",
            "description",
            "link",
            "image",
            "language",
            "author",
            "copyright",
            "explicit",
        ):
            value = getattr(other, field_name)
            if value:
                setattr(self, field_name, value)


def _text(element: ET.Element | None) -> str:
    return (element.text or "").strip() if element is not None else ""


def read_existing_feed(path: str) -> tuple[ChannelMeta, list[Episode]]:
    """Read a previously generated feed. Never raises: a damaged file simply
    means we rebuild from the live sources."""
    meta = ChannelMeta()
    episodes: list[Episode] = []
    if not os.path.exists(path):
        return meta, episodes
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        LOG.warning("%s is unreadable (%s); rebuilding it from scratch", path, exc)
        return meta, episodes

    channel = root.find("channel")
    if channel is None:
        return meta, episodes

    meta.title = _text(channel.find("title"))
    meta.description = _text(channel.find("description"))
    meta.language = _text(channel.find("language")) or FEED_LANGUAGE
    meta.author = _text(channel.find(f"{{{NS['itunes']}}}author"))
    meta.copyright = _text(channel.find("copyright"))
    explicit = _text(channel.find(f"{{{NS['itunes']}}}explicit"))
    if explicit:
        meta.explicit = explicit
    image = channel.find("image")
    if image is not None:
        meta.image = _text(image.find("url"))
    if not meta.image:
        itunes_image = channel.find(f"{{{NS['itunes']}}}image")
        if itunes_image is not None:
            meta.image = (itunes_image.get("href") or "").strip()

    for item in channel.findall("item"):
        enclosure = item.find("enclosure")
        url = (enclosure.get("url") or "").strip() if enclosure is not None else ""
        if not url:
            continue
        try:
            length = int((enclosure.get("length") or "0").strip())
        except ValueError:
            length = 0
        # The old generator wrote a constant placeholder size for every
        # episode; do not carry that lie forward.
        if length in (0, 100_000_000):
            length = 0
        guid_element = item.find("guid")
        episodes.append(
            Episode(
                guid=_text(guid_element) or url,
                title=_text(item.find("title")),
                description=_text(item.find("description")),
                published=parse_datetime(_text(item.find("pubDate"))),
                duration=parse_duration(
                    _text(item.find(f"{{{NS['itunes']}}}duration"))
                ),
                url=url,
                length=length,
                mime=(enclosure.get("type") or "audio/mpeg").strip(),
                link=_text(item.find("link")),
                origin="existing",
            )
        )

    # Feed order is rebuilt from dates later on, so we are free to insert the
    # progressive copies first: when an old duplicate pair collapses, the
    # surviving GUID is then the one that matches the URL we keep publishing.
    episodes.sort(key=lambda ep: 0 if is_progressive(ep.url, ep.mime) else 1)
    return meta, episodes


# ---------------------------------------------------------------------------
# Source 2 - the OHdio show page
# ---------------------------------------------------------------------------

_STATE_RE = re.compile(r"window\._rcState_\s*=\s*(.*?);\s*</script>", re.DOTALL)

PROGRAMME_QUERY = """
query GetProgramme($params: ProgrammeByIdInput!) {
  programmeById(params: $params) {
    ... on EmissionBalado { canonicalUrl }
    ... on EmissionPremiere { canonicalUrl }
    ... on EmissionMusique { canonicalUrl }
    ... on EmissionGrandesSeries { canonicalUrl }
  }
}
"""


def fetch_canonical_url(show: Show) -> str:
    try:
        data = graphql(PROGRAMME_QUERY, {"params": {"id": show.id, "forceWithoutCueSheet": False}})
        canonical = ((data.get("programmeById") or {}).get("canonicalUrl") or "").strip()
    except (requests.RequestException, ValueError) as exc:
        LOG.debug("[%s] canonical URL lookup failed: %s", show.id, exc)
        canonical = ""
    if canonical:
        return f"{OHDIO_ROOT}{canonical}"
    return f"{OHDIO_ROOT}/balados/{show.id}"


def _iter_media_objects(node, depth: int = 0) -> Iterator[dict]:
    """Walk the page state looking for episode entries."""
    if depth > 40:
        return
    if isinstance(node, dict):
        if node.get("mediaIds"):
            yield node
        for value in node.values():
            yield from _iter_media_objects(value, depth + 1)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_media_objects(value, depth + 1)


def resolve_media_url(media_id: str) -> tuple[str, str]:
    """Resolve a media id to a playable URL. Returns ("", "") on failure."""
    MEDIA_LIMITER.wait()
    params = {
        "appCode": "medianet",
        "deviceType": "ipad",
        "connectionType": "wifi",
        "idMedia": media_id,
        "output": "json",
    }
    try:
        response = session().get(VALIDATION_URL, params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        LOG.debug("media %s: %s", media_id, exc)
        return "", ""
    if response.status_code != 200:
        LOG.debug("media %s: HTTP %s", media_id, response.status_code)
        return "", ""
    try:
        payload = response.json()
    except ValueError:
        LOG.debug("media %s: invalid JSON", media_id)
        return "", ""
    url = (payload.get("url") or "").strip()
    if not url:
        LOG.debug("media %s: %s", media_id, payload.get("message") or "no url")
        return "", ""
    mime = "application/x-mpegURL" if not is_progressive(url) else "audio/mpeg"
    for param in payload.get("params") or ():
        if param.get("name") == "contentType" and param.get("value"):
            value = str(param["value"])
            mime = "application/x-mpegURL" if "mpegURL" in value else "audio/mpeg"
    return url, mime


def fetch_page_episodes(show: Show, page_url: str) -> tuple[list[Episode], str]:
    """Episodes advertised on the show page, plus the page artwork.

    Media ids are *not* resolved here: that is done by the caller, and only
    for the episodes that are not in the feed yet. A steady-state run
    therefore performs no call at all to the media validation endpoint.
    """
    response = session().get(page_url, timeout=TIMEOUT)
    response.raise_for_status()
    body = response.text

    image = ""
    match = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', body)
    if not match:
        match = re.search(r'content="([^"]+)"\s+property="og:image"', body)
    if match:
        image = match.group(1)

    state_match = _STATE_RE.search(body)
    if not state_match:
        raise ValueError("episode data not found in page HTML")
    state = json.loads(state_match.group(1))

    episodes: list[Episode] = []
    seen_media: set[str] = set()
    for node in _iter_media_objects(state):
        media_ids = node.get("mediaIds") or []
        if not media_ids:
            continue
        media_id = str(media_ids[0])
        if media_id in seen_media:
            continue
        seen_media.add(media_id)

        duration = node.get("duration")
        if isinstance(duration, dict):
            duration = duration.get("durationInSeconds")
        link = (node.get("url") or "").strip()
        episodes.append(
            Episode(
                title=clean_text(node.get("title")),
                description=clean_text(node.get("summary") or node.get("description")),
                published=parse_datetime(
                    node.get("broadcastedFirstTimeAt")
                    or node.get("publishedAt")
                    or node.get("updatedAt")
                ),
                duration=parse_duration(duration),
                link=f"{OHDIO_ROOT}{link}" if link.startswith("/") else link,
                media_id=media_id,
                origin="page",
            )
        )
    return episodes, image


# ---------------------------------------------------------------------------
# Source 3 - the podcast RSS document
# ---------------------------------------------------------------------------

PODCAST_QUERY = """
query GetShowEpisodes($params: PodcastByProgrammeIdInput!) {
  podcastByProgrammeId(params: $params) {
    ... on PodcastRss {
      channel {
        title
        description
        link
        language
        copyright
        itunesAuthor
        itunesExplicit
        image { url }
        items {
          title
          description
          pubDate
          enclosure { url length type }
          itunesDuration
        }
      }
    }
  }
}
"""


def fetch_podcast_rss(show: Show) -> tuple[ChannelMeta, list[Episode]]:
    data = graphql(PODCAST_QUERY, {"params": {"programmeId": show.id, "withAds": False}})
    channel = (data.get("podcastByProgrammeId") or {}).get("channel")
    if not channel:
        raise ValueError("podcast RSS returned no channel")

    meta = ChannelMeta(
        title=clean_text(channel.get("title")),
        description=clean_text(channel.get("description")),
        link=(channel.get("link") or "").strip(),
        image=((channel.get("image") or {}).get("url") or "").strip(),
        language=(channel.get("language") or "").strip() or FEED_LANGUAGE,
        author=clean_text(channel.get("itunesAuthor")),
        copyright=clean_text(channel.get("copyright")),
        explicit=(channel.get("itunesExplicit") or "no").strip() or "no",
    )

    episodes: list[Episode] = []
    for item in channel.get("items") or ():
        enclosure = item.get("enclosure") or {}
        url = (enclosure.get("url") or "").strip()
        if not url:
            continue
        try:
            length = int(enclosure.get("length") or 0)
        except (TypeError, ValueError):
            length = 0
        episodes.append(
            Episode(
                title=clean_text(item.get("title")),
                description=clean_text(item.get("description")),
                published=parse_datetime(item.get("pubDate")),
                duration=parse_duration(item.get("itunesDuration")),
                url=url,
                length=max(length, 0),
                mime=(enclosure.get("type") or "audio/mpeg").strip(),
                origin="rss",
            )
        )
    return meta, episodes


# ---------------------------------------------------------------------------
# Feed rendering
# ---------------------------------------------------------------------------


def _sub(parent: ET.Element, tag: str, text: str = "") -> ET.Element:
    element = ET.SubElement(parent, tag)
    if text:
        element.text = text
    return element


def build_feed_xml(show: Show, meta: ChannelMeta, episodes: Sequence[Episode]) -> str:
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")

    title = meta.title or show.title
    _sub(channel, "title", title)
    _sub(channel, "description", meta.description or title)
    _sub(channel, "link", meta.link or show.feed_url)
    _sub(channel, "language", meta.language or FEED_LANGUAGE)
    if meta.copyright:
        _sub(channel, "copyright", meta.copyright)
    _sub(channel, "generator", "mohlio")
    _sub(channel, "ttl", str(FEED_TTL_MINUTES))

    # Pointing at the feed's own address is what lets clients and validators
    # follow it; it is also a plain RSS best practice.
    ET.SubElement(
        channel,
        f"{{{NS['atom']}}}link",
        {"href": show.feed_url, "rel": "self", "type": "application/rss+xml"},
    )

    # Derived from the newest episode rather than from "now", so that a run
    # that changes nothing produces a byte-identical file and no commit.
    newest = next((ep.published for ep in episodes if ep.published), None)
    if newest:
        _sub(channel, "pubDate", format_datetime(newest))
        _sub(channel, "lastBuildDate", format_datetime(newest))

    if meta.image:
        image = ET.SubElement(channel, "image")
        _sub(image, "url", meta.image)
        _sub(image, "title", title)
        _sub(image, "link", meta.link or show.feed_url)
        ET.SubElement(channel, f"{{{NS['itunes']}}}image", {"href": meta.image})

    if meta.author:
        _sub(channel, f"{{{NS['itunes']}}}author", meta.author)
    _sub(channel, f"{{{NS['itunes']}}}explicit", meta.explicit or "no")
    _sub(channel, f"{{{NS['itunes']}}}summary", meta.description or title)

    for episode in episodes:
        item = ET.SubElement(channel, "item")
        _sub(item, "title", episode.title)
        _sub(item, "description", episode.description)
        if episode.link:
            _sub(item, "link", episode.link)
        if episode.published:
            _sub(item, "pubDate", format_datetime(episode.published))
        ET.SubElement(
            item,
            "enclosure",
            {
                "url": episode.url,
                "length": str(episode.enclosure_length),
                "type": episode.mime or "audio/mpeg",
            },
        )
        guid = _sub(item, "guid", episode.guid or episode.url)
        guid.set("isPermaLink", "false")
        duration = format_duration(episode.duration)
        if duration:
            _sub(item, f"{{{NS['itunes']}}}duration", duration)

    ET.indent(rss, space="  ")
    body = ET.tostring(rss, encoding="unicode")
    return '<?xml version="1.0" encoding="utf-8"?>\n' + body + "\n"


def write_if_changed(path: str, content: str) -> bool:
    """Atomically replace ``path``; return True when the bytes changed.

    Writing through a temporary file in the same directory means an
    interrupted run can never leave a half-written feed behind.
    """
    payload = content.encode("utf-8")
    try:
        with open(path, "rb") as handle:
            if handle.read() == payload:
                return False
    except FileNotFoundError:
        pass

    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".feed-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return True


# ---------------------------------------------------------------------------
# Per-show pipeline
# ---------------------------------------------------------------------------


@dataclass
class ShowResult:
    show: Show
    changed: bool = False
    written: bool = False
    episode_count: int = 0
    new_episodes: int = 0
    newest: datetime | None = None
    warnings: list[str] = None
    error: str = ""

    def __post_init__(self) -> None:
        if self.warnings is None:
            self.warnings = []


def process_show(show: Show, out_dir: str, dry_run: bool = False) -> ShowResult:
    result = ShowResult(show=show)
    path = os.path.join(out_dir, show.filename)

    meta, existing = read_existing_feed(path)
    index = EpisodeIndex()
    for episode in existing:
        index.add(episode)
    known_before = len(index)
    LOG.info("[%s] %s: %d episode(s) on disk", show.id, show.title, known_before)

    # --- Source 3: podcast RSS (authoritative metadata, MP3 enclosures) ---
    page_link = ""
    try:
        rss_meta, rss_episodes = fetch_podcast_rss(show)
        meta.merge(rss_meta)
        page_link = rss_meta.link
        for episode in rss_episodes:
            index.add(episode)
        LOG.info("[%s] podcast RSS: %d episode(s)", show.id, len(rss_episodes))
    except (requests.RequestException, ValueError, KeyError) as exc:
        result.warnings.append(f"podcast RSS unavailable ({exc})")
        LOG.warning("[%s] podcast RSS unavailable: %s", show.id, exc)

    # --- Source 2: show page (fastest to publish a new episode) ---
    try:
        page_url = page_link or fetch_canonical_url(show)
        page_episodes, page_image = fetch_page_episodes(show, page_url)
        if page_image and not meta.image:
            meta.image = page_image
        if not meta.link:
            meta.link = page_url

        pending: list[Episode] = []
        for episode in page_episodes:
            known = index.find(episode)
            if known is not None:
                # Already published: refresh its metadata, but do not spend a
                # media-validation call on a URL we already have.
                index.add(episode)
            else:
                pending.append(episode)

        for episode in pending:
            url, mime = resolve_media_url(episode.media_id)
            if not url:
                result.warnings.append(f"media {episode.media_id} could not be resolved")
                continue
            episode.url = url
            episode.mime = mime
            episode.guid = f"{url}?v=2"
            index.add(episode)
            result.new_episodes += 1
        LOG.info(
            "[%s] page: %d episode(s), %d new",
            show.id,
            len(page_episodes),
            result.new_episodes,
        )
    except (requests.RequestException, ValueError, KeyError, json.JSONDecodeError) as exc:
        result.warnings.append(f"show page unavailable ({exc})")
        LOG.warning("[%s] show page unavailable: %s", show.id, exc)

    episodes = index.sorted_episodes()
    for episode in episodes:
        if not episode.guid:
            episode.guid = f"{episode.url}?v=2"
    if len(episodes) > MAX_ITEMS:
        episodes = episodes[:MAX_ITEMS]

    if not episodes:
        result.error = "no episode available from any source"
        LOG.error("[%s] %s", show.id, result.error)
        return result

    # Safety net: every source can fail at once, and an empty or truncated
    # answer must never be allowed to shrink a feed that was fine before.
    if known_before and len(episodes) < known_before * 0.5:
        result.error = (
            f"refusing to shrink the feed from {known_before} to {len(episodes)} episodes"
        )
        LOG.error("[%s] %s", show.id, result.error)
        return result

    if not meta.title:
        meta.title = show.title
    if not meta.description:
        meta.description = show.title

    result.episode_count = len(episodes)
    result.newest = next((ep.published for ep in episodes if ep.published), None)

    content = build_feed_xml(show, meta, episodes)
    # Parsing our own output before publishing it keeps a malformed feed from
    # ever reaching subscribers.
    ET.fromstring(content)

    if dry_run:
        result.changed = content.encode("utf-8") != _read_bytes(path)
        LOG.info("[%s] dry run: %d episode(s)", show.id, len(episodes))
        return result

    result.written = write_if_changed(path, content)
    result.changed = result.written
    LOG.info(
        "[%s] %s: %d episode(s)%s",
        show.id,
        show.filename,
        len(episodes),
        " (updated)" if result.written else " (unchanged)",
    )
    return result


def _read_bytes(path: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return b""


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

README_START = "<!-- RUN_LOG_START -->"
README_END = "<!-- RUN_LOG_END -->"


def build_readme_log(results: Sequence[ShowResult]) -> str:
    lines = ["\n### Feeds\n"]
    for result in sorted(results, key=lambda item: item.show.slug):
        show = result.show
        if result.error:
            lines.append(f"- ❌ **{show.slug}** — {result.error}")
            continue
        newest = (
            result.newest.astimezone(EASTERN).strftime("%Y-%m-%d %H:%M ET")
            if result.newest
            else "unknown"
        )
        marker = "🆕" if result.changed else "✅"
        lines.append(
            f"- {marker} [{show.slug}]({show.feed_url}) — "
            f"{result.episode_count} episodes, latest {newest}"
        )
    warnings = [
        f"- `{result.show.slug}`: {warning}"
        for result in sorted(results, key=lambda item: item.show.slug)
        for warning in result.warnings
    ]
    if warnings:
        lines.append("\n### Warnings\n")
        lines.extend(warnings)
    return "\n".join(lines) + "\n"


def update_readme_log(results: Sequence[ShowResult], readme_path: str = "README.md") -> bool:
    """Refresh the run log, but only when it would actually say something new.

    The log used to carry a "last run" timestamp that changed on every run,
    which produced a commit even when no feed had moved.
    """
    try:
        with open(readme_path, "r", encoding="utf-8") as handle:
            content = handle.read()
    except OSError as exc:
        LOG.warning("cannot read %s: %s", readme_path, exc)
        return False

    if README_START not in content or README_END not in content:
        LOG.warning("%s has no run-log markers; leaving it alone", readme_path)
        return False

    before, rest = content.split(README_START, 1)
    _, after = rest.split(README_END, 1)

    body = build_readme_log(results)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    log = f"\nLast update: {timestamp}\n{body}"

    def strip_timestamp(text: str) -> str:
        return "\n".join(
            line for line in text.splitlines() if not line.startswith("Last update:")
        )

    if strip_timestamp(log) == strip_timestamp(rest.split(README_END, 1)[0]):
        return False

    new_content = f"{before}{README_START}{log}{README_END}{after}"
    return write_if_changed(readme_path, new_content)


def write_job_outputs(results: Sequence[ShowResult]) -> None:
    """Expose the list of updated feeds to the workflow, so the commit
    message says what moved instead of a uniform "update feeds"."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    slugs = [result.show.slug for result in results if result.changed]
    summary = ", ".join(slugs[:5])
    if len(slugs) > 5:
        summary += f" and {len(slugs) - 5} more"
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"changed={summary}\n")
            handle.write(f"changed_count={len(slugs)}\n")
    except OSError as exc:
        LOG.debug("cannot write the job outputs: %s", exc)


def write_step_summary(results: Sequence[ShowResult]) -> None:
    """Report to the GitHub Actions run summary, so a healthy run stays quiet
    in git history while still being visible in the Actions tab."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = ["## Feed update", "", "| Show | Episodes | Latest episode | Status |", "|---|---|---|---|"]
    for result in sorted(results, key=lambda item: item.show.slug):
        if result.error:
            status = f"❌ {result.error}"
        elif result.changed:
            status = "🆕 updated"
        else:
            status = "✅ unchanged"
        newest = (
            result.newest.astimezone(EASTERN).strftime("%Y-%m-%d %H:%M ET")
            if result.newest
            else "—"
        )
        lines.append(
            f"| {result.show.slug} | {result.episode_count} | {newest} | {status} |"
        )
    warnings = [
        f"- `{result.show.slug}`: {warning}"
        for result in results
        for warning in result.warnings
    ]
    if warnings:
        lines += ["", "### Warnings", ""] + warnings
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as exc:
        LOG.debug("cannot write the step summary: %s", exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--shows",
        default="",
        help="comma separated show ids or slugs (default: all of them)",
    )
    parser.add_argument("--out-dir", default=".", help="where the feeds are written")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="build and validate the feeds without writing anything",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="how many shows to process at once (default: 4)",
    )
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    return parser.parse_args(argv)


def select_shows(selector: str) -> list[Show]:
    if not selector.strip():
        return list(SHOWS)
    wanted = [part.strip() for part in selector.split(",") if part.strip()]
    by_slug = {show.slug: show for show in SHOWS}
    chosen: list[Show] = []
    for item in wanted:
        show = by_slug.get(item)
        if show is None and item.isdigit():
            show = SHOWS_BY_ID.get(int(item))
        if show is None:
            raise SystemExit(f"unknown show: {item}")
        chosen.append(show)
    return chosen


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stdout,
    )

    shows = select_shows(args.shows)
    LOG.info("Updating %d feed(s)...", len(shows))
    started = time.monotonic()

    results: list[ShowResult] = []
    workers = max(1, min(args.workers, len(shows)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(process_show, show, args.out_dir, args.dry_run): show
            for show in shows
        }
        for future in concurrent.futures.as_completed(futures):
            show = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:  # one broken show must not stop the rest
                LOG.exception("[%s] unexpected failure", show.id)
                results.append(ShowResult(show=show, error=f"unexpected failure: {exc}"))

    results.sort(key=lambda item: item.show.slug)
    changed = [result for result in results if result.changed]
    failed = [result for result in results if result.error]

    if not args.dry_run and (changed or failed):
        update_readme_log(results)
    write_step_summary(results)
    write_job_outputs(results)

    LOG.info(
        "Done in %.1fs: %d feed(s) updated, %d unchanged, %d failed.",
        time.monotonic() - started,
        len(changed),
        len(results) - len(changed) - len(failed),
        len(failed),
    )
    for result in failed:
        LOG.error("[%s] %s", result.show.id, result.error)

    # A single flaky show must not turn the whole run red: the other feeds
    # were published and the failure is reported in the run summary. Only a
    # total failure is worth failing the job for.
    if failed and len(failed) == len(results):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
