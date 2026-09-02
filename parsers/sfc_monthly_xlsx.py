"""
Parser for the SFC "UK Monthly Downtime Summary" xlsx — one sheet per
machine, each with its own "Downtime Reason / # of Events / Duration"
table. Returns the same dict shape the daily PDF parser returns, so
reconciliation.py doesn't need to know or care which source it came from.
"""
import re
import pandas as pd

from time_utils import hms_to_hours
from config import BLAME_FAULT_CODES, FAULT_CODES, TOOLROOM_CODES, PLANNED_CODES, NON_MACHINE_SHEETS


def _find_report_period(df):
    """The whole 'Report Period ... start ... To' phrase lives in ONE
    cell as text; the end date is a separate Timestamp cell further
    along the same row. Returns (period_str, period_hrs) or ('', None)."""
    for i in range(len(df)):
        for j in range(df.shape[1]):
            v = df.iat[i, j]
            if isinstance(v, str) and v.strip().startswith('Report Period'):
                m = re.search(r'Report Period\s+(.+?)\s+To\s*$', v.strip())
                start = m.group(1).strip() if m else None
                end = None
                for k in range(j + 1, df.shape[1]):
                    cell = df.iat[i, k]
                    if pd.notna(cell):
                        end = cell
                        break
                if start and end is not None:
                    period = f"{start} to {end}"
                    try:
                        period_hrs = round(
                            (pd.to_datetime(end) - pd.to_datetime(start)).total_seconds() / 3600, 2
                        )
                    except Exception:
                        period_hrs = None
                    return period, period_hrs
    return '', None


def _find_header_row(df):
    """Row index where the 'Downtime Reason' table header sits, or None."""
    for i in range(len(df)):
        if str(df.iat[i, 0]).strip() == 'Downtime Reason':
            return i
    return None


def _categorise(reason):
    """Which department owns this downtime reason.

    Same three-way split config.py already defines for the site-wide
    totals, applied per machine here. Anything not in a known code list
    is 'production' — that's the residual bucket (No Labour, Tooling
    Problem, Wait for Rectification, No Material...), and it's a
    residual on purpose: a new reason code appearing in SFC lands there
    rather than silently vanishing from the totals.
    """
    up = reason.strip().upper()
    # FAULT_CODES, not BLAME_FAULT_CODES — see the note in config.py.
    # This is departmental ownership, a different question from what the
    # gap report can match against a work order.
    if up in FAULT_CODES:
        return 'fault'
    if up in TOOLROOM_CODES:
        return 'tool'
    if up in PLANNED_CODES:
        return 'planned'
    return 'production'


def _read_reason_rows(df, header_row, reasons, reason_events):
    """Walk the reason/events/duration rows for one sheet, accumulating
    into the shared site-wide reasons/reason_events dicts AND building a
    per-machine breakdown for that one sheet.

    Returns (sheet_total_hrs, sheet_total_events, machine_row).

    WHY THE PER-MACHINE BREAKDOWN WIDENED
    -------------------------------------
    This used to keep only the BLAME_FAULT_CODES subset per machine, so
    the only per-machine chart possible was the maintenance one. The
    board review needs a Pareto per department — top 5 presses by
    production downtime, by tool downtime, and by fault downtime — and
    the tool/production hours were being computed and thrown away.

    Now every category is kept per machine, plus that machine's own
    reason dict, so any Pareto for any department can be rebuilt from a
    saved run without re-uploading the source workbook. Same reasoning
    as the SFC daily snapshots keeping their reasons JSONB.
    """
    total_hrs, total_events = 0, 0
    cat_hrs = {'fault': 0.0, 'tool': 0.0, 'planned': 0.0, 'production': 0.0}
    cat_events = {'fault': 0, 'tool': 0, 'planned': 0, 'production': 0}
    m_reasons, m_reason_events = {}, {}

    r = header_row + 1
    while r < len(df):
        reason = df.iat[r, 0]
        if pd.isna(reason) or str(reason).strip() == '':
            break
        reason = str(reason).strip()
        events = df.iat[r, 5]
        duration = df.iat[r, -1]
        hrs = round(hms_to_hours(duration), 3)
        events_n = int(events) if pd.notna(events) else 0

        if reason == 'Totals':
            total_hrs, total_events = hrs, events_n
        elif hrs > 0:
            reasons[reason] = round(reasons.get(reason, 0) + hrs, 3)
            reason_events[reason] = reason_events.get(reason, 0) + events_n

            m_reasons[reason] = round(m_reasons.get(reason, 0) + hrs, 3)
            m_reason_events[reason] = m_reason_events.get(reason, 0) + events_n

            cat = _categorise(reason)
            cat_hrs[cat] += hrs
            cat_events[cat] += events_n
        r += 1

    machine_row = {
        # fault_hrs / fault_events keep their original names and meaning.
        # compute_machine_breakdown() and the existing dashboard chart
        # read these, so renaming them would break both for no gain.
        'fault_hrs': round(cat_hrs['fault'], 3),
        'fault_events': cat_events['fault'],
        'tool_hrs': round(cat_hrs['tool'], 3),
        'tool_events': cat_events['tool'],
        'planned_hrs': round(cat_hrs['planned'], 3),
        'planned_events': cat_events['planned'],
        'production_hrs': round(cat_hrs['production'], 3),
        'production_events': cat_events['production'],
        'total_hrs': round(total_hrs, 3),
        'total_events': total_events,
        # Everything that wasn't scheduled off. This is the honest
        # denominator for a downtime Pareto — charting total_hrs would
        # rank machines by how little they were scheduled to run, which
        # says nothing about reliability.
        'unplanned_hrs': round(total_hrs - cat_hrs['planned'], 3),
        'reasons': m_reasons,
        'reason_events': m_reason_events,
    }
    return total_hrs, total_events, machine_row


def parse_monthly_summary_xlsx(filepath):
    reasons, reason_events = {}, {}
    grand_total_hrs, grand_total_events = 0, 0
    period, period_hrs = '', None
    machine_count = 0
    # machine name -> per-category hrs/events + that machine's own reasons dict
    by_machine = {}

    with pd.ExcelFile(filepath) as xls:
        for sheet in xls.sheet_names:
            if sheet.strip().lower() in NON_MACHINE_SHEETS:
                continue  # not a real asset — skip entirely

            df = pd.read_excel(xls, sheet_name=sheet, header=None)

            header_row = _find_header_row(df)
            if header_row is None:
                continue

            if not period:
                period, period_hrs = _find_report_period(df)

            machine_count += 1
            sheet_hrs, sheet_events, machine_row = _read_reason_rows(
                df, header_row, reasons, reason_events
            )
            grand_total_hrs += sheet_hrs
            grand_total_events += sheet_events
            by_machine[sheet] = machine_row

    if period_hrs is None:
        period_hrs = 24

    maint_hrs = round(sum(v for k, v in reasons.items() if k.strip().upper() in BLAME_FAULT_CODES), 2)
    tool_hrs = round(sum(v for k, v in reasons.items() if k.strip().upper() in TOOLROOM_CODES), 2)
    prod_hrs = round(grand_total_hrs - maint_hrs - tool_hrs, 2)

    planned_offline_hrs = round(sum(v for k, v in reasons.items() if k.strip().upper() in PLANNED_CODES), 2)
    max_possible_hrs = round(machine_count * period_hrs, 2)
    scheduled_hrs = round(max_possible_hrs - planned_offline_hrs, 2)

    return {
        'period': period,
        'reasons': reasons,
        'reason_events': reason_events,
        'by_machine': by_machine,
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
