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
from config import MAINTENANCE_CRAFTS, MAINTENANCE_JOB_TYPES


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


def _parse_all_maintenance_wos(filepath):
    """Shared core: walks every WO block, returns ALL Maintenance/
    Electrician-craft WOs regardless of job type, plus asset_lookup.
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

        resources, crafts, next_i = _collect_block_crafts(rows, i + 1)
        i = next_i - 1  # resume scan right after this job's block

        is_maintenance_craft = any(c in MAINTENANCE_CRAFTS for c in crafts)
        if is_maintenance_craft:
            records.append({
                'jobNo': job_no,
                'asset': asset_code,
                'assetName': asset_name,
                'jobType': job_type,
                'status': status,
                'desc': desc,
                'craft': ', '.join(sorted(set(crafts))),
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


def parse_wo_file_all_types(filepath):
    """Maintenance/Electrician-craft WOs, EVERY job type included — no
    restriction. Used by the Daily View, which needs to see and bucket
    Breakdown/Planned/Project-CI/Other itself, not just the monthly
    reconciler's narrower scope."""
    return _parse_all_maintenance_wos(filepath)
