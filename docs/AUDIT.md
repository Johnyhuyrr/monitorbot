# Zade Meadows Monitor — audit (2026-10-05)

This audit covers the project as delivered: `bot.py`, `cards.py`, `instagram.py`,
`selftest.py`, `test_auto_monitor.py` and `tryframe.py`, plus the history files
`bot.py.full.patch`, `bot.py.reliability.patch`, `bot.py.backup-2026-09-17` and
`data/jobs.json(.bak)`. It was written before any code changed, and the
baseline commit holds that delivered code.

## 1. History files: what they contain

| File | What it is | Status in `bot.py` |
|---|---|---|
| `bot.py.backup-2026-09-17` | Older build: services *Verification* and *Account Support*, button `zm_account_support`, no Ban Check, no auto-monitor | Superseded. Panels posted by this build still have a dead `zm_account_support` button. |
| `bot.py.full.patch` | Adds the automatic Unban monitor, the shared `_completing` guard, the `/complete` retry with `final_snapshot`, and `completion_embed` | **Applied**: `patch -R --dry-run` is clean. |
| `bot.py.reliability.patch` | Delivers the auto-completion message before saving it as completed | **Applied**: `patch -R --dry-run` is clean. |
| `data/jobs.json` | 5 real jobs. ZM-0003 is an *active* "Account Support" job whose username is `ZM-0002`, a job ID typed by mistake | The legacy service still has to display and close correctly. |
| `data/jobs.json.bak` | One write behind `jobs.json` | The backup rotation works as designed. |

## 2. Architecture

The bot is a single process with three modules:

```
bot.py        discord.py commands.Bot, slash commands, persistent View, modals,
              JSON persistence, dashboard loop (60 s), Unban monitor loop (60 s),
              single-instance lock (TCP port 49221 on 127.0.0.1)
instagram.py  anonymous GET web_profile_info -> Snapshot(state=ok|gone|unknown),
              120 s in-memory cache (UNKNOWN never cached), avatar download
cards.py      Pillow: Instagram-style profile card PNG + animated logo GIF
              (assets/logo.png is never altered)
```

The data model is a flat JSON list in `data/jobs.json`, filtered by `guild_id`, plus
`data/config.json` holding `{guild_id: {monitor_channel_id, dashboard_message_id}}`.

The persisted job fields are `id, guild_id, username, service, started, status,
opened_by`, and after closing also `completed, duration_seconds, closed_by`
(manual only), plus `ig_user_id`. Legacy rows may have no `guild_id` and may have
`service = "Account Support"`.

### Discord flow
1. `setup_hook` registers the persistent view, syncs commands (globally, or to `SYNC_GUILD_ID`), and starts both loops.
2. `on_ready` force-redraws every guild's dashboard. It edits the stored message, or posts a new one and saves its ID.
3. Buttons open modals, and modals call `create_job`, `run_ban_check` or `complete_job`. Slash commands call the same functions.

### Instagram flow
`lookup()` returns the cached result if one exists. Otherwise it does a GET and maps the reply:

| Reply | State |
|---|---|
| 404 | GONE |
| 401, 403, 429, 5xx, any other non-200, non-JSON body | UNKNOWN |
| 200 JSON | parsed; OK if `data.user` exists, otherwise GONE |

For an OK result it also downloads the avatar.

### Monitor flow
Every 60 s the monitor collects the active Unban jobs and runs `check_job` on them, limited by a semaphore of 3:
- OK: `auto_complete_job` builds the card, posts it to the monitor channel and, only if delivery succeeded, marks the job completed and saves.
- GONE or UNKNOWN: no change.

## 3. Bugs found (ordered by severity)

1. **False GONE from malformed or throttled 200 responses.** `_parse` returns GONE for any JSON without `data.user`. That includes Instagram's `{"status":"fail","require_login":true}` and `{"message":"Please wait a few minutes"}` throttle replies. This breaks the rule that an UNKNOWN result must never become GONE.
2. **Wrong-account auto-completion.** When a job has `ig_user_id` and the handle now resolves to a *different* numeric ID (the handle was re-registered by someone else), the monitor still auto-completes. The ID is stored but never compared.
3. **`ig_user_id` not stored on completion.** Auto and manual completion never record the ID from the closing read. An Unban job whose account was gone at creation therefore never gets an ID.
4. **Cross-guild leakage of legacy jobs.** `guild_jobs()` uses `j.get("guild_id", gid)`, so a job without `guild_id` appears in **every** server. Any server can complete it, and `/complete` then stamps that server's ID onto it. The monitor tries to post for such jobs to guild `"None"` and logs an error every minute.
5. **Duplicate dashboards.** `update_dashboard` has no per-guild lock. `on_ready`, the dashboard loop, `/setup`, Refresh and job events can run concurrently, and when no message ID is stored yet, each one posts a new dashboard. Running `/setup` again also leaves the previous dashboard orphaned.
6. **Manual `/complete` saves before rendering.** It saves `completed` first, then renders the card and replies. If the render throws, the job is closed with an error shown. The guard is also released before the save, so the critical section is narrower than it looks.
7. **Persistence failures are silent.** `_write_json` logs and swallows every exception, so callers believe the save succeeded.
8. **Backup rotation can destroy the good copy.** If `jobs.json` is corrupt and `.bak` is good, the next save moves the corrupt primary *into* `.bak`. There is also a window, between `path.replace(backup)` and `os.replace(tmp, path)`, where no primary exists. A file that parses to the wrong type (`{}` for jobs) is silently treated as empty.
9. **Usernames are not validated.** Anything up to 100 characters is accepted (including `ZM-0002`, as in the real data) and inserted raw into the Instagram URL. Instagram handles are `[A-Za-z0-9._]{1,30}`.
10. **No rate-limit backoff.** After a 429 the monitor fires the remaining checks in the same tick and keeps polling every 60 s. `/bancheck` and `/newjob` bypass the semaphore, so Instagram requests are unbounded under user load.
11. **Stale avatar from the cache.** A cached OK snapshot read with `want_avatar=False` is served to a caller that wants the avatar, so the card shows no photo.
12. **Timeout mislabelled.** The code catches `asyncio.TimeoutError`, but httpx raises `httpx.TimeoutException`, so timeouts are reported as "could not reach Instagram".
13. **The monitor's `gather` lacks `return_exceptions`.** One failing job aborts the tick's error handling. Loop restarts rely on calling `restart()` from inside the error handler, which works in discord.py 2.7 only because `after_loop` happens not to suspend.
14. **Commands work in DMs.** `/setup` in a DM writes `config["None"]`, and `/jobs` and `/stats` in a DM show the guild-less legacy jobs.
15. **Tests write production files.** Importing `bot.py` creates `data/` and `logs/`, reads the real `jobs.json`, and attaches a handler to the real `logs/bot.log`. The delivered `bot.log` contains test output (`Lookup of no.route failed`).
16. **Blocking render.** Card rendering and the first logo render run synchronously on the event loop. They are cheap but block heartbeats.
17. **Fragile font fallback.** `cards._font` falls back to the bitmap `load_default()`, which has no `.size` and no `anchor` support, so a host without DejaVu or Arial fonts crashes card rendering.
18. **Dead legacy button.** Panels posted by the old build have a `zm_account_support` button, and clicking it gives "This interaction failed".
19. **Warning spam.** The bot logs "Privileged message content intent is missing" at every start, because it has a string prefix but no message-content intent. It also uses `Intents.default()` where only `guilds` is needed.
20. **No graceful SIGTERM.** systemd's stop kills the process mid-flight. Atomic writes keep the data safe, but no "shutdown" line is logged.

## 4. Technical debt
- All state is module-global and loaded at import time, which is hard to test.
- `selftest.py` and `test_auto_monitor.py` run only against hand-written stubs of discord.py and httpx, so real discord.py objects (`len(embed)`, View persistence) are never exercised.
- `pytest` collects 0 tests: both files are scripts with `main()`.
- The patch files and backup sit in the project root.
- The README says "~45 checks", but the suite has 51 + 73.

## 5. Security
- **The `.env` handed over contains a live-format Discord bot token (72 chars).** Treat it as compromised and reset it in the Developer Portal. It is not committed, and `.gitignore` now blocks `.env`. **This repository is public.**
- `data/jobs.json` holds real guild and user IDs. These are not secrets, but they are excluded from the public repo.
- The avatar URL taken from the Instagram response is fetched unrestricted. It should be restricted to https on Instagram/Facebook CDN hosts.
- No self-bot or user-token behaviour: the bot uses the official bot token and slash commands only.
- Instagram reads are anonymous scraping of a non-public API. They are fragile and governed by Instagram's terms; see Known limitations in the README.

## 6. Missing tests
- No tests run against real discord.py.
- Missing Instagram cases: malformed 200 JSON, `require_login` JSON, timeouts, avatar host restriction, avatar size cap, rate-limit backoff.
- Missing multi-guild isolation tests, including legacy jobs without `guild_id`.
- Missing persistence tests: interrupted write, corrupt primary with a good backup, wrong-type JSON.
- Missing tests for concurrent dashboard creation and for recovering a deleted dashboard.
- Missing tests for the wrong-account (ID mismatch) guard and for manual-completion ordering.

## 7. Baseline (before changes)

| Command | Result |
|---|---|
| `python selftest.py` | 51 PASS, 0 FAIL |
| `python test_auto_monitor.py` | 73 PASS, 0 FAIL (the traceback printed is TEST 8's intentional failure) |
| `pytest` | collected 0 items |
| `import bot` with real discord.py 2.7.1 | OK: 8 commands, persistent view with the 5 custom_ids |

## 8. Plan

The work goes in priority order. Each step is a small change with tests.

1. **Data integrity** (`storage`):
   - safe backup rotation, so a primary always exists
   - write failures raise
   - type validation with fallback to `.bak`
   - quarantine of corrupt files
   - loading moved out of import time
   - test isolation
2. **Instagram correctness**:
   - strict OK/GONE/UNKNOWN parsing
   - username validation and URL encoding
   - shared request gate with concurrency and spacing
   - global rate-limit cooldown that honours `Retry-After` and is never cached as state
   - avatar host and size limits
   - cache that respects `want_avatar`
3. **Completion correctness**:
   - guard held for the whole manual completion
   - render before saving, with the save checked
   - `ig_user_id` stored
   - ID-mismatch guard that never auto-completes a different account
4. **Multi-guild**:
   - guild-less legacy jobs hidden from every guild
   - adopted only when exactly one guild is configured, or via `LEGACY_GUILD_ID`
   - `guild_only` on all commands
5. **Dashboard**:
   - per-guild lock
   - `/setup` reuses or cleans up the previous dashboard
   - per-job monitor state shown
   - recent completions shown
   - log de-duplication
6. **Loops**:
   - bodies never raise
   - `gather(return_exceptions=True)`
   - watchdog restart
7. **Runtime**:
   - minimal intents
   - SIGTERM shutdown
   - token-redacting log filter
   - data-directory file lock alongside the port lock
   - legacy button handler
8. **Tests**:
   - keep both suites, adjusting only where the behaviour intentionally changed and saying so in the test
   - add a pytest suite against real discord.py and `httpx.MockTransport`
   - pytest also runs the two legacy scripts

## 9. Resolution

| # | Fix | Proven by |
|---|---|---|
| 1 | `_parse` returns GONE only for an explicit `data.user: null`. Throttle and login JSON, malformed bodies, and replies about another username are UNKNOWN. | `test_instagram.py::test_please_wait_json_*`, `test_malformed_json_*` |
| 2 | `different_account()`: a different numeric ID is never auto-completed. The dashboard shows it, and a manual close carries a note. | `test_jobs.py::test_handle_taken_over_*`, monitor TEST 13 |
| 3 | `apply_completion` stores `ig_user_id` from the confirming read. | `test_auto_completion_posts_then_saves`, monitor TEST 14 |
| 4 | `guild_jobs` matches the guild exactly. Orphans are adopted only via `LEGACY_GUILD_ID` or a single configured guild. | `test_dashboard.py::test_legacy_jobs_*`, `test_guild_a_*` |
| 5 | Per-guild dashboard lock. `/setup` reuses the dashboard in the same channel and deletes the old one when the channel changes. | `test_concurrent_updates_never_create_duplicate_dashboards` (mutation-verified), `test_setup_*` |
| 6 | Manual completion holds the guard until the save. It renders first and re-checks the status, and a failed save reverts the job. | `test_manual_complete_*`, `test_manual_and_auto_completion_race_completes_once` |
| 7 | `storage.write_json` raises `PersistenceError`. `save_jobs()` returns a bool and failed saves are retried by the dashboard loop. | `test_bot_save_failure_is_reported_and_retried` |
| 8 | The primary is copied (not moved) to `.bak`. A damaged primary is quarantined, and a wrong-typed file falls back to `.bak`. | `test_storage.py` (interrupted write, damaged primary, both damaged) |
| 9 | `normalize_handle` validates handles (it also accepts profile links). The handle is URL-quoted. | `test_invalid_handle_never_reaches_instagram`, `test_invalid_username_is_rejected_*` |
| 10 | Bot-wide gate of 3 concurrent requests spaced 1 s apart. Cooldown runs 30 s → 15 min and honours `Retry-After`. `/bancheck` has a slash-command cooldown. | `test_concurrency_is_bounded`, `test_requests_are_spaced`, `test_429_*`, monitor TEST 12 |
| 11 | The cache is skipped when the caller wants an avatar the cached read lacks. GONE is cached 30 s (less than the monitor interval). | `test_cache_without_picture_*`, `test_gone_is_cached_only_briefly` |
| 12 | `httpx.TimeoutException` is reported as a timeout. | `test_timeout_is_unknown_and_says_timeout` |
| 13 | Loop bodies never raise. `gather(return_exceptions=True)`. Error handlers schedule a restart after the task ends, and each loop watches the other. | `test_loop_bodies_swallow_errors`, `test_a_crashed_loop_is_started_again` |
| 14 | `guild_only` on every command except `/ping`. `default_permissions(manage_guild)` on `/setup` and `/panel`. | `test_command_set_is_preserved`, `test_commands_refuse_dms` |
| 15 | No I/O at import. `setup_logging()` and `load_state()` run from `main()`. `ZM_DATA_DIR` and `ZM_LOG_DIR` overrides. | `test_importing_the_bot_writes_nothing`, `test_paths_*` |
| 16 | The card and the first logo render run via `asyncio.to_thread`. | |
| 17 | The font fallback uses `load_default(size=)` and copes with bitmap fonts. | |
| 18 | `LegacyButtons` answers `zm_account_support`. | `test_panel_buttons_are_persistent_with_stable_ids` |
| 19 | `Intents.none() + guilds`. `when_mentioned` prefix, so there is no warning. | `test_only_the_guilds_intent_is_requested` |
| 20 | SIGTERM triggers `bot.close()`. "Shut down." is logged. A data-folder file lock sits beside the port lock. | `test_second_instance_exits_cleanly`, `test_port_lock_*`, `test_data_lock_*` |

Two further problems were found while testing:

- **Missing "Attach Files" permission.** The README omitted it, so every automatic completion failed forever. Posts now fall back to a text-only embed.
- **Cards that fail the same way every time.** Such a card was retried forever. It is now posted without the image after 2 attempts.

Mutation check: each guard above was reverted in a scratch copy, and the pytest suite failed for all of them. The exceptions are two belt-and-braces duplicates, which can't be caught without a different guard firing first.
