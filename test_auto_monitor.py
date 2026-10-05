#!/usr/bin/env python3
"""
Offline checks for the automatic Unban monitor.

    python test_auto_monitor.py

No Discord, no Instagram, no real 60-second waits: Instagram's answers are
scripted per handle, monitor ticks are driven directly (each call to
B.monitor_loop() is one tick - no sleeping involved), and jobs.json is
redirected to a temp file, so your real data/jobs.json is never touched.
Reuses the stand-ins from selftest.py. Not loaded by the bot.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import selftest as S  # noqa: E402


def profile(handle: str, followers: int, following: int, posts: int) -> dict:
    return {"data": {"user": {
        "id": f"id-{handle}", "username": handle, "full_name": f"Name of {handle}",
        "is_verified": False, "is_private": False,
        "edge_followed_by": {"count": followers},
        "edge_follow": {"count": following},
        "edge_owner_to_timeline_media": {"count": posts},
        "profile_pic_url_hd": "https://scontent.cdninstagram.com/avatar.jpg",
    }}}


def main() -> int:
    stub_root = S.build_stubs()
    sys.path.insert(0, str(stub_root))

    import httpx                      # the stub
    import instagram, cards           # noqa: F401
    import bot as B

    check, section = S.check, S.section
    tmp = Path(tempfile.mkdtemp(prefix="zm-monitortest-"))
    B.JOBS_FILE = tmp / "jobs.json"

    AVATAR = S.sample_avatar((60, 140, 200))
    script: dict[str, list] = {}   # script[handle] = [(status, body), ...]; last repeats
    reads: list[str] = []

    async def scripted_get(self, url, headers=None):
        if "web_profile_info" in url:
            handle = url.split("username=")[1]
            reads.append(handle)
            seq = script[handle]
            status, body = seq.pop(0) if len(seq) > 1 else seq[0]
            return httpx._Reply(status, body)
        return httpx._Reply(200, AVATAR)
    httpx.AsyncClient.get = scripted_get

    cards_drawn: list[dict] = []
    real_render = cards.render_profile_card

    def spy_render(username, **kw):
        cards_drawn.append({"username": username, **kw})
        return real_render(username, **kw)
    B.cards.render_profile_card = spy_render

    log_lines: list[str] = []

    class Grab(logging.Handler):
        def emit(self, record):
            log_lines.append(f"{record.levelname}: {record.getMessage()}")
    logging.getLogger("zm").addHandler(Grab())

    channel_sends: list[dict] = []

    class FakeChannel:
        def __init__(self, guild):
            self.guild = guild

        async def send(self, embed=None, files=None):
            channel_sends.append({"guild": self.guild, "embed": embed, "files": files or []})

    channels: dict[int, FakeChannel] = {}
    B.bot.get_channel = lambda cid: channels.get(cid)

    async def fake_fetch_channel(cid):
        if cid in channels:
            return channels[cid]
        raise B_NotFound()

    class B_NotFound(Exception):
        pass
    import discord as _discord_stub
    _discord_stub.NotFound = B_NotFound
    B.bot.fetch_channel = fake_fetch_channel

    dashboards: list[int] = []

    async def fake_dashboard(guild_id, force=False):
        dashboards.append(guild_id)
    B.update_dashboard = fake_dashboard

    class User:
        id = 42
        display_avatar = types.SimpleNamespace(url="https://x/a.png")

    class Followup:
        def __init__(self):
            self.sent = []

        async def send(self, content=None, *, embed=None, files=None, ephemeral=False, **kw):
            self.sent.append(types.SimpleNamespace(content=content, embed=embed, files=files or []))

    class Response:
        async def defer(self, **kw): pass
        async def send_message(self, *a, **k): pass

    class Interaction:
        def __init__(self, guild_id=1):
            self.guild_id, self.user = guild_id, User()
            self.client = types.SimpleNamespace(user=User())
            self.response, self.followup = Response(), Followup()

    def wire_channel(guild_id: int) -> None:
        """Equivalent of running /setup - gives the guild a monitor channel."""
        settings = B.guild_config(guild_id)
        settings["monitor_channel_id"] = 900_000 + guild_id
        channels[settings["monitor_channel_id"]] = FakeChannel(guild_id)

    def new_active_job(job_id, handle, service=None, guild="1", age=0):
        started = (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()
        return {"id": job_id, "guild_id": guild, "username": handle,
                "service": service or B.SERVICE_UNBAN, "started": started,
                "status": "active", "opened_by": "7"}

    def reset():
        script.clear(); reads.clear(); cards_drawn.clear(); log_lines.clear()
        channel_sends.clear(); dashboards.clear()
        instagram._cache.clear(); B._completing.clear()
        instagram.HTTPX_AVAILABLE = True
        B.jobs.clear(); B.config.clear(); channels.clear()
        B.save_jobs()

    def disk():
        return json.loads(B.JOBS_FILE.read_text())

    async def tick():
        """One monitor pass over every active Unban job, right now."""
        await B.monitor_loop()

    # ==================================================================
    section("TEST 1  ·  normal - UNKNOWN a few times, then a real OK")
    reset()
    wire_channel(1)
    B.jobs.append(new_active_job("ZM-1001", "alpha"))
    B.save_jobs()
    script["alpha"] = [(429, {}), (429, {}), (200, profile("alpha", 12_345, 321, 87))]

    asyncio.run(tick())
    check("stays active through UNKNOWN #1", B.jobs[0]["status"] == "active")
    asyncio.run(tick())
    check("stays active through UNKNOWN #2", B.jobs[0]["status"] == "active")
    check("nothing posted yet, nothing saved as completed",
          channel_sends == [] and disk()[0]["status"] == "active")
    asyncio.run(tick())
    job = B.jobs[0]
    check("completes automatically once Instagram answers OK", job["status"] == "completed")
    check("saved to disk", disk()[0]["status"] == "completed")
    check("duration measured from the ORIGINAL job creation time, not from the tick",
          0 <= job["duration_seconds"] <= 2, job["duration_seconds"])
    drawn = cards_drawn[-1]
    check("real follower/following/post counts reached the existing card",
          (drawn["followers"], drawn["following"], drawn["posts"]) == (12_345, 321, 87))
    check("real avatar reached the card", drawn["avatar_bytes"] == AVATAR)
    check("card state is ok", drawn["state"] == "ok")
    check("exactly one completion message, with the card attached",
          len(channel_sends) == 1 and len(channel_sends[0]["files"]) == 1)
    d = channel_sends[0]["embed"].description
    check("Job ID and elapsed time both shown", "ZM-1001" in d and "Elapsed" in d)
    check("no closed_by invented for an automatic completion", "closed_by" not in job)
    check("dashboard refreshed", dashboards == [1])

    # ==================================================================
    section("TEST 2  ·  429 repeatedly - never a false completion")
    reset()
    wire_channel(1)
    B.jobs.append(new_active_job("ZM-1002", "beta"))
    B.save_jobs()
    script["beta"] = [(429, {})]
    for _ in range(5):
        asyncio.run(tick())
    check("still active after five straight throttles", B.jobs[0]["status"] == "active")
    check("nothing ever completed", disk()[0]["status"] == "active")
    check("no card was ever drawn", cards_drawn == [])
    check("no completion message was ever sent", channel_sends == [])
    check("monitor is still willing to check again later (no crash, no give-up state)",
          B.jobs[0].get("status") == "active")
    script["beta"] = [(200, profile("beta", 10, 1, 1))]
    asyncio.run(tick())
    check("completes as soon as a real answer arrives", B.jobs[0]["status"] == "completed")

    # ==================================================================
    section("TEST 3  ·  GONE must never be read as a successful unban")
    reset()
    wire_channel(1)
    B.jobs.append(new_active_job("ZM-1003", "vanished"))
    B.save_jobs()
    script["vanished"] = [(404, {})]
    for _ in range(3):
        asyncio.run(tick())
    check("an Unban job facing a 404 stays active, not completed",
          B.jobs[0]["status"] == "active")
    check("never completed on disk either", disk()[0]["status"] == "active")
    check("no card, no message for a GONE read", cards_drawn == [] and channel_sends == [])
    check("GONE is logged so an operator can see it, without closing the job",
          any("not reachable yet" in l for l in log_lines))

    # ==================================================================
    section("TEST 4  ·  restart - active jobs are picked back up from disk")
    reset()
    wire_channel(1)
    B.jobs.append(new_active_job("ZM-1004", "gamma"))
    B.save_jobs()
    # ---- simulate a restart: wipe every in-memory structure and reload
    B.jobs.clear(); B._completing.clear(); instagram._cache.clear()
    B.jobs.extend(j for j in json.loads(B.JOBS_FILE.read_text()) if isinstance(j, dict))
    check("the active job survived the restart with its id intact",
          len(B.jobs) == 1 and B.jobs[0]["id"] == "ZM-1004" and B.jobs[0]["status"] == "active")
    script["gamma"] = [(429, {}), (200, profile("gamma", 55, 6, 7))]
    asyncio.run(tick())
    check("still active on the first post-restart tick", B.jobs[0]["status"] == "active")
    asyncio.run(tick())
    check("monitoring resumed and the job completes once Instagram answers",
          B.jobs[0]["status"] == "completed")

    # ==================================================================
    section("TEST 5  ·  three jobs at once - slow, quick, and mixed")
    reset()
    wire_channel(1)
    B.jobs.append(new_active_job("ZM-1005", "slow"))
    B.jobs.append(new_active_job("ZM-1006", "quick"))
    B.jobs.append(new_active_job("ZM-1007", "mixed"))
    B.save_jobs()
    script["slow"] = [(429, {})]
    script["quick"] = [(200, profile("quick", 1, 1, 1))]
    script["mixed"] = [(429, {}), (200, profile("mixed", 2, 2, 2))]

    asyncio.run(tick())
    by_id = {j["id"]: j for j in B.jobs}
    check("the quick job completed on the very first tick, without waiting for the others",
          by_id["ZM-1006"]["status"] == "completed")
    check("the slow job is still active", by_id["ZM-1005"]["status"] == "active")
    check("the mixed job is not done yet either", by_id["ZM-1007"]["status"] == "active")

    asyncio.run(tick())
    by_id = {j["id"]: j for j in B.jobs}
    check("the mixed job completed on its second tick", by_id["ZM-1007"]["status"] == "completed")
    check("the slow job is still waiting, unaffected by the others", by_id["ZM-1005"]["status"] == "active")

    by_user = {c["username"]: c for c in cards_drawn}
    check("each completed job's card has its own numbers - no cross-contamination",
          (by_user["quick"]["followers"], by_user["quick"]["posts"]) == (1, 1) and
          (by_user["mixed"]["followers"], by_user["mixed"]["posts"]) == (2, 2))
    check("no duplicate completion messages for either finished job",
          sum(1 for s in channel_sends if "ZM-1006" in (s["embed"].description or "")) == 1 and
          sum(1 for s in channel_sends if "ZM-1007" in (s["embed"].description or "")) == 1)

    # ==================================================================
    section("TEST 6  ·  duplicate-completion protection")
    reset()
    wire_channel(1)
    job = new_active_job("ZM-1008", "delta")
    B.jobs.append(job)
    B.save_jobs()
    snap = instagram.Snapshot("delta", instagram.OK, followers=9, following=9, posts=9)

    async def race():
        await asyncio.gather(
            B.auto_complete_job(job, snap),
            B.auto_complete_job(job, snap),
            B.auto_complete_job(job, snap),
        )
    asyncio.run(race())
    check("exactly one status transition", B.jobs[0]["status"] == "completed")
    check("exactly one card drawn", len(cards_drawn) == 1)
    check("exactly one completion message sent", len(channel_sends) == 1)
    check("the guard is clear afterwards", B._completing == set())

    # ==================================================================
    section("TEST 7  ·  manual /complete still works, and defers to the monitor")
    reset()
    wire_channel(1)
    B.jobs.append(new_active_job("ZM-1009", "epsilon"))
    B.save_jobs()
    script["epsilon"] = [(200, profile("epsilon", 4, 4, 4))]
    it = Interaction(1)
    asyncio.run(B.complete_job(it, "ZM-1009"))
    check("manual /complete still finishes a job on its own",
          B.jobs[0]["status"] == "completed" and len(it.followup.sent[0].files) == 1)
    check("closed_by IS set for a manual completion (unlike an automatic one)",
          B.jobs[0].get("closed_by") == "42")

    # /complete pressed while the monitor is already mid-completion on it
    reset()
    wire_channel(1)
    job2 = new_active_job("ZM-1010", "zeta")
    B.jobs.append(job2)
    B.save_jobs()
    script["zeta"] = [(200, profile("zeta", 1, 1, 1))]
    B._completing.add((str(job2["guild_id"]), job2["id"]))   # monitor "holds" this job
    it2 = Interaction(1)
    asyncio.run(B.complete_job(it2, "ZM-1010"))
    check("told the job is already being processed, instead of racing the monitor",
          "already being closed" in it2.followup.sent[0].content)
    check("no duplicate card while the monitor holds the job", cards_drawn == [])
    B._completing.discard((str(job2["guild_id"]), job2["id"]))

    # ==================================================================
    section("EXTRA  ·  immediate completion at /newjob time")
    reset()
    wire_channel(1)
    script["already.free"] = [(200, profile("already.free", 500, 50, 5))]
    it3 = Interaction(1)
    asyncio.run(B.create_job(it3, "already.free", B.SERVICE_UNBAN))
    check("an Unban job that is already reachable at creation completes immediately",
          len(B.jobs) == 1 and B.jobs[0]["status"] == "completed")
    check("elapsed time is a small, valid number, not negative or missing",
          isinstance(B.jobs[0].get("duration_seconds"), int) and B.jobs[0]["duration_seconds"] >= 0)
    check("exactly one completion message (plus the separate 'Job Opened' reply)",
          len(channel_sends) == 1)
    check("the opened reply and the completion message do not duplicate each other",
          len(it3.followup.sent) == 1 and "Opened" in it3.followup.sent[0].embed.footer)
    check("the newly-completed job is skipped by the monitor from here on",
          reads.count("already.free") == 1)
    asyncio.run(tick())
    check("a later tick does not touch an already-completed job",
          reads.count("already.free") == 1 and len(channel_sends) == 1)

    section("EXTRA  ·  Verification jobs are never auto-completed")
    reset()
    wire_channel(1)
    B.jobs.append(new_active_job("ZM-1020", "verify.me", service=B.SERVICE_VERIFICATION))
    B.save_jobs()
    script["verify.me"] = [(200, profile("verify.me", 1, 1, 1))]
    asyncio.run(tick())
    check("a Verification job is not touched by the automatic monitor even when reachable",
          B.jobs[0]["status"] == "active" and reads == [] and channel_sends == [])

    section("EXTRA  ·  no monitor channel configured")
    reset()
    # deliberately skip wire_channel(1) - guild never ran /setup
    job3 = new_active_job("ZM-1030", "nowhere")
    B.jobs.append(job3)
    B.save_jobs()
    before = disk()
    snap3 = instagram.Snapshot("nowhere", instagram.OK, followers=1, following=1, posts=1)
    asyncio.run(B.auto_complete_job(job3, snap3))
    check("with nowhere to post it, the job is left ACTIVE - not falsely completed",
          B.jobs[0]["status"] == "active")
    check("nothing was written to disk either", disk() == before)
    check("a warning is logged instead of silently losing the completion",
          any("no monitor channel" in l for l in log_lines))
    check("the guard is released so a later attempt can retry", B._completing == set())
    # once a channel exists, the very same job/snapshot completes cleanly
    wire_channel(1)
    asyncio.run(B.auto_complete_job(job3, snap3))
    check("recovers and completes once a channel is configured",
          B.jobs[0]["status"] == "completed" and len(channel_sends) == 1)

    # ==================================================================
    section("TEST 8  ·  card generation failure must not complete the job")
    reset()
    wire_channel(1)
    job4 = new_active_job("ZM-2001", "cardfail")
    B.jobs.append(job4)
    B.save_jobs()
    before = disk()
    snap4 = instagram.Snapshot("cardfail", instagram.OK, followers=1, following=1, posts=1)

    def broken_render(username, **kw):
        raise RuntimeError("Pillow choked on a corrupt avatar")
    real_render_ref = cards.render_profile_card
    B.cards.render_profile_card = broken_render
    try:
        asyncio.run(B.auto_complete_job(job4, snap4))
    finally:
        B.cards.render_profile_card = real_render_ref
    check("job left ACTIVE when the card itself cannot be built",
          B.jobs[0]["status"] == "active")
    check("jobs.json on disk is completely untouched", disk() == before)
    check("no completion message was sent - there was no card for it",
          channel_sends == [])
    check("the failure is logged clearly, naming the job",
          any("ZM-2001" in l and "could not build the completion card" in l for l in log_lines))
    check("the guard is released so the next tick can try again", B._completing == set())
    # the same job recovers cleanly once rendering works again
    asyncio.run(B.auto_complete_job(job4, snap4))
    check("recovers on the next attempt with a working renderer",
          B.jobs[0]["status"] == "completed" and len(channel_sends) == 1)

    # ==================================================================
    section("TEST 9  ·  Discord send failure must not complete the job")
    reset()
    wire_channel(1)
    job5 = new_active_job("ZM-2002", "sendfail")
    B.jobs.append(job5)
    B.save_jobs()
    before = disk()
    snap5 = instagram.Snapshot("sendfail", instagram.OK, followers=2, following=2, posts=2)

    async def failing_send(self, embed=None, files=None):
        raise B.discord.HTTPException("Discord returned a 503")
    real_channel_send = FakeChannel.send
    FakeChannel.send = failing_send
    try:
        asyncio.run(B.auto_complete_job(job5, snap5))
    finally:
        FakeChannel.send = real_channel_send
    check("job left ACTIVE when Discord rejects the send",
          B.jobs[0]["status"] == "active")
    check("jobs.json on disk is completely untouched", disk() == before)
    check("no completion is silently assumed to have gone out", channel_sends == [])
    check("the delivery failure is logged clearly, naming the job",
          any("ZM-2002" in l and "could not be delivered" in l for l in log_lines))
    check("the guard is released so the next tick can retry delivery", B._completing == set())
    # once Discord/the channel is healthy again, the same job completes
    asyncio.run(B.auto_complete_job(job5, snap5))
    check("recovers and completes once delivery succeeds",
          B.jobs[0]["status"] == "completed" and len(channel_sends) == 1)

    # ==================================================================
    section("TEST 10  ·  ordinary success is unaffected by the new ordering")
    reset()
    wire_channel(1)
    job6 = new_active_job("ZM-2003", "clean")
    B.jobs.append(job6)
    B.save_jobs()
    script["clean"] = [(200, profile("clean", 777, 88, 9))]
    asyncio.run(tick())
    check("completes normally when the card builds and Discord accepts it",
          B.jobs[0]["status"] == "completed")
    check("saved to disk", disk()[0]["status"] == "completed")
    check("exactly one card with the real numbers", len(cards_drawn) == 1 and
          (cards_drawn[0]["followers"], cards_drawn[0]["posts"]) == (777, 9))
    check("exactly one completion message", len(channel_sends) == 1)

    # ==================================================================
    section("TEST 11  ·  no duplicate completion after recovering from a failure")
    reset()
    wire_channel(1)
    job7 = new_active_job("ZM-2004", "flaky")
    B.jobs.append(job7)
    B.save_jobs()
    snap7 = instagram.Snapshot("flaky", instagram.OK, followers=3, following=3, posts=3)

    attempts = {"n": 0}

    async def flaky_send(self, embed=None, files=None):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise B.discord.HTTPException("temporary failure")
        channel_sends.append({"guild": self.guild, "embed": embed, "files": files or []})
    real_channel_send2 = FakeChannel.send
    FakeChannel.send = flaky_send
    try:
        asyncio.run(B.auto_complete_job(job7, snap7))     # fails - stays active
        check("first attempt failed and left the job active",
              B.jobs[0]["status"] == "active" and channel_sends == [])
        asyncio.run(B.auto_complete_job(job7, snap7))      # succeeds on retry
    finally:
        FakeChannel.send = real_channel_send2
    check("completes on the retry after the transient failure clears",
          B.jobs[0]["status"] == "completed")
    check("exactly one completion message was ever sent - no duplicate from the failed attempt",
          len(channel_sends) == 1)
    check("the card was rebuilt for the retry (cheap, local, no extra Instagram call) "
          "but only the successful attempt's card was ever delivered",
          sum(1 for c in cards_drawn if c["username"] == "flaky") == 2 and reads == [])

    if S.FAILURES:
        print(f"\n{len(S.FAILURES)} check(s) FAILED:")
        for label in S.FAILURES:
            print("  -", label)
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
