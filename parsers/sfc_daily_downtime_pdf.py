"""
Parser for the SFC "Daily Downtime Summary" PDF (Microsoft Reporting
Services export) — one whole-site table, unlike the monthly xlsx's
one-sheet-per-machine layout. Returns the same dict shape as
parse_monthly_summary_xlsx(), so reconciliation.py and the Daily View
don't need to know or care which source produced it.

'by_machine' is always {} here — this report has no per-machine split,
so there's nothing to build a per-machine chart from this source alone.
Don't mistake the empty dict for a bug; it's a real limitation of the
report itself.

Unlike the monthly xlsx (no built-in checksum), this report gives one
for free: its own "Grand Totals" row. _parse_reason_rows() sums the
events it parsed and raises ValueError if that sum disagrees with the
report's own total — catching a missed/mis-parsed row immediately
instead of silently under-counting.
"""
import re
from datetime import datetime

import pdfplumber

from time_utils import hms_to_hours
from config import BLAME_FAULT_CODES, TOOLROOM_CODES, PLANNED_CODES, NON_MACHINE_SHEETS

ROW_RE = re.compile(r'^(.+?)\s+(\d+)\s+(\d{1,3}:\d{2}:\d{2})$')
PERIOD_RE = re.compile(
    r'Report Period\s+(\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}:\d{2}\s+[AP]M)'
    r'\s+To\s+(\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}:\d{2}\s+[AP]M)'
)
SELECTED_MACHINES_RE = re.compile(r'Selected Machines:\s*(.+)', re.DOTALL)


def _extract_pages(filepath):
    with pdfplumber.open(filepath) as pdf:
        return [page.extract_text() or '' for page in pdf.pages]


def _parse_period(page1_text):
    """Same 'start to end' + computed period_hrs shape as the monthly
    parser's _find_report_period, just read from PDF text instead of
    an xlsx cell.

    Also derives the calendar date this report represents, as an ISO
    'YYYY-MM-DD' string: the START of the period. A report spanning
    6:30am-to-6:30am is the production day that STARTED at 6:30am —
    it's generated and closes out the following morning, but it isn't
    that following day's data. Getting this backwards is exactly what
    was defaulting the daily check's date picker to the wrong day."""
    m = PERIOD_RE.search(page1_text)
    if not m:
        return '', None, None
    start_s, end_s = m.groups()
    period = f"{start_s} to {end_s}"
    detected_date = None
    try:
        start = datetime.strptime(start_s, '%m/%d/%Y %I:%M:%S %p')
        end = datetime.strptime(end_s, '%m/%d/%Y %I:%M:%S %p')
        period_hrs = round((end - start).total_seconds() / 3600, 2)
        detected_date = start.date().isoformat()
    except ValueError:
        period_hrs = None
    return period, period_hrs, detected_date


def _parse_reason_rows(page1_text):
    """Walk the Downtime Reason / # of Events / Duration table.
    Returns (reasons, reason_events, grand_total_hrs, grand_total_events).
    Raises ValueError if the report's layout can't be found at all, or
    if the parsed event count disagrees with the report's own Grand
    Totals row — either means a row was missed, not that the data is
    genuinely empty."""
    reasons, reason_events = {}, {}
    grand_total_events, grand_total_hrs = None, None

    in_table = False
    for line in page1_text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith('Downtime Reason'):
            in_table = True
            continue
        if not in_table:
            continue

        m = ROW_RE.match(line)
        if not m:
            continue
        reason, events_s, duration_s = m.groups()
        reason = reason.strip()
        events_n = int(events_s)
        hrs = round(hms_to_hours(duration_s), 3)

        if reason.lower() == 'grand totals':
            grand_total_events, grand_total_hrs = events_n, hrs
            break

        if hrs > 0:
            reasons[reason] = round(reasons.get(reason, 0) + hrs, 3)
            reason_events[reason] = reason_events.get(reason, 0) + events_n

    if grand_total_events is None:
        raise ValueError('Could not find a "Grand Totals" row — the PDF layout may have changed')

    parsed_events = sum(reason_events.values())
    if parsed_events != grand_total_events:
        raise ValueError(
            f'Parsed event count ({parsed_events}) does not match the '
            f"report's own Grand Totals ({grand_total_events}) — a row "
            f'was likely missed. Check the PDF layout hasn\'t changed.'
        )

    return reasons, reason_events, grand_total_hrs, grand_total_events


def _parse_machine_count(all_text):
    """Page 2's 'Selected Machines: A, B, C...' list — this report's
    equivalent of the monthly xlsx's one-sheet-per-machine count. Real
    presses only; NON_MACHINE_SHEETS entries (e.g. 'Spare SFC Box') are
    excluded, same rule the monthly parser uses."""
    m = SELECTED_MACHINES_RE.search(all_text)
    if not m:
        return 0
    names = [n.strip() for n in m.group(1).split(',') if n.strip()]
    return sum(1 for n in names if n.lower() not in NON_MACHINE_SHEETS)


def parse_daily_downtime_pdf(filepath):
    pages = _extract_pages(filepath)
    page1 = pages[0] if pages else ''
    all_text = '\n'.join(pages)

    period, period_hrs, detected_date = _parse_period(page1)
    reasons, reason_events, grand_total_hrs, grand_total_events = _parse_reason_rows(page1)
    machine_count = _parse_machine_count(all_text)

    if period_hrs is None:
        period_hrs = 24

    maint_hrs = round(sum(v for k, v in reasons.items() if k.upper() in BLAME_FAULT_CODES), 2)
    tool_hrs = round(sum(v for k, v in reasons.items() if k.upper() in TOOLROOM_CODES), 2)
    prod_hrs = round(grand_total_hrs - maint_hrs - tool_hrs, 2)

    planned_offline_hrs = round(sum(v for k, v in reasons.items() if k.upper() in PLANNED_CODES), 2)
    max_possible_hrs = round(machine_count * period_hrs, 2)
    scheduled_hrs = round(max_possible_hrs - planned_offline_hrs, 2)

    return {
        'period': period,
        'detected_date': detected_date,
        'reasons': reasons,
        'reason_events': reason_events,
        'by_machine': {},  # no per-machine breakdown in this report — see module docstring
        'total_events': grand_total_events,
        'total_hrs': round(grand_total_hrs, 2),
        'maintenance_hrs': maint_hrs,
        'toolroom_hrs': tool_hrs,
        'production_hrs': prod_hrs,
        'machine_count': machine_count,
        'period_hrs': period_hrs,
        'max_possible_hrs': max_possible_hrs,
        'planned_offline_hrs': planned_offline_hrs,
        'scheduled_hrs': scheduled_hrs,
    }
