"""
Rollup calculations for the Daily View's saved history (daily_snapshots)
into weekly and monthly aggregates, for the Daily Trend view.

Same principle as the daily numbers themselves: sum the raw totals for
every day in the period FIRST, then calculate one percentage / one MTTR
for the whole period — never average each day's own percentage, which
would quietly skew things on days with very different job counts.
"""
from collections import OrderedDict
from datetime import datetime, timedelta

SUM_FIELDS = [
    'total_wos', 'press_machine_wos', 'sitewide_wos',
    'breakdowns_total', 'breakdowns_completed',
    'planned_total', 'planned_completed',
    'project_ci_total', 'project_ci_completed',
    'other_total',
]


def _parse_date(d):
    return datetime.strptime(d, '%Y-%m-%d').date() if isinstance(d, str) else d


def _week_key(date_obj):
    iso_year, iso_week, _ = date_obj.isocalendar()
    monday = date_obj - timedelta(days=date_obj.weekday())
    return f'{iso_year}-W{iso_week:02d}', monday.isoformat()


def _month_key(date_obj):
    return date_obj.strftime('%Y-%m'), date_obj.strftime('%B %Y')


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


def _rollup(snapshots, key_fn):
    buckets = OrderedDict()
    for row in snapshots:
        date_obj = _parse_date(row['date'])
        key, label = key_fn(date_obj)
        if key not in buckets:
            buckets[key] = _empty_bucket(key, label)
        _add_row(buckets[key], row)
    return [_finalize(b) for b in buckets.values()]


def weekly_rollup(snapshots):
    """snapshots: output of db.get_daily_snapshots(), oldest first.
    Returns one row per ISO week (Mon–Sun), oldest first."""
    return _rollup(snapshots, _week_key)


def monthly_rollup(snapshots):
    """snapshots: output of db.get_daily_snapshots(), oldest first.
    Returns one row per calendar month, oldest first."""
    return _rollup(snapshots, _month_key)
