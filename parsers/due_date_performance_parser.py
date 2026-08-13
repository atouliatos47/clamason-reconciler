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
"""
import pandas as pd


def parse_due_date_performance(filepath):
    """Returns a list of dicts: asset, job_type, status, due_date,
    comp_date (both as pandas Timestamps), for every row with valid
    dates on both sides.

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
           ('asset', 'job type', 'status', 'due date')
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
