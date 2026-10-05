"""The live dashboard: content, Discord's limits, no duplicates, recovery -
and strict separation between servers."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from conftest import (B, FakeInteraction, assert_embed_within_limits, discord, http_error,
                      instagram, profile)


def run(coro):
    return asyncio.run(coro)


def field(embed, name_start):
    return next((f for f in embed.fields if f.name.startswith(name_start)), None)


# --------------------------------------------------------------- content

def test_empty_dashboard(env):
    embed = B.build_dashboard(1, None, with_logo=True)
    assert "All clear" in embed.description
    assert embed.colour.value == B.COLOR_IDLE
    assert field(embed, "Active") is None
    assert embed.thumbnail.url == B.LOGO_URL
    assert_embed_within_limits(embed)


def test_active_jobs_show_id_user_type_time_and_state(env):
    env.job("ZM-0001", "under_score_name", service=B.SERVICE_UNBAN, age=300)
    env.job("ZM-0002", "verify.me", service=B.SERVICE_VERIFICATION)
    B.record_monitor_state(B.jobs[0], instagram.GONE)
    embed = B.build_dashboard(1, None)
    active = field(embed, "Active").value
    assert embed.colour.value == B.COLOR_ACTIVE
    assert "ZM-0001" in active and "ZM-0002" in active
    assert r"under\_score\_name" in active                 # markdown escaped
    assert "Unban" in active and "Instagram Verification" in active
    assert "opened <t:" in active                           # relative timestamp
    assert "Not reachable yet" in active
    assert "Awaiting manual completion" in active
    assert "banned" not in active.lower()


def test_unknown_state_is_never_shown_as_gone(env):
    env.job("ZM-0001", "abc")
    B.record_monitor_state(B.jobs[0], instagram.UNKNOWN, "rate limited")
    assert B.job_state_text(B.jobs[0]) == "Could not check"


def test_completion_information(env):
    now = datetime.now(timezone.utc)
    for i in range(5):
        env.job(f"ZM-000{i + 1}", f"done{i}", status="completed",
                completed=(now - timedelta(minutes=i)).isoformat(), duration_seconds=90)
    embed = B.build_dashboard(1, None)
    recent = field(embed, "Recently completed").value
    assert "ZM-0001" in recent and "1m 30s" in recent
    assert recent.count("\n") == B.RECENT_SHOWN - 1
    assert field(embed, "Completed").value == "5"
    assert field(embed, "Today").value == "5"
    assert field(embed, "Average").value == "1m 30s"


def test_many_jobs_stay_within_discord_limits(env):
    now = datetime.now(timezone.utc)
    for i in range(300):
        B.jobs.append({"id": f"ZM-{i:04d}", "guild_id": "1", "username": "x_" * 50,
                       "service": B.SERVICE_UNBAN, "status": "active",
                       "started": (now - timedelta(minutes=i)).isoformat()})
    for i in range(300):
        B.jobs.append({"id": f"ZM-9{i:03d}", "guild_id": "1", "username": "*_~" * 33,
                       "service": "S" * 300, "status": "completed",
                       "started": now.isoformat(), "completed": now.isoformat(),
                       "duration_seconds": 10 ** 9})
    for job in B.jobs[:300]:
        B.record_monitor_state(job, "mismatch")
    with_logo = B.build_dashboard(1, None, with_logo=True)
    assert_embed_within_limits(with_logo)
    assert "more." in field(with_logo, "Active").value
    assert_embed_within_limits(B.jobs_list_embed(1))
    assert_embed_within_limits(B.stats_embed(1))


def test_long_brand_name_still_fits(env, monkeypatch):
    monkeypatch.setattr(B, "BRAND", "B" * 1000)
    assert_embed_within_limits(B.build_dashboard(1, None))


# ------------------------------------------------- posting and recovery

def test_first_update_posts_and_remembers_the_message(env):
    channel = env.dc.setup(1)
    assert run(B.update_dashboard(1)) is True
    assert len(channel.sent) == 1
    assert env.disk_config()["1"]["dashboard_message_id"] == channel.sent[0].id
    assert isinstance(channel.sent[0].view, B.DashboardButtons)
    assert channel.sent[0].files[0].filename == B.LOGO_FILENAME


def test_later_updates_edit_instead_of_posting(env):
    channel = env.dc.setup(1)
    run(B.update_dashboard(1))
    env.job("ZM-0001", "abc")
    run(B.update_dashboard(1))
    run(B.update_dashboard(1, force=True))
    assert len(channel.sent) == 1
    assert channel.sent[0].edits == 2
    assert "ZM-0001" in field(channel.sent[0].embed, "Active").value


def test_unchanged_dashboard_is_not_redrawn(env):
    channel = env.dc.setup(1)
    run(B.update_dashboard(1))
    run(B.update_dashboard(1))
    assert channel.sent[0].edits == 0


def test_concurrent_updates_never_create_duplicate_dashboards(env):
    channel = env.dc.setup(1)

    async def stampede():
        await asyncio.gather(*(B.update_dashboard(1, force=True) for _ in range(10)))
    run(stampede())
    assert len(channel.sent) == 1


def test_restart_reattaches_to_the_existing_dashboard(env):
    channel = env.dc.setup(1)
    run(B.update_dashboard(1))
    first_id = channel.sent[0].id

    # restart: memory is wiped, config.json survives
    B.config.clear(); B.bot.signatures.clear(); B._dashboard_locks.clear()
    B.load_state()
    run(B.update_dashboard(1, force=True))                  # what on_ready does
    assert len(channel.sent) == 1
    assert channel.sent[0].id == first_id and channel.sent[0].edits == 1


def test_deleted_dashboard_is_recreated_once(env):
    channel = env.dc.setup(1)
    run(B.update_dashboard(1))
    run(channel.sent[0].delete())
    run(B.update_dashboard(1, force=True))
    run(B.update_dashboard(1, force=True))
    assert len(channel.sent) == 2
    assert env.disk_config()["1"]["dashboard_message_id"] == channel.sent[1].id


def test_failed_edit_is_logged_once_and_retried(env, logs):
    channel = env.dc.setup(1)
    run(B.update_dashboard(1))
    channel.fail_edit = http_error(discord.HTTPException, 500, "Internal Server Error")
    env.job("ZM-0001", "abc")
    for _ in range(5):
        assert run(B.update_dashboard(1)) is False
    assert sum("dashboard edit failed" in line for line in logs) == 1
    channel.fail_edit = None
    assert run(B.update_dashboard(1)) is True              # signature was not stored
    assert any("working again" in line for line in logs)


def test_missing_attach_permission_falls_back_to_text(env):
    channel = env.dc.setup(1)
    channel.fail_send_with_files = http_error(discord.Forbidden, 403, "Missing Permissions")
    assert run(B.update_dashboard(1)) is True
    assert channel.sent[0].files == [] and channel.sent[0].embed.thumbnail.url is None


def test_deleted_channel_does_not_crash_the_loop(env, logs):
    env.dc.setup(1)
    env.dc.setup(2)
    del env.dc.channels[env.dc.channel(1).id]
    run(B.run_dashboard_tick())
    run(B.run_dashboard_tick())
    assert len(env.dc.channel(2).sent) == 1                  # guild 2 unaffected
    assert sum("no longer exists" in line for line in logs) == 1


def test_one_failing_guild_does_not_stop_the_others(env, monkeypatch):
    env.dc.setup(1)
    env.dc.setup(2)
    real = B.build_dashboard

    def explode_for_guild_1(guild_id, *a, **k):
        if int(guild_id) == 1:
            raise RuntimeError("bad data")
        return real(guild_id, *a, **k)
    monkeypatch.setattr(B, "build_dashboard", explode_for_guild_1)
    run(B.run_dashboard_tick())
    assert len(env.dc.channel(2).sent) == 1


def test_setup_same_channel_twice_keeps_one_dashboard(env, monkeypatch):
    channel = env.dc.channel(1)
    run(B.setup_command.callback(FakeInteraction(1, channel_id=channel.id)))
    run(B.setup_command.callback(FakeInteraction(1, channel_id=channel.id)))
    assert len(channel.sent) == 1


def test_setup_in_a_new_channel_removes_the_old_dashboard(env):
    old = env.dc.channel(1)
    run(B.setup_command.callback(FakeInteraction(1, channel_id=old.id)))
    new = env.dc.channels.setdefault(777, type(old)(777, 1))
    it = FakeInteraction(1, channel_id=777)
    run(B.setup_command.callback(it))
    assert old.sent[0].deleted
    assert len(new.sent) == 1
    assert B.config["1"]["monitor_channel_id"] == 777
    assert "now the live monitor" in it.followup.sent[0].content


def test_setup_reports_when_it_cannot_post(env):
    channel = env.dc.channel(1)
    channel.fail_send = http_error(discord.Forbidden, 403, "Missing Access")
    it = FakeInteraction(1, channel_id=channel.id)
    run(B.setup_command.callback(it))
    assert "could not be posted" in it.followup.sent[0].content


# ----------------------------------------------------------- multi-guild

def test_guild_a_cannot_see_guild_b_jobs(env):
    env.job("ZM-0001", "alpha", guild="1")
    env.job("ZM-0001", "bravo", guild="2")
    assert [j["username"] for j in B.guild_jobs(1)] == ["alpha"]
    assert [j["username"] for j in B.guild_jobs(2)] == ["bravo"]
    assert "bravo" not in field(B.build_dashboard(1, None), "Active").value
    assert "bravo" not in B.jobs_list_embed(1).description
    assert B.find_job(1, "ZM-0001")["username"] == "alpha"
    assert B.find_job(3, "ZM-0001") is None


def test_guild_a_cannot_complete_guild_b_job(env):
    env.job("ZM-0007", "bravo", guild="2", service=B.SERVICE_VERIFICATION)
    it = FakeInteraction(1)
    run(B.complete_job(it, "ZM-0007"))
    assert "was not found" in it.followup.sent[0].content
    assert B.jobs[0]["status"] == "active"


def test_same_job_id_in_two_guilds_complete_independently(env):
    a, b = env.dc.setup(1), env.dc.setup(2)
    env.job("ZM-0001", "alpha", guild="1")
    env.job("ZM-0001", "bravo", guild="2")
    env.ig.set("alpha", (200, profile("alpha")))
    env.ig.set("bravo", (404, {}))
    run(env.tick())
    assert [j["status"] for j in B.jobs] == ["completed", "active"]
    assert any("alpha" in (m.embed.title or "") for m in a.sent)
    assert not any("alpha" in (m.embed.title or "") for m in b.sent)


def test_independent_configuration(env):
    a, b = env.dc.setup(1), env.dc.setup(2)
    run(B.update_dashboard(1))
    run(B.update_dashboard(2))
    assert B.config["1"]["monitor_channel_id"] != B.config["2"]["monitor_channel_id"]
    assert B.config["1"]["dashboard_message_id"] == a.sent[0].id
    assert B.config["2"]["dashboard_message_id"] == b.sent[0].id


def test_completion_is_never_posted_to_another_guilds_channel(env):
    env.dc.setup(1)
    foreign = env.dc.channel(2)
    B.guild_config(1)["monitor_channel_id"] = foreign.id    # tampered config
    job = env.job("ZM-0001", "abc", guild="1")
    run(B.auto_complete_job(job, instagram.Snapshot("abc", instagram.OK)))
    assert foreign.sent == [] and job["status"] == "active"


def test_legacy_jobs_without_guild_are_hidden_from_everyone(env, logs):
    B.jobs.append({"id": "ZM-0001", "username": "old", "status": "active",
                   "service": "Unban"})
    B.config.update({"1": {}, "2": {}})
    B.adopt_orphan_jobs()
    assert B.guild_jobs(1) == [] and B.guild_jobs(2) == []
    assert B.monitor_candidates() == []
    assert any("LEGACY_GUILD_ID" in line for line in logs)


def test_legacy_jobs_are_adopted_by_the_only_configured_guild(env):
    B.jobs.append({"id": "ZM-0001", "username": "old", "status": "active"})
    B.config.update({"5": {"monitor_channel_id": 1}})
    B.adopt_orphan_jobs()
    assert B.jobs[0]["guild_id"] == "5"
    assert env.disk_jobs()[0]["guild_id"] == "5"


def test_legacy_jobs_follow_legacy_guild_id(env, monkeypatch):
    monkeypatch.setattr(B, "LEGACY_GUILD_ID", "9")
    B.jobs.append({"id": "ZM-0001", "username": "old", "status": "active"})
    B.config.update({"1": {}, "2": {}})
    B.adopt_orphan_jobs()
    assert B.guild_jobs(9)[0]["id"] == "ZM-0001"


def test_commands_refuse_dms(env):
    it = FakeInteraction(None)
    run(B.create_job(it, "abc", B.SERVICE_UNBAN))
    run(B.complete_job(it, "1"))
    assert all("inside a server" in m.content for m in it.response.messages)
    assert B.jobs == [] and B.config == {}


# ----------------------------------------------- persistent views (real)

def test_panel_buttons_are_persistent_with_stable_ids(env):
    async def build():
        return B.DashboardButtons(), B.LegacyButtons()
    view, legacy = run(build())
    assert view.is_persistent() and legacy.is_persistent()
    assert [c.custom_id for c in view.children] == [
        "zm_verification", "zm_unban", "zm_ban_check", "zm_complete_job", "zm_refresh"]
    assert [c.label for c in view.children] == [
        "Verification", "Unban", "Ban Check", "Complete", "Refresh"]
    assert [c.custom_id for c in legacy.children] == ["zm_account_support"]


def test_command_set_is_preserved(env):
    names = sorted(c.name for c in B.bot.tree.get_commands())
    assert names == ["bancheck", "complete", "jobs", "newjob", "panel", "ping", "setup", "stats"]
    for name in ("setup", "panel", "newjob", "bancheck", "complete", "jobs", "stats"):
        assert B.bot.tree.get_command(name).guild_only, name
    setup = B.bot.tree.get_command("setup")
    assert setup.default_permissions.manage_guild


def test_only_the_guilds_intent_is_requested(env):
    assert B.intents.guilds
    assert not B.intents.message_content
    assert not B.intents.members and not B.intents.presences
