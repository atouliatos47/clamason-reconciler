"""
Parser for the Agility "Mean Time Between Failure" export.

One row per asset, with Agility's own reliability figures already
calculated: total downtime, job count, average wait, MTTR and MTBF.

Returns raw rows only — no craft, asset-type or job-type filtering.
Same contract as downtime_parser: the parser reads the file format,
and reconciliation.py decides which rows count. That matters here more
than usual, because this export has no craft column at all and happily
mixes presses, plant and TOOLS in one list (P0847-DP-037, HM43SW0416
and HRB54065 are all tool assets). Averaging the lot together produces
a "maintenance MTTR" dominated by toolroom work — on June 2026 that
inflates it roughly thirteen-fold.

UNITS
-----
Agility reports MTTR and wait time in MINUTES and MTBF in DAYS.
Everything else in this codebase works in hours, so mttr_hrs and
wait_hrs are converted on the way out and the original minute values
kept alongside for audit. MTBF stays in days because that's the unit
that makes sense for it and converting would just invite confusion.

'Insufficient data'
-------------------
Agility writes that string into the MTBF column for any asset with
fewer than two work orders — with one job there's no interval between
failures to measure. It comes back as None rather than 0, because 0
would read as "fails constantly" when it actually means "we don't
know". On June 2026 that's 24 of 40 assets, so a fleet MTBF built by
ignoring it would rest on a very thin slice.
"""
import re

import pandas as pd


# Fixed column positions. Agility emits merged/blank spacer columns
# between the real ones, so these aren't contiguous.
COL_ASSET = 1
COL_DESC = 4
COL_DOWNTIME_HRS = 7
COL_AVG_DOWNTIME_MINS = 9
COL_JOBS = 11
COL_AVG_WAIT_MINS = 12
COL_MTTR_MINS = 15
COL_ELAPSED_DAYS = 16
COL_MTBF_DAYS = 18

HEADER_LABEL = 'Asset'
INSUFFICIENT = 'insufficient'


def _num(value):
    """Float, or None if the cell isn't a number.

    Covers blanks, NaN, and Agility's 'Insufficient data' string in one
    place so no caller has to special-case it.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        return float(str(value).strip().replace(',', ''))
    except (ValueError, TypeError):
        return None


def _find_header_row(df):
    """Row index where the asset table header sits, or None.

    Located by content rather than hardcoded to row 14: the metadata
    block above it varies in height depending on how many report filters
    were set when the export was run.
    """
    for i in range(len(df)):
        if len(df.columns) > COL_ASSET and str(df.iat[i, COL_ASSET]).strip() == HEADER_LABEL:
            return i
    return None


def _find_breakdown_range(df):
    """The 'Breakdown Range:' value, e.g. '01/06/2026 - 30/06/2026'.

    Stored as-is for audit rather than parsed into dates — it's the
    period the whole report covers, and the caller already knows which
    month it asked for.
    """
    for i in range(min(len(df), 20)):
        for j in range(df.shape[1]):
            v = df.iat[i, j]
            if isinstance(v, str) and v.strip().startswith('Breakdown Range'):
                for k in range(j + 1, df.shape[1]):
                    cell = df.iat[i, k]
                    if pd.notna(cell) and str(cell).strip():
                        return str(cell).strip()
    return ''


def parse_mtbf_file(filepath):
    """Returns (records, breakdown_range).

    Each record carries Agility's figures for one asset plus a
    has_mtbf flag, so a caller can count how much of the fleet the
    MTBF figure actually rests on without re-testing for None.
    """
    df = pd.read_excel(filepath, header=None)

    header_row = _find_header_row(df)
    if header_row is None:
        return [], ''

    breakdown_range = _find_breakdown_range(df)

    records = []
    for i in range(header_row + 1, len(df)):
        asset = df.iat[i, COL_ASSET] if df.shape[1] > COL_ASSET else None
        if asset is None or pd.isna(asset):
            continue
        asset = str(asset).strip()
        if not asset:
            continue
        # Trailing 'Total:' / 'End of report' rows, and any repeat of the
        # header if Agility paginated the export.
        if asset.lower().startswith(('total', 'end of', 'printed', 'asset')):
            continue

        def cell(col):
            return df.iat[i, col] if df.shape[1] > col else None

        mtbf_days = _num(cell(COL_MTBF_DAYS))
        raw_mtbf = cell(COL_MTBF_DAYS)
        insufficient = (
            isinstance(raw_mtbf, str) and INSUFFICIENT in raw_mtbf.strip().lower()
        )

        mttr_mins = _num(cell(COL_MTTR_MINS))
        wait_mins = _num(cell(COL_AVG_WAIT_MINS))

        records.append({
            'asset': asset,
            'description': re.sub(r'\s+', ' ', str(cell(COL_DESC) or '')).strip(),

            'downtime_hrs': _num(cell(COL_DOWNTIME_HRS)),
            'jobs': int(_num(cell(COL_JOBS)) or 0),

            # Converted to hours to match the rest of the codebase.
            'mttr_hrs': round(mttr_mins / 60, 4) if mttr_mins is not None else None,
            'wait_hrs': round(wait_mins / 60, 4) if wait_mins is not None else None,

            # Agility's originals, kept so a figure can be traced back
            # to the export it came from without re-deriving it.
            'mttr_mins': mttr_mins,
            'wait_mins': wait_mins,
            'avg_downtime_mins': _num(cell(COL_AVG_DOWNTIME_MINS)),

            'elapsed_days': _num(cell(COL_ELAPSED_DAYS)),
            'mtbf_days': mtbf_days,
            # True only where Agility gave a real number. An asset with
            # one job has no measurable interval between failures.
            'has_mtbf': mtbf_days is not None and not insufficient,
            'mtbf_insufficient': insufficient,
        })

    return records, breakdown_range


def summarise_mtbf(records):
    """Fleet-level rollup across whatever rows are handed in.

    Deliberately takes a list rather than a filepath so the caller can
    filter first — press-only, all-plant, tools excluded — and get a
    summary of exactly that set.

    MTTR and wait are weighted by job count, not averaged across assets:
    an asset with 14 breakdowns should carry fourteen times the weight
    of one with a single job. A plain mean over assets would let a
    one-off failure on a rarely-used machine swing the fleet figure as
    hard as a chronically failing press.

    MTBF is NOT weighted the same way — it's a mean over only those
    assets Agility could compute it for, with the count reported
    alongside so a thin sample is visible rather than implied.
    """
    jobs_total = sum(r['jobs'] for r in records)

    def weighted(key):
        pairs = [(r[key], r['jobs']) for r in records
                 if r.get(key) is not None and r['jobs']]
        if not pairs:
            return None
        total_jobs = sum(j for _, j in pairs)
        return round(sum(v * j for v, j in pairs) / total_jobs, 2)

    with_mtbf = [r for r in records if r['has_mtbf']]
    mtbf = (round(sum(r['mtbf_days'] for r in with_mtbf) / len(with_mtbf), 2)
            if with_mtbf else None)

    return {
        'asset_count': len(records),
        'jobs': jobs_total,
        'downtime_hrs': round(sum(r['downtime_hrs'] or 0 for r in records), 2),
        'mttr_hrs': weighted('mttr_hrs'),
        'wait_hrs': weighted('wait_hrs'),
        'mtbf_days': mtbf,
        'mtbf_assets': len(with_mtbf),
        # How much of the fleet had too few jobs to measure an interval.
        # Surfaced so the UI can say "4.3 days across 16 of 40 assets"
        # rather than presenting a thin figure as a fleet-wide fact.
        'mtbf_insufficient': sum(1 for r in records if r['mtbf_insufficient']),
    }
