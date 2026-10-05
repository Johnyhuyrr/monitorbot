"""Job lifecycle: creating, completing (by hand and automatically), the
shared completion guard, persistence and recovery after a restart."""

from __future__ import annotations

import asyncio
import json

import pytest

from conftest import B, FakeInteraction, discord, http_error, instagram, profile


def run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------- create

def test_create_verification_job(env):
    env.dc.setup(1)
    env.ig.set("verify.me", (200, profile("verify.me", user_id="111")))
    it = FakeInteraction(1, user_id=77)
    run(B.create_job(it, "@verify.me", B.SERVICE_VERIFICATION))

    assert len(B.jobs) == 1
    job = B.jobs[0]
    assert job["id"] == "ZM-0001"
    assert job["guild_id"] == "1"
    assert job["username"] == "verify.me"
    assert job["service"] == B.SERVICE_VERIFICATION
    assert job["status"] == "active"
    assert job["opened_by"] == "77"
    assert job["ig_user_id"] == "111"
    assert B.parse_time(job["started"]) is not None
    assert env.disk_jobs() == B.jobs                      # persisted
    reply = it.followup.sent[0]
    assert reply.ephemeral and "Opened" in reply.embed.footer.text
    assert len(reply.files) == 1                          # the profile card


def test_verification_job_is_never_auto_completed(env):
    env.dc.setup(1)
    env.ig.set("verify.me", (200, profile("verify.me")))
    run(B.create_job(FakeInteraction(1), "verify.me", B.SERVICE_VERIFICATION))
    for _ in range(3):
        run(env.tick())
    assert B.jobs[0]["status"] == "active"
    assert not any("Job Complete" in (m.embed.title or "") for m in env.dc.channel(1).sent)
    assert env.ig.profile_reads == ["verify.me"]            # the monitor never looked


def test_create_unban_job_for_unreachable_account(env):
    env.dc.setup(1)
    env.ig.set("lost.acct", (404, {}))
    it = FakeInteraction(1)
    run(B.create_job(it, "lost.acct", B.SERVICE_UNBAN))
    job = B.jobs[0]
    assert job["status"] == "active" and "ig_user_id" not in job
    assert B.job_state_text(job) == "Not reachable yet"
    text = it.followup.sent[0].embed.description
    assert "Not reachable" in text and "not proof of a ban" in text


def test_job_ids_increase_per_server(env):
    env.ig.set("a", (404, {}))
    for _ in range(3):
        run(B.create_job(FakeInteraction(1), "a", B.SERVICE_UNBAN))
    run(B.create_job(FakeInteraction(2), "a", B.SERVICE_UNBAN))
    assert [j["id"] for j in B.jobs] == ["ZM-0001", "ZM-0002", "ZM-0003", "ZM-0001"]


def test_invalid_username_is_rejected_before_anything_is_saved(env):
    it = FakeInteraction(1)
    run(B.create_job(it, "ZM-0002", B.SERVICE_UNBAN))
    assert B.jobs == []
    assert "does not look like an Instagram username" in it.response.messages[0].content
    assert env.ig.profile_reads == []


def test_duplicate_open_job_is_flagged(env):
    env.ig.set("twice", (404, {}))
    run(B.create_job(FakeInteraction(1), "twice", B.SERVICE_UNBAN))
    it = FakeInteraction(1)
    run(B.create_job(it, "twice", B.SERVICE_UNBAN))
    assert "ZM-0001 is already open" in it.followup.sent[0].embed.description


def test_unban_without_setup_tells_the_user(env):
    env.ig.set("x.y", (404, {}))
    it = FakeInteraction(1)
    run(B.create_job(it, "x.y", B.SERVICE_UNBAN))
    assert "/setup" in it.followup.sent[0].embed.description


def test_unban_already_reachable_completes_immediately(env):
    channel = env.dc.setup(1)
    env.ig.set("free.now", (200, profile("free.now", user_id="555")))
    it = FakeInteraction(1)
    run(B.create_job(it, "free.now", B.SERVICE_UNBAN))
    job = B.jobs[0]
    assert job["status"] == "completed" and job["ig_user_id"] == "555"
    completions = [m for m in channel.sent if (m.embed.title or "").startswith("Job Complete")]
    assert len(completions) == 1
    assert env.ig.profile_reads == ["free.now"]           # one read, reused


# -------------------------------------------------------- manual complete

def test_manual_complete_by_id_and_by_number(env):
    env.ig.set("one", (200, profile("one")))
    env.ig.set("two", (200, profile("two")))
    env.job("ZM-0001", "one", service=B.SERVICE_VERIFICATION, age=120)
    env.job("ZM-0002", "two", service=B.SERVICE_VERIFICATION)

    it = FakeInteraction(1, user_id=9)
    run(B.complete_job(it, "ZM-0001"))
    it2 = FakeInteraction(1, user_id=9)
    run(B.complete_job(it2, "2"))

    one, two = B.jobs
    assert one["status"] == two["status"] == "completed"
    assert one["closed_by"] == "9"
    assert 119 <= one["duration_seconds"] <= 125
    assert B.parse_time(one["completed"]) is not None
    assert "ig_user_id" in one
    assert env.disk_jobs()[0]["status"] == "completed"
    assert "Job Complete" in it.followup.sent[0].embed.title
    assert len(it.followup.sent[0].files) == 1


@pytest.mark.parametrize("raw", ["ZM-0003", "zm-3", "zm3", "3", "0003", " ZM-0003 "])
def test_find_job_accepts_every_id_form(env, raw):
    env.job("ZM-0003", "abc")
    assert B.find_job(1, raw)["id"] == "ZM-0003"


def test_complete_unknown_id(env):
    it = FakeInteraction(1)
    run(B.complete_job(it, "ZM-9999"))
    assert "was not found" in it.followup.sent[0].content


def test_complete_already_completed(env):
    env.job("ZM-0001", "abc", status="completed")
    B.jobs[0]["status"] = "completed"
    it = FakeInteraction(1)
    run(B.complete_job(it, "1"))
    assert "already completed" in it.followup.sent[0].content


def test_manual_complete_refuses_while_instagram_cannot_answer(env):
    env.ig.set("blocked", (503, {}))
    env.job("ZM-0001", "blocked", service=B.SERVICE_VERIFICATION)
    it = FakeInteraction(1)
    run(B.complete_job(it, "1"))
    assert B.jobs[0]["status"] == "active"
    assert env.disk_jobs()[0]["status"] == "active"
    assert "**not** completed" in it.followup.sent[0].content
    assert B._completing == set()


def test_manual_complete_of_legacy_job_without_a_real_handle(env):
    """Real data has an Account Support job whose 'username' is ZM-0002.
    There is no profile to read, so it must still be closable."""
    env.job("ZM-0003", "ZM-0002", service="Account Support")
    it = FakeInteraction(1)
    run(B.complete_job(it, "ZM-0003"))
    assert B.jobs[0]["status"] == "completed"
    assert env.ig.profile_reads == []


def test_manual_complete_gone_account_is_allowed(env):
    env.ig.set("gone.acct", (404, {}))
    env.job("ZM-0001", "gone.acct", service=B.SERVICE_VERIFICATION)
    it = FakeInteraction(1)
    run(B.complete_job(it, "1"))
    assert B.jobs[0]["status"] == "completed"
    assert "not proof of a ban" in it.followup.sent[0].embed.description


def test_manual_complete_survives_card_failure(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("Pillow choked")
    monkeypatch.setattr(B.cards, "render_profile_card", boom)
    env.ig.set("abc", (200, profile("abc")))
    env.job("ZM-0001", "abc", service=B.SERVICE_VERIFICATION)
    it = FakeInteraction(1)
    run(B.complete_job(it, "1"))
    assert B.jobs[0]["status"] == "completed"
    assert it.followup.sent[0].files == []                # text-only, not a crash


def test_manual_complete_save_failure_keeps_job_active(env, monkeypatch):
    env.ig.set("abc", (200, profile("abc")))
    env.job("ZM-0001", "abc", service=B.SERVICE_VERIFICATION)
    before = env.disk_jobs()

    def fail(path, payload):
        raise B.storage.PersistenceError("disk full")
    monkeypatch.setattr(B.storage, "write_json", fail)
    it = FakeInteraction(1)
    run(B.complete_job(it, "1"))
    assert B.jobs[0]["status"] == "active"
    assert "closed_by" not in B.jobs[0]
    assert env.disk_jobs() == before
    assert "could not be saved" in it.followup.sent[0].content


# ------------------------------------------------------ auto completion

def test_auto_completion_posts_then_saves(env, logs):
    channel = env.dc.setup(1)
    env.job("ZM-0001", "back.again", age=600)
    env.ig.set("back.again", (404, {}), (200, profile("back.again", user_id="999",
                                                      followers=12_345)))
    run(env.tick())
    assert B.jobs[0]["status"] == "active"
    run(env.tick())
    job = B.jobs[0]
    assert job["status"] == "completed"
    assert job["ig_user_id"] == "999"
    assert "closed_by" not in job
    assert 600 <= job["duration_seconds"] <= 610
    assert env.disk_jobs()[0]["status"] == "completed"
    post = [m for m in channel.sent if (m.embed.title or "").startswith("Job Complete")]
    assert len(post) == 1 and len(post[0].files) == 1
    assert "ZM-0001" in post[0].embed.description
    assert any("automatically completed" in line for line in logs)


@pytest.mark.parametrize("reply", [(404, {}), (429, {}), (503, {}),
                                   (200, b"<html>login</html>"), (200, {"data": {}})])
def test_auto_monitor_never_completes_without_ok(env, reply):
    channel = env.dc.setup(1)
    env.job("ZM-0001", "waiting")
    env.ig.set("waiting", reply)
    for _ in range(4):
        run(env.tick())
    assert B.jobs[0]["status"] == "active"
    assert env.disk_jobs()[0]["status"] == "active"
    assert not any((m.embed.title or "").startswith("Job Complete") for m in channel.sent)


def test_auto_completion_delivery_failure_keeps_job_active(env, logs):
    channel = env.dc.setup(1)
    channel.fail_send = http_error(discord.HTTPException, 503, "Service Unavailable")
    env.job("ZM-0001", "ok.now")
    before = env.disk_jobs()
    env.ig.set("ok.now", (200, profile("ok.now")))
    run(env.tick())
    assert B.jobs[0]["status"] == "active"
    assert env.disk_jobs() == before
    assert any("could not be delivered" in line for line in logs)
    channel.fail_send = None
    run(env.tick())
    assert B.jobs[0]["status"] == "completed"


def test_missing_attach_files_permission_still_announces(env):
    channel = env.dc.setup(1)
    channel.fail_send_with_files = http_error(discord.Forbidden, 403, "Missing Permissions")
    env.job("ZM-0001", "ok.now")
    env.ig.set("ok.now", (200, profile("ok.now")))
    run(env.tick())
    assert B.jobs[0]["status"] == "completed"
    post = [m for m in channel.sent if (m.embed.title or "").startswith("Job Complete")]
    assert len(post) == 1 and post[0].files == [] and post[0].embed.image.url is None


def test_card_that_always_fails_eventually_completes_without_it(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("corrupt avatar")
    monkeypatch.setattr(B.cards, "render_profile_card", boom)
    channel = env.dc.setup(1)
    env.job("ZM-0001", "ok.now")
    env.ig.set("ok.now", (200, profile("ok.now")))
    for _ in range(B.CARD_RETRIES_BEFORE_PLAIN):
        run(env.tick())
        assert B.jobs[0]["status"] == "active"
    run(env.tick())
    assert B.jobs[0]["status"] == "completed"              # not retried forever
    assert len([m for m in channel.sent if m.embed.title.startswith("Job Complete")]) == 1


# --------------------------------------------- duplicate completion guard

def test_concurrent_auto_completions_finish_once(env):
    channel = env.dc.setup(1)
    job = env.job("ZM-0001", "racer")
    snap = instagram.Snapshot("racer", instagram.OK, user_id="1")

    async def race():
        await asyncio.gather(*(B.auto_complete_job(job, snap) for _ in range(5)))
    run(race())
    assert job["status"] == "completed"
    assert len([m for m in channel.sent if m.embed.title.startswith("Job Complete")]) == 1
    assert B._completing == set()


def test_manual_and_auto_completion_race_completes_once(env):
    """/complete and the monitor reach the same job at the same moment."""
    channel = env.dc.setup(1)
    env.job("ZM-0001", "racer", service=B.SERVICE_UNBAN)
    env.ig.set("racer", (200, profile("racer")))
    it = FakeInteraction(1)

    async def race():
        await asyncio.gather(B.complete_job(it, "1"), B.run_monitor_tick(),
                             B.complete_job(FakeInteraction(1), "1"))
    run(race())
    job = B.jobs[0]
    assert job["status"] == "completed"
    auto_posts = [m for m in channel.sent if (m.embed.title or "").startswith("Job Complete")]
    manual_cards = [s for s in it.followup.sent if s.embed is not None]
    # Exactly one path finished it: one channel post OR one manual card.
    assert len(auto_posts) + len(manual_cards) == 1
    assert env.disk_jobs()[0]["status"] == "completed"
    assert B._completing == set()


def test_manual_complete_while_monitor_holds_the_job(env):
    env.job("ZM-0001", "held")
    B._completing.add(("1", "ZM-0001"))
    it = FakeInteraction(1)
    run(B.complete_job(it, "1"))
    assert "already being closed" in it.followup.sent[0].content
    assert B.jobs[0]["status"] == "active"


def test_monitor_skips_job_held_by_manual_complete(env):
    env.dc.setup(1)
    env.job("ZM-0001", "held")
    env.ig.set("held", (200, profile("held")))
    B._completing.add(("1", "ZM-0001"))
    run(env.tick())
    assert B.jobs[0]["status"] == "active"
    assert env.ig.profile_reads == []


def test_job_completed_mid_read_is_not_completed_again(env):
    channel = env.dc.setup(1)
    job = env.job("ZM-0001", "abc")
    job["status"] = "completed"
    snap = instagram.Snapshot("abc", instagram.OK)
    run(B.auto_complete_job(job, snap))
    assert channel.sent == []


# ------------------------------------------- persistence and restarting

def test_restart_recovers_active_jobs_and_resumes_monitoring(env):
    env.dc.setup(1)
    B.save_config()
    env.job("ZM-0001", "gamma")
    env.ig.set("gamma", (503, {}), (200, profile("gamma")))

    # --- restart: everything in memory is gone, only the files remain
    B.jobs.clear(); B.config.clear(); B.monitor_status.clear(); B._completing.clear()
    instagram._cache.clear()
    B.load_state()
    assert [j["id"] for j in B.jobs] == ["ZM-0001"] and B.jobs[0]["status"] == "active"
    assert B.config["1"]["monitor_channel_id"] == env.dc.channel(1).id

    run(env.tick())
    assert B.jobs[0]["status"] == "active"
    run(env.tick())
    assert B.jobs[0]["status"] == "completed"


def test_legacy_jobs_json_shape_loads_and_round_trips(env):
    legacy = [
        {"id": "ZM-0001", "guild_id": "1", "username": "kushina.uzk",
         "service": "Instagram Verification", "started": "2026-09-17T07:38:33.365770+00:00",
         "status": "completed", "opened_by": "5", "completed": "2026-09-17T07:39:49+00:00",
         "duration_seconds": 75, "closed_by": "5"},
        {"id": "ZM-0003", "guild_id": "1", "username": "ZM-0002", "service": "Account Support",
         "started": "2026-09-17T07:59:24.440462+00:00", "status": "active", "opened_by": "5"},
        {"id": "ZM-0004", "guild_id": "1", "username": "beachyyallie", "service": "Unban",
         "started": "2026-09-19T11:58:44+00:00", "status": "active", "opened_by": "5",
         "some_future_field": {"kept": True}},
    ]
    B.JOBS_FILE.parent.mkdir(parents=True)
    B.JOBS_FILE.write_text(json.dumps(legacy), encoding="utf-8")
    B.load_state()
    assert B.jobs == legacy
    assert B.next_job_id(1) == "ZM-0005"
    B.save_jobs()
    assert env.disk_jobs() == legacy                        # unknown fields kept


def test_old_jobs_json_next_to_bot_is_imported_once(env):
    (env.tmp / "jobs.json").write_text(json.dumps([{"id": "ZM-0001", "guild_id": "1",
                                                     "status": "active"}]))
    B.load_state()
    assert len(B.jobs) == 1 and B.JOBS_FILE.exists()


def test_handle_taken_over_by_a_different_account_is_not_auto_completed(env, logs):
    channel = env.dc.setup(1)
    env.job("ZM-0001", "taken.over", ig_user_id="1111")
    env.ig.set("taken.over", (200, profile("taken.over", user_id="2222")))
    for _ in range(3):
        run(env.tick())
    assert B.jobs[0]["status"] == "active"
    assert B.jobs[0]["ig_user_id"] == "1111"
    assert not any((m.embed.title or "").startswith("Job Complete") for m in channel.sent)
    assert B.job_state_text(B.jobs[0]).startswith("Handle now on")
    assert sum("different Instagram account" in line for line in logs) == 1
    # A person can still close it by hand, and is told what changed.
    it = FakeInteraction(1)
    run(B.complete_job(it, "1"))
    assert B.jobs[0]["status"] == "completed" and B.jobs[0]["ig_user_id"] == "1111"
    assert "different Instagram account" in it.followup.sent[0].embed.description


def test_same_account_with_stored_id_completes(env):
    env.dc.setup(1)
    env.job("ZM-0001", "same", ig_user_id="3333")
    env.ig.set("same", (200, profile("same", user_id="3333")))
    run(env.tick())
    assert B.jobs[0]["status"] == "completed"
