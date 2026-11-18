"""
Upload reminders — the badges on the sidebar's department tabs.

The hub already knows which days have been saved (every daily save goes
into its own table, one row per date), so instead of a fixed alarm
("upload at 8am") each reminder checks what is actually MISSING. Save
the report and the reminder clears itself; forget a day and it stays
until that day is saved.

Three kinds of upload rule, all of which skip weekends and NON_WORKING_DAYS
(plus one for PO Approvals, at the end):

DAILY — each working day should have a saved row
------------------------------------------------
Every daily report is saved under the day its data COVERS, not the day
it was uploaded: Daily Check defaults to yesterday, and SFC Daily reads
the date from the report itself (the 12->13 Oct report is saved as
12 Oct). So:
  - the most recent working day before today is "due" (amber) — today
    is when you'd normally upload it;
  - any earlier working day still missing is "overdue" (red);
  - today itself is never due yet: its data doesn't exist until tomorrow.
A day whose report doesn't exist at all can be cleared with the "Ignore"
button under the reminder (skip_day()) — one day at a time.
Daily OEE is the one exception to "saved under the day it covers": its page
fills the date in from the END of the report's period, so Wednesday's
production can be saved as Thursday. Its reminder reads the production day
from the start of the report's own period instead — see oee_production_days().

WEEKLY — each week should have a row for its Monday
---------------------------------------------------
Saved under the week's Monday (Open Jobs files each upload under the
current week's Monday). "Due" on the week's first working day (normally Monday), "overdue"
after that. A week with no working days at all (Christmas) is skipped.
A reminder the hub can't check by itself, because nothing is uploaded for
it, can have a "Mark as done" button instead ('ack': True); mark_done()
records the click in reminder_acks. The Monday open-jobs check was the
first, until it got its own upload page (Open Jobs, 20 Oct 2026) — none
uses the button now, but it's kept for the next one.

MONTHLY — last month should be saved by the Nth working day
-----------------------------------------------------------
"Due" from the month's first working day until MONTHLY_DEADLINE_WORKING_DAY,
"overdue" after that.

PO APPROVALS — quotes waiting for approval whose chase date has come
--------------------------------------------------------------------
Not an upload: "due" when a quote's chase date is today, "overdue" once it
has passed. Shows the NUMBER of quotes on the menu item. See check_quotes().

ADDING A REMINDER
-----------------
Add an entry to the right list below: which sidebar tab it belongs to
(the id after 'nav-header-' in the page's sidebar), the menu link it
decorates (None for a "Mark as done" reminder, which gets its own line),
and the db function that reads what's been saved.
"""
import calendar
import re
import threading
import time
from datetime import date, datetime, timedelta

import db

# Two working weeks. Long enough to catch a missed day after a holiday,
# short enough that a gap from before reminders existed drops off.
LOOKBACK_WORKING_DAYS = 10
LOOKBACK_WEEKS = 2

# Nothing before this date is ever reminded about. Set by Andreas on the
# day reminders went live (14 Oct 2026): the gaps found in the two weeks
# before then were days whose reports he no longer has, so there's no way
# to clear them by uploading — a reminder that can never be cleared just
# teaches everyone to ignore the badges. Weekly reports start from the
# first week whose Monday is on or after this date; monthly from the first
# month whose report falls due on or after it.
REMINDERS_START = date(2026, 10, 14)

# Days nobody works, so nobody uploads and nothing is due: the Christmas
# shutdown, and any bank holiday the site doesn't work. Every rule skips
# these exactly like a weekend — a Friday before the shutdown is "due" on
# the first working day after it, not on 25 December.
#
# So far only the three Christmas/New Year bank holidays are in. ADD THE
# FULL SHUTDOWN RANGE HERE once it's confirmed — one date per day.
NON_WORKING_DAYS = {
    date(2026, 12, 25),   # Christmas Day
    date(2026, 12, 28),   # Boxing Day (substitute — 26th is a Saturday)
    date(2027, 1, 1),     # New Year's Day
}

# Monthly Check: last month saved by this working day of the new month.
# Andreas, 14 Oct 2026: the 3rd.
MONTHLY_DEADLINE_WORKING_DAY = 3


def _oee_production_day(row):
    """The production day a saved Daily OEE report covers: the start date of
    the report's own period ('10/14/2026 6:30:00 AM to 2026-10-15' -> 14 Oct).
    SFC writes that start date month first. Falls back to the saved date if
    the period is missing or isn't in that shape."""
    m = re.match(r'\s*(\d{1,2})/(\d{1,2})/(\d{4})', row.get('period') or '')
    if m:
        try:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
        except ValueError:
            pass
    return str(row.get('date'))[:10]


def open_jobs_weeks(start_date, end_date):
    """The Open Jobs reminder's saved_weeks: the Mondays from start_date to
    end_date that count as checked.

    A saved check covers its own week AND any earlier week that was skipped.
    It's Agility as it is now, so checking on a Tuesday also answers "what's
    open?" for a Monday that was missed — the same rule the old Mark as done
    button had (see mark_done()). The skipped week still has no point on the
    trend; only the reminder treats it as done. Clicks of that old button,
    in reminder_acks, still count too."""
    done = {r['period'] for r in db.get_reminder_acks('maintenance-open-jobs', start_date, end_date)}
    latest = max((w['week_start'] for w in db.get_open_jobs_weeks(start_date=start_date)), default=None)
    if latest:
        monday = date.fromisoformat(start_date)
        while monday.isoformat() <= min(end_date, latest):
            done.add(monday.isoformat())
            monday += timedelta(weeks=1)
    return sorted(done)


def oee_production_days(start_date, end_date):
    """The Daily OEE reminder's saved_days: one {'date': production day} per
    saved report. Asks the database for a few days past end_date, because a
    report is saved under its period's END date — Wednesday's production can
    be saved as Thursday, and Friday's as Saturday or Monday."""
    lo = date.fromisoformat(start_date) - timedelta(days=1)
    hi = date.fromisoformat(end_date) + timedelta(days=4)
    rows = db.get_oee_daily_snapshots(start_date=lo.isoformat(), end_date=hi.isoformat())
    return [{'date': _oee_production_day(r)} for r in rows]


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
    {
        # Matt's Daily OEE (SFC's Daily UK OEE By Machine Tabular), uploaded
        # the morning after, like the others. Checked by the production day
        # the report covers, not the date it was saved under.
        'id': 'production-daily-oee',
        'dept': 'production',
        'label': 'Daily OEE',
        'href': '/daily-oee',
        'saved_days': oee_production_days,
    },
    {
        # Matt's own copy of SFC Daily — a separate table, so Production's
        # saves and Maintenance's never count for each other.
        'id': 'production-sfc-daily',
        'dept': 'production',
        'label': 'SFC Daily',
        'href': '/production-sfc-daily',
        'saved_days': db.get_production_sfc_daily_snapshots,
    },
]

WEEKLY_REMINDERS = [
    {
        # Monday look at open and overdue jobs in Agility (Selective Work
        # Orders, all statuses, due 01/01/2025 -> 31/12 this year), saved on
        # the Open Jobs page. Same id as when it was a "Mark as done" button,
        # so weeks marked done that way still count — see open_jobs_weeks().
        'id': 'maintenance-open-jobs',
        'dept': 'maintenance',
        'label': 'Open Jobs',
        'href': '/maintenance-open-jobs',
        'saved_weeks': lambda start_date, end_date: open_jobs_weeks(start_date, end_date),
    },
]

MONTHLY_REMINDERS = [
    {
        'id': 'board-monthly-check',
        'dept': 'board-review',
        'label': 'Monthly Check',
        'href': '/',
        'saved_months': db.get_saved_month_labels,
    },
]


# --- dates ------------------------------------------------------------------

def london_today():
    """Today's date at Clamason, not on the server. Render runs on UTC,
    which would be a day out between midnight and 1am in summer time."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo('Europe/London')).date()
    except Exception:
        return datetime.utcnow().date()


def is_working_day(d):
    return d.weekday() < 5 and d not in NON_WORKING_DAYS


def working_days_before(today, n):
    """The n working days before `today` (today excluded), oldest first."""
    days, d = [], today
    while len(days) < n:
        d -= timedelta(days=1)
        if is_working_day(d):
            days.append(d)
    return days[::-1]


def nth_working_day(year, month, n):
    """The nth working day of a month (1 = the first)."""
    d, count = date(year, month, 1), 0
    while True:
        if is_working_day(d):
            count += 1
            if count == n:
                return d
        d += timedelta(days=1)


def first_working_day_of_week(monday):
    """The first working day Mon-Fri of the week starting `monday`, or None
    if the whole week is non-working (e.g. inside the Christmas shutdown)."""
    for k in range(5):
        d = monday + timedelta(days=k)
        if is_working_day(d):
            return d
    return None


def _short(d):
    return f'{d:%a} {d.day} {d:%b}'   # 'Mon 12 Oct' (no leading zero on any OS)


def week_label(monday):
    return f'w/c {monday.day} {monday:%b}'      # 'w/c 12 Oct'


def _result(reminder, status, missing, detail, **extra):
    out = {
        'id': reminder['id'],
        'dept': reminder['dept'],
        'label': reminder['label'],
        'href': reminder['href'],
        'status': status,
        'missing': missing,
        'detail': detail,
    }
    out.update(extra)
    return out


# --- daily --------------------------------------------------------------------

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
    """Status of one daily reminder: 'ok', 'due' or 'overdue'.

    A day counts as done when its report is saved OR when someone pressed
    "Ignore" for it (no report exists for that day — see skip_day()).
    Ignored days are kept in reminder_acks, the same table as the weekly
    "Mark as done" clicks, under this reminder's own id — so ignoring a day
    on SFC Daily never clears that day on Daily Check, or Matt's copy."""
    window = [d for d in working_days_before(today, LOOKBACK_WORKING_DAYS) if d >= REMINDERS_START]
    if window:
        start, end = window[0].isoformat(), window[-1].isoformat()
        rows = reminder['saved_days'](start_date=start, end_date=end)
        saved = {str(r.get('date'))[:10] for r in rows}
        saved |= {r['period'] for r in db.get_reminder_acks(reminder['id'], start, end)}
    else:
        saved = set()   # nothing to check yet
    missing = [d for d in window if d.isoformat() not in saved]

    if not missing:
        status = 'ok'
    elif missing == [window[-1]]:
        status = 'due'                # only the latest working day — upload it today
    else:
        status = 'overdue'
    return _result(reminder, status, [d.isoformat() for d in missing], describe(missing, status),
                   skip=True)


def skip_day(reminder_id, day, today=None):
    """The "Ignore" button on a daily reminder: there is no report for this
    day (e.g. SFC didn't produce one), so it can never be cleared by
    uploading — and a reminder that can never clear teaches everyone to
    ignore the badges. Records the day in reminder_acks for this reminder.

    Deliberately narrow, so a click can only ever hide a day nobody can fill:
      * daily reminders only, by their real id;
      * one day per click, never "everything missing";
      * only a day this reminder is showing as missing RIGHT NOW — not any
        date someone types into a request.
    Saving the report for that day later still works as normal."""
    today = today or london_today()
    reminder = next((r for r in DAILY_REMINDERS if r['id'] == reminder_id), None)
    if reminder is None:
        raise ValueError(f'"{reminder_id}" is not a daily reminder')
    try:
        day = date.fromisoformat(day).isoformat()
    except ValueError:
        raise ValueError(f'"{day}" is not a date (YYYY-MM-DD)')
    if day not in check(reminder, today)['missing']:
        raise ValueError(f'{day} is not a missing day for {reminder["label"]}')
    db.save_reminder_ack(reminder_id, day)


# --- weekly -------------------------------------------------------------------

def weeks_to_check(today):
    """(monday, first working day) for each week that's already due, oldest
    first. A week isn't due before its first working day, and a week with no
    working days at all isn't checked."""
    this_monday = today - timedelta(days=today.weekday())
    out = []
    for k in range(LOOKBACK_WEEKS - 1, -1, -1):
        monday = this_monday - timedelta(weeks=k)
        first = first_working_day_of_week(monday)
        if monday >= REMINDERS_START and first and first <= today:
            out.append((monday, first))
    return out


def check_weekly(reminder, today):
    """Status of one weekly reminder: 'ok', 'due' or 'overdue'."""
    weeks = weeks_to_check(today)
    if weeks:
        saved = {str(w)[:10] for w in reminder['saved_weeks'](
            start_date=weeks[0][0].isoformat(), end_date=weeks[-1][0].isoformat())}
    else:
        saved = set()
    missing = [(m, first) for m, first in weeks if m.isoformat() not in saved]
    verb = 'done' if reminder.get('ack') else 'saved'

    if not missing:
        status, detail = 'ok', ''
    elif len(missing) == 1 and missing[0][1] == today:
        status, detail = 'due', f'{week_label(missing[0][0])} not {verb} yet'
    else:
        status = 'overdue'
        detail = ' & '.join(week_label(m) for m, _ in missing) + f' not {verb}'
    return _result(reminder, status, [m.isoformat() for m, _ in missing], detail,
                   ack=bool(reminder.get('ack')))


def mark_done(reminder_id, today=None):
    """The "Mark as done" button: record every week this reminder is
    currently missing as done. All of them, not just this week's — a check
    of how things are NOW covers a week that was skipped too (the rule the
    open-jobs check had, and still has on its page — see open_jobs_weeks()).

    Only reminders with 'ack': True can be marked done; anything else
    (a report reminder, or a made-up id) is refused — those clear
    themselves when the report is saved. The one exception is a daily
    report that doesn't exist at all: that day can be ignored on its own,
    one day at a time, with skip_day() below — never in bulk from here."""
    today = today or london_today()
    reminder = next((r for r in WEEKLY_REMINDERS if r['id'] == reminder_id and r.get('ack')), None)
    if reminder is None:
        raise ValueError(f'"{reminder_id}" is not a reminder that can be marked as done')
    for period in check_weekly(reminder, today)['missing']:
        db.save_reminder_ack(reminder_id, period)


# --- monthly ------------------------------------------------------------------

_MONTH_NUMBERS = {name.lower(): i for i, name in enumerate(calendar.month_abbr) if name}


def label_to_month(label):
    """'September 2026' / 'Sept 2026' / 'sep 2026' -> (2026, 9); anything
    without a recognisable month name and 4-digit year -> None."""
    m = re.search(r'([A-Za-z]{3,9})\.?\s*(\d{4})', label or '')
    if not m:
        return None
    month = _MONTH_NUMBERS.get(m.group(1)[:3].lower())
    return (int(m.group(2)), month) if month else None


def check_monthly(reminder, today):
    """Status of one monthly reminder — is LAST month saved?"""
    year, month = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    month_name = f'{calendar.month_name[month]} {year}'
    opens = nth_working_day(today.year, today.month, 1)
    deadline = nth_working_day(today.year, today.month, MONTHLY_DEADLINE_WORKING_DAY)

    # Not due yet (before the month's first working day), or due before
    # reminders existed — nothing to show.
    if today < opens or date(today.year, today.month, 1) < REMINDERS_START:
        return _result(reminder, 'ok', [], '')

    saved = {label_to_month(lbl) for lbl in reminder['saved_months']()}
    if (year, month) in saved:
        return _result(reminder, 'ok', [], '')
    if today <= deadline:
        return _result(reminder, 'due', [month_name], f'{month_name} due by {_short(deadline)}')
    return _result(reminder, 'overdue', [month_name], f'{month_name} not saved (was due {_short(deadline)})')


# --- PO approvals to chase -------------------------------------------------------
#
# Not an upload: a quote sent for approval whose chase date has come. "Due"
# (amber) when a chase date is today, "overdue" (red) once one has passed.
# Cleared by chasing it ("Chased today" moves the chase date on) or by the
# quote moving on (approved, PO sent...). 'count' tells reminders.js to
# show the NUMBER of quotes on the menu item instead of a plain dot.

QUOTE_REMINDERS = [
    {
        'id': 'maintenance-po-approvals',
        'dept': 'maintenance',
        'label': 'PO Approvals',
        'href': '/maintenance-po-approvals',
    },
]


def check_quotes(reminder, today):
    """Status of the PO Approvals reminder: 'ok', 'due' or 'overdue'."""
    from quotes import WAITING_STATUSES   # here, not at the top: quotes.py imports this file
    to_chase = db.get_quotes_to_chase(today, WAITING_STATUSES)
    if not to_chase:
        return _result(reminder, 'ok', [], '', count=0)
    overdue = [q for q in to_chase if q['chase_date'] < today]
    n, n_late = len(to_chase), len(overdue)
    parts = []
    if n_late:
        parts.append(f'{n_late} overdue')
    if n - n_late:
        parts.append(f'{n - n_late} due today')
    detail = f"{n} quote{'s' if n != 1 else ''} to chase: {', '.join(parts)}"
    return _result(reminder, 'overdue' if overdue else 'due',
                   [q['chase_date'].isoformat() for q in to_chase], detail, count=n)


# --- all together ---------------------------------------------------------------

def all_reminders(today=None):
    """Every reminder's current state. One that can't be checked (e.g. the
    database is unreachable) is left out rather than shown as overdue —
    a false alarm would teach people to ignore the badges."""
    today = today or london_today()
    out = []
    for checker, reminders in ((check, DAILY_REMINDERS), (check_weekly, WEEKLY_REMINDERS),
                               (check_monthly, MONTHLY_REMINDERS), (check_quotes, QUOTE_REMINDERS)):
        for r in reminders:
            try:
                out.append(checker(r, today))
            except Exception:
                continue
    return {'today': today.isoformat(), 'reminders': out}


# --- Remembering the answer for a minute -----------------------------------
# Every page in the hub asks /api/reminders for its sidebar badges, and a
# full check reads ~8 tables — on the live site that's ~8 trips to the
# database on EVERY click, most of the wait when opening a tab. The answer
# only changes when something is saved, so it's kept for CACHE_SECONDS and
# thrown away early by clear_cache(), which routes.py calls after every
# POST (every save in the hub is a POST) — a badge clears straight after
# an upload, not a minute later. Also dropped when the London date
# changes, since "today" decides what's due.
CACHE_SECONDS = 60
_cache = {'at': 0.0, 'result': None}
_cache_lock = threading.Lock()   # the server can answer several requests at once


def cached_reminders():
    """all_reminders(), reusing the last answer if it's under
    CACHE_SECONDS old and from the same London day."""
    today = london_today().isoformat()
    with _cache_lock:
        hit = _cache['result']
        if hit is not None and hit['today'] == today and time.monotonic() - _cache['at'] < CACHE_SECONDS:
            return hit
    result = all_reminders()
    with _cache_lock:
        _cache['at'] = time.monotonic()
        _cache['result'] = result
    return result


def clear_cache():
    """Forget the remembered answer — the next request does a full check."""
    with _cache_lock:
        _cache['result'] = None
