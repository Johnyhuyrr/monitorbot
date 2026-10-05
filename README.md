# Zade Meadows Monitor — setup and 24/7 hosting

## 1. Do this first: replace your token

The `.env` file you shared contains a live bot token. Anyone who has it controls your bot completely. Treat it as already compromised.

1. Go to <https://discord.com/developers/applications> → your app → **Bot**
2. Click **Reset Token**, copy the new one
3. Put it in a new `.env` file (copy `.env.example` and rename it)
4. Never put `.env` in a GitHub repo — Discord scans public repos and auto-resets leaked tokens. If that has happened to you before, it explains a lot of the random "bot stopped working" moments.

## 2. Folder layout

```
zade-bot/
├── bot.py
├── cards.py           <- draws the Instagram mini frame and the moving logo
├── instagram.py       <- anonymous public-profile reads
├── selftest.py        <- checks everything offline: python selftest.py
├── tryframe.py        <- renders the frame for a real handle: python tryframe.py someone
├── assets/logo.png    <- your logo, used exactly as supplied
├── .env               <- your token (never share)
├── requirements.txt
├── start_bot.bat      <- Windows launcher
├── zade-bot.service   <- Linux service file
├── data/              <- created automatically: jobs.json, config.json, backups
└── logs/              <- created automatically: bot.log
```

Your old `jobs.json` is imported automatically the first time you run the new bot. Keep it next to `bot.py` for that first run.

## 3. Install and run

```bash
pip install -r requirements.txt
python bot.py
```

Windows: double-click `start_bot.bat`. It now restarts the bot automatically if it ever exits.

## 4. Invite the bot so anyone can add it

Developer Portal → **OAuth2 → URL Generator**:

- Scopes: `bot`, `applications.commands`
- Permissions: View Channels, Send Messages, Embed Links, Read Message History

Copy the generated link. Anyone with **Manage Server** in their own Discord can now add your bot. Each server keeps its own jobs and its own dashboard — nothing is shared between servers.

## 5. First-time setup inside a server

| Command | Who | What it does |
|---|---|---|
| `/setup` | Admin | Makes the current channel the live dashboard |
| `/panel` | Admin | Posts a control panel with buttons anywhere |
| `/newjob` | Anyone | Opens a job (Verification or Unban) |
| `/bancheck` | Anyone | Looks a handle up right now; opens no job |
| `/complete` | Anyone | Closes a job by ID (`ZM-0001` or just `1`) |
| `/jobs` | Anyone | Lists active jobs |
| `/stats` | Anyone | Server statistics |
| `/ping` | Anyone | Confirms the bot is alive |

Slash commands can take up to an hour to appear worldwide the first time. To see them instantly while testing, put your server ID in `SYNC_GUILD_ID` in `.env`.

## 5a. The Instagram mini frame

Every job alert and every ban check carries a rendered card showing that
individual's real profile picture, handle, verified tick, and post / follower /
following counts, drawn in Instagram's dark style. The picture and the numbers
come from an anonymous read of the public profile — no login, no cookies, no
paid API — handled in `instagram.py` and drawn in `cards.py`.

Two things to know:

- Anonymous reads get rate limited. When that happens the card says **Could not
  check**, not "banned". A failed read is never treated as a ban.
- Instagram gives the identical "not found" answer for an account that was
  banned, deleted, deactivated, or simply renamed. The bot therefore says **Not
  reachable** and spells that out underneath. The numeric Instagram user id is
  saved with each job (`ig_user_id`) because it is the only reliable way to tell
  a rename from a real loss afterwards.

Your logo lives at `assets/logo.png` and is never redrawn — the animated
thumbnail is that exact file with a light sweeping across it, generated once at
start-up and cached in memory.

If Pillow or httpx are missing, the bot still runs: embeds simply arrive without
pictures. Install both with `pip install -r requirements.txt`.

To see the frame for a real account before you even start the bot:

```bash
python tryframe.py kushina.uzk
```

That writes `previews/kushina.uzk.png` — the exact picture the bot would attach
for that person. And after any edit to `bot.py`, `cards.py` or `instagram.py`:

```bash
python selftest.py
```

runs ~45 checks offline (no Discord, no Instagram) covering the embed size
limits, the button IDs, the attachments, and the rule that a handle which does
not answer is never written up as a ban.

## 6. Hosting it 24/7

Your PC is the weakest link — sleep, updates, Wi-Fi drops and reboots all kill the bot. Pick one:

**Cheap VPS (recommended).** Hetzner (~€4/mo), DigitalOcean ($4–6/mo), or Oracle Cloud's free ARM tier. Ubuntu 22.04+:

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
```

`Restart=always` brings the bot back within 15 seconds of any crash, and `enable` starts it automatically after a server reboot. That is what "permanent" actually means.

**Railway / Render / Fly.io.** Easier, around $5/mo. Important: their filesystems are wiped on every redeploy, so **attach a persistent volume mounted at `/app/data`** or you will lose every job. Set `DISCORD_TOKEN` as an environment variable in their dashboard instead of uploading `.env`.

**Staying on Windows.** Use [NSSM](https://nssm.cc) to run `bot.py` as a real Windows service so it survives logout and starts on boot. Also disable sleep: Settings → System → Power → Screen and sleep → Never.

## 7. When something goes wrong

Check `logs/bot.log` first — every error is recorded there with a timestamp. Common messages:

| Log message | Fix |
|---|---|
| Discord rejected the token | Token was reset. Generate a new one. |
| A privileged intent is not enabled | Set `ENABLE_MESSAGE_CONTENT=false`, or tick the intent in the portal. |
| Another copy of this bot is already running | Close the other window. Two copies corrupt your data. |
| Missing permission to post the dashboard | Give the bot Send Messages + Embed Links in that channel. |

## 8. What changed from your old script

| Problem | Fix |
|---|---|
| `jobs.json` corrupted or emptied on crash | Atomic writes plus an automatic `.bak` fallback |
| Data vanished when launched from a different folder | All paths are now absolute, based on the script location |
| Dashboard silently froze forever | The 1024-char embed limit is enforced, and the refresh loop restarts itself on error |
| `/complete` never appeared in Discord | The command tree is now synced on startup |
| Buttons dead after restart | Persistent view registered in `setup_hook`, dashboard message ID saved to disk |
| Duplicate dashboards after every restart | The bot re-attaches to its existing message |
| One hardcoded channel, one server | Per-server config via `/setup` |
| Crash with no explanation | Rotating log file with full tracebacks |
| Two copies fighting over the data | Single-instance lock |
| Emoji crash on Windows console | Forced UTF-8 output |
