"""
Shared fixtures for the pytest suite.

Unlike selftest.py (which runs against hand-written stand-ins), these tests
import the bot against the REAL discord.py and httpx. Nothing goes over the
network: Instagram is an httpx.MockTransport, Discord channels, messages and
interactions are small fakes, and every file lives in a pytest tmp folder.
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import discord  # noqa: E402

import bot as B  # noqa: E402
import cards  # noqa: E402
import instagram  # noqa: E402


# --------------------------------------------------------------------------
# Instagram
# --------------------------------------------------------------------------

def sample_avatar(colour=(60, 140, 200)) -> bytes:
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), colour).save(buffer, "PNG")
    return buffer.getvalue()


AVATAR = sample_avatar()
AVATAR_URL = "https://scontent-lhr8-1.cdninstagram.com/v/t51/avatar.jpg"


def profile(handle: str, *, user_id: Optional[str] = None, followers: int = 1234,
            following: int = 56, posts: int = 7, verified: bool = False,
            private: bool = False, pic: Optional[str] = AVATAR_URL) -> dict:
    return {"status": "ok", "data": {"user": {
        "id": user_id or str(17841400000000000 + sum(map(ord, handle))),
        "username": handle, "full_name": f"Name of {handle}",
        "is_verified": verified, "is_private": private,
        "edge_followed_by": {"count": followers},
        "edge_follow": {"count": following},
        "edge_owner_to_timeline_media": {"count": posts},
        "profile_pic_url_hd": pic,
    }}}


class FakeInstagram:
    """Scripted Instagram. script[handle] is a list of replies; the last one
    repeats. A reply is an httpx.Response, an exception instance, or a
    (status, body) tuple where body is a dict (JSON) or bytes."""

    def __init__(self) -> None:
        self.script: dict[str, list] = {}
        self.pages: dict[str, list] = {}       # instagram.com/<handle>/ replies
        self.session: dict[str, list] = {}     # logged-in API replies
        self.session_reads: list[str] = []
        self.cookies_seen_elsewhere: list[str] = []
        self.page_reads: list[str] = []
        self.profile_reads: list[str] = []
        self.avatar_reads: list[str] = []
        self.avatar_reply: Any = (200, AVATAR)

    def set(self, handle: str, *replies: Any) -> None:
        self.script[handle] = list(replies)

    def set_page(self, handle: str, *replies: Any) -> None:
        self.pages[handle] = list(replies)

    def set_session(self, handle: str, *replies: Any) -> None:
        self.session[handle] = list(replies)

    @staticmethod
    def _response(reply: Any) -> httpx.Response:
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, httpx.Response):
            return reply
        status, body = reply
        if isinstance(body, (dict, list)):
            return httpx.Response(status, json=body)
        return httpx.Response(status, content=body)

    def handler(self, request: httpx.Request) -> httpx.Response:
        cookie = request.headers.get("cookie", "")
        if "web_profile_info" in request.url.path and "sessionid=" in cookie:
            handle = request.url.params.get("username", "")
            self.session_reads.append(handle)
            seq = self.session.get(handle)
            if not seq:
                raise httpx.ConnectError("no session script for " + handle)
            reply = seq.pop(0) if len(seq) > 1 else seq[0]
            return self._response(reply)
        if cookie:
            self.cookies_seen_elsewhere.append(f"{request.url} {cookie}")
        if "web_profile_info" in request.url.path:
            handle = request.url.params.get("username", "")
            self.profile_reads.append(handle)
            seq = self.script.get(handle)
            if not seq:
                raise httpx.ConnectError("no script for " + handle)
            reply = seq.pop(0) if len(seq) > 1 else seq[0]
            return self._response(reply)
        if request.url.host == "www.instagram.com":
            handle = request.url.path.strip("/")
            self.page_reads.append(handle)
            seq = self.pages.get(handle)
            if not seq:
                raise httpx.ConnectError("no page script for " + handle)
            reply = seq.pop(0) if len(seq) > 1 else seq[0]
            return self._response(reply)
        self.avatar_reads.append(str(request.url))
        return self._response(self.avatar_reply)


def profile_page(handle: str, *, name: Optional[str] = "Some Name",
                 followers: str = "1,234", following: str = "56", posts: str = "7",
                 user_id: Optional[str] = None, private: Optional[bool] = None,
                 pic: Optional[str] = AVATAR_URL) -> bytes:
    """A logged-out instagram.com/<handle>/ page, reduced to what matters:
    the preview (og:) tags, attributes in Instagram's own order."""
    who = f"{name} (@{handle})" if name else f"@{handle}"
    desc = (f"{followers} Followers, {following} Following, {posts} Posts - "
            f"See Instagram photos and videos from {who}")
    extra = ""
    if user_id:
        extra += f'<meta property="instapp:owner_user_id" content="{user_id}" />'
    if private is not None:
        extra += f'<script>{{"is_private":{"true" if private else "false"}}}</script>'
    image = f'<meta property="og:image" content="{pic}" />' if pic else ""
    html_text = (
        "<!DOCTYPE html><html><head>"
        f'<meta property="og:title" content="{who} &#x2022; Instagram photos and videos" />'
        f'<meta content="{desc}" property="og:description" />'
        f"{image}{extra}</head><body></body></html>"
    )
    return html_text.encode()


class Clock:
    def __init__(self) -> None:
        self.now = 10_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --------------------------------------------------------------------------
# Discord fakes
# --------------------------------------------------------------------------

def http_error(cls: type, status: int, text: str = "error") -> discord.HTTPException:
    response = types.SimpleNamespace(status=status, reason=text)
    return cls(response, text)


class FakeMessage:
    _next_id = 5_000

    def __init__(self, channel: "FakeChannel", embed=None, files=None, view=None) -> None:
        FakeMessage._next_id += 1
        self.id = FakeMessage._next_id
        self.channel = channel
        self.embed, self.files, self.view = embed, list(files or []), view
        self.edits = 0
        self.deleted = False

    async def edit(self, *, embed=None, view=None, attachments=None, **kw) -> "FakeMessage":
        await asyncio.sleep(0)
        if self.channel.fail_edit:
            raise self.channel.fail_edit
        self.embed, self.view = embed, view
        self.files = list(attachments or [])
        self.edits += 1
        return self

    async def delete(self) -> None:
        self.deleted = True
        self.channel.messages.pop(self.id, None)


class FakeChannel:
    def __init__(self, channel_id: int, guild_id: int) -> None:
        self.id = channel_id
        self.guild = types.SimpleNamespace(id=guild_id)
        self.messages: dict[int, FakeMessage] = {}
        self.sent: list[FakeMessage] = []
        self.fail_send: Optional[BaseException] = None
        self.fail_send_with_files: Optional[BaseException] = None
        self.fail_edit: Optional[BaseException] = None

    async def send(self, content=None, *, embed=None, files=None, view=None, **kw) -> FakeMessage:
        await asyncio.sleep(0)  # a real API call always yields to the loop
        if self.fail_send:
            raise self.fail_send
        if files and self.fail_send_with_files:
            raise self.fail_send_with_files
        # discord.py enforces the embed limits client-side too; mirror the
        # API's verdict so a too-big embed fails loudly in tests.
        if embed is not None:
            assert_embed_within_limits(embed)
        message = FakeMessage(self, embed, files, view)
        self.messages[message.id] = message
        self.sent.append(message)
        return message

    async def fetch_message(self, message_id: int) -> FakeMessage:
        await asyncio.sleep(0)
        if message_id in self.messages:
            return self.messages[message_id]
        raise http_error(discord.NotFound, 404, "Unknown Message")


def assert_embed_within_limits(embed: discord.Embed) -> None:
    assert len(embed) <= 6000, len(embed)
    assert len(embed.title or "") <= 256
    assert len(embed.description or "") <= 4096
    assert len(embed.fields) <= 25
    for f in embed.fields:
        assert len(f.name or "") <= 256, f.name
        assert len(f.value or "") <= 1024, len(f.value or "")


class FakeDiscord:
    def __init__(self) -> None:
        self.channels: dict[int, FakeChannel] = {}

    def channel(self, guild_id: int) -> FakeChannel:
        cid = 900_000 + guild_id
        if cid not in self.channels:
            self.channels[cid] = FakeChannel(cid, guild_id)
        return self.channels[cid]

    def setup(self, guild_id: int) -> FakeChannel:
        """What /setup leaves behind in config.json."""
        channel = self.channel(guild_id)
        B.guild_config(guild_id)["monitor_channel_id"] = channel.id
        return channel

    def get_channel(self, cid: int):
        return self.channels.get(cid)

    async def fetch_channel(self, cid: int):
        if cid in self.channels:
            return self.channels[cid]
        raise http_error(discord.NotFound, 404, "Unknown Channel")


class FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[types.SimpleNamespace] = []
        self.fail: Optional[BaseException] = None

    async def send(self, content=None, *, embed=None, files=None, ephemeral=False, **kw):
        if self.fail:
            raise self.fail
        if embed is not None:
            assert_embed_within_limits(embed)
        self.sent.append(types.SimpleNamespace(content=content, embed=embed,
                                               files=list(files or []), ephemeral=ephemeral))


class FakeResponse:
    def __init__(self) -> None:
        self.deferred = False
        self.messages: list[types.SimpleNamespace] = []

    def is_done(self) -> bool:
        return self.deferred or bool(self.messages)

    async def defer(self, **kw) -> None:
        self.deferred = True

    async def send_message(self, content=None, *, embed=None, files=None, ephemeral=False,
                           view=None, **kw) -> None:
        self.messages.append(types.SimpleNamespace(content=content, embed=embed,
                                                   files=list(files or []), view=view,
                                                   ephemeral=ephemeral))


class FakeUser:
    def __init__(self, user_id: int = 42) -> None:
        self.id = user_id
        self.display_avatar = types.SimpleNamespace(url="https://cdn.example/a.png")

    def __str__(self) -> str:
        return f"user{self.id}"


class FakeInteraction:
    def __init__(self, guild_id: Optional[int] = 1, user_id: int = 42,
                 channel_id: Optional[int] = None) -> None:
        self.guild_id = guild_id
        self.channel_id = channel_id if channel_id is not None else 900_000 + (guild_id or 0)
        self.user = FakeUser(user_id)
        self.client = types.SimpleNamespace(user=None)
        self.response = FakeResponse()
        self.followup = FakeFollowup()

    def replies(self) -> list:
        return self.response.messages + self.followup.sent


# --------------------------------------------------------------------------
# The fixture every test uses
# --------------------------------------------------------------------------

class Env:
    def __init__(self, tmp: Path, ig: FakeInstagram, dc: FakeDiscord, clock: Clock) -> None:
        self.tmp, self.ig, self.dc, self.clock = tmp, ig, dc, clock

    def disk_jobs(self) -> list:
        return json.loads(B.JOBS_FILE.read_text(encoding="utf-8"))

    def disk_config(self) -> dict:
        return json.loads(B.CONFIG_FILE.read_text(encoding="utf-8"))

    def job(self, job_id: str, handle: str, *, service: str = B.SERVICE_UNBAN,
            guild: Any = "1", age: int = 0, **extra) -> dict:
        started = (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()
        record = {"id": job_id, "guild_id": guild, "username": handle, "service": service,
                  "started": started, "status": "active", "opened_by": "7", **extra}
        B.jobs.append(record)
        B.save_jobs()
        return record

    async def tick(self) -> None:
        self.clock.advance(B.MONITOR_INTERVAL_SECONDS)
        await B.run_monitor_tick()


@pytest.fixture
def env(tmp_path, monkeypatch) -> Env:
    ig, dc, clock = FakeInstagram(), FakeDiscord(), Clock()

    monkeypatch.setattr(B, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(B, "JOBS_FILE", tmp_path / "data" / "jobs.json")
    monkeypatch.setattr(B, "CONFIG_FILE", tmp_path / "data" / "config.json")
    monkeypatch.setattr(B, "BASE_DIR", tmp_path)
    monkeypatch.setattr(B, "LEGACY_GUILD_ID", "")

    monkeypatch.setattr(instagram, "_transport", httpx.MockTransport(ig.handler))
    monkeypatch.setattr(instagram, "_clock", clock)
    monkeypatch.setattr(instagram, "MIN_GAP_SECONDS", 0)
    monkeypatch.setattr(instagram, "HTTPX_AVAILABLE", True)
    monkeypatch.setattr(instagram, "SESSION_ID", "")
    monkeypatch.setattr(instagram, "SESSION_MIN_GAP_SECONDS", 0)
    instagram._cache.clear()
    instagram.reset_rate_limit()

    monkeypatch.setattr(B.bot, "get_channel", dc.get_channel)
    monkeypatch.setattr(B.bot, "fetch_channel", dc.fetch_channel)
    monkeypatch.setattr(B, "FINAL_LOOKUP_DELAYS", (0, 0))

    B.jobs.clear()
    B.config.clear()
    B.monitor_status.clear()
    B._completing.clear()
    B._card_failures.clear()
    B._dashboard_locks.clear()
    B._dashboard_problems.clear()
    B._unconfigured_warned.clear()
    B.bot.signatures.clear()
    B._save_pending = False
    yield Env(tmp_path, ig, dc, clock)
    B.jobs.clear()
    B.config.clear()
    instagram._cache.clear()
    instagram.reset_rate_limit()


@pytest.fixture
def logs():
    """Collects every log line from the bot's loggers."""
    import logging
    lines: list[str] = []

    class Grab(logging.Handler):
        def emit(self, record):
            lines.append(f"{record.levelname}: {record.getMessage()}")

    handler = Grab()
    logging.getLogger("zm").addHandler(handler)
    yield lines
    logging.getLogger("zm").removeHandler(handler)


__all__ = ["B", "cards", "instagram", "discord", "profile", "AVATAR", "AVATAR_URL",
           "FakeInteraction", "http_error", "assert_embed_within_limits", "Callable"]
