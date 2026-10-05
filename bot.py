#!/usr/bin/env python3
"""
Zade Meadows - Discord Live Support Monitor
Hardened build: multi-server, crash-resistant, safe to run 24/7.

Setup in each server (one time, admin only):
    /setup            -> makes the current channel the live monitor channel
    /panel            -> posts a control panel anywhere you like

Everyday use (anyone allowed by your Discord permissions):
    Buttons on the dashboard, plus /newjob, /complete, /stats, /jobs, /ping
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import socket
import sys
import time
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

# ==========================================================================
# PATHS  -  always absolute, never depends on the working directory.
# This alone fixes "my jobs disappeared" when started from Task Scheduler.
# ==========================================================================

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"

DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

JOBS_FILE = DATA_DIR / "jobs.json"
CONFIG_FILE = DATA_DIR / "config.json"
LOG_FILE = LOG_DIR / "bot.log"

load_dotenv(BASE_DIR / ".env")

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
SYNC_GUILD_ID = os.getenv("SYNC_GUILD_ID", "").strip()
LOCK_PORT = int(os.getenv("LOCK_PORT", "49221"))
BRAND = os.getenv("BRAND_NAME", "Zade Meadows")

REFRESH_SECONDS = 60          # safety-net redraw
MAX_ACTIVE_SHOWN = 10         # keep embeds under Discord's 1024-char field cap
EMBED_FIELD_LIMIT = 1000

# How often the automatic Unban monitor re-checks each active job, and how
# many of those checks are allowed to hit Instagram at the same time. Kept
# low on purpose - anonymous reads are what gets an IP rate limited.
MONITOR_INTERVAL_SECONDS = 60
MONITOR_CONCURRENCY = 3

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

USERNAME_PATTERN = re.compile(r"[A-Za-z0-9._]{1,30}")

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
    data = cards.animated_logo()
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
        colour=discord.Colour(colour), title=title, url=url, description=description
    )
    embed.set_author(
        name=BRAND,
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


# ==========================================================================
# INSTAGRAM MINI FRAME
# One helper turns a lookup into (embed rows, attached picture). The wording
# is deliberate: a handle that does not answer is "not reachable", never
# "banned" - Instagram gives that same answer for a delete, a deactivation
# and a rename, so claiming a ban would be a guess dressed up as a fact.
# ==========================================================================

async def build_frame(
    username: str, snapshot: Optional[instagram.Snapshot] = None,
) -> tuple[instagram.Snapshot, list[discord.File]]:
    # complete_job passes in the read it already retried until it was usable.
    # Every other caller passes nothing and gets the same lookup as before.
    if snapshot is None:
        snapshot = await instagram.lookup(username)
    png = cards.render_profile_card(
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
    return snapshot, card_files(png)


def account_row(snapshot: instagram.Snapshot) -> str:
    """One line about the account, safe to post in a channel."""
    bits = [snapshot.headline]
    if snapshot.state == instagram.OK:
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
# ==========================================================================

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

log = logging.getLogger("zm")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", "%Y-%m-%d %H:%M:%S")

_file_handler = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
_file_handler.setFormatter(_fmt)
log.addHandler(_file_handler)

_console = logging.StreamHandler(sys.stdout)
_console.setFormatter(_fmt)
log.addHandler(_console)

logging.getLogger("discord").setLevel(logging.WARNING)
logging.getLogger("discord").addHandler(_file_handler)


# ==========================================================================
# STORAGE  -  atomic writes + automatic backup.
# A normal open(..., "w") truncates the file first. If the process dies in
# that window (PC sleeps, power cut, Ctrl-C) you are left with a half-written
# or empty jobs.json, which is why the data kept needing manual repair.
# ==========================================================================

def _read_json(path: Path, default: Any) -> Any:
    for candidate in (path, path.with_suffix(path.suffix + ".bak")):
        if not candidate.exists():
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if candidate != path:
                log.warning("%s was unreadable; recovered from backup.", path.name)
            return data
        except Exception as exc:
            log.error("Could not read %s (%s: %s)", candidate.name, type(exc).__name__, exc)

    if path.exists():
        quarantine = path.with_name(f"{path.stem}.corrupt-{int(time.time())}{path.suffix}")
        try:
            path.rename(quarantine)
            log.error("Moved damaged file to %s - starting fresh.", quarantine.name)
        except Exception:
            pass
    return default


def _write_json(path: Path, payload: Any) -> None:
    """Write to a temp file, flush to disk, then swap it in. Never truncates
    the live file, so a crash mid-write cannot destroy your data."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    backup = path.with_suffix(path.suffix + ".bak")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            try:
                backup.unlink(missing_ok=True)
                path.replace(backup)
            except Exception:
                pass
        os.replace(tmp, path)
    except Exception as exc:
        log.exception("Failed to save %s: %s", path.name, exc)
    finally:
        tmp.unlink(missing_ok=True)


def migrate_legacy_jobs() -> None:
    """Import an old jobs.json that sat next to bot.py, once."""
    legacy = BASE_DIR / "jobs.json"
    if legacy.exists() and not JOBS_FILE.exists():
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
            if isinstance(data, list):
                _write_json(JOBS_FILE, data)
                log.info("Imported %d job(s) from the old jobs.json.", len(data))
        except Exception as exc:
            log.error("Could not import old jobs.json: %s", exc)


migrate_legacy_jobs()

jobs: list[dict] = [j for j in _read_json(JOBS_FILE, []) if isinstance(j, dict)]
config: dict = _read_json(CONFIG_FILE, {}) or {}


def save_jobs() -> None:
    _write_json(JOBS_FILE, jobs)


def save_config() -> None:
    _write_json(CONFIG_FILE, config)


def guild_config(guild_id: int) -> dict:
    return config.setdefault(str(guild_id), {})


# ==========================================================================
# HELPERS
# ==========================================================================

def guild_jobs(guild_id: int) -> list[dict]:
    """Jobs belonging to one server. Legacy jobs have no guild_id, so they are
    adopted by the first server that loads them."""
    gid = str(guild_id)
    return [j for j in jobs if str(j.get("guild_id", gid)) == gid]


def next_job_id(guild_id: int) -> str:
    numbers = []
    for job in guild_jobs(guild_id):
        try:
            numbers.append(int(str(job.get("id", "")).split("-")[-1]))
        except (ValueError, IndexError):
            continue
    return f"ZM-{max(numbers, default=0) + 1:04d}"


def find_job(guild_id: int, job_id: str) -> Optional[dict]:
    job_id = job_id.strip().upper()
    if job_id and not job_id.startswith("ZM-") and job_id.isdigit():
        job_id = f"ZM-{int(job_id):04d}"
    return next((j for j in guild_jobs(guild_id) if str(j.get("id", "")).upper() == job_id), None)


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
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

    durations = [j["duration_seconds"] for j in completed if j.get("duration_seconds")]
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
                f"**{job.get('id', '????')}**  ·  @{clean(job.get('username', 'unknown'))}\n"
                f"{clean(job.get('service', 'unknown'))}{when}\n\n"
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
            value="".join(lines).strip() or "—",
            inline=False,
        )

    embed.add_field(name="Completed", value=str(len(completed)), inline=True)
    embed.add_field(name="Today", value=str(completed_today), inline=True)
    embed.add_field(name="Average", value=average, inline=True)
    return embed


def dashboard_signature(guild_id: int) -> str:
    """Only redraw when something actually changed - saves rate limit budget.
    Relative timestamps update by themselves on the user's client."""
    records = guild_jobs(guild_id)
    return json.dumps(
        [(j.get("id"), j.get("status"), j.get("completed")) for j in records],
        sort_keys=True,
    )


# ==========================================================================
# BOT
# ==========================================================================

intents = discord.Intents.default()
# message_content is a PRIVILEGED intent. Leave it off unless you have ticked
# it in the Developer Portal, otherwise login fails outright.
if os.getenv("ENABLE_MESSAGE_CONTENT", "false").lower() == "true":
    intents.message_content = True


class MonitorBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.signatures: dict[int, str] = {}

    async def setup_hook(self) -> None:
        # Registering the view here means buttons keep working after a restart.
        self.add_view(DashboardButtons())

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

        dashboard_loop.start()
        monitor_loop.start()


bot = MonitorBot()


async def update_dashboard(guild_id: int, force: bool = False) -> None:
    """Edit the pinned dashboard for one server, creating it if needed."""
    settings = config.get(str(guild_id))
    if not settings or not settings.get("monitor_channel_id"):
        return

    signature = dashboard_signature(guild_id)
    if not force and bot.signatures.get(guild_id) == signature:
        return

    channel = bot.get_channel(int(settings["monitor_channel_id"]))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(settings["monitor_channel_id"]))
        except (discord.NotFound, discord.Forbidden):
            log.warning("Monitor channel for guild %s is gone or not visible.", guild_id)
            return
        except discord.HTTPException:
            return

    embed = build_dashboard(guild_id, bot.user)
    view = DashboardButtons()
    message_id = settings.get("dashboard_message_id")

    if message_id:
        try:
            message = await channel.fetch_message(int(message_id))
            # The animated logo is re-uploaded on every redraw. Discord's own
            # attachment links expire, so re-sending the bytes is what keeps
            # the thumbnail from turning into a broken image after a day.
            files = logo_files()
            embed = build_dashboard(guild_id, bot.user, bool(files))
            await message.edit(embed=embed, view=view, attachments=files)
            bot.signatures[guild_id] = signature
            return
        except discord.NotFound:
            settings.pop("dashboard_message_id", None)
        except discord.Forbidden:
            log.warning("Missing permission to edit the dashboard in guild %s.", guild_id)
            return
        except discord.HTTPException as exc:
            log.warning("Dashboard edit failed in guild %s: %s", guild_id, exc)
            return

    try:
        files = logo_files()
        embed = build_dashboard(guild_id, bot.user, bool(files))
        message = await channel.send(embed=embed, view=view, files=files)
        settings["dashboard_message_id"] = message.id
        save_config()
        bot.signatures[guild_id] = signature
    except discord.Forbidden:
        log.warning("Missing permission to post the dashboard in guild %s.", guild_id)
    except discord.HTTPException as exc:
        log.warning("Could not post dashboard in guild %s: %s", guild_id, exc)


@tasks.loop(seconds=REFRESH_SECONDS)
async def dashboard_loop() -> None:
    for guild_id in list(config.keys()):
        try:
            await update_dashboard(int(guild_id))
        except Exception as exc:
            # One bad server must never take the whole loop down.
            log.exception("Dashboard refresh failed for guild %s: %s", guild_id, exc)


@dashboard_loop.before_loop
async def before_dashboard_loop() -> None:
    await bot.wait_until_ready()


@dashboard_loop.error
async def dashboard_loop_error(exc: BaseException) -> None:
    """Without this, ONE unhandled error stops the loop permanently and
    silently - the bot looks alive but the dashboard freezes."""
    log.exception("Dashboard loop crashed, restarting it: %s", exc)
    dashboard_loop.restart()


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


async def send_completion_message(guild_id: Any, embed: discord.Embed,
                                  files: list[discord.File]) -> bool:
    """Post a completion card with no interaction to reply to - the monitor
    has none. /complete keeps replying to its interaction as before; this is
    only for completions the monitor finds by itself. The only channel this
    bot remembers for a server is the one /setup made the live monitor, so an
    automatic completion is posted there, alongside the dashboard.

    Returns whether the card actually reached Discord. The caller uses this
    to decide whether the job is really finished - a completion nobody was
    ever shown must not be recorded as one."""
    settings = config.get(str(guild_id))
    if not settings or not settings.get("monitor_channel_id"):
        log.warning("Guild %s has no monitor channel set up (/setup); "
                    "automatic completion was not posted anywhere.", guild_id)
        return False

    channel = bot.get_channel(int(settings["monitor_channel_id"]))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(settings["monitor_channel_id"]))
        except (discord.NotFound, discord.Forbidden):
            log.warning("Monitor channel for guild %s is gone or not visible; "
                       "could not post automatic completion.", guild_id)
            return False
        except discord.HTTPException:
            return False

    try:
        await channel.send(embed=embed, files=files)
        return True
    except discord.Forbidden:
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
    key = (str(guild_id), str(job.get("id")))
    if key in _completing:
        return
    _completing.add(key)
    try:
        if job.get("status") != "active":
            return  # already finished by /complete while this read was in flight

        now = datetime.now(timezone.utc)
        started = parse_time(job.get("started")) or now
        duration = max((now - started).total_seconds(), 0)
        username = str(job.get("username", "unknown"))

        try:
            snapshot, files = await build_frame(username, snapshot)
        except Exception as exc:
            log.exception("Job %s: could not build the completion card (%s). "
                          "Left active - will retry on the next check.", job.get("id"), exc)
            return

        embed = completion_embed(job, username, duration, snapshot, bool(files))
        delivered = await send_completion_message(guild_id, embed, files)
        if not delivered:
            log.error("Job %s: the completion card could not be delivered. "
                      "Left active - will retry once the channel or Discord recovers.",
                      job.get("id"))
            return

        # Only now, with the card actually shown, is the job recorded as done.
        job["status"] = "completed"
        job["completed"] = now.isoformat()
        job["duration_seconds"] = int(duration)
        # No closed_by: nobody closed this one, and closed_by is optional -
        # nothing downstream requires it, so an automatic completion leaves
        # it out rather than inventing a Discord user id.
        job.setdefault("guild_id", str(guild_id))
        save_jobs()

        log.info("Job %s automatically completed in guild %s - Instagram now reachable.",
                 job["id"], guild_id)
        await update_dashboard(int(guild_id))
    finally:
        _completing.discard(key)


async def check_job(job: dict, semaphore: asyncio.Semaphore) -> None:
    """One monitor tick's look at one job. A read that comes back UNKNOWN or
    GONE changes nothing - the job just gets checked again on the next tick,
    which is this monitor's whole retry strategy. Only OK is a real unban."""
    key = (str(job.get("guild_id")), str(job.get("id")))
    if key in _completing:
        return
    try:
        async with semaphore:
            snapshot = await instagram.lookup(str(job.get("username", "")))
    except Exception as exc:
        # instagram.lookup() already turns its own failures into an UNKNOWN
        # Snapshot rather than raising, but a broken job record or a card-
        # rendering error later must not be allowed to take the whole loop
        # down either.
        log.exception("Monitor: checking job %s failed: %s", job.get("id"), exc)
        return

    if snapshot.state == instagram.GONE:
        log.info("Monitor: job %s (@%s) is not reachable yet; staying active.",
                 job.get("id"), job.get("username"))
        return
    if snapshot.state != instagram.OK:
        return  # UNKNOWN - a temporary read, not an answer; try again next tick

    await auto_complete_job(job, snapshot)


@tasks.loop(seconds=MONITOR_INTERVAL_SECONDS)
async def monitor_loop() -> None:
    candidates = [j for j in jobs
                 if j.get("status") == "active" and j.get("service") == SERVICE_UNBAN]
    if not candidates:
        return
    semaphore = asyncio.Semaphore(MONITOR_CONCURRENCY)
    await asyncio.gather(*(check_job(job, semaphore) for job in candidates))


@monitor_loop.before_loop
async def before_monitor_loop() -> None:
    await bot.wait_until_ready()


@monitor_loop.error
async def monitor_loop_error(exc: BaseException) -> None:
    """Same reasoning as the dashboard loop: one unhandled error must not
    silently stop every job from ever being checked again."""
    log.exception("Unban monitor crashed, restarting it: %s", exc)
    monitor_loop.restart()


# ==========================================================================
# JOB ACTIONS
# ==========================================================================

async def create_job(interaction: discord.Interaction, username: str, service: str) -> None:
    if interaction.guild_id is None:
        await interaction.response.send_message("Use this inside a server.", ephemeral=True)
        return

    username = username.strip().lstrip("@")[:100]
    if not username:
        await interaction.response.send_message("Please enter a username.", ephemeral=True)
        return

    # Reading a profile takes a few seconds and Discord only gives us three
    # before it shows "This interaction failed", so acknowledge first and
    # send the real answer as a follow-up.
    await interaction.response.defer(ephemeral=True)

    job = {
        "id": next_job_id(interaction.guild_id),
        "guild_id": str(interaction.guild_id),
        "username": username,
        "service": service,
        "started": datetime.now(timezone.utc).isoformat(),
        "status": "active",
        "opened_by": str(interaction.user.id),
    }
    jobs.append(job)
    save_jobs()

    snapshot, files = await build_frame(username)
    if snapshot.user_id:
        # Instagram's numeric id never changes. Keeping it is the only way to
        # tell later whether a vanished handle was renamed or actually lost.
        job["ig_user_id"] = snapshot.user_id
        save_jobs()

    embed = house_embed(
        COLOR_ACTIVE,
        title=f"Job Opened  ·  @{username}",
        url=profile_url(username),
        description=(
            f"**Service:** {clean(service)}\n"
            f"**Job ID:** {job['id']}\n"
            f"{account_row(snapshot)}{caveat(snapshot)}"
        ),
        icon_user=interaction.client.user,
        footer="Opened",
        stamp=True,
        card=bool(files),
    )
    await interaction.followup.send(embed=embed, files=files, ephemeral=True)
    log.info("Job %s opened in guild %s by %s", job["id"], interaction.guild_id, interaction.user)
    await update_dashboard(interaction.guild_id)

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
            f"Job `{raw_id.strip().upper()}` was not found in this server.", ephemeral=True
        )
        return

    if job.get("status") != "active":
        await interaction.followup.send(
            f"`{job['id']}` is already completed.", ephemeral=True
        )
        return

    # Elapsed time is fixed at the moment the job is closed, exactly as before.
    # Time spent waiting on Instagram below is not part of the job's duration.
    now = datetime.now(timezone.utc)
    started = parse_time(job.get("started")) or now
    duration = max((now - started).total_seconds(), 0)
    username = str(job.get("username", "unknown"))

    key = (str(interaction.guild_id), str(job.get("id")))
    if key in _completing:
        await interaction.followup.send(
            f"`{job['id']}` is already being closed - give it a moment.", ephemeral=True
        )
        return

    # Re-read the account on the way out, so the closing card shows the state
    # you actually delivered rather than the one you started with. This happens
    # BEFORE the job is marked completed: a throttled read (UNKNOWN) must never
    # close a job and produce an empty card.
    _completing.add(key)
    try:
        snapshot = await final_snapshot(username, job["id"])
    finally:
        _completing.discard(key)

    if snapshot.state == instagram.UNKNOWN:
        # Nothing has been saved, so the job is still active and can simply be
        # closed again once Instagram answers.
        await interaction.followup.send(
            f"`{job['id']}` was **not** completed. "
            f"{snapshot.note or 'Instagram could not be read.'}\n"
            f"The job is still active - run `/complete {job['id']}` again in a few minutes.",
            ephemeral=True,
        )
        return

    job["status"] = "completed"
    job["completed"] = now.isoformat()
    job["duration_seconds"] = int(duration)
    job["closed_by"] = str(interaction.user.id)
    job.setdefault("guild_id", str(interaction.guild_id))
    save_jobs()

    snapshot, files = await build_frame(username, snapshot)
    embed = completion_embed(job, username, duration, snapshot, bool(files))
    await interaction.followup.send(embed=embed, files=files, ephemeral=True)
    log.info("Job %s completed in guild %s by %s", job["id"], interaction.guild_id, interaction.user)
    await update_dashboard(interaction.guild_id)


async def run_ban_check(interaction: discord.Interaction, username: str) -> None:
    """A look-up, not a job. Nothing is stored and nothing is opened."""
    username = str(username).strip().lstrip("@")[:100]
    if not username:
        await interaction.response.send_message("Please enter a username.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    snapshot, files = await build_frame(username)

    embed = house_embed(
        snapshot_colour(snapshot),
        title=f"Ban Check  ·  @{username}",
        url=profile_url(username),
        description=f"{account_row(snapshot)}{caveat(snapshot)}",
        icon_user=interaction.client.user,
        footer="Checked",
        stamp=True,
        card=bool(files),
    )
    await interaction.followup.send(embed=embed, files=files, ephemeral=True)
    log.info("Ban check on %s in guild %s -> %s", username, interaction.guild_id, snapshot.state)


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
    message = "Something went wrong, but the bot is still running. Please try again."
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
            if interaction.guild_id:
                await update_dashboard(interaction.guild_id, force=True)
            await interaction.followup.send("Monitor refreshed.", ephemeral=True)
        except Exception as exc:
            log.exception("Refresh failed: %s", exc)
            await safe_error_reply(interaction)

    async def on_error(self, interaction: discord.Interaction,
                       error: Exception, item: discord.ui.Item) -> None:
        log.exception("Button error on %s: %s", item, error)
        await safe_error_reply(interaction)


# ==========================================================================
# SLASH COMMANDS
# ==========================================================================

@bot.tree.command(name="setup", description="Make this channel the live monitor channel")
@app_commands.checks.has_permissions(manage_guild=True)
async def setup_command(interaction: discord.Interaction) -> None:
    settings = guild_config(interaction.guild_id)
    settings["monitor_channel_id"] = interaction.channel_id
    settings.pop("dashboard_message_id", None)
    save_config()
    bot.signatures.pop(interaction.guild_id, None)

    await interaction.response.send_message(
        "This channel is now the live monitor. The dashboard will appear in a moment "
        "and update itself automatically.",
        ephemeral=True,
    )
    await update_dashboard(interaction.guild_id, force=True)


@bot.tree.command(name="panel", description="Post the control panel in this channel")
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
@app_commands.describe(username="Instagram username", service="Type of service")
@app_commands.choices(service=[
    app_commands.Choice(name=SERVICE_VERIFICATION, value=SERVICE_VERIFICATION),
    app_commands.Choice(name=SERVICE_UNBAN, value=SERVICE_UNBAN),
])
async def newjob_command(interaction: discord.Interaction, username: str,
                         service: app_commands.Choice[str]) -> None:
    await create_job(interaction, username, service.value)


@bot.tree.command(name="bancheck", description="Check whether a handle is reachable right now")
@app_commands.describe(username="Instagram username")
async def bancheck_command(interaction: discord.Interaction, username: str) -> None:
    await run_ban_check(interaction, username)


@bot.tree.command(name="complete", description="Mark an active job as completed")
@app_commands.describe(job_id="The Job ID, for example ZM-0001")
async def complete_command(interaction: discord.Interaction, job_id: str) -> None:
    await complete_job(interaction, job_id)


@bot.tree.command(name="jobs", description="List the active jobs in this server")
async def jobs_command(interaction: discord.Interaction) -> None:
    active = [j for j in guild_jobs(interaction.guild_id) if j.get("status") == "active"]
    if not active:
        await interaction.response.send_message("Nothing in progress.", ephemeral=True)
        return

    lines = []
    for job in active[:15]:
        started = parse_time(job.get("started"))
        when = f" · opened <t:{int(started.timestamp())}:R>" if started else ""
        lines.append(
            f"**{job.get('id')}**  ·  @{clean(job.get('username', 'unknown'))}\n"
            f"{clean(job.get('service', 'unknown'))}{when}"
        )
    if len(active) > 15:
        lines.append(f"and {len(active) - 15} more.")

    embed = house_embed(
        COLOR_ACTIVE,
        title="Active Jobs",
        description="\n\n".join(lines),
        icon_user=bot.user,
        footer=f"{len(active)} in progress",
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="stats", description="Show job statistics for this server")
async def stats_command(interaction: discord.Interaction) -> None:
    records = guild_jobs(interaction.guild_id)
    active = sum(1 for j in records if j.get("status") == "active")
    completed = sum(1 for j in records if j.get("status") == "completed")

    embed = house_embed(
        COLOR_ACTIVE if active else COLOR_IDLE,
        title="Statistics",
        icon_user=bot.user,
        footer=BRAND,
        stamp=True,
    )
    embed.add_field(name="Total", value=str(len(records)), inline=True)
    embed.add_field(name="Active", value=str(active), inline=True)
    embed.add_field(name="Completed", value=str(completed), inline=True)
    await interaction.response.send_message(embed=embed)


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
    log.exception("Slash command error: %s", error)
    await safe_error_reply(interaction)


async def safe_reply(interaction: discord.Interaction, message: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


# ==========================================================================
# LIFECYCLE
# ==========================================================================

@bot.event
async def on_ready() -> None:
    log.info("Online as %s in %d server(s).", bot.user, len(bot.guilds))
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching, name="support jobs"
    ))
    for guild in bot.guilds:
        await update_dashboard(guild.id, force=True)


@bot.event
async def on_resumed() -> None:
    log.info("Reconnected to Discord.")


@bot.event
async def on_guild_join(guild: discord.Guild) -> None:
    log.info("Added to a new server: %s (%s)", guild.name, guild.id)


# ==========================================================================
# SINGLE INSTANCE GUARD
# Two copies of the bot fighting over jobs.json is a classic cause of
# duplicate dashboards and lost records.
# ==========================================================================

_lock_socket: Optional[socket.socket] = None


def acquire_lock() -> bool:
    global _lock_socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", LOCK_PORT))
        sock.listen(1)
        _lock_socket = sock
        return True
    except OSError:
        sock.close()
        return False


def main() -> int:
    if not TOKEN:
        log.error("DISCORD_TOKEN is missing. Put it in a .env file next to bot.py.")
        return 2

    if not acquire_lock():
        log.error("Another copy of this bot is already running on this machine. Exiting.")
        return 3

    try:
        bot.run(TOKEN, log_handler=None, reconnect=True)
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
