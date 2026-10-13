"""
Upload reminders — the badges on the sidebar's department tabs.

The hub already knows which days have been saved (every daily save goes
into its own table, one row per date), so instead of a fixed alarm
("upload at 8am") each reminder checks what is actually MISSING. Save
the report and the reminder clears itself; forget a day and it stays
until that day is saved.

THE RULE FOR A DAILY REPORT
---------------------------
Every daily report is saved under the day its data COVERS, not the day
it was uploaded: Daily Check defaults to yesterday, and SFC Daily reads
the date from the report itself (the 12->13 Oct report is saved as
12 Oct). So:

  - each weekday (Mon-Fri) should have a saved row;
  - the most recent weekday before today is "due" (amber) — today is
    when you'd normally upload it;
  - any earlier weekday still missing is "overdue" (red);
  - today itself is never due yet: its data doesn't exist until tomorrow;
  - weekends are ignored.

Only the last LOOKBACK_WORKING_DAYS weekdays are checked, so an old gap
from before reminders existed doesn't nag forever.

ADDING A REMINDER
-----------------
Add an entry to DAILY_REMINDERS: which sidebar tab it belongs to (the id
after 'nav-header-' in the page's sidebar), the menu link it points at,
and the db function that reads that report's saved days.
"""
from datetime import date, datetime, timedelta

import db

# Two working weeks. Long enough to catch a missed day after a holiday,
# short enough that a gap from before reminders existed drops off.
LOOKBACK_WORKING_DAYS = 10

DAILY_REMINDERS = [
    {
        'id': 'maintenance-daily-check',
        'dept': 'maintenance',          # -> sidebar tab #nav-header-maintenance
        'label': 'Daily Check',
        'href': '/daily',               # -> the menu link it decorates, and where a click goes
        'saved_days': db.get_daily_snapshots,
    },
    {
        'id': 'maintenance-sfc-daily',
        'dept': 'maintenance',
        'label': 'SFC Daily',
        'href': '/sfc-daily',
        'saved_days': db.get_sfc_daily_snapshots,
    },
]


def london_today():
    """Today's date at Clamason, not on the server. Render runs on UTC,
    which would be a day out between midnight and 1am in summer time."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo('Europe/London')).date()
    except Exception:
        return datetime.utcnow().date()


def working_days_before(today, n):
    """The n weekdays before `today` (today excluded), oldest first."""
    days, d = [], today
    while len(days) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:          # Mon=0 .. Fri=4
            days.append(d)
    return days[::-1]


def _short(d):
    return f'{d:%a} {d.day} {d:%b}'   # 'Mon 12 Oct' (no leading zero on any OS)


def describe(missing, status):
    """One line for under the menu item, e.g. 'Fri 9 & Mon 12 Oct not saved'."""
    if not missing:
        return ''
    if status == 'due':
        return f'{_short(missing[0])} not saved yet'
    if len(missing) <= 2:
        return ' & '.join(_short(d) for d in missing) + ' not saved'
    return f'{len(missing)} days not saved, oldest {_short(missing[0])}'


def check(reminder, today):
    """Status of one daily reminder: 'ok', 'due' or 'overdue'."""
    window = working_days_before(today, LOOKBACK_WORKING_DAYS)
    rows = reminder['saved_days'](start_date=window[0].isoformat(), end_date=window[-1].isoformat())
    saved = {str(r.get('date'))[:10] for r in rows}
    missing = [d for d in window if d.isoformat() not in saved]

    if not missing:
        status = 'ok'
    elif missing == [window[-1]]:
        status = 'due'                # only the latest weekday — upload it today
    else:
        status = 'overdue'

    return {
        'id': reminder['id'],
        'dept': reminder['dept'],
        'label': reminder['label'],
        'href': reminder['href'],
        'status': status,
        'missing': [d.isoformat() for d in missing],
        'detail': describe(missing, status),
    }


def all_reminders(today=None):
    """Every reminder's current state. One that can't be checked (e.g. the
    database is unreachable) is left out rather than shown as overdue —
    a false alarm would teach people to ignore the badges."""
    today = today or london_today()
    out = []
    for r in DAILY_REMINDERS:
        try:
            out.append(check(r, today))
        except Exception:
            continue
    return {'today': today.isoformat(), 'reminders': out}
