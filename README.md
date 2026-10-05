# Zade Meadows Monitor: setup and 24/7 hosting

A Discord bot for running Instagram support jobs. It handles two job types:

- **Verification**: a person does the work and closes the job with `/complete`.
- **Unban**: the bot closes the job itself. It re-checks the handle every 60 s and completes the job the moment Instagram shows the profile again.

## 1. Do this first: replace your token

The `.env` file that was shared with this project contains a live bot token, and this repository is **public**. Anyone who has that token controls your bot completely, so treat it as already compromised:

1. Go to <https://discord.com/developers/applications>, open your app, then open **Bot**.
2. Click **Reset Token** and copy the new one.
3. Copy `.env.example` to `.env` and put the new token in it.
4. Never commit `.env`. `.gitignore` already excludes it.

## 2. Folder layout

```
zade-bot/
├── bot.py             <- the bot: commands, buttons, dashboard, Unban monitor
├── instagram.py       <- anonymous public-profile reads, cache, rate limiting
├── cards.py           <- draws the Instagram mini frame and the moving logo
├── storage.py         <- atomic JSON writes, .bak fallback, data-folder lock
├── assets/logo.png    <- your logo, used exactly as supplied
├── selftest.py        <- original offline checks:    python selftest.py
├── test_auto_monitor.py <- original monitor checks:  python test_auto_monitor.py
├── tests/             <- pytest suite against the real discord.py: pytest
├── tryframe.py        <- renders the frame for a real handle: python tryframe.py someone
├── .env               <- your token (never share, never commit)
├── .env.example       <- every setting, documented
├── requirements.txt   (requirements-dev.txt adds pytest)
├── start_bot.bat      <- Windows launcher, restarts on exit
├── zade-bot.service   <- Linux systemd unit
├── docs/              <- audit report, history files, design preview
├── data/              <- created automatically: jobs.json, config.json, .bak copies, bot.lock
└── logs/              <- created automatically: bot.log (rotating, 5 x 2 MB)
```

All paths are worked out from where `bot.py` lives, never from the current folder. That means the bot finds the same data when it is started from a terminal, Task Scheduler, systemd, or anywhere else.

## 3. Install, test, run

```bash
python -m venv venv
venv/bin/pip install -r requirements-dev.txt     # Windows: venv\Scripts\pip ...

venv/bin/python selftest.py          # original offline checks
venv/bin/python test_auto_monitor.py # original Unban-monitor checks
venv/bin/python -m pytest            # full suite (also runs the two above)

venv/bin/python bot.py
```

On Windows you can double-click `start_bot.bat`. It restarts the bot automatically whenever it exits.

None of the tests touch Discord, Instagram, or your real `data/` and `logs/` folders.

## 4. Invite the bot

In the Developer Portal, go to **OAuth2 → URL Generator** and choose:

- Scopes: `bot`, `applications.commands`
- Permissions: **View Channels, Send Messages, Embed Links, Attach Files, Read Message History**

Attach Files is what carries the logo and the profile card. Without it the bot still works, but it posts plain embeds and logs a warning asking for the permission.

The bot asks Discord only for the `guilds` intent. It needs no privileged intents.

Each server keeps its own jobs, its own dashboard, and its own settings. Nothing is shared between servers.

## 5. Commands

| Command | Who | What it does |
|---|---|---|
| `/setup` | Manage Server | Makes the current channel the live dashboard. Running it again in the same channel keeps the existing dashboard. Running it in a new channel moves the dashboard and removes the old one. |
| `/panel` | Manage Server | Posts a control panel with the five buttons: Verification, Unban, Ban Check, Complete, Refresh |
| `/newjob` | Anyone | Opens a Verification or Unban job. Accepts `name`, `@name`, or a profile link. |
| `/bancheck` | Anyone | Looks a handle up right now. Opens no job. The slash command allows 5 checks per user per minute. |
| `/complete` | Anyone | Closes a job by ID: `ZM-0001`, `zm-1`, or just `1` |
| `/jobs` | Anyone | Lists active jobs, with what the monitor last saw for each |
| `/stats` | Anyone | Server statistics |
| `/ping` | Anyone | Confirms the bot is alive |

All commands except `/ping` work only inside a server.

Slash commands can take up to an hour to appear worldwide the first time. To see them instantly while testing, put your server ID in `SYNC_GUILD_ID`.

You can limit who may use each command in **Server Settings → Integrations**.

## 6. How the Instagram checks behave

Every read ends in one of three states, and the wording is chosen so the bot never claims more than it knows:

| State | Shown as | Meaning |
|---|---|---|
| OK | **Reachable** | Instagram returned a valid public profile. |
| GONE | **Not reachable** | Instagram explicitly said there is no such profile. A ban, a delete, a deactivation and a rename all look the same, so this is **not proof of a ban** and the bot says so. |
| UNKNOWN | **Could not check** | The bot does not know. This covers rate limits (429/401/403), "please wait" or "log in" replies, login-wall pages, timeouts, network errors, malformed replies, a reply about a different username, an invalid handle, or httpx not being installed. |

An UNKNOWN result is never turned into GONE, and nothing is ever called "banned".

**Unban monitor.** Every 60 s it reads each active Unban job, at most 3 at a time:

- OK: the bot completes the job.
- GONE or UNKNOWN: the job stays active.

A completion goes through these steps:

1. The bot draws the profile card.
2. It posts the card to the server's monitor channel.
3. Only after Discord accepts the post does it save the job as completed. It records the completion time, the duration and the Instagram numeric ID.
4. It refreshes the dashboard.

If the post fails, the job stays active and the bot retries on the next check. A job is never marked done without anyone seeing it.

**Rename and handle takeover.** The numeric Instagram ID is saved with each job (`ig_user_id`). If an Unban job's handle comes back owned by a *different* ID, someone else has taken the name. The bot does **not** auto-complete that job: it shows "Handle now on a different account" on the dashboard and leaves the decision to a person.

**Not hammering Instagram.** The bot limits its own traffic in three ways:

- **Bot-wide cap.** Every Instagram request goes through one gate: at most 3 at a time, started at least 1 s apart. This covers the monitor, `/bancheck` and `/newjob` together.
- **Back-off after refusals.** After a refusal, all reads pause for 30 s. The pause doubles on each repeat refusal, up to 15 min, and honours `Retry-After`.
- **Short cache.** Successful reads are cached for 2 min. "Not reachable" is cached for only 30 s, so the monitor really re-checks a missing account on every pass. Failed reads are never cached.

**Duplicate completions.** `/complete` and the monitor share one guard keyed by (server, job ID), so a job can only be completed once.

If Pillow or httpx is missing, the bot still runs. With no Pillow, embeds arrive without pictures. With no httpx, every check says "Could not check".

To see the card for a real account:

```bash
python tryframe.py kushina.uzk      # writes previews/kushina.uzk.png
```

## 7. The live dashboard

The dashboard lists the active jobs with these details:

- the job ID and handle
- the job type
- when the job was opened
- what the monitor last saw for it

It also shows the three most recent completions plus totals for completed jobs, today's count and the average time.

It stays within Discord's embed limits however many jobs there are, and redraws only when something changed.

After a restart the bot re-attaches to the same dashboard message. If that message was deleted, it posts one new dashboard; it never stacks duplicates. A per-server lock stops two redraws from racing.

If either background loop (dashboard or monitor) ever crashes, the crash is logged, the loop is restarted after 5 s, and each loop also watches that the other is running.

## 8. Data and compatibility

`data/jobs.json` is a flat list of jobs. Each job looks like this:

```json
{"id": "ZM-0004", "guild_id": "153...", "username": "name", "service": "Unban",
 "started": "2026-09-19T11:58:44+00:00", "status": "active", "opened_by": "153...",
 "completed": "...", "duration_seconds": 181, "closed_by": "153...", "ig_user_id": "1784..."}
```

`completed`, `duration_seconds` and `closed_by` appear only once a job is closed, and `closed_by` only for manual completions. Fields the bot doesn't know are kept untouched.

`data/config.json` holds one entry per server: `{"<guild id>": {"monitor_channel_id": ..., "dashboard_message_id": ...}}`.

Saving works like this:

- Every save writes a temporary file, flushes it to disk, copies the current file to `.bak`, then swaps the new file in atomically.
- A complete `jobs.json` exists at every moment.
- If the file is ever unreadable, the bot loads `.bak` and keeps the damaged file as `jobs.corrupt-<time>.json`. It never deletes it.

Old data files load as they are:

- Old "Account Support" jobs still display and can still be closed, even one whose "username" is a job ID.
- An old `jobs.json` sitting next to `bot.py` is imported once.
- Old rows without a `guild_id` are hidden from every server until they are assigned. That happens automatically when exactly one server has run `/setup`; otherwise set `LEGACY_GUILD_ID`. (The old build showed them in *every* server.)

Only one bot may use a data folder at a time:

- An OS lock on `data/bot.lock` enforces this.
- The original localhost-port lock (`LOCK_PORT`, default 49221) also stops a second copy on the same machine.
- A second copy logs a clear message and exits with code 3.

## 9. Hosting it 24/7

Your PC is the weakest link: sleep, updates, Wi-Fi drops and reboots all kill the bot. Pick one of these:

**Cheap VPS (recommended).** Hetzner (~€4/mo), DigitalOcean ($4–6/mo), or Oracle Cloud's free ARM tier. On Ubuntu 22.04 or later:

```bash
sudo adduser --disabled-password botuser
sudo -u botuser -i
mkdir zade-bot && cd zade-bot          # upload your files here
python3 -m venv venv
venv/bin/pip install -r requirements.txt
exit

sudo cp /home/botuser/zade-bot/zade-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now zade-bot
sudo systemctl status zade-bot
journalctl -u zade-bot -f
```

`Restart=always` brings the bot back within 15 seconds of any crash, and `enable` starts it after a reboot. `systemctl stop` sends SIGTERM, and the bot then shuts down cleanly.

**Railway, Render or Fly.io.** These are easier and cost around $5/mo. Their filesystems are wiped on every redeploy, so **attach a persistent volume** and point `ZM_DATA_DIR` at it, or you will lose every job. Set `DISCORD_TOKEN` in their dashboard instead of uploading `.env`.

**Staying on Windows.** Use [NSSM](https://nssm.cc) to run `bot.py` as a real Windows service, and turn off sleep.

## 10. When something goes wrong

Check `logs/bot.log` first. Repeated problems, such as a missing channel or a failed dashboard edit, are logged once when they start and once when they clear, not every minute.

| Log message | Fix |
|---|---|
| Discord rejected the token | The token was reset. Generate a new one. |
| A privileged intent is not enabled | Set `ENABLE_MESSAGE_CONTENT=false`. |
| Another copy of this bot is already ... | Close the other copy. Two copies would corrupt your data. |
| Dashboard, guild …: missing permission | Give the bot View Channel, Send Messages, Embed Links and Attach Files in that channel. |
| … no monitor channel set up (/setup) | Run `/setup` in that server. Unban jobs are only checked once it has a monitor channel. |
| Instagram rate limit … Pausing Instagram reads | This is normal. The bot waits and then resumes by itself. |
| … old job(s) have no server recorded | Set `LEGACY_GUILD_ID` in `.env`. |

## 11. Known limitations

- **Unofficial endpoint.** Instagram reads use the same anonymous endpoint instagram.com uses. It is unofficial, it can change or close without notice, and automated reads may conflict with Instagram's terms. If Instagram changes it, most checks will say "Could not check" until `instagram.py` is updated.
- **Throttling.** Anonymous reads from one IP get throttled. Heavy use means slower Unban detection, but never wrong results.
- **Not-found means not reachable, nothing more.** A 404 from that endpoint is taken as "not reachable". If Instagram ever retired the endpoint with 404s, accounts would show "Not reachable" (never "banned"), and no job would be completed because of it.
- **Rename detection needs an ID.** It only works when the ID was captured, either at job creation or at completion. An Unban job opened for an account that was already gone has no ID to compare.
- **Manual completion needs Instagram.** `/complete` re-reads Instagram and will not close a job while Instagram cannot answer, apart from legacy rows that hold no real handle. During a long rate-limit pause, people have to wait.
- **Possible duplicate announcement.** If the process is killed in the instant between posting an automatic completion and saving it, the completion can be announced again after restart. This was a deliberate choice, explained in `auto_complete_job`.
- **What's lost on restart.** The dashboard's per-job "last seen" state is kept in memory only, and is re-learned within one monitor pass after a restart.
- **"Today" uses UTC.**
