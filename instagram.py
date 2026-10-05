#!/usr/bin/env python3
"""
Anonymous Instagram profile lookups. No login, no cookies, no paid API.

One rule runs through this whole file: a failed read is UNKNOWN, never a ban.
Instagram returns the exact same "not found" for an account that was banned,
deleted, deactivated, or simply renamed - so this module reports "not
reachable" and nothing stronger. The numeric user id is the only thing that
can tell a rename apart from a ban later, so it is captured whenever we see it.

Anonymous requests get throttled. Expect UNKNOWN sometimes; that is the API
being rate limited, not the account being gone, and the two must never be
confused in the channel.

Three things keep this module from hammering Instagram:
  * a short cache - a handle checked twice in a row is read once;
  * one shared gate - at most MAX_CONCURRENT requests in flight across the
    whole bot (monitor, /bancheck and /newjob alike), started at least
    MIN_GAP_SECONDS apart;
  * a cooldown - once Instagram says "slow down" (429, a login wall, a
    "please wait" reply) every read pauses for a while, doubling up to
    COOLDOWN_MAX_SECONDS, and honouring Retry-After. A cooldown is time
    limited and is never stored as an account's state.

Two ways in, each with its own cooldown:
  * "api"  - the JSON endpoint instagram.com itself uses. Complete data,
    including the numeric user id. Tried first.
  * "page" - the public profile page, instagram.com/<name>/. Only used when
    the API refuses us (401/403/429, login wall, "please wait"); Instagram
    limits it separately. Counts, name and picture come from its preview
    tags; the numeric id and private/verified status often do not.
  * "session" - the same API, logged in with the sessionid cookie of an
    Instagram account (IG_SESSIONID in .env). Optional and last: only used
    when both anonymous ways are refused, spaced SESSION_MIN_GAP_SECONDS
    apart, never sent anywhere but the profile API, never logged. If
    Instagram rejects the cookie, it is set aside for SESSION_INVALID_PAUSE
    seconds and the log says to paste a fresh one.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional
from urllib.parse import quote, urlparse

try:
    import httpx
    HTTPX_AVAILABLE = True
except Exception:  # pragma: no cover
    httpx = None  # type: ignore[assignment]
    HTTPX_AVAILABLE = False

log = logging.getLogger("zm.instagram")

PROFILE_ENDPOINT = "https://www.instagram.com/api/v1/users/web_profile_info/?username={}"
PROFILE_PAGE = "https://www.instagram.com/{}/"

# Fall back to the public profile page when the API refuses us. bot.py sets
# this from INSTAGRAM_PAGE_FALLBACK in .env.
PAGE_FALLBACK = True

PAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
PAGE_MAX_BYTES = 3_000_000

# Logged-in reads (optional). bot.py sets SESSION_ID from IG_SESSIONID.
SESSION_ID = ""
SESSION_MIN_GAP_SECONDS = 3.0      # logged-in reads are spaced further apart
SESSION_INVALID_PAUSE = 3600       # a rejected cookie is not retried for an hour
_SESSION_SHAPE = re.compile(r"[A-Za-z0-9%:._-]{10,512}")


def normalize_session_id(raw: Any) -> str:
    """Accept the cookie value as copied from a browser - with or without a
    'sessionid=' prefix, quotes or a trailing ';'. Returns '' if it cannot be
    a session id (which also keeps anything odd out of the HTTP header)."""
    value = str(raw or "").strip().strip(";").strip().strip('"').strip("'")
    if value.lower().startswith("sessionid="):
        value = value.split("=", 1)[1].strip().strip('"')
    return value if _SESSION_SHAPE.fullmatch(value) else ""

# This app id is what instagram.com itself sends for logged-out profile reads.
HEADERS = {
    "x-ig-app-id": "936619743392459",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.instagram.com/",
}

TIMEOUT = 12.0
AVATAR_MAX_BYTES = 3_000_000
CACHE_SECONDS = 120          # a handle checked twice in a row is read once
# "Not reachable" is kept for less than one monitor interval, so the Unban
# monitor really re-reads a missing account on every tick and notices the
# moment it comes back. Only a successful read is kept the full two minutes.
GONE_CACHE_SECONDS = 30
CACHE_MAX_ENTRIES = 512

MAX_CONCURRENT = 3           # requests in flight at once, bot-wide
MIN_GAP_SECONDS = 1.0        # minimum spacing between request starts
COOLDOWN_BASE_SECONDS = 30   # first pause after Instagram says "slow down"
COOLDOWN_MAX_SECONDS = 900   # longest pause, however often it repeats

# Profile pictures are only fetched from Instagram's own image hosts. The URL
# comes out of a response body, so it is never trusted to point anywhere else.
AVATAR_HOST_SUFFIXES = (".cdninstagram.com", ".fbcdn.net")

OK, GONE, UNKNOWN = "ok", "gone", "unknown"

HANDLE_PATTERN = re.compile(r"[A-Za-z0-9._]{1,30}")
_PROFILE_LINK = re.compile(
    r"(?:https?://)?(?:www\.|m\.)?instagram\.com/([A-Za-z0-9._]{1,30})/?(?:[?#].*)?",
    re.IGNORECASE,
)

NOTE_GONE = "Same answer Instagram gives for a delete, a rename or a deactivation."
NOTE_THROTTLED = "Instagram is rate limiting anonymous checks. Try again shortly."
NOTE_LOGIN = ("Instagram is asking for a login before it shows profiles to this "
              "connection. Try again later.")
NOTE_FROM_PAGE = "Read from the public profile page."
NOTE_SESSION_INVALID = ("Instagram rejected the logged-in session (IG_SESSIONID). "
                        "Paste a fresh sessionid cookie into .env and restart the bot.")
NOTE_OFFLINE = "Could not reach Instagram from this machine."
NOTE_TIMEOUT = "Instagram did not answer in time."
NOTE_MALFORMED = "Instagram sent a reply that could not be understood."
NOTE_NO_HTTPX = "httpx is not installed - run: pip install -r requirements.txt"
NOTE_INVALID = "Not a valid Instagram username."

# Injectable for tests. Drives the cache and the cooldown, nothing else.
_clock = time.monotonic


@dataclass
class Snapshot:
    """What one anonymous read of a public profile could establish."""
    username: str
    state: str = UNKNOWN
    user_id: Optional[str] = None
    full_name: Optional[str] = None
    verified: bool = False
    private: Optional[bool] = False      # None: the source did not say
    followers: Optional[int] = None
    following: Optional[int] = None
    posts: Optional[int] = None
    avatar: Optional[bytes] = field(default=None, repr=False)
    avatar_url: Optional[str] = field(default=None, repr=False)
    note: str = ""
    source: str = "api"                  # "api", "page" or "session"

    @property
    def reachable(self) -> bool:
        return self.state == OK

    @property
    def headline(self) -> str:
        """Wording that is safe to post in a channel."""
        if self.state == OK:
            return "Reachable"
        if self.state == GONE:
            return "Not reachable"
        return "Could not check"


def normalize_handle(text: Any) -> Optional[str]:
    """'@Name', 'name' or a profile link -> 'Name'. None if it cannot be a
    real Instagram username (letters, digits, '.', '_', at most 30)."""
    raw = str(text or "").strip()
    link = _PROFILE_LINK.fullmatch(raw)
    if link:
        raw = link.group(1)
    raw = raw.lstrip("@").strip()
    return raw if HANDLE_PATTERN.fullmatch(raw) else None


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

_cache: dict[str, tuple[float, Snapshot]] = {}


def _cached(username: str, want_avatar: bool) -> Optional[Snapshot]:
    hit = _cache.get(username.lower())
    if not hit:
        return None
    snapshot = hit[1]
    ttl = CACHE_SECONDS if snapshot.state == OK else GONE_CACHE_SECONDS
    if (_clock() - hit[0]) >= ttl:
        return None
    # A read made without the picture must not be handed to a caller that
    # is about to draw a card - it would show a blank face for no reason.
    if want_avatar and snapshot.state == OK and snapshot.avatar is None and snapshot.avatar_url:
        return None
    return snapshot


def _remember(snapshot: Snapshot, handle: str) -> Snapshot:
    # Never cache UNKNOWN: the next attempt must be allowed to succeed.
    if snapshot.state == UNKNOWN:
        return snapshot
    now = _clock()
    if len(_cache) >= CACHE_MAX_ENTRIES:
        for key in [k for k, (t, _) in _cache.items() if now - t >= CACHE_SECONDS]:
            _cache.pop(key, None)
        while len(_cache) >= CACHE_MAX_ENTRIES:
            _cache.pop(next(iter(_cache)))
    _cache[handle.lower()] = (now, snapshot)
    return snapshot


# --------------------------------------------------------------------------
# Rate limiting: one gate for the whole process, plus a cooldown
# --------------------------------------------------------------------------

SOURCES = ("api", "page", "session")
_cooldown: dict[str, list] = {source: [0.0, 0] for source in SOURCES}  # [until, step]


def _sources() -> tuple[str, ...]:
    usable = ["api"]
    if PAGE_FALLBACK:
        usable.append("page")
    if SESSION_ID:
        usable.append("session")
    return tuple(usable)


def cooldown_remaining(source: Optional[str] = None) -> float:
    """Seconds until Instagram may be asked again - by one source, or (no
    argument) by whichever usable source frees up first."""
    now = _clock()
    if source is not None:
        return max(0.0, _cooldown[source][0] - now)
    return min(max(0.0, _cooldown[name][0] - now) for name in _sources())


def reset_rate_limit() -> None:
    global _session_problem_logged
    for state in _cooldown.values():
        state[0], state[1] = 0.0, 0
    _session_problem_logged = False


def _note_throttle(reason: str, retry_after: Optional[float] = None,
                   source: str = "api") -> None:
    state = _cooldown[source]
    state[1] = min(state[1] + 1, 10)
    delay = min(COOLDOWN_BASE_SECONDS * 2 ** (state[1] - 1), COOLDOWN_MAX_SECONDS)
    if retry_after:
        delay = min(max(delay, retry_after), COOLDOWN_MAX_SECONDS)
    state[0] = max(state[0], _clock() + delay)
    what = {"page": "profile page", "session": "logged-in session"}.get(source, "API")
    log.warning("Instagram refused the %s (%s). Pausing %s reads for %ds.",
                what, reason, what, int(delay))


def _note_success(source: str = "api") -> None:
    # A clean answer means the next throttle starts again from the short
    # pause. A pause already running is left alone - requests that were in
    # flight together can finish in any order.
    _cooldown[source][1] = 0


def _throttled_snapshot(handle: str, source: Optional[str] = None) -> Snapshot:
    wait = int(cooldown_remaining(source))
    when = f"about {max(1, round(wait / 60))} min" if wait >= 60 else f"about {max(wait, 1)} s"
    return Snapshot(handle, UNKNOWN,
                    note=f"Instagram is refusing anonymous checks from this connection "
                         f"for now. Next try in {when}.")


class _Gate:
    """Bounded, spaced access to Instagram. Rebuilt per event loop, because
    asyncio primitives belong to the loop that first waits on them."""

    def __init__(self, gap: Any = lambda: MIN_GAP_SECONDS) -> None:
        self._gap = gap
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._sem: Optional[asyncio.Semaphore] = None
        self._spacing: Optional[asyncio.Lock] = None
        self._last = 0.0

    def _ensure(self) -> None:
        loop = asyncio.get_running_loop()
        if loop is not self._loop:
            self._loop = loop
            self._sem = asyncio.Semaphore(MAX_CONCURRENT)
            self._spacing = asyncio.Lock()
            self._last = 0.0

    @contextlib.asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        self._ensure()
        assert self._sem is not None and self._spacing is not None
        async with self._sem:
            async with self._spacing:
                wait = self._last + self._gap() - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last = time.monotonic()
            yield


_gate = _Gate()
# Extra spacing for logged-in reads only, on top of the bot-wide gate.
_session_gate = _Gate(lambda: SESSION_MIN_GAP_SECONDS)

# Optional transport override, used by the tests (httpx.MockTransport).
_transport: Any = None


def _client(headers: Optional[dict] = None) -> "httpx.AsyncClient":
    kwargs: dict[str, Any] = {"timeout": TIMEOUT, "follow_redirects": True,
                              "headers": headers or HEADERS}
    if _transport is not None:
        kwargs["transport"] = _transport
    return httpx.AsyncClient(**kwargs)


def _timeout_errors() -> tuple[type[BaseException], ...]:
    errors: tuple[type[BaseException], ...] = (asyncio.TimeoutError,)
    timeout_cls = getattr(httpx, "TimeoutException", None)
    if isinstance(timeout_cls, type):
        errors += (timeout_cls,)
    return errors


def _retry_after(reply: Any) -> Optional[float]:
    headers = getattr(reply, "headers", None) or {}
    try:
        value = float(headers.get("retry-after") or headers.get("Retry-After") or 0)
    except (TypeError, ValueError, AttributeError):
        return None
    return value if value > 0 else None


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _looks_throttled(payload: dict) -> bool:
    """Instagram's 'please wait' / 'log in first' replies, which can arrive
    as JSON with a 200 status. They describe us, not the account."""
    if payload.get("require_login") or payload.get("spam"):
        return True
    if str(payload.get("status", "")).lower() == "fail":
        return True
    message = str(payload.get("message", "")).lower()
    return "wait a few minutes" in message or "login" in message


def _parse(username: str, payload: Any) -> Snapshot:
    """Map one JSON reply to a Snapshot. GONE only when Instagram explicitly
    answered 'there is no such user'; anything odd is UNKNOWN."""
    if not isinstance(payload, dict):
        return Snapshot(username, UNKNOWN, note=NOTE_MALFORMED)
    if _looks_throttled(payload):
        return Snapshot(username, UNKNOWN, note=NOTE_THROTTLED)

    data = payload.get("data")
    if not isinstance(data, dict) or "user" not in data:
        return Snapshot(username, UNKNOWN, note=NOTE_MALFORMED)

    user = data["user"]
    if user is None:
        return Snapshot(username, GONE, note=NOTE_GONE)
    if not isinstance(user, dict) or not (user.get("id") or user.get("username")):
        return Snapshot(username, UNKNOWN, note=NOTE_MALFORMED)

    answered_for = str(user.get("username") or username)
    if answered_for.lower() != username.lower():
        # Never let a reply about someone else complete this person's job.
        return Snapshot(username, UNKNOWN,
                        note="Instagram answered for a different username.")

    def count(key: str) -> Optional[int]:
        block = user.get(key)
        return _as_int(block.get("count")) if isinstance(block, dict) else None

    return Snapshot(
        username=answered_for,
        state=OK,
        user_id=str(user.get("id")) if user.get("id") else None,
        full_name=user.get("full_name") or None,
        verified=bool(user.get("is_verified")),
        private=bool(user.get("is_private")),
        followers=count("edge_followed_by"),
        following=count("edge_follow"),
        posts=count("edge_owner_to_timeline_media"),
        avatar_url=user.get("profile_pic_url_hd") or user.get("profile_pic_url") or None,
        note="This account is private." if user.get("is_private") else "",
    )


def _avatar_url_allowed(url: str) -> bool:
    try:
        parts = urlparse(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(
        host.endswith(suffix) or host == suffix.lstrip(".") for suffix in AVATAR_HOST_SUFFIXES
    )


async def _download_avatar(client: "httpx.AsyncClient", url: Optional[str],
                           username: str) -> Optional[bytes]:
    if not url:
        return None
    if not _avatar_url_allowed(url):
        log.warning("Ignored a profile picture URL for %s on an unexpected host.", username)
        return None
    try:
        async with _gate.slot():
            reply = await client.get(url, headers={"User-Agent": HEADERS["User-Agent"]})
        if reply.status_code == 200 and 0 < len(reply.content) <= AVATAR_MAX_BYTES:
            return reply.content
        log.info("Avatar for %s not used (HTTP %s, %d bytes).",
                 username, reply.status_code, len(reply.content or b""))
    except Exception as exc:
        log.info("Avatar download failed for %s: %s", username, exc)
    return None


# --------------------------------------------------------------------------
# The JSON API (anonymous)
# --------------------------------------------------------------------------

async def _read_api(handle: str, want_avatar: bool) -> tuple[Snapshot, bool]:
    """One read of the JSON endpoint. Returns (snapshot, refused): refused
    means Instagram declined to answer us, so the page may be worth a try."""
    try:
        async with _client() as client:
            async with _gate.slot():
                # Re-check: a request queued behind one that just got a 429
                # must not go out anyway.
                if cooldown_remaining("api") > 0:
                    return _throttled_snapshot(handle, "api"), True
                reply = await client.get(PROFILE_ENDPOINT.format(quote(handle, safe="._")))

            status = reply.status_code
            if status == 404:
                _note_success("api")
                return _remember(Snapshot(handle, GONE, note=NOTE_GONE), handle), False

            # 401/403 = logged-out reads refused, 429 = too many. Both mean
            # "we do not know", which is not the same as "gone".
            if status in (401, 403, 429):
                log.info("Instagram refused an anonymous read of %s (HTTP %s).", handle, status)
                _note_throttle(f"HTTP {status}", _retry_after(reply), "api")
                note = NOTE_THROTTLED if status == 429 else NOTE_LOGIN
                return Snapshot(handle, UNKNOWN, note=note), True

            if status >= 500:
                log.info("Instagram server error reading %s (HTTP %s).", handle, status)
                return Snapshot(handle, UNKNOWN, note="Instagram is having trouble."), False

            if status != 200:
                log.info("Unexpected HTTP %s reading %s.", status, handle)
                return Snapshot(handle, UNKNOWN,
                                note=f"Unexpected reply from Instagram (HTTP {status})."), False

            try:
                payload = reply.json()
            except Exception:
                # A login wall serves HTML with a 200. Not a ban.
                log.info("Instagram served a non-JSON page (login wall) for %s.", handle)
                _note_throttle("login wall", source="api")
                return Snapshot(handle, UNKNOWN, note=NOTE_LOGIN), True

            snapshot = _parse(handle, payload)
            if snapshot.state == UNKNOWN:
                if snapshot.note == NOTE_THROTTLED:
                    _note_throttle("please-wait reply", source="api")
                    return snapshot, True
                log.info("Unusable reply for %s: %s", handle, snapshot.note)
                return snapshot, False

            _note_success("api")
            if snapshot.state == OK and want_avatar:
                snapshot.avatar = await _download_avatar(client, snapshot.avatar_url, handle)
            return _remember(snapshot, handle), False

    except _timeout_errors():
        log.info("Lookup of %s timed out.", handle)
        return Snapshot(handle, UNKNOWN, note=NOTE_TIMEOUT), False
    except Exception as exc:
        log.info("Lookup of %s failed: %s: %s", handle, type(exc).__name__, exc)
        return Snapshot(handle, UNKNOWN, note=NOTE_OFFLINE), False


# --------------------------------------------------------------------------
# The public profile page
# --------------------------------------------------------------------------

_META_TAG = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_ATTRIBUTE = re.compile(r'([\w:.-]+)\s*=\s*"([^"]*)"')
_COUNTS = re.compile(
    r"([\d.,]+\s*[KMB]?)\s+Followers?\s*,\s*([\d.,]+\s*[KMB]?)\s+Following\s*,\s*"
    r"([\d.,]+\s*[KMB]?)\s+Posts?\b", re.IGNORECASE)
_HANDLE_IN_TEXT = re.compile(r"\(@([A-Za-z0-9._]{1,30})\)")
_HANDLE_AT_START = re.compile(r"^@([A-Za-z0-9._]{1,30})\b")
_PAGE_ID_PATTERNS = (
    re.compile(r'<meta[^>]+property="instapp:owner_user_id"[^>]+content="(\d{3,25})"'),
    re.compile(r'"profilePage_(\d{3,25})"'),
    re.compile(r'"profile_id"\s*:\s*"(\d{3,25})"'),
)
_PAGE_GONE_PHRASES = ("Sorry, this page isn't available", "Sorry, this page isn&#039;t available",
                      "Page Not Found")


def _meta_tags(text: str) -> dict[str, str]:
    tags: dict[str, str] = {}
    for tag in _META_TAG.findall(text):
        attrs = {k.lower(): v for k, v in _ATTRIBUTE.findall(tag)}
        key = attrs.get("property") or attrs.get("name")
        if key and "content" in attrs and key not in tags:
            tags[key] = html.unescape(attrs["content"])
    return tags


def _count(text: str) -> Optional[int]:
    """'1,234' -> 1234, '12.3K' -> 12300, '2.3M' -> 2300000."""
    raw = text.replace(" ", "").upper()
    scale = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(raw[-1:], 1)
    number = raw[:-1] if scale != 1 else raw
    try:
        if scale == 1:
            return int(number.replace(",", "").replace(".", ""))
        return int(round(float(number.replace(",", "")) * scale))
    except ValueError:
        return None


def _parse_page(handle: str, text: str) -> Snapshot:
    """Map a profile page to a Snapshot. OK needs the preview tags of THIS
    handle with all three counts; anything less is UNKNOWN."""
    tags = _meta_tags(text)
    description = tags.get("og:description") or tags.get("description") or ""
    title = tags.get("og:title") or ""

    counts = _COUNTS.search(description)
    named = (_HANDLE_IN_TEXT.search(title) or _HANDLE_IN_TEXT.search(description)
             or _HANDLE_AT_START.search(title.strip()))
    if not counts or not named:
        if not title and any(phrase in text for phrase in _PAGE_GONE_PHRASES):
            return Snapshot(handle, GONE, note=NOTE_GONE, source="page")
        return Snapshot(handle, UNKNOWN, note=NOTE_LOGIN, source="page")
    if named.group(1).lower() != handle.lower():
        return Snapshot(handle, UNKNOWN, source="page",
                        note="Instagram answered for a different username.")

    # "Name (@handle) • Instagram photos and videos", or the description's
    # "... See Instagram photos and videos from Name (@handle)".
    full_name = None
    source_text = title if "(@" in title else description
    if "(@" in source_text:
        head = source_text.split("(@")[0]
        if " from " in head:
            head = head.rsplit(" from ", 1)[1]
        head = head.strip(" •·-\u2022")
        if head and not _COUNTS.search(head):
            full_name = head

    user_id = None
    for pattern in _PAGE_ID_PATTERNS:
        found = pattern.search(text)
        if found:
            user_id = found.group(1)
            break

    private: Optional[bool] = None
    if '"is_private":true' in text:
        private = True
    elif '"is_private":false' in text:
        private = False

    return Snapshot(
        username=named.group(1),
        state=OK,
        user_id=user_id,
        full_name=full_name,
        verified=False,               # not reliably on the page; never guessed
        private=private,
        followers=_count(counts.group(1)),
        following=_count(counts.group(2)),
        posts=_count(counts.group(3)),
        avatar_url=tags.get("og:image") or None,
        note=NOTE_FROM_PAGE,
        source="page",
    )


async def _read_page(handle: str, want_avatar: bool) -> Snapshot:
    """One read of instagram.com/<handle>/. Never raises."""
    try:
        async with _client(PAGE_HEADERS) as client:
            async with _gate.slot():
                if cooldown_remaining("page") > 0:
                    return _throttled_snapshot(handle, "page")
                reply = await client.get(PROFILE_PAGE.format(quote(handle, safe="._")))

            status = reply.status_code
            final_path = str(getattr(getattr(reply, "url", None), "path", "") or "")
            if status == 404:
                _note_success("page")
                return Snapshot(handle, GONE, note=NOTE_GONE, source="page")
            if status in (401, 403, 429) or final_path.startswith(("/accounts/login",
                                                                    "/challenge")):
                _note_throttle(f"HTTP {status}" if status != 200 else "login redirect",
                               _retry_after(reply), "page")
                return Snapshot(handle, UNKNOWN, note=NOTE_LOGIN, source="page")
            if status != 200:
                return Snapshot(handle, UNKNOWN, source="page",
                                note=f"Unexpected reply from Instagram (HTTP {status}).")

            body = reply.content or b""
            if len(body) > PAGE_MAX_BYTES:
                return Snapshot(handle, UNKNOWN, note=NOTE_MALFORMED, source="page")
            text = getattr(reply, "text", None)
            if not isinstance(text, str):
                text = body.decode("utf-8", "replace")

            snapshot = _parse_page(handle, text)
            if snapshot.state == UNKNOWN:
                if snapshot.note == NOTE_LOGIN:
                    _note_throttle("login wall", source="page")
                return snapshot
            _note_success("page")
            if snapshot.state == OK and want_avatar:
                snapshot.avatar = await _download_avatar(client, snapshot.avatar_url, handle)
            return snapshot
    except _timeout_errors():
        return Snapshot(handle, UNKNOWN, note=NOTE_TIMEOUT, source="page")
    except Exception as exc:
        log.info("Profile page read of %s failed: %s: %s", handle, type(exc).__name__, exc)
        return Snapshot(handle, UNKNOWN, note=NOTE_OFFLINE, source="page")


# --------------------------------------------------------------------------
# Logged-in reads
# --------------------------------------------------------------------------

_session_problem_logged = False


def _session_rejected(why: str) -> Snapshot:
    """Instagram no longer accepts the cookie (expired, logged out, or the
    account is being challenged). Stop using it for a while and say so once."""
    global _session_problem_logged
    state = _cooldown["session"]
    state[0] = max(state[0], _clock() + SESSION_INVALID_PAUSE)
    if not _session_problem_logged:
        _session_problem_logged = True
        log.error("Instagram rejected the logged-in session (%s). Logged-in reads are "
                  "paused for %d min. Paste a fresh sessionid cookie into IG_SESSIONID "
                  "in .env and restart the bot.", why, SESSION_INVALID_PAUSE // 60)
    return Snapshot("", UNKNOWN, note=NOTE_SESSION_INVALID, source="session")


def _session_trouble(payload: dict) -> bool:
    text = " ".join(str(payload.get(k, "")) for k in ("message", "error_type", "status")).lower()
    return bool(payload.get("require_login")) or any(
        word in text for word in ("login_required", "checkpoint", "challenge", "consent_required"))


async def _read_session(handle: str, want_avatar: bool) -> Snapshot:
    """One logged-in read of the profile API. Never raises."""
    global _session_problem_logged

    def tagged(snapshot: Snapshot) -> Snapshot:
        snapshot.username = snapshot.username or handle
        snapshot.source = "session"
        return snapshot

    try:
        # Redirects are not followed: the cookie must never travel anywhere
        # but this one URL, and a redirect here means "log in again" anyway.
        async with _client() as client:
            async with _session_gate.slot():
                async with _gate.slot():
                    if cooldown_remaining("session") > 0:
                        return tagged(_throttled_snapshot(handle, "session"))
                    reply = await client.get(
                        PROFILE_ENDPOINT.format(quote(handle, safe="._")),
                        headers={"Cookie": f"sessionid={SESSION_ID}"},
                        follow_redirects=False,
                    )

            status = reply.status_code
            if 300 <= status < 400:
                return tagged(_session_rejected(f"redirected to log in, HTTP {status}"))
            if status == 404:
                _note_success("session")
                return Snapshot(handle, GONE, note=NOTE_GONE, source="session")
            if status == 429:
                _note_throttle("HTTP 429", _retry_after(reply), "session")
                return Snapshot(handle, UNKNOWN, note=NOTE_THROTTLED, source="session")
            if status in (401, 403):
                return tagged(_session_rejected(f"HTTP {status}"))
            if status != 200:
                return Snapshot(handle, UNKNOWN, source="session",
                                note=f"Unexpected reply from Instagram (HTTP {status}).")
            try:
                payload = reply.json()
            except Exception:
                return tagged(_session_rejected("login page instead of data"))
            if isinstance(payload, dict) and _session_trouble(payload):
                return tagged(_session_rejected(str(payload.get("message") or "login required")))

            snapshot = _parse(handle, payload)
            snapshot.source = "session"
            if snapshot.state == UNKNOWN:
                if snapshot.note == NOTE_THROTTLED:
                    _note_throttle("please-wait reply", source="session")
                return snapshot
            _note_success("session")
            _session_problem_logged = False
            if snapshot.state == OK and want_avatar:
                # Same client, but the cookie was only on the request above.
                snapshot.avatar = await _download_avatar(client, snapshot.avatar_url, handle)
            return snapshot
    except _timeout_errors():
        return Snapshot(handle, UNKNOWN, note=NOTE_TIMEOUT, source="session")
    except Exception as exc:
        log.info("Logged-in read of %s failed: %s", handle, type(exc).__name__)
        return Snapshot(handle, UNKNOWN, note=NOTE_OFFLINE, source="session")


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

async def lookup(username: str, *, want_avatar: bool = True,
                 use_cache: bool = True) -> Snapshot:
    """Read one public profile. Always returns a Snapshot, never raises.
    Tries the API; only if Instagram refuses it, the profile page; only if
    that is refused too, the logged-in session (when one is configured)."""
    raw = str(username).strip().lstrip("@")
    if not raw:
        return Snapshot(raw, UNKNOWN, note="No username given.")
    handle = normalize_handle(raw)
    if handle is None:
        return Snapshot(raw, UNKNOWN, note=NOTE_INVALID)

    if not HTTPX_AVAILABLE:
        return Snapshot(handle, UNKNOWN, note=NOTE_NO_HTTPX)

    if use_cache:
        hit = _cached(handle, want_avatar)
        if hit is not None:
            return hit

    if cooldown_remaining("api") <= 0:
        primary, refused = await _read_api(handle, want_avatar)
        if not refused:
            return primary
    else:
        primary = _throttled_snapshot(handle, "api")

    if PAGE_FALLBACK and cooldown_remaining("page") <= 0:
        page = await _read_page(handle, want_avatar)
        if page.state != UNKNOWN:
            log.info("Read %s from the public profile page (API refused): %s.",
                     handle, page.state)
            return _remember(page, handle)

    if SESSION_ID and cooldown_remaining("session") <= 0:
        logged_in = await _read_session(handle, want_avatar)
        if logged_in.state != UNKNOWN:
            log.info("Read %s with the logged-in session (anonymous reads refused): %s.",
                     handle, logged_in.state)
            return _remember(logged_in, handle)
        if logged_in.note == NOTE_SESSION_INVALID:
            return Snapshot(handle, UNKNOWN, note=NOTE_SESSION_INVALID, source="session")

    # The API's own reason (login wanted / rate limited / pausing) is the
    # most useful thing to show when the page could not answer either.
    return primary


async def lookup_many(usernames: list[str], *, want_avatar: bool = False,
                      gap: float = 1.5) -> list[Snapshot]:
    """Sequential on purpose, with a gap. Firing these in parallel is the
    fastest way to get the machine's IP rate limited."""
    results = []
    for index, name in enumerate(usernames):
        if index:
            await asyncio.sleep(gap)
        results.append(await lookup(name, want_avatar=want_avatar))
    return results
