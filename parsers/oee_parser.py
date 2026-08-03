"""
Parser for the SFC "Weekly UK OEE By Machine Tabular" .xls export.

One block per machine: a 'Machine: <name>' row, a date range, a header,
one row per interval, then a 'Sub Totals' row. Only the Sub Totals row
is read — it's the machine's figure for the whole week, already summed
by SFC.

These are legacy BIFF .xls files, not xlsx, so pandas/openpyxl can't
read them. xlrd handles them, and warns about a sector-size mismatch on
every file SFC produces; the warning is harmless and the data reads
correctly, so it's suppressed rather than surfaced to the user.

ONE FILE PER WEEK, AND WEEKS DON'T RESPECT MONTHS
------------------------------------------------
Every other input here is one file per period. OEE isn't: it's a weekly
export running Sunday to Sunday, so a month is four or five files and
the boundaries never line up. June 2026 is Wk23 (starts 31 May) through
Wk26 (ends 28 Jun) — the first file reaches back a day into May and the
last stops two days short of month end.

This parser therefore does NOT decide what a month is. It reads one
file, reports the date range it found, and leaves the choice of which
weeks constitute a period to the caller. Baking month logic in here
would hide an assumption that changes the headline OEE number.

AGGREGATION ORDER IS THE WHOLE POINT
------------------------------------
aggregate_oee() sums the raw hours and part counts across weeks and
machines FIRST, then computes one Availability, Performance and Quality
from those totals. It never averages the per-week percentages.

That isn't pedantry. A machine that ran 3 hours in a week and one that
ran 160 do not get equal votes in a fleet figure, and SFC's own weekly
percentages carry at least two errors that a naive average would
propagate straight onto a board slide (see ANOMALIES below).
"""
import io

import xlrd


# Fixed column positions in the Sub Totals row. SFC emits merged/blank
# spacer columns between the real ones, so these aren't contiguous.
COL_LABEL = 1
COL_DATERANGE = 3
COL_INTERVAL = 5
COL_TOTAL_AVAIL = 6
COL_PLANNED_DOWN = 9
COL_NET_AVAIL = 14
COL_UNPLANNED_DOWN = 15
COL_RUN_TIME = 17
COL_AVAIL_PCT = 21
COL_TOTAL_PARTS = 23
COL_IDEAL_PARTS = 25
COL_PERF_PCT = 29
COL_SCRAP_PARTS = 31
COL_QUALITY_PCT = 33
COL_OEE_PCT = 35

_TIME_FIELDS = ('total_avail_hrs', 'planned_down_hrs', 'net_avail_hrs',
                'unplanned_down_hrs', 'run_time_hrs')


def _hours(value):
    """'168:00:00' -> 168.0. Returns 0.0 for anything unparseable.

    Hours can exceed 24 (a week is 168), so this can't use a time type —
    it's a duration written in H:M:S, not a clock reading.
    """
    s = str(value).strip()
    if ':' not in s:
        return 0.0
    parts = s.split(':')
    try:
        return round(int(parts[0]) + int(parts[1]) / 60 + int(parts[2]) / 3600, 4)
    except (ValueError, IndexError):
        return 0.0


def _num(value):
    try:
        return float(str(value).strip())
    except (ValueError, TypeError):
        return None


def parse_oee_file(filepath):
    """Returns (records, date_range).

    One record per machine — its Sub Totals row for that week. SFC's own
    percentage columns are kept alongside the raw counts, but they are
    for display and audit only: every aggregate figure is recomputed
    from the raw hours and parts.
    """
    # xlrd prints 'file size not 512 + multiple of sector size' to stdout —
    # not through warnings, so it can't be filtered. SFC produces it on
    # every file and the data reads correctly, so it goes to a throwaway
    # buffer rather than the app's logs.
    book = xlrd.open_workbook(filepath, logfile=io.StringIO())
    sheet = book.sheet_by_index(0)

    records = []
    machine = None
    date_range = ''

    for r in range(sheet.nrows):
        label = str(sheet.cell_value(r, COL_LABEL)).strip()

        if label.startswith('Machine:'):
            machine = label.replace('Machine:', '').strip()
            continue

        if not date_range:
            cell = str(sheet.cell_value(r, COL_DATERANGE)).strip()
            if cell.startswith('Date Range:'):
                start = cell.replace('Date Range:', '').replace('To', '').strip()
                end = ''
                # The end date is a separate serial-number cell further
                # along the same row, not part of the label text.
                for k in range(COL_DATERANGE + 1, sheet.ncols):
                    v = sheet.cell_value(r, k)
                    if v not in ('', None):
                        try:
                            end = str(xlrd.xldate_as_datetime(float(v), book.datemode).date())
                        except (ValueError, TypeError):
                            end = str(v).strip()
                        break
                date_range = f'{start} to {end}'.strip()

        # 'Grand Totals' is skipped deliberately: it's SFC's own
        # fleet roll-up, and aggregate_oee() recomputes that from the
        # per-machine rows so the same rule applies to every period.
        if label != 'Sub Totals' or machine is None:
            continue

        def cell(col):
            return sheet.cell_value(r, col)

        rec = {
            'machine': machine,
            'total_avail_hrs': _hours(cell(COL_TOTAL_AVAIL)),
            'planned_down_hrs': _hours(cell(COL_PLANNED_DOWN)),
            'net_avail_hrs': _hours(cell(COL_NET_AVAIL)),
            'unplanned_down_hrs': _hours(cell(COL_UNPLANNED_DOWN)),
            'run_time_hrs': _hours(cell(COL_RUN_TIME)),
            'total_parts': _num(cell(COL_TOTAL_PARTS)) or 0,
            'ideal_parts': _num(cell(COL_IDEAL_PARTS)) or 0,
            'scrap_parts': _num(cell(COL_SCRAP_PARTS)) or 0,
            # SFC's own percentages — display and audit only.
            'sfc_avail_pct': _num(cell(COL_AVAIL_PCT)),
            'sfc_perf_pct': _num(cell(COL_PERF_PCT)),
            'sfc_quality_pct': _num(cell(COL_QUALITY_PCT)),
            'sfc_oee_pct': _num(cell(COL_OEE_PCT)),
        }
        records.append(rec)
        machine = None

    return records, date_range


def _compute(totals):
    """Availability, Performance, Quality and OEE from summed raw values.

    ANOMALIES — why each pillar is guarded
    --------------------------------------
    Performance is capped at 100% for the OEE product. Parts can exceed
    'ideal' when a machine's cycle time or parts-per-stroke is set wrong
    in SFC (June 2026: Kaiser 50T 1 at 128%, Heenan 2 at 112%). Letting
    that through would inflate OEE and, worse, disguise a configuration
    error as good performance. perf_pct_raw keeps the uncapped figure so
    the problem stays visible.

    Quality returns None when scrap exceeds parts produced, rather than
    a negative number. June 2026 Wk25 has Bihler at 209 scrap against 2
    parts, which SFC itself reports as -10350%. That is a broken row,
    not a real quality rate, and averaging it into anything produces
    nonsense. Excluding it and saying so is honest; including it is not.
    """
    net = totals['net_avail_hrs']
    run = totals['run_time_hrs']
    parts = totals['total_parts']
    ideal = totals['ideal_parts']
    scrap = totals['scrap_parts']

    avail = round(run / net * 100, 2) if net else None
    perf_raw = round(parts / ideal * 100, 2) if ideal else None
    perf = min(perf_raw, 100.0) if perf_raw is not None else None

    quality = None
    scrap_exceeds_parts = parts > 0 and scrap > parts
    if parts > 0 and not scrap_exceeds_parts:
        quality = round((parts - scrap) / parts * 100, 4)

    oee = None
    if avail is not None and perf is not None:
        # Quality of exactly 100 is the correct treatment for a period
        # with no scrap recorded — but note SFC's scrap field is
        # under-populated (June: 1,777 parts against EFACS's 6,955), so
        # a 100% here means "nothing was logged", not "nothing was bad".
        q = quality if quality is not None else 100.0
        oee = round(avail / 100 * perf / 100 * q / 100 * 100, 2)

    return {
        'availability_pct': avail,
        'performance_pct': perf,
        'performance_pct_raw': perf_raw,
        'quality_pct': quality,
        'oee_pct': oee,
        'quality_unavailable': scrap_exceeds_parts,
    }


def aggregate_oee(weekly_records):
    """Roll up any number of weeks into fleet and per-machine figures.

    weekly_records: a list of the `records` lists returned by
    parse_oee_file — one entry per week. Which weeks go in is the
    caller's decision; see the module docstring on month boundaries.

    Returns fleet totals plus a per_machine list sorted by OEE ascending,
    so the worst performer is first — that's the one the board asks about.
    """
    by_machine = {}
    for week in weekly_records:
        for rec in week:
            acc = by_machine.setdefault(rec['machine'], {
                'machine': rec['machine'], 'weeks': 0,
                **{f: 0.0 for f in _TIME_FIELDS},
                'total_parts': 0.0, 'ideal_parts': 0.0, 'scrap_parts': 0.0,
                'anomaly_weeks': 0,
            })
            acc['weeks'] += 1
            for f in _TIME_FIELDS:
                acc[f] += rec[f]
            acc['total_parts'] += rec['total_parts']
            acc['ideal_parts'] += rec['ideal_parts']
            acc['scrap_parts'] += rec['scrap_parts']
            # Count the weeks SFC itself reported as impossible, so a
            # machine carrying bad source data can be flagged even when
            # the monthly totals happen to absorb it.
            if rec['total_parts'] > 0 and rec['scrap_parts'] > rec['total_parts']:
                acc['anomaly_weeks'] += 1

    per_machine = []
    for acc in by_machine.values():
        row = dict(acc)
        for f in _TIME_FIELDS:
            row[f] = round(row[f], 2)
        row.update(_compute(acc))
        per_machine.append(row)

    fleet = {'machine_count': len(per_machine),
             'week_count': len(weekly_records),
             **{f: 0.0 for f in _TIME_FIELDS},
             'total_parts': 0.0, 'ideal_parts': 0.0, 'scrap_parts': 0.0}
    for acc in by_machine.values():
        for f in _TIME_FIELDS:
            fleet[f] += acc[f]
        for f in ('total_parts', 'ideal_parts', 'scrap_parts'):
            fleet[f] += acc[f]
    for f in _TIME_FIELDS:
        fleet[f] = round(fleet[f], 2)
    fleet.update(_compute(fleet))

    fleet['anomaly_machines'] = [
        m['machine'] for m in per_machine if m['anomaly_weeks'] or
        (m['performance_pct_raw'] or 0) > 100
    ]

    per_machine.sort(key=lambda m: (m['oee_pct'] is None, m['oee_pct']))
    return {'fleet': fleet, 'per_machine': per_machine}
