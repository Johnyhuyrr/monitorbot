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
"""

from __future__ import annotations

import asyncio
import contextlib
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
    private: bool = False
    followers: Optional[int] = None
    following: Optional[int] = None
    posts: Optional[int] = None
    avatar: Optional[bytes] = field(default=None, repr=False)
    avatar_url: Optional[str] = field(default=None, repr=False)
    note: str = ""

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

_cooldown_until = 0.0
_cooldown_step = 0


def cooldown_remaining() -> float:
    return max(0.0, _cooldown_until - _clock())


def reset_rate_limit() -> None:
    global _cooldown_until, _cooldown_step
    _cooldown_until, _cooldown_step = 0.0, 0


def _note_throttle(reason: str, retry_after: Optional[float] = None) -> None:
    global _cooldown_until, _cooldown_step
    _cooldown_step = min(_cooldown_step + 1, 10)
    delay = min(COOLDOWN_BASE_SECONDS * 2 ** (_cooldown_step - 1), COOLDOWN_MAX_SECONDS)
    if retry_after:
        delay = min(max(delay, retry_after), COOLDOWN_MAX_SECONDS)
    _cooldown_until = max(_cooldown_until, _clock() + delay)
    log.warning("Instagram rate limit (%s). Pausing Instagram reads for %ds.", reason, int(delay))


def _note_success() -> None:
    # A clean answer means the next throttle starts again from the short
    # pause. A pause already running is left alone - requests that were in
    # flight together can finish in any order.
    global _cooldown_step
    _cooldown_step = 0


def _throttled_snapshot(handle: str) -> Snapshot:
    wait = int(cooldown_remaining())
    when = f"about {max(1, round(wait / 60))} min" if wait >= 60 else f"about {max(wait, 1)} s"
    return Snapshot(handle, UNKNOWN,
                    note=f"Instagram is rate limiting anonymous checks. Next try in {when}.")


class _Gate:
    """Bounded, spaced access to Instagram. Rebuilt per event loop, because
    asyncio primitives belong to the loop that first waits on them."""

    def __init__(self) -> None:
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
                wait = self._last + MIN_GAP_SECONDS - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last = time.monotonic()
            yield


_gate = _Gate()

# Optional transport override, used by the tests (httpx.MockTransport).
_transport: Any = None


def _client() -> "httpx.AsyncClient":
    kwargs: dict[str, Any] = {"timeout": TIMEOUT, "follow_redirects": True, "headers": HEADERS}
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
# Public API
# --------------------------------------------------------------------------

async def lookup(username: str, *, want_avatar: bool = True,
                 use_cache: bool = True) -> Snapshot:
    """Read one public profile. Always returns a Snapshot, never raises."""
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

    if cooldown_remaining() > 0:
        return _throttled_snapshot(handle)

    try:
        async with _client() as client:
            async with _gate.slot():
                # Re-check: a request queued behind one that just got a 429
                # must not go out anyway.
                if cooldown_remaining() > 0:
                    return _throttled_snapshot(handle)
                reply = await client.get(PROFILE_ENDPOINT.format(quote(handle, safe="._")))

            status = reply.status_code
            if status == 404:
                _note_success()
                return _remember(Snapshot(handle, GONE, note=NOTE_GONE), handle)

            # 401/403 = logged-out reads refused, 429 = too many. Both mean
            # "we do not know", which is not the same as "gone".
            if status in (401, 403, 429):
                log.info("Instagram refused an anonymous read of %s (HTTP %s).", handle, status)
                _note_throttle(f"HTTP {status}", _retry_after(reply))
                return Snapshot(handle, UNKNOWN, note=NOTE_THROTTLED)

            if status >= 500:
                log.info("Instagram server error reading %s (HTTP %s).", handle, status)
                return Snapshot(handle, UNKNOWN, note="Instagram is having trouble.")

            if status != 200:
                log.info("Unexpected HTTP %s reading %s.", status, handle)
                return Snapshot(handle, UNKNOWN,
                                note=f"Unexpected reply from Instagram (HTTP {status}).")

            try:
                payload = reply.json()
            except Exception:
                # A login wall serves HTML with a 200. Not a ban.
                log.info("Instagram served a non-JSON page (login wall) for %s.", handle)
                _note_throttle("login wall")
                return Snapshot(handle, UNKNOWN, note=NOTE_THROTTLED)

            snapshot = _parse(handle, payload)
            if snapshot.state == UNKNOWN:
                if snapshot.note == NOTE_THROTTLED:
                    _note_throttle("please-wait reply")
                else:
                    log.info("Unusable reply for %s: %s", handle, snapshot.note)
                return snapshot

            _note_success()
            if snapshot.state == OK and want_avatar:
                snapshot.avatar = await _download_avatar(client, snapshot.avatar_url, handle)
            return _remember(snapshot, handle)

    except _timeout_errors():
        log.info("Lookup of %s timed out.", handle)
        return Snapshot(handle, UNKNOWN, note=NOTE_TIMEOUT)
    except Exception as exc:
        log.info("Lookup of %s failed: %s: %s", handle, type(exc).__name__, exc)
        return Snapshot(handle, UNKNOWN, note=NOTE_OFFLINE)


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
