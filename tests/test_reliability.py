"""Failures in one part must not take down another: Instagram, Discord,
the dashboard, card rendering, missing optional dependencies, the loops."""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from conftest import B, FakeInteraction, cards, discord, http_error, instagram, profile


def run(coro):
    return asyncio.run(coro)


def test_instagram_failures_do_not_crash_the_monitor(env):
    env.dc.setup(1)
    env.job("ZM-0001", "timeout")
    env.job("ZM-0002", "refused")
    env.job("ZM-0003", "fine")
    env.ig.set("timeout", httpx.ReadTimeout("slow"))
    env.ig.set("refused", httpx.ConnectError("refused"))
    env.ig.set("fine", (200, profile("fine")))
    run(env.tick())
    assert [j["status"] for j in B.jobs] == ["active", "active", "completed"]


def test_unexpected_exception_in_one_check_does_not_stop_the_tick(env, monkeypatch, logs):
    env.dc.setup(1)
    env.job("ZM-0001", "boom")
    env.job("ZM-0002", "fine")
    env.ig.set("fine", (200, profile("fine")))
    real = instagram.lookup

    async def sometimes_raises(name, **kw):
        if name == "boom":
            raise ValueError("unexpected")
        return await real(name, **kw)
    monkeypatch.setattr(instagram, "lookup", sometimes_raises)
    run(env.tick())
    assert B.jobs[1]["status"] == "completed"
    assert any("checking job ZM-0001 failed" in line for line in logs)


def test_dashboard_failure_does_not_undo_a_completion(env, monkeypatch):
    env.dc.setup(1)
    env.job("ZM-0001", "fine")
    env.ig.set("fine", (200, profile("fine")))

    async def broken_dashboard(*a, **k):
        raise RuntimeError("dashboard exploded")
    monkeypatch.setattr(B, "update_dashboard", broken_dashboard)
    run(env.tick())
    assert B.jobs[0]["status"] == "completed"
    assert env.disk_jobs()[0]["status"] == "completed"


def test_discord_failure_does_not_corrupt_jobs(env):
    channel = env.dc.setup(1)
    channel.fail_send = http_error(discord.HTTPException, 500, "Internal Server Error")
    env.job("ZM-0001", "fine")
    env.job("ZM-0002", "other", service=B.SERVICE_VERIFICATION)
    env.ig.set("fine", (200, profile("fine")))
    before = env.disk_jobs()
    for _ in range(3):
        run(env.tick())
        run(B.run_dashboard_tick())
    assert env.disk_jobs() == before
    assert B.jobs == before


def test_loop_bodies_swallow_errors(env, monkeypatch, logs):
    async def boom():
        raise RuntimeError("tick failed")
    monkeypatch.setattr(B, "run_monitor_tick", boom)
    monkeypatch.setattr(B, "run_dashboard_tick", boom)
    monkeypatch.setattr(B, "_ensure_running", lambda *a: None)
    run(B.monitor_loop.coro())
    run(B.dashboard_loop.coro())
    assert sum("iteration failed" in line for line in logs) == 2


def test_a_crashed_loop_is_started_again(env):
    """The watchdog that both loops run for each other, end to end on a
    real discord.py tasks.loop."""
    from discord.ext import tasks
    ticks = {"n": 0}

    @tasks.loop(seconds=0.01)
    async def fragile():
        ticks["n"] += 1
        if ticks["n"] == 2:
            raise RuntimeError("crash")

    @fragile.error
    async def on_error(exc):
        asyncio.get_running_loop().call_later(0.02, B._ensure_running, fragile, "fragile")

    async def scenario():
        fragile.start()
        for _ in range(100):
            await asyncio.sleep(0.01)
            if ticks["n"] >= 5:
                break
        fragile.cancel()
    run(scenario())
    assert ticks["n"] >= 5


def test_card_unavailable_without_pillow(env, monkeypatch):
    monkeypatch.setattr(cards, "PIL_AVAILABLE", False)
    monkeypatch.setattr(cards, "_logo_cache", None)
    env.dc.setup(1)
    env.ig.set("fine", (200, profile("fine")))
    it = FakeInteraction(1)
    run(B.create_job(it, "fine", B.SERVICE_UNBAN))
    assert it.followup.sent[0].files == []
    assert it.followup.sent[0].embed.image.url is None
    assert B.jobs[0]["status"] == "completed"               # still announced
    assert B.logo_files() == []
    assert run(B.update_dashboard(1)) is True


def test_bancheck_without_httpx(env, monkeypatch):
    monkeypatch.setattr(instagram, "HTTPX_AVAILABLE", False)
    it = FakeInteraction(1)
    run(B.run_ban_check(it, "anyone"))
    text = it.followup.sent[0].embed.description
    assert "Could not check" in text and "ban" not in text.lower().replace("ban check", "")


@pytest.mark.parametrize("reply,phrase,colour", [
    ((200, profile("x.y")), "Reachable", B.COLOR_DONE),
    ((404, {}), "Not reachable", B.COLOR_WARN),
    ((429, {}), "Could not check", B.COLOR_IDLE),
    ((200, {"status": "fail", "require_login": True}), "Could not check", B.COLOR_IDLE),
])
def test_bancheck_wording_never_claims_a_ban(env, reply, phrase, colour):
    env.ig.set("x.y", reply)
    it = FakeInteraction(1)
    run(B.run_ban_check(it, "x.y"))
    embed = it.followup.sent[0].embed
    assert phrase in embed.description
    assert embed.colour.value == colour
    body = embed.description.lower()
    assert "is banned" not in body and "was banned" not in body and "account banned" not in body
    assert B.jobs == []                                      # opens nothing


def test_bancheck_rejects_invalid_handles(env):
    it = FakeInteraction(1)
    run(B.run_ban_check(it, "not a handle"))
    assert "does not look like" in it.response.messages[0].content
    assert env.ig.profile_reads == []


def test_token_never_reaches_the_log(monkeypatch):
    # Built at runtime so no token-shaped literal sits in the repository.
    fake = ".".join(["MTIz" + "NDU2" * 5, "Gabc" + "DE", "abcdefghij" * 3 + "01234"])
    monkeypatch.setattr(B, "TOKEN", fake)
    formatter = B.RedactingFormatter("%(message)s")
    record = logging.LogRecord("zm", logging.ERROR, __file__, 1,
                               "login with %s failed", (fake,), None)
    assert fake not in formatter.format(record)
    other = ".".join(["OTk5" * 6, "G" + "x" * 5, "y" * 36])
    record = logging.LogRecord("zm", logging.ERROR, __file__, 1, "x %s", (other,), None)
    assert other not in formatter.format(record)


def test_refresh_button_reports_failure_honestly(env):
    it = FakeInteraction(1)
    view = run(_make_view())
    run(view.refresh_button.callback(it))
    assert "could not be refreshed" in it.followup.sent[0].content


async def _make_view():
    return B.DashboardButtons()
