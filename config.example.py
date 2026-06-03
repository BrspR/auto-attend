import pytz

TIMEZONE = pytz.timezone("Asia/Tehran")

MAX_STAY_MINUTES = 120       # max time to stay in class (covers a full 2h class)
FIRST_TRY_MINUTES = 5        # wait this long after class start before the first attempt
RETRY_INTERVAL_MINUTES = 1   # if an attempt fails, retry every this-many minutes until success/class end
TIMEOUT_GROWTH = 1.5         # multiply page timeouts by this after each failed attempt
MAX_TIMEOUT_SCALE = 4.0      # cap on timeout growth (4x ≈ up to ~2 min) so it never balloons

LMS_MAIN_URL = "https://lmshome.aut.ac.ir"
LMS_NIMA_URL = "https://lms.aut.ac.ir"

# Python weekday: 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat, 6=Sun
# Add each class you want to auto-attend here.
# Do NOT add classes you actually attend in person.
CLASSES = [
    {
        "name": "Class Name",               # any label (used in logs)
        "keywords": ["keyword"],            # not used if URL is cached
        "days": [5, 0],                     # 0=Mon 1=Tue 2=Wed 3=Thu 4=Fri 5=Sat 6=Sun
        "start": "13:00",                   # class start time (24h, Tehran time)
        "end": "15:00",                     # class end time
        "lms": "main",                      # "main" (Fararoom) or "nima" (Nima)
    },
    # Add more classes below...
]
