"""
Open Jobs — the Monday look at what Maintenance still has open in Agility.

Reads Agility's Selective Work Orders export run with EVERY status (Job
Status (NULL)), Start Date not set, Due Date 01/01/2025 to 31/12 of this
year. Agility has no single "open" status — unfinished jobs show up as
Accepted Job, Scheduled, Engineer On Site, Work Suspended and so on (the
13 Oct 2026 check found "Open" on its own matched nothing), so the export
brings back everything and this file drops the finished ones itself.

The block-by-block reading of the export is NOT repeated here: it's
wo_parser._parse_all_maintenance_wos(), the same walk the Daily Check
uses every morning, called with craft_filter=None so jobs nobody has
been allocated to yet come back too.

What counts:
  - OPEN: any status that isn't completed or cancelled (see FINISHED_WORDS).
  - MAINTENANCE'S: a Maintenance or Electrician resource on the task list,
    or nobody at all yet ("Unallocated" — someone raised it and nobody has
    picked it up, which is exactly what this check is for). Toolroom jobs
    are Scott's and stay out, counted on one line so nothing is hidden.
  - OVERDUE: due before today, by calendar day — a job due today isn't
    late until tomorrow. Same rule as the TPM on-time %.
  - DUE SOON: due today or in the next DUE_SOON_DAYS days. Agility creates
    PPM jobs about two weeks ahead, which is why the check is weekly.
"""
from datetime import date, datetime, timedelta

from config import MAINTENANCE_CRAFTS, TOOLROOM_CRAFTS, PLANNED_JOB_TYPES_DAILY, PERSONNEL_PPM_EXCLUDED_EMPLOYEES
from parsers.wo_parser import _parse_all_maintenance_wos

# A status containing any of these means the job is finished. Substring
# match, like the Daily Check's 'complet', so "Completed", "Job Complete"
# and "Cancelled"/"Canceled" all count.
FINISHED_WORDS = ('complet', 'cancel', 'closed')

DUE_SOON_DAYS = 14


def is_open(status):
    s = (status or '').lower()
    return not any(word in s for word in FINISHED_WORDS)


def _person(resource):
    """'Electrician George Boyle' -> 'George Boyle'. The first word of a
    resource is its craft (wo_parser reads it the same way). A resource
    that's only a craft, with no name after it, isn't a person."""
    name = resource.split(' ', 1)[1].strip() if ' ' in resource else ''
    if name.isupper():
        name = name.title()
    if name and name.upper() in PERSONNEL_PPM_EXCLUDED_EMPLOYEES:
        name += ' (left)'     # still allocated to a leaver — needs moving to someone
    return name


def _team(resources):
    """Which side a job belongs to, from the crafts on its task list:
    'maintenance', 'unallocated', 'toolroom' or 'other' (Quality, Manager...).
    A job with a Maintenance/Electrician resource is Maintenance's even if a
    toolmaker is on it too."""
    crafts = {r.split(' ')[0].strip().lower() for r in resources}
    if not crafts:
        return 'unallocated'
    if crafts & set(MAINTENANCE_CRAFTS):
        return 'maintenance'
    if crafts & set(TOOLROOM_CRAFTS):
        return 'toolroom'
    return 'other'


def _due_date(due):
    """wo_parser hands the due date over as 'YYYY-MM-DDTHH:MM', or ''."""
    try:
        return datetime.fromisoformat(due).date() if due else None
    except ValueError:
        return None


def summarise_open_jobs(filepath, as_of=None):
    """Everything the Open Jobs page shows for one export, as of `as_of`
    (a date; today at Clamason when not given). Raises ValueError with a
    plain reason when the file can't be the right export."""
    if as_of is None:
        from reminders import london_today
        as_of = london_today()
    soon_until = as_of + timedelta(days=DUE_SOON_DAYS)

    try:
        records, _ = _parse_all_maintenance_wos(filepath, craft_filter=None)
    except Exception as e:
        raise ValueError(
            f'Couldn\'t read this file as an Excel export ({e}). Upload the .xlsx file '
            f'Agility gives you for Selective Work Orders.')
    if not records:
        raise ValueError(
            'No jobs found in this file. Check it\'s the Selective Work Orders export, '
            'and that Job Status is (NULL) and Start Date is Not Set — either can empty it.')
    if not any(r.get('status') for r in records):
        raise ValueError(
            'None of the jobs in this file has a Status, so open and finished jobs '
            'can\'t be told apart. Is this the Selective Work Orders export?')

    dues = [d for d in (_due_date(r.get('due')) for r in records) if d]
    jobs, not_counted, finished = [], {'toolroom': 0, 'other': 0}, 0

    for r in records:
        if not is_open(r.get('status')):
            finished += 1
            continue
        resources = [x for x in (r.get('resource') or '').split(', ') if x.strip()]
        team = _team(resources)
        if team in not_counted:
            not_counted[team] += 1
            continue

        people = []
        for res in resources:
            if res.split(' ')[0].strip().lower() in MAINTENANCE_CRAFTS:
                name = _person(res)
                if name and name.lower() not in {p.lower() for p in people}:
                    people.append(name)

        due = _due_date(r.get('due'))
        jobs.append({
            'jobNo': r.get('jobNo'),
            'due': due.isoformat() if due else None,
            'asset': r.get('asset'),
            'assetName': r.get('assetName'),
            'jobType': r.get('jobType'),
            'status': r.get('status'),
            'desc': r.get('desc'),
            'people': people or ['Unallocated'],
            'unallocated': team == 'unallocated',
            'overdue': bool(due and due < as_of),
            'daysOverdue': (as_of - due).days if due and due < as_of else 0,
            'dueSoon': bool(due and as_of <= due <= soon_until),
            'ppm': (r.get('jobType') or '').strip().lower() in PLANNED_JOB_TYPES_DAILY,
        })

    # Overdue first (oldest first), then by due date; no due date last.
    jobs.sort(key=lambda j: (j['due'] is None, j['due'] or '', j['jobNo'] or ''))

    by_person = {}
    for j in jobs:
        for p in j['people']:
            row = by_person.setdefault(p, {'person': p, 'open': 0, 'overdue': 0})
            row['open'] += 1
            row['overdue'] += 1 if j['overdue'] else 0

    overdue = [j for j in jobs if j['overdue']]
    warnings = []
    latest_due = max(dues) if dues else None
    if latest_due and latest_due < as_of:
        warnings.append(
            f'The latest due date in this export is {latest_due:%d/%m/%Y}, before today, so jobs '
            f'due from today onwards are missing. Run it with Due Date To 31/12/{as_of.year}.')

    return {
        'as_of': as_of.isoformat(),
        'week_start': (as_of - timedelta(days=as_of.weekday())).isoformat(),
        'jobs_in_file': len(records),
        'finished_in_file': finished,
        'due_from': min(dues).isoformat() if dues else None,
        'due_to': latest_due.isoformat() if latest_due else None,
        'open_total': len(jobs),
        'overdue': len(overdue),
        'due_soon': sum(1 for j in jobs if j['dueSoon']),
        'no_due_date': sum(1 for j in jobs if not j['due']),
        'open_ppm': sum(1 for j in jobs if j['ppm']),
        'overdue_ppm': sum(1 for j in overdue if j['ppm']),
        'unallocated': sum(1 for j in jobs if j['unallocated']),
        'oldest_overdue': overdue[0]['due'] if overdue else None,
        'by_person': sorted(by_person.values(), key=lambda p: (-p['open'], p['person'])),
        'not_counted': not_counted,
        'warnings': warnings,
        'jobs': jobs,
    }
