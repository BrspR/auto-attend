# AUT Auto-Attendance

Automatically attends online classes on your behalf via the Amirkabir University of Technology (AUT) LMS systems. Logs in, finds the live session, clicks the attendance button, and holds the session open — all while you do something else.

---

## How it works

- Runs a background scheduler that fires at each class's start time
- At **T+5 min**: logs into the LMS and clicks the "ورود" attendance button
- If that fails: retries at **T+15 min**
- Holds the session open for up to **1h 45m** (or until the class ends)
- Logs everything to the terminal so you can see what's happening

---

## Requirements

- Python 3.11+
- A Linux/macOS machine that stays on during class hours
- Your AUT student ID and password

---

## Setup

```bash
git clone https://github.com/YOUR_USERNAME/auto-attend.git
cd auto-attend

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
playwright install chromium

cp .env.example .env
nano .env          # fill in your student ID and password
```

`.env` contents:
```
LMS_USERNAME=your.student.id
LMS_PASSWORD=YourPassword
```

---

## Configure your classes

Open `config.py` and edit the `CLASSES` list. Each entry is a class you want the script to attend **on your behalf** (i.e. classes you skip).

```python
{
    "name": "نام درس 2",          # any label you want (used in logs)
    "keywords": ["نام درس"],       # not used if URL is cached — safe to leave as-is
    "days": [5, 0],                     # weekdays: 0=Mon 1=Tue 2=Wed 3=Thu 4=Fri 5=Sat 6=Sun
    "start": "13:00",                   # class start time (24h, Tehran time)
    "end": "15:00",                     # class end time
    "lms": "main",                      # "main" or "andishe" — see below
},
```

**Do NOT add classes you actually attend** — the script only handles the ones you skip.

---

## Which LMS to use: `main` vs `andishe`

AUT has two separate online class systems:

| System | URL | `lms` value |
|--------|-----|-------------|
| فراروم (Fararoom) | `lmshome.aut.ac.ir` | `"main"` |
| نیما (Nima) | `lms.aut.ac.ir` | `"andishe"` |

**Almost every class uses `"main"`.**  
The only known exception is **نام درس**, which uses `"andishe"`.

If you're not sure which one your class uses:
- Go to `lmshome.aut.ac.ir/panel/home` when logged in
- If your class appears there → use `"main"`
- If it doesn't → try `lms.aut.ac.ir` → use `"andishe"`

---

## Find your class URLs (do this once)

After configuring `config.py`, run:

```bash
source .venv/bin/activate
python main.py --discover
```

This logs in and finds the URL for each class in `config.py`, saving them to `cache/class_urls.json`. You only need to do this once per semester (URLs change each term).

If a class is not found automatically, add it manually to `cache/class_urls.json`:

```json
{
  "نام درس 2": "https://lmshome.aut.ac.ir/panel/myLesson/COURSE_ID/GROUP/TERM"
}
```

To find the URL manually: log into `lmshome.aut.ac.ir`, click on the class, and copy the URL from your browser.

---

## Test before running

```bash
# Check credentials work on both LMS systems
python main.py --test-login

# See what the script would do today (no clicking)
python main.py --dry-run
```

---

## Run

```bash
source .venv/bin/activate
python main.py
```

Keep the terminal open, or use `screen` / `tmux` to run it in the background:

```bash
screen -S attend
python main.py
# Ctrl+A then D to detach
# screen -r attend to reattach
```

Watch logs live:
```bash
tail -f scheduler.log
```

Stop:
```bash
# find the PID
pgrep -f "python main.py"
kill <PID>
```

**The script must be running while your laptop/PC is on.** It does not run on a server — if your machine is off during class time, it won't attend. Use a machine that stays on, or a cheap VPS.

---

## Updating each semester

Class URLs change every term. At the start of a new semester:
1. Delete `cache/class_urls.json`
2. Update times in `config.py` if your schedule changed
3. Run `python main.py --discover` again

---

## File structure

```
auto-attend/
├── main.py          # entry point + CLI flags
├── config.py        # YOUR SCHEDULE — edit this
├── lms_main.py      # Fararoom (lmshome.aut.ac.ir) automation
├── lms_andishe.py   # Nima (lms.aut.ac.ir) automation
├── scheduler.py     # timing logic
├── requirements.txt
├── .env.example     # copy to .env and fill in credentials
└── cache/           # auto-generated, gitignored
    └── class_urls.json
```

---

## Notes

- The script uses a headless browser (invisible Chrome) — it won't interrupt your work
- Attendance is only recorded when the professor has opened the session; if they open it late the T+15 retry handles it
- `cache/class_urls.json` is gitignored — each user generates their own based on their enrolled groups
- `.env` is gitignored — never commit your password
