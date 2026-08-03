"""
Daily View calculations — the replacement for Maintenance Daily's own
stat-card logic, built on the reconciler's already-tested parsers and
config instead of a second, separate set of rules.

Scope decision (confirmed with Andreas): whole site, not press-only —
this covers everything Maintenance/Electrician touch day to day
(presses AND chillers, compressors, etc.), with each WO additionally
tagged `known_machine` so the UI can flag which ones are part of the
monthly board story.

MTTR is calculated for real when Down Time Analysis is provided (see
compute_daily_summary below) — matched by WO number against the
breakdown jobs already filtered to Maintenance/Electrician craft, so
it can't pick up the same class of bug Maintenance Daily's own MTTR
had (that one summed DTA events sitewide, unfiltered by craft or job
type). If Down Time Analysis isn't uploaded, mttr_hrs comes back None
and the UI falls back to manual entry.

WHAT 'MTTR' MEANS HERE, AND WHAT IT USED TO MEAN
------------------------------------------------
This used to read the DTA 'Down Time' column, which is
Breakdown -> OnLine — the machine's TOTAL unavailability, including
however long it sat waiting before anyone started. That's MDT, not
MTTR, and on June 2026 data the difference is not cosmetic:

    MTTA  Reported  -> Started    23.45 h   waiting
    MTTR  Started   -> Finished   18.97 h   actual repair
    MDT   Breakdown -> OnLine     42.83 h   what was being reported

So the old figure was roughly 2.3x the real repair time. All three are
now reported separately, because the gap between them IS the finding:
more time is spent waiting for work to start than doing it.

Note the craft filter does the toolroom exclusion by itself — asset
type is NOT used for this. The DTA export has no craft column at all
(it's run with Job Type: ALL), so a WO raised against a tool asset by
a maintenance technician is still maintenance work, and a WO against a
press by a toolmaker is not. Craft comes from the WO export and is
already applied to wo_data before it reaches here. Filtering on asset
codes as a proxy would get both of those cases wrong.
"""
from config import (
    BREAKDOWN_JOB_TYPE, PLANNED_JOB_TYPES_DAILY, PROJECT_JOB_TYPES_DAILY,
    is_known_machine,
)


def _is_completed(job):
    return 'complet' in (job.get('status') or '').lower()


def compute_daily_summary(wo_data, downtime_data=None):
    """wo_data: output of parsers.wo_parser.parse_wo_file_all_types() —
    every Maintenance/Electrician-craft WO, any job type. Returns the
    bucketed counts the Daily View's stat cards need.

    downtime_data (optional): output of parsers.downtime_parser.parse_downtime_file()
    — the Down Time Analysis export. If provided, MTTR is calculated
    properly: only for today's Breakdown Repair WOs, matched to their
    real timestamped duration by WO number. This is deliberately NOT
    the same shortcut Maintenance Daily's own MTTR took (summing every
    DTA row sitewide, unfiltered by craft or job type) — a breakdown
    with no matching DTA entry is tracked as unmatched, not silently
    counted as zero duration."""
    for w in wo_data:
        w['known_machine'] = is_known_machine(w['asset'])

    total = len(wo_data)

    def _job_type(w):
        return (w.get('jobType') or '').strip().lower()

    breakdown_jobs = [w for w in wo_data if _job_type(w) == BREAKDOWN_JOB_TYPE]
    planned_jobs = [w for w in wo_data if _job_type(w) in PLANNED_JOB_TYPES_DAILY]
    project_jobs = [w for w in wo_data if _job_type(w) in PROJECT_JOB_TYPES_DAILY]

    known_types = {BREAKDOWN_JOB_TYPE} | PLANNED_JOB_TYPES_DAILY | PROJECT_JOB_TYPES_DAILY
    other_jobs = [w for w in wo_data if _job_type(w) not in known_types]

    breakdown_completed = [w for w in breakdown_jobs if _is_completed(w)]
    planned_completed = [w for w in planned_jobs if _is_completed(w)]
    project_completed = [w for w in project_jobs if _is_completed(w)]

    pct_completion = (
        round(len(planned_completed) / len(planned_jobs) * 100)
        if planned_jobs else None
    )

    press_count = sum(1 for w in wo_data if w['known_machine'])

    mttr_hrs = None
    mtta_hrs = None
    mdt_hrs = None
    mttr_matched = 0
    mttr_unmatched = 0
    if downtime_data is not None:
        # Keyed on the whole record now, not one column, so all three
        # durations come from the same matched row.
        dt_by_wo = {d['wo']: d for d in downtime_data}

        repair, wait, total_dt = [], [], []
        for job in breakdown_jobs:
            row = dt_by_wo.get(job['jobNo'])
            if row is None:
                mttr_unmatched += 1
                continue
            # A row can match on WO number but still have an unusable
            # timestamp pair (blank cell, or Finished before Started).
            # It only counts as matched if the repair duration is real,
            # so mttr_matched stays an honest denominator.
            if row.get('mttr_hrs') is not None:
                repair.append(row['mttr_hrs'])
                mttr_matched += 1
            else:
                mttr_unmatched += 1
            if row.get('mtta_hrs') is not None:
                wait.append(row['mtta_hrs'])
            if row.get('mdt_hrs') is not None:
                total_dt.append(row['mdt_hrs'])

        mean = lambda L: round(sum(L) / len(L), 2) if L else None
        mttr_hrs = mean(repair)
        mtta_hrs = mean(wait)
        mdt_hrs = mean(total_dt)

    return {
        'total_wos': total,
        'press_machine_wos': press_count,
        'sitewide_wos': total - press_count,
        'breakdowns': {
            'total': len(breakdown_jobs),
            'completed': len(breakdown_completed),
            'jobs': breakdown_jobs,
        },
        'planned': {
            'total': len(planned_jobs),
            'completed': len(planned_completed),
            'pct_completion': pct_completion,
            'jobs': planned_jobs,
        },
        'project_ci': {
            'total': len(project_jobs),
            'completed': len(project_completed),
            'open': len(project_jobs) - len(project_completed),
            'jobs': project_jobs,
        },
        'other': {
            'total': len(other_jobs),
            'jobs': other_jobs,
        },
        'mttr_hrs': mttr_hrs,
        'mtta_hrs': mtta_hrs,
        'mdt_hrs': mdt_hrs,
        'mttr_matched': mttr_matched,
        'mttr_unmatched': mttr_unmatched,
        'all_jobs': wo_data,
    }
