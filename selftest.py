#!/usr/bin/env python3
"""
Offline self-test for the Zade Meadows monitor.

Run it any time you change bot.py, cards.py or instagram.py:

    python selftest.py

It never touches Discord and never touches Instagram. It builds throwaway
stand-ins for discord.py and httpx in a temp folder, imports the bot against
those, and checks the things that actually break in production:

  * the five panel buttons and their custom_ids
  * Discord's embed size limits, with a deliberately overloaded dashboard
  * the pictures (animated logo, profile card) really get attached
  * the wording rules: a handle that does not answer is never called a ban
  * the bot still works with Pillow and httpx missing

Exit code is 0 when everything passes, 1 otherwise, so it can sit in CI.
"""

from __future__ import annotations

import asyncio
import io
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent

FAILURES: list[str] = []


def check(label: str, ok: bool, extra: object = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + label + (f"   [{extra}]" if extra != "" else ""))
    if not ok:
        FAILURES.append(label)


def section(title: str) -> None:
    print(f"\n{title}")


# --------------------------------------------------------------------------
# Throwaway stand-ins, written fresh each run so they can never drift.
# --------------------------------------------------------------------------

DISCORD_STUB = '''
import re as _re

class Colour:
    def __init__(self, value=0): self.value = value
    @classmethod
    def dark_grey(cls): return cls(0x36393e)
Color = Colour

class _Utils:
    @staticmethod
    def escape_markdown(text):
        return _re.sub(r"([*_~`|\\\\>])", r"\\\\\\1", str(text))
utils = _Utils()

class Embed:
    def __init__(self, colour=None, title=None, url=None, description=None, timestamp=None, **kw):
        self.colour = colour; self.title = title; self.url = url
        self.description = description; self.timestamp = timestamp
        self.fields = []; self.author = None; self.footer = None
        self.thumbnail = None; self.image = None
    def set_author(self, name=None, icon_url=None): self.author = (name, icon_url); return self
    def set_footer(self, text=None): self.footer = text; return self
    def set_thumbnail(self, url=None): self.thumbnail = url; return self
    def set_image(self, url=None): self.image = url; return self
    def add_field(self, name=None, value=None, inline=False):
        self.fields.append({"name": name, "value": value, "inline": inline}); return self
    @property
    def total_length(self):
        n = len(self.title or "") + len(self.description or "") + len(self.footer or "")
        n += len((self.author or ("", ""))[0] or "")
        return n + sum(len(f["name"] or "") + len(f["value"] or "") for f in self.fields)

class File:
    def __init__(self, fp, filename=None, **kw):
        self.fp = fp; self.filename = filename

class abc:
    class User: pass

class Intents:
    def __init__(self): self.message_content = False; self.guilds = False
    @classmethod
    def default(cls): return cls()
    @classmethod
    def none(cls): return cls()

class Object:
    def __init__(self, id=None): self.id = id

class ButtonStyle:
    primary = "primary"; secondary = "secondary"; success = "success"; danger = "danger"

class ActivityType:
    watching = "watching"

class Activity:
    def __init__(self, **kw): self.kw = kw

class Interaction: pass
class HTTPException(Exception): pass
class NotFound(HTTPException): pass
class Forbidden(HTTPException): pass
class LoginFailure(Exception): pass
class PrivilegedIntentsRequired(Exception): pass

class _UI:
    class Item: pass
    class View:
        def __init_subclass__(cls, **kw): pass
        def __init__(self, timeout=None): pass
    class Modal:
        def __init_subclass__(cls, **kw): pass
        def __init__(self, title=None): pass
        def add_item(self, item): pass
    class TextInput:
        def __init__(self, **kw): self.value = ""
    @staticmethod
    def button(**kw):
        def deco(fn):
            fn.__button__ = kw
            return fn
        return deco
ui = _UI()
'''

APP_COMMANDS_STUB = '''
class Choice:
    def __init__(self, name=None, value=None): self.name = name; self.value = value

def command(**kw):
    def deco(fn): return fn
    return deco

def describe(**kw):
    def deco(fn): return fn
    return deco

def choices(**kw):
    def deco(fn): return fn
    return deco

def guild_only():
    def deco(fn): return fn
    return deco

def default_permissions(**kw):
    def deco(fn): return fn
    return deco

class CommandTree:
    def __init__(self, *a, **kw): pass

class _Checks:
    @staticmethod
    def has_permissions(**kw):
        def deco(fn): return fn
        return deco
    @staticmethod
    def cooldown(*a, **kw):
        def deco(fn): return fn
        return deco
    @staticmethod
    def bot_has_permissions(**kw):
        def deco(fn): return fn
        return deco
checks = _Checks()

class AppCommandError(Exception): pass
class MissingPermissions(AppCommandError): pass
class BotMissingPermissions(AppCommandError): pass
class CommandOnCooldown(AppCommandError):
    def __init__(self, *a, **kw):
        super().__init__(*a)
        self.retry_after = 0.0
class CheckFailure(AppCommandError): pass
class NoPrivateMessage(CheckFailure): pass
class TransformerError(AppCommandError): pass
class CommandInvokeError(AppCommandError): pass
'''

COMMANDS_STUB = '''
def when_mentioned(bot, msg): return []

class Bot:
    def __init__(self, *a, **kw):
        self.user = None
        self.tree = _Tree()
    def event(self, fn): return fn
    def run(self, *a, **kw): pass
    def get_channel(self, *a, **kw): return None
    async def change_presence(self, **kw): pass

class _Tree:
    def command(self, **kw):
        def deco(fn): return fn
        return deco
    def error(self, fn): return fn
    async def sync(self, **kw): return []
    def copy_global_to(self, **kw): pass
'''

TASKS_STUB = '''
def loop(**kw):
    def deco(fn):
        class _Loop:
            def __init__(self, f): self.f = f
            def start(self, *a, **k): pass
            def stop(self, *a, **k): pass
            def restart(self, *a, **k): pass
            def before_loop(self, f): return f
            def error(self, f): return f
            def __call__(self, *a, **k): return self.f(*a, **k)
        return _Loop(fn)
    return deco
'''

DOTENV_STUB = '''
def load_dotenv(*a, **kw): return False
'''

HTTPX_STUB = '''
"""Routes are set by the test: _ROUTES[url_fragment] = (status, body)."""
import json as _json

_ROUTES = {}
_CALLS = []

class _Reply:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
    @property
    def content(self):
        return self._body if isinstance(self._body, bytes) else _json.dumps(self._body).encode()
    def json(self):
        if isinstance(self._body, (dict, list)):
            return self._body
        raise ValueError("not json")

class AsyncClient:
    def __init__(self, **kw): self.kw = kw
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def get(self, url, headers=None):
        _CALLS.append(url)
        for fragment, (status, body) in _ROUTES.items():
            if fragment in url:
                return _Reply(status, body)
        raise RuntimeError("no route for " + url)
'''


def build_stubs() -> Path:
    root = Path(tempfile.mkdtemp(prefix="zm-selftest-"))
    pkg = root / "discord"
    (pkg / "ext").mkdir(parents=True)
    (pkg / "__init__.py").write_text(DISCORD_STUB, encoding="utf-8")
    (pkg / "app_commands.py").write_text(APP_COMMANDS_STUB, encoding="utf-8")
    (pkg / "ext" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "ext" / "commands.py").write_text(COMMANDS_STUB, encoding="utf-8")
    (pkg / "ext" / "tasks.py").write_text(TASKS_STUB, encoding="utf-8")
    (root / "dotenv.py").write_text(DOTENV_STUB, encoding="utf-8")
    (root / "httpx.py").write_text(HTTPX_STUB, encoding="utf-8")
    return root


def sample_avatar(colour=(70, 110, 190)) -> bytes:
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", (320, 320), colour).save(buffer, "PNG")
    return buffer.getvalue()


PROFILE = {"data": {"user": {
    "id": "17841400000000000",
    "username": "kushina.uzk",
    "full_name": "Amardeep Banarjee",
    "is_verified": True,
    "is_private": False,
    "edge_followed_by": {"count": 2_300_000},
    "edge_follow": {"count": 231},
    "edge_owner_to_timeline_media": {"count": 87},
    "profile_pic_url_hd": "https://scontent.cdninstagram.com/avatar.jpg",
}}}


def main() -> int:
    stub_root = build_stubs()
    sys.path.insert(0, str(HERE))
    sys.path.insert(0, str(stub_root))

    import httpx                      # the stub
    import instagram, cards
    import bot as B

    print(f"Zade Meadows self-test  ·  {datetime.now():%Y-%m-%d %H:%M}")

    # ----------------------------------------------------------------- setup
    section("Wiring")
    check("stubs are in charge (no real Discord call can happen)",
          "zm-selftest" in str(sys.modules["discord"].__file__))
    check("Pillow available", cards.PIL_AVAILABLE)
    check("four house colours defined", all(isinstance(c, int) for c in (
        B.COLOR_IDLE, B.COLOR_ACTIVE, B.COLOR_DONE, B.COLOR_WARN)))
    check("job types are Verification and Unban",
          B.SERVICES == (B.SERVICE_VERIFICATION, B.SERVICE_UNBAN), B.SERVICES)

    # --------------------------------------------------------------- buttons
    section("Control panel")
    ids = [kw.get("custom_id") for kw in
           (getattr(obj, "__button__", None) for obj in vars(B.DashboardButtons).values()) if kw]
    expected = {"zm_verification", "zm_unban", "zm_ban_check", "zm_complete_job", "zm_refresh"}
    check("the five expected buttons are present", set(ids) == expected, ", ".join(sorted(ids)))
    check("Account Support is gone", "zm_account_support" not in ids)
    check("they fit on one row (Discord allows 5)", len(ids) <= 5, len(ids))

    # ------------------------------------------------------------- pictures
    section("Pictures")
    logo = B.logo_files()
    check("animated logo attaches", len(logo) == 1 and logo[0].filename == B.LOGO_FILENAME)
    gif = cards.animated_logo()
    check("logo stays light enough to re-upload on every refresh",
          bool(gif) and len(gif) < 120_000, f"{len(gif or b''):,} bytes")
    card = cards.render_profile_card("kushina.uzk", full_name="Amardeep Banarjee",
                                     posts=87, followers=2_300_000, following=231,
                                     verified=True, avatar_bytes=sample_avatar())
    check("profile card renders", bool(card), f"{len(card or b''):,} bytes")
    check("card file attaches", len(B.card_files(card)) == 1)
    check("no picture, no attachment", B.card_files(None) == [])
    embed = B.house_embed(B.COLOR_ACTIVE, title="t", description="d", logo=True, card=True)
    check("logo lands on the thumbnail", embed.thumbnail == B.LOGO_URL)
    check("card lands on the image", embed.image == B.CARD_URL)
    check("counts shorten the way Instagram does",
          (cards.human_count(2_300_000), cards.human_count(12_345), cards.human_count(87))
          == ("2.3M", "12.3K", "87"))

    # -------------------------------------------------------------- handles
    section("Handles")
    check("underscores cannot italicise the embed", "\\_" in B.clean("kushina_uzk_x"),
          B.clean("kushina_uzk_x"))
    check("real handle becomes a link",
          B.profile_url("kushina.uzk") == "https://instagram.com/kushina.uzk")
    check("rubbish is not linked", B.profile_url("not a handle!") is None)

    # ------------------------------------------------------------- live read
    section("Live profile read")
    buf_avatar = sample_avatar()
    httpx._ROUTES.clear(); instagram._cache.clear()
    httpx._ROUTES["web_profile_info"] = (200, PROFILE)
    httpx._ROUTES["cdninstagram"] = (200, buf_avatar)
    snap, files = asyncio.run(B.build_frame("kushina.uzk"))
    check("profile read", snap.state == instagram.OK and snap.followers == 2_300_000)
    check("that person's own photo was downloaded",
          snap.avatar == buf_avatar, f"{len(snap.avatar or b''):,} bytes")
    check("numeric id kept (tells a rename from a loss later)",
          snap.user_id == "17841400000000000")
    check("card built from that photo", len(files) == 1 and files[0].filename == B.CARD_FILENAME)

    calls = len(httpx._CALLS)
    asyncio.run(instagram.lookup("kushina.uzk"))
    check("a repeat check inside 2 minutes is served from cache",
          len(httpx._CALLS) == calls)

    httpx._ROUTES.clear(); instagram._cache.clear()
    httpx._ROUTES["web_profile_info"] = (404, {})
    gone = asyncio.run(instagram.lookup("vanished.acct"))
    check("404 reads as not reachable", gone.state == instagram.GONE)

    httpx._ROUTES.clear(); instagram._cache.clear()
    httpx._ROUTES["web_profile_info"] = (429, {})
    throttled = asyncio.run(instagram.lookup("throttled.acct"))
    check("rate limit reads as unknown", throttled.state == instagram.UNKNOWN)
    check("a rate limit is never cached", not instagram._cache)

    httpx._ROUTES.clear(); instagram._cache.clear()
    httpx._ROUTES["web_profile_info"] = (200, b"<html>login wall</html>")
    walled = asyncio.run(instagram.lookup("walled.acct"))
    check("a login wall is unknown, not gone", walled.state == instagram.UNKNOWN)

    httpx._ROUTES.clear(); instagram._cache.clear()
    broken = asyncio.run(instagram.lookup("no.route"))
    check("a dead connection is unknown, not gone", broken.state == instagram.UNKNOWN)

    # ------------------------------------------------------------- wording
    section("Wording  ·  nothing is ever called a ban")
    ok_snap = instagram.Snapshot("x", instagram.OK, private=True, verified=True)
    for snap_, phrase in ((ok_snap, "Reachable"), (gone, "Not reachable"),
                          (throttled, "Could not check")):
        row = B.account_row(snap_)
        check(f"{snap_.state}: says '{phrase}'", phrase in row, row.strip())
        check(f"{snap_.state}: the status line never asserts a ban", "ban" not in row.lower())
    gone_note = B.caveat(gone).lower()
    check("not-reachable spells out the other explanations",
          "rename" in gone_note and "delete" in gone_note)
    check("not-reachable says outright it is not proof", "not proof of a ban" in gone_note)
    check("rate limited never mentions a ban at all", "ban" not in B.caveat(throttled).lower())
    check("not reachable is red", B.snapshot_colour(gone) == B.COLOR_WARN)
    check("could not check is slate, not red", B.snapshot_colour(throttled) == B.COLOR_IDLE)

    # ------------------------------------------------------------ dashboard
    section("Dashboard under load  ·  Discord's limits")
    B.jobs.clear()
    now = datetime.now(timezone.utc)
    for index in range(60):
        B.jobs.append({"id": f"ZM-{index:04d}", "guild_id": "1",
                       "username": "x" * 30 + str(index), "service": B.SERVICE_UNBAN,
                       "started": (now - timedelta(minutes=index)).isoformat(),
                       "status": "active", "opened_by": "1", "ig_user_id": str(index)})
    for index in range(5):
        B.jobs.append({"id": f"ZM-9{index:03d}", "guild_id": "1", "username": "done",
                       "service": B.SERVICE_VERIFICATION, "started": now.isoformat(),
                       "completed": now.isoformat(), "status": "completed",
                       "duration_seconds": 900, "closed_by": "1"})

    class FakeUser:
        name = "Zade Meadows"
        display_avatar = type("A", (), {"url": "https://example/avatar.png"})()

    busy = B.build_dashboard(1, FakeUser(), with_logo=True)
    check("no field over 1024 characters",
          all(len(f["value"]) <= 1024 for f in busy.fields),
          max(len(f["value"]) for f in busy.fields))
    check("no field name over 256", all(len(f["name"]) <= 256 for f in busy.fields))
    check("at most 25 fields", len(busy.fields) <= 25, len(busy.fields))
    check("under the 6000 character total", busy.total_length < 6000, busy.total_length)
    check("busy dashboard is amber", busy.colour.value == B.COLOR_ACTIVE)

    B.jobs[:] = [job for job in B.jobs if job["status"] == "completed"]
    quiet = B.build_dashboard(1, FakeUser())
    check("quiet dashboard is slate", quiet.colour.value == B.COLOR_IDLE)
    check("quiet dashboard says so", "All clear" in (quiet.description or ""))

    allowed = {"id", "guild_id", "username", "service", "started", "status", "opened_by",
               "completed", "duration_seconds", "closed_by", "ig_user_id"}
    stored = set().union(*(set(job) for job in B.jobs)) if B.jobs else set()
    check("jobs.json keeps its shape", stored <= allowed, stored - allowed)

    # ------------------------------------------------------- graceful degrade
    section("With Pillow and httpx missing")
    cards.PIL_AVAILABLE = False
    cards._logo_cache = None
    instagram.HTTPX_AVAILABLE = False
    instagram._cache.clear()
    check("logo quietly skipped", B.logo_files() == [])
    bare, bare_files = asyncio.run(B.build_frame("someone"))
    check("no card, no crash", bare_files == [])
    bare_row = B.account_row(bare)
    check("still posts a safe status line",
          "Could not check" in bare_row and "ban" not in bare_row.lower(), bare_row.strip())
    bare_embed = B.house_embed(B.COLOR_ACTIVE, title="Job Opened", description=bare_row)
    check("embed still sends", bare_embed.total_length < 6000)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED:")
        for item in FAILURES:
            print("  -", item)
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
