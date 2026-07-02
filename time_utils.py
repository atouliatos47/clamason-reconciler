"""
One shared set of duration parsers, used everywhere in the reconciler.

The old server.py had three separate hand-rolled versions of essentially
the same "turn a text duration into hours" logic (parse_time_to_hours,
parse_downtime_str, and parse_hms redefined inline in three different
functions). Keeping only one copy here means a fix only has to happen
once, and every parser stays consistent with every other parser.
"""
import re


def hms_to_hours(s):
    """'109:51:12' (H:MM:SS, H can exceed 24) -> hours as float.
    Used by: SFC downtime summary parsers (daily PDF + monthly xlsx)."""
    if not isinstance(s, str):
        return 0
    parts = s.strip().split(':')
    if len(parts) != 3:
        return 0
    try:
        h, m, sec = parts
        return int(h) + int(m) / 60 + float(sec) / 3600
    except (ValueError, TypeError):
        return 0


def dhm_to_hours(s):
    """'1d 03h 16m' / '01d03h16m' style strings -> hours as float.
    Used by: Agility Down Time Analysis 'Down Time' column.

    Some Agility exports contain malformed negative durations like
    '-17h-01m' (a data-entry glitch where Started/Finished timestamps
    don't add up on that WO). The old regex-only parser silently
    stripped the minus sign and returned +17.02 — a wrong number that
    looked valid. This version detects a leading '-' and returns 0
    instead, since a negative downtime isn't a real value to sum."""
    if not isinstance(s, str):
        return 0
    s = s.strip()
    if s.startswith('-'):
        return 0  # malformed/invalid duration — do not silently flip sign
    total = 0
    d = re.search(r'(\d+)d', s)
    h = re.search(r'(\d+)h', s)
    m = re.search(r'(\d+)m', s)
    if d:
        total += int(d.group(1)) * 24
    if h:
        total += int(h.group(1))
    if m:
        total += int(m.group(1)) / 60
    return round(total, 2)


def excel_time_to_hours(v):
    """Excel numeric time-of-day (0.0-1.0 fraction of a day) or an
    already-numeric hours value -> hours as float.
    Used by: OEE weekly xls parser."""
    if v is None:
        return 0
    try:
        import numpy as np
        if isinstance(v, float) and np.isnan(v):
            return 0
    except ImportError:
        pass
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0
