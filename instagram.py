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
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

try:
    import httpx
    HTTPX_AVAILABLE = True
except Exception:  # pragma: no cover
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

OK, GONE, UNKNOWN = "ok", "gone", "unknown"

NOTE_GONE = "Same answer Instagram gives for a delete, a rename or a deactivation."
NOTE_THROTTLED = "Instagram is rate limiting anonymous checks. Try again shortly."
NOTE_OFFLINE = "Could not reach Instagram from this machine."
NOTE_NO_HTTPX = "httpx is not installed - run: pip install -r requirements.txt"


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


_cache: dict[str, tuple[float, Snapshot]] = {}


def _cached(username: str) -> Optional[Snapshot]:
    hit = _cache.get(username.lower())
    if hit and (time.time() - hit[0]) < CACHE_SECONDS:
        return hit[1]
    return None


def _remember(snapshot: Snapshot) -> Snapshot:
    # Never cache a throttle: the next attempt should be allowed to succeed.
    if snapshot.state != UNKNOWN:
        _cache[snapshot.username.lower()] = (time.time(), snapshot)
    return snapshot


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse(username: str, payload: dict) -> Snapshot:
    user = ((payload or {}).get("data") or {}).get("user")
    if not user:
        return Snapshot(username, GONE, note=NOTE_GONE)

    return Snapshot(
        username=user.get("username") or username,
        state=OK,
        user_id=str(user.get("id")) if user.get("id") else None,
        full_name=user.get("full_name") or None,
        verified=bool(user.get("is_verified")),
        private=bool(user.get("is_private")),
        followers=_as_int((user.get("edge_followed_by") or {}).get("count")),
        following=_as_int((user.get("edge_follow") or {}).get("count")),
        posts=_as_int((user.get("edge_owner_to_timeline_media") or {}).get("count")),
        note="This account is private." if user.get("is_private") else "",
    )


async def _download_avatar(client: "httpx.AsyncClient", user: dict) -> Optional[bytes]:
    url = user.get("profile_pic_url_hd") or user.get("profile_pic_url")
    if not url:
        return None
    try:
        reply = await client.get(url, headers={"User-Agent": HEADERS["User-Agent"]})
        if reply.status_code == 200 and len(reply.content) <= AVATAR_MAX_BYTES:
            return reply.content
    except Exception as exc:
        log.info("Avatar download failed for %s: %s", user.get("username"), exc)
    return None


async def lookup(username: str, *, want_avatar: bool = True,
                 use_cache: bool = True) -> Snapshot:
    """Read one public profile. Always returns a Snapshot, never raises."""
    handle = str(username).strip().lstrip("@")
    if not handle:
        return Snapshot(handle, UNKNOWN, note="No username given.")

    if not HTTPX_AVAILABLE:
        return Snapshot(handle, UNKNOWN, note=NOTE_NO_HTTPX)

    if use_cache:
        hit = _cached(handle)
        if hit is not None:
            return hit

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True,
                                     headers=HEADERS) as client:
            reply = await client.get(PROFILE_ENDPOINT.format(handle))

            if reply.status_code == 404:
                return _remember(Snapshot(handle, GONE, note=NOTE_GONE))

            # 401/403 = logged-out reads refused, 429 = too many. Both mean
            # "we do not know", which is not the same as "gone".
            if reply.status_code in (401, 403, 429):
                log.info("Instagram refused an anonymous read of %s (HTTP %s).",
                         handle, reply.status_code)
                return Snapshot(handle, UNKNOWN, note=NOTE_THROTTLED)

            if reply.status_code >= 500:
                return Snapshot(handle, UNKNOWN, note="Instagram is having trouble.")

            if reply.status_code != 200:
                return Snapshot(handle, UNKNOWN,
                                note=f"Unexpected reply from Instagram (HTTP {reply.status_code}).")

            try:
                payload = reply.json()
            except Exception:
                # A login wall serves HTML with a 200. Not a ban.
                return Snapshot(handle, UNKNOWN, note=NOTE_THROTTLED)

            snapshot = _parse(handle, payload)
            if snapshot.state == OK and want_avatar:
                user = ((payload or {}).get("data") or {}).get("user") or {}
                snapshot.avatar = await _download_avatar(client, user)
            return _remember(snapshot)

    except (asyncio.TimeoutError,):
        return Snapshot(handle, UNKNOWN, note="Instagram did not answer in time.")
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
