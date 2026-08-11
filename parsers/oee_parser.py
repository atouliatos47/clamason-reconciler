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

from config import SHIFT_HOURS_PER_WEEK


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


def _compute_vs_intended(totals, oee_pct):
    """TEEP against the intended shift pattern (config.SHIFT_HOURS_PER_WEEK)
    instead of the full 24/7 calendar. See config.py for why this table
    exists and can't be derived from any SFC/Agility/EFACS report — it's
    a roster decision, not shop-floor data.

    totals needs 'machine' and 'total_avail_hrs' (used only to recover
    the period length in days — 744h / 24 = 31 days — not as the TEEP
    denominator itself, which is the whole point of this variant).
    oee_pct is passed in already computed by _compute() rather than
    recomputed here, so this can never drift from the OEE figure shown
    right next to it.

    None (not 0) for every field here when the machine has no real entry
    in SHIFT_HOURS_PER_WEEK yet — still at its 0.0 "not configured"
    placeholder. Same reasoning as the None-vs-0 distinction everywhere
    else in this module: a 0% figure reads as "achieving nothing", not
    "nobody's told this the intended answer yet", and those are very
    different things to show someone.
    """
    total_avail = totals.get('total_avail_hrs')
    hours_per_week = SHIFT_HOURS_PER_WEEK.get(totals.get('machine'), 0.0)

    if not total_avail or not hours_per_week:
        return {
            'intended_hours': None,
            'utilization_vs_intended_pct': None,
            'teep_vs_intended_pct': None,
        }

    period_days = total_avail / 24
    intended_hours = round(hours_per_week * period_days / 7, 2)

    net = totals.get('net_avail_hrs', 0.0)
    # Deliberately not capped at 100 — running MORE than the intended
    # pattern (overtime, an extra shift added for a push) is a real and
    # useful thing to see, not an error to hide the way OEE's own
    # Performance factor caps a >100% figure (see _compute() above).
    utilization_vs_intended = round(net / intended_hours * 100, 2)

    teep_vs_intended = None
    if oee_pct is not None:
        teep_vs_intended = round(oee_pct * utilization_vs_intended / 100, 2)

    return {
        'intended_hours': intended_hours,
        'utilization_vs_intended_pct': utilization_vs_intended,
        'teep_vs_intended_pct': teep_vs_intended,
    }


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
    total_avail = totals['total_avail_hrs']

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

    # Utilization is the fourth factor OEE deliberately leaves out: how
    # much of the full calendar period this machine was even scheduled
    # to run, before Availability/Performance/Quality get a say. OEE
    # judges only the scheduled window (net_avail_hrs); TEEP judges every
    # hour that exists (total_avail_hrs) — the two numbers can look very
    # different on a site that isn't scheduled 24/7, and that gap is the
    # whole point of tracking TEEP alongside OEE rather than instead of it.
    utilization = round(net / total_avail * 100, 2) if total_avail else None

    teep = None
    if oee is not None and utilization is not None:
        teep = round(oee * utilization / 100, 2)

    return {
        'availability_pct': avail,
        'performance_pct': perf,
        'performance_pct_raw': perf_raw,
        'quality_pct': quality,
        'oee_pct': oee,
        'quality_unavailable': scrap_exceeds_parts,
        'utilization_pct': utilization,
        'teep_pct': teep,
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
        row.update(_compute_vs_intended(acc, row['oee_pct']))
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

    # Fleet intended_hours is a straight sum of whichever machines have a
    # real (non-placeholder) entry in SHIFT_HOURS_PER_WEEK — summed from
    # per_machine rather than recomputed from fleet totals, since fleet
    # has no per-machine identity to look a shift pattern up against.
    # A machine still at its 0.0 placeholder contributes nothing here,
    # same as it contributing nothing to fleet net_avail_hrs when it
    # never ran — the fleet figure only reflects machines that are
    # actually configured, not silently assumes 0 intended = 0 fleet
    # impact in some other, wrong direction.
    configured = [m for m in per_machine if m['intended_hours'] is not None]
    if configured:
        fleet['intended_hours'] = round(sum(m['intended_hours'] for m in configured), 2)
        fleet_net_configured = sum(m['net_avail_hrs'] for m in configured)
        fleet['utilization_vs_intended_pct'] = round(
            fleet_net_configured / fleet['intended_hours'] * 100, 2)
        fleet['teep_vs_intended_pct'] = (
            round(fleet['oee_pct'] * fleet['utilization_vs_intended_pct'] / 100, 2)
            if fleet['oee_pct'] is not None else None)
        fleet['intended_configured_count'] = len(configured)
    else:
        fleet['intended_hours'] = None
        fleet['utilization_vs_intended_pct'] = None
        fleet['teep_vs_intended_pct'] = None
        fleet['intended_configured_count'] = 0

    fleet['anomaly_machines'] = [
        m['machine'] for m in per_machine if m['anomaly_weeks'] or
        (m['performance_pct_raw'] or 0) > 100
    ]

    per_machine.sort(key=lambda m: (m['oee_pct'] is None, m['oee_pct']))
    return {'fleet': fleet, 'per_machine': per_machine}


def apply_efacs_scrap_correction(oee_result, efacs_scrap_qty):
    """Recompute FLEET quality/OEE using EFACS's scrap count in place of
    SFC's own scrap_parts total.

    SFC's scrap field is badly under-populated — July 2026 it logged 125
    scrap parts fleet-wide against EFACS's real 4,995, a ~40x gap (June
    2026 was a smaller but still real ~4x gap: 1,777 vs 6,955). EFACS is
    the system of record here — it's driven by the works-order booking
    process, not a shop-floor sensor count — so its total is the one
    that should feed the board's Quality figure.

    FLEET ONLY. EFACS's Cost of Scrap export has no machine or press
    column — it's keyed by works order and part — so there is no
    EFACS-sourced equivalent for per-machine quality/OEE. per_machine is
    returned completely untouched; only fleet changes. Anywhere this
    correction shows on a slide or report needs to make that scope
    clear, or a reader could assume every machine's figure moved when
    only the fleet total did.

    Mutates and returns oee_result rather than the more defensive
    deepcopy-and-return — routes.py calls this exactly once, immediately
    after aggregate_oee() produces oee_result, on a dict nothing else
    holds a reference to yet.

    Returns oee_result unchanged (fleet.quality_source left as 'sfc') if
    oee_result is None (no OEE file uploaded — nothing to correct) or if
    EFACS's count somehow exceeds total parts produced (would produce a
    negative quality, the same guard _compute() already applies to SFC's
    own scrap figure).
    """
    if oee_result is None:
        return None

    fleet = oee_result['fleet']
    fleet['quality_source'] = 'sfc'  # default; overwritten below on success
    parts = fleet['total_parts']

    if parts <= 0 or efacs_scrap_qty > parts:
        return oee_result

    quality = round((parts - efacs_scrap_qty) / parts * 100, 4)
    fleet['sfc_scrap_parts'] = fleet['scrap_parts']  # kept for audit trail
    fleet['efacs_scrap_parts'] = efacs_scrap_qty
    fleet['quality_pct'] = quality
    fleet['quality_source'] = 'efacs'
    fleet['quality_unavailable'] = False

    avail = fleet['availability_pct']
    perf = fleet['performance_pct']
    if avail is not None and perf is not None:
        fleet['oee_pct'] = round(avail / 100 * perf / 100 * quality / 100 * 100, 2)

    # teep_pct is derived from oee_pct (teep = oee * utilization / 100),
    # so it goes stale the moment oee_pct changes above unless it's
    # recomputed here too. utilization_pct itself doesn't depend on
    # quality/scrap at all, so it's untouched — only teep_pct needs it.
    if fleet['oee_pct'] is not None and fleet.get('utilization_pct') is not None:
        fleet['teep_pct'] = round(fleet['oee_pct'] * fleet['utilization_pct'] / 100, 2)

    # Same staleness risk, same fix, for the vs-intended variant — it's
    # oee_pct * utilization_vs_intended_pct, so it goes stale right
    # alongside teep_pct above for the identical reason.
    if fleet['oee_pct'] is not None and fleet.get('utilization_vs_intended_pct') is not None:
        fleet['teep_vs_intended_pct'] = round(
            fleet['oee_pct'] * fleet['utilization_vs_intended_pct'] / 100, 2)

    return oee_result
