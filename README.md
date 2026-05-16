# AUT Auto-Attendance

Automatically logs into AUT's LMS, finds your live class session, clicks the attendance button, and keeps the session open — all in the background while you do something else.

**How it works:**
- Fires at each class's scheduled start time
- Waits **T+5 min**, then logs in and clicks `ورود`
- If the session isn't live yet, retries at **T+15 min**
- Holds the session open until the class ends (max 1h 45m)

---

## First-time setup

```bash
git clone https://github.com/YOUR_USERNAME/auto-attend.git
cd auto-attend

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

cp .env.example .env
nano .env                    # enter your student ID and password
cp config.example.py config.py
nano config.py               # add your classes (see format below)
```

### `.env`
```
LMS_USERNAME=your.student.id
LMS_PASSWORD=YourPassword
```

### `config.py` — your schedule
Copy from `config.example.py` and fill in your classes:

```python
CLASSES = [
    {
        "name": "نام درس 2",       # any label (shows in logs)
        "keywords": ["نام درس"],    # used to find the class URL automatically
        "days": [5, 0],                  # 0=Mon 1=Tue 2=Wed 3=Thu 4=Fri 5=Sat 6=Sun
        "start": "13:00",               # class start (24h, Tehran time)
        "end":   "15:00",               # class end
        "lms": "main",                  # "main" (Fararoom) or "nima" (Nima)
    },
]
```

**Which LMS?**

| System | Site | `lms` value |
|--------|------|-------------|
| فراروم (Fararoom) | `lmshome.aut.ac.ir` | `"main"` |
| نیما (Nima) | `lms.aut.ac.ir` | `"nima"` |

Almost every class uses `"main"`. `"nima"` is typically only for نام درس.

---

## Start of semester (do once)

```bash
source .venv/bin/activate

# 1. Verify your credentials and see your class list
python main.py --test-login

# 2. Discover and cache class URLs (needed before scheduler can run)
python main.py --discover
```

`--discover` saves each class's URL to `cache/class_urls.json`. You only need to redo this at the start of each new semester, since URLs change every term.

If a class isn't found automatically, add it manually to `cache/class_urls.json`:
```json
{
  "نام درس 2": "https://lmshome.aut.ac.ir/panel/myLesson/COURSE_ID/GROUP/TERM"
}
```
To find the URL: log into `lmshome.aut.ac.ir`, open the class, copy the URL from your browser.

---

## Every day

```bash
source .venv/bin/activate

# Optional: preview what runs today (no clicking)
python main.py --dry-run

# Start the scheduler — keep it running all day
python main.py
```

To run in the background (survives closing the terminal):
```bash
screen -S attend
source .venv/bin/activate && python main.py
# Ctrl+A then D  →  detach
# screen -r attend  →  reattach
```

Watch the logs:
```bash
tail -f scheduler.log
```

Stop it:
```bash
pkill -f "python main.py"
```

> **Your machine must be on and the script must be running during class time.**  
> If your laptop sleeps or the script isn't running, attendance won't be recorded.

---

## New semester checklist

1. Delete `cache/class_urls.json`
2. Update `config.py` with your new schedule
3. Run `python main.py --discover` to re-cache URLs

---

## File structure

```
auto-attend/
├── main.py             # entry point — all CLI flags live here
├── config.py           # YOUR schedule (gitignored — private)
├── config.example.py   # template to copy from
├── lms_main.py         # Fararoom automation (lmshome.aut.ac.ir)
├── lms_nima.py         # Nima automation (lms.aut.ac.ir)
├── scheduler.py        # timing and job scheduling
├── requirements.txt
├── .env                # your credentials (gitignored — private)
├── .env.example        # template to copy from
└── cache/
    └── class_urls.json # auto-generated, gitignored
```
