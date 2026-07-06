"""
Daily View calculations — the replacement for Maintenance Daily's own
stat-card logic, built on the reconciler's already-tested parsers and
config instead of a second, separate set of rules.

Scope decision (confirmed with Andreas): whole site, not press-only —
this covers everything Maintenance/Electrician touch day to day
(presses AND chillers, compressors, etc.), with each WO additionally
tagged `known_machine` so the UI can flag which ones are part of the
monthly board story.

MTTR is deliberately NOT calculated here yet. Andreas asked for it to
show as a manual 0 for now, until the Down Time Analysis file can be
properly cross-referenced against Selective Work Orders for craft —
the same class of bug we found in Maintenance Daily's own MTTR
calculation (it summed DTA events unfiltered by craft or job type).
"""
from config import (
    BREAKDOWN_JOB_TYPE, PLANNED_JOB_TYPES_DAILY, PROJECT_JOB_TYPES_DAILY,
    is_known_machine,
)


def _is_completed(job):
    return 'complet' in (job.get('status') or '').lower()


def compute_daily_summary(wo_data):
    """wo_data: output of parsers.wo_parser.parse_wo_file_all_types() —
    every Maintenance/Electrician-craft WO, any job type. Returns the
    bucketed counts the Daily View's stat cards need."""
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
        'mttr_hrs': None,  # manual entry for now — see module docstring
        'all_jobs': wo_data,
    }
