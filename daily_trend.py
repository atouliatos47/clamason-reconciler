"""
Rollup calculations for the Daily View's saved history (daily_snapshots)
and the SFC Daily Downtime history (sfc_daily_snapshots) into weekly and
monthly aggregates, for the Daily Trend and SFC Daily Trend views.

Same principle throughout, and the reason this module exists at all:
sum the raw totals for every day in the period FIRST, then calculate one
percentage / one MTTR for the whole period — never average each day's own
percentage, which would quietly skew things on days with very different
job counts or very different amounts of running time.

The two halves are deliberately kept separate. daily_snapshots is Agility
work-order counts; sfc_daily_snapshots is SFC downtime hours. They come
from different systems and either can be uploaded without the other, so
they roll up independently and neither is required for the other to work.
"""
from collections import OrderedDict
from datetime import datetime, timedelta

# Reused rather than reimplemented — see this module's own docstring on
# why summed-then-ratioed beats averaged-percentages, and _compute()'s
# docs for exactly what it does. A second implementation of the same
# OEE math here would be precisely the trap this file exists to avoid.
from parsers.oee_parser import _compute as _oee_compute

# --- Agility Daily View fields ---------------------------------------------
SUM_FIELDS = [
    'total_wos', 'press_machine_wos', 'sitewide_wos',
    'breakdowns_total', 'breakdowns_completed',
    'planned_total', 'planned_completed',
    'project_ci_total', 'project_ci_completed',
    'other_total',
]

# --- SFC Daily Downtime fields ---------------------------------------------
# Every one of these is genuinely additive across days: hours lost on
# Monday plus hours lost on Tuesday is hours lost over the two days.
#
# machine_count is NOT in this list on purpose. It's how many machines SFC
# was monitoring that day, not a quantity of anything — summing it over a
# 5-day week would report 95 machines on a 19-machine site. It's taken as a
# max instead (see _sfc_add_row), so adding a machine mid-week is reflected
# without inventing four extra sites.
SFC_SUM_FIELDS = [
    'total_events', 'total_hrs',
    'maintenance_hrs', 'toolroom_hrs', 'production_hrs',
    'period_hrs', 'max_possible_hrs',
    'planned_offline_hrs', 'scheduled_hrs',
]


def _parse_date(d):
    return datetime.strptime(d, '%Y-%m-%d').date() if isinstance(d, str) else d


def _day_key(date_obj):
    return date_obj.isoformat(), date_obj.strftime('%a %d %b')


def _week_key(date_obj):
    iso_year, iso_week, _ = date_obj.isocalendar()
    monday = date_obj - timedelta(days=date_obj.weekday())
    # Both the week number and the actual start date in the label —
    # some people think in "week 33", others in "week of 10 Aug", and
    # showing both avoids anyone needing to convert between the two in
    # their head. Shared by all three trend pages (Agility Daily,
    # SFC Daily, OEE Daily), so this one change reaches all of them,
    # not just whichever page prompted it.
    return f'{iso_year}-W{iso_week:02d}', f'Week {iso_week} ({monday.strftime("%d %b")})'


def _month_key(date_obj):
    return date_obj.strftime('%Y-%m'), date_obj.strftime('%B %Y')


# ---------------------------------------------------------------------------
# Agility Daily View rollup
# ---------------------------------------------------------------------------

def _empty_bucket(key, label):
    bucket = {'key': key, 'label': label, 'days': 0, 'mttr_matched': 0, '_mttr_weighted_sum': 0.0}
    for f in SUM_FIELDS:
        bucket[f] = 0
    return bucket


def _add_row(bucket, row):
    bucket['days'] += 1
    for f in SUM_FIELDS:
        bucket[f] += row.get(f) or 0
    if row.get('mttr_hrs') is not None and row.get('mttr_matched'):
        bucket['_mttr_weighted_sum'] += row['mttr_hrs'] * row['mttr_matched']
        bucket['mttr_matched'] += row['mttr_matched']


def _finalize(bucket):
    bucket['planned_pct'] = (
        round(bucket['planned_completed'] / bucket['planned_total'] * 100)
        if bucket['planned_total'] else None
    )
    bucket['mttr_hrs'] = (
        round(bucket['_mttr_weighted_sum'] / bucket['mttr_matched'], 2)
        if bucket['mttr_matched'] else None
    )
    del bucket['_mttr_weighted_sum']
    return bucket


# ---------------------------------------------------------------------------
# SFC Daily Downtime rollup
# ---------------------------------------------------------------------------

def _sfc_empty_bucket(key, label):
    bucket = {
        'key': key,
        'label': label,
        'days': 0,
        'first_date': None,
        'last_date': None,
        'machine_count': 0,
        'reasons': {},
        'reason_events': {},
    }
    for f in SFC_SUM_FIELDS:
        bucket[f] = 0.0
    return bucket


def _sfc_add_row(bucket, row):
    bucket['days'] += 1

    d = row.get('date')
    if d is not None:
        d = d if isinstance(d, str) else d.isoformat()
        if bucket['first_date'] is None or d < bucket['first_date']:
            bucket['first_date'] = d
        if bucket['last_date'] is None or d > bucket['last_date']:
            bucket['last_date'] = d

    for f in SFC_SUM_FIELDS:
        bucket[f] += row.get(f) or 0

    # Not summed — see the SFC_SUM_FIELDS note.
    bucket['machine_count'] = max(bucket['machine_count'], row.get('machine_count') or 0)

    # Merge the per-day Pareto dicts by summing each reason across the
    # period. This is what lets the trend view rebuild a weekly or monthly
    # Pareto without re-uploading a single PDF.
    for src, dest in (('reasons', 'reasons'), ('reason_events', 'reason_events')):
        day_map = row.get(src) or {}
        if isinstance(day_map, dict):
            for reason, val in day_map.items():
                bucket[dest][reason] = bucket[dest].get(reason, 0) + (val or 0)


def _sfc_finalize(bucket):
    for f in SFC_SUM_FIELDS:
        bucket[f] = round(bucket[f], 2)

    # Buckets accumulate as floats so the hour fields don't lose precision,
    # but an event is a count — hand it back as an int so the UI renders
    # "41 events", not "41.0 events".
    bucket['total_events'] = int(bucket['total_events'])

    total = bucket['total_hrs']
    scheduled = bucket['scheduled_hrs']

    # total_hrs is EVERY downtime reason including Planned Offline and No
    # Production Planned. scheduled_hrs has already had those same planned
    # hours subtracted out (max_possible - planned_offline). Dividing one by
    # the other therefore double-counts planned time and can exceed 100% —
    # on real Clamason data, 388.76 / 136.8 = 284%, which is nonsense.
    #
    # Unplanned downtime is the honest numerator: what was lost during time
    # the site was actually scheduled to be running.
    bucket['unplanned_hrs'] = round(total - bucket['planned_offline_hrs'], 2)
    unplanned = bucket['unplanned_hrs']

    # Share of ALL downtime — matches the "% of Total" column on the SFC
    # Daily page, so the two views agree on any given reason.
    bucket['maintenance_pct'] = round(bucket['maintenance_hrs'] / total * 100, 1) if total else None
    bucket['toolroom_pct'] = round(bucket['toolroom_hrs'] / total * 100, 1) if total else None
    bucket['production_pct'] = round(bucket['production_hrs'] / total * 100, 1) if total else None

    # Share of UNPLANNED downtime — the number that actually means something
    # for a maintenance conversation. Maintenance is ~1.8% of all downtime
    # but ~10% of unplanned downtime; the second figure is the real one.
    bucket['maintenance_pct_of_unplanned'] = (
        round(bucket['maintenance_hrs'] / unplanned * 100, 1) if unplanned > 0 else None
    )
    bucket['toolroom_pct_of_unplanned'] = (
        round(bucket['toolroom_hrs'] / unplanned * 100, 1) if unplanned > 0 else None
    )

    # How much of the time the site WAS scheduled to run got lost to
    # unplanned stoppages.
    bucket['downtime_pct_of_scheduled'] = (
        round(unplanned / scheduled * 100, 1) if scheduled else None
    )

    # Everything unplanned that wasn't Maintenance or Toolroom — setting,
    # changeover, labour, quality, etc. Derived here rather than in the page
    # so the three stack segments always sum to unplanned_hrs exactly.
    #
    # Note this is NOT production_hrs from the parser: that one is
    # total - maintenance - toolroom, so it still has all the planned
    # offline hours buried in it and would dwarf the chart.
    bucket['other_unplanned_hrs'] = round(
        max(unplanned - bucket['maintenance_hrs'] - bucket['toolroom_hrs'], 0), 2
    )

    # Pareto, biggest loss first — the order the chart draws in.
    events = bucket['reason_events']
    bucket['top_reasons'] = [
        {
            'reason': reason,
            'hrs': round(hrs, 2),
            'events': events.get(reason, 0),
            'pct_of_total': round(hrs / total * 100, 1) if total else None,
        }
        for reason, hrs in sorted(bucket['reasons'].items(), key=lambda kv: -kv[1])
    ]
    bucket['reasons'] = {k: round(v, 2) for k, v in bucket['reasons'].items()}
    return bucket


# ---------------------------------------------------------------------------
# Shared walk
# ---------------------------------------------------------------------------

def _rollup(snapshots, key_fn, empty_fn, add_fn, finalize_fn):
    """One bucketing walk, shared by both the Agility and SFC rollups.
    Parameterised rather than duplicated so a fix to the week/month
    bucketing can only ever have to happen once."""
    buckets = OrderedDict()
    for row in snapshots:
        date_obj = _parse_date(row['date'])
        key, label = key_fn(date_obj)
        if key not in buckets:
            buckets[key] = empty_fn(key, label)
        add_fn(buckets[key], row)
    return [finalize_fn(b) for b in buckets.values()]


def weekly_rollup(snapshots):
    """snapshots: output of db.get_daily_snapshots(), oldest first.
    Returns one row per ISO week (Mon–Sun), oldest first."""
    return _rollup(snapshots, _week_key, _empty_bucket, _add_row, _finalize)


def monthly_rollup(snapshots):
    """snapshots: output of db.get_daily_snapshots(), oldest first.
    Returns one row per calendar month, oldest first."""
    return _rollup(snapshots, _month_key, _empty_bucket, _add_row, _finalize)


def sfc_daily_rollup(snapshots):
    """snapshots: output of db.get_sfc_daily_snapshots(), oldest first.
    One bucket per day — so a single day gets exactly the same derived
    fields (unplanned_hrs, percentages, top_reasons) as a week or a month.

    Without this the Daily tab would have to recompute all of that in
    JavaScript, which is how the two-implementations-of-one-rule bug gets
    in. A one-row bucket is a trivial rollup, and it keeps every number on
    every tab coming out of the same function."""
    return _rollup(snapshots, _day_key, _sfc_empty_bucket, _sfc_add_row, _sfc_finalize)


def sfc_weekly_rollup(snapshots):
    """snapshots: output of db.get_sfc_daily_snapshots(), oldest first.
    Returns one row per ISO week (Mon–Sun), oldest first."""
    return _rollup(snapshots, _week_key, _sfc_empty_bucket, _sfc_add_row, _sfc_finalize)


def sfc_monthly_rollup(snapshots):
    """snapshots: output of db.get_sfc_daily_snapshots(), oldest first.
    Returns one row per calendar month, oldest first."""
    return _rollup(snapshots, _month_key, _sfc_empty_bucket, _sfc_add_row, _sfc_finalize)


# --- OEE Daily fields --------------------------------------------------
# Every field here is a raw hours or parts count, summable across days —
# deliberately not oee_pct/availability_pct/etc, which can't be. See
# this module's docstring and oee_daily_snapshots' own schema comment.
OEE_SUM_FIELDS = [
    'total_avail_hrs', 'planned_down_hrs', 'net_avail_hrs',
    'unplanned_down_hrs', 'run_time_hrs', 'total_parts',
    'ideal_parts', 'scrap_parts',
]


def _oee_empty_bucket(key, label):
    bucket = {
        'key': key, 'label': label, 'days': 0,
        'first_date': None, 'last_date': None, 'machine_count': 0,
        # Internal accumulator, not part of the bucket's public shape —
        # _oee_finalize() below replaces this with a real 'per_machine'
        # list before returning. Underscore-prefixed as a signal this
        # is scratch space, same convention as elsewhere in this file.
        '_machines': {},
    }
    for f in OEE_SUM_FIELDS:
        bucket[f] = 0.0
    return bucket


def _oee_add_row(bucket, row):
    bucket['days'] += 1

    d = row.get('date')
    if d is not None:
        d = d if isinstance(d, str) else d.isoformat()
        if bucket['first_date'] is None or d < bucket['first_date']:
            bucket['first_date'] = d
        if bucket['last_date'] is None or d > bucket['last_date']:
            bucket['last_date'] = d

    for f in OEE_SUM_FIELDS:
        bucket[f] += row.get(f) or 0

    # Machine count, like the SFC rollup's, is the max seen rather than
    # summed — it's "how many presses this covers," not a quantity that
    # accumulates across days.
    bucket['machine_count'] = max(bucket['machine_count'], row.get('machine_count') or 0)

    # Same sum-raw-then-ratio principle as the fleet totals above,
    # applied per machine name rather than to the fleet as a whole — a
    # machine's weekly OEE% is computed from ITS OWN summed hours and
    # parts across the days it appears, not from averaging its daily
    # percentages. A machine missing from some days in the period (no
    # data that day) simply contributes nothing for those days, the
    # same way it would if it were genuinely idle.
    for m in (row.get('per_machine') or []):
        name = m.get('machine')
        if not name:
            continue
        acc = bucket['_machines'].setdefault(name, {f: 0.0 for f in OEE_SUM_FIELDS})
        for f in OEE_SUM_FIELDS:
            acc[f] += m.get(f) or 0


def _oee_finalize(bucket):
    for f in OEE_SUM_FIELDS:
        bucket[f] = round(bucket[f], 2)

    # oee_parser._compute() reads exactly the OEE_SUM_FIELDS keys above
    # off `bucket` and returns availability_pct/performance_pct/
    # quality_pct/oee_pct/utilization_pct/teep_pct/quality_unavailable —
    # merged straight in, so a week or month gets the identical
    # percentage fields a single day already has.
    bucket.update(_oee_compute(bucket))

    # Per-machine figures for this same period, computed the identical
    # way — reusing _oee_compute() again rather than a second
    # implementation of the same ratio math. Unifies daily, weekly, and
    # monthly onto the same shape: every bucket carries its own
    # 'per_machine' list directly, rather than daily being a special
    # case that looks up a separate raw snapshot elsewhere.
    #
    # _oee_compute() runs on the UNROUNDED acc, not the rounded display
    # row — rounding first and computing second (an earlier version of
    # this exact code did that) introduces small drift, confirmed
    # against real data: 23.11% became 23.16% for one machine purely
    # from rounding raw hours before dividing them, not from any real
    # difference in the underlying figures. Same "full precision for
    # the maths, rounded only for what's displayed" rule the rest of
    # this codebase already follows everywhere else.
    per_machine = []
    for name, acc in bucket['_machines'].items():
        computed = _oee_compute(acc)
        row = {'machine': name}
        for f in OEE_SUM_FIELDS:
            row[f] = round(acc[f], 2)
        row.update(computed)
        per_machine.append(row)
    bucket['per_machine'] = per_machine
    del bucket['_machines']

    return bucket


def oee_daily_rollup(snapshots):
    """snapshots: output of db.get_oee_daily_snapshots(), oldest first.
    One bucket per day — a single day gets exactly the same derived
    percentage fields as a week or a month, computed the same way."""
    return _rollup(snapshots, _day_key, _oee_empty_bucket, _oee_add_row, _oee_finalize)


def oee_weekly_rollup(snapshots):
    """snapshots: output of db.get_oee_daily_snapshots(), oldest first.
    Returns one row per ISO week (Mon-Sun), oldest first."""
    return _rollup(snapshots, _week_key, _oee_empty_bucket, _oee_add_row, _oee_finalize)


def oee_monthly_rollup(snapshots):
    """snapshots: output of db.get_oee_daily_snapshots(), oldest first.
    Returns one row per calendar month, oldest first."""
    return _rollup(snapshots, _month_key, _oee_empty_bucket, _oee_add_row, _oee_finalize)


def attach_production_plan(weekly_buckets, plan_weeks):
    """Merges saved Production Plan weeks onto matching weekly OEE
    buckets, in place. plan_weeks is db.get_production_plan_weeks()'s
    output — each row's week_start is a Monday date; weekly_buckets'
    own 'key' is the ISO 'YYYY-Www' format _week_key() builds every
    other weekly bucket from. Converting the stored Monday into that
    same key is what makes the two line up, rather than trying to
    match on the Monday date directly (the bucket doesn't carry one).

    Every bucket gets plan_quantity/plan_hours added — None on weeks
    with no saved plan, not 0, so the trend page can show a dash
    rather than implying nothing was planned."""
    by_week_key = {}
    for p in plan_weeks:
        d = _parse_date(p['week_start'])
        iso_year, iso_week, _ = d.isocalendar()
        by_week_key[f'{iso_year}-W{iso_week:02d}'] = p

    for bucket in weekly_buckets:
        plan = by_week_key.get(bucket.get('key'))
        bucket['plan_quantity'] = plan['plan_quantity'] if plan else None
        bucket['plan_hours'] = plan['plan_hours'] if plan else None
    return weekly_buckets
