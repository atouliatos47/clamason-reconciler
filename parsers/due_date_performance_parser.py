"""
Parser for the Agility "Due Date Performance" .xlsx export (report code
AG3-205 — the same code as the Down Time Analysis export used elsewhere
in this app, just run with different settings; confirmed from the
filename on a real export, not assumed).

Scoped by COMPLETION date, not raised date: the report's own header
reads "From Completion Date: X To Completion Date: Y" — so uploading
July's export means "every job completed in July," which may include
jobs that were originally due months or years earlier. That's not a
parsing quirk, it's the report's whole design.

WHY THIS FILE EXISTS
---------------------
This is the real source behind the board's "Number of Full TPM
schedules completed to plan (%)" figure — previously computed in the
old clamason-oee-dashboard project (routes/upload.js,
parseDueDatePerformance / due-date-stats), never in this reconciler.
The methodology here is a direct port of that already-proven logic,
not a fresh guess:
  - Filtered to Job Type == 'Planned Service & Maintenance' only.
  - A job only counts if it has BOTH a Due Date and a Comp(letion)
    Date — one without the other can't be judged on-time or late.
  - On time means comp_date <= due_date, by calendar date (the old
    app compared full timestamps; this compares dates, since a Comp
    Date time-of-day like 08:26 on the due date shouldn't read as
    "late" against a Due Date stamped at 12:00 the same day).

A verified, real example of why this matters: July 2026's own export
has 21 "Planned Service & Maintenance" jobs — over a quarter of the
month's total — all completed on the same day (14 July), with Due
Dates scattered across 2020. That's a backlog being cleared in one
administrative sweep, not ordinary lateness, and it single-handedly
drags July's on-time% down by roughly 15 points. Worth knowing before
reading a low month as a sudden performance drop.

PER-ENGINEER PERSONNEL FIGURES (summarise_by_employee, added later)
---------------------------------------------------------------------
Same export, same file, a different cut: instead of one company-wide
on-time % this groups by the Employee column to answer "how is Richard
doing, how is George doing." Confirmed from a real August 2026 pair of
exports that Agility's Craft/Labour filter does NOT isolate one person
— running it for Craft = Maintenance returned Richard Hickman's,
George Boyle's AND Jamie Halford's jobs together (plus some rows with
no employee at all, or a bare numeric code instead of a name — payroll
IDs for a leaver or an unassigned job, never resolved to a name by
Agility's export). So this function is deliberately tolerant of extra
people showing up in an export and deliberately drops rows with no
usable employee name, rather than assuming the file contains only the
one person Andreas meant to filter for.
"""
import re
import statistics
from datetime import datetime

import pandas as pd

import config

# Matches the export's own header band, a single merged cell reading e.g.
# "Site: CLUK : Clamason UK\nCraft: Maintenance:Maintenance\nFrom
# Completion Date: 01/08/2026\xa0\xa0\xa0\xa0\xa0To Completion Date:
# 31/08/2026" (confirmed verbatim, non-breaking spaces and all, from a
# real export — not guessed at). DOTALL so '.*?' can cross the \n
# between "Craft: ..." and "From Completion Date: ...".
_PERIOD_HEADER_RE = re.compile(
    r'From Completion Date:\s*(\d{2}/\d{2}/\d{4}).*?'
    r'To Completion Date:\s*(\d{2}/\d{2}/\d{4})',
    re.DOTALL,
)


def detect_report_period(filepath):
    """Reads the export's own "From Completion Date: X To Completion
    Date: Y" header and returns the calendar month it covers — so the
    app reads which month a file is for from the file itself, instead
    of guessing from today's date (which is wrong the moment a month
    is uploaded late, early, or out of order; Andreas hit exactly this
    uploading a July export after today's date had already rolled into
    a month where "last month" meant August).

    Returns {'period': 'YYYY-MM', 'period_label': 'Jul 2026',
    'from_date': date, 'to_date': date, 'spans_months': bool}. period
    is taken from the FROM date — spans_months is True when TO falls in
    a different calendar month, which callers can surface as a caution
    (an unusual custom date range) without it being fatal on its own.

    Raises ValueError if the header can't be found in the first 10 rows
    or doesn't parse as two dates — refusing beats guessing here, since
    a wrong guess files a real month's figures under the wrong period
    with nothing to show it happened.
    """
    df = pd.read_excel(filepath, sheet_name=0, header=None, nrows=10)
    for i in range(len(df)):
        for cell in df.iloc[i]:
            if not isinstance(cell, str):
                continue
            m = _PERIOD_HEADER_RE.search(cell)
            if not m:
                continue
            from_date = datetime.strptime(m.group(1), '%d/%m/%Y').date()
            to_date = datetime.strptime(m.group(2), '%d/%m/%Y').date()
            return {
                'period': f'{from_date.year:04d}-{from_date.month:02d}',
                'period_label': from_date.strftime('%b %Y'),
                'from_date': from_date,
                'to_date': to_date,
                'spans_months': (from_date.year, from_date.month) != (to_date.year, to_date.month),
            }
    raise ValueError(
        "Couldn't find this export's date range (the 'From Completion Date: "
        "... To Completion Date: ...' header) in its first 10 rows — this "
        "doesn't look like a standard Agility Due Date Performance export."
    )


def parse_due_date_performance(filepath):
    """Returns a list of dicts: asset, job_type, status, due_date,
    comp_date, employee, crafts (both as pandas Timestamps for the
    dates), for every row with valid dates on both sides.

    employee and crafts are '' when the export has no such column (an
    older or differently-configured report) rather than raising —
    summarise_due_date_performance doesn't touch them at all, and
    summarise_by_employee treats a blank employee as "not a real
    person" and drops the row (see its own docstring).

    Raises ValueError if the expected header row isn't found — better
    than silently reading the wrong columns as data.
    """
    df = pd.read_excel(filepath, sheet_name=0, header=None)

    header_row = None
    for i in range(min(10, len(df))):
        row = [str(c).strip().lower() if pd.notna(c) else '' for c in df.iloc[i]]
        if 'due date' in row and ('comp date' in row or 'completion date' in row):
            header_row = i
            break
    if header_row is None:
        raise ValueError(
            "Couldn't find 'Due Date' and 'Comp Date' columns — this "
            "doesn't look like an Agility Due Date Performance export."
        )

    header = [str(c).strip().lower() if pd.notna(c) else '' for c in df.iloc[header_row]]
    col = {name: header.index(name) for name in
           ('asset', 'job type', 'status', 'due date', 'employee', 'crafts')
           if name in header}
    comp_col = header.index('comp date') if 'comp date' in header else header.index('completion date')

    records = []
    for i in range(header_row + 1, len(df)):
        row = df.iloc[i]
        due = pd.to_datetime(row[col['due date']], errors='coerce') if 'due date' in col else pd.NaT
        comp = pd.to_datetime(row[comp_col], errors='coerce')
        if pd.isna(due) or pd.isna(comp):
            continue
        records.append({
            'asset': row[col['asset']] if 'asset' in col else None,
            'job_type': str(row[col['job type']]).strip() if 'job type' in col else '',
            'status': str(row[col['status']]).strip() if 'status' in col else '',
            'due_date': due,
            'comp_date': comp,
            'employee': str(row[col['employee']]).strip() if 'employee' in col and pd.notna(row[col['employee']]) else '',
            'crafts': str(row[col['crafts']]).strip() if 'crafts' in col and pd.notna(row[col['crafts']]) else '',
        })
    return records


def summarise_due_date_performance(records):
    """Of the 'Planned Service & Maintenance' jobs completed this
    period, what fraction were completed on or before their due date.

    Compares calendar dates, not full timestamps — a job due at 12:00
    and completed at 08:26 the same day is on time, not "4 hours
    early" vs "late by a few hours" depending on which side of
    midnight a timestamp comparison would land on.
    """
    ppm_jobs = [r for r in records if r['job_type'] == 'Planned Service & Maintenance']
    on_time = sum(1 for r in ppm_jobs if r['comp_date'].date() <= r['due_date'].date())
    total = len(ppm_jobs)

    return {
        'total': total,
        'completed': on_time,
        'pct': round(on_time / total * 100, 1) if total else None,
    }


def _is_real_employee(name):
    """True for something that looks like an actual person's name, not
    a blank cell or a bare payroll number. Agility exports both: some
    rows have no Employee at all, and some have a numeric code (e.g.
    '22', '130') instead of a name — neither can be shown on a
    performance report, so both are dropped rather than guessed at.

    Also filters out config.PERSONNEL_PPM_EXCLUDED_EMPLOYEES — people
    who no longer work at Clamason. Confirmed real case: Jamie Halford
    still turns up in a Maintenance-craft export months after leaving,
    because Agility's Craft/Labour filter returns the whole craft
    group, not one current employee (see this module's docstring)."""
    if not name or name.isdigit():
        return False
    if name.strip().upper() in config.PERSONNEL_PPM_EXCLUDED_EMPLOYEES:
        return False
    return True


def summarise_by_employee(all_records):
    """Per-engineer planned-PPM performance for one period, across
    however many exports were uploaded together (e.g. a Maintenance-
    craft file and an Electrician-craft file) — pass in the combined
    list of parse_due_date_performance() records from all of them.

    Scope, same as the Richard Hickman / George Boyle Claude Docs
    reports this is built to match:
      - Job Type in config.PERSONNEL_PPM_JOB_TYPES (Planned Service &
        Maintenance + Tool Preventative Maintenance) — a narrower set
        than either of the other two job-type buckets in config.py;
        see its comment for why this one is kept separate.
      - due_date on or after config.PERSONNEL_PPM_BACKLOG_CUTOFF.
        Earlier due dates are counted separately as excluded_backlog
        rather than silently dropped, so a month that clears old
        backlog doesn't read as a mysteriously smaller job count.
      - Rows with no usable employee name are dropped entirely
        (_is_real_employee) — they can't be attributed to anyone's
        performance review.

    Delay is measured in whole calendar days (comp_date.date() -
    due_date.date()), the same date-not-timestamp comparison
    summarise_due_date_performance uses above, and only over the LATE
    jobs — a job finished early has no "lateness" to average in.

    Returns a list of dicts, one per employee, unsorted (the caller
    decides display order):
        employee, craft, jobs_completed, on_time_count, on_time_pct,
        avg_delay_days, median_delay_days, longest_delay_days,
        excluded_backlog_count, job_types
    """
    cutoff = pd.Timestamp(config.PERSONNEL_PPM_BACKLOG_CUTOFF)

    by_employee = {}
    for r in all_records:
        if r['job_type'] not in config.PERSONNEL_PPM_JOB_TYPES:
            continue
        if not _is_real_employee(r['employee']):
            continue
        by_employee.setdefault(r['employee'], []).append(r)

    results = []
    for employee, rows in by_employee.items():
        in_scope = [r for r in rows if r['due_date'] >= cutoff]
        excluded_backlog = [r for r in rows if r['due_date'] < cutoff]

        late_delays = []
        on_time_count = 0
        for r in in_scope:
            delay_days = (r['comp_date'].date() - r['due_date'].date()).days
            if delay_days <= 0:
                on_time_count += 1
            else:
                late_delays.append(delay_days)

        total = len(in_scope)
        crafts = sorted({r['crafts'] for r in rows if r['crafts']})
        job_types = sorted({r['job_type'] for r in in_scope})

        results.append({
            'employee': employee,
            'craft': ', '.join(crafts) if crafts else None,
            'jobs_completed': total,
            'on_time_count': on_time_count,
            'on_time_pct': round(on_time_count / total * 100, 1) if total else None,
            'avg_delay_days': round(statistics.mean(late_delays), 1) if late_delays else None,
            'median_delay_days': round(statistics.median(late_delays), 1) if late_delays else None,
            'longest_delay_days': max(late_delays) if late_delays else None,
            'excluded_backlog_count': len(excluded_backlog),
            'job_types': job_types,
        })

    return results
