"""Instagram lookups: the OK / GONE / UNKNOWN state model, caching, rate
limits and the profile data captured from a successful read."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from conftest import AVATAR, AVATAR_URL, instagram, profile


def lookup(handle: str, **kw) -> instagram.Snapshot:
    return asyncio.run(instagram.lookup(handle, **kw))


# ----------------------------------------------------------------- OK

def test_successful_profile_captures_everything(env):
    env.ig.set("kushina.uzk", (200, profile("kushina.uzk", user_id="17841400000000000",
                                            followers=2_300_000, following=231, posts=87,
                                            verified=True, private=False)))
    snap = lookup("kushina.uzk")
    assert snap.state == instagram.OK
    assert snap.headline == "Reachable"
    assert snap.user_id == "17841400000000000"          # numeric id kept
    assert snap.username == "kushina.uzk"
    assert snap.full_name == "Name of kushina.uzk"
    assert snap.verified is True and snap.private is False
    assert (snap.posts, snap.followers, snap.following) == (87, 2_300_000, 231)
    assert snap.avatar == AVATAR                          # real picture downloaded
    assert env.ig.avatar_reads == [AVATAR_URL]


def test_private_profile_is_still_reachable(env):
    env.ig.set("hidden", (200, profile("hidden", private=True)))
    snap = lookup("hidden")
    assert snap.state == instagram.OK and snap.private
    assert "private" in snap.note.lower()


def test_handle_forms_are_normalised(env):
    env.ig.set("some.one", (200, profile("some.one")))
    for form in ("@some.one", " some.one ", "https://www.instagram.com/some.one/",
                 "instagram.com/some.one?igsh=abc"):
        instagram._cache.clear()
        assert lookup(form).state == instagram.OK, form


# --------------------------------------------------------------- GONE

def test_404_is_gone_not_banned(env):
    env.ig.set("vanished", (404, {}))
    snap = lookup("vanished")
    assert snap.state == instagram.GONE
    assert snap.headline == "Not reachable"
    assert "ban" not in snap.headline.lower()


def test_explicit_null_user_is_gone(env):
    env.ig.set("nobody", (200, {"status": "ok", "data": {"user": None}}))
    assert lookup("nobody").state == instagram.GONE


# ------------------------------------------------------------ UNKNOWN

@pytest.mark.parametrize("status", [429, 401, 403])
def test_refusals_are_unknown_and_never_cached(env, status):
    env.ig.set("throttled", (status, {"message": "Please wait a few minutes"}))
    snap = lookup("throttled")
    assert snap.state == instagram.UNKNOWN
    assert snap.headline == "Could not check"
    assert not instagram._cache


@pytest.mark.parametrize("status", [500, 502, 503, 400, 418])
def test_other_statuses_are_unknown(env, status):
    env.ig.set("odd", (status, {}))
    assert lookup("odd").state == instagram.UNKNOWN


def test_login_wall_html_is_unknown(env):
    env.ig.set("walled", (200, b"<html><body>Log in to Instagram</body></html>"))
    snap = lookup("walled")
    assert snap.state == instagram.UNKNOWN


@pytest.mark.parametrize("body", [
    {"message": "Please wait a few minutes before you try again.", "require_login": True,
     "status": "fail"},
    {"status": "fail", "message": "login_required"},
    {"spam": True, "status": "fail"},
])
def test_please_wait_json_with_200_is_unknown_not_gone(env, body):
    # The delivered build mapped these to GONE: "no data.user" == "no account".
    env.ig.set("waiting", (200, body))
    snap = lookup("waiting")
    assert snap.state == instagram.UNKNOWN
    assert snap.state != instagram.GONE


@pytest.mark.parametrize("body", [
    {}, {"data": {}}, {"data": None}, {"data": {"user": "nonsense"}},
    {"data": {"user": {}}}, [], ["data"], {"status": "ok"},
])
def test_malformed_json_is_unknown(env, body):
    env.ig.set("garbled", (200, body))
    assert lookup("garbled").state == instagram.UNKNOWN


def test_reply_for_a_different_username_is_unknown(env):
    env.ig.set("asked.for", (200, profile("someone.else")))
    assert lookup("asked.for").state == instagram.UNKNOWN


def test_network_failure_is_unknown(env):
    env.ig.set("offline", httpx.ConnectError("connection refused"))
    snap = lookup("offline")
    assert snap.state == instagram.UNKNOWN
    assert snap.note == instagram.NOTE_OFFLINE


def test_timeout_is_unknown_and_says_timeout(env):
    env.ig.set("slow", httpx.ReadTimeout("timed out"))
    snap = lookup("slow")
    assert snap.state == instagram.UNKNOWN
    assert snap.note == instagram.NOTE_TIMEOUT


def test_invalid_handle_never_reaches_instagram(env):
    for bad in ("ZM-0002", "a b", "x" * 31, "../etc", "name&foo=bar", "名前"):
        snap = lookup(bad)
        assert snap.state == instagram.UNKNOWN, bad
    assert env.ig.profile_reads == []


def test_missing_httpx_is_unknown(env, monkeypatch):
    monkeypatch.setattr(instagram, "HTTPX_AVAILABLE", False)
    snap = lookup("anyone")
    assert snap.state == instagram.UNKNOWN and "httpx" in snap.note


# ------------------------------------------------------------- caching

def test_repeat_lookup_is_served_from_cache(env):
    env.ig.set("cached", (200, profile("cached")))
    lookup("cached")
    lookup("cached")
    lookup("CACHED")
    assert env.ig.profile_reads == ["cached"]


def test_cache_expires(env):
    env.ig.set("cached", (200, profile("cached")))
    lookup("cached")
    env.clock.advance(instagram.CACHE_SECONDS + 1)
    lookup("cached")
    assert env.ig.profile_reads == ["cached", "cached"]


def test_gone_is_cached_only_briefly(env):
    env.ig.set("flip", (404, {}), (200, profile("flip")))
    assert lookup("flip").state == instagram.GONE
    assert lookup("flip").state == instagram.GONE           # cached
    assert env.ig.profile_reads == ["flip"]
    env.clock.advance(instagram.GONE_CACHE_SECONDS + 1)
    assert instagram.GONE_CACHE_SECONDS < 60                # < one monitor interval
    assert lookup("flip").state == instagram.OK             # not stuck forever


def test_unknown_is_never_cached(env):
    env.ig.set("flaky", (503, {}), (200, profile("flaky")))
    assert lookup("flaky").state == instagram.UNKNOWN
    assert lookup("flaky").state == instagram.OK


def test_cache_without_picture_is_not_reused_for_a_card(env):
    env.ig.set("pic", (200, profile("pic")))
    first = lookup("pic", want_avatar=False)
    assert first.avatar is None
    second = lookup("pic", want_avatar=True)
    assert second.avatar == AVATAR


def test_use_cache_false_reads_again(env):
    env.ig.set("fresh", (200, profile("fresh")))
    lookup("fresh")
    lookup("fresh", use_cache=False)
    assert len(env.ig.profile_reads) == 2


# --------------------------------------------------------- rate limits

def test_429_pauses_reads_then_resumes(env):
    env.ig.set("a", (429, {}))
    env.ig.set("b", (200, profile("b")))
    assert lookup("a").state == instagram.UNKNOWN
    snap = lookup("b")                                       # inside the pause
    assert snap.state == instagram.UNKNOWN
    assert "Next try in" in snap.note
    assert env.ig.profile_reads == ["a"]                     # b never went out
    env.clock.advance(instagram.cooldown_remaining("api") + 1)
    assert lookup("b").state == instagram.OK


def test_pause_grows_and_is_capped(env):
    env.ig.set("a", (429, {}))
    pauses = []
    for _ in range(12):
        lookup("a")
        pauses.append(instagram.cooldown_remaining("api"))
        env.clock.advance(pauses[-1] + 1)
    assert pauses[1] > pauses[0]
    assert max(pauses) <= instagram.COOLDOWN_MAX_SECONDS


def test_retry_after_is_honoured(env):
    env.ig.set("a", httpx.Response(429, json={}, headers={"Retry-After": "300"}))
    lookup("a")
    assert 299 <= instagram.cooldown_remaining("api") <= 300


def test_concurrency_is_bounded(env, monkeypatch):
    """However many lookups are fired at once, Instagram never sees more
    than MAX_CONCURRENT requests in flight."""
    in_flight = {"now": 0, "max": 0}

    async def slow_handler(request: httpx.Request) -> httpx.Response:
        in_flight["now"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["now"])
        await asyncio.sleep(0.01)
        in_flight["now"] -= 1
        handle = request.url.params.get("username", "")
        return httpx.Response(200, json=profile(handle, pic=None))

    monkeypatch.setattr(instagram, "_transport", httpx.MockTransport(slow_handler))

    async def burst():
        return await asyncio.gather(*(instagram.lookup(f"user{i}") for i in range(12)))

    results = asyncio.run(burst())
    assert all(r.state == instagram.OK for r in results)
    assert in_flight["max"] <= instagram.MAX_CONCURRENT


def test_requests_are_spaced(env, monkeypatch):
    import time
    starts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        starts.append(time.monotonic())
        return httpx.Response(200, json=profile(request.url.params["username"], pic=None))

    monkeypatch.setattr(instagram, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(instagram, "MIN_GAP_SECONDS", 0.05)

    async def burst():
        await asyncio.gather(*(instagram.lookup(f"s{i}") for i in range(4)))

    asyncio.run(burst())
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert all(g >= 0.045 for g in gaps), gaps


# -------------------------------------------------------------- avatar

def test_avatar_from_unexpected_host_is_not_fetched(env):
    env.ig.set("sneaky", (200, profile("sneaky", pic="https://evil.example.com/a.jpg")))
    snap = lookup("sneaky")
    assert snap.state == instagram.OK and snap.avatar is None
    assert env.ig.avatar_reads == []


def test_avatar_over_http_is_not_fetched(env):
    env.ig.set("plain", (200, profile("plain", pic="http://scontent.cdninstagram.com/a.jpg")))
    assert lookup("plain").avatar is None
    assert env.ig.avatar_reads == []


def test_avatar_too_large_is_dropped(env, monkeypatch):
    monkeypatch.setattr(instagram, "AVATAR_MAX_BYTES", 10)
    env.ig.set("big", (200, profile("big")))
    snap = lookup("big")
    assert snap.state == instagram.OK and snap.avatar is None


def test_avatar_failure_does_not_spoil_the_read(env):
    env.ig.avatar_reply = httpx.ConnectError("cdn down")
    env.ig.set("nopic", (200, profile("nopic")))
    snap = lookup("nopic")
    assert snap.state == instagram.OK and snap.avatar is None


def test_no_profile_picture(env):
    env.ig.set("blank", (200, profile("blank", pic=None)))
    snap = lookup("blank")
    assert snap.state == instagram.OK and snap.avatar is None
    assert env.ig.avatar_reads == []


# ------------------------------------------- fallback: the profile page

from conftest import profile_page  # noqa: E402


def test_api_401_falls_back_to_the_profile_page(env):
    """What happened on the first real run: the API answered 401."""
    env.ig.set("j00hnyx", (401, {"message": "Please wait a few minutes", "status": "fail"}))
    env.ig.set_page("j00hnyx", (200, profile_page("j00hnyx", name="John", followers="12.3K",
                                                  following="1,001", posts="42")))
    snap = lookup("j00hnyx")
    assert snap.state == instagram.OK and snap.source == "page"
    assert (snap.followers, snap.following, snap.posts) == (12_300, 1_001, 42)
    assert snap.full_name == "John"
    assert snap.avatar == AVATAR                           # og:image, allowed host
    assert snap.user_id is None and snap.private is None and snap.verified is False
    assert snap.headline == "Reachable"


def test_page_is_not_read_when_the_api_answers(env):
    env.ig.set("fine", (200, profile("fine")))
    env.ig.set("gone", (404, {}))
    env.ig.set("broken", (503, {}))
    env.ig.set("offline", httpx.ConnectError("down"))
    for handle in ("fine", "gone", "broken", "offline"):
        lookup(handle)
    assert env.ig.page_reads == []


def test_while_the_api_is_paused_only_the_page_is_read(env):
    env.ig.set("a", (429, {}))
    env.ig.set_page("a", (200, profile_page("a")))
    env.ig.set_page("b", (200, profile_page("b")))
    assert lookup("a").state == instagram.OK
    assert lookup("b").state == instagram.OK
    assert env.ig.profile_reads == ["a"]                   # API not retried during pause
    assert env.ig.page_reads == ["a", "b"]


def test_page_id_and_private_flag_are_used_when_present(env):
    env.ig.set("p", (401, {}))
    env.ig.set_page("p", (200, profile_page("p", user_id="17841400000000123", private=True)))
    snap = lookup("p")
    assert snap.user_id == "17841400000000123" and snap.private is True


def test_page_without_a_display_name(env):
    env.ig.set("noname", (401, {}))
    env.ig.set_page("noname", (200, profile_page("noname", name=None)))
    snap = lookup("noname")
    assert snap.state == instagram.OK and snap.full_name is None


def test_page_404_is_gone(env):
    env.ig.set("vanished", (401, {}))
    env.ig.set_page("vanished", (404, b"<html></html>"))
    assert lookup("vanished").state == instagram.GONE


def test_page_not_available_text_is_gone(env):
    env.ig.set("vanished", (401, {}))
    env.ig.set_page("vanished", (200, b"<html><body>Sorry, this page isn't available."
                                      b"</body></html>"))
    assert lookup("vanished").state == instagram.GONE


@pytest.mark.parametrize("page", [
    (200, b"<html><head><title>Instagram</title></head><body>Log in</body></html>"),
    (429, b""),
    (401, b""),
    (500, b""),
    (200, b"\x89PNG not html"),
    (200, profile_page("someone.else")),                    # page about another handle
    (200, b'<meta property="og:description" content="Followers? who knows" />'),
])
def test_page_that_cannot_answer_is_unknown_never_gone(env, page):
    env.ig.set("x.y", (401, {}))
    env.ig.set_page("x.y", page)
    snap = lookup("x.y")
    assert snap.state == instagram.UNKNOWN
    assert not instagram._cache


def test_login_redirect_on_the_page_is_unknown_and_pauses_the_page(env):
    env.ig.set("x.y", (401, {}))
    redirect = httpx.Response(302, headers={"Location":
                                            "https://www.instagram.com/accounts/login/?next=/x.y/"})
    env.ig.set_page("x.y", redirect)
    env.ig.pages["accounts/login"] = [(200, b"<html>Log in</html>")]
    snap = lookup("x.y")
    assert snap.state == instagram.UNKNOWN
    assert instagram.cooldown_remaining("page") > 0
    assert "login" in snap.note.lower()


def test_both_sources_refusing_keeps_the_api_reason(env):
    env.ig.set("x.y", (401, {}))
    env.ig.set_page("x.y", (429, b""))
    snap = lookup("x.y")
    assert snap.state == instagram.UNKNOWN
    assert snap.note == instagram.NOTE_LOGIN                 # 401 = login wanted, not "rate limit"
    assert instagram.cooldown_remaining("api") > 0 and instagram.cooldown_remaining("page") > 0
    reads = (len(env.ig.profile_reads), len(env.ig.page_reads))
    lookup("x.y")
    assert (len(env.ig.profile_reads), len(env.ig.page_reads)) == reads   # both paused


def test_429_note_still_says_rate_limit(env):
    env.ig.set("x.y", (429, {}))
    assert lookup("x.y").note == instagram.NOTE_THROTTLED


def test_fallback_can_be_switched_off(env, monkeypatch):
    monkeypatch.setattr(instagram, "PAGE_FALLBACK", False)
    env.ig.set("x.y", (401, {}))
    env.ig.set_page("x.y", (200, profile_page("x.y")))
    assert lookup("x.y").state == instagram.UNKNOWN
    assert env.ig.page_reads == []


def test_page_avatar_from_unexpected_host_is_ignored(env):
    env.ig.set("x.y", (401, {}))
    env.ig.set_page("x.y", (200, profile_page("x.y", pic="https://evil.example/a.jpg")))
    snap = lookup("x.y")
    assert snap.state == instagram.OK and snap.avatar is None and env.ig.avatar_reads == []


@pytest.mark.parametrize("text,value", [("1,234", 1234), ("12.3K", 12_300), ("2.3M", 2_300_000),
                                        ("987", 987), ("1B", 1_000_000_000), ("abc", None)])
def test_page_count_formats(text, value):
    assert instagram._count(text) == value
