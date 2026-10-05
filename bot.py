#!/usr/bin/env python3
"""
Zade Meadows - Discord Live Support Monitor
Hardened build: multi-server, crash-resistant, safe to run 24/7.

Setup in each server (one time, admin only):
    /setup            -> makes the current channel the live monitor channel
    /panel            -> posts a control panel anywhere you like

Everyday use (anyone allowed by your Discord permissions):
    Buttons on the dashboard, plus /newjob, /bancheck, /complete, /stats,
    /jobs, /ping
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import signal
import socket
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

import cards
import instagram
import storage

# ==========================================================================
# PATHS  -  always absolute, never depends on the working directory.
# This alone fixes "my jobs disappeared" when started from Task Scheduler.
# Nothing is created or read at import time: main() does that, so importing
# this module (the tests do) never touches the real data or log files.
# ==========================================================================

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _folder(env_name: str, default: str) -> Path:
    """Optional override (e.g. a mounted volume on a host). A relative value
    is taken relative to this file's folder, never the working directory."""
    raw = os.getenv(env_name, "").strip()
    path = Path(raw).expanduser() if raw else Path(default)
    return (path if path.is_absolute() else BASE_DIR / path).resolve()


DATA_DIR = _folder("ZM_DATA_DIR", "data")
LOG_DIR = _folder("ZM_LOG_DIR", "logs")

JOBS_FILE = DATA_DIR / "jobs.json"
CONFIG_FILE = DATA_DIR / "config.json"
LOCK_FILE = DATA_DIR / "bot.lock"
LOG_FILE = LOG_DIR / "bot.log"


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"Ignoring {name}={raw!r}: not a whole number. Using {default}.", file=sys.stderr)
        return default


TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
SYNC_GUILD_ID = os.getenv("SYNC_GUILD_ID", "").strip()
LOCK_PORT = _env_int("LOCK_PORT", 49221)          # 0 turns the port lock off
BRAND = os.getenv("BRAND_NAME", "").strip() or "Zade Meadows"
# Old jobs.json rows written before jobs carried a server id. They belong to
# this server; if unset, they are adopted only when exactly one server has
# run /setup, and otherwise stay hidden from every server.
LEGACY_GUILD_ID = os.getenv("LEGACY_GUILD_ID", "").strip()
# When Instagram's API refuses this connection, read the public profile page
# instead. Set to false to use the API only.
instagram.PAGE_FALLBACK = os.getenv("INSTAGRAM_PAGE_FALLBACK", "true").strip().lower() != "false"
# Optional: the sessionid cookie of a spare Instagram account, used only when
# Instagram refuses every anonymous read. Treat it like a password.
IG_SESSIONID_RAW = os.getenv("IG_SESSIONID", "").strip()
instagram.SESSION_ID = instagram.normalize_session_id(IG_SESSIONID_RAW)

REFRESH_SECONDS = 60          # safety-net redraw
MAX_ACTIVE_SHOWN = 10         # keep embeds under Discord's 1024-char field cap
EMBED_FIELD_LIMIT = 1000
RECENT_SHOWN = 3

# How often the automatic Unban monitor re-checks each active job, and how
# many of those checks are allowed to hit Instagram at the same time. Kept
# low on purpose - anonymous reads are what gets an IP rate limited.
# instagram.py adds a bot-wide gate and a rate-limit cooldown on top.
MONITOR_INTERVAL_SECONDS = 60
MONITOR_CONCURRENCY = 3

# Discord's hard limits. Going over any of them makes the API reject the
# whole message - for the dashboard that used to mean a silent freeze.
LIMIT_TITLE = 256
LIMIT_DESCRIPTION = 4096
LIMIT_FIELD_NAME = 256
LIMIT_FIELD_VALUE = 1024
LIMIT_FIELDS = 25
LIMIT_TOTAL = 6000

# ==========================================================================
# HOUSE STYLE
# One palette, one author line, no decorative emoji anywhere.
# The coloured bar down the left edge of an embed carries the status, so the
# text itself can stay plain and quiet. Change these four values and every
# embed in the bot changes with them.
# ==========================================================================

COLOR_IDLE = 0x8A94A6       # slate  - nothing in progress
COLOR_ACTIVE = 0xE2B04A     # amber  - work in progress
COLOR_DONE = 0x57C98B       # green  - finished
COLOR_WARN = 0xE0736B       # red    - something went wrong

USERNAME_PATTERN = instagram.HANDLE_PATTERN

# ==========================================================================
# SERVICES
# Account Support is gone. Verification and Unban are job types you open and
# later close; Ban Check is an instant look-up that opens nothing.
# Old "Account Support" rows already in jobs.json still display fine.
# ==========================================================================

SERVICE_VERIFICATION = "Instagram Verification"
SERVICE_UNBAN = "Unban"
SERVICES = (SERVICE_VERIFICATION, SERVICE_UNBAN)

# ==========================================================================
# ATTACHMENTS
# Embeds point at uploaded files with attachment:// URLs. A discord.File can
# only be uploaded once, so these helpers hand back fresh objects every time.
# ==========================================================================

LOGO_FILENAME = "logo.gif"
CARD_FILENAME = "profile.png"
LOGO_URL = f"attachment://{LOGO_FILENAME}"
CARD_URL = f"attachment://{CARD_FILENAME}"


def logo_files() -> list[discord.File]:
    """The animated logo, rendered once at start-up and cached in cards.py."""
    try:
        data = cards.animated_logo()
    except Exception as exc:
        log.error("Animated logo could not be rendered: %s", exc)
        return []
    if not data:
        return []
    return [discord.File(io.BytesIO(data), filename=LOGO_FILENAME)]


def card_files(png: Optional[bytes]) -> list[discord.File]:
    if not png:
        return []
    return [discord.File(io.BytesIO(png), filename=CARD_FILENAME)]


def clean(text: Any) -> str:
    """Usernames full of underscores turn embed text into italics. Escaping
    them keeps the layout intact without wrapping everything in code pills."""
    return discord.utils.escape_markdown(str(text))


def fit(text: str, limit: int) -> str:
    """Cut text to a Discord limit, visibly."""
    text = str(text)
    return text if len(text) <= limit else text[: max(limit - 1, 0)] + "…"


def profile_url(username: str) -> Optional[str]:
    """A title with a url renders blue and clickable, which is where the
    accent colour in the layout comes from. Only link real handles."""
    username = str(username).strip().lstrip("@")
    if USERNAME_PATTERN.fullmatch(username):
        return f"https://instagram.com/{username}"
    return None


def house_embed(
    colour: int,
    *,
    title: Optional[str] = None,
    url: Optional[str] = None,
    description: Optional[str] = None,
    icon_user: Optional[discord.abc.User] = None,
    footer: Optional[str] = None,
    stamp: bool = False,
    logo: bool = False,
    card: bool = False,
) -> discord.Embed:
    """Every embed the bot sends is built here, so they all look related."""
    embed = discord.Embed(
        colour=discord.Colour(colour),
        title=fit(title, LIMIT_TITLE) if title else None,
        url=url,
        description=fit(description, LIMIT_DESCRIPTION) if description else None,
    )
    embed.set_author(
        name=BRAND[:LIMIT_TITLE],
        icon_url=icon_user.display_avatar.url if icon_user else None,
    )
    if logo:
        embed.set_thumbnail(url=LOGO_URL)
    if card:
        embed.set_image(url=CARD_URL)
    if footer:
        embed.set_footer(text=footer)
    if stamp:
        embed.timestamp = datetime.now(timezone.utc)
    return embed


def embed_length(embed: discord.Embed) -> int:
    try:
        return len(embed)
    except TypeError:  # the offline test stand-in
        return embed.total_length


# ==========================================================================
# INSTAGRAM MINI FRAME
# One helper turns a lookup into (embed rows, attached picture). The wording
# is deliberate: a handle that does not answer is "not reachable", never
# "banned" - Instagram gives that same answer for a delete, a deactivation
# and a rename, so claiming a ban would be a guess dressed up as a fact.
# ==========================================================================

async def render_card(snapshot: instagram.Snapshot, username: str,
                      *, strict: bool = False) -> Optional[bytes]:
    """Draw the profile card off the event loop. A drawing failure means no
    picture, not a crash - unless strict, where the caller decides."""
    try:
        return await asyncio.to_thread(
            cards.render_profile_card,
            snapshot.username or username,
            full_name=snapshot.full_name,
            followers=snapshot.followers,
            following=snapshot.following,
            posts=snapshot.posts,
            verified=snapshot.verified,
            private=snapshot.private,
            avatar_bytes=snapshot.avatar,
            state=snapshot.state,
            note=snapshot.note or None,
        )
    except Exception as exc:
        if strict:
            raise
        log.exception("Profile card for @%s could not be drawn (%s); sending without it.",
                      username, exc)
        return None


async def build_frame(
    username: str, snapshot: Optional[instagram.Snapshot] = None,
) -> tuple[instagram.Snapshot, list[discord.File]]:
    # complete_job passes in the read it already retried until it was usable.
    # Every other caller passes nothing and gets the same lookup as before.
    if snapshot is None:
        snapshot = await instagram.lookup(username)
    png = await render_card(snapshot, username)
    return snapshot, card_files(png)


def account_row(snapshot: instagram.Snapshot) -> str:
    """One line about the account, safe to post in a channel."""
    bits = [snapshot.headline]
    if snapshot.state == instagram.OK:
        if snapshot.private is not None:
            bits.append("private" if snapshot.private else "public")
        if snapshot.verified:
            bits.append("verified")
    return f"**Account:** {' · '.join(bits)}"


def caveat(snapshot: instagram.Snapshot) -> str:
    """Extra line only when the answer is not a clean yes."""
    if snapshot.state == instagram.GONE:
        return ("\nInstagram answers the same way for a ban, a delete, a "
                "deactivation and a rename, so this is not proof of a ban.")
    if snapshot.state == instagram.UNKNOWN:
        return f"\n{snapshot.note}" if snapshot.note else ""
    return ""


def snapshot_colour(snapshot: instagram.Snapshot) -> int:
    return {
        instagram.OK: COLOR_DONE,
        instagram.GONE: COLOR_WARN,
    }.get(snapshot.state, COLOR_IDLE)


# ==========================================================================
# LOGGING  -  UTF-8 everywhere. Windows consoles are cp1252 by default and
# printing an emoji there raises UnicodeEncodeError, which can kill the bot.
# Handlers are attached by setup_logging() from main(), never on import.
# ==========================================================================

log = logging.getLogger("zm")
log.setLevel(logging.INFO)

_SESSION_COOKIE = re.compile(r"(sessionid=)[^;\s\"']+", re.IGNORECASE)
_TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,}")


class RedactingFormatter(logging.Formatter):
    """Belt and braces: whatever ends up in a log line, the bot token never
    does - neither the configured one nor anything shaped like one."""

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in (TOKEN, instagram.SESSION_ID, IG_SESSIONID_RAW):
            if secret and len(secret) >= 8:
                text = text.replace(secret, "[REDACTED]")
        text = _SESSION_COOKIE.sub(r"\1[REDACTED]", text)
        return _TOKEN_SHAPE.sub("[REDACTED]", text)


def setup_logging() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = RedactingFormatter("%(asctime)s | %(levelname)-7s | %(message)s", "%Y-%m-%d %H:%M:%S")

    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=5,
                                       encoding="utf-8")
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    for handler in (file_handler, console):
        log.addHandler(handler)

    discord_log = logging.getLogger("discord")
    discord_log.setLevel(logging.WARNING)
    discord_log.propagate = False
    discord_log.addHandler(file_handler)
    discord_log.addHandler(console)


# ==========================================================================
# STATE  -  one list of jobs, always filtered by server.
# storage.py does the atomic writes and the .bak fallback.
# ==========================================================================

jobs: list[dict] = []
config: dict = {}

# Last thing the monitor learned about each active Unban job, for the
# dashboard. Memory only: it is re-learned within a minute of a restart.
monitor_status: dict[tuple[str, str], dict] = {}

_save_pending = False


def migrate_legacy_jobs() -> None:
    """Import an old jobs.json that sat next to bot.py, once."""
    legacy = BASE_DIR / "jobs.json"
    if legacy.exists() and not JOBS_FILE.exists():
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
            if isinstance(data, list):
                storage.write_json(JOBS_FILE, data)
                log.info("Imported %d job(s) from the old jobs.json.", len(data))
        except Exception as exc:
            log.error("Could not import old jobs.json: %s", exc)


def load_state() -> None:
    """Read jobs.json and config.json into memory. Called once from main()."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    migrate_legacy_jobs()
    loaded_jobs = storage.read_json(JOBS_FILE, [], validate=lambda d: isinstance(d, list))
    jobs[:] = [j for j in loaded_jobs if isinstance(j, dict)]
    loaded_config = storage.read_json(CONFIG_FILE, {}, validate=lambda d: isinstance(d, dict))
    config.clear()
    config.update({str(k): v for k, v in loaded_config.items()
                   if str(k).isdigit() and isinstance(v, dict)})
    adopt_orphan_jobs()
    active = sum(1 for j in jobs if j.get("status") == "active")
    log.info("Loaded %d job(s) (%d active) and %d server configuration(s).",
             len(jobs), active, len(config))


def adopt_orphan_jobs() -> None:
    """Jobs without a server id must never be shown to every server. Give
    them to LEGACY_GUILD_ID, or to the only configured server; otherwise
    leave them hidden and say so."""
    orphans = [j for j in jobs if not str(j.get("guild_id") or "").isdigit()]
    if not orphans:
        return
    owner = LEGACY_GUILD_ID if LEGACY_GUILD_ID.isdigit() else None
    if owner is None and len(config) == 1:
        owner = next(iter(config))
    if owner is None:
        log.warning("%d old job(s) have no server recorded and are hidden from every "
                    "server. Set LEGACY_GUILD_ID in .env to the server they belong to.",
                    len(orphans))
        return
    for job in orphans:
        job["guild_id"] = owner
    if save_jobs():
        log.info("Assigned %d old job(s) without a server to server %s.", len(orphans), owner)


def save_jobs() -> bool:
    """True when jobs.json on disk now matches memory. A failure is logged
    and retried by the dashboard loop until it goes through."""
    global _save_pending
    try:
        storage.write_json(JOBS_FILE, jobs)
        _save_pending = False
        return True
    except storage.PersistenceError as exc:
        _save_pending = True
        log.error("%s - will retry.", exc)
        return False


def save_config() -> bool:
    try:
        storage.write_json(CONFIG_FILE, config)
        return True
    except storage.PersistenceError as exc:
        log.error("%s", exc)
        return False


def flush_pending_saves() -> None:
    if _save_pending:
        if save_jobs():
            log.info("A previously failed save of jobs.json has now gone through.")


def guild_config(guild_id: int) -> dict:
    return config.setdefault(str(guild_id), {})


# ==========================================================================
# HELPERS
# ==========================================================================

def guild_jobs(guild_id: Any) -> list[dict]:
    """Jobs belonging to one server, and only that server."""
    gid = str(guild_id)
    return [j for j in jobs if str(j.get("guild_id")) == gid]


def job_key(job: dict) -> tuple[str, str]:
    return (str(job.get("guild_id")), str(job.get("id")))


def next_job_id(guild_id: int) -> str:
    numbers = []
    for job in guild_jobs(guild_id):
        try:
            numbers.append(int(str(job.get("id", "")).split("-")[-1]))
        except (ValueError, IndexError):
            continue
    return f"ZM-{max(numbers, default=0) + 1:04d}"


_JOB_NUMBER = re.compile(r"(?:ZM-?)?0*(\d{1,9})")


def find_job(guild_id: int, job_id: str) -> Optional[dict]:
    """Accepts ZM-0001, zm-1, zm1, 0001 and 1."""
    raw = str(job_id).strip().upper().replace(" ", "")
    if not raw:
        return None
    records = guild_jobs(guild_id)
    exact = next((j for j in records if str(j.get("id", "")).upper() == raw), None)
    if exact is not None:
        return exact
    match = _JOB_NUMBER.fullmatch(raw)
    if match:
        wanted = f"ZM-{int(match.group(1)):04d}"
        return next((j for j in records if str(j.get("id", "")).upper() == wanted), None)
    return None


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def parse_time(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


# ==========================================================================
# DASHBOARD EMBED
# Layout: author line, plain title, one sentence of status, then the job
# rows. Nothing is decorated - the coloured bar already says what state the
# server is in (amber = jobs open, slate = all clear).
# ==========================================================================

MONITOR_STATE_TEXT = {
    instagram.OK: "Reachable · closing",
    instagram.GONE: "Not reachable yet",
    instagram.UNKNOWN: "Could not check",
    "mismatch": "Handle now on a different account",
    "unconfirmed": "Reachable · same account not confirmed",
}


def job_state_text(job: dict) -> str:
    if job.get("service") != SERVICE_UNBAN:
        return "Awaiting manual completion"
    status = monitor_status.get(job_key(job))
    if not status:
        return "Waiting for first check"
    return MONITOR_STATE_TEXT.get(status.get("state"), "Could not check")


def build_dashboard(guild_id: int, bot_user: Optional[discord.abc.User],
                    with_logo: bool = False) -> discord.Embed:
    records = guild_jobs(guild_id)
    active = [j for j in records if j.get("status") == "active"]
    completed = [j for j in records if j.get("status") == "completed"]

    today = datetime.now(timezone.utc).date()
    completed_today = sum(
        1 for j in completed
        if (t := parse_time(j.get("completed"))) and t.date() == today
    )

    durations = [j["duration_seconds"] for j in completed
                 if isinstance(j.get("duration_seconds"), (int, float))]
    average = format_duration(sum(durations) / len(durations)) if durations else "—"

    if active:
        headline = f"{len(active)} job{'s' if len(active) != 1 else ''} in progress."
    else:
        headline = "All clear. Nothing in progress."

    embed = house_embed(
        COLOR_ACTIVE if active else COLOR_IDLE,
        title="Live Monitor",
        description=headline,
        icon_user=bot_user,
        footer="Updates automatically",
        stamp=True,
        logo=with_logo,
    )

    if active:
        lines, shown = [], 0
        for job in active[:MAX_ACTIVE_SHOWN]:
            started = parse_time(job.get("started"))
            when = f" · opened <t:{int(started.timestamp())}:R>" if started else ""
            block = (
                f"**{clean(fit(job.get('id', '????'), 20))}**  ·  "
                f"@{clean(fit(job.get('username', 'unknown'), 40))}\n"
                f"{clean(fit(job.get('service', 'unknown'), 40))}{when}\n"
                f"{job_state_text(job)}\n\n"
            )
            # Hard stop before Discord's 1024-character field limit. Going over
            # it made the old dashboard silently stop updating forever.
            if sum(len(x) for x in lines) + len(block) > EMBED_FIELD_LIMIT:
                break
            lines.append(block)
            shown += 1
        if len(active) > shown:
            lines.append(f"and {len(active) - shown} more.")
        embed.add_field(
            name=f"Active · {len(active)}",
            value=fit("".join(lines).strip() or "—", LIMIT_FIELD_VALUE),
            inline=False,
        )

    recent = sorted(
        (j for j in completed if parse_time(j.get("completed"))),
        key=lambda j: parse_time(j.get("completed")),
        reverse=True,
    )[:RECENT_SHOWN]
    if recent:
        rows = []
        for job in recent:
            done = parse_time(job.get("completed"))
            took = job.get("duration_seconds")
            took_text = f" · {format_duration(took)}" if isinstance(took, (int, float)) else ""
            rows.append(
                f"**{clean(fit(job.get('id', '????'), 20))}**  ·  "
                f"@{clean(fit(job.get('username', 'unknown'), 40))}  ·  "
                f"{clean(fit(job.get('service', 'unknown'), 40))}{took_text}"
                f" · <t:{int(done.timestamp())}:R>"
            )
        embed.add_field(name="Recently completed",
                        value=fit("\n".join(rows), LIMIT_FIELD_VALUE), inline=False)

    embed.add_field(name="Completed", value=str(len(completed)), inline=True)
    embed.add_field(name="Today", value=str(completed_today), inline=True)
    embed.add_field(name="Average", value=average, inline=True)
    return embed


def dashboard_signature(guild_id: int) -> str:
    """Only redraw when something actually changed - saves rate limit budget.
    Relative timestamps update by themselves on the user's client."""
    records = guild_jobs(guild_id)
    return json.dumps(
        [(j.get("id"), j.get("status"), j.get("completed"),
          job_state_text(j) if j.get("status") == "active" else None) for j in records],
        sort_keys=True,
    )


# ==========================================================================
# BOT
# ==========================================================================

# Slash commands and buttons need only the guilds intent. message_content is
# PRIVILEGED; leave it off unless you have ticked it in the Developer
# Portal, otherwise login fails outright. Nothing in this bot reads messages.
intents = discord.Intents.none()
intents.guilds = True
if os.getenv("ENABLE_MESSAGE_CONTENT", "false").strip().lower() == "true":
    intents.guild_messages = True
    intents.message_content = True


class MonitorBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix=commands.when_mentioned, intents=intents,
                         help_command=None)
        self.signatures: dict[int, str] = {}

    async def setup_hook(self) -> None:
        # Registering the views here means buttons keep working after a
        # restart - including the retired button on very old panels.
        self.add_view(DashboardButtons())
        self.add_view(LegacyButtons())

        # The old script defined a slash command but never synced the tree,
        # so /complete never actually appeared in Discord.
        try:
            if SYNC_GUILD_ID:
                guild = discord.Object(id=int(SYNC_GUILD_ID))
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                log.info("Slash commands synced instantly to guild %s.", SYNC_GUILD_ID)
            else:
                await self.tree.sync()
                log.info("Slash commands synced globally (can take up to 1 hour).")
        except Exception as exc:
            log.error("Slash command sync failed: %s: %s", type(exc).__name__, exc)

        # Render the logo once, off the event loop, before anyone needs it.
        try:
            await asyncio.to_thread(cards.animated_logo)
        except Exception as exc:
            log.error("Animated logo could not be rendered: %s", exc)

        dashboard_loop.start()
        monitor_loop.start()


bot = MonitorBot()

# ==========================================================================
# LIVE DASHBOARD
# One lock per server: on_ready, the refresh loop, /setup, the Refresh
# button and job events can all ask for a redraw at the same moment, and
# without the lock each of them could post its own brand-new dashboard.
# ==========================================================================

_dashboard_locks: dict[int, asyncio.Lock] = {}
_dashboard_problems: dict[int, str] = {}


def _dashboard_lock(guild_id: int) -> asyncio.Lock:
    lock = _dashboard_locks.get(guild_id)
    if lock is None:
        lock = _dashboard_locks[guild_id] = asyncio.Lock()
    return lock


def _dashboard_problem(guild_id: int, message: str) -> None:
    """Log a dashboard problem once, not once a minute forever."""
    if _dashboard_problems.get(guild_id) != message:
        _dashboard_problems[guild_id] = message
        log.warning("Dashboard, guild %s: %s", guild_id, message)


def _dashboard_ok(guild_id: int) -> None:
    if _dashboard_problems.pop(guild_id, None) is not None:
        log.info("Dashboard, guild %s: working again.", guild_id)


async def resolve_monitor_channel(guild_id: Any) -> tuple[Optional[Any], str]:
    """The /setup channel for a server, or (None, reason)."""
    settings = config.get(str(guild_id))
    if not settings or not settings.get("monitor_channel_id"):
        return None, "no monitor channel set up (/setup)"
    channel_id = int(settings["monitor_channel_id"])
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.NotFound:
            return None, "the monitor channel no longer exists"
        except discord.Forbidden:
            return None, "the monitor channel is not visible to the bot"
        except discord.HTTPException as exc:
            return None, f"Discord error fetching the monitor channel: {exc}"
    owner = getattr(getattr(channel, "guild", None), "id", None)
    if owner is not None and str(owner) != str(guild_id):
        return None, "the configured channel belongs to a different server"
    return channel, ""


async def update_dashboard(guild_id: int, force: bool = False) -> bool:
    """Edit the dashboard for one server, creating it if needed. Returns
    whether the dashboard is now up to date."""
    guild_id = int(guild_id)
    async with _dashboard_lock(guild_id):
        return await _update_dashboard_locked(guild_id, force)


async def _update_dashboard_locked(guild_id: int, force: bool) -> bool:
    settings = config.get(str(guild_id))
    if not settings or not settings.get("monitor_channel_id"):
        return False

    signature = dashboard_signature(guild_id)
    if not force and bot.signatures.get(guild_id) == signature:
        return True

    channel, problem = await resolve_monitor_channel(guild_id)
    if channel is None:
        _dashboard_problem(guild_id, problem)
        return False

    view = DashboardButtons()
    message_id = settings.get("dashboard_message_id")

    if message_id:
        try:
            message = await channel.fetch_message(int(message_id))
        except discord.NotFound:
            log.info("Dashboard message in guild %s was deleted; posting a new one.", guild_id)
            settings.pop("dashboard_message_id", None)
            save_config()
            message = None
        except discord.Forbidden:
            _dashboard_problem(guild_id, "missing permission to read the dashboard message")
            return False
        except discord.HTTPException as exc:
            _dashboard_problem(guild_id, f"could not fetch the dashboard: {exc}")
            return False

        if message is not None:
            # The animated logo is re-uploaded on every redraw. Discord's own
            # attachment links expire, so re-sending the bytes is what keeps
            # the thumbnail from turning into a broken image after a day.
            files = logo_files()
            try:
                await message.edit(embed=build_dashboard(guild_id, bot.user, bool(files)),
                                   view=view, attachments=files)
            except discord.Forbidden:
                # Usually "Attach Files" is missing - the text matters more
                # than the logo, so try once without the picture.
                try:
                    await message.edit(embed=build_dashboard(guild_id, bot.user, False),
                                       view=view, attachments=[])
                except discord.HTTPException as exc:
                    _dashboard_problem(guild_id, f"missing permission to edit the dashboard ({exc})")
                    return False
            except discord.HTTPException as exc:
                _dashboard_problem(guild_id, f"dashboard edit failed: {exc}")
                return False
            bot.signatures[guild_id] = signature
            _dashboard_ok(guild_id)
            return True

    files = logo_files()
    try:
        try:
            message = await channel.send(embed=build_dashboard(guild_id, bot.user, bool(files)),
                                         view=view, files=files)
        except discord.Forbidden:
            if not files:
                raise
            message = await channel.send(embed=build_dashboard(guild_id, bot.user, False),
                                         view=view)
    except discord.Forbidden:
        _dashboard_problem(guild_id, "missing permission to post the dashboard "
                                     "(needs View Channel, Send Messages, Embed Links)")
        return False
    except discord.HTTPException as exc:
        _dashboard_problem(guild_id, f"could not post the dashboard: {exc}")
        return False

    settings["dashboard_message_id"] = message.id
    save_config()
    bot.signatures[guild_id] = signature
    _dashboard_ok(guild_id)
    log.info("Posted a new dashboard in guild %s.", guild_id)
    return True


async def safe_update_dashboard(guild_id: Any, force: bool = False) -> None:
    """For callers that have already done their real work: a dashboard
    failure is logged, never propagated."""
    try:
        await update_dashboard(int(guild_id), force=force)
    except Exception as exc:
        log.exception("Dashboard refresh failed for guild %s: %s", guild_id, exc)


def _ensure_running(loop_obj: Any, name: str) -> None:
    """Watchdog: start a background loop again if it has stopped."""
    is_running = getattr(loop_obj, "is_running", None)
    if is_running is None or is_running():
        return
    log.error("%s was not running - starting it again.", name)
    try:
        loop_obj.start()
    except RuntimeError:
        pass


async def run_dashboard_tick() -> None:
    flush_pending_saves()
    for guild_id in list(config.keys()):
        try:
            await update_dashboard(int(guild_id))
        except Exception as exc:
            # One bad server must never take the whole loop down.
            log.exception("Dashboard refresh failed for guild %s: %s", guild_id, exc)


@tasks.loop(seconds=REFRESH_SECONDS)
async def dashboard_loop() -> None:
    try:
        _ensure_running(monitor_loop, "Unban monitor")
        await run_dashboard_tick()
    except Exception as exc:
        log.exception("Dashboard loop iteration failed: %s", exc)


@dashboard_loop.before_loop
async def before_dashboard_loop() -> None:
    await bot.wait_until_ready()


@dashboard_loop.error
async def dashboard_loop_error(exc: BaseException) -> None:
    """Without this, ONE unhandled error stops the loop permanently and
    silently - the bot looks alive but the dashboard freezes. The restart is
    scheduled for after this task has fully ended, which is reliable; calling
    restart() from inside the dying task is not."""
    log.error("Dashboard loop crashed, restarting it in 5 s.", exc_info=exc)
    asyncio.get_running_loop().call_later(5, _ensure_running, dashboard_loop, "Dashboard loop")


# ==========================================================================
# AUTOMATIC UNBAN MONITOR
# /newjob no longer waits for a human to run /complete. An active Unban job
# is re-checked on a timer; the moment Instagram answers OK, the job closes
# itself with the same card and the same embed /complete has always sent.
# Verification jobs are untouched - only a person can confirm a verification,
# so they still wait for /complete exactly as before.
#
# (guild id, job id) pairs currently mid-completion, whether that completion
# was started by this monitor or by /complete. Shared with complete_job()
# below so the two can never both finish the same job.
# ==========================================================================

_completing: set[tuple[str, str]] = set()

# Consecutive card-drawing failures per job. A corrupt avatar fails the same
# way every time, so after this many tries the completion goes out without
# the picture instead of retrying forever.
CARD_RETRIES_BEFORE_PLAIN = 2
_card_failures: dict[tuple[str, str], int] = {}

_unconfigured_warned: set[str] = set()


def completion_embed(job: dict, username: str, duration: float,
                     snapshot: instagram.Snapshot, has_card: bool) -> discord.Embed:
    """The embed a finished job is announced with - identical whether a
    person ran /complete or the monitor closed the job by itself."""
    return house_embed(
        COLOR_DONE,
        title=f"Job Complete  ·  @{username}",
        url=profile_url(username),
        description=(
            f"**Service:** {clean(job.get('service', 'unknown'))}\n"
            f"**Job ID:** {job['id']}\n"
            f"**Elapsed:** {format_duration(duration)}\n"
            f"{account_row(snapshot)}{caveat(snapshot)}"
        ),
        icon_user=bot.user,
        footer="Completed",
        stamp=True,
        card=has_card,
    )


def different_account(job: dict, snapshot: instagram.Snapshot) -> bool:
    """True when the handle now belongs to a different Instagram account
    than the one this job was opened for - e.g. someone registered the
    handle after the original account vanished. Only the numeric id can
    tell; without a stored id the read is taken at face value."""
    stored = str(job.get("ig_user_id") or "")
    seen = str(snapshot.user_id or "")
    return bool(stored and seen and stored != seen)


def identity_unconfirmed(job: dict, snapshot: instagram.Snapshot) -> bool:
    """The job knows which account it is about, but this read (typically
    from the profile page) carries no numeric id to compare - so it cannot
    prove the reachable profile is still that same account."""
    return bool(job.get("ig_user_id")) and not snapshot.user_id


def identity_state(job: dict, snapshot: instagram.Snapshot) -> str:
    if different_account(job, snapshot):
        return "mismatch"
    if identity_unconfirmed(job, snapshot):
        return "unconfirmed"
    return instagram.OK


def apply_completion(job: dict, now: datetime, duration: float,
                     snapshot: instagram.Snapshot, closed_by: Optional[str] = None) -> None:
    job["status"] = "completed"
    job["completed"] = now.isoformat()
    job["duration_seconds"] = int(duration)
    if closed_by:
        job["closed_by"] = closed_by
    # No closed_by for an automatic completion: nobody closed it, and
    # closed_by is optional - nothing downstream requires it.
    if snapshot.state == instagram.OK and snapshot.user_id and not job.get("ig_user_id"):
        job["ig_user_id"] = snapshot.user_id


def record_monitor_state(job: dict, state: str, note: str = "") -> None:
    key = job_key(job)
    previous = monitor_status.get(key, {}).get("state")
    monitor_status[key] = {"state": state, "note": note,
                           "checked": datetime.now(timezone.utc).isoformat()}
    if previous == state:
        return
    if state == instagram.GONE:
        log.info("Monitor: job %s (@%s) is not reachable yet; staying active.",
                 job.get("id"), job.get("username"))
    elif state == instagram.UNKNOWN:
        log.info("Monitor: job %s (@%s) could not be checked (%s); staying active.",
                 job.get("id"), job.get("username"), note or "no detail")
    elif state == "unconfirmed":
        log.info("Monitor: job %s - @%s is reachable, but this read has no account id to "
                 "confirm it is the same account. Not completing automatically yet.",
                 job.get("id"), job.get("username"))
    elif state == "mismatch":
        log.warning("Monitor: job %s - @%s now belongs to a different Instagram account "
                    "(id changed). Not completing automatically; a person must decide.",
                    job.get("id"), job.get("username"))


async def send_completion_message(guild_id: Any, embed: discord.Embed,
                                  files: list[discord.File],
                                  plain_embed: Optional[discord.Embed] = None) -> bool:
    """Post a completion card with no interaction to reply to - the monitor
    has none. /complete keeps replying to its interaction as before; this is
    only for completions the monitor finds by itself. The only channel this
    bot remembers for a server is the one /setup made the live monitor, so an
    automatic completion is posted there, alongside the dashboard.

    Returns whether the card actually reached Discord. The caller uses this
    to decide whether the job is really finished - a completion nobody was
    ever shown must not be recorded as one."""
    channel, problem = await resolve_monitor_channel(guild_id)
    if channel is None:
        if problem.startswith("no monitor channel"):
            log.warning("Guild %s has no monitor channel set up (/setup); "
                        "automatic completion was not posted anywhere.", guild_id)
        else:
            log.warning("Guild %s: %s; could not post automatic completion.", guild_id, problem)
        return False

    try:
        await channel.send(embed=embed, files=files)
        return True
    except discord.Forbidden:
        if files and plain_embed is not None:
            # Most often the bot lacks "Attach Files" there. The completion
            # still has to be seen, so send it once more without the card.
            try:
                await channel.send(embed=plain_embed)
                log.warning("Guild %s: completion posted without its card - give the bot "
                            "Attach Files in the monitor channel.", guild_id)
                return True
            except discord.HTTPException as exc:
                log.warning("Missing permission to post an automatic completion in "
                            "guild %s: %s", guild_id, exc)
                return False
        log.warning("Missing permission to post an automatic completion in guild %s.", guild_id)
        return False
    except discord.HTTPException as exc:
        log.warning("Could not post automatic completion in guild %s: %s", guild_id, exc)
        return False


async def auto_complete_job(job: dict, snapshot: instagram.Snapshot) -> None:
    """Close one job using a snapshot the monitor already confirmed is OK.
    Guarded the same way /complete is guarded, so a job can never be
    finished twice no matter which of the two triggers it.

    The card is built and delivered BEFORE anything is written to jobs.json.
    Either can fail - Pillow can choke on a corrupt avatar, Discord can be
    down, the channel can be gone - and none of those failures may leave a
    job permanently marked completed with no card ever shown for it: there is
    no /reopen command, so that state could only be fixed by hand. Leaving
    the job active instead costs at most a few extra checks: _completing is
    released in the `finally` below either way, so the very next monitor
    tick simply tries the whole thing again once whatever failed recovers.

    The opposite ordering - save "completed" first, notify after - was
    rejected: it fails the same way in the more common case (a rendering or
    Discord error) while only avoiding a much rarer one (the process being
    killed in the instant between a successful send and the save that
    follows it here). That narrow case can, at worst, repeat one already-
    delivered notification once the job is retried - a harmless duplicate,
    and a far smaller problem than a job stuck "completed" forever with
    nothing ever posted for it."""
    guild_id = job.get("guild_id")
    key = job_key(job)
    if key in _completing:
        return
    _completing.add(key)
    try:
        if job.get("status") != "active":
            return  # already finished by /complete while this read was in flight
        if snapshot.state != instagram.OK:
            return  # only a confirmed reachable profile closes an Unban job
        identity = identity_state(job, snapshot)
        if identity != instagram.OK:
            record_monitor_state(job, identity)
            return

        now = datetime.now(timezone.utc)
        started = parse_time(job.get("started")) or now
        duration = max((now - started).total_seconds(), 0)
        username = str(job.get("username", "unknown"))

        failures = _card_failures.get(key, 0)
        try:
            png = await render_card(snapshot, username,
                                    strict=failures < CARD_RETRIES_BEFORE_PLAIN)
        except Exception as exc:
            _card_failures[key] = failures + 1
            log.exception("Job %s: could not build the completion card (%s). "
                          "Left active - will retry on the next check.", job.get("id"), exc)
            return
        files = card_files(png)

        embed = completion_embed(job, username, duration, snapshot, bool(files))
        plain = completion_embed(job, username, duration, snapshot, False) if files else None
        delivered = await send_completion_message(guild_id, embed, files, plain)
        if not delivered:
            log.error("Job %s: the completion card could not be delivered. "
                      "Left active - will retry once the channel or Discord recovers.",
                      job.get("id"))
            return

        # Only now, with the card actually shown, is the job recorded as done.
        apply_completion(job, now, duration, snapshot)
        _card_failures.pop(key, None)
        monitor_status.pop(key, None)
        if not save_jobs():
            log.error("Job %s: completion was announced but jobs.json could not be "
                      "saved yet; the save will be retried.", job.get("id"))

        log.info("Job %s automatically completed in guild %s - Instagram now reachable.",
                 job["id"], guild_id)
        await safe_update_dashboard(guild_id)
    finally:
        _completing.discard(key)


async def check_job(job: dict, semaphore: asyncio.Semaphore) -> None:
    """One monitor tick's look at one job. A read that comes back UNKNOWN or
    GONE changes nothing - the job just gets checked again on the next tick,
    which is this monitor's whole retry strategy. Only OK is a real unban."""
    key = job_key(job)
    if key in _completing:
        return
    try:
        async with semaphore:
            snapshot = await instagram.lookup(str(job.get("username", "")))

        if job.get("status") != "active":
            return
        if snapshot.state != instagram.OK:
            # GONE or UNKNOWN - not an answer to "is it back?"; try again
            # next tick. UNKNOWN is never turned into GONE.
            record_monitor_state(job, snapshot.state, snapshot.note)
            return

        record_monitor_state(job, identity_state(job, snapshot))
        await auto_complete_job(job, snapshot)
    except Exception as exc:
        # A broken job record or an unexpected error later must not be
        # allowed to take the whole loop down.
        log.exception("Monitor: checking job %s failed: %s", job.get("id"), exc)


def monitor_candidates() -> list[dict]:
    candidates = []
    for job in jobs:
        if job.get("status") != "active" or job.get("service") != SERVICE_UNBAN:
            continue
        gid = str(job.get("guild_id") or "")
        if not gid.isdigit():
            continue  # unassigned legacy job - nowhere it could be announced
        if not (config.get(gid) or {}).get("monitor_channel_id"):
            if gid not in _unconfigured_warned:
                _unconfigured_warned.add(gid)
                log.warning("Guild %s has active Unban jobs but no monitor channel; "
                            "they are not checked until someone runs /setup there.", gid)
            continue
        _unconfigured_warned.discard(gid)
        candidates.append(job)
    return candidates


async def run_monitor_tick() -> None:
    candidates = monitor_candidates()
    if not candidates:
        return
    semaphore = asyncio.Semaphore(MONITOR_CONCURRENCY)
    results = await asyncio.gather(*(check_job(job, semaphore) for job in candidates),
                                   return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
            log.error("Monitor: a job check failed unexpectedly.", exc_info=result)


@tasks.loop(seconds=MONITOR_INTERVAL_SECONDS)
async def monitor_loop() -> None:
    try:
        _ensure_running(dashboard_loop, "Dashboard loop")
        await run_monitor_tick()
    except Exception as exc:
        log.exception("Unban monitor iteration failed: %s", exc)


@monitor_loop.before_loop
async def before_monitor_loop() -> None:
    await bot.wait_until_ready()


@monitor_loop.error
async def monitor_loop_error(exc: BaseException) -> None:
    """Same reasoning as the dashboard loop: one unhandled error must not
    silently stop every job from ever being checked again."""
    log.error("Unban monitor crashed, restarting it in 5 s.", exc_info=exc)
    asyncio.get_running_loop().call_later(5, _ensure_running, monitor_loop, "Unban monitor")


# ==========================================================================
# JOB ACTIONS
# ==========================================================================

BAD_HANDLE_MESSAGE = ("That does not look like an Instagram username - letters, numbers, "
                      "`.` and `_` only, at most 30 characters. A profile link works too.")


async def create_job(interaction: discord.Interaction, username: str, service: str) -> None:
    if interaction.guild_id is None:
        await interaction.response.send_message("Use this inside a server.", ephemeral=True)
        return

    if not str(username).strip().lstrip("@"):
        await interaction.response.send_message("Please enter a username.", ephemeral=True)
        return
    handle = instagram.normalize_handle(username)
    if handle is None:
        await interaction.response.send_message(BAD_HANDLE_MESSAGE, ephemeral=True)
        return
    if service not in SERVICES:
        await interaction.response.send_message("Unknown service.", ephemeral=True)
        return

    # Reading a profile takes a few seconds and Discord only gives us three
    # before it shows "This interaction failed", so acknowledge first and
    # send the real answer as a follow-up.
    await interaction.response.defer(ephemeral=True)

    duplicate = next((j for j in guild_jobs(interaction.guild_id)
                      if j.get("status") == "active" and j.get("service") == service
                      and str(j.get("username", "")).lower() == handle.lower()), None)

    job = {
        "id": next_job_id(interaction.guild_id),
        "guild_id": str(interaction.guild_id),
        "username": handle,
        "service": service,
        "started": datetime.now(timezone.utc).isoformat(),
        "status": "active",
        "opened_by": str(interaction.user.id),
    }
    jobs.append(job)
    if not save_jobs():
        jobs.remove(job)
        await interaction.followup.send(
            "The job could not be saved, so it was not opened. Please try again.",
            ephemeral=True)
        return

    snapshot = await instagram.lookup(handle)
    if snapshot.user_id:
        # Instagram's numeric id never changes. Keeping it is the only way to
        # tell later whether a vanished handle was renamed or actually lost.
        job["ig_user_id"] = snapshot.user_id
        save_jobs()
    if service == SERVICE_UNBAN and snapshot.state != instagram.OK:
        record_monitor_state(job, snapshot.state, snapshot.note)
    files = card_files(await render_card(snapshot, handle))

    notes = []
    if duplicate is not None:
        notes.append(f"\nNote: {duplicate['id']} is already open for @{clean(handle)}.")
    if service == SERVICE_UNBAN and not (config.get(str(interaction.guild_id)) or {}).get(
            "monitor_channel_id"):
        notes.append("\nAn admin needs to run `/setup` before Unban jobs are checked "
                     "and closed automatically.")

    embed = house_embed(
        COLOR_ACTIVE,
        title=f"Job Opened  ·  @{handle}",
        url=profile_url(handle),
        description=(
            f"**Service:** {clean(service)}\n"
            f"**Job ID:** {job['id']}\n"
            f"{account_row(snapshot)}{caveat(snapshot)}{''.join(notes)}"
        ),
        icon_user=interaction.client.user,
        footer="Opened",
        stamp=True,
        card=bool(files),
    )
    try:
        await interaction.followup.send(embed=embed, files=files, ephemeral=True)
    except discord.HTTPException as exc:
        log.warning("Job %s opened, but the confirmation could not be sent: %s", job["id"], exc)
    log.info("Job %s (%s, @%s) opened in guild %s by %s", job["id"], service, handle,
             interaction.guild_id, interaction.user)
    await safe_update_dashboard(interaction.guild_id)

    if service == SERVICE_UNBAN and snapshot.state == instagram.OK:
        # The account is already reachable, so the one thing an Unban job
        # waits for has already happened. Close it now rather than making
        # the monitor wait for its next tick to notice the same answer.
        await auto_complete_job(job, snapshot)


# Closing a job re-reads the account so the card shows real numbers. Instagram
# throttles anonymous reads, and a throttled read is UNKNOWN - not an answer -
# so it is retried before giving up. Each entry is the wait before one retry:
# 5 retries after the first read, at most 125 s of waiting in total.
FINAL_LOOKUP_DELAYS = (5, 10, 20, 30, 60)

# _completing is defined once, above, next to the automatic monitor - both it
# and /complete share the same set, so a job can never be closed twice.


async def final_snapshot(username: str, job_id: str) -> instagram.Snapshot:
    """Read the account for a closing card, retrying only while Instagram
    cannot answer. Returns the first snapshot that is OK or GONE (both are real
    answers). If every read is UNKNOWN, returns the last one - the caller must
    then leave the job open. Waits with asyncio.sleep, so other jobs, buttons
    and the dashboard keep running during the backoff."""
    retries = 0
    snapshot = await instagram.lookup(username)
    while snapshot.state == instagram.UNKNOWN and retries < len(FINAL_LOOKUP_DELAYS):
        if not instagram.HTTPX_AVAILABLE:
            break  # not temporary: waiting will never make httpx appear
        if instagram.normalize_handle(username) is None:
            break  # not temporary either: the handle itself is invalid
        if instagram.cooldown_remaining() > sum(FINAL_LOOKUP_DELAYS[retries:]):
            break  # Instagram asked for a longer pause than we would wait
        delay = FINAL_LOOKUP_DELAYS[retries]
        retries += 1
        log.warning("Job %s: read of @%s was not usable (%s) - retry %d/%d in %ds.",
                    job_id, username, snapshot.note or "no detail",
                    retries, len(FINAL_LOOKUP_DELAYS), delay)
        await asyncio.sleep(delay)
        snapshot = await instagram.lookup(username)

    if snapshot.state == instagram.UNKNOWN:
        log.error("Job %s: could not read @%s after %d attempt(s) (%s). "
                  "The job was NOT completed and stays active.",
                  job_id, username, retries + 1, snapshot.note or "no detail")
    elif retries:
        log.info("Job %s: read of @%s succeeded after %d retries.", job_id, username, retries)
    return snapshot


async def complete_job(interaction: discord.Interaction, raw_id: str) -> None:
    if interaction.guild_id is None:
        await interaction.response.send_message("Use this inside a server.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    job = find_job(interaction.guild_id, raw_id)
    if job is None:
        await interaction.followup.send(
            f"Job `{fit(str(raw_id).strip().upper(), 40)}` was not found in this server.",
            ephemeral=True,
        )
        return

    if job.get("status") != "active":
        await interaction.followup.send(
            f"`{job['id']}` is already completed.", ephemeral=True
        )
        return

    key = job_key(job)
    if key in _completing:
        await interaction.followup.send(
            f"`{job['id']}` is already being closed - give it a moment.", ephemeral=True
        )
        return

    # Elapsed time is fixed at the moment the job is closed, exactly as before.
    # Time spent waiting on Instagram below is not part of the job's duration.
    now = datetime.now(timezone.utc)
    started = parse_time(job.get("started")) or now
    duration = max((now - started).total_seconds(), 0)
    username = str(job.get("username", "unknown"))

    # The guard is held from here until the job is saved, so the monitor
    # cannot announce the same job while this one is in progress.
    _completing.add(key)
    try:
        if instagram.normalize_handle(username) is None:
            # Old rows can hold something that was never a handle (one holds
            # a job ID). There is nothing to read; let the person close it.
            snapshot = instagram.Snapshot(username, instagram.UNKNOWN,
                                          note="No Instagram profile to read for this job.")
        else:
            # Re-read the account on the way out, so the closing card shows the
            # state you actually delivered rather than the one you started
            # with. A throttled read (UNKNOWN) must never close a job.
            snapshot = await final_snapshot(username, job["id"])
            if snapshot.state == instagram.UNKNOWN:
                # Nothing has been saved, so the job is still active and can
                # simply be closed again once Instagram answers.
                await interaction.followup.send(
                    f"`{job['id']}` was **not** completed. "
                    f"{snapshot.note or 'Instagram could not be read.'}\n"
                    f"The job is still active - run `/complete {job['id']}` again "
                    f"in a few minutes.",
                    ephemeral=True,
                )
                return

        if job.get("status") != "active":
            await interaction.followup.send(f"`{job['id']}` is already completed.",
                                            ephemeral=True)
            return

        files = card_files(await render_card(snapshot, username))
        embed = completion_embed(job, username, duration, snapshot, bool(files))
        if different_account(job, snapshot):
            embed.description = fit(
                (embed.description or "") + "\nNote: this handle now belongs to a different "
                "Instagram account than when the job was opened.", LIMIT_DESCRIPTION)

        before = dict(job)
        apply_completion(job, now, duration, snapshot, closed_by=str(interaction.user.id))
        if not save_jobs():
            job.clear()
            job.update(before)
            await interaction.followup.send(
                f"`{job['id']}` could not be saved, so it is still active. "
                f"Please try again in a moment.", ephemeral=True)
            return
        monitor_status.pop(key, None)
    finally:
        _completing.discard(key)

    try:
        await interaction.followup.send(embed=embed, files=files, ephemeral=True)
    except discord.HTTPException as exc:
        log.warning("Job %s completed, but the confirmation could not be sent: %s",
                    job["id"], exc)
    log.info("Job %s completed in guild %s by %s", job["id"], interaction.guild_id,
             interaction.user)
    await safe_update_dashboard(interaction.guild_id)


async def run_ban_check(interaction: discord.Interaction, username: str) -> None:
    """A look-up, not a job. Nothing is stored and nothing is opened."""
    if not str(username).strip().lstrip("@"):
        await interaction.response.send_message("Please enter a username.", ephemeral=True)
        return
    handle = instagram.normalize_handle(username)
    if handle is None:
        await interaction.response.send_message(BAD_HANDLE_MESSAGE, ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    snapshot, files = await build_frame(handle)

    embed = house_embed(
        snapshot_colour(snapshot),
        title=f"Ban Check  ·  @{handle}",
        url=profile_url(handle),
        description=f"{account_row(snapshot)}{caveat(snapshot)}",
        icon_user=interaction.client.user,
        footer="Checked",
        stamp=True,
        card=bool(files),
    )
    await interaction.followup.send(embed=embed, files=files, ephemeral=True)
    log.info("Ban check on %s in guild %s -> %s", handle, interaction.guild_id, snapshot.state)


# ==========================================================================
# MODALS
# ==========================================================================

class UsernameModal(discord.ui.Modal):
    def __init__(self, service: str) -> None:
        super().__init__(title=f"{service} - New Job"[:45])
        self.service = service
        self.username = discord.ui.TextInput(
            label="Instagram username", placeholder="example_username",
            required=True, max_length=100,
        )
        self.add_item(self.username)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await create_job(interaction, self.username.value, self.service)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("New-job modal error: %s", error)
        await safe_error_reply(interaction)


class BanCheckModal(discord.ui.Modal, title="Ban Check"):
    username = discord.ui.TextInput(
        label="Instagram username", placeholder="example_username",
        required=True, max_length=100,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await run_ban_check(interaction, self.username.value)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Ban-check modal error: %s", error)
        await safe_error_reply(interaction)


class CompleteModal(discord.ui.Modal, title="Complete Job"):
    job_id = discord.ui.TextInput(label="Job ID", placeholder="ZM-0001", required=True, max_length=20)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await complete_job(interaction, self.job_id.value)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Complete-job modal error: %s", error)
        await safe_error_reply(interaction)


async def safe_error_reply(interaction: discord.Interaction) -> None:
    await safe_reply(interaction,
                     "Something went wrong, but the bot is still running. Please try again.")


async def safe_reply(interaction: discord.Interaction, message: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


# ==========================================================================
# BUTTONS  -  custom_id + timeout=None makes them survive restarts.
# Labels carry no emoji; one accent button, the rest quiet grey.
# The custom_ids are unchanged, so panels already posted keep working.
# ==========================================================================

class DashboardButtons(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Verification",
                       style=discord.ButtonStyle.primary, custom_id="zm_verification")
    async def verification_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(UsernameModal(SERVICE_VERIFICATION))

    @discord.ui.button(label="Unban",
                       style=discord.ButtonStyle.secondary, custom_id="zm_unban")
    async def unban_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(UsernameModal(SERVICE_UNBAN))

    @discord.ui.button(label="Ban Check",
                       style=discord.ButtonStyle.secondary, custom_id="zm_ban_check")
    async def ban_check_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(BanCheckModal())

    @discord.ui.button(label="Complete",
                       style=discord.ButtonStyle.secondary, custom_id="zm_complete_job")
    async def complete_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(CompleteModal())

    @discord.ui.button(label="Refresh",
                       style=discord.ButtonStyle.secondary, custom_id="zm_refresh")
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            ok = bool(interaction.guild_id) and await update_dashboard(
                interaction.guild_id, force=True)
            await interaction.followup.send(
                "Monitor refreshed." if ok else
                "The monitor could not be refreshed - has an admin run `/setup`, and can "
                "the bot post in that channel?", ephemeral=True)
        except Exception as exc:
            log.exception("Refresh failed: %s", exc)
            await safe_error_reply(interaction)

    async def on_error(self, interaction: discord.Interaction,
                       error: Exception, item: discord.ui.Item) -> None:
        log.exception("Button error on %s: %s", item, error)
        await safe_error_reply(interaction)


class LegacyButtons(discord.ui.View):
    """Panels posted by the oldest build still carry an Account Support
    button. It answers politely instead of 'This interaction failed'."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Account Support",
                       style=discord.ButtonStyle.secondary, custom_id="zm_account_support")
    async def legacy_account_support(self, interaction: discord.Interaction,
                                     button: discord.ui.Button) -> None:
        await interaction.response.send_message(
            "This panel is out of date - Account Support was retired. "
            "Ask an admin to run `/panel` for the current buttons.", ephemeral=True)


# ==========================================================================
# SLASH COMMANDS
# ==========================================================================

async def remove_old_dashboard(channel_id: Any, message_id: Any) -> None:
    """Best effort: a dashboard that /setup replaced should not linger as a
    second, frozen copy."""
    try:
        channel = bot.get_channel(int(channel_id)) or await bot.fetch_channel(int(channel_id))
        message = await channel.fetch_message(int(message_id))
        await message.delete()
        log.info("Removed the previous dashboard message %s.", message_id)
    except Exception as exc:
        log.info("Previous dashboard %s was not removed (%s).", message_id, exc)


@bot.tree.command(name="setup", description="Make this channel the live monitor channel")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
async def setup_command(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    settings = guild_config(interaction.guild_id)
    old_channel = settings.get("monitor_channel_id")
    old_message = settings.get("dashboard_message_id")

    if str(old_channel) != str(interaction.channel_id):
        settings["monitor_channel_id"] = interaction.channel_id
        settings.pop("dashboard_message_id", None)
        if old_channel and old_message:
            await remove_old_dashboard(old_channel, old_message)
    # Same channel again: keep the existing dashboard message instead of
    # stacking a second one under it.
    save_config()
    bot.signatures.pop(interaction.guild_id, None)
    log.info("Guild %s: monitor channel set to %s by %s.", interaction.guild_id,
             interaction.channel_id, interaction.user)

    ok = await update_dashboard(interaction.guild_id, force=True)
    await interaction.followup.send(
        "This channel is now the live monitor. The dashboard is below and updates "
        "itself automatically." if ok else
        "This channel is now the live monitor, but the dashboard could not be posted. "
        "Give the bot View Channel, Send Messages, Embed Links and Attach Files here, "
        "then press Refresh or run `/setup` again.",
        ephemeral=True,
    )


@bot.tree.command(name="panel", description="Post the control panel in this channel")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
async def panel_command(interaction: discord.Interaction) -> None:
    files = logo_files()
    embed = house_embed(
        COLOR_IDLE,
        title="Control Panel",
        description=(
            "**Verification** — open an Instagram verification job\n"
            "**Unban** — open a job for an account you are getting back\n"
            "**Ban Check** — look a handle up right now, opens nothing\n"
            "**Complete** — close an active job using its ID\n"
            "**Refresh** — redraw the live monitor now"
        ),
        icon_user=bot.user,
        footer=BRAND,
        logo=bool(files),
    )
    await interaction.response.send_message(embed=embed, view=DashboardButtons(), files=files)


@bot.tree.command(name="newjob", description="Open a new job")
@app_commands.guild_only()
@app_commands.describe(username="Instagram username", service="Type of service")
@app_commands.choices(service=[
    app_commands.Choice(name=SERVICE_VERIFICATION, value=SERVICE_VERIFICATION),
    app_commands.Choice(name=SERVICE_UNBAN, value=SERVICE_UNBAN),
])
async def newjob_command(interaction: discord.Interaction, username: str,
                         service: app_commands.Choice[str]) -> None:
    await create_job(interaction, username, service.value)


@bot.tree.command(name="bancheck", description="Check whether a handle is reachable right now")
@app_commands.guild_only()
@app_commands.describe(username="Instagram username")
@app_commands.checks.cooldown(5, 60.0)
async def bancheck_command(interaction: discord.Interaction, username: str) -> None:
    await run_ban_check(interaction, username)


@bot.tree.command(name="complete", description="Mark an active job as completed")
@app_commands.guild_only()
@app_commands.describe(job_id="The Job ID, for example ZM-0001 or just 1")
async def complete_command(interaction: discord.Interaction, job_id: str) -> None:
    await complete_job(interaction, job_id)


def jobs_list_embed(guild_id: int) -> Optional[discord.Embed]:
    active = [j for j in guild_jobs(guild_id) if j.get("status") == "active"]
    if not active:
        return None

    lines = []
    for job in active[:15]:
        started = parse_time(job.get("started"))
        when = f" · opened <t:{int(started.timestamp())}:R>" if started else ""
        lines.append(
            f"**{clean(fit(job.get('id', '????'), 20))}**  ·  "
            f"@{clean(fit(job.get('username', 'unknown'), 40))}\n"
            f"{clean(fit(job.get('service', 'unknown'), 40))}{when} · {job_state_text(job)}"
        )
    if len(active) > 15:
        lines.append(f"and {len(active) - 15} more.")

    return house_embed(
        COLOR_ACTIVE,
        title="Active Jobs",
        description="\n\n".join(lines),
        icon_user=bot.user,
        footer=f"{len(active)} in progress",
    )


@bot.tree.command(name="jobs", description="List the active jobs in this server")
@app_commands.guild_only()
async def jobs_command(interaction: discord.Interaction) -> None:
    embed = jobs_list_embed(interaction.guild_id)
    if embed is None:
        await interaction.response.send_message("Nothing in progress.", ephemeral=True)
        return
    await interaction.response.send_message(embed=embed, ephemeral=True)


def stats_embed(guild_id: int) -> discord.Embed:
    records = guild_jobs(guild_id)
    active = sum(1 for j in records if j.get("status") == "active")
    completed = [j for j in records if j.get("status") == "completed"]
    durations = [j["duration_seconds"] for j in completed
                 if isinstance(j.get("duration_seconds"), (int, float))]

    embed = house_embed(
        COLOR_ACTIVE if active else COLOR_IDLE,
        title="Statistics",
        icon_user=bot.user,
        footer=BRAND,
        stamp=True,
    )
    embed.add_field(name="Total", value=str(len(records)), inline=True)
    embed.add_field(name="Active", value=str(active), inline=True)
    embed.add_field(name="Completed", value=str(len(completed)), inline=True)
    for service in SERVICES:
        count = sum(1 for j in records if j.get("service") == service)
        embed.add_field(name=service, value=str(count), inline=True)
    embed.add_field(name="Average",
                    value=format_duration(sum(durations) / len(durations)) if durations else "—",
                    inline=True)
    return embed


@bot.tree.command(name="stats", description="Show job statistics for this server")
@app_commands.guild_only()
async def stats_command(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(embed=stats_embed(interaction.guild_id))


@bot.tree.command(name="ping", description="Check that the bot is alive")
async def ping_command(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        f"Online · {round(bot.latency * 1000)} ms", ephemeral=True
    )


@bot.tree.error
async def on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        await safe_reply(interaction, "You need the **Manage Server** permission for that.")
        return
    if isinstance(error, app_commands.CommandOnCooldown):
        await safe_reply(interaction, f"Slow down a little - try again in "
                                      f"{max(1, round(error.retry_after))} s.")
        return
    if isinstance(error, app_commands.NoPrivateMessage):
        await safe_reply(interaction, "Use this inside a server.")
        return
    log.error("Slash command error: %s", error, exc_info=error)
    await safe_error_reply(interaction)


# ==========================================================================
# LIFECYCLE
# ==========================================================================

@bot.event
async def on_ready() -> None:
    log.info("Online as %s in %d server(s).", bot.user, len(bot.guilds))
    try:
        await bot.change_presence(activity=discord.Activity(
            type=discord.ActivityType.watching, name="support jobs"
        ))
    except Exception as exc:
        log.warning("Could not set presence: %s", exc)
    # Re-attach to every saved dashboard (or recreate a deleted one). The
    # per-server lock makes this safe alongside the refresh loop.
    for guild in bot.guilds:
        if str(guild.id) in config:
            await safe_update_dashboard(guild.id, force=True)


@bot.event
async def on_connect() -> None:
    log.info("Connected to Discord.")


@bot.event
async def on_disconnect() -> None:
    log.warning("Disconnected from Discord; discord.py will reconnect.")


@bot.event
async def on_resumed() -> None:
    log.info("Reconnected to Discord.")


@bot.event
async def on_guild_join(guild: discord.Guild) -> None:
    log.info("Added to a new server: %s (%s)", guild.name, guild.id)


@bot.event
async def on_guild_remove(guild: discord.Guild) -> None:
    log.info("Removed from server %s (%s). Its jobs and settings are kept.", guild.name, guild.id)


# ==========================================================================
# SINGLE INSTANCE GUARD
# Two copies of the bot fighting over jobs.json is a classic cause of
# duplicate dashboards and lost records. Two locks: the original localhost
# port (one bot per machine, LOCK_PORT=0 turns it off) and an OS file lock
# on the data folder itself (one bot per set of data files, always on).
# ==========================================================================

_lock_socket: Optional[socket.socket] = None
_data_lock = storage.DataLock(LOCK_FILE)


def acquire_lock() -> bool:
    global _lock_socket
    if LOCK_PORT <= 0:
        return True
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", LOCK_PORT))
        sock.listen(1)
        _lock_socket = sock
        return True
    except OSError:
        sock.close()
        return False


def release_locks() -> None:
    global _lock_socket
    if _lock_socket is not None:
        _lock_socket.close()
        _lock_socket = None
    _data_lock.release()


async def run_bot() -> None:
    """bot.start() under our own event loop, so SIGTERM (systemd stop, a
    container shutdown) closes the Discord connection cleanly."""
    loop = asyncio.get_running_loop()

    def request_stop(sig: signal.Signals) -> None:
        log.info("Received %s - shutting down.", sig.name)
        asyncio.ensure_future(bot.close())

    if os.name != "nt":
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, request_stop, sig)
            except (NotImplementedError, RuntimeError):
                pass

    async with bot:
        await bot.start(TOKEN, reconnect=True)


def main() -> int:
    setup_logging()
    log.info("Starting %s monitor (pid %d, data in %s).", BRAND, os.getpid(), DATA_DIR)

    if not TOKEN:
        log.error("DISCORD_TOKEN is missing. Put it in a .env file next to bot.py.")
        return 2

    if instagram.SESSION_ID:
        log.info("Instagram: logged-in session configured; used only when anonymous "
                 "reads are refused.")
    elif IG_SESSIONID_RAW:
        log.error("IG_SESSIONID in .env does not look like an Instagram sessionid cookie; "
                  "logged-in reads are off. Copy only the cookie's value.")

    if not acquire_lock():
        log.error("Another copy of this bot is already running on this machine "
                  "(port %s is taken). Exiting.", LOCK_PORT)
        return 3
    if not _data_lock.acquire():
        log.error("Another copy of this bot is already using %s. Exiting.", DATA_DIR)
        release_locks()
        return 3

    try:
        load_state()
        asyncio.run(run_bot())
    except discord.LoginFailure:
        log.error(
            "Discord rejected the token. It was most likely reset because it leaked. "
            "Generate a new one in the Developer Portal and update your .env file."
        )
        return 4
    except discord.PrivilegedIntentsRequired:
        log.error(
            "A privileged intent is not enabled. Either tick MESSAGE CONTENT INTENT in the "
            "Developer Portal, or set ENABLE_MESSAGE_CONTENT=false in your .env."
        )
        return 5
    except KeyboardInterrupt:
        log.info("Stopped by user.")
        return 0
    except Exception as exc:
        log.exception("Unhandled crash: %s: %s", type(exc).__name__, exc)
        return 1
    finally:
        if _save_pending and not save_jobs():
            log.error("jobs.json still has unsaved changes at shutdown.")
        release_locks()
        log.info("Shut down.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
