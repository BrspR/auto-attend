import pytz

TIMEZONE = pytz.timezone("Asia/Tehran")

MAX_STAY_MINUTES = 105      # max time to stay in class (1h 45m)
FIRST_TRY_MINUTES = 5       # try attendance at T+5
SECOND_TRY_MINUTES = 15     # retry at T+15 if first failed

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
