"""
Parser for the Agility "Selective Work Orders" export.

Layout is block-structured, not tabular: each WO spans several rows
(Job No / Asset / Job Type / Job Desc / Std Hours / one or more Task
rows). This walks each block, pulls out the fields, and only keeps WOs
where a Maintenance/Electrician-craft resource is on the task list AND
the job type is one of the maintenance job types.

Machine-scope filtering (is this asset one SFC actually tracks?) does
NOT happen here — that's reconciliation.py's job, same as in
downtime_parser.py. This file only answers "is this a real Maintenance
WO", not "does it matter for the press-fault gap report".
"""
from config import MAINTENANCE_CRAFTS, MAINTENANCE_JOB_TYPES, TOOLROOM_CRAFTS, PLANNED_JOB_TYPES_DAILY


def _clean(val):
    s = str(val).strip().strip("'")
    return '' if s in ('nan', 'None', '') else s


def _find_in_row(row, label):
    """Find `label` in row, return the next non-blank value after it."""
    for idx, cell in enumerate(row):
        if str(cell).strip().lower() == label.lower():
            for offset in range(1, 4):
                if idx + offset < len(row):
                    val = _clean(row[idx + offset])
                    if val:
                        return val
    return ''


def _collect_block_crafts(rows, start_idx):
    """Walk forward from a WO's block collecting every TASKxx resource
    row, stopping at the next Job No or a fully blank separator row.
    Returns (resources, crafts, next_index_to_resume_from)."""
    resources, crafts = [], []
    j = start_idx
    while j < len(rows):
        r = rows[j]
        first = _clean(r[0]) if len(r) > 0 else ''
        if first == 'Job No':
            break
        if first.upper().startswith('TASK') and first.upper() != 'TASK':
            resource = _clean(r[1]) if len(r) > 1 else ''
            if resource and resource.lower() != 'resource':
                resources.append(resource)
                crafts.append(resource.split(' ')[0].strip().lower())
        if all(_clean(c) == '' for c in r):
            j += 1
            break
        j += 1
    return resources, crafts, j


def _parse_all_maintenance_wos(filepath, craft_filter=MAINTENANCE_CRAFTS):
    """Shared core: walks every WO block, returns every WO whose task
    list carries one of `crafts`, regardless of job type, plus
    asset_lookup.

    `crafts` defaults to Maintenance/Electrician so every existing
    caller behaves exactly as before. It exists so the same block-walk
    can also count Toolmaker WOs without a second copy of the parsing
    logic — the thing this module's docstring already warns about.
    Both parse_wo_file() (job-type-restricted, for the monthly board
    reconciliation) and parse_wo_file_all_types() (every job type, for
    the Daily View) build on this same walk, so there's only one place
    that has to get the block-parsing right."""
    import pandas as pd
    df = pd.read_excel(filepath, header=None)
    rows = df.values.tolist()
    records = []
    asset_lookup = {}

    i = 0
    while i < len(rows):
        row = rows[i]
        if _clean(row[0]) != 'Job No' or not row[1]:
            i += 1
            continue

        job_no = _clean(row[1])
        block = [rows[i + j] if i + j < len(rows) else [] for j in range(1, 6)]
        asset_row, type_row, desc_row = block[0], block[1], block[2]

        asset_code = _clean(asset_row[1]) if len(asset_row) > 1 else ''
        asset_name = _clean(asset_row[3]).replace('\\n', ' ').replace('\n', ' ') if len(asset_row) > 3 else ''
        job_type = _find_in_row(type_row, 'Job Type') or (_clean(type_row[1]) if len(type_row) > 1 else '')
        desc = _clean(desc_row[1]) if len(desc_row) > 1 else ''

        status = ''
        for b_row in block:
            status = _find_in_row(b_row, 'Status')
            if status:
                break

        if asset_code and asset_name:
            asset_lookup[asset_code] = asset_name

        resources, block_crafts, next_i = _collect_block_crafts(rows, i + 1)
        i = next_i - 1  # resume scan right after this job's block

        if any(c in craft_filter for c in block_crafts):
            records.append({
                'jobNo': job_no,
                'asset': asset_code,
                'assetName': asset_name,
                'jobType': job_type,
                'status': status,
                'desc': desc,
                'craft': ', '.join(sorted(set(block_crafts))),
                'resource': ', '.join(resources),
            })

        i += 1

    return records, asset_lookup


def parse_wo_file(filepath):
    """Maintenance/Electrician-craft WOs, restricted to MAINTENANCE_JOB_TYPES
    only. Used by the monthly board reconciliation — unchanged behavior
    from before this refactor, verified against real June data."""
    records, asset_lookup = _parse_all_maintenance_wos(filepath)
    filtered = [
        r for r in records
        if not r['jobType'] or r['jobType'].lower() in MAINTENANCE_JOB_TYPES
    ]
    return filtered, asset_lookup


def parse_wo_file_for_toolroom_gap(filepath):
    """Toolmaker-craft WOs, restricted to MAINTENANCE_JOB_TYPES — the
    Toolroom equivalent of parse_wo_file(). Kept separate from
    parse_toolroom_wo_file() on purpose: that one deliberately returns
    every job type for the Toolroom backlog card, and narrowing it
    would move a number already on the board. This one feeds the new
    Toolroom SFC-vs-Agility gap instead, which — like the Maintenance
    gap — should only count WOs that are actually repair work, not
    every WO a Toolmaker's name appears on."""
    records, asset_lookup = _parse_all_maintenance_wos(filepath, craft_filter=TOOLROOM_CRAFTS)
    filtered = [
        r for r in records
        if not r['jobType'] or r['jobType'].lower() in MAINTENANCE_JOB_TYPES
    ]
    return filtered, asset_lookup


def parse_toolroom_wo_file(filepath):
    """Toolmaker-craft WOs, every job type. Used only for the Toolroom
    card on the board review.

    Deliberately a separate call over the same file rather than a change
    to parse_wo_file(): that function feeds the SFC-vs-Agility gap, a
    number already presented to the board, and widening what it returns
    would move it."""
    records, _ = _parse_all_maintenance_wos(filepath, craft_filter=TOOLROOM_CRAFTS)
    return records


def parse_wo_file_all_types(filepath):
    """Maintenance/Electrician-craft WOs, EVERY job type included — no
    restriction. Used by the Daily View, which needs to see and bucket
    Breakdown/Planned/Project-CI/Other itself, not just the monthly
    reconciler's narrower scope."""
    return _parse_all_maintenance_wos(filepath)


def summarise_ppm_completion(records):
    """Of the Maintenance/Electrician PPM-type jobs raised this period,
    what fraction were actually completed — the board's "TPM Schedule
    Completion" figure. Tracked on other board slides already; this
    reconciler never surfaced it before now.

    records must come from parse_wo_file_all_types(), not parse_wo_file()
    — the latter is filtered to MAINTENANCE_JOB_TYPES only, which is
    missing 'tool preventative maintenance' entirely (see the set
    mismatch note below), so it would silently undercount.

    Deliberately keys on PLANNED_JOB_TYPES_DAILY rather than
    MAINTENANCE_JOB_TYPES (used elsewhere in this file for the gap
    report) — the two sets disagree on two job types, not just
    overlap imperfectly:
      - 'corrective maintenance' is in MAINTENANCE_JOB_TYPES but the
        Daily View's own categorisation deliberately does NOT treat it
        as planned work.
      - 'tool preventative maintenance' is in PLANNED_JOB_TYPES_DAILY
        but absent from MAINTENANCE_JOB_TYPES entirely.
    Using the wrong set here would count corrective (reactive) work as
    if it were scheduled PPM, or silently drop real PPM jobs — either
    way the wrong number for a board-facing completion rate.

    Same status-matching as the Daily View (a substring check on
    'complet', not an exact match) rather than reinventing a slightly
    different rule for what "completed" means.
    """
    def _job_type(w):
        return (w.get('jobType') or '').strip().lower()

    def _is_completed(w):
        return 'complet' in (w.get('status') or '').lower()

    ppm_jobs = [w for w in records if _job_type(w) in PLANNED_JOB_TYPES_DAILY]
    completed = sum(1 for w in ppm_jobs if _is_completed(w))
    total = len(ppm_jobs)

    return {
        'total': total,
        'completed': completed,
        'pct': round(completed / total * 100, 1) if total else None,
    }
