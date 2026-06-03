# AUT Auto-Attendance

Automatically logs into AUT's LMS, finds your live class session, clicks the attendance button, and keeps the session open — all in the background while you do something else.

**How it works:**
- Fires at each class's scheduled start time
- Waits **T+5 min**, then logs in and clicks `ورود`
- If it fails (session not live yet, slow network, login hiccup) it **retries every minute** until it works or the class ends — and each retry waits **1.5× longer** for pages to load (capped at 4×), so a slow/unstable connection eventually gets through
- Once in, it **stays in the room** (Listen-only) until the class ends (up to 2h), **auto-answers attendance polls** (نظرسنجی → first option, after a short human-like delay), and posts a plain **«خسته نباشید»** in the chat ~2 min before the end
- Optional: **push notifications** (Bale) when a class is attended or missed

---

## Requirements

- Python 3.11+
- Your AUT student ID and password
- A machine that stays **on** during class hours

---

## Setup

### Linux / macOS

```bash
git clone https://github.com/YOUR_USERNAME/auto-attend.git
cd auto-attend

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

cp .env.example .env
nano .env                  # enter your student ID and password

cp config.example.py config.py
nano config.py             # add your class schedule
```

### Windows

```powershell
git clone https://github.com/YOUR_USERNAME/auto-attend.git
cd auto-attend

python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium

copy .env.example .env
notepad .env               # enter your student ID and password

copy config.example.py config.py
notepad config.py          # add your class schedule
```

---

## Credentials — `.env`

```
LMS_USERNAME=your.student.id
LMS_PASSWORD=YourPassword

# Optional — push notifications via Bale (leave blank to disable)
BALE_BOT_TOKEN=
BALE_CHAT_ID=
```

---

## Notifications (optional)

Get pinged when a class is attended or missed. Telegram/email are blocked from an
Iran server, so this uses **Bale**:

1. In Bale, create a bot with `@botfather`, copy its token into `BALE_BOT_TOKEN`.
2. DM your new bot once, then run `python main.py --notify-test` — it finds your
   `chat_id`, sends a test message, and prints the id to optionally pin in `.env`.

Leave `BALE_BOT_TOKEN` blank and notifications are simply skipped (no errors).

**You get pinged on:** attendance confirmed ✅ / failed ❌, bot start 🟢, polls auto-answered 🗳️, خسته نباشید sent 💬, a join screenshot 📸, plus a **morning plan** and **evening summary**.

**Two-way commands** — DM the bot:
- `/status` — is it running + your next class
- `/today` — today's classes and which are done
- `/log` — the last few log lines

---

## Configure your classes — `config.py`

Copy from `config.example.py` and fill in your own schedule:

```python
CLASSES = [
    {
        "name": "نام درس 2",       # any label (shows in logs)
        "keywords": ["نام درس"],    # used to auto-find the class URL
        "days": [5, 0],                  # 0=Mon 1=Tue 2=Wed 3=Thu 4=Fri 5=Sat 6=Sun
        "start": "13:00",               # class start (24h, Tehran time)
        "end":   "15:00",               # class end
        "lms": "main",                  # "main" = Fararoom, "nima" = Nima
    },
]
```

**Which LMS?**

| System | Site | `lms` value |
|--------|------|-------------|
| فراروم (Fararoom) | `lmshome.aut.ac.ir` | `"main"` |
| نیما (Nima) | `lms.aut.ac.ir` | `"nima"` |

Almost all classes use `"main"`. Use `"nima"` only for classes hosted on `lms.aut.ac.ir` (e.g. نام درس, نام درس).

---

## First-time setup (do once per semester)

**Step 1 — Verify login and see your class list:**

```bash
# Linux/macOS
source .venv/bin/activate
python main.py --test-login

# Windows
.venv\Scripts\activate
python main.py --test-login
```

**Step 2 — Discover and cache class URLs:**

```bash
python main.py --discover
```

This logs in and finds the session URL for each Fararoom class, saving to `cache/class_urls.json`. Nima classes use the announcements page automatically — no URL needed.

If a Fararoom class isn't found automatically, add it manually to `cache/class_urls.json`:
```json
{
  "نام درس 2": "https://lmshome.aut.ac.ir/panel/myLesson/COURSE_ID/GROUP/TERM"
}
```

---

## Every day

### Linux / macOS

```bash
source .venv/bin/activate
python main.py --dry-run   # optional: preview today's classes
python main.py             # start the scheduler
```

Run in the background (survives closing the terminal):
```bash
screen -S attend
source .venv/bin/activate && python main.py
# Ctrl+A then D to detach
# screen -r attend to reattach
```

### Windows

Double-click **`run.bat`** — it activates the environment and starts the scheduler in one click.

Or from PowerShell:
```powershell
.venv\Scripts\activate
python main.py --dry-run   # optional: preview today's classes
python main.py             # start the scheduler
```

Run in the background (survives closing the window):
```powershell
Start-Process python -ArgumentList "main.py" -WindowStyle Minimized
```

---

## Multi-user mode (`--serve`) — run it for a few friends

A single supervisor onboards a small, invite-gated group and runs one worker per
person (their own login, own browser-on-demand, own private Bale messages).

```bash
python main.py --gen-invites 15      # print 15 invite tokens; hand one to each friend
python main.py --serve               # run the supervisor (workers + onboarding bot)
```

A friend activates by DMing the bot:  `/start <token>` → it asks for their LMS
username, then password (the password message is deleted after it's checked), logs
in to verify, and starts taking their attendance automatically — no schedule needed.
Their commands: `/status`, `/stop`, `/start` (resume).

**Storage & honesty:** passwords are encrypted at rest (`users.json` + a `600`
`.users_key`, both gitignored). The key sits on the same box, so this protects a
leaked file, **not** a full server compromise. You are the custodian of everyone's
LMS login — only invite people who understand that, and note that many accounts
logging in from one server IP is detectable by the university.

> Status: the per-user live-session detection and the in-room BBB actions are
> **pending verification on a real live class** — the `bbb_debug_*` dumps capture
> the real page the first time, to confirm selectors.

---

## Run 24/7 on a server (recommended)

So you don't have to keep your own computer on. Any always-on Linux box that can
reach AUT works (an Iranian VPS is ideal).

**Heads-up:** three files are **gitignored** (private), so `git clone` alone is not
enough — copy them to the server too:
`.env` (credentials), `config.py` (your schedule), and `cache/class_urls.json`
(or just run `python main.py --discover` on the server after copying `config.py`).

```bash
# on the server
git clone https://github.com/YOUR_USERNAME/auto-attend.git && cd auto-attend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium     # --with-deps installs Chromium's OS libs (needs sudo)

# copy your private files FROM your computer:
#   scp .env config.py  user@server:~/auto-attend/
#   scp cache/class_urls.json  user@server:~/auto-attend/cache/

python main.py --test-login                 # expect both "SUCCESS"
```

### Simple: keep it running with `tmux`

```bash
tmux new -s attend                          # open a detachable session
source .venv/bin/activate && python main.py
# press Ctrl+B then D to detach — it keeps running after you log out
tmux attach -t attend                       # reattach later to watch logs
```

### Robust: run as a systemd service (auto-restarts, survives reboot)

Create `/etc/systemd/system/auto-attend.service` (replace `YOURUSER`):

```ini
[Unit]
Description=AUT Auto-Attendance Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOURUSER
WorkingDirectory=/home/YOURUSER/auto-attend
Environment=TZ=Asia/Tehran
ExecStart=/home/YOURUSER/auto-attend/.venv/bin/python main.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now auto-attend     # start now + on every boot
journalctl -u auto-attend -f                # watch live logs
```

The schedule is locked to `Asia/Tehran` in code, so classes fire at the right time
regardless of the server's clock. After updating code with `git pull`, restart with
`sudo systemctl restart auto-attend` (your `config.py`/`.env`/cache are untouched).

---

## Watch the logs

```bash
tail -f scheduler.log      # Linux/macOS
Get-Content scheduler.log -Wait   # Windows PowerShell
```

## Stop the scheduler

```bash
pkill -f "python main.py"      # Linux/macOS
taskkill /F /IM python.exe     # Windows
```

---

## New semester checklist

1. Delete `cache/class_urls.json`
2. Update times/days in `config.py` if your schedule changed
3. Run `python main.py --discover` again

---

## File structure

```
auto-attend/
├── main.py             # entry point — all CLI flags
├── config.py           # YOUR schedule (gitignored — private)
├── config.example.py   # template to copy from
├── lms_main.py         # Fararoom automation (lmshome.aut.ac.ir)
├── lms_nima.py         # Nima automation (lms.aut.ac.ir)
├── scheduler.py        # timing and job scheduling
├── run.bat             # Windows one-click launcher
├── requirements.txt
├── .env                # your credentials (gitignored — private)
├── .env.example        # template to copy from
└── cache/
    └── class_urls.json # auto-generated, gitignored
```

> **Your machine must be on and the script must be running during class time.**
> If your laptop sleeps or the terminal closes without `screen`/background mode, attendance won't be recorded.
