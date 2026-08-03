"""
Parser for the Agility "Down Time Analysis" (AG3-205) export.

Each row is a WO with a 6-digit job number, an asset code, five
timestamps, and a Down Time column like '02h25m' or '01d03h16m'.
Returns raw rows only — no craft/machine/asset filtering here. That
filtering belongs in reconciliation.py and daily.py, not in the parser,
so the parser stays a pure "read this file format" function.

WHY THE TIMESTAMPS ARE READ AND NOT JUST THE 'Down Time' COLUMN
--------------------------------------------------------------
Agility's own Down Time column can't be trusted:

  * It goes NEGATIVE. June 2026 has 2 rows of 81 like '-17h-01m'
    (WO 034137) where the real Breakdown->OnLine elapsed is 3h47m.
    dhm_to_hours() returns 0 for those rather than a wrong-signed
    number, so those WOs silently contribute nothing to any total.
  * It loses an hour across midnight. A 12:00 -> 08:28-next-day job
    reads 19h28m when the true elapsed is 20h28m.

The five timestamp columns (Breakdown / Reported / Started / Finished /
OnLine) are the underlying source and are correct, so every duration
this parser reports is derived from them by subtraction.

THREE DIFFERENT DURATIONS, NOT ONE
----------------------------------
'Downtime' is not one number, and collapsing them hides where the loss
actually is. On June 2026 plant WOs the wait was 8h45m against 1h24m of
repair — i.e. six times longer waiting for work to start than doing it,
which is invisible if you only ever look at a single figure:

  mtta_hrs  Reported -> Started   response: how long before someone starts
  mttr_hrs  Started  -> Finished  repair: actual wrench time. THIS is MTTR.
  mdt_hrs   Breakdown -> OnLine   total downtime the machine was unavailable

The old parser exposed only the Down Time column and daily.py used it as
MTTR — but that column is Breakdown->OnLine, so what was labelled MTTR
was really MDT, roughly 30x larger on June's plant data.

downtime_hrs / downtime_raw are still returned unchanged so
reconciliation.py and the monthly board report keep working exactly as
before. This module is additive; nothing downstream had to change.
"""
import re
from datetime import datetime

import pandas as pd

from time_utils import dhm_to_hours


# Column positions in the AG3-205 export. Fixed layout — Agility emits
# merged/blank spacer columns between the real ones, which is why these
# aren't contiguous.
COL_WO = 0
COL_ASSET = 1
COL_BREAKDOWN = 3
COL_REPORTED = 4
COL_STARTED = 5
COL_FINISHED = 6
COL_ONLINE = 7
COL_DOWNTIME = 9


def _as_datetime(value):
    """Return a real datetime, or None if the cell isn't one.

    pandas hands these back as Timestamp (a datetime subclass) when the
    column parses cleanly, but a blank or malformed cell comes through
    as NaT or a string. Anything that isn't genuinely a datetime returns
    None so the caller can skip that duration rather than compute
    nonsense from it.
    """
    if value is None or value is pd.NaT:
        return None
    if isinstance(value, datetime):
        return value
    if hasattr(value, 'to_pydatetime'):
        try:
            return value.to_pydatetime()
        except (ValueError, AttributeError):
            return None
    return None


def _elapsed_hours(start, end):
    """Hours between two timestamps, or None if either is missing.

    Negative results return None rather than a negative number. A repair
    that finishes before it starts is a data-entry error, not a real
    duration, and letting it through would drag any mean it lands in
    below the truth. None keeps it out of the average entirely, which is
    the same reasoning as mttr_hrs being NULLable in the database.
    """
    if start is None or end is None:
        return None
    delta = (end - start).total_seconds() / 3600
    if delta < 0:
        return None
    return round(delta, 4)


def parse_downtime_file(filepath):
    df = pd.read_excel(filepath, header=None)
    records = []
    for _, row in df.iterrows():
        vals = list(row)
        v0 = str(vals[COL_WO]).strip() if vals[COL_WO] is not None else ''
        if not re.match(r'^\d{6}$', v0):
            continue

        asset = str(vals[COL_ASSET]).strip() if vals[COL_ASSET] else ''
        downtime_str = (
            str(vals[COL_DOWNTIME]).strip()
            if len(vals) > COL_DOWNTIME and vals[COL_DOWNTIME] else ''
        )

        get = lambda c: _as_datetime(vals[c]) if len(vals) > c else None
        breakdown = get(COL_BREAKDOWN)
        reported = get(COL_REPORTED)
        started = get(COL_STARTED)
        finished = get(COL_FINISHED)
        online = get(COL_ONLINE)

        records.append({
            'wo': v0,
            'asset': asset,

            # Unchanged — reconciliation.py and the monthly report read these.
            'downtime_hrs': dhm_to_hours(downtime_str),
            'downtime_raw': downtime_str,

            # Derived from the timestamps. Any of these can be None when
            # the underlying cells are blank or inconsistent.
            'mtta_hrs': _elapsed_hours(reported, started),
            'mttr_hrs': _elapsed_hours(started, finished),
            'mdt_hrs': _elapsed_hours(breakdown, online),

            # Kept so callers can audit or re-derive without re-reading
            # the file. ISO strings rather than datetimes so a record
            # stays JSON-serialisable straight out of the parser.
            'breakdown_at': breakdown.isoformat() if breakdown else None,
            'reported_at': reported.isoformat() if reported else None,
            'started_at': started.isoformat() if started else None,
            'finished_at': finished.isoformat() if finished else None,
            'online_at': online.isoformat() if online else None,

            'job_type': '',
            'status': '',
            'desc': '',
            'asset_name': '',
        })
    return records
